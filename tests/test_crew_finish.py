"""Finishing a parent and a crew task, and closing one (docs/SPEC.md §4 side effects, §4.2 "Finishing", "Reviews",
"Leaving and closing", "Who controls subtasks"): what an agent's completion needs, who is told when the subtasks
finish, the check of the whole and a returned crew task, a member leaving, and the cascade when a parent is closed."""

from __future__ import annotations

import pytest

from dreamteam import prompts
from dreamteam.board import Board
from dreamteam.db import loads
from dreamteam.models import USER, AgentKind, AgentStatus, Status, Task, ToolError
from dreamteam.swarm import QUIET_KEY
from helpers.crew import finish_raw, keys_for, make_crew_raw, notes_of, set_raw

ANNA, BOB, CARL, DINA, EVA = "Anna Smith", "Bob Brown", "Carl Clark", "Dina Evans", "Eva Orlova"
EXT = "Ext Agent"


@pytest.fixture
def swarm(fake_swarm):
    fake_swarm.board = Board(fake_swarm)
    for name in (ANNA, BOB, CARL, DINA):
        fake_swarm.add_member(name, language="en")
    fake_swarm.add_member(EVA, language="ru")
    fake_swarm.add_member(EXT, kind=AgentKind.EXTERNAL, language="en")
    return fake_swarm


@pytest.fixture
def board(swarm) -> Board:
    return swarm.board


def crew_task(board: Board, crew: int = 3, titles=("parser", "cli", "docs"), owner: str | None = ANNA,
              creator: str = ANNA, **kw) -> tuple[Task, list[Task]]:
    """A crew task created by `creator` (its directions too), held by `owner` in progress (None: todo, no owner)."""
    task, subs = make_crew_raw(board, creator, crew, list(titles), **kw)
    if owner is not None:
        set_raw(board, task.id, assignee=owner, status=Status.IN_PROGRESS)
    return board.get(task.id), subs


def work(board: Board, sid: int, name: str, status: str = Status.IN_PROGRESS, **values) -> Task:
    return set_raw(board, sid, assignee=name, status=status, **values)


def events(swarm, type_: str) -> list[dict]:
    return [e for e in swarm.bus.recent(limit=1000) if e["type"] == type_]


def updated_ids(swarm) -> list[int]:
    return [e["data"]["task"]["id"] for e in events(swarm, "task.updated")]


def refused(call) -> str:
    with pytest.raises(ToolError) as exc:
        call()
    return str(exc.value)


def done_text(board: Board, parent_id: int, lang: str = "en", form: str = "holder") -> str:
    """The n_subtasks_done text for `parent_id` as it is now."""
    parent = board.get(parent_id)
    state, counts = board._finish_info([parent_id])[parent_id]
    return prompts.t(lang, "n_subtasks_done", id=parent.id, title=parent.title, total=sum(counts.values()),
                     counts=board._counts_text(counts, lang), ending=board._ending(parent, state, form, lang))


# --- what an agent's completion needs ------------------------------------------------------------------------------

def test_an_agent_cannot_complete_a_task_with_open_subtasks(swarm, board):
    parent = board.create(ANNA, "parent", assignee=ANNA)
    set_raw(board, parent.id, status=Status.IN_PROGRESS)
    a = board.create(ANNA, "a", parent_id=parent.id)
    b = board.create(ANNA, "b", parent_id=parent.id)
    c = board.create(ANNA, "c", parent_id=parent.id)
    mine = board.create(ANNA, "mine", parent_id=parent.id)
    work(board, a.id, BOB)
    work(board, b.id, CARL, Status.REVIEW)
    finish_raw(board, c.id, Status.DONE, "ok")
    work(board, mine.id, ANNA, Status.WAITING)
    text = refused(lambda: board.complete(ANNA, parent.id, "all"))
    assert text == (f"Task #{parent.id} has open subtasks: #{a.id} [in_progress, {BOB}], #{b.id} [review], "
                    f"#{mine.id} [waiting, you]. Complete it when they are finished; as its creator or assignee you "
                    f"may cancel the ones no longer needed (task_update status=cancelled), and move the ones that are "
                    f"not parts of #{parent.id} out of it (task_update parent_id=0).")
    assert board.get(parent.id).status == Status.IN_PROGRESS


def test_an_agent_cannot_complete_a_crew_task_with_a_failed_direction(board):
    task, subs = crew_task(board)
    finish_raw(board, subs[0].id, Status.DONE, "ok", assignee=BOB)
    finish_raw(board, subs[1].id, Status.FAILED, assignee=CARL)
    finish_raw(board, subs[2].id, Status.CANCELLED)
    text = refused(lambda: board.complete(ANNA, task.id, "all"))
    assert text == (f"Crew task #{task.id} has failed directions: #{subs[1].id} \"cli\". Reopen each to be done "
                    f"again (task_update status=todo) or cancel it if #{task.id} does without it (status=cancelled), "
                    f"then complete #{task.id}.")


def test_open_subtasks_are_named_before_a_failed_direction(board):
    task, subs = crew_task(board)
    finish_raw(board, subs[0].id, Status.FAILED, assignee=CARL)
    work(board, subs[1].id, BOB)
    assert refused(lambda: board.complete(ANNA, task.id, "all")).startswith(f"Task #{task.id} has open subtasks")


@pytest.mark.parametrize("crew", [2, 8])
def test_a_direction_needs_its_goal_before_it_is_handed_in(board, crew):
    task, subs = crew_task(board, crew=crew)
    board.claim(BOB, subs[0].id)
    text = refused(lambda: board.complete(BOB, subs[0].id, "parsed"))
    assert text == (f"Write the goal of #{subs[0].id} first (task_update goal=...): its reviewer checks the work "
                    "against it.")
    board.update(BOB, subs[0].id, goal="parses the input")
    assert board.complete(BOB, subs[0].id, "parsed").status == Status.REVIEW


def test_a_subtask_of_an_ordinary_parent_needs_no_goal(swarm, board):
    swarm.config.swarm.independent_review = False
    parent = board.create(ANNA, "parent")
    sub = board.create(ANNA, "sub", parent_id=parent.id)
    board.claim(BOB, sub.id)
    assert board.complete(BOB, sub.id, "ok").status == Status.DONE


def test_user_completes_regardless(swarm, board):
    task, subs = crew_task(board)
    work(board, subs[0].id, BOB)
    finish_raw(board, subs[1].id, Status.FAILED, assignee=CARL)
    done = board.complete(USER, task.id, "the user's result")
    assert done.status == Status.DONE
    assert board.get(subs[2].id).status == Status.CANCELLED  # todo without an assignee: cancelled with it
    assert board.get(subs[0].id).status == Status.IN_PROGRESS
    assert notes_of(swarm, BOB, "n_parent_closed") == [
        {"id": task.id, "title": task.title, "status": Status.DONE, "sub": subs[0].id}]


