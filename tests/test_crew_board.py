"""Crew tasks and subtasks on the board (docs/SPEC.md §4.2): the reads, the working set, offers, liveness marks and
the texts the board renders for agents."""

from __future__ import annotations

import re

import pytest

from dreamteam import prompts
from dreamteam.board import CREW_MAX, MARK_FREE_DIRECTIONS, MARK_SUBTASKS_DONE, OWNER_LIST_MAX, Board
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


def crew_task(board: Board, crew: int = 3, titles=("parser", "cli", "docs"), owner: str | None = ANNA, **kw):
    """A crew task created by ANNA, held by `owner` in progress (None: todo without an owner)."""
    task, subs = make_crew_raw(board, ANNA, crew, list(titles), **kw)
    if owner is not None:
        set_raw(board, task.id, assignee=owner, status=Status.IN_PROGRESS)
    return board.get(task.id), subs


def work(board: Board, sid: int, name: str, status: str = Status.IN_PROGRESS) -> Task:
    return set_raw(board, sid, assignee=name, status=status)


# --- the basics -----------------------------------------------------------------------------------------------

def test_the_crew_constants_and_the_switch(swarm, board):
    assert CREW_MAX == 8
    assert board.crew_review is True
    swarm.config.swarm.crew_review = False
    assert board.crew_review is False  # read on every call


def test_task_fields_in_to_dict_and_short(board):
    task, subs = crew_task(board)
    work(board, subs[0].id, BOB)
    task = board.get(task.id)
    data = task.to_dict()
    assert data["crew"] == 3 and data["crew_working"] == [ANNA, BOB]
    assert "crew 2/3" in task.short()
    assert "crew" not in board.get(subs[1].id).short()
    assert board.get(subs[1].id).to_dict()["crew_working"] == []


# --- subtasks_of, contributors_of ------------------------------------------------------------------------------

def test_subtasks_of_in_creation_order_filled_and_open_only(board):
    parent = board.create(ANNA, "parent")
    a = board.create(ANNA, "a", parent_id=parent.id, priority=0)
    b = board.create(ANNA, "b", parent_id=parent.id, priority=3, depends_on=[a.id])
    c = board.create(ANNA, "c", parent_id=parent.id)
    board.create(ANNA, "elsewhere")
    finish_raw(board, c.id, Status.CANCELLED)
    subs = board.subtasks_of(parent.id)
    assert [s.id for s in subs] == [a.id, b.id, c.id]  # id ascending, not by priority
    assert subs[1].blocked_by == [a.id]  # filled
    assert [s.id for s in board.subtasks_of(parent.id, open_only=True)] == [a.id, b.id]
    assert board.subtasks_of(a.id) == []


def test_contributors_are_the_assignees_of_its_subtasks_in_any_status(board):
    task, subs = crew_task(board)
    work(board, subs[0].id, BOB)
    finish_raw(board, subs[1].id, Status.DONE, "ok", assignee=CARL)
    finish_raw(board, subs[2].id, Status.CANCELLED, assignee=DINA)
    assert board.contributors_of(task) == {BOB, CARL, DINA}
    assert ANNA not in board.contributors_of(task)  # the owner holds the crew task, not a direction


# --- the working set ---------------------------------------------------------------------------------------------

def test_the_working_set_counts_the_owner_once(board):
    task, subs = crew_task(board)
    work(board, subs[0].id, ANNA)  # the owner's own direction takes no second place
    work(board, subs[1].id, BOB, Status.WAITING)
    assert board.crew_working(task) == [ANNA, BOB]
    assert board.get(task.id).crew_working == [ANNA, BOB]


def test_a_direction_in_review_or_todo_takes_no_place(board):
    task, subs = crew_task(board)
    work(board, subs[0].id, BOB, Status.REVIEW)
    work(board, subs[1].id, CARL, Status.TODO)
    finish_raw(board, subs[2].id, Status.DONE, "ok", assignee=DINA)
    assert board.crew_working(task) == [ANNA]


def test_user_takes_no_place(board):
    task, subs = crew_task(board, owner=USER)
    work(board, subs[0].id, USER)
    work(board, subs[1].id, BOB)
    assert board.crew_working(board.get(task.id)) == [BOB]


def test_the_owner_counts_only_while_the_crew_task_is_open(board):
    task, subs = crew_task(board)
    work(board, subs[0].id, BOB)
    finish_raw(board, task.id, Status.CANCELLED, assignee=ANNA)
    assert board.crew_working(board.get(task.id)) == [BOB]


def test_list_fills_the_working_sets_with_one_query(board, monkeypatch):
    tasks = []
    for _ in range(3):
        task, subs = crew_task(board)
        work(board, subs[0].id, BOB)
        tasks.append(task)
    calls: list[str] = []
    real = board.db.all
    monkeypatch.setattr(board.db, "all", lambda sql, params=(): calls.append(sql) or real(sql, params))
    listed = {t.id: t for t in board.list()}
    assert sum("parent_id IN" in sql for sql in calls) == 1
    for task in tasks:
        assert listed[task.id].crew_working == [ANNA, BOB]


# --- finish states -----------------------------------------------------------------------------------------------

def test_finish_states_in_one_query(board, monkeypatch):
    lonely = board.create(ANNA, "no subtasks")
    open_parent, (o1, o2) = make_crew_raw(board, ANNA, 0, ["o1", "o2"])
    finish_raw(board, o1.id, Status.DONE, "ok")
    plain, (p1, p2) = make_crew_raw(board, ANNA, 0, ["p1", "p2"])
    finish_raw(board, p1.id, Status.DONE, "ok")
    finish_raw(board, p2.id, Status.FAILED, "no")
    failed, (f1, f2) = crew_task(board, titles=("f1", "f2"))
    finish_raw(board, f1.id, Status.FAILED, "no")
    finish_raw(board, f2.id, Status.CANCELLED)
    cancelled, (c1,) = crew_task(board, titles=("c1",))
    finish_raw(board, c1.id, Status.CANCELLED)
    done, (d1, d2) = crew_task(board, titles=("d1", "d2"))
    finish_raw(board, d1.id, Status.DONE, "ok")
    finish_raw(board, d2.id, Status.CANCELLED)
    in_review, (r1,) = crew_task(board, titles=("r1",))
    work(board, r1.id, BOB, Status.REVIEW)
    calls: list[str] = []
    real = board.db.all
    monkeypatch.setattr(board.db, "all", lambda sql, params=(): calls.append(sql) or real(sql, params))
    ids = [lonely.id, open_parent.id, plain.id, failed.id, cancelled.id, done.id, in_review.id, 999]
    states = board.finish_states(ids)
    assert len(calls) == 1
    assert states == {lonely.id: None, open_parent.id: None, plain.id: "finished", failed.id: "failed",
                      cancelled.id: "finished", done.id: "finished", in_review.id: None, 999: None}
    assert board.finish_states([]) == {}


def test_finish_info_counts_per_status(board):
    task, subs = crew_task(board, titles=("a", "b", "c"))
    finish_raw(board, subs[0].id, Status.DONE, "ok")
    finish_raw(board, subs[1].id, Status.DONE, "ok")
    finish_raw(board, subs[2].id, Status.CANCELLED)
    assert board._finish_info([task.id]) == {task.id: ("finished", {"done": 2, "cancelled": 1})}


# --- offers ------------------------------------------------------------------------------------------------------

def ready_ids(board: Board) -> set[int]:
    return {t.id for t in board.ready_unassigned()}


def test_a_free_direction_is_offered_while_its_crew_task_takes_it(board):
    task, subs = crew_task(board, crew=2)
    assert {s.id for s in subs} <= ready_ids(board)
    work(board, subs[0].id, BOB)  # ANNA + BOB: full
    assert ready_ids(board).isdisjoint({subs[1].id, subs[2].id})
    work(board, subs[0].id, BOB, Status.REVIEW)  # handed in: a place again
    assert {subs[1].id, subs[2].id} <= ready_ids(board)


