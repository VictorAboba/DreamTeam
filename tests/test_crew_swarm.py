"""Crew tasks at the swarm level (docs/SPEC.md §3 items 1-3 and 8, §4.2 "Offers", Liveness and "A free place", §6.2):
offers keyed by the pair (task id, finish state), the liveness key (stalled ids, marks) of the scheduler's items 2 and
3, the owner's notice of a free place nobody can fill (item 8), the in-activation nudge of a crew task's owner, the
user's review reminder, and the seams other parts read (snapshot, events, settings, session statistics, export). Crew
tasks are built with tests/helpers/crew.py, straight into the rows; the members are `IdleRuntime`s of a real Swarm
(test_review.py), except for the nudge, which runs a scripted agent."""

from __future__ import annotations

import dataclasses
import sqlite3
import time

import pytest

from dreamteam import prompts
from dreamteam import settings as st
from dreamteam import swarm as swarm_module
from dreamteam.db import Database, loads
from dreamteam.models import USER, AgentStatus, Status
from dreamteam.sessions import read_stats
from dreamteam.swarm import CREW_GAP_FACTOR
from dreamteam.tools import frozen
from dreamteam.tools.fmt import trim
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


async def test_an_owner_whose_own_direction_waits_in_a_full_crew_task_keeps_no_reminder_back(tmp_path):
    """Everyone in the crew, the owner included, waits on a direction: nobody can take the free one, so the user
    is told that only their reviews keep the swarm going."""
    swarm = await live_team(tmp_path, ANNA, BOB, CARL)
    try:
        board = swarm.board
        impl = board.create(USER, "impl", goal="g")
        swarm.set_goal(USER, "Ship it")
        crew, (d1, d2, d3) = make_crew_raw(board, USER, 2, ["d1", "d2", "d3"], title="Tool", goal="g")
        set_raw(board, crew.id, status=Status.IN_PROGRESS, assignee=ANNA)
        set_raw(board, d1.id, status=Status.WAITING, assignee=ANNA)
        set_raw(board, d2.id, status=Status.WAITING, assignee=BOB)
        finish_raw(board, impl.id, Status.REVIEW, "done", assignee=CARL, reviewer=USER)
        deliver(swarm)
        assert [t.id for t in board.free_directions(board.get(crew.id))] == [d3.id]
        assert board.stall_marks(ANNA) == frozenset() and board.ready_unassigned() == []
        assert swarm._has_task_in_progress() is False and swarm._has_actionable_todo() is False
        swarm._remind_user_reviews()
        assert kinds_for(swarm, USER).count("u_reviews_waiting") == 1
    finally:
        await shutdown(swarm)


async def test_a_direction_is_not_offered_to_a_member_that_works_on_another_of_its_crew_task(tmp_path):
    swarm = await live_team(tmp_path, ANNA, BOB, CARL)
    try:
        board = swarm.board
        crew, (parser, cli, docs) = make_crew_raw(board, USER, 3, ["parser", "cli", "docs"], title="Tool", goal="g")
        swarm.set_goal(USER, "Ship it")
        set_raw(board, crew.id, status=Status.IN_PROGRESS, assignee=ANNA)
        set_raw(board, parser.id, status=Status.IN_PROGRESS, assignee=ANNA)   # the owner's own part
        set_raw(board, cli.id, status=Status.WAITING, assignee=BOB)
        offer_rounds(swarm, 4)
        assert [offered(swarm, n, docs) for n in (ANNA, BOB, CARL)] == [0, 0, 1]
        assert f"#{docs.id} docs" not in swarm.render_wake(ANNA) and f"#{docs.id} docs" in swarm.render_wake(CARL)
    finally:
        await shutdown(swarm)


async def test_an_owner_whose_directions_wait_behind_a_chain_to_a_closed_task_is_woken(tmp_path):
    swarm = await live_team(tmp_path, ANNA, BOB)
    try:
        board = swarm.board
        crew, (d1, d2) = make_crew_raw(board, USER, 2, ["d1", "d2"], title="Tool", goal="g")
        failed = board.create(USER, "F", goal="g", assignee=BOB)
        board.update(USER, d1.id, depends_on=[failed.id])
        board.update(USER, d2.id, depends_on=[d1.id])
        set_raw(board, crew.id, status=Status.IN_PROGRESS, assignee=ANNA)
        assert swarm._stall_ids(ANNA) == frozenset()
        finish_raw(board, failed.id, Status.FAILED, assignee=BOB)
        assert swarm._stall_ids(ANNA) == frozenset({crew.id})
        assert swarm.crew_nudge(ANNA, [board.get(crew.id)]) == (set(), "")
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