def test_a_failed_direction_blocks_until_it_is_reopened_or_cancelled(swarm, board):
    task, subs = crew_task(board, titles=("parser", "cli"))
    finish_raw(board, subs[0].id, Status.DONE, "parsed", assignee=BOB)
    finish_raw(board, subs[1].id, Status.FAILED, assignee=CARL)
    assert "has failed directions" in refused(lambda: board.complete(ANNA, task.id, "all"))
    assert "has failed directions" in refused(lambda: board.update(ANNA, task.id, crew=0))
    # reopened: open again (to be done by whoever takes it), so the completion waits for it
    board.update(ANNA, subs[1].id, status="todo")
    assert board.get(subs[1].id).assignee is None
    assert "has open subtasks" in refused(lambda: board.complete(ANNA, task.id, "all"))
    board.update(ANNA, subs[1].id, status="cancelled")
    assert board.complete(ANNA, task.id, "all").status == Status.REVIEW
    other, others = crew_task(board, titles=("parser", "cli"))
    finish_raw(board, others[0].id, Status.DONE, "parsed", assignee=BOB)
    finish_raw(board, others[1].id, Status.FAILED, assignee=CARL)
    board.update(ANNA, others[1].id, status="cancelled")
    assert board.update(ANNA, other.id, crew=0).crew == 0


# --- the subtasks finish: n_subtasks_done ------------------------------------------------------------------------

def test_the_owner_is_told_when_someone_else_finishes_the_last_direction(swarm, board):
    task, subs = crew_task(board)
    finish_raw(board, subs[0].id, Status.DONE, "parser done", assignee=BOB)
    finish_raw(board, subs[1].id, Status.CANCELLED)
    work(board, subs[2].id, CARL, Status.REVIEW, reviewer=DINA, result="docs done", goal="docs")
    swarm.notified.clear()
    before = len(events(swarm, "task.updated"))
    board.review(DINA, subs[2].id, True)
    assert keys_for(swarm, ANNA) == ["n_subtasks_done"]  # in place of n_task_closed: ANNA created the direction
    kw = notes_of(swarm, ANNA, "n_subtasks_done")[0]
    assert {k: kw[k] for k in ("id", "title", "total", "counts")} == {
        "id": task.id, "title": task.title, "total": 3, "counts": "2 done, 1 cancelled"}
    assert kw["ending"] == board._ending(board.get(task.id), "finished", "holder", "en")
    assert kw["ending"].startswith(f"check that they fit together, write #{task.id}'s result")
    assert f"\nDraft for #{task.id}'s result" in kw["ending"] and "parser done" in kw["ending"]
    # the crew task never changes its status by itself
    assert board.get(task.id).status == Status.IN_PROGRESS
    assert updated_ids(swarm)[before:] == [subs[2].id]


def test_the_holders_own_review_carries_the_text_in_its_result(swarm, board):
    task, subs = crew_task(board, titles=("parser", "cli"))
    finish_raw(board, subs[0].id, Status.DONE, "parser done", assignee=BOB)
    work(board, subs[1].id, CARL, Status.REVIEW, reviewer=ANNA, result="cli done", goal="cli")
    swarm.notified.clear()
    notes: list[str] = []
    board.review(ANNA, subs[1].id, True, notes=notes)
    assert notes == [" " + done_text(board, task.id)]
    assert "n_subtasks_done" not in keys_for(swarm, ANNA)


def test_the_owner_completing_its_own_last_direction_reads_the_draft(swarm, board):
    swarm.config.swarm.independent_review = False
    task, subs = crew_task(board, titles=("parser", "cli"))
    finish_raw(board, subs[0].id, Status.DONE, "parser done", assignee=BOB)
    work(board, subs[1].id, ANNA, goal="cli")
    swarm.notified.clear()
    notes: list[str] = []
    board.complete(ANNA, subs[1].id, "cli done", notes=notes)
    assert notes == [" " + done_text(board, task.id)]
    assert "cli done" in notes[0] and "parser done" in notes[0]
    assert keys_for(swarm, ANNA) == []


def test_a_completion_that_goes_to_review_finishes_nothing(swarm, board):
    task, subs = crew_task(board, titles=("parser", "cli"))
    finish_raw(board, subs[0].id, Status.DONE, "parser done", assignee=BOB)
    work(board, subs[1].id, ANNA, goal="cli")
    notes: list[str] = []
    board.complete(ANNA, subs[1].id, "cli done", notes=notes)
    assert notes == [] and "n_subtasks_done" not in keys_for(swarm, ANNA)


def test_the_owner_cancelling_the_last_open_direction_reads_it_in_its_result(swarm, board):
    task, subs = crew_task(board, titles=("parser", "cli"))
    finish_raw(board, subs[0].id, Status.DONE, "parser done", assignee=BOB)
    work(board, subs[1].id, CARL)
    swarm.notified.clear()
    notes: list[str] = []
    board.update(ANNA, subs[1].id, status="cancelled", notes=notes)
    assert notes == [" " + done_text(board, task.id)]
    assert keys_for(swarm, CARL) == ["n_subtask_cancelled"]
    assert keys_for(swarm, ANNA) == []


def test_an_ordinary_parent_gets_the_plain_ending(swarm, board):
    swarm.config.swarm.independent_review = False
    parent = board.create(ANNA, "parent", assignee=ANNA)
    sub = board.create(ANNA, "sub", parent_id=parent.id)
    other = board.create(ANNA, "other", parent_id=parent.id)
    finish_raw(board, other.id, Status.CANCELLED)
    board.claim(BOB, sub.id)
    swarm.notified.clear()
    board.complete(BOB, sub.id, "ok")
    assert keys_for(swarm, ANNA) == ["n_subtasks_done"]
    kw = notes_of(swarm, ANNA, "n_subtasks_done")[0]
    assert kw["ending"] == "assemble their results and complete it." and kw["counts"] == "1 done, 1 cancelled"
    assert prompts.t("en", "n_subtasks_done", **kw) == (
        f"All 2 subtasks of #{parent.id} \"parent\" are finished (1 done, 1 cancelled): assemble their results and "
        "complete it.")


def test_a_parent_without_a_holder_tells_its_creator_in_the_creator_form(swarm, board):
    task, subs = crew_task(board, titles=("parser", "cli"), owner=None)
    finish_raw(board, subs[0].id, Status.DONE, "parser done", assignee=BOB)
    work(board, subs[1].id, CARL, Status.REVIEW, reviewer=DINA, result="cli done", goal="cli")
    swarm.notified.clear()
    board.review(USER, subs[1].id, True)
    assert keys_for(swarm, ANNA) == ["n_subtasks_done"]
    ending = notes_of(swarm, ANNA, "n_subtasks_done")[0]["ending"]
    assert ending == board._ending(board.get(task.id), "finished", "creator", "en")
    assert ending.startswith(f"take #{task.id} (task_claim), check that they fit together")
    assert "Draft" not in ending  # the creator form has no draft: its claim's result gives it
    assert board.get(task.id).status == Status.TODO


def test_a_creator_that_is_user_or_has_left_is_told_nothing(swarm, board):
    for creator in (USER, DINA):
        swarm.notified.clear()
        task, subs = crew_task(board, titles=("parser",), owner=None, creator=creator)
        if creator == DINA:
            swarm.db.execute("UPDATE agents SET status = ? WHERE name = ?", (AgentStatus.RETIRED, DINA))
        work(board, subs[0].id, CARL, Status.REVIEW, reviewer=BOB, result="parser done", goal="parser")
        board.review(BOB, subs[0].id, True)
        assert [r for r, k, _ in swarm.notified if k == "n_subtasks_done"] == []


def test_the_creator_reads_it_in_its_own_result(swarm, board):
    task, subs = crew_task(board, titles=("parser",), owner=None)
    work(board, subs[0].id, CARL, Status.REVIEW, reviewer=ANNA, result="parser done", goal="parser")
    notes: list[str] = []
    board.review(ANNA, subs[0].id, True, notes=notes)
    assert notes == [" " + done_text(board, task.id, form="creator")]


