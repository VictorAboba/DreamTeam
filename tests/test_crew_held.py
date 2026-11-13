"""Held directions of crew tasks (docs/SPEC.md §4.2 "A held direction", "Also assigned", "The next direction"): the
note in the result of the call that leaves a direction held back for its assignee, the quiet notice the assignee gets
in place of n_assigned, the other todo directions named at a take, and the direction to take named at a hand-in."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dreamteam import prompts
from dreamteam.board import Board
from dreamteam.config import Config
from dreamteam.db import loads
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentKind, Status, Task
from dreamteam.swarm import QUIET_KEY, Swarm
from dreamteam.tools import ToolContext, call_tool
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


def quiet(swarm, name: str, key: str) -> list[bool]:
    """Whether each `key` notification of `name` was sent with wake=False, oldest first."""
    rows = swarm.db.all("SELECT data FROM notifications WHERE recipient = ? AND kind = ? ORDER BY id", (name, key))
    return [bool(loads(r["data"], {}).get(QUIET_KEY)) for r in rows]


def texts(swarm, name: str, key: str) -> list[str]:
    """The texts of the `key` notifications `name` got (rendered in its language), oldest first."""
    rows = swarm.db.all("SELECT text FROM notifications WHERE recipient = ? AND kind = ? ORDER BY id", (name, key))
    return [r["text"] for r in rows]


def assign(board: Board, actor: str, sid: int, name: str | None) -> list[str]:
    """`actor` makes `name` the assignee of task `sid`; the notes of the call's result."""
    notes: list[str] = []
    board.update(actor, sid, assignee=name, notes=notes)
    return notes


# --- the texts, rendered as the board renders them -----------------------------------------------------------------

def busy(sub: int, n: int, name: str, m: int, working: int, crew: int, lang: str = "en") -> str:
    return prompts.t(lang, "note_direction_held_busy", sub=sub, id=n, name=name, m=m, working=working, crew=crew)


def busy_you(sub: int, n: int, m: int, lang: str = "en") -> str:
    return prompts.t(lang, "note_direction_held_busy_you", sub=sub, id=n, m=m)


def full(sub: int, n: int, name: str, names: list[str], crew: int, lang: str = "en") -> str:
    return prompts.t(lang, "note_direction_held_full", sub=sub, id=n, name=name, working=len(names), crew=crew,
                     names=", ".join(names))


def full_you(sub: int, n: int, names: list[str], crew: int, lang: str = "en") -> str:
    return prompts.t(lang, "note_direction_held_full_you", sub=sub, id=n, working=len(names), crew=crew,
                     names=", ".join(names))


def tail(sub: int, n: int, name: str, lang: str = "en") -> str:
    return prompts.t(lang, "note_direction_held_tail", sub=sub, id=n, name=name)


def tail_you(sub: int, lang: str = "en") -> str:
    return prompts.t(lang, "note_direction_held_tail_you", sub=sub)


def also(sub: int, n: int, m: int, lang: str = "en") -> str:
    return prompts.t(lang, "note_direction_held_also", sub=sub, id=n, m=m)


def to_take(sub: int, title: str, n: int, lang: str = "en") -> str:
    return prompts.t(lang, "mark_direction_to_take", sub=sub, title=title, id=n)


def card(sub: int, n: int, owner_key: str, lang: str = "en", **kw: Any) -> str:
    return prompts.t(lang, "note_direction_card", sub=sub, id=n, owner=prompts.t(lang, owner_key, **kw))


# --- A held direction: the busy note to another actor ------------------------------------------------------------

def test_the_creator_handing_a_busy_member_a_direction_reads_the_busy_note_and_the_tail(swarm, board):
    task, (parser, cli, docs) = crew_task(board)
    work(board, parser.id, BOB)
    notes = assign(board, ANNA, cli.id, BOB)
    held = board.get(cli.id)
    assert (held.status, held.assignee) == (Status.TODO, BOB)  # the assignment takes no place
    assert notes == [busy(cli.id, task.id, BOB, parser.id, 2, 3) + tail(cli.id, task.id, BOB)]
    n, m, s = task.id, parser.id, cli.id
    assert notes[0] == (
        f" #{s} waits: Bob Brown works on #{m}, another direction of crew task #{n}, and can take #{s} only after "
        f"handing #{m} in; #{n} has 2 of its 3 places taken now. If another member can do #{s} now, unassign it "
        f"(task_update assignee='') and a free member is offered it; if only Bob Brown can, keep it: a free place "
        f"need not be filled. Add a direction only for a part of #{n} that no direction covers yet and that can be "
        "done now (task_update directions).")


@pytest.mark.parametrize("actor", [CARL, USER])
def test_the_holder_of_the_crew_task_and_user_read_the_tail_too(swarm, board, actor):
    swarm.language = "en"  # USER reads the swarm language
    task, (parser, cli, docs) = crew_task(board, owner=CARL)  # created by ANNA, held by CARL
    work(board, parser.id, BOB)
    assert assign(board, actor, cli.id, BOB) == [busy(cli.id, task.id, BOB, parser.id, 2, 3)
                                                 + tail(cli.id, task.id, BOB)]