# ================================================================== a free place (§3 item 8, §4.2 "A free place")

OLGA, DINA, EXT = "Olga Owner", "Dina Dale", "Ext Agent"
ROLES = {OLGA: "Generalist", ANNA: "Developer", BOB: "Generalist", CARL: "Tester", DINA: "Analyst"}
DUE = CREW_GAP_FACTOR * WAIT    # how long a crew task keeps its state before its owner is told


async def gap_team(tmp_path):
    """A live team of the members of ROLES, hired in that order (Olga owns the crew tasks), with room for more."""
    swarm = await live_team(tmp_path, max_agents=12)
    for name, role in ROLES.items():
        await swarm.hire(USER, role, name=name)
        swarm.runtimes[name] = IdleRuntime()
    deliver(swarm)
    return swarm


def engine(swarm, crew: int = 3, owner: str = OLGA, creator: str = USER):
    """Crew task "engine" (crew `crew`, created by `creator`, held in progress by `owner`) with the todo directions
    "search", "eval" and "uci"; the goal is set and the members have read their news."""
    board = swarm.board
    task, subs = make_crew_raw(board, creator, crew, ["search", "eval", "uci"], title="engine", goal="g")
    set_raw(board, task.id, status=Status.IN_PROGRESS, assignee=owner)
    if not swarm.goal():
        swarm.set_goal(USER, "Ship it")
    deliver(swarm)
    return board.get(task.id), subs


def last_direction(swarm, owner: str = OLGA, **kw):
    """The run's second state (§4.2 "A free place"): the owner's "search" and Bob's "eval" are done, Anna works on
    "uci", one place of three is free; Bob, Carl and Dina have nothing to go on with."""
    task, (search, evalu, uci) = engine(swarm, owner=owner, **kw)
    finish_raw(swarm.board, search.id, Status.DONE, "r", assignee=owner)
    finish_raw(swarm.board, evalu.id, Status.DONE, "r", assignee=BOB)
    set_raw(swarm.board, uci.id, status=Status.IN_PROGRESS, assignee=ANNA)
    return swarm.board.get(task.id), (search, evalu, uci)


def rested(swarm, *first: str) -> None:
    """Every member has been idle for a while (the free members' own wait); `first` the longest, in this order."""
    now = time.time()
    swarm.db.execute("UPDATE agents SET last_active = ?", (now - 10 * WAIT,))
    for i, name in enumerate(first):
        swarm.db.execute("UPDATE agents SET last_active = ? WHERE name = ?", (now - 20 * WAIT + i * 0.1, name))


def later(swarm, seconds: float) -> None:
    """Item 8's clocks: each crew task has kept its state `seconds` longer."""
    swarm._crew_gaps = {tid: (key, since - seconds) for tid, (key, since) in swarm._crew_gaps.items()}


def tell_when_due(swarm) -> None:
    """Item 8 sees the states (a clock starts for a new one), and again once they are due."""
    swarm._tell_crew_gaps()
    later(swarm, DUE)
    swarm._tell_crew_gaps()


def gap_notices(swarm, name: str = OLGA) -> list[dict]:
    """The placeholders of each n_crew_gap `name` got, oldest first."""
    return [loads(r["data"]) for r in swarm.db.all("SELECT data FROM notifications WHERE recipient = ? AND kind = ?"
                                                   " ORDER BY id", (name, "n_crew_gap"))]


def free_names(swarm, task) -> list[str]:
    """The free members of `task` now (Swarm._free_members), by name."""
    free = swarm._free_members(swarm.board.get(task.id), swarm._idle_llm_members(), time.time(), swarm._offers())
    return [m["name"] for m in free]