def test_no_offer_while_the_crew_task_is_in_review_terminal_or_blocked(board):
    dep = board.create(ANNA, "dep")
    task, subs = crew_task(board, depends_on=[dep.id])
    assert ready_ids(board).isdisjoint(s.id for s in subs)  # blocked crew task
    finish_raw(board, dep.id, Status.DONE, "ok")
    assert {s.id for s in subs} <= ready_ids(board)
    for status in (Status.REVIEW, Status.DONE, Status.CANCELLED):
        set_raw(board, task.id, status=status)
        assert ready_ids(board).isdisjoint(s.id for s in subs), status
    set_raw(board, task.id, status=Status.WAITING)
    assert {s.id for s in subs} <= ready_ids(board)


def test_a_crew_task_without_an_owner_offers_its_directions_and_itself(board):
    task, subs = crew_task(board, owner=None)
    assert {task.id, *(s.id for s in subs)} <= ready_ids(board)


def test_subtasks_of_an_ordinary_parent_are_offered_as_before(board):
    parent = board.create(ANNA, "parent")
    sub = board.create(ANNA, "sub", parent_id=parent.id)
    set_raw(board, parent.id, status=Status.REVIEW)
    assert sub.id in ready_ids(board)


# --- free directions, open directions -------------------------------------------------------------------------------

def test_free_directions_and_open_directions(board):
    task, subs = crew_task(board)
    work(board, subs[0].id, BOB)
    finish_raw(board, subs[1].id, Status.CANCELLED)
    assert [s.id for s in board.free_directions(task)] == [subs[2].id]
    assert board.has_open_directions(task)
    set_raw(board, task.id, status=Status.REVIEW)
    assert board.free_directions(board.get(task.id)) == []
    finish_raw(board, subs[0].id, Status.DONE, "ok")
    finish_raw(board, subs[2].id, Status.DONE, "ok")
    assert not board.has_open_directions(board.get(task.id))
    plain = board.create(ANNA, "plain")
    board.create(ANNA, "sub", parent_id=plain.id)
    assert not board.has_open_directions(plain) and board.free_directions(plain) == []


# --- participants --------------------------------------------------------------------------------------------------

def test_the_crew_takes_part_in_the_discussion_while_its_directions_are_open(board):
    task, subs = crew_task(board, crew=4, titles=("a", "b", "c", "d"))
    work(board, subs[0].id, BOB)
    work(board, subs[1].id, CARL, Status.REVIEW)
    finish_raw(board, subs[2].id, Status.DONE, "ok", assignee=DINA)
    assert board.participants(task.id) == {ANNA, BOB, CARL}


def test_an_ordinary_parent_has_no_crew_participants(board):
    parent = board.create(ANNA, "parent")
    sub = board.create(ANNA, "sub", parent_id=parent.id)
    work(board, sub.id, BOB)
    assert board.participants(parent.id) == {ANNA}


def test_a_post_wakes_the_crew(swarm, board):
    task, subs = crew_task(board)
    work(board, subs[0].id, BOB)
    board.post(CARL, task.id, "interfaces: see api.md")
    assert {r for r, k, _ in swarm.notified if k == "n_discussion"} == {ANNA, BOB}


# --- structure -----------------------------------------------------------------------------------------------------

def test_ancestors_and_crews_below(board):
    top = board.create(ANNA, "top")
    mid = board.create(ANNA, "mid", parent_id=top.id)
    low = board.create(ANNA, "low", parent_id=mid.id)
    assert [r["id"] for r in board._ancestors(low.id)] == [low.id, mid.id, top.id]
    assert board._ancestors(None) == []
    assert not board._has_crew_below(top.id)
    set_raw(board, low.id, crew=2)
    assert board._has_crew_below(top.id) and board._has_crew_below(mid.id)
    assert not board._has_crew_below(low.id)
    row = board._ancestors(mid.id)[0]
    assert (row["crew"], row["status"], row["created_by"], row["assignee"]) == (0, "todo", ANNA, None)


def test_the_parent_loop_check_still_holds(board):
    from dreamteam.models import ToolError
    top = board.create(ANNA, "top")
    low = board.create(ANNA, "low", parent_id=top.id)
    with pytest.raises(ToolError, match=f"Task #{low.id} is a subtask of #{top.id}"):
        board.update(USER, top.id, parent_id=low.id)


# --- liveness marks ------------------------------------------------------------------------------------------------

def test_a_holder_whose_subtasks_are_all_terminal_gets_the_subtasks_done_mark(board):
    parent = board.create(ANNA, "parent")
    set_raw(board, parent.id, assignee=BOB, status=Status.WAITING)
    sub = board.create(ANNA, "sub", parent_id=parent.id)
    assert board.stall_marks(BOB) == frozenset()
    finish_raw(board, sub.id, Status.DONE, "ok")
    assert board.stall_marks(BOB) == frozenset({(parent.id, MARK_SUBTASKS_DONE)})
    lonely = board.create(BOB, "no subtasks")
    set_raw(board, lonely.id, assignee=BOB, status=Status.WAITING)
    assert board.stall_marks(BOB) == frozenset({(parent.id, MARK_SUBTASKS_DONE)})


def test_the_owner_gets_the_free_directions_mark_while_it_works_on_none(board):
    task, subs = crew_task(board)
    work(board, subs[0].id, BOB)
    assert board.stall_marks(ANNA) == frozenset({(task.id, MARK_FREE_DIRECTIONS)})
    work(board, subs[1].id, ANNA, Status.TODO)  # its own part, not started: still works on none
    assert board.stall_marks(ANNA) == frozenset({(task.id, MARK_FREE_DIRECTIONS)})
    work(board, subs[1].id, ANNA)
    assert board.stall_marks(ANNA) == frozenset()


def test_no_free_directions_mark_while_the_crew_task_is_blocked_or_none_is_free(board):
    dep = board.create(ANNA, "dep")
    task, subs = crew_task(board, depends_on=[dep.id])
    assert board.stall_marks(ANNA) == frozenset()
    finish_raw(board, dep.id, Status.DONE, "ok")
    assert board.stall_marks(ANNA) == frozenset({(task.id, MARK_FREE_DIRECTIONS)})
    for sub, name in zip(subs, (BOB, CARL, DINA)):
        work(board, sub.id, name)
    assert board.stall_marks(ANNA) == frozenset()


def test_an_owner_of_a_todo_crew_task_gets_the_free_directions_mark(board):
    task, subs = crew_task(board)
    set_raw(board, task.id, status=Status.TODO)  # assigned by its creator, not claimed yet
    assert board.stall_marks(ANNA) == frozenset({(task.id, MARK_FREE_DIRECTIONS)})


def test_mark_text_in_the_members_language(swarm, board):
    task, subs = crew_task(board, owner=EVA)
    parent = board.create(ANNA, "parent")
    set_raw(board, parent.id, assignee=EVA, status=Status.IN_PROGRESS)
    finish_raw(board, board.create(ANNA, "sub", parent_id=parent.id).id, Status.DONE, "ok")
    marks = board.stall_marks(EVA)
    assert marks == {(task.id, MARK_FREE_DIRECTIONS), (parent.id, MARK_SUBTASKS_DONE)}
    text = board.mark_text(EVA, marks)
    free = ", ".join(f"#{s.id} «{s.title}»" for s in subs)
    assert text == (prompts.t("ru", "mark_free_directions", id=task.id, directions=free)
                    + prompts.t("ru", "mark_subtasks_done", id=parent.id, title="parent",
                                ending=prompts.t("ru", "owner_prefix_plain") + prompts.t("ru", "ending_plain")))
    assert text.startswith(" ")
    assert board.mark_text(EVA, []) == ""


def test_a_mark_that_no_longer_applies_gives_no_sentence(board):
    task, subs = crew_task(board)
    marks = board.stall_marks(ANNA)
    for sub, name in zip(subs, (BOB, CARL, DINA)):
        work(board, sub.id, name)
    assert board.mark_text(ANNA, marks) == ""