def test_an_actor_that_neither_created_nor_holds_the_crew_task_reads_no_tail(swarm, board):
    task, (parser, cli, docs) = crew_task(board)
    work(board, parser.id, BOB)
    assert assign(board, DINA, cli.id, BOB) == [busy(cli.id, task.id, BOB, parser.id, 2, 3)]
    assert notes_of(swarm, BOB, "n_direction_held_busy") == [
        {"by": DINA, "id": cli.id, "title": "cli", "parent": task.id, "m": parser.id}]


NO_TAIL = ["no free place", "a blocker of its own", "the crew task blocked", "#M depends on it",
           "#M waits through another task", "#M waits through an open subtask"]


def held_without_tail(board: Board, case: str) -> tuple[Task, Task, Task]:
    """A crew task where BOB works on parser and cli, if it were unassigned, fails the tail's condition by `case`:
    (the crew task, parser, cli)."""
    dep = board.create(ANNA, "dep") if case == "the crew task blocked" else None
    task, (parser, cli, docs) = crew_task(board, crew=2 if case == "no free place" else 3,
                                          depends_on=[dep.id] if dep else None)
    if case == "a blocker of its own":
        board.update(ANNA, cli.id, depends_on=[board.create(ANNA, "dep").id])
    elif case == "#M depends on it":
        board.update(ANNA, parser.id, depends_on=[cli.id])
    elif case == "#M waits through another task":
        between = board.create(ANNA, "between", depends_on=[cli.id])
        board.update(ANNA, parser.id, depends_on=[between.id])
    elif case == "#M waits through an open subtask":
        board.create(ANNA, "part", parent_id=parser.id, depends_on=[cli.id])
    work(board, parser.id, BOB)
    return board.get(task.id), parser, cli


@pytest.mark.parametrize("case", NO_TAIL)
def test_the_busy_note_without_its_tail(board, case):
    """The tail needs a free place, the direction free if unassigned (no blocker of its own, the crew task not
    blocked), and #M not waiting for it (_wait_for_held: depends_on, transitively, and open subtasks)."""
    task, parser, cli = held_without_tail(board, case)
    assert assign(board, ANNA, cli.id, BOB) == [busy(cli.id, task.id, BOB, parser.id, 2, task.crew)]


@pytest.mark.parametrize("case", NO_TAIL)
def test_the_busy_you_note_without_its_tail(swarm, board, case):
    task, parser, cli = held_without_tail(board, case)
    swarm.notified.clear()
    assert assign(board, BOB, cli.id, BOB) == [busy_you(cli.id, task.id, parser.id)]
    assert swarm.notified == []  # the actor is never told


def test_the_lowest_direction_the_assignee_works_on_is_named(board):
    task, (parser, cli, docs) = crew_task(board)
    work(board, parser.id, BOB)
    work(board, cli.id, BOB)  # e.g. parser withdrawn from review after cli was taken
    assert assign(board, ANNA, docs.id, BOB) == [busy(docs.id, task.id, BOB, parser.id, 2, 3)
                                                 + tail(docs.id, task.id, BOB)]


@pytest.mark.parametrize("status", [Status.TODO, Status.WAITING])
def test_a_crew_task_todo_or_waiting_holds_its_directions_back_too(board, status):
    task, (parser, cli, docs) = crew_task(board, owner=None if status == Status.TODO else ANNA)
    if status == Status.WAITING:
        set_raw(board, task.id, status=status)
    work(board, parser.id, BOB)
    working = 1 if status == Status.TODO else 2  # a todo crew task without an owner: BOB alone
    assert assign(board, ANNA, cli.id, BOB) == [busy(cli.id, task.id, BOB, parser.id, working, 3)
                                                + tail(cli.id, task.id, BOB)]


def test_a_busy_member_of_a_full_crew_task_reads_the_busy_reason_not_the_full_one(board):
    """The two reasons never come together: a member that works on a direction is in the working set."""
    task, (parser, cli, docs) = crew_task(board, crew=2)
    work(board, parser.id, BOB)  # ANNA + BOB: full
    assert assign(board, ANNA, cli.id, BOB) == [busy(cli.id, task.id, BOB, parser.id, 2, 2)]


# --- A held direction: the actor is the assignee; the full reason -------------------------------------------------

def test_a_busy_member_assigning_itself_reads_the_you_form_and_its_tail(swarm, board):
    """BOB neither created nor holds the crew task: the tail of the you form depends on its condition only."""
    task, (parser, cli, docs) = crew_task(board)
    work(board, parser.id, BOB)
    swarm.notified.clear()
    notes = assign(board, BOB, cli.id, BOB)
    assert notes == [busy_you(cli.id, task.id, parser.id) + tail_you(cli.id)]
    n, m, s = task.id, parser.id, cli.id
    assert notes[0] == (
        f" #{s} waits for you: you work on #{m}, another direction of crew task #{n}, and can take #{s} only after "
        f"handing #{m} in (task_complete). If you take other work after that, unassign yourself from #{s} first "
        f"(task_update assignee=''), so it does not wait for you. If another member can do #{s} now, unassign "
        "yourself (task_update assignee='') and a free member is offered it.")
    assert swarm.notified == []