async def test_the_owner_is_told_once_after_ten_times_the_wait_and_not_before(tmp_path):
    """The run's second state: nothing before CREW_GAP_FACTOR x agents.idle_nudge_seconds, then one n_crew_gap to the
    owner naming the free members, the longest idle first, and never again for that state; nobody else is told."""
    swarm = await gap_team(tmp_path)
    try:
        task, (search, evalu, uci) = last_direction(swarm)
        rested(swarm, CARL, BOB)
        swarm._tell_crew_gaps()                                         # the clock starts
        later(swarm, DUE - WAIT / 2)
        swarm._tell_crew_gaps()
        assert gap_notices(swarm) == []
        later(swarm, WAIT / 2)
        swarm._tell_crew_gaps()
        directions, held = swarm.board.crew_gap_parts(task, [], "en")
        assert directions == (f' Its directions: #{search.id} [done] "search" @{OLGA}; #{evalu.id} [done] "eval" '
                              f'@{BOB}; #{uci.id} [in_progress] "uci" @{ANNA}.') and held == ""
        assert texts_for(swarm, OLGA, "n_crew_gap") == [prompts.t(
            "en", "n_crew_gap", id=task.id, title="engine", working=2, crew=3, names=f"{OLGA}, {ANNA}",
            idle=f"{CARL} (Tester), {BOB} (Generalist), {DINA} (Analyst)", directions=directions, held="")]
        assert swarm._crew_gap_told == {task.id: (OLGA, 3, frozenset({uci.id}), frozenset())}
        assert swarm.db.scalar("SELECT COUNT(*) FROM notifications WHERE kind = 'n_crew_gap'") == 1
        deliver(swarm)
        for _ in range(3):
            later(swarm, DUE)
            swarm._tell_crew_gaps()
        assert len(gap_notices(swarm)) == 1
    finally:
        await shutdown(swarm)


async def test_an_owner_at_work_is_told_at_the_first_pass_that_finds_it_idle(tmp_path, monkeypatch):
    """Working, busy with a job or with news pending, the owner is not told (the notice never lands in a running
    activation); the first pass that finds it idle tells it, the state still holding."""
    swarm = await gap_team(tmp_path)
    try:
        last_direction(swarm)
        rested(swarm)
        swarm.db.execute("UPDATE agents SET status = ? WHERE name = ?", (AgentStatus.WORKING, OLGA))
        tell_when_due(swarm)
        assert gap_notices(swarm) == []
        swarm.db.execute("UPDATE agents SET status = ? WHERE name = ?", (AgentStatus.IDLE, OLGA))
        busy = swarm.jobs.busy
        monkeypatch.setattr(swarm.jobs, "busy", lambda name: name == OLGA)     # its command went on as a job
        swarm._tell_crew_gaps()
        assert gap_notices(swarm) == []
        monkeypatch.setattr(swarm.jobs, "busy", busy)
        swarm.mail.send(USER, OLGA, "status?")
        swarm._tell_crew_gaps()
        assert gap_notices(swarm) == []
        deliver(swarm, OLGA)
        swarm._tell_crew_gaps()
        assert len(gap_notices(swarm)) == 1
    finally:
        await shutdown(swarm)


async def test_an_owner_waiting_for_its_own_job_is_told_but_a_member_waiting_for_one_is_not_free(tmp_path,
                                                                                               monkeypatch):
    swarm = await gap_team(tmp_path)
    try:
        last_direction(swarm)
        rested(swarm, CARL, BOB, DINA)
        monkeypatch.setattr(swarm.jobs, "awaited", lambda name: ["j1"] if name in (OLGA, BOB) else [])
        tell_when_due(swarm)
        assert [d["idle"] for d in gap_notices(swarm)] == [f"{CARL} (Tester), {DINA} (Analyst)"]
    finally:
        await shutdown(swarm)


@pytest.mark.parametrize("owner", [USER, EXT])
async def test_the_user_or_an_external_owner_is_never_told_nor_the_creator_in_its_place(tmp_path, owner):
    swarm = await gap_team(tmp_path)
    try:
        await swarm.hire(USER, "Developer", name=EXT, kind="external")
        task, _ = last_direction(swarm, owner=owner, creator=DINA)
        rested(swarm)
        for _ in range(3):
            tell_when_due(swarm)
        assert [t.id for t, _, _ in swarm.board.crew_gaps()] == ([] if owner == USER else [task.id])
        assert swarm.db.scalar("SELECT COUNT(*) FROM notifications WHERE kind = 'n_crew_gap'") == 0
    finally:
        await shutdown(swarm)