def test_two_drafts_in_one_wake_share_the_budget(swarm, board):
    swarm.config.agents.tool_output_chars = 2000  # B = 1000
    marks = set()
    for _ in range(2):
        task, subs = crew_task(board, titles=("a", "b"))
        for sub in subs:
            finish_raw(board, sub.id, Status.DONE, "r" * 3000, assignee=BOB)
        marks.add((task.id, MARK_SUBTASKS_DONE))
    assert board.stall_marks(ANNA) == marks
    text = board.mark_text(ANNA, marks)
    expected = ""
    for tid in sorted(tid for tid, _ in marks):
        task = board.get(tid)
        draft = board._crew_draft(task, "en", budget=500)
        assert 0 < len(draft) <= 500 and draft != board._crew_draft(task, "en")  # B // 2, not B
        ending = prompts.t("en", "owner_prefix_finished") + prompts.t("en", "ending_crew_finished", id=tid) + draft
        expected += prompts.t("en", "mark_subtasks_done", id=tid, title=task.title, ending=ending)
    assert text == expected
    assert text.count("\nDraft for #") == 2


# --- endings -------------------------------------------------------------------------------------------------------

def plain_parent(board: Board) -> Task:
    parent = board.create(ANNA, "parent")
    sub = board.create(ANNA, "sub", parent_id=parent.id)
    finish_raw(board, sub.id, Status.DONE, "ok")
    return board.get(parent.id)


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_the_plain_ending_in_every_form(board, lang):
    parent = plain_parent(board)
    t = prompts.TEXTS[lang]
    assert board._ending(parent, "finished", "holder", lang) == t["ending_plain"]
    assert board._ending(parent, "finished", "creator", lang) == t["ending_plain_creator"].format(id=parent.id)
    assert board._ending(parent, "finished", "owner", lang) == t["owner_prefix_plain"] + t["ending_plain"]
    assert board._ending(parent, None, "owner", lang) == ""
    with pytest.raises(ValueError):
        board._ending(parent, "finished", "boss", lang)


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_the_crew_endings_in_every_form(board, lang):
    t = prompts.TEXTS[lang]
    failed, fs = crew_task(board, titles=("a", "b", "c"))
    finish_raw(board, fs[0].id, Status.DONE, "ok")
    finish_raw(board, fs[1].id, Status.FAILED, "no")
    finish_raw(board, fs[2].id, Status.FAILED, "no")
    kw = {"id": failed.id, "failed": fs[1].id}  # the first failed one
    assert board._ending(failed, "failed", "holder", lang) == t["ending_crew_failed"].format(**kw)
    assert board._ending(failed, "failed", "creator", lang) == t["ending_crew_failed_creator"].format(**kw)
    assert board._ending(failed, "failed", "owner", lang) == (t["owner_prefix_failed"]
                                                              + t["ending_crew_failed"].format(**kw))
    cancelled, cs = crew_task(board, titles=("a",))
    finish_raw(board, cs[0].id, Status.CANCELLED)
    kw = {"id": cancelled.id}
    assert board._ending(cancelled, "finished", "holder", lang) == t["ending_crew_cancelled"].format(**kw)
    assert board._ending(cancelled, "finished", "creator", lang) == t["ending_crew_cancelled_creator"].format(**kw)
    assert board._ending(cancelled, "finished", "owner", lang) == (t["owner_prefix_cancelled"]
                                                                   + t["ending_crew_cancelled"].format(**kw))
    done, ds = crew_task(board, titles=("a", "b"))
    finish_raw(board, ds[0].id, Status.DONE, "first result", assignee=BOB)
    finish_raw(board, ds[1].id, Status.CANCELLED)
    kw = {"id": done.id}
    draft = board._crew_draft(done, lang)
    assert draft and "first result" in draft
    assert board._ending(done, "finished", "holder", lang) == t["ending_crew_finished"].format(**kw) + draft
    assert board._ending(done, "finished", "owner", lang) == (t["owner_prefix_finished"]
                                                              + t["ending_crew_finished"].format(**kw) + draft)
    creator = board._ending(done, "finished", "creator", lang)
    assert creator == t["ending_crew_finished_creator"].format(**kw)  # no draft: the creator is asked to take it
    small = board._ending(done, "finished", "holder", lang, budget=150)
    assert len(small) <= len(t["ending_crew_finished"].format(**kw)) + 150


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_counts(board, lang):
    t = prompts.TEXTS[lang]
    assert board._counts_text({"done": 5, "cancelled": 1}, lang) == ", ".join(
        [t["count_done"].format(n=5), t["count_cancelled"].format(n=1)])
    assert board._counts_text({"failed": 2, "done": 1}, lang) == ", ".join(
        [t["count_done"].format(n=1), t["count_failed"].format(n=2)])
    if lang == "en":
        assert board._counts_text({"done": 5, "cancelled": 1}, lang) == "5 done, 1 cancelled"
    else:
        assert board._counts_text({"done": 5, "cancelled": 1}, lang) == "выполнено: 5, отменено: 1"


def test_the_open_list_of_the_errors(board):
    task, subs = crew_task(board, crew=4, titles=("a", "b", "c", "d"))
    work(board, subs[0].id, ANNA)
    work(board, subs[1].id, BOB)
    work(board, subs[2].id, CARL, Status.REVIEW)
    finish_raw(board, subs[3].id, Status.DONE, "ok", assignee=DINA)
    listed = board._open_list(board.subtasks_of(task.id), ANNA)
    assert listed == f"#{subs[0].id} [in_progress, you], #{subs[1].id} [in_progress, {BOB}], #{subs[2].id} [review]"


# --- the owner sentence ------------------------------------------------------------------------------------------------

def test_the_owner_sentence_take_one(board):
    task, subs = crew_task(board)
    work(board, subs[0].id, BOB)
    instructions, directions = board._owner_sentence(task, ANNA, "en")
    assert instructions == prompts.t("en", "crew_owner_take", id=task.id, crew=3)
    assert directions == (f" Its directions: #{subs[0].id} [in_progress] \"parser\" @{BOB}; "
                          f"#{subs[1].id} [todo] \"cli\"; #{subs[2].id} [todo] \"docs\".")
    assert "up to 3 members at once" in instructions


def test_the_owner_sentence_own_direction_with_its_status(board):
    task, subs = crew_task(board)
    work(board, subs[1].id, ANNA, Status.WAITING)
    instructions, _ = board._owner_sentence(task, ANNA, "en")
    assert instructions == prompts.t("en", "crew_owner_own", id=task.id, sub=subs[1].id, status="waiting", crew=3)
    assert f"your own direction is #{subs[1].id} [waiting]" in instructions


def test_a_cancelled_own_direction_is_not_its_own(board):
    task, subs = crew_task(board)
    finish_raw(board, subs[0].id, Status.CANCELLED, assignee=ANNA)
    instructions, directions = board._owner_sentence(task, ANNA, "en")
    assert instructions == prompts.t("en", "crew_owner_take", id=task.id, crew=3)
    assert f"#{subs[0].id}" not in directions  # the cancelled ones are not listed


def test_the_owner_sentence_all_taken(board):
    task, subs = crew_task(board, titles=("a", "b"))
    work(board, subs[0].id, BOB)
    finish_raw(board, subs[1].id, Status.DONE, "ok", assignee=CARL)
    instructions, _ = board._owner_sentence(task, ANNA, "en")
    assert instructions == prompts.t("en", "crew_owner_all_taken", id=task.id, crew=3)


def test_the_owner_sentence_without_directions(board):
    task = board.create(ANNA, "crew")
    task = set_raw(board, task.id, crew=2)
    assert board._owner_sentence(task, ANNA, "en") == (prompts.t("en", "crew_owner_none", id=task.id), "")