def full_crew(board: Board) -> tuple[Task, Task]:
    """A crew task of 3 whose working set is full, in its order: ANNA (the owner), DINA (parser), BOB (docs); cli is
    free. (the crew task, cli)"""
    task, (parser, cli, docs) = crew_task(board)
    work(board, parser.id, DINA)
    work(board, docs.id, BOB)
    task = board.get(task.id)
    assert task.crew_working == [ANNA, DINA, BOB]
    return task, cli


def test_the_full_note_names_the_working_set_in_its_order(swarm, board):
    task, cli = full_crew(board)
    notes = assign(board, ANNA, cli.id, CARL)
    assert notes == [full(cli.id, task.id, CARL, [ANNA, DINA, BOB], 3)]
    assert notes[0] == (f" #{cli.id} waits: all places in crew task #{task.id} are taken (3 of 3: Anna Smith, Dina "
                        f"Evans, Bob Brown), so Carl Clark can take #{cli.id} only when a place is free.")


def test_the_full_you_note(swarm, board):
    task, cli = full_crew(board)
    swarm.notified.clear()
    notes = assign(board, CARL, cli.id, CARL)
    assert notes == [full_you(cli.id, task.id, [ANNA, DINA, BOB], 3)]
    assert notes[0] == (
        f" #{cli.id} waits for you: all places in crew task #{task.id} are taken (3 of 3: Anna Smith, Dina Evans, Bob "
        f"Brown); you can take #{cli.id} only when a place is free. If you take other work meanwhile that will take "
        f"long, unassign yourself from #{cli.id} first (task_update assignee=''), so it does not wait for you.")
    assert swarm.notified == []


def test_the_owner_is_never_held_back_by_a_full_crew_task(swarm, board):
    task, (parser, cli, docs) = crew_task(board, crew=2)
    work(board, parser.id, BOB)  # full
    assert assign(board, ANNA, cli.id, ANNA) == []  # the owner is in the working set: it takes cli when it likes


# --- A held direction: the assignee's notice ----------------------------------------------------------------------

def test_the_busy_assignee_gets_the_quiet_busy_notice_in_place_of_n_assigned(swarm, board):
    task, (parser, cli, docs) = crew_task(board)
    work(board, parser.id, BOB)
    assign(board, ANNA, cli.id, BOB)
    assert keys_for(swarm, BOB) == ["n_direction_held_busy"]  # never also n_assigned
    assert notes_of(swarm, BOB, "n_direction_held_busy") == [
        {"by": ANNA, "id": cli.id, "title": "cli", "parent": task.id, "m": parser.id}]
    assert quiet(swarm, BOB, "n_direction_held_busy") == [True]
    n, m, s = task.id, parser.id, cli.id
    assert texts(swarm, BOB, "n_direction_held_busy") == [
        f'Task #{s} "cli", a direction of crew task #{n}, was assigned to you by Anna Smith. You work on #{m}, '
        f"another direction of #{n}: keep working on it; you can take #{s} only after handing #{m} in (task_complete), "
        f"and you are told when you can claim #{s} (task_claim). If you take other work after handing #{m} in, "
        f"unassign yourself from #{s} first (task_update assignee=''), so it does not wait for you."]


def test_the_assignee_outside_a_full_working_set_gets_the_quiet_full_notice(swarm, board):
    task, cli = full_crew(board)
    assign(board, ANNA, cli.id, CARL)
    assert keys_for(swarm, CARL) == ["n_direction_held_full"]
    assert notes_of(swarm, CARL, "n_direction_held_full") == [
        {"by": ANNA, "id": cli.id, "title": "cli", "parent": task.id, "working": 3, "crew": 3,
         "names": f"{ANNA}, {DINA}, {BOB}"}]
    assert quiet(swarm, CARL, "n_direction_held_full") == [True]
    n, s = task.id, cli.id
    assert texts(swarm, CARL, "n_direction_held_full") == [
        f'Task #{s} "cli", a direction of crew task #{n}, was assigned to you by Anna Smith. All places in #{n} are '
        f"taken now (3 of 3: Anna Smith, Dina Evans, Bob Brown): you can take #{s} only when a place is free, and you "
        f"are told when you can claim #{s} (task_claim). If you take other work meanwhile that will take long, "
        f"unassign yourself from #{s} first (task_update assignee=''), so it does not wait for you."]


@pytest.mark.parametrize("reason", ["busy", "full"])
def test_an_external_member_keeps_n_assigned(swarm, board, reason):
    """Nothing would tell an external member later: it keeps n_assigned, and the actor still reads the note."""
    task, (parser, cli, docs) = crew_task(board, crew=3 if reason == "busy" else 2)
    work(board, parser.id, EXT if reason == "busy" else BOB)
    notes = assign(board, ANNA, cli.id, EXT)
    assert keys_for(swarm, EXT) == ["n_assigned"] and quiet(swarm, EXT, "n_assigned") == [False]
    if reason == "busy":
        assert notes == [busy(cli.id, task.id, EXT, parser.id, 2, 3) + tail(cli.id, task.id, EXT)]
    else:
        assert notes == [full(cli.id, task.id, EXT, [ANNA, BOB], 2)]