async def test_members_whose_tasks_wait_are_free(tmp_path):
    """Bob holds a task blocked by the crew task (the planner of that run) and a direction of another crew task, held
    back since that one is full; Carl holds a task in `waiting`; Dina owns the other crew task while working on none
    of its directions: all three are free. The owner and Anna, the crew task's working set, never are."""
    swarm = await gap_team(tmp_path)
    try:
        board = swarm.board
        task, _ = last_direction(swarm)
        report = board.create(USER, "report", goal="g", depends_on=[task.id])
        set_raw(board, report.id, assignee=BOB)
        docs = board.create(USER, "docs", goal="g")
        set_raw(board, docs.id, status=Status.WAITING, assignee=CARL)
        other, (part, more) = make_crew_raw(board, USER, 2, ["part", "more"], title="other", goal="g")
        set_raw(board, other.id, status=Status.IN_PROGRESS, assignee=DINA)
        set_raw(board, part.id, status=Status.IN_PROGRESS, assignee=ANNA)
        set_raw(board, more.id, assignee=BOB)
        assert board.stall_marks(BOB) == board.stall_marks(DINA) == frozenset()
        rested(swarm, BOB, CARL, DINA)
        assert free_names(swarm, task) == [BOB, CARL, DINA]
        tell_when_due(swarm)
        assert [d["idle"] for d in gap_notices(swarm)] == [f"{BOB} (Generalist), {CARL} (Tester), {DINA} (Analyst)"]
    finally:
        await shutdown(swarm)


@pytest.mark.parametrize("case", ["in progress", "todo", "todo subtask", "review", "mark", "file", "just idle"])
async def test_a_member_with_something_to_go_on_with_is_not_free(tmp_path, case):
    """Bob works on a task, holds a todo one no blocker stops (no direction: a top-level one, or a subtask of Dina's
    plain task, which leaves Dina free), has a review waiting for him, has a liveness mark (a direction of another crew
    task his claim would take now), waits for a file, or has just gone idle (someone may still give him work): not
    free."""
    swarm = await gap_team(tmp_path)
    try:
        board = swarm.board
        task, _ = last_direction(swarm)
        side = board.create(USER, "side", goal="g")
        if case == "in progress":
            set_raw(board, side.id, status=Status.IN_PROGRESS, assignee=BOB)
        elif case == "todo":
            set_raw(board, side.id, assignee=BOB)
        elif case == "todo subtask":
            set_raw(board, side.id, status=Status.IN_PROGRESS, assignee=DINA)
            part = board.create(USER, "part", goal="g", parent_id=side.id)
            set_raw(board, part.id, assignee=BOB)
        elif case == "review":
            finish_raw(board, side.id, Status.REVIEW, "r", assignee=ANNA, reviewer=BOB)
        elif case == "mark":
            set_raw(board, side.id, status=Status.CANCELLED)
            other, (part,) = make_crew_raw(board, USER, 2, ["part"], title="other", goal="g")
            set_raw(board, other.id, status=Status.IN_PROGRESS, assignee=DINA)
            set_raw(board, part.id, assignee=BOB)
            assert board.stall_marks(BOB) == frozenset({(part.id, "direction to take")})
        else:
            set_raw(board, side.id, status=Status.CANCELLED)
            if case == "file":
                swarm.db.execute("INSERT INTO lock_waiters(agent, path, since) VALUES (?, ?, ?)",
                                 (BOB, "x.md", time.time()))
        rested(swarm)
        if case == "just idle":
            swarm.db.execute("UPDATE agents SET last_active = ? WHERE name = ?", (time.time(), BOB))
        assert free_names(swarm, task) == [CARL, DINA]
    finally:
        await shutdown(swarm)


async def test_a_member_is_free_once_item_1_offered_it_every_ready_task_it_would_offer(tmp_path):
    """A ready task not offered to a member yet keeps it out (item 1 offers it first; once offered and not taken, it
    is free). A free direction of a crew task in which the member works on a `waiting` direction is never offered to
    it (busy_crews), so it keeps that member out of nothing."""
    swarm = await gap_team(tmp_path)
    try:
        board = swarm.board
        task, _ = last_direction(swarm)
        other, (mine, spare) = make_crew_raw(board, USER, 3, ["mine", "spare"], title="other", goal="g")
        set_raw(board, other.id, status=Status.IN_PROGRESS, assignee=USER)
        set_raw(board, mine.id, status=Status.WAITING, assignee=CARL)
        side = board.create(USER, "side", goal="g")
        rested(swarm)
        assert [t.id for t in board.ready_unassigned()] == [spare.id, side.id]
        assert free_names(swarm, task) == []
        offer_rounds(swarm, 6)
        deliver(swarm)
        assert [offered(swarm, n, spare) for n in (BOB, CARL, DINA)] == [1, 0, 1]
        assert free_names(swarm, task) == [BOB, CARL, DINA]
    finally:
        await shutdown(swarm)