def test_the_owner_sentence_when_all_directions_are_finished_carries_the_draft(board):
    task, subs = crew_task(board, titles=("a", "b"))
    finish_raw(board, subs[0].id, Status.DONE, "alpha done", assignee=BOB)
    finish_raw(board, subs[1].id, Status.CANCELLED)
    instructions, directions = board._owner_sentence(task, ANNA, "en")
    assert instructions == " " + board._ending(task, "finished", "owner", "en")
    assert instructions.startswith(" All its directions are finished: ") and "alpha done" in instructions
    assert directions == ""  # the draft lists them
    rendered = prompts.t("en", "n_crew_owner", id=task.id, title=task.title, crew=3, instructions=instructions,
                         directions=directions)
    assert rendered.startswith(f"#{task.id} \"crew task\" is yours now (crew 3). All its directions are finished")


def test_the_owner_sentence_with_a_failed_direction_lists_them(board):
    task, subs = crew_task(board, titles=("a", "b"))
    finish_raw(board, subs[0].id, Status.FAILED, "no", assignee=BOB)
    finish_raw(board, subs[1].id, Status.DONE, "ok", assignee=CARL)
    instructions, directions = board._owner_sentence(task, ANNA, "en")
    assert instructions.startswith(" A direction failed: reopen the failed #")
    assert f"#{subs[0].id} [failed]" in directions and f"#{subs[1].id} [done]" in directions


def test_the_owner_sentence_with_all_directions_cancelled(board):
    task, subs = crew_task(board, titles=("a",))
    finish_raw(board, subs[0].id, Status.CANCELLED)
    instructions, directions = board._owner_sentence(task, ANNA, "en")
    assert instructions.startswith(" All its directions were cancelled: ")
    assert directions == ""


def test_the_owner_sentence_of_an_ordinary_parent(board):
    parent = plain_parent(board)
    assert board._owner_sentence(parent, ANNA, "en") == (" All its subtasks are finished: assemble their results "
                                                         "and complete it.", "")
    assert board._owner_sentence(board.create(ANNA, "solo"), ANNA, "en") == ("", "")


def test_the_owner_list_is_capped(board):
    titles = [f"d{i}" for i in range(OWNER_LIST_MAX + 3)]
    task, subs = crew_task(board, titles=titles)
    _, directions = board._owner_sentence(task, ANNA, "en")
    assert directions.count("[todo]") == OWNER_LIST_MAX
    assert directions.endswith(prompts.t("en", "crew_directions_more", k=3, id=task.id))
    assert f"#{subs[OWNER_LIST_MAX].id} " not in directions


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_the_owner_sentence_in_both_languages(board, lang):
    task, subs = crew_task(board, owner=EVA if lang == "ru" else ANNA)
    holder = EVA if lang == "ru" else ANNA
    instructions, directions = board._owner_sentence(task, holder, lang)
    text = prompts.t(lang, "n_crew_owner", id=task.id, title=task.title, crew=task.crew, instructions=instructions,
                     directions=directions)
    assert not re.search(r"{\w+}", text)
    if lang == "ru":
        assert "групповой задачи" in text and "«parser»" in directions
        assert "команд" not in text
    else:
        assert "You own crew task" in text and "\"parser\"" in directions


# --- writes: helpers -----------------------------------------------------------------------------------------------

def events(swarm, type_: str) -> list[dict]:
    return [e for e in swarm.bus.recent(limit=1000) if e["type"] == type_]


def created_ids(swarm) -> list[int]:
    return [e["data"]["task"]["id"] for e in events(swarm, "task.created")]


def quiet(swarm, name: str, key: str) -> list[bool]:
    """Whether each `key` notification of `name` was sent with wake=False, oldest first."""
    rows = swarm.db.all("SELECT data FROM notifications WHERE recipient = ? AND kind = ? ORDER BY id", (name, key))
    return [bool(loads(r["data"], {}).get(QUIET_KEY)) for r in rows]


def table(board: Board) -> list[tuple]:
    return [tuple(r) for r in board.db.all("SELECT * FROM tasks ORDER BY id")]


def owner_note(board: Board, task_id: int, holder: str, lang: str = "en") -> str:
    instructions, directions = board._owner_sentence(board.get(task_id), holder, lang)
    return instructions + directions


# --- Marking: create -----------------------------------------------------------------------------------------------

def test_create_with_crew_inserts_its_directions(swarm, board):
    dep = board.create(ANNA, "dep")
    task = board.create(BOB, "auth", crew=3, directions=[" oauth2 ", "pkce"], priority=2, tags=["backend"],
                        depends_on=[dep.id], reviewer=CARL)
    subs = board.subtasks_of(task.id)
    assert task.crew == 3 and task.status == Status.TODO and task.assignee is None
    assert [s.title for s in subs] == ["oauth2", "pkce"]
    for sub in subs:
        assert (sub.status, sub.goal, sub.description, sub.parent_id, sub.created_by) == (
            Status.TODO, "", "", task.id, BOB)
        assert (sub.priority, sub.tags, sub.assignee, sub.reviewer, sub.depends_on, sub.crew) == (
            2, ["backend"], None, None, [], 0)
    assert created_ids(swarm) == [dep.id, task.id, *(s.id for s in subs)]  # one event per task, id order
    assert swarm.notified == []


def test_the_owner_naming_itself_gets_its_own_part(swarm, board):
    notes: list[str] = []
    task = board.create(ANNA, "auth", crew=3, directions=["oauth2", "pkce"], assignee=ANNA, notes=notes)
    own, other = board.subtasks_of(task.id)
    assert (own.status, own.assignee) == (Status.IN_PROGRESS, ANNA)
    assert (other.status, other.assignee) == (Status.TODO, None)
    assert board.get(task.id).crew_working == [ANNA]  # the owner once
    assert notes == [prompts.t("en", "note_own_part", sub=own.id, title="oauth2", id=task.id)]
    assert notes[0].startswith(f" #{own.id} \"oauth2\" is your own part: write its card (task_update)")
    assert swarm.notified == []  # no n_direction_taken, no n_crew_owner to the actor


def test_the_own_part_of_a_blocked_crew_task_waits_in_todo(swarm, board):
    dep = board.create(ANNA, "dep")
    other = board.create(ANNA, "other")
    notes: list[str] = []
    task = board.create(EVA, "auth", crew=2, directions=["oauth2"], assignee=EVA, depends_on=[dep.id, other.id],
                        notes=notes)
    own = board.subtasks_of(task.id)[0]
    assert (own.status, own.assignee) == (Status.TODO, EVA)  # still its own
    assert notes == [prompts.t("ru", "note_own_part_blocked", sub=own.id, title="oauth2", id=task.id,
                               blockers=f"#{dep.id}, #{other.id}")]
    assert "«oauth2»" in notes[0]


def test_create_for_another_member_tells_it_once(swarm, board):
    task = board.create(ANNA, "auth", crew=3, directions=["oauth2", "pkce"], assignee=BOB)
    assert all(s.assignee is None for s in board.subtasks_of(task.id))  # the own part is the owner's to take
    assert keys_for(swarm, BOB) == ["n_crew_owner"]  # never also n_assigned
    (kw,) = notes_of(swarm, BOB, "n_crew_owner")
    assert kw["instructions"] == prompts.t("en", "crew_owner_take", id=task.id, crew=3)
    assert kw["crew"] == 3 and "\"oauth2\"" in kw["directions"]
    assert quiet(swarm, BOB, "n_crew_owner") == [False]  # it wakes
    board.create(ANNA, "plain", assignee=BOB)
    assert keys_for(swarm, BOB) == ["n_crew_owner", "n_assigned"]


def test_a_crew_task_assigned_without_directions_tells_the_owner_to_add_them(swarm, board):
    task = board.create(ANNA, "auth", crew=2, assignee=BOB)
    (kw,) = notes_of(swarm, BOB, "n_crew_owner")
    assert (kw["instructions"], kw["directions"]) == (prompts.t("en", "crew_owner_none", id=task.id), "")


def test_a_create_that_fails_writes_nothing(swarm, board):
    task, subs = crew_task(board)
    before = table(board)
    created = len(events(swarm, "task.created"))
    for kw in ({"directions": ["a", "b", "x" * 201]},
               {"directions": ["a", "b"], "parent_id": subs[0].id},     # crews are one level deep
               {"directions": ["a"], "parent_id": task.id, "depends_on": [99]}):
        with pytest.raises(ToolError):
            board.create(ANNA, "new", crew=2, assignee=ANNA, **kw)
    assert table(board) == before
    assert len(events(swarm, "task.created")) == created and swarm.notified == []