def test_a_direction_that_is_not_held_back_keeps_n_assigned_and_no_note(swarm, board):
    task, (parser, cli, docs) = crew_task(board)
    work(board, parser.id, BOB)
    assert assign(board, ANNA, cli.id, CARL) == []  # a free place, and CARL works on no direction of it
    assert keys_for(swarm, CARL) == ["n_assigned"] and quiet(swarm, CARL, "n_assigned") == [False]


@pytest.mark.parametrize("status", [Status.REVIEW, Status.DONE, Status.CANCELLED])
def test_a_direction_of_a_crew_task_in_review_or_terminal_keeps_n_assigned_and_no_note(swarm, board, status):
    swarm.language = "en"
    task, (parser, cli, docs) = crew_task(board)
    work(board, parser.id, BOB)
    set_raw(board, task.id, status=status)
    assert assign(board, USER, cli.id, BOB) == []
    assert keys_for(swarm, BOB) == ["n_assigned"]


def test_user_as_the_assignee_is_never_held_back(swarm, board):
    task, (parser, cli, docs) = crew_task(board, crew=2)
    work(board, parser.id, BOB)  # full
    work(board, docs.id, USER)  # and USER works on another direction (it takes no place)
    swarm.notified.clear()
    assert assign(board, ANNA, cli.id, USER) == []
    assert swarm.notified == []


# --- A held direction: the calls that give a direction its assignee -----------------------------------------------

@pytest.mark.parametrize("actor", [ANNA, USER])
def test_task_create_under_the_crew_task_with_an_assignee(swarm, board, actor):
    swarm.language = "en"
    task, (parser, cli, docs) = crew_task(board)
    work(board, parser.id, BOB)
    notes: list[str] = []
    extra = board.create(actor, "extra", parent_id=task.id, assignee=BOB, notes=notes)
    assert notes == [busy(extra.id, task.id, BOB, parser.id, 2, 3) + tail(extra.id, task.id, BOB)]
    assert keys_for(swarm, BOB) == ["n_direction_held_busy"]
    assert notes_of(swarm, BOB, "n_direction_held_busy") == [
        {"by": actor, "id": extra.id, "title": "extra", "parent": task.id, "m": parser.id}]
    assert quiet(swarm, BOB, "n_direction_held_busy") == [True]
    # the full reason, on create too
    work(board, docs.id, DINA)  # ANNA, BOB, DINA: full
    notes.clear()
    more = board.create(actor, "more", parent_id=task.id, assignee=CARL, notes=notes)
    assert notes == [full(more.id, task.id, CARL, [ANNA, BOB, DINA], 3)]
    assert keys_for(swarm, CARL) == ["n_direction_held_full"]


def test_task_create_for_the_actor_itself_reads_the_you_form(swarm, board):
    task, (parser, cli, docs) = crew_task(board)
    work(board, parser.id, ANNA)  # the owner works on its own part
    notes: list[str] = []
    extra = board.create(ANNA, "extra", parent_id=task.id, assignee=ANNA, notes=notes)
    assert notes == [busy_you(extra.id, task.id, parser.id) + tail_you(extra.id)]
    assert swarm.notified == []


def test_task_create_of_a_task_that_is_not_held_back_has_no_note(swarm, board):
    task, (parser, cli, docs) = crew_task(board)
    plain = board.create(ANNA, "plain")
    notes: list[str] = []
    board.create(ANNA, "sub", parent_id=plain.id, assignee=BOB, notes=notes)  # under an ordinary parent
    board.create(ANNA, "extra", parent_id=task.id, assignee=BOB, notes=notes)  # BOB works on no direction
    board.create(ANNA, "loose", assignee=BOB, notes=notes)
    assert notes == [] and keys_for(swarm, BOB) == ["n_assigned"] * 3


def test_reassigning_a_direction_in_progress_resets_it_and_reads_the_state_after_the_call(swarm, board):
    task, (parser, cli, docs) = crew_task(board)
    work(board, parser.id, BOB)
    work(board, cli.id, CARL)  # ANNA, BOB, CARL: full before the call
    notes = assign(board, ANNA, cli.id, BOB)
    moved = board.get(cli.id)
    assert (moved.status, moved.assignee) == (Status.TODO, BOB)
    # after the call CARL's place is free: 2 of 3, and the tail applies
    assert notes == [busy(cli.id, task.id, BOB, parser.id, 2, 3) + tail(cli.id, task.id, BOB)]
    assert keys_for(swarm, CARL) == ["n_unassigned"]
    assert keys_for(swarm, BOB) == ["n_direction_held_busy"]


