"""Finishing a parent and a crew task, and closing one (docs/SPEC.md §4 side effects, §4.2 "Finishing", "Leaving
and closing", "Who controls subtasks"): what an agent's completion needs, who is told when the subtasks finish, and
the cascade when a parent is closed."""

from __future__ import annotations

import pytest

from dreamteam import prompts
from dreamteam.board import Board
from dreamteam.models import USER, AgentKind, AgentStatus, Status, Task, ToolError
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
                    "may cancel the ones no longer needed (task_update status=cancelled).")
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


def test_a_direction_needs_its_goal_before_it_is_handed_in(board):
    task, subs = crew_task(board)
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


def test_an_owner_that_created_the_failed_direction_gets_only_n_task_closed(swarm, board):
    task, subs = crew_task(board)
    work(board, subs[0].id, BOB)
    work(board, subs[1].id, CARL, goal="cli")
    swarm.notified.clear()
    board.update(CARL, subs[1].id, status="failed")
    assert keys_for(swarm, ANNA) == ["n_task_closed"]


def test_exactly_one_notice_for_the_owner_that_created_a_direction_its_holder_cancels(swarm, board):
    task, subs = crew_task(board)
    work(board, subs[0].id, BOB)
    work(board, subs[1].id, CARL)
    swarm.notified.clear()
    board.update(BOB, subs[0].id, status="cancelled")
    assert keys_for(swarm, ANNA) == ["n_task_closed"]
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