@pytest.mark.parametrize("crew, text", [
    (1, "Invalid crew 1: use 2..8 (how many members may work on it at once, its owner included), or 0 to make it an "
        "ordinary task again."),
    (9, "Invalid crew 9: use 2..8"),
    (-1, "Invalid crew -1: use 2..8"),
    ("x", "crew must be an integer, got 'x'."),
    (True, "crew must be an integer, got True."),
])
def test_invalid_crew(board, crew, text):
    with pytest.raises(ToolError) as err:
        board.create(ANNA, "t", crew=crew)
    assert str(err.value).startswith(text)
    plain = board.create(ANNA, "plain")
    with pytest.raises(ToolError) as err:
        board.update(ANNA, plain.id, crew=crew)
    assert str(err.value).startswith(text)


def test_crew_as_text_and_the_limits(board):
    assert board.create(ANNA, "two", crew="2").crew == 2
    assert board.create(ANNA, "eight", crew=CREW_MAX).crew == CREW_MAX
    assert board.create(ANNA, "zero", crew=0).crew == 0
    assert board.create(ANNA, "none", crew=None).crew == 0


LIST_ERROR = "directions must be a list of 1..20 direction titles, e.g. [\"parser\", \"cli\"]."


@pytest.mark.parametrize("directions, text", [
    ("parser", LIST_ERROR),
    ([], LIST_ERROR),
    ([f"d{i}" for i in range(21)], LIST_ERROR),
    (["a", 1], LIST_ERROR),
    ({"a": 1}, LIST_ERROR),
    ([" Parser ", "cli", "parser"], "Direction \"parser\" is listed twice."),
    (["a", "  "], "Task title must not be empty."),
    (["a", "x" * 201], "Task title is too long (201 chars, max 200)."),
])
def test_invalid_directions(board, directions, text):
    with pytest.raises(ToolError) as err:
        board.create(ANNA, "t", crew=2, directions=directions)
    assert str(err.value).startswith(text)
    task, _ = crew_task(board)
    with pytest.raises(ToolError) as err:
        board.update(ANNA, task.id, directions=directions)
    assert str(err.value).startswith(text)


def test_directions_need_a_crew_task(board):
    need = ("directions are for crew tasks: pass crew (how many members may work on it at once, its owner "
            "included).")
    with pytest.raises(ToolError) as err:
        board.create(ANNA, "t", directions=["a"])
    assert str(err.value) == need
    plain = board.create(ANNA, "plain")
    with pytest.raises(ToolError) as err:
        board.update(ANNA, plain.id, directions=["a"])
    assert str(err.value) == need
    task, subs = crew_task(board)
    board.update(ANNA, task.id, directions=["more"])  # a crew task takes them without crew in the call
    assert [s.title for s in board.subtasks_of(task.id)] == ["parser", "cli", "docs", "more"]
    assert board.subtasks_of(task.id)[-1].created_by == ANNA


def test_titles_are_not_compared_with_the_existing_directions(board):
    task, _ = crew_task(board)
    board.update(ANNA, task.id, directions=["parser"])
    assert [s.title for s in board.subtasks_of(task.id)].count("parser") == 2


# --- Marking: update -----------------------------------------------------------------------------------------------

def test_the_assignee_makes_its_task_a_crew_task(swarm, board):
    big = board.create(ANNA, "big")
    board.claim(BOB, big.id)
    swarm.notified.clear()
    notes: list[str] = []
    task = board.update(BOB, big.id, crew=3, directions=["x", "y"], notes=notes)
    own, other = board.subtasks_of(task.id)
    assert task.crew == 3 and (own.assignee, own.status) == (BOB, Status.IN_PROGRESS) and other.assignee is None
    assert notes == [prompts.t("en", "note_own_part", sub=own.id, title="x", id=task.id),
                     prompts.t("en", "note_crew_say_why", id=task.id),
                     owner_note(board, task.id, BOB)]
    assert f"your own direction is #{own.id} [in_progress]" in notes[2]
    assert keys_for(swarm, ANNA) == ["n_crew_changed"] and quiet(swarm, ANNA, "n_crew_changed") == [True]
    assert notes_of(swarm, ANNA, "n_crew_changed") == [{"id": task.id, "title": "big", "by": BOB, "crew": 3}]
    assert keys_for(swarm, BOB) == []


def test_the_crew_is_set_first_then_the_directions_are_added(swarm, board):
    big = board.create(ANNA, "big")
    board.update(ANNA, big.id, crew=2, directions=["x"])
    changes = [e["data"]["changes"] for e in events(swarm, "task.updated")]
    assert changes == [{"crew": [0, 2]}]
    # task.updated of the crew task, then task.created of each direction
    kinds = [(e["type"], e["data"]["task"]["id"]) for e in swarm.bus.recent(limit=1000)
             if e["type"] in ("task.updated", "task.created")]
    assert kinds[-2:] == [("task.updated", big.id), ("task.created", big.id + 1)]


def test_the_creator_marks_a_task_someone_holds(swarm, board):
    big = board.create(ANNA, "big")
    board.claim(BOB, big.id)
    swarm.notified.clear()
    notes: list[str] = []
    board.update(ANNA, big.id, crew=3, directions=["x", "y"], notes=notes)
    assert notes == []  # no own part: the creator is not the holder
    assert all(s.assignee is None for s in board.subtasks_of(big.id))
    assert keys_for(swarm, BOB) == ["n_crew_owner"] and quiet(swarm, BOB, "n_crew_owner") == [False]
    assert notes_of(swarm, BOB, "n_crew_owner")[0]["instructions"] == prompts.t("en", "crew_owner_take", id=big.id,
                                                                                 crew=3)
    board.update(ANNA, big.id, crew=4)  # a resize: quiet
    assert keys_for(swarm, BOB) == ["n_crew_owner", "n_crew_changed"]
    assert quiet(swarm, BOB, "n_crew_changed") == [True]
    assert keys_for(swarm, ANNA) == []


def test_user_marks_a_task_someone_holds(swarm, board):
    big = board.create(ANNA, "big")
    board.claim(BOB, big.id)
    swarm.notified.clear()
    board.update(USER, big.id, crew=2)
    assert keys_for(swarm, BOB) == ["n_crew_owner"] and keys_for(swarm, ANNA) == []
    board.update(USER, big.id, crew=0)
    assert keys_for(swarm, BOB) == ["n_crew_owner", "n_crew_changed"]
    assert notes_of(swarm, BOB, "n_crew_changed")[0]["crew"] == 0


def test_an_assignment_and_a_crew_change_in_one_call_is_one_notice(swarm, board):
    big = board.create(ANNA, "big")
    board.claim(BOB, big.id)
    swarm.notified.clear()
    board.update(ANNA, big.id, crew=3, assignee=CARL)
    assert keys_for(swarm, CARL) == ["n_crew_owner"]
    assert keys_for(swarm, BOB) == ["n_unassigned"]
    task, _ = crew_task(board, owner=BOB)
    swarm.notified.clear()
    board.update(ANNA, task.id, crew=4, assignee=CARL)
    assert keys_for(swarm, CARL) == ["n_crew_owner"]  # not also n_crew_changed


def test_the_owner_resizes_its_crew_task(swarm, board):
    task, _ = crew_task(board, owner=BOB)
    notes: list[str] = []
    board.update(BOB, task.id, crew=2, notes=notes)
    assert notes == [prompts.t("en", "note_crew_say_why", id=task.id)]  # it was the owner already
    assert keys_for(swarm, ANNA) == ["n_crew_changed"]
    notes.clear()
    board.update(BOB, task.id, crew=2, notes=notes)  # the same value: no change
    assert notes == [] and keys_for(swarm, ANNA) == ["n_crew_changed"]