@pytest.mark.parametrize("actor", [ANNA, USER])
def test_a_reopening_that_keeps_the_assignee(swarm, board, actor):
    swarm.language = "en"
    task, (parser, cli, docs) = crew_task(board)
    work(board, parser.id, BOB)
    finish_raw(board, cli.id, Status.CANCELLED, assignee=BOB)
    notes: list[str] = []
    reopened = board.update(actor, cli.id, status=Status.TODO, notes=notes)
    assert (reopened.status, reopened.assignee) == (Status.TODO, BOB)
    assert notes == [busy(cli.id, task.id, BOB, parser.id, 2, 3) + tail(cli.id, task.id, BOB)]
    assert keys_for(swarm, BOB) == ["n_direction_held_busy"]
    assert notes_of(swarm, BOB, "n_direction_held_busy")[0]["by"] == actor


def test_a_failed_direction_reopened_unassigned_has_no_note(swarm, board):
    task, (parser, cli, docs) = crew_task(board)
    work(board, parser.id, BOB)
    finish_raw(board, cli.id, Status.FAILED, assignee=BOB)
    notes: list[str] = []
    assert board.update(ANNA, cli.id, status=Status.TODO, notes=notes).assignee is None
    assert notes == [] and keys_for(swarm, BOB) == []


def test_a_move_under_the_crew_task_reads_the_note_and_tells_the_assignee_nothing(swarm, board):
    task, (parser, cli, docs) = crew_task(board)
    loose = board.create(ANNA, "loose", assignee=BOB)
    work(board, parser.id, BOB)
    swarm.notified.clear()
    notes: list[str] = []
    moved = board.update(ANNA, loose.id, parent_id=task.id, notes=notes)
    assert (moved.parent_id, moved.status, moved.assignee) == (task.id, Status.TODO, BOB)
    assert notes == [busy(loose.id, task.id, BOB, parser.id, 2, 3) + tail(loose.id, task.id, BOB)]
    assert keys_for(swarm, BOB) == []  # as before: a move sends the assignee nothing


def test_passing_the_same_assignee_again_changes_nothing_and_notes_nothing(swarm, board):
    task, (parser, cli, docs) = crew_task(board)
    work(board, parser.id, BOB)
    assert assign(board, ANNA, cli.id, BOB)  # held: the first time it is said
    swarm.notified.clear()
    assert assign(board, ANNA, cli.id, BOB) == []
    notes: list[str] = []
    board.update(ANNA, cli.id, assignee=BOB, title="cli tool", notes=notes)  # another field changes: no assignment
    assert notes == [] and swarm.notified == []


# --- A held direction: USER and the languages ---------------------------------------------------------------------

@pytest.mark.parametrize("lang", ["en", "ru"])
@pytest.mark.parametrize("crew", [3, 2])
def test_user_reads_the_note_in_the_third_person_and_the_swarm_language(swarm, board, lang, crew):
    swarm.language = lang
    task, (parser, cli, docs) = crew_task(board, crew=crew)
    work(board, parser.id, BOB)
    notes = assign(board, USER, cli.id, BOB)
    expected = busy(cli.id, task.id, BOB, parser.id, 2, crew, lang)
    if crew == 3:  # a free place: the tail
        expected += tail(cli.id, task.id, BOB, lang)
    assert notes == [expected]
    assert notes_of(swarm, BOB, "n_direction_held_busy")[0]["by"] == USER


def test_the_note_is_in_the_actors_language_and_the_notice_in_the_assignees(swarm, board):
    task, (parser, cli, docs) = crew_task(board, owner=EVA)  # EVA holds it
    work(board, parser.id, BOB)
    assert assign(board, EVA, cli.id, BOB) == [busy(cli.id, task.id, BOB, parser.id, 2, 3, "ru")
                                               + tail(cli.id, task.id, BOB, "ru")]
    assert texts(swarm, BOB, "n_direction_held_busy") == [
        prompts.t("en", "n_direction_held_busy", by=EVA, id=cli.id, title="cli", parent=task.id, m=parser.id)]
    other, (o1, o2, o3) = crew_task(board, titles=("o1", "o2", "o3"))
    work(board, o1.id, EVA)
    assert assign(board, ANNA, o2.id, EVA) == [busy(o2.id, other.id, EVA, o1.id, 2, 3)
                                               + tail(o2.id, other.id, EVA)]
    assert texts(swarm, EVA, "n_direction_held_busy") == [
        prompts.t("ru", "n_direction_held_busy", by=ANNA, id=o2.id, title="o2", parent=other.id, m=o1.id)]
    assert "групповой задачи" in texts(swarm, EVA, "n_direction_held_busy")[0]


def test_a_direction_still_held_back_gets_n_unblocked_like_any_task(swarm, board):
    """What stays: n_unblocked is unchanged, and the refusal of the claim says why the direction still waits."""
    task, (parser, cli, docs) = crew_task(board)
    dep = board.create(ANNA, "dep")
    board.update(ANNA, cli.id, depends_on=[dep.id])
    work(board, parser.id, BOB)
    assign(board, ANNA, cli.id, BOB)
    board.update(USER, dep.id, status=Status.DONE)
    assert keys_for(swarm, BOB) == ["n_direction_held_busy", "n_unblocked"]
    assert board.directions_to_take(BOB) == []  # it still works on parser