def test_the_last_open_direction_failing_tells_the_owner_the_failed_ending(swarm, board):
    task, subs = crew_task(board, titles=("parser", "cli"))
    finish_raw(board, subs[0].id, Status.DONE, "parser done", assignee=BOB)
    work(board, subs[1].id, CARL, goal="cli")
    swarm.notified.clear()
    board.update(CARL, subs[1].id, status="failed")
    assert keys_for(swarm, ANNA) == ["n_subtasks_done"]  # not n_direction_failed: it was the last one
    ending = notes_of(swarm, ANNA, "n_subtasks_done")[0]["ending"]
    assert ending.startswith(f"reopen the failed #{subs[1].id} to be done again")


def test_the_last_failed_direction_cancelled_tells_the_owner_again(swarm, board):
    task, subs = crew_task(board)
    finish_raw(board, subs[0].id, Status.DONE, "parser done", assignee=BOB)
    finish_raw(board, subs[1].id, Status.FAILED, assignee=CARL)
    finish_raw(board, subs[2].id, Status.CANCELLED)
    swarm.notified.clear()
    board.update(USER, subs[1].id, status="cancelled")
    assert keys_for(swarm, ANNA) == ["n_subtasks_done"]  # failed -> finished; in place of n_task_closed
    kw = notes_of(swarm, ANNA, "n_subtasks_done")[0]
    assert kw["counts"] == "1 done, 2 cancelled" and "Draft for" in kw["ending"]
    assert keys_for(swarm, CARL) == []  # a failed direction is not open: no n_subtask_cancelled


def test_a_failed_direction_moved_out_of_its_crew_task_tells_the_owner(swarm, board):
    task, subs = crew_task(board, titles=("parser", "cli"), creator=DINA)
    finish_raw(board, subs[0].id, Status.DONE, "parser done", assignee=BOB)
    finish_raw(board, subs[1].id, Status.FAILED, assignee=CARL)
    swarm.notified.clear()
    board.update(DINA, subs[1].id, parent_id=None)
    assert board.get(subs[1].id).parent_id is None
    assert keys_for(swarm, ANNA) == ["n_subtasks_done"]
    kw = notes_of(swarm, ANNA, "n_subtasks_done")[0]
    assert kw["total"] == 1 and kw["ending"].startswith("check that they fit together")


def test_a_move_looks_at_the_old_and_the_new_parent(swarm, board):
    old_parent = board.create(ANNA, "old parent", assignee=BOB)
    new_parent = board.create(ANNA, "new parent", assignee=CARL)
    set_raw(board, old_parent.id, status=Status.IN_PROGRESS)
    set_raw(board, new_parent.id, status=Status.IN_PROGRESS)
    done = board.create(ANNA, "done", parent_id=old_parent.id)
    finish_raw(board, done.id, Status.DONE, "ok")
    moving = board.create(ANNA, "moving", parent_id=old_parent.id)
    swarm.notified.clear()
    board.update(ANNA, moving.id, parent_id=new_parent.id, status="cancelled")
    assert notes_of(swarm, BOB, "n_subtasks_done")[0]["id"] == old_parent.id
    assert notes_of(swarm, CARL, "n_subtasks_done")[0]["id"] == new_parent.id
    assert notes_of(swarm, CARL, "n_subtasks_done")[0]["counts"] == "1 cancelled"


def test_an_unchanged_finish_state_tells_nobody(swarm, board):
    parent = board.create(ANNA, "parent", assignee=BOB)
    set_raw(board, parent.id, status=Status.IN_PROGRESS)
    a = board.create(ANNA, "a", parent_id=parent.id)
    b = board.create(ANNA, "b", parent_id=parent.id)
    finish_raw(board, a.id, Status.DONE, "ok")
    finish_raw(board, b.id, Status.FAILED)
    swarm.notified.clear()
    board.update(ANNA, b.id, status="cancelled")  # finished before and after (an ordinary parent has no failed state)
    assert "n_subtasks_done" not in keys_for(swarm, BOB)


@pytest.mark.parametrize("status", [Status.REVIEW, Status.DONE, Status.CANCELLED])
def test_a_parent_in_review_or_terminal_is_left_alone(swarm, board, status):
    task, subs = crew_task(board, titles=("parser",))
    work(board, subs[0].id, CARL, goal="parser")
    set_raw(board, task.id, status=status)
    swarm.notified.clear()
    board.update(CARL, subs[0].id, status="failed")
    assert keys_for(swarm, ANNA) == ["n_task_closed"]
    assert board.get(task.id).status == status


def test_n_subtasks_done_in_the_holders_language(swarm, board):
    task, subs = crew_task(board, titles=("parser", "cli"), owner=EVA)
    finish_raw(board, subs[0].id, Status.DONE, "parser done", assignee=BOB)
    work(board, subs[1].id, CARL, Status.REVIEW, reviewer=DINA, result="cli done", goal="cli")
    board.review(DINA, subs[1].id, True)
    row = swarm.db.one("SELECT text FROM notifications WHERE recipient = ? AND kind = ?", (EVA, "n_subtasks_done"))
    assert row["text"].startswith(f"Все подзадачи #{task.id} «crew task» завершены (всего: 2; выполнено: 2): ")
    assert f"Черновик итога #{task.id}" in row["text"]


# --- a direction fails or is cancelled while another is open -------------------------------------------------------

def test_a_failed_direction_tells_the_owner_while_another_is_open(swarm, board):
    task, subs = crew_task(board, creator=DINA)
    work(board, subs[0].id, BOB)
    work(board, subs[1].id, CARL, goal="cli")
    swarm.notified.clear()
    board.update(CARL, subs[1].id, status="failed")
    assert keys_for(swarm, ANNA) == ["n_direction_failed"]
    assert notes_of(swarm, ANNA, "n_direction_failed") == [
        {"by": CARL, "id": subs[1].id, "title": "cli", "parent": task.id}]
    assert keys_for(swarm, DINA) == ["n_task_closed"]  # the direction's creator


def test_an_owner_that_created_the_failed_direction_gets_only_the_crew_notice(swarm, board):
    task, subs = crew_task(board)
    work(board, subs[0].id, BOB)
    work(board, subs[1].id, CARL, goal="cli")
    swarm.notified.clear()
    board.update(CARL, subs[1].id, status="failed")
    assert keys_for(swarm, ANNA) == ["n_direction_failed"]  # with its options, never also n_task_closed
    assert notes_of(swarm, ANNA, "n_direction_failed") == [
        {"by": CARL, "id": subs[1].id, "title": "cli", "parent": task.id}]


def test_exactly_one_notice_for_the_owner_that_created_a_direction_its_holder_cancels(swarm, board):
    task, subs = crew_task(board)
    work(board, subs[0].id, BOB)
    work(board, subs[1].id, CARL)
    swarm.notified.clear()
    board.update(BOB, subs[0].id, status="cancelled")
    assert keys_for(swarm, ANNA) == ["n_direction_cancelled"]
    assert keys_for(swarm, BOB) == []