async def test_a_finished_direction_rearms_the_notice_and_a_hand_in_or_a_claim_does_not(tmp_path):
    swarm = await gap_team(tmp_path)
    try:
        board = swarm.board
        task, (search, evalu, uci) = engine(swarm, crew=4)
        finish_raw(board, search.id, Status.DONE, "r", assignee=OLGA)
        set_raw(board, evalu.id, status=Status.IN_PROGRESS, assignee=BOB)
        set_raw(board, uci.id, status=Status.IN_PROGRESS, assignee=ANNA)
        rested(swarm)
        tell_when_due(swarm)
        assert len(gap_notices(swarm)) == 1
        assert swarm._crew_gap_told[task.id] == (OLGA, 4, frozenset({evalu.id, uci.id}), frozenset())
        deliver(swarm)
        set_raw(board, evalu.id, status=Status.REVIEW, reviewer=USER)          # Bob hands "eval" in: the same key
        tell_when_due(swarm)
        set_raw(board, uci.id, status=Status.TODO, assignee=None)              # Anna lets "uci" go: a free direction
        swarm._tell_crew_gaps()
        assert task.id not in swarm._crew_gaps
        board.claim(CARL, uci.id)                                              # Carl takes the place: the same key
        deliver(swarm)
        tell_when_due(swarm)
        assert len(gap_notices(swarm)) == 1
        finish_raw(board, evalu.id, Status.DONE, "r", assignee=BOB)            # "eval" finished: a new key
        swarm._tell_crew_gaps()
        later(swarm, DUE - 1)
        swarm._tell_crew_gaps()
        assert len(gap_notices(swarm)) == 1                                    # its clock started again
        later(swarm, 1)
        swarm._tell_crew_gaps()
        assert len(gap_notices(swarm)) == 2
        assert swarm._crew_gap_told[task.id] == (OLGA, 4, frozenset({uci.id}), frozenset())
    finally:
        await shutdown(swarm)


async def test_a_direction_newly_held_for_a_busy_member_rearms_the_notice(tmp_path):
    """"uci" is first an external member's (neither held nor named), then assigned to Bob, who works on "eval": it is
    held, a new key, and the second notice names it."""
    swarm = await gap_team(tmp_path)
    try:
        await swarm.hire(USER, "Developer", name=EXT, kind="external")
        board = swarm.board
        task, (search, evalu, uci) = engine(swarm)
        set_raw(board, search.id, status=Status.IN_PROGRESS, assignee=OLGA)
        set_raw(board, evalu.id, status=Status.IN_PROGRESS, assignee=BOB)
        set_raw(board, uci.id, assignee=EXT)
        rested(swarm)
        tell_when_due(swarm)
        [first] = gap_notices(swarm)
        assert first["held"] == ""
        deliver(swarm)
        set_raw(board, uci.id, assignee=BOB)
        swarm._tell_crew_gaps()
        assert len(gap_notices(swarm)) == 1                                    # the new key's clock first
        later(swarm, DUE)
        swarm._tell_crew_gaps()
        second = gap_notices(swarm)[1]
        assert second["held"] == prompts.t("en", "crew_gap_held", sub=uci.id, name=BOB, m=evalu.id)
        assert swarm._crew_gap_told[task.id][3] == frozenset({uci.id})
    finally:
        await shutdown(swarm)


async def test_at_most_two_notices_per_owner_and_crew_task_across_a_restart(tmp_path, monkeypatch):
    """A restart loses the clocks and the keys told (in memory): the same state may be told once more, but the count
    is read from the notifications table, so never a third time, nor for a new key. At the cap a key due is recorded
    as told at once, with no free member looked for (here none is free: a ready task nobody was offered yet), so
    the pass after it costs nothing."""
    swarm = await gap_team(tmp_path)
    try:
        task, (_, _, uci) = last_direction(swarm)
        rested(swarm)
        for told in (1, 2, 2):
            swarm._crew_gaps, swarm._crew_gap_told = {}, {}                    # a restart
            tell_when_due(swarm)
            deliver(swarm)
            assert len(gap_notices(swarm)) == told
        assert swarm._crew_gap_told == {task.id: (OLGA, 3, frozenset({uci.id}), frozenset())}  # at the cap
        swarm.board.create(USER, "side", goal="g")
        set_raw(swarm.board, task.id, crew=4)
        deliver(swarm)
        looked = []
        free_members = swarm._free_members
        monkeypatch.setattr(swarm, "_free_members", lambda *a: looked.append(a[0].id) or free_members(*a))
        tell_when_due(swarm)
        assert len(gap_notices(swarm)) == 2 and swarm._crew_gap_told[task.id][1] == 4 and looked == []
    finally:
        await shutdown(swarm)