def test_the_you_forms_in_russian(swarm, board):
    task, (parser, cli, docs) = crew_task(board, owner=EVA)
    work(board, parser.id, EVA)
    assert assign(board, EVA, cli.id, EVA) == [busy_you(cli.id, task.id, parser.id, "ru") + tail_you(cli.id, "ru")]
    task, cli = full_crew(board)
    assert assign(board, EVA, cli.id, EVA) == [full_you(cli.id, task.id, [ANNA, DINA, BOB], 3, "ru")]


# --- Also assigned -----------------------------------------------------------------------------------------------

def test_a_claim_names_the_actors_other_todo_directions_after_the_card_and_the_reviewer_sentence(swarm, board):
    swarm.config.swarm.independent_review = True
    task, (parser, cli, docs) = crew_task(board, reviewer=BOB)
    other, (elsewhere,) = crew_task(board, titles=("elsewhere",))
    for sub in (docs, parser, cli, elsewhere):
        assert assign(board, ANNA, sub.id, BOB) == []  # BOB works on no direction yet: nothing is held back
    notes: list[str] = []
    board.claim(BOB, parser.id, notes=notes)
    assert notes == [card(parser.id, task.id, "direction_owner_named", name=ANNA),
                     prompts.t("en", "note_reviewer_cleared", id=task.id),
                     also(cli.id, task.id, parser.id) + tail_you(cli.id),
                     also(docs.id, task.id, parser.id) + tail_you(docs.id)]   # id ascending; not elsewhere
    n, m, s = task.id, parser.id, cli.id
    assert notes[2] == (
        f" #{s}, also assigned to you, waits: you can take it only after handing #{m} in (task_complete), since a "
        f"member works on one direction of crew task #{n} at a time. If you take other work after that, unassign "
        f"yourself from #{s} first (task_update assignee=''), so it does not wait for you. If another member can do "
        f"#{s} now, unassign yourself (task_update assignee='') and a free member is offered it.")


def test_a_take_that_fills_the_last_place_names_the_others_without_the_tail(board):
    task, (parser, cli, docs) = crew_task(board, crew=2)
    for sub in (parser, cli, docs):
        assign(board, ANNA, sub.id, BOB)
    notes: list[str] = []
    board.claim(BOB, parser.id, notes=notes)
    assert notes == [card(parser.id, task.id, "direction_owner_named", name=ANNA),
                     also(cli.id, task.id, parser.id), also(docs.id, task.id, parser.id)]


def test_each_also_note_has_the_tail_on_its_own_condition(board):
    task, (parser, cli, docs, tests) = crew_task(board, titles=("parser", "cli", "docs", "tests"))
    board.update(ANNA, cli.id, depends_on=[board.create(ANNA, "dep").id])  # a blocker of its own
    board.create(ANNA, "part", parent_id=parser.id, depends_on=[docs.id])  # parser waits for docs
    for sub in (parser, cli, docs, tests):
        assign(board, ANNA, sub.id, BOB)
    notes: list[str] = []
    board.claim(BOB, parser.id, notes=notes)
    assert notes == [card(parser.id, task.id, "direction_owner_named", name=ANNA),
                     also(cli.id, task.id, parser.id), also(docs.id, task.id, parser.id),
                     also(tests.id, task.id, parser.id) + tail_you(tests.id)]


@pytest.mark.parametrize("way", ["status", "assignee and status"])
def test_a_take_by_update_names_the_other_directions_too(board, way):
    task, (parser, cli, docs) = crew_task(board)
    for sub in ((parser, cli, docs) if way == "status" else (cli, docs)):
        assign(board, ANNA, sub.id, BOB)
    notes: list[str] = []
    if way == "status":
        board.update(BOB, parser.id, status=Status.IN_PROGRESS, notes=notes)
    else:
        board.update(BOB, parser.id, assignee=BOB, status=Status.IN_PROGRESS, notes=notes)
    assert notes == [card(parser.id, task.id, "direction_owner_named", name=ANNA),
                     also(cli.id, task.id, parser.id) + tail_you(cli.id),
                     also(docs.id, task.id, parser.id) + tail_you(docs.id)]


def test_a_move_that_takes_a_direction_for_the_actor_names_its_other_ones(board):
    task, (parser, cli, docs) = crew_task(board)
    assert assign(board, ANNA, cli.id, ANNA) == []  # it works on no direction: not held back
    mine = board.create(ANNA, "mine")
    board.claim(ANNA, mine.id)
    notes: list[str] = []
    board.update(ANNA, mine.id, parent_id=task.id, notes=notes)
    assert notes == [card(mine.id, task.id, "direction_owner_you", id=task.id),
                     also(cli.id, task.id, mine.id) + tail_you(cli.id)]


def test_the_also_note_in_the_actors_language(board):
    task, (parser, cli, docs) = crew_task(board)
    assign(board, ANNA, cli.id, EVA)
    notes: list[str] = []
    board.claim(EVA, parser.id, notes=notes)
    assert notes == [card(parser.id, task.id, "direction_owner_named", "ru", name=ANNA),
                     also(cli.id, task.id, parser.id, "ru") + tail_you(cli.id, "ru")]