def test_exactly_one_notice_for_an_owner_that_did_not_create_the_cancelled_direction(swarm, board):
    task, subs = crew_task(board, creator=DINA)
    work(board, subs[0].id, BOB)
    swarm.notified.clear()
    board.update(DINA, subs[0].id, status="cancelled")
    assert keys_for(swarm, ANNA) == ["n_direction_cancelled"]
    assert notes_of(swarm, ANNA, "n_direction_cancelled") == [
        {"by": DINA, "id": subs[0].id, "title": "parser", "parent": task.id}]
    assert keys_for(swarm, BOB) == ["n_subtask_cancelled"]
    assert notes_of(swarm, BOB, "n_subtask_cancelled") == [
        {"by": DINA, "id": subs[0].id, "title": "parser", "parent": task.id}]
    assert keys_for(swarm, DINA) == []


def test_exactly_one_notice_for_the_owners_own_part_cancelled_by_the_creator(swarm, board):
    task = board.create(DINA, "crew task", assignee=ANNA)
    set_raw(board, task.id, status=Status.IN_PROGRESS)
    board.update(ANNA, task.id, crew=3, directions=["own", "other"])
    own, other = board.subtasks_of(task.id)
    assert own.assignee == ANNA and own.created_by == ANNA and other.status == Status.TODO
    swarm.notified.clear()
    board.update(DINA, own.id, status="cancelled")
    assert keys_for(swarm, ANNA) == ["n_subtask_cancelled"]


def test_no_direction_notice_once_the_crew_task_is_in_review(swarm, board):
    task, subs = crew_task(board, creator=DINA)
    work(board, subs[0].id, BOB)
    work(board, subs[1].id, CARL, goal="cli")
    set_raw(board, task.id, status=Status.REVIEW, reviewer=EVA)
    swarm.notified.clear()
    board.update(CARL, subs[1].id, status="failed")
    assert keys_for(swarm, ANNA) == []


def test_a_cancelled_subtask_in_review_tells_its_executor(swarm, board):
    task, subs = crew_task(board)
    work(board, subs[0].id, BOB, Status.REVIEW, reviewer=CARL, result="parsed", goal="parser")
    work(board, subs[1].id, DINA)
    swarm.notified.clear()
    board.update(ANNA, subs[0].id, status="cancelled")
    assert keys_for(swarm, BOB) == ["n_subtask_cancelled"]
    assert keys_for(swarm, CARL) == ["n_review_cancelled"]


def test_a_subtask_of_an_ordinary_parent_cancelled_by_the_parents_holder(swarm, board):
    parent = board.create(ANNA, "parent", assignee=CARL)
    sub = board.create(ANNA, "sub", parent_id=parent.id)
    other = board.create(ANNA, "other", parent_id=parent.id)
    work(board, sub.id, BOB)
    work(board, other.id, DINA)
    swarm.notified.clear()
    board.update(CARL, sub.id, status="cancelled")
    assert keys_for(swarm, BOB) == ["n_subtask_cancelled"]
    assert keys_for(swarm, ANNA) == ["n_task_closed"]  # an ordinary parent has no n_direction_cancelled


def test_the_texts_render_in_russian(swarm, board):
    task, subs = crew_task(board, creator=DINA, owner=EVA)
    work(board, subs[0].id, EVA)
    work(board, subs[1].id, CARL)
    work(board, subs[2].id, BOB)
    board.update(DINA, subs[1].id, status="cancelled")  # n_direction_cancelled to EVA
    board.update(DINA, subs[0].id, status="cancelled")  # n_subtask_cancelled to EVA
    rows = swarm.db.all("SELECT kind, text FROM notifications WHERE recipient = ? ORDER BY id", (EVA,))
    texts = {r["kind"]: r["text"] for r in rows}
    assert texts["n_direction_cancelled"].startswith(f"#{subs[1].id} «cli», направление твоей групповой задачи")
    assert texts["n_subtask_cancelled"].startswith(f"{DINA} отменил(а) #{subs[0].id} «parser»")
    finish_raw(board, subs[0].id, Status.TODO)
    work(board, subs[0].id, EVA)
    board.update(DINA, task.id, status="cancelled")
    row = swarm.db.one("SELECT text FROM notifications WHERE recipient = ? AND kind = ?", (EVA, "n_parent_closed"))
    assert row["text"].startswith(f"#{task.id} «crew task», частью которой является твоя #{subs[0].id}")


# --- closing a parent: the cascade ---------------------------------------------------------------------------------

@pytest.mark.parametrize("status", [Status.CANCELLED, Status.FAILED])
def test_closing_a_crew_task_cancels_its_free_directions_and_tells_the_others(swarm, board, status):
    task, subs = crew_task(board, crew=4, titles=("own", "taken", "free", "assigned"), creator=DINA)
    work(board, subs[0].id, ANNA)
    work(board, subs[1].id, BOB)
    work(board, subs[3].id, CARL, Status.TODO)
    swarm.notified.clear()
    before = len(events(swarm, "task.updated"))
    notes: list[str] = []
    board.update(ANNA, task.id, status=status, notes=notes)
    assert board.get(subs[2].id).status == Status.CANCELLED
    assert [board.get(s.id).status for s in (subs[0], subs[1], subs[3])] == [
        Status.IN_PROGRESS, Status.IN_PROGRESS, Status.TODO]
    assert updated_ids(swarm)[before:] == [task.id, subs[2].id]  # the primary row, then the cascade's
    cascaded = events(swarm, "task.updated")[-1]
    assert cascaded["data"]["changes"] == {"status": [Status.TODO, Status.CANCELLED]}
    assert notes_of(swarm, BOB, "n_parent_closed") == [
        {"id": task.id, "title": task.title, "status": status, "sub": subs[1].id}]
    assert keys_for(swarm, CARL) == ["n_parent_closed"]
    assert notes == [f" Your #{subs[0].id} is still open: finish it only if it is still needed on its own; "
                     "otherwise cancel it (task_update status=cancelled)."]
    # DINA created the cancelled direction too: only the crew task's closing is news for her
    assert keys_for(swarm, DINA) == ["n_task_closed"]
    assert notes_of(swarm, DINA, "n_task_closed")[0]["id"] == task.id


@pytest.mark.parametrize("actor, status", [(USER, Status.CANCELLED), (ANNA, Status.FAILED)])
@pytest.mark.parametrize("held", [False, True])
def test_a_crew_task_the_cascade_cancels_takes_its_open_directions_along(swarm, board, actor, status, held):
    """A plain parent closes with a crew subtask nobody took: its free directions go too (nobody may take a direction
    of a terminal crew task), and the holders of the others are told the crew task is cancelled."""
    epic = board.create(ANNA, "epic", assignee=ANNA)
    set_raw(board, epic.id, status=Status.IN_PROGRESS)
    crew = board.create(ANNA, "auth", parent_id=epic.id, crew=2, directions=["docs", "ui"])
    docs, ui = board.subtasks_of(crew.id)
    if held:
        work(board, ui.id, CARL, Status.TODO)  # assigned, not taken yet
    swarm.notified.clear()
    before = len(events(swarm, "task.updated"))
    board.update(actor, epic.id, status=status)
    assert board.get(crew.id).status == Status.CANCELLED
    assert board.get(docs.id).status == Status.CANCELLED
    cascaded = [epic.id, crew.id, docs.id] + ([] if held else [ui.id])
    assert updated_ids(swarm)[before:] == cascaded   # the primary row, the crew task, then its directions
    if held:
        assert board.get(ui.id).status == Status.TODO
        assert notes_of(swarm, CARL, "n_parent_closed") == [
            {"id": crew.id, "title": "auth", "status": Status.CANCELLED, "sub": ui.id}]
        board.update(CARL, ui.id, assignee="")   # its holder lets it go: cancelled, not freed
        assert board.get(ui.id).status == Status.CANCELLED
    assert [t.id for t in board.list() if t.status in Status.OPEN] == []