async def test_a_pause_moves_the_clock_on_by_its_length(tmp_path):
    swarm = await gap_team(tmp_path)
    try:
        last_direction(swarm)
        rested(swarm)
        swarm._tell_crew_gaps()
        later(swarm, DUE + 1)                       # due by the wall clock, but the last 3 s of it were a pause
        swarm.pause(USER, "user")
        swarm._paused_at -= 3
        swarm.resume(USER)
        deliver(swarm)
        swarm._tell_crew_gaps()
        assert gap_notices(swarm) == []
        later(swarm, 2)
        swarm._tell_crew_gaps()
        assert len(gap_notices(swarm)) == 1
    finally:
        await shutdown(swarm)


async def test_nothing_is_told_with_the_goal_done_or_in_wrap_up_and_the_clock_starts_again(tmp_path):
    swarm = await gap_team(tmp_path)
    try:
        last_direction(swarm)
        rested(swarm)
        swarm._tell_crew_gaps()
        later(swarm, DUE)
        swarm.complete_goal(USER, "enough")
        swarm._tell_crew_gaps()
        assert gap_notices(swarm) == [] and swarm._crew_gaps == {}
        swarm.reopen_goal(USER)
        deliver(swarm)
        swarm._tell_crew_gaps()                     # a new clock
        assert gap_notices(swarm) == []
        later(swarm, DUE)
        swarm.wrap_up(USER)
        deliver(swarm)
        swarm._tell_crew_gaps()
        assert gap_notices(swarm) == [] and swarm._crew_gaps == {}
        swarm.end_wrap_up(USER)
        swarm._tell_crew_gaps()
        assert gap_notices(swarm) == []
        later(swarm, DUE)
        swarm._tell_crew_gaps()
        assert len(gap_notices(swarm)) == 1
    finally:
        await shutdown(swarm)


async def test_an_owner_of_two_crew_tasks_due_is_told_of_both_in_one_pass(tmp_path):
    swarm = await gap_team(tmp_path)
    try:
        board = swarm.board
        crews = []
        for title, worker in (("alpha", ANNA), ("beta", BOB)):
            crew, (part,) = make_crew_raw(board, USER, 3, ["part"], title=title, goal="g")
            set_raw(board, crew.id, status=Status.IN_PROGRESS, assignee=OLGA)
            set_raw(board, part.id, status=Status.IN_PROGRESS, assignee=worker)
            crews.append(crew)
        swarm.set_goal(USER, "Ship it")
        deliver(swarm)
        rested(swarm)
        swarm._tell_crew_gaps()
        later(swarm, DUE)
        swarm._tell_crew_gaps()                     # one pass
        notices = gap_notices(swarm)
        assert [d["id"] for d in notices] == [c.id for c in crews]
        assert [d["idle"] for d in notices] == [f"{CARL} (Tester), {DINA} (Analyst)"] * 2
    finally:
        await shutdown(swarm)


async def test_an_owner_told_earlier_in_the_pass_is_no_free_member_of_the_next_crew_task(tmp_path):
    """Dina owns "alpha" (Anna works on it), Olga "beta" (Bob works on it), both due in one pass. Dina, told of alpha
    first, has news pending, so beta's notice does not name her; Olga, told only after, is named in alpha's."""
    swarm = await gap_team(tmp_path)
    try:
        board = swarm.board
        for title, owner, worker in (("alpha", DINA, ANNA), ("beta", OLGA, BOB)):
            crew, (part,) = make_crew_raw(board, USER, 3, ["part"], title=title, goal="g")
            set_raw(board, crew.id, status=Status.IN_PROGRESS, assignee=owner)
            set_raw(board, part.id, status=Status.IN_PROGRESS, assignee=worker)
        swarm.set_goal(USER, "Ship it")
        deliver(swarm)
        rested(swarm)
        swarm._tell_crew_gaps()
        later(swarm, DUE)
        swarm._tell_crew_gaps()                     # one pass
        assert [d["idle"] for d in gap_notices(swarm, DINA)] == [f"{OLGA} (Generalist), {CARL} (Tester)"]
        assert [d["idle"] for d in gap_notices(swarm, OLGA)] == [f"{CARL} (Tester)"]
    finally:
        await shutdown(swarm)