@pytest.mark.parametrize("case", ["user", "resume from waiting", "withdrawal from review", "a take for another"])
def test_no_also_note(swarm, board, case):
    task, (parser, cli, docs) = crew_task(board)
    notes: list[str] = []
    if case == "user":
        set_raw(board, cli.id, assignee=USER)
        set_raw(board, docs.id, assignee=USER)
        board.claim(USER, parser.id, notes=notes)
        assert board.get(parser.id).assignee == USER
    elif case == "resume from waiting":
        work(board, parser.id, BOB, Status.WAITING)
        set_raw(board, cli.id, assignee=BOB)
        board.update(BOB, parser.id, status=Status.IN_PROGRESS, notes=notes)
    elif case == "withdrawal from review":
        work(board, parser.id, BOB, Status.REVIEW)
        set_raw(board, parser.id, reviewer=DINA)
        set_raw(board, cli.id, assignee=BOB)
        board.update(BOB, parser.id, status=Status.IN_PROGRESS, notes=notes)
    else:  # CARL hands the direction it works on to BOB: a take for BOB, not for the actor
        work(board, parser.id, CARL)
        set_raw(board, cli.id, assignee=BOB)
        board.update(CARL, parser.id, assignee=BOB, status=Status.IN_PROGRESS, notes=notes)
        assert board.get(parser.id).assignee == BOB
    assert board.get(parser.id).status == Status.IN_PROGRESS
    assert notes == []


# --- The next direction ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("member", [BOB, EVA])
def test_handing_a_direction_in_names_the_direction_to_take(swarm, board, member):
    lang = swarm.language_of(member)
    task, (parser, cli, docs) = crew_task(board)
    board.claim(member, parser.id)
    assign(board, ANNA, cli.id, member)  # held back: it works on parser
    assert board.directions_to_take(member) == []
    board.update(member, parser.id, goal="a parser")
    notes: list[str] = []
    assert board.complete(member, parser.id, "parser in src/parser.py", notes=notes).status == Status.REVIEW
    assert notes == [to_take(cli.id, "cli", task.id, lang)]
    if lang == "en":
        assert notes[0] == (f' #{cli.id} "cli", your direction of crew task #{task.id}, can be taken now: claim it '
                            "(task_claim) and do it, or unassign yourself if you will not (task_update assignee='').")


def test_only_the_lowest_direction_to_take_is_named_the_next_one_at_the_next_hand_in(board):
    task, (parser, cli, docs) = crew_task(board)
    board.claim(BOB, parser.id)
    assign(board, ANNA, cli.id, BOB)
    assign(board, ANNA, docs.id, BOB)
    board.update(BOB, parser.id, goal="a parser")
    notes: list[str] = []
    board.complete(BOB, parser.id, "parser", notes=notes)
    assert notes == [to_take(cli.id, "cli", task.id)]
    board.claim(BOB, cli.id)
    board.update(BOB, cli.id, goal="a cli")
    notes.clear()
    board.complete(BOB, cli.id, "cli", notes=notes)
    assert notes == [to_take(docs.id, "docs", task.id)]


def test_any_hand_in_names_the_direction_to_take(swarm, board):
    """Not only a direction's: an agent's task_complete of any task names one its claim would take now."""
    task, (parser, cli, docs) = crew_task(board)
    assign(board, ANNA, cli.id, BOB)
    plain = board.create(ANNA, "plain", assignee=BOB)
    board.claim(BOB, plain.id)
    notes: list[str] = []
    board.complete(BOB, plain.id, "done", notes=notes)
    assert notes == [to_take(cli.id, "cli", task.id)]


def test_a_direction_its_claim_would_refuse_is_skipped(board):
    task, (parser, cli, docs) = crew_task(board)
    board.claim(BOB, parser.id)
    board.update(ANNA, cli.id, depends_on=[board.create(ANNA, "dep").id])
    assign(board, ANNA, cli.id, BOB)
    assign(board, ANNA, docs.id, BOB)
    board.update(BOB, parser.id, goal="a parser")
    notes: list[str] = []
    board.complete(BOB, parser.id, "parser", notes=notes)
    assert notes == [to_take(docs.id, "docs", task.id)]


def test_no_direction_to_take_while_it_waits_for_the_one_handed_in(swarm, board):
    task, (parser, cli, docs) = crew_task(board)
    board.claim(BOB, parser.id)
    board.update(ANNA, cli.id, depends_on=[parser.id])
    assign(board, ANNA, cli.id, BOB)
    board.update(BOB, parser.id, goal="a parser")
    notes: list[str] = []
    board.complete(BOB, parser.id, "parser", notes=notes)
    assert notes == []  # parser is in review: cli is still blocked
    reviewer = board.get(parser.id).reviewer
    board.review(reviewer, parser.id, True)
    assert "n_unblocked" in keys_for(swarm, BOB)  # the approval tells it
    assert [t.id for t in board.directions_to_take(BOB)] == [cli.id]