def test_the_user_setting_done_closes_an_ordinary_parent_too(swarm, board):
    parent = board.create(ANNA, "parent", assignee=CARL)
    open_sub = board.create(ANNA, "open", parent_id=parent.id)
    free = board.create(ANNA, "free", parent_id=parent.id)
    work(board, open_sub.id, BOB, Status.WAITING)
    swarm.notified.clear()
    board.update(USER, parent.id, status="done")
    assert board.get(free.id).status == Status.CANCELLED
    assert keys_for(swarm, BOB) == ["n_parent_closed"]
    assert notes_of(swarm, BOB, "n_parent_closed")[0]["status"] == Status.DONE


def test_an_approval_of_a_parent_the_user_completed_closes_it(swarm, board):
    task, subs = crew_task(board)
    work(board, subs[0].id, BOB)
    set_raw(board, task.id, reviewer=CARL)
    board.complete(USER, task.id, "the user's result")
    assert board.get(task.id).status == Status.REVIEW
    assert board.get(subs[1].id).status == Status.TODO  # in review it is not closed yet
    swarm.notified.clear()
    board.review(CARL, task.id, True)
    assert board.get(subs[1].id).status == Status.CANCELLED
    assert "n_parent_closed" in keys_for(swarm, BOB)


def test_the_parents_creator_holding_an_open_subtask_gets_one_notice(swarm, board):
    task, subs = crew_task(board, creator=DINA)
    work(board, subs[0].id, DINA)
    swarm.notified.clear()
    board.update(ANNA, task.id, status="cancelled")
    assert keys_for(swarm, DINA) == ["n_parent_closed"]


def test_a_task_without_open_subtasks_closes_as_before(swarm, board):
    swarm.config.swarm.independent_review = False
    task = board.create(ANNA, "plain")
    board.claim(BOB, task.id)
    swarm.notified.clear()
    board.complete(BOB, task.id, "ok")
    assert keys_for(swarm, ANNA) == ["n_task_closed"]
    assert notes_of(swarm, ANNA, "n_task_closed")[0]["status"] == Status.DONE


# --- the check of the whole ----------------------------------------------------------------------------------------

def quiet(swarm, name: str, kind: str) -> list[bool]:
    """For each stored `kind` notification of `name`, oldest first: was it sent with wake=False."""
    rows = swarm.db.all("SELECT data FROM notifications WHERE recipient = ? AND kind = ? ORDER BY id", (name, kind))
    return [bool(loads(r["data"], {}).get(QUIET_KEY)) for r in rows]


def in_review(board: Board, done: tuple[str, ...] = (BOB,), reviewer: str = CARL, **kw) -> tuple[Task, list[Task]]:
    """A crew task held by ANNA waiting for `reviewer`, with one done direction per name in `done`."""
    task, subs = crew_task(board, titles=tuple(f"part{i}" for i in range(len(done))), **kw)
    for sub, name in zip(subs, done):
        finish_raw(board, sub.id, Status.DONE, f"{sub.title} done", assignee=name)
    return set_raw(board, task.id, status=Status.REVIEW, reviewer=reviewer, result="whole"), subs


def review_keys(swarm) -> list[tuple[str, str]]:
    return [(r, k) for r, k, _ in swarm.notified if k in ("n_review_requested", "n_crew_review_requested")]


@pytest.mark.parametrize("done", [True, False])
@pytest.mark.parametrize("own", [True, False])
@pytest.mark.parametrize("independent", [True, False])
@pytest.mark.parametrize("crew_review", [True, False])
def test_the_owners_completion_follows_the_matrix(swarm, board, crew_review, independent, own, done):
    swarm.config.swarm.crew_review = crew_review
    swarm.config.swarm.independent_review = independent
    task, subs = crew_task(board, titles=("parser", "cli"), creator=DINA)
    for sub, name in zip(subs, (BOB, CARL)):
        if done:
            finish_raw(board, sub.id, Status.DONE, f"{sub.title} done", assignee=name)
        else:
            finish_raw(board, sub.id, Status.CANCELLED)
    if own:
        set_raw(board, task.id, reviewer=DINA, reviewer_named=1)
    swarm.notified.clear()
    board.complete(ANNA, task.id, "the whole")
    after = board.get(task.id)
    if not own and not (independent and crew_review):
        # no reviewer of its own: with independent review off as for any task; with crew_review off whatever
        # independent review says (each direction was reviewed already), and the board picks nobody
        assert (after.status, after.reviewer, after.result, review_keys(swarm)) == (Status.DONE, None, "the whole", [])
        return
    whole = done and crew_review  # with crew_review off, a reviewer of its own reviews it as any task
    # the task's own reviewer; else the pick: with the contributor key someone outside the crew (DINA created it and
    # comes after EVA), without it the first by name (BOB, a contributor)
    expected = DINA if own else EVA if whole else BOB
    assert (after.status, after.reviewer) == (Status.REVIEW, expected)
    key = "n_crew_review_requested" if whole else "n_review_requested"
    assert review_keys(swarm) == [(expected, key)]
    kw = {"id": task.id, "title": task.title, "by": ANNA}
    assert notes_of(swarm, expected, key) == [{**kw, "n": 2} if whole else kw]


def test_the_check_of_the_whole_goes_to_a_contributor_before_the_user(swarm, board):
    task, subs = crew_task(board, crew=4, titles=("a", "b", "c", "d"))
    for sub, name in zip(subs, (BOB, CARL, DINA, EVA)):
        finish_raw(board, sub.id, Status.DONE, "ok", assignee=name)
    board.complete(ANNA, task.id, "the whole")
    reviewer = board.get(task.id).reviewer
    assert reviewer in board.contributors_of(board.get(task.id)) and reviewer not in (ANNA, USER)
    assert keys_for(swarm, reviewer) == ["n_crew_review_requested"]


def test_a_crew_task_without_a_done_direction_is_reviewed_as_any_task(swarm, board):
    task, subs = crew_task(board, titles=("parser",))
    finish_raw(board, subs[0].id, Status.CANCELLED)
    board.complete(ANNA, task.id, "done it myself")
    assert review_keys(swarm) == [(board.get(task.id).reviewer, "n_review_requested")]


@pytest.mark.parametrize("crew_review, key", [(True, "n_crew_review_requested"), (False, "n_review_requested")])
def test_a_hand_over_is_the_check_of_the_whole_only_through_crew_review(swarm, board, crew_review, key):
    swarm.config.swarm.crew_review = crew_review
    task, _ = in_review(board)
    swarm.notified.clear()
    board.hand_over_review(task.id, exclude=())
    reviewer = board.get(task.id).reviewer
    assert reviewer not in (CARL, ANNA, USER)
    expected = {"id": task.id, "title": task.title, "by": ANNA}
    assert notes_of(swarm, reviewer, key) == [{**expected, "n": 1} if crew_review else expected]
    assert keys_for(swarm, CARL) == ["n_review_reassigned"]


def test_a_re_pick_when_the_reviewer_leaves_asks_for_the_check_of_the_whole(swarm, board):
    task, _ = in_review(board)
    swarm.notified.clear()
    board.release_all(CARL, USER)
    reviewer = board.get(task.id).reviewer
    assert reviewer not in (CARL, ANNA, USER)
    assert notes_of(swarm, reviewer, "n_crew_review_requested") == [
        {"id": task.id, "title": task.title, "by": ANNA, "n": 1}]