async def test_the_scheduler_loop_tells_the_owner_after_item_1_in_its_pass(tmp_path, monkeypatch):
    """Item 8 runs in the scheduler's loop after items 1-7: in the pass in which item 1 offers the idle owner a ready
    task (a free direction of a crew task in which Carl, the one free member, works on a `waiting` one, so it is not
    offered to him), the owner has news pending and is not told; a later pass that finds it idle tells it."""
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)
    monkeypatch.setattr(swarm_module, "CREW_GAP_FACTOR", 0)
    swarm = await gap_team(tmp_path)
    try:
        board = swarm.board
        last_direction(swarm)
        other, (mine, spare) = make_crew_raw(board, USER, 3, ["mine", "spare"], title="other", goal="g")
        set_raw(board, other.id, status=Status.IN_PROGRESS, assignee=USER)
        set_raw(board, mine.id, status=Status.WAITING, assignee=CARL)
        rested(swarm, OLGA)                         # the longest idle: item 1 offers "spare" to Olga first
        ready = "SELECT recipient FROM notifications WHERE kind = 'n_ready'"
        await run_until(lambda: swarm.db.all(ready), timeout=5)
        assert [r["recipient"] for r in swarm.db.all(ready)] == [OLGA] and gap_notices(swarm) == []
        deliver(swarm, OLGA)
        await run_until(lambda: gap_notices(swarm), timeout=5)
        assert [d["idle"] for d in gap_notices(swarm)] == [f"{CARL} (Tester)"]
    finally:
        await shutdown(swarm)


async def test_an_owner_in_russian_gets_the_notice_and_its_parts_in_russian(tmp_path):
    swarm = await gap_team(tmp_path)
    try:
        owner = "Дарья Петрова"
        await swarm.hire(USER, "Developer", name=owner, language="ru")
        swarm.runtimes[owner] = IdleRuntime()
        task, _ = last_direction(swarm, owner=owner)
        rested(swarm)
        tell_when_due(swarm)
        [text] = texts_for(swarm, owner, "n_crew_gap")
        directions, _ = swarm.board.crew_gap_parts(task, [], "ru")
        assert text.startswith(f"#{task.id} «engine», твоя групповая задача") and directions in text
        assert directions.startswith(" Её направления: ")
    finally:
        await shutdown(swarm)


async def test_the_notice_names_at_most_eight_free_members_with_their_roles_cut(tmp_path):
    swarm = await gap_team(tmp_path)
    try:
        extra = [await swarm.hire(USER, "Specialist " * 8) for _ in range(6)]
        for name in extra:
            swarm.runtimes[name] = IdleRuntime()
        last_direction(swarm)
        rested(swarm, BOB, CARL, DINA, *extra)
        tell_when_due(swarm)
        cut = trim(swarm.member(extra[0])["role"], 60)
        assert len(cut) == 60 and cut.endswith("…")
        named = [f"{BOB} (Generalist)", f"{CARL} (Tester)", f"{DINA} (Analyst)", *(f"{n} ({cut})" for n in extra[:5])]
        assert [d["idle"] for d in gap_notices(swarm)] == [", ".join([*named, "…"])]
    finally:
        await shutdown(swarm)


async def test_a_failed_render_leaves_the_free_place_for_the_next_pass(tmp_path, monkeypatch):
    swarm = await gap_team(tmp_path)
    try:
        task, _ = last_direction(swarm)
        rested(swarm)
        real = swarm.board.crew_gap_parts

        def broken(*args):
            raise RuntimeError("render failed")

        monkeypatch.setattr(swarm.board, "crew_gap_parts", broken)
        tell_when_due(swarm)
        assert gap_notices(swarm) == [] and task.id not in swarm._crew_gap_told
        monkeypatch.setattr(swarm.board, "crew_gap_parts", real)
        swarm._tell_crew_gaps()
        assert len(gap_notices(swarm)) == 1
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