def test_an_owner_that_created_the_task_tells_nobody(swarm, board):
    big = board.create(ANNA, "big")
    board.claim(ANNA, big.id)
    notes: list[str] = []
    board.update(ANNA, big.id, crew=2, notes=notes)
    assert notes == [owner_note(board, big.id, ANNA)]
    assert notes[0] == prompts.t("en", "crew_owner_none", id=big.id)
    assert swarm.notified == []


def test_crew_by_another_member_is_refused(board):
    big = board.create(ANNA, "big", assignee=BOB)
    with pytest.raises(ToolError) as err:
        board.update(CARL, big.id, crew=2)
    assert str(err.value) == (f"Only the creator ({ANNA}) or the assignee ({BOB}) of task #{big.id} can change its "
                              "crew. Suggest it in its discussion.")
    with pytest.raises(ToolError, match="Only the creator"):
        board.update(CARL, board.create(ANNA, "free").id, crew=2)
    task, _ = crew_task(board, owner=BOB)
    with pytest.raises(ToolError) as err:
        board.update(CARL, task.id, directions=["x"])
    assert str(err.value).startswith(f"Only the creator ({ANNA}) or the assignee ({BOB}) of task #{task.id} can "
                                     "change its directions.")


def test_more_directions_from_the_creator_the_assignee_and_user(board):
    task, _ = crew_task(board, owner=BOB)
    for actor in (ANNA, BOB, USER):
        board.update(actor, task.id, directions=[f"by {actor}"])
    subs = board.subtasks_of(task.id)
    assert [(s.title, s.created_by) for s in subs[3:]] == [(f"by {a}", a) for a in (ANNA, BOB, USER)]
    # BOB had none of its own: its first one became its own part, the others are free
    assert [(s.assignee, s.status) for s in subs[3:]] == [(None, Status.TODO), (BOB, Status.IN_PROGRESS),
                                                          (None, Status.TODO)]


def test_no_second_own_part_while_one_is_not_cancelled(board):
    task, subs = crew_task(board, owner=BOB)
    finish_raw(board, subs[0].id, Status.DONE, "ok", assignee=BOB)
    notes: list[str] = []
    board.update(BOB, task.id, directions=["x"], notes=notes)
    assert board.subtasks_of(task.id)[-1].assignee is None and notes == []
    finish_raw(board, subs[0].id, Status.CANCELLED, assignee=BOB)
    board.update(BOB, task.id, directions=["y", "z"], notes=notes)
    y, z = board.subtasks_of(task.id)[-2:]
    assert (y.assignee, y.status, z.assignee) == (BOB, Status.IN_PROGRESS, None)
    assert notes == [prompts.t("en", "note_own_part", sub=y.id, title="y", id=task.id)]


def test_user_holding_a_crew_task_gets_no_own_part(board):
    task, _ = crew_task(board, owner=USER)
    board.update(USER, task.id, directions=["x"])
    assert board.subtasks_of(task.id)[-1].assignee is None


# --- Marking: the order of the checks ------------------------------------------------------------------------------

def test_crew_zero_with_open_directions_lists_them(board):
    task, subs = crew_task(board, crew=4, titles=("a", "b", "c", "d"))
    work(board, subs[0].id, ANNA)
    work(board, subs[1].id, BOB)
    work(board, subs[2].id, CARL, Status.REVIEW)
    finish_raw(board, subs[3].id, Status.DONE, "ok", assignee=DINA)
    for status in (None, Status.CANCELLED):  # the crew check comes first and sees the task before the call
        with pytest.raises(ToolError) as err:
            board.update(ANNA, task.id, crew=0, status=status)
        assert str(err.value) == (
            f"#{task.id} has open directions: #{subs[0].id} [in_progress, you], #{subs[1].id} [in_progress, {BOB}], "
            f"#{subs[2].id} [review]: finish or cancel them first (task_update status=cancelled); their holders are "
            "told.")
    assert board.get(task.id).status == Status.IN_PROGRESS


def test_crew_zero_with_a_failed_direction(board):
    task, subs = crew_task(board, titles=("a", "oidc"))
    finish_raw(board, subs[0].id, Status.DONE, "ok")
    finish_raw(board, subs[1].id, Status.FAILED, "no")
    with pytest.raises(ToolError) as err:
        board.update(ANNA, task.id, crew=0)
    assert str(err.value) == (
        f"Crew task #{task.id} has failed directions: #{subs[1].id} \"oidc\". Reopen each to be done again "
        f"(task_update status=todo) or cancel it if #{task.id} does without it (status=cancelled) first.")
    board.update(ANNA, subs[1].id, status=Status.CANCELLED)
    assert board.update(ANNA, task.id, crew=0).crew == 0
    assert [s.parent_id for s in board.subtasks_of(task.id)] == [task.id, task.id]  # they stay its subtasks


def test_crew_zero_is_accepted_in_review_and_after(board):
    task, subs = crew_task(board, titles=("a",))
    finish_raw(board, subs[0].id, Status.DONE, "ok")
    set_raw(board, task.id, status=Status.REVIEW, reviewer=BOB)
    assert board.update(ANNA, task.id, crew=0).crew == 0
    with pytest.raises(ToolError) as err:
        board.update(ANNA, task.id, crew=2)
    assert str(err.value) == f"#{task.id} is review: it cannot become a crew task now."
    done = board.create(ANNA, "done")
    set_raw(board, done.id, status=Status.DONE)
    with pytest.raises(ToolError, match=f"#{done.id} is done: it cannot become a crew task now"):
        board.update(ANNA, done.id, crew=3)


def test_crew_and_directions_see_a_terminal_status_set_in_the_call(board):
    big = board.create(ANNA, "big")
    with pytest.raises(ToolError) as err:
        board.update(ANNA, big.id, crew=2, status=Status.CANCELLED)
    assert str(err.value) == f"#{big.id} is cancelled: it cannot become a crew task now."
    task, _ = crew_task(board)
    with pytest.raises(ToolError) as err:
        board.update(ANNA, task.id, directions=["x"], status=Status.CANCELLED)
    assert str(err.value) == f"#{task.id} is cancelled: it takes no new subtasks."
    set_raw(board, task.id, status=Status.REVIEW, reviewer=BOB)
    with pytest.raises(ToolError, match="is review: it takes no new subtasks"):
        board.update(ANNA, task.id, directions=["x"])
    assert len(board.subtasks_of(task.id)) == 3


def test_the_fields_come_before_the_crew_and_the_crew_before_the_status(board):
    big = board.create(ANNA, "big")
    with pytest.raises(ToolError, match="Task title must not be empty"):
        board.update(ANNA, big.id, title=" ", crew=1)
    with pytest.raises(ToolError, match="Invalid crew 1"):
        board.update(ANNA, big.id, crew=1, directions="x", status="nonsense")
    with pytest.raises(ToolError, match="directions must be a list"):
        board.update(ANNA, big.id, crew=2, directions="x", status="nonsense")
    with pytest.raises(ToolError, match="Unknown status"):
        board.update(ANNA, big.id, crew=2, directions=["x"], status="nonsense")
    assert board.get(big.id).crew == 0 and board.subtasks_of(big.id) == []


# --- Marking: one level deep, planning -----------------------------------------------------------------------------

def test_crew_on_a_direction_names_who_to_ask(board):
    task, subs = crew_task(board, owner=BOB)
    with pytest.raises(ToolError) as err:
        board.update(ANNA, subs[0].id, crew=2)
    n = task.id
    assert str(err.value) == (f"#{subs[0].id} is a direction of crew task #{n}, and crews are one level deep: ask its "
                              f"owner ({BOB}) in #{n}'s discussion to add directions (task_create parent_id=#{n}, or "
                              "task_update directions).")
    free, free_subs = crew_task(board, owner=None)
    with pytest.raises(ToolError, match=f"ask its owner \\(its creator {ANNA}\\)"):
        board.update(ANNA, free_subs[0].id, crew=2)
    with pytest.raises(ToolError) as err:
        board.create(ANNA, "nested", crew=2, parent_id=task.id)
    assert str(err.value).startswith(f"#{n} is a crew task, and crews are one level deep: a task under it is one "
                                     f"of its directions and cannot be a crew task itself. Pass no crew, or ask its "
                                     f"owner ({BOB})")