@pytest.mark.parametrize("crew_review", [True, False])
@pytest.mark.parametrize("actor", [USER, ANNA])
def test_a_reviewer_passed_by_an_update_in_review_follows_crew_review(swarm, board, actor, crew_review):
    swarm.config.swarm.crew_review = crew_review
    task, _ = in_review(board, done=(BOB, CARL), reviewer=DINA)
    swarm.notified.clear()
    board.update(actor, task.id, reviewer=EVA)
    expected = {"id": task.id, "title": task.title, "by": actor}
    if crew_review:
        assert review_keys(swarm) == [(EVA, "n_crew_review_requested")]
        assert notes_of(swarm, EVA, "n_crew_review_requested") == [{**expected, "n": 2}]
    else:  # an ordinary review
        assert review_keys(swarm) == [(EVA, "n_review_requested")]
        assert notes_of(swarm, EVA, "n_review_requested") == [expected]
    assert keys_for(swarm, DINA) == ["n_review_cancelled"]


@pytest.mark.parametrize("crew_review, key", [(False, "n_review_requested"), (True, "n_crew_review_requested")])
def test_the_users_reassignment_re_picks_without_the_tasks_own_reviewer(swarm, board, crew_review, key):
    swarm.config.swarm.crew_review = crew_review
    task, _ = in_review(board)
    swarm.notified.clear()
    board.update(USER, task.id, assignee=CARL)
    after = board.get(task.id)
    assert after.assignee == CARL and after.reviewer not in (CARL, ANNA, USER)
    assert review_keys(swarm) == [(after.reviewer, key)]


# --- crew_review off: no second review ----------------------------------------------------------------------------

def no_pick(*args, **kw):
    raise AssertionError("the board picks no reviewer for a crew task while crew_review is off")


@pytest.mark.parametrize("done", [True, False])
def test_with_crew_review_off_the_owners_completion_is_done_at_once_whatever_independent_review_says(
        swarm, board, monkeypatch, done):
    swarm.config.swarm.crew_review = False
    assert board.independent_review
    monkeypatch.setattr(board, "pick_reviewer", no_pick)
    task, subs = crew_task(board, titles=("parser", "cli"), creator=DINA)
    for sub, name in zip(subs, (BOB, CARL)):
        if done:
            finish_raw(board, sub.id, Status.DONE, f"{sub.title} done", assignee=name)
        else:  # all cancelled: the owner did the work itself
            finish_raw(board, sub.id, Status.CANCELLED)
    swarm.notified.clear()
    board.complete(ANNA, task.id, "the whole")
    after = board.get(task.id)
    assert (after.status, after.reviewer, after.result) == (Status.DONE, None, "the whole")
    assert review_keys(swarm) == []
    assert [kw["status"] for kw in notes_of(swarm, DINA, "n_task_closed")] == [Status.DONE]
    assert events(swarm, "task.updated")[-1]["data"]["changes"]["status"] == [Status.IN_PROGRESS, Status.DONE]


@pytest.mark.parametrize("independent", [True, False])
@pytest.mark.parametrize("named_by", ["create", "update", "user"])
def test_with_crew_review_off_a_named_reviewer_reviews_the_crew_task_as_any_task(swarm, board, monkeypatch,
                                                                                    named_by, independent):
    """The reviewer named in task_create or task_update, or set by USER, reviews the owner's completion as an
    ordinary review; a return goes back to the owner, and its next completion goes to the same reviewer."""
    swarm.config.swarm.crew_review = False
    swarm.config.swarm.independent_review = independent
    monkeypatch.setattr(board, "pick_reviewer", no_pick)
    if named_by == "create":
        task = board.create(DINA, "Auth", crew=2, directions=["parser", "cli"], reviewer=EVA)
    else:
        task = board.create(DINA, "Auth", crew=2, directions=["parser", "cli"])
        board.update(USER if named_by == "user" else DINA, task.id, reviewer=EVA)
    for sub, name in zip(board.subtasks_of(task.id), (BOB, CARL)):
        finish_raw(board, sub.id, Status.DONE, f"{sub.title} done", assignee=name)
    set_raw(board, task.id, assignee=ANNA, status=Status.IN_PROGRESS)
    swarm.notified.clear()
    board.complete(ANNA, task.id, "whole v1")
    after = board.get(task.id)
    assert (after.status, after.reviewer) == (Status.REVIEW, EVA)
    assert review_keys(swarm) == [(EVA, "n_review_requested")]
    assert notes_of(swarm, EVA, "n_review_requested") == [{"id": task.id, "title": "Auth", "by": ANNA}]
    board.review(EVA, task.id, False, "the cli does not parse")
    after = board.get(task.id)
    assert (after.status, after.assignee, after.reviewer) == (Status.IN_PROGRESS, ANNA, EVA)
    (kw,) = notes_of(swarm, ANNA, "n_review_rejected")
    assert kw["ending"] == prompts.t("en", "review_rejected_crew", id=task.id)
    swarm.notified.clear()
    board.complete(ANNA, task.id, "whole v2")
    after = board.get(task.id)
    assert (after.status, after.reviewer, after.result) == (Status.REVIEW, EVA, "whole v2")
    assert review_keys(swarm) == [(EVA, "n_review_requested")]
    board.review(EVA, task.id, True)
    assert board.get(task.id).status == Status.DONE


def test_with_crew_review_off_the_user_as_the_named_reviewer_is_asked(swarm, board, monkeypatch):
    swarm.config.swarm.crew_review = False
    monkeypatch.setattr(board, "pick_reviewer", no_pick)
    task, subs = crew_task(board, titles=("parser",), creator=DINA, reviewer=USER)
    finish_raw(board, subs[0].id, Status.DONE, "parser done", assignee=BOB)
    swarm.notified.clear()
    board.complete(ANNA, task.id, "the whole")
    assert (board.get(task.id).status, board.get(task.id).reviewer) == (Status.REVIEW, USER)
    assert notes_of(swarm, USER, "u_review_requested") == [{"by": ANNA, "id": task.id, "title": task.title}]
    assert review_keys(swarm) == []


def test_with_crew_review_off_a_reviewer_cleared_by_taking_a_direction_leaves_no_review(swarm, board, monkeypatch):
    swarm.config.swarm.crew_review = False
    monkeypatch.setattr(board, "pick_reviewer", no_pick)
    task, subs = crew_task(board, titles=("parser", "cli"), creator=DINA, reviewer=BOB)
    board.claim(BOB, subs[0].id)  # its reviewer works on a direction now: it is no longer the crew task's
    assert board.get(task.id).reviewer is None
    finish_raw(board, subs[0].id, Status.DONE, "parser done")
    finish_raw(board, subs[1].id, Status.DONE, "cli done", assignee=CARL)
    board.complete(ANNA, task.id, "the whole")
    assert (board.get(task.id).status, board.get(task.id).reviewer) == (Status.DONE, None)