def test_users_completion_names_no_direction(board):
    task, (parser, cli, docs, tests) = crew_task(board, titles=("parser", "cli", "docs", "tests"))
    work(board, parser.id, USER)
    set_raw(board, cli.id, assignee=USER)
    notes: list[str] = []
    board.complete(USER, parser.id, "parser", notes=notes)
    assert notes == []
    # nor when USER completes a member's direction that leaves the member one to take
    work(board, docs.id, BOB)
    set_raw(board, tests.id, assignee=BOB)
    board.complete(USER, docs.id, "docs", notes=notes)
    assert [t.id for t in board.directions_to_take(BOB)] == [tests.id]
    assert notes == []


@pytest.mark.parametrize("member", [ANNA, EVA])
def test_the_direction_to_take_after_a_crew_draft_starts_on_a_line_of_its_own(swarm, board, member):
    """The owner completes the last direction of its crew task (the draft, over several lines) while it holds a
    takeable direction of another crew task."""
    swarm.config.swarm.independent_review = False  # the completion finishes the direction at once
    lang = swarm.language_of(member)
    finished, (a1, a2) = crew_task(board, crew=2, titles=("a1", "a2"), owner=member)
    finish_raw(board, a1.id, Status.DONE, "built a1\nall of it", assignee=BOB)
    set_raw(board, a2.id, assignee=member, status=Status.IN_PROGRESS, goal="a2 works")
    other, (b1,) = crew_task(board, titles=("b1",), owner=CARL)
    set_raw(board, b1.id, assignee=member)  # todo, and its claim would take it
    notes: list[str] = []
    assert board.complete(member, a2.id, "a2 done", notes=notes).status == Status.DONE
    parent = board.get(finished.id)
    ending = board._ending(parent, "finished", "holder", lang)
    draft = " " + prompts.t(lang, "n_subtasks_done", id=parent.id, title=parent.title, total=2,
                            counts=board._counts_text({Status.DONE: 2}, lang), ending=ending)
    assert "\n" in draft
    assert notes == [draft, "\n" + to_take(b1.id, "b1", other.id, lang).lstrip()]


def test_the_direction_to_take_after_a_one_line_note_keeps_its_space(swarm, board):
    swarm.config.swarm.independent_review = False
    plain = board.create(ANNA, "plain")
    set_raw(board, plain.id, assignee=BOB, status=Status.IN_PROGRESS)
    sub = board.create(ANNA, "sub", parent_id=plain.id)
    work(board, sub.id, BOB)
    task, (parser, cli, docs) = crew_task(board)
    set_raw(board, cli.id, assignee=BOB)
    notes: list[str] = []
    board.complete(BOB, sub.id, "done", notes=notes)
    ending = board._ending(board.get(plain.id), "finished", "holder", "en")
    first = " " + prompts.t("en", "n_subtasks_done", id=plain.id, title="plain", total=1,
                            counts=board._counts_text({Status.DONE: 1}, "en"), ending=ending)
    assert "\n" not in first
    assert notes == [first, to_take(cli.id, "cli", task.id)]


# --- the tool layer ----------------------------------------------------------------------------------------------

def tool_config(tmp_path: Path) -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Held directions", "language": "en", "start_agents": 4, "max_agents": 8, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "max_steps_per_turn": 10},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    return Config.from_dict(tmp_path / "project", raw)


@pytest.fixture
async def live(tmp_path):
    sw = Swarm(config=tool_config(tmp_path), llm_factory=lambda profile, agent: ScriptedLLM(lambda *a: "ok", profile))
    await sw.start(run_agents=False)
    try:
        yield sw
    finally:
        await sw.stop()
        sw.close()


async def call(sw: Swarm, actor: str, tool: str, args: Any) -> str:
    text, is_error = await call_tool(ToolContext(sw, actor), tool, args)
    assert not is_error, text
    return text


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_task_update_returns_the_held_note_in_its_text(live):
    a, b, *_ = [m["name"] for m in live.members() if m["kind"] == AgentKind.LLM]
    await call(live, a, "task_create", {"title": "Auth", "goal": "log in", "assignee": "me", "crew": 3,
                                        "directions": ["parser", "cli", "docs"]})
    # #1 the crew task, #2 parser (a's own part, in progress), #3 cli, #4 docs
    await call(live, b, "task_claim", {"task_id": 4})
    text = await call(live, a, "task_update", {"task_id": 3, "assignee": b})
    note = busy(3, 1, b, 4, 2, 3) + tail(3, 1, b)
    assert text.endswith("\n" + note.lstrip())
    rows = live.db.all("SELECT text, data FROM notifications WHERE recipient = ? AND kind = ?",
                       (b, "n_direction_held_busy"))
    assert [r["text"] for r in rows] == [
        prompts.t("en", "n_direction_held_busy", by=a, id=3, title="cli", parent=1, m=4)]
    assert loads(rows[0]["data"], {}).get(QUIET_KEY)
    assert live.db.all("SELECT id FROM notifications WHERE recipient = ? AND kind = 'n_assigned'", (b,)) == []