def test_crews_are_one_level_deep_both_ways(board):
    task, subs = crew_task(board)
    below = board.create(ANNA, "below a direction", parent_id=subs[0].id)
    with pytest.raises(ToolError) as err:
        board.update(ANNA, below.id, crew=2)
    assert str(err.value) == (f"#{below.id} is below crew task #{task.id}, and crews are one level deep: it cannot "
                              "be a crew task itself.")
    with pytest.raises(ToolError, match=f"The new task is below crew task #{task.id}"):
        board.create(ANNA, "deeper", crew=2, parent_id=below.id)
    top = board.create(ANNA, "top")
    board.update(ANNA, task.id, parent_id=top.id)  # a crew task may have an ordinary parent
    with pytest.raises(ToolError) as err:
        board.update(ANNA, top.id, crew=2)
    assert str(err.value) == (f"#{top.id} has a crew task below it, and crews are one level deep: it cannot be a "
                              "crew task itself.")


def test_a_move_never_nests_crews(board):
    task, subs = crew_task(board)
    other, _ = crew_task(board, titles=("x",))
    with pytest.raises(ToolError) as err:
        board.update(ANNA, other.id, parent_id=subs[0].id)
    assert str(err.value) == (f"#{other.id} is a crew task, and crews are one level deep: it cannot go below crew "
                              f"task #{task.id}.")
    holder = board.create(ANNA, "holder")
    board.update(ANNA, other.id, parent_id=holder.id)
    with pytest.raises(ToolError) as err:
        board.update(ANNA, holder.id, parent_id=task.id)
    assert str(err.value) == (f"#{holder.id} has a crew task below it, and crews are one level deep: it cannot go "
                              f"below crew task #{task.id}.")
    assert board.get(holder.id).parent_id is None  # a refused move leaves it where it was
    # crew and parent_id in one call see each other
    plain = board.create(ANNA, "plain")
    with pytest.raises(ToolError, match="crews are one level deep"):
        board.update(ANNA, plain.id, parent_id=task.id, crew=2)
    assert board.get(plain.id).parent_id is None and board.get(plain.id).crew == 0
    board.update(ANNA, plain.id, parent_id=task.id)  # an ordinary task becomes a direction
    assert plain.id in [s.id for s in board.subtasks_of(task.id)]


def test_planning_stays_with_one_member(board):
    with pytest.raises(ToolError) as err:
        board.create(ANNA, "plan", crew=2, tags=["Planning"])
    assert str(err.value) == ("The task is tagged planning, and planning stays with one member: it cannot be a crew "
                              "task.")
    plan = board.create(ANNA, "plan", tags=["planning"])
    with pytest.raises(ToolError) as err:
        board.update(ANNA, plan.id, crew=2)
    assert str(err.value) == (f"#{plan.id} is tagged planning, and planning stays with one member: it cannot be a "
                              "crew task.")
    task, _ = crew_task(board)
    with pytest.raises(ToolError) as err:
        board.update(ANNA, task.id, tags=["backend", "planning"])
    assert str(err.value) == (f"#{task.id} is a crew task, and planning stays with one member: it cannot be tagged "
                              "planning.")
    assert board.update(ANNA, task.id, tags=["backend"]).tags == ["backend"]


def test_crew_and_the_planning_tag_in_one_call_both_pass(board):
    """Each check sees the task as it was before the call (docs/SPEC.md §4.2 "Order"): a planning crew task."""
    big = board.create(ANNA, "big")
    task = board.update(ANNA, big.id, crew=2, tags=["planning"])
    assert task.crew == 2 and task.tags == ["planning"]


# --- Who controls subtasks -----------------------------------------------------------------------------------------

def test_only_the_parents_creator_or_assignee_adds_subtasks(board):
    parent = board.create(ANNA, "parent")
    with pytest.raises(ToolError) as err:
        board.create(BOB, "sub", parent_id=parent.id)
    assert str(err.value) == (f"Only the creator ({ANNA}) or the assignee (nobody) of task #{parent.id} can add "
                              "subtasks to it. Suggest it in its discussion.")
    loose = board.create(BOB, "loose")
    with pytest.raises(ToolError, match="can add subtasks to it"):
        board.update(BOB, loose.id, parent_id=parent.id)
    assert board.get(loose.id).parent_id is None
    board.claim(BOB, parent.id)
    assert board.create(BOB, "sub", parent_id=parent.id).parent_id == parent.id
    assert board.update(BOB, loose.id, parent_id=parent.id).parent_id == parent.id
    assert board.create(ANNA, "by creator", parent_id=parent.id).parent_id == parent.id
    assert board.create(USER, "by user", parent_id=parent.id).parent_id == parent.id
    assert board.update(BOB, loose.id, parent_id=None).parent_id is None  # leaving a parent needs no rule


@pytest.mark.parametrize("status", [Status.REVIEW, Status.DONE, Status.FAILED, Status.CANCELLED])
def test_no_new_subtask_under_a_parent_in_review_or_terminal(board, status):
    parent = board.create(ANNA, "parent")
    set_raw(board, parent.id, status=status)
    with pytest.raises(ToolError) as err:
        board.create(ANNA, "sub", parent_id=parent.id)
    assert str(err.value) == f"#{parent.id} is {status}: it takes no new subtasks."
    loose = board.create(ANNA, "loose")
    with pytest.raises(ToolError, match="it takes no new subtasks"):
        board.update(ANNA, loose.id, parent_id=parent.id)
    assert board.create(USER, "by user", parent_id=parent.id).parent_id == parent.id  # USER may do anything


def subtask_family(board: Board) -> tuple[Task, Task]:
    """A parent created by ANNA and held by CARL, with a subtask CARL created and assigned to BOB (in progress)."""
    parent = board.create(ANNA, "parent")
    board.claim(CARL, parent.id)
    sub = board.create(CARL, "sub", parent_id=parent.id, assignee=BOB)
    board.claim(BOB, sub.id)
    return parent, sub


def test_the_parents_creator_and_assignee_cancel_open_subtasks(swarm, board):
    for actor in (ANNA, CARL):
        _, sub = subtask_family(board)
        assert board.update(actor, sub.id, status=Status.CANCELLED).status == Status.CANCELLED
    _, sub = subtask_family(board)
    with pytest.raises(ToolError, match="can cancel task"):
        board.update(DINA, sub.id, status=Status.CANCELLED)
    set_raw(board, sub.id, status=Status.REVIEW, reviewer=DINA)
    assert board.update(ANNA, sub.id, status=Status.CANCELLED).status == Status.CANCELLED


def test_the_parents_creator_and_assignee_unassign_open_subtasks(swarm, board):
    for actor in (ANNA, CARL):
        _, sub = subtask_family(board)
        sub = board.update(actor, sub.id, assignee=None)
        assert (sub.assignee, sub.status) == (None, Status.TODO)
    _, sub = subtask_family(board)
    with pytest.raises(ToolError, match="only they or the creator"):
        board.update(DINA, sub.id, assignee=None)
    with pytest.raises(ToolError, match="only they or the creator"):
        board.update(ANNA, sub.id, assignee=DINA)  # unassigning only, not reassigning


@pytest.mark.parametrize("independent", [True, False])
def test_unassigning_a_subtask_in_review_is_refused(swarm, board, independent):
    swarm.config.swarm.independent_review = independent
    _, sub = subtask_family(board)
    set_raw(board, sub.id, status=Status.REVIEW, reviewer=DINA)
    with pytest.raises(ToolError, match="only they or the creator"):
        board.update(ANNA, sub.id, assignee=None)
    assert board.get(sub.id).assignee == BOB