@pytest.mark.parametrize("approve", [True, False])
def test_a_toggle_of_crew_review_off_mid_review_changes_nothing_for_that_review(swarm, board, approve):
    task, subs = crew_task(board, titles=("parser", "cli"), creator=DINA)
    for sub, name in zip(subs, (BOB, CARL)):
        finish_raw(board, sub.id, Status.DONE, f"{sub.title} done", assignee=name)
    board.complete(ANNA, task.id, "whole v1")  # crew_review on: the check of the whole, outside the crew
    assert keys_for(swarm, EVA) == ["n_crew_review_requested"]
    swarm.config.swarm.crew_review = False
    after = board.get(task.id)
    assert (after.status, after.reviewer, after.result) == (Status.REVIEW, EVA, "whole v1")
    assert [t.id for t in board.reviews_of(EVA)] == [task.id]
    board.review(EVA, task.id, approve, "" if approve else "the parts do not fit")
    after = board.get(task.id)
    if approve:
        assert after.status == Status.DONE
        return
    assert (after.status, after.assignee, after.reviewer) == (Status.IN_PROGRESS, ANNA, EVA)
    # the next completion follows the setting now: EVA was the board's pick, not named, so no second review
    swarm.notified.clear()
    board.complete(ANNA, task.id, "whole v2")
    assert (board.get(task.id).status, board.get(task.id).result) == (Status.DONE, "whole v2")
    assert review_keys(swarm) == []


@pytest.mark.parametrize("renamed", [False, True])
def test_with_crew_review_off_a_pick_kept_from_a_review_before_the_crew_is_not_the_crew_tasks_own(
        swarm, board, monkeypatch, renamed):
    """A plain task the board's pick returned ("split it") becomes a crew task: that pick is not named, so the owner's
    completion is done; passing the same reviewer in task_update names it, and then it reviews as any task."""
    swarm.config.swarm.crew_review = False
    task = board.create(BOB, "Auth")
    board.claim(ANNA, task.id)
    board.complete(ANNA, task.id, "all of it")
    picked = board.get(task.id).reviewer
    assert picked not in (None, ANNA, USER) and not board.get(task.id).reviewer_named
    board.review(picked, task.id, False, "too big: split it")
    assert (board.get(task.id).status, board.get(task.id).reviewer) == (Status.IN_PROGRESS, picked)
    board.update(ANNA, task.id, crew=2, directions=["parser"])
    (sub,) = board.subtasks_of(task.id)
    assert sub.assignee == ANNA  # the owner's own part
    finish_raw(board, sub.id, Status.DONE, "parser done")
    if renamed:
        board.update(BOB, task.id, reviewer=picked)
        assert board.get(task.id).reviewer_named
    monkeypatch.setattr(board, "pick_reviewer", no_pick)
    swarm.notified.clear()
    board.complete(ANNA, task.id, "the whole")
    after = board.get(task.id)
    if renamed:
        assert (after.status, after.reviewer) == (Status.REVIEW, picked)
        assert review_keys(swarm) == [(picked, "n_review_requested")]
    else:
        assert (after.status, after.result) == (Status.DONE, "the whole")
        assert review_keys(swarm) == []


def test_with_crew_review_off_and_independent_review_off_a_reviewer_not_named_reviews_nothing(swarm, board):
    swarm.config.swarm.crew_review = False
    swarm.config.swarm.independent_review = False
    task, subs = crew_task(board, titles=("parser",), creator=DINA)
    finish_raw(board, subs[0].id, Status.DONE, "parser done", assignee=BOB)
    set_raw(board, task.id, reviewer=EVA)   # e.g. a hand-over's pick, kept after a return
    board.complete(ANNA, task.id, "the whole")
    assert (board.get(task.id).status, review_keys(swarm)) == (Status.DONE, [])


def test_the_board_clears_a_named_reviewer_mark_when_it_re_picks(swarm, board):
    """A named reviewer the board replaces (a hand-over) is the board's pick from then on."""
    task, _ = crew_task(board, titles=("parser",), creator=DINA, reviewer=CARL)
    assert board.get(task.id).reviewer_named
    set_raw(board, task.id, status=Status.REVIEW, result="whole")
    board.hand_over_review(task.id, exclude=())
    after = board.get(task.id)
    assert after.reviewer not in (CARL, None) and not after.reviewer_named


def test_a_toggle_of_crew_review_on_mid_review_changes_nothing_for_that_review(swarm, board):
    swarm.config.swarm.crew_review = False
    task, subs = crew_task(board, titles=("parser", "cli"), creator=DINA, reviewer=DINA)
    for sub, name in zip(subs, (BOB, CARL)):
        finish_raw(board, sub.id, Status.DONE, f"{sub.title} done", assignee=name)
    board.complete(ANNA, task.id, "whole")  # an ordinary review by its own reviewer
    assert keys_for(swarm, DINA)[-1] == "n_review_requested"
    swarm.config.swarm.crew_review = True
    after = board.get(task.id)
    assert (after.status, after.reviewer) == (Status.REVIEW, DINA)
    board.review(DINA, task.id, True)
    assert board.get(task.id).status == Status.DONE


# --- a returned crew task ------------------------------------------------------------------------------------------

def test_a_returned_crew_task_goes_back_to_its_owner_with_the_crew_ending(swarm, board):
    task, _ = in_review(board)
    swarm.notified.clear()
    board.review(CARL, task.id, False, "the parts do not fit")
    after = board.get(task.id)
    assert (after.status, after.assignee, after.reviewer) == (Status.IN_PROGRESS, ANNA, CARL)
    (kw,) = notes_of(swarm, ANNA, "n_review_rejected")
    assert kw["ending"] == prompts.t("en", "review_rejected_crew", id=task.id)
    assert kw["ending"].startswith(f"#{task.id} is a crew task: fix it yourself, or with new directions")
    assert "Draft" not in kw["ending"] and "part0 done" not in kw["ending"]  # the owner wrote the result itself
    assert quiet(swarm, ANNA, "n_review_rejected") == [False]


def test_the_crew_ending_in_russian(swarm, board):
    task, _ = in_review(board, owner=EVA)
    board.review(CARL, task.id, False, "не сходится")
    row = swarm.db.one("SELECT text FROM notifications WHERE recipient = ? AND kind = ?", (EVA, "n_review_rejected"))
    assert row["text"].endswith(f"\n#{task.id} — групповая задача: исправь её сам(а) или новыми направлениями "
                                f"(task_update directions) и, когда они завершатся, обнови результат #{task.id}; "
                                "затем снова вызови `task_complete`.")


def test_a_returned_ordinary_task_keeps_the_plain_ending(swarm, board):
    task = board.create(DINA, "plain")
    board.claim(ANNA, task.id)
    board.complete(ANNA, task.id, "ok")
    board.review(board.get(task.id).reviewer, task.id, False, "redo")
    assert notes_of(swarm, ANNA, "n_review_rejected")[0]["ending"] == "Fix it, then call `task_complete` again."


def test_a_returned_crew_task_without_an_owner_is_ready_and_its_next_owner_reads_the_draft(swarm, board):
    task, subs = in_review(board, done=(BOB, CARL), reviewer=DINA)
    set_raw(board, task.id, assignee=None)  # its owner left while it waited
    board.review(DINA, task.id, False, "the parts do not fit")
    after = board.get(task.id)
    assert (after.status, after.assignee) == (Status.TODO, None)
    assert task.id in [t.id for t in board.ready_unassigned()]
    notes: list[str] = []
    board.claim(EVA, task.id, notes=notes)
    assert notes == [" " + board._ending(board.get(task.id), "finished", "owner", "ru")]
    assert notes[0].startswith(" " + prompts.t("ru", "owner_prefix_finished"))
    assert "part0 done" in notes[0] and "part1 done" in notes[0]


