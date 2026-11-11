"""Crew tasks and subtasks on the board (docs/SPEC.md §4.2): the reads, the working set, offers, liveness marks and
the texts the board renders for agents."""

from __future__ import annotations

import re

import pytest

from dreamteam import prompts
from dreamteam.board import CREW_MAX, MARK_FREE_DIRECTIONS, MARK_SUBTASKS_DONE, OWNER_LIST_MAX, Board
from dreamteam.models import USER, AgentKind, Status, Task
from helpers.crew import finish_raw, make_crew_raw, set_raw

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
    set_raw(board, parent.id, status=Status.REVIEW)
    sub = board.create(ANNA, "sub", parent_id=parent.id)
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