def test_the_parent_reopens_a_failed_subtask_unassigned(swarm, board):
    parent, sub = subtask_family(board)
    board.update(BOB, sub.id, status=Status.FAILED)
    swarm.notified.clear()
    reopened = board.update(ANNA, sub.id, status=Status.TODO)
    assert (reopened.status, reopened.assignee) == (Status.TODO, None)
    assert keys_for(swarm, BOB) == []  # its old assignee gets nothing
    parent2, sub2 = subtask_family(board)
    board.update(BOB, sub2.id, status=Status.FAILED)
    assert board.update(CARL, sub2.id, status=Status.CANCELLED).status == Status.CANCELLED
    _, sub3 = subtask_family(board)
    board.update(BOB, sub3.id, status=Status.FAILED)
    with pytest.raises(ToolError, match="only its creator"):
        board.update(DINA, sub3.id, status=Status.TODO)
    with pytest.raises(ToolError, match="only its creator"):
        board.update(ANNA, sub3.id, status=Status.IN_PROGRESS)
    # the subtask's own creator reopening a failed subtask: unassigned too
    assert board.update(CARL, sub3.id, status=Status.TODO).assignee is None


def test_a_cancelled_subtask_is_reopened_only_by_its_creator(board):
    _, sub = subtask_family(board)
    board.update(CARL, sub.id, status=Status.CANCELLED)
    with pytest.raises(ToolError, match="only its creator"):
        board.update(ANNA, sub.id, status=Status.TODO)  # the parent's creator: failed ones only
    reopened = board.update(CARL, sub.id, status=Status.TODO)
    assert reopened.assignee == BOB  # a reopened task keeps its assignee


@pytest.mark.parametrize("status", [Status.REVIEW, Status.DONE, Status.CANCELLED])
def test_no_subtask_is_reopened_under_a_parent_in_review_or_terminal(board, status):
    parent, sub = subtask_family(board)
    other = board.create(CARL, "other", parent_id=parent.id)
    board.update(BOB, sub.id, status=Status.FAILED)
    board.update(CARL, other.id, status=Status.CANCELLED)
    set_raw(board, parent.id, status=status)
    for actor, tid, new in ((CARL, other.id, Status.TODO), (ANNA, sub.id, Status.TODO),
                            (CARL, sub.id, Status.CANCELLED)):
        with pytest.raises(ToolError) as err:
            board.update(actor, tid, status=new)
        assert str(err.value) == f"#{parent.id} is {status}: it takes no new subtasks; reopen #{parent.id} first."
    assert board.update(USER, other.id, status=Status.TODO).status == Status.TODO  # USER may do anything


def test_a_reopened_task_whose_assignee_left_is_ready(swarm, board):
    task = board.create(ANNA, "t", assignee=BOB)
    board.claim(BOB, task.id)
    board.update(BOB, task.id, status=Status.CANCELLED)
    swarm.db.execute("UPDATE agents SET status = ? WHERE name = ?", (AgentStatus.RETIRED, BOB))
    reopened = board.update(ANNA, task.id, status=Status.TODO)
    assert (reopened.status, reopened.assignee) == (Status.TODO, None)
    assert task.id in ready_ids(board)
    kept = board.create(ANNA, "kept", assignee=CARL)
    board.claim(CARL, kept.id)
    board.update(CARL, kept.id, status=Status.FAILED)
    swarm.notified.clear()
    assert board.update(ANNA, kept.id, status=Status.TODO).assignee == CARL  # not a subtask: it keeps it
    assert keys_for(swarm, CARL) == ["n_assigned"]


# --- The owner: the holder's notices and notes ---------------------------------------------------------------------

def test_self_assigning_a_crew_task_gives_the_owner_sentence(swarm, board):
    task, subs = crew_task(board, owner=None)
    notes: list[str] = []
    board.update(BOB, task.id, assignee=BOB, notes=notes)
    assert notes == [owner_note(board, task.id, BOB)]
    assert notes[0].startswith(prompts.t("en", "crew_owner_take", id=task.id, crew=3))
    assert swarm.notified == []


def test_the_creators_reopening_of_a_crew_task(swarm, board):
    task, subs = crew_task(board, owner=ANNA)
    finish_raw(board, task.id, Status.CANCELLED)
    notes: list[str] = []
    board.update(ANNA, task.id, status=Status.TODO, notes=notes)
    assert notes == [owner_note(board, task.id, ANNA)]
    held, _ = crew_task(board, owner=BOB)
    finish_raw(board, held.id, Status.CANCELLED)
    swarm.notified.clear()
    board.update(ANNA, held.id, status=Status.TODO)
    assert keys_for(swarm, BOB) == ["n_crew_owner"]  # never also n_assigned


def test_the_owner_notice_in_the_holders_language(swarm, board):
    task = board.create(ANNA, "auth", crew=2, directions=["a"], assignee=EVA)
    (kw,) = notes_of(swarm, EVA, "n_crew_owner")
    assert kw["instructions"] == prompts.t("ru", "crew_owner_take", id=task.id, crew=2)
    assert "«a»" in kw["directions"]


def test_an_owner_with_all_directions_finished_gets_the_draft(swarm, board):
    task, subs = crew_task(board, owner=None, titles=("a",))
    finish_raw(board, subs[0].id, Status.DONE, "the alpha result", assignee=BOB)
    board.update(ANNA, task.id, assignee=CARL)
    (kw,) = notes_of(swarm, CARL, "n_crew_owner")
    assert kw["instructions"].startswith(" All its directions are finished: ") and kw["directions"] == ""
    assert "the alpha result" in kw["instructions"]


# --- the result notes of the other writes, the mutation ------------------------------------------------------------

def test_claim_complete_and_review_take_notes(board):
    notes: list[str] = []
    task = board.create(ANNA, "t")
    board.claim(BOB, task.id, notes=notes)
    board.complete(BOB, task.id, "done", notes=notes)
    board.review(board.get(task.id).reviewer, task.id, True, notes=notes)
    assert notes == [] and board.get(task.id).status == Status.DONE


def test_the_mutation_reads_the_finish_state_of_its_parents_before_and_after(board):
    task, subs = crew_task(board, titles=("a", "b"))
    finish_raw(board, subs[0].id, Status.DONE, "ok")
    other = board.create(ANNA, "other")

    def plan(old, batch):
        batch.parents.add(other.id)
        return {"status": Status.CANCELLED}

    mutation = board._mutate(ANNA, subs[1].id, plan)
    assert mutation.before[task.id][0] is None
    assert mutation.after[task.id] == ("finished", {Status.DONE: 1, Status.CANCELLED: 1})
    assert mutation.before[other.id] == (None, {}) and mutation.changes == {"status": [Status.TODO,
                                                                                         Status.CANCELLED]}


def test_the_mutation_writes_side_rows_and_emits_them_after_the_primary_row(swarm, board):
    task, subs = crew_task(board, titles=("a",), reviewer=CARL)
    swarm.bus.recent(limit=1000)

    def plan(old, batch):
        batch.writes.append((board.get(task.id), {"reviewer": None}))
        batch.writes.append((board.get(task.id), {"title": "crew task"}))  # no change: no write, no event
        batch.after.append(lambda: swarm.signal("after"))
        return {"assignee": BOB}

    board._mutate(ANNA, subs[0].id, plan)
    updated = [(e["data"]["task"]["id"], e["data"]["changes"]) for e in swarm.bus.recent(limit=1000)
               if e["type"] == "task.updated"]
    assert updated[-2:] == [(subs[0].id, {"assignee": [None, BOB]}), (task.id, {"reviewer": [CARL, None]})]
    assert board.get(task.id).reviewer is None and swarm.signals[-1] == "after"


def test_a_failing_plan_writes_nothing(swarm, board):
    task, _ = crew_task(board)
    before = table(board)

    def plan(old, batch):
        board._insert_task("x", ANNA, parent_id=task.id)
        batch.writes.append((board.get(task.id), {"title": "changed"}))
        raise ToolError("no")

    with pytest.raises(ToolError):
        board._mutate(ANNA, task.id, plan)
    assert table(board) == before