def test_a_returned_crew_task_completed_again_after_new_directions(swarm, board):
    task, subs = crew_task(board, titles=("parser", "cli"), creator=DINA)
    finish_raw(board, subs[0].id, Status.DONE, "parser done", assignee=BOB)
    finish_raw(board, subs[1].id, Status.DONE, "cli done", assignee=CARL)
    board.complete(ANNA, task.id, "whole v1")
    whole_reviewer = board.get(task.id).reviewer
    board.review(whole_reviewer, task.id, False, "no tests")
    board.update(ANNA, task.id, directions=["tests"])
    new = board.subtasks_of(task.id)[-1]
    assert (new.title, new.assignee, new.status) == ("tests", ANNA, Status.IN_PROGRESS)  # its own part
    board.update(ANNA, new.id, goal="the tests pass")
    board.complete(ANNA, new.id, "tests done")
    swarm.notified.clear()
    board.review(board.get(new.id).reviewer, new.id, True)
    # the owner is told again, with the draft of all its directions, the earlier ones first
    (kw,) = notes_of(swarm, ANNA, "n_subtasks_done")
    assert (kw["total"], kw["counts"]) == (3, "3 done")
    ending = kw["ending"]
    places = [ending.index(f"#{s.id} [done]") for s in (*subs, new)]
    assert places == sorted(places)
    assert all(text in ending for text in ("parser done", "cli done", "tests done"))
    swarm.notified.clear()
    board.complete(ANNA, task.id, "whole v2")
    after = board.get(task.id)
    assert (after.status, after.reviewer) == (Status.REVIEW, whole_reviewer)  # kept from the first check
    assert notes_of(swarm, whole_reviewer, "n_crew_review_requested") == [
        {"id": task.id, "title": task.title, "by": ANNA, "n": 3}]


# --- n_crew_owner with all directions finished ---------------------------------------------------------------------

def test_n_crew_owner_carries_the_draft_when_all_directions_are_finished(swarm, board):
    task, subs = crew_task(board, titles=("parser", "cli"), owner=None, creator=DINA)
    finish_raw(board, subs[0].id, Status.DONE, "parser done", assignee=BOB)
    finish_raw(board, subs[1].id, Status.CANCELLED)
    swarm.notified.clear()
    board.update(DINA, task.id, assignee=BOB)
    assert keys_for(swarm, BOB) == ["n_crew_owner"]  # never also n_assigned
    (kw,) = notes_of(swarm, BOB, "n_crew_owner")
    assert kw["directions"] == ""  # the draft lists them
    assert kw["instructions"] == " " + board._ending(board.get(task.id), "finished", "owner", "en")
    assert kw["instructions"].startswith(" All its directions are finished: check that they fit together")
    assert f"\nDraft for #{task.id}'s result" in kw["instructions"] and "parser done" in kw["instructions"]


def test_n_crew_owner_after_the_creators_reopening_carries_the_draft_of_all_directions(swarm, board):
    task, subs = crew_task(board, titles=("parser", "cli", "tests"), creator=DINA)
    for sub, name in zip(subs, (BOB, CARL, ANNA)):
        finish_raw(board, sub.id, Status.DONE, f"{sub.title} done", assignee=name)
    finish_raw(board, task.id, Status.DONE, "whole", assignee=ANNA)
    swarm.notified.clear()
    board.update(DINA, task.id, status="todo")
    after = board.get(task.id)
    assert (after.status, after.assignee) == (Status.TODO, ANNA)
    assert keys_for(swarm, ANNA) == ["n_crew_owner"]
    (kw,) = notes_of(swarm, ANNA, "n_crew_owner")
    assert kw["directions"] == ""
    places = [kw["instructions"].index(f"#{s.id} [done] \"{s.title}\"") for s in subs]
    assert places == sorted(places)
    assert all(f"{s.title} done" in kw["instructions"] for s in subs)


# --- leaving: the owner, a direction's holder ----------------------------------------------------------------------

def test_an_owner_releasing_its_crew_task_keeps_its_own_direction(swarm, board):
    task, subs = crew_task(board)
    work(board, subs[0].id, ANNA)
    work(board, subs[1].id, BOB)
    swarm.notified.clear()
    board.update(ANNA, task.id, status="todo")
    after = board.get(task.id)
    assert (after.status, after.assignee) == (Status.TODO, None)
    assert task.id in [t.id for t in board.ready_unassigned()]  # its next claim makes a new owner
    own = board.get(subs[0].id)
    assert (own.status, own.assignee) == (Status.IN_PROGRESS, ANNA)
    assert set(board.crew_working(after)) == {ANNA, BOB}  # as its direction's assignee it keeps its place
    assert not [k for _, k, _ in swarm.notified if k == "n_direction_freed"]


def test_an_owner_leaving_leaves_its_directions_going(swarm, board):
    task, subs = crew_task(board)
    work(board, subs[0].id, ANNA)
    work(board, subs[1].id, BOB)
    swarm.notified.clear()
    released = board.release_all(ANNA, USER)
    assert sorted(released) == sorted([task.id, subs[0].id])
    assert [(t.status, t.assignee) for t in (board.get(task.id), board.get(subs[0].id))] == [
        (Status.TODO, None), (Status.TODO, None)]
    assert (board.get(subs[1].id).status, board.get(subs[1].id).assignee) == (Status.IN_PROGRESS, BOB)
    ready = [t.id for t in board.ready_unassigned()]
    assert {task.id, subs[0].id, subs[2].id} <= set(ready)
    # nobody is told: the next owner reads the directions in the owner sentence
    assert not [k for _, k, _ in swarm.notified if k == "n_direction_freed"]


@pytest.mark.parametrize("independent", [True, False])
def test_a_direction_holder_leaving_frees_only_the_directions_back_in_todo(swarm, board, independent):
    swarm.config.swarm.independent_review = independent
    task, subs = crew_task(board, crew=4, titles=("parser", "cli", "docs"))
    work(board, subs[0].id, BOB)
    work(board, subs[1].id, BOB, Status.REVIEW, reviewer=CARL, result="cli done", goal="cli")
    work(board, subs[2].id, BOB, Status.TODO)  # assigned, not taken yet
    swarm.notified.clear()
    board.release_all(BOB, USER)
    freed = [subs[0], subs[2]] if independent else [subs[0], subs[1], subs[2]]
    assert sorted(notes_of(swarm, ANNA, "n_direction_freed"), key=lambda kw: kw["id"]) == [
        {"id": s.id, "title": s.title, "by": BOB} for s in freed]
    assert set(quiet(swarm, ANNA, "n_direction_freed")) == {True}
    if independent:  # handed in: it stays in review without an assignee, and is not free
        cli = board.get(subs[1].id)
        assert (cli.status, cli.assignee) == (Status.REVIEW, None)


def test_a_crew_task_held_by_the_user_hears_nothing_of_a_freed_direction(swarm, board):
    task, subs = crew_task(board, owner=USER)
    work(board, subs[0].id, BOB)
    swarm.notified.clear()
    board.release_all(BOB, ANNA)
    assert not [k for _, k, _ in swarm.notified if k == "n_direction_freed"]  # USER is never told


def test_a_subtask_of_an_ordinary_parent_freed_tells_nobody(swarm, board):
    parent = board.create(ANNA, "parent", assignee=ANNA)
    sub = board.create(ANNA, "sub", parent_id=parent.id)
    work(board, sub.id, BOB)
    swarm.notified.clear()
    board.release_all(BOB, USER)
    assert board.get(sub.id).assignee is None
    assert "n_direction_freed" not in keys_for(swarm, ANNA)
