"""A free place in a crew task (docs/SPEC.md §4.2 "A free place"), the board's part: Board.crew_gaps, the crew
tasks whose owner may be told that a place is free while no direction can be taken now, with the held directions to
name; and Board.crew_gap_parts, the list of directions and the held sentences of the owner's notice (n_crew_gap).
The scheduler item that sends it (§3 item 8) is tested in tests/test_crew_swarm.py."""

from __future__ import annotations

import pytest

from dreamteam import prompts
from dreamteam.board import CREW_GAP_HELD_MAX, Board
from dreamteam.models import USER, AgentKind, AgentStatus, Status, Task
from helpers.crew import finish_raw, make_crew_raw, set_raw

# the people of the SPEC's examples: OLGA owns #8; PETE (the planner) created it
OLGA, ANNA, BOB, CARL, PETE = "Olga Owner", "Anna Smith", "Bob Brown", "Carl Diaz", "Pete Plan"
EXT = "Ext Agent"


@pytest.fixture
def swarm(fake_swarm):
    fake_swarm.board = Board(fake_swarm)
    for name, role in ((OLGA, "Generalist"), (ANNA, "Developer"), (BOB, "Generalist"), (CARL, "Tester"),
                       (PETE, "Planner")):
        fake_swarm.add_member(name, role=role, language="en")
    fake_swarm.add_member(EXT, kind=AgentKind.EXTERNAL, language="en")
    return fake_swarm


@pytest.fixture
def board(swarm) -> Board:
    return swarm.board


def engine(board: Board, crew: int = 3, owner: str | None = OLGA, status: str = Status.IN_PROGRESS) -> Task:
    """Crew task #8 "engine" (crew `crew`, created by PETE, held by `owner` in `status`; None: todo without an owner)
    with the directions of the SPEC's examples, #14 "search", #15 "eval" and #16 "uci", all todo and free; #1-#7
    and #9-#13 are other tasks of the run, done."""
    for _ in range(7):
        finish_raw(board, board.create(PETE, "earlier").id, Status.DONE, "r")
    task = board.create(PETE, "engine")
    for _ in range(5):
        finish_raw(board, board.create(PETE, "earlier").id, Status.DONE, "r")
    subs = [board.create(PETE, title, parent_id=task.id) for title in ("search", "eval", "uci")]
    assert (task.id, [s.id for s in subs]) == (8, [14, 15, 16])
    set_raw(board, task.id, crew=crew, **({"assignee": owner, "status": status} if owner else {}))
    return board.get(task.id)


def work(board: Board, sid: int, name: str, status: str = Status.IN_PROGRESS) -> Task:
    return set_raw(board, sid, assignee=name, status=status)


def done(board: Board, sid: int, name: str) -> Task:
    return finish_raw(board, sid, Status.DONE, "r", assignee=name)


def give(board: Board, sid: int, name: str) -> Task:
    """`name` becomes the assignee of todo task `sid` (it stays todo: an assignment takes no place)."""
    return set_raw(board, sid, assignee=name)


def gaps(board: Board) -> dict[int, tuple[list[int], list[tuple[int, str, int]]]]:
    """crew_gaps() as {crew task id: (the ids of its open directions, the held ones)}."""
    return {task.id: ([s.id for s in subs], held) for task, subs, held in board.crew_gaps()}


def notice(board: Board, lang: str, idle: str) -> str:
    """n_crew_gap of #8 in `lang`, with `idle` as the free members and the parts the board renders."""
    task, _, held = next(gap for gap in board.crew_gaps() if gap[0].id == 8)
    directions, held_text = board.crew_gap_parts(task, held, lang)
    return prompts.t(lang, "n_crew_gap", id=task.id, title=task.title, working=len(task.crew_working),
                     crew=task.crew, names=", ".join(task.crew_working), idle=idle, directions=directions,
                     held=held_text)


def held_text(board: Board, task_id: int = 8, lang: str = "en") -> str:
    task, _, held = next(gap for gap in board.crew_gaps() if gap[0].id == task_id)
    return board.crew_gap_parts(task, held, lang)[1]


def held(lang: str, sub: int, name: str, m: int) -> str:
    return prompts.t(lang, "crew_gap_held", sub=sub, name=name, m=m)


# --- the run of 2026-11-13: both states are listed ------------------------------------------------------------------

def test_the_runs_second_state_is_listed_and_reads_as_the_spec_says(board):
    """One member works on the last direction, the owner's own is handed in, a place is free: nothing names a
    held direction, and the notice reads word for word as docs/SPEC.md §4.2 "A free place" gives it."""
    engine(board)
    done(board, 14, OLGA)
    done(board, 15, BOB)
    work(board, 16, ANNA)
    pete_report = board.create(PETE, "report", depends_on=[8])
    give(board, pete_report.id, PETE)  # the planner holds a task blocked by #8: it changes nothing here
    assert gaps(board) == {8: ([16], [])}
    assert notice(board, "en", "Carl Diaz (Tester), Bob Brown (Generalist)") == (
        '#8 "engine", your crew task, has a free place (2 of its 3 places taken: Olga Owner, Anna Smith), but none of '
        "its directions can be taken now, and these members are idle, with no task of their own to go on with: Carl "
        'Diaz (Tester), Bob Brown (Generalist). Its directions: #14 [done] "search" @Olga Owner; #15 [done] "eval" '
        '@Bob Brown; #16 [in_progress] "uci" @Anna Smith. If a part of #8 that no direction covers yet can be done '
        "now, add it as a direction (task_update directions). If none of this applies, leave #8 as it is: a free "
        "place need not be filled, and no reply is needed.")
    assert notice(board, "ru", "Carl Diaz (Тестировщик), Bob Brown (Универсал)") == (
        "#8 «engine», твоя групповая задача: в ней есть свободное место (занято 2 из 3: Olga Owner, Anna Smith), но "
        "ни одно её направление сейчас нельзя взять, а эти участники простаивают и ни одну свою задачу сейчас вести "
        "не могут: Carl Diaz (Тестировщик), Bob Brown (Универсал). Её направления: #14 [done] «search» @Olga Owner; "
        "#15 [done] «eval» @Bob Brown; #16 [in_progress] «uci» @Anna Smith. Если в #8 есть часть, которую не "
        "покрывает ни одно направление и которую можно делать уже сейчас, добавь её как направление (task_update "
        "directions). Если ничего из этого не подходит, оставь #8 как есть: свободное место не обязательно "
        "занимать, и отвечать не нужно.")


def test_the_runs_first_state_names_the_direction_held_for_a_busy_member(board):
    """#16 was assigned to BOB while BOB works on #15: it is held back, and the notice names it."""
    engine(board)
    work(board, 14, OLGA)
    work(board, 15, BOB)
    give(board, 16, BOB)
    assert gaps(board) == {8: ([14, 15, 16], [(16, BOB, 15)])}
    assert held_text(board) == held("en", 16, BOB, 15) == (
        " #16 waits: Bob Brown works on #15 and can take #16 only after handing #15 in. If another member can do #16 "
        "now, unassign it (task_update assignee='') and a free member is offered it.")
    assert notice(board, "ru", "Carl Diaz (Тестировщик), Anna Smith (Разработчик)") == (
        "#8 «engine», твоя групповая задача: в ней есть свободное место (занято 2 из 3: Olga Owner, Bob Brown), но ни "
        "одно её направление сейчас нельзя взять, а эти участники простаивают и ни одну свою задачу сейчас вести не "
        "могут: Carl Diaz (Тестировщик), Anna Smith (Разработчик). Её направления: #14 [in_progress] «search» @Olga "
        "Owner; #15 [in_progress] «eval» @Bob Brown; #16 [todo] «uci» @Bob Brown. #16 пока ждёт: Bob Brown ведёт #15 "
        "и сможет взять #16 только после сдачи #15. Если #16 уже сейчас может сделать и другой участник, сними с #16 "
        "исполнителя (task_update assignee=''), и #16 предложат свободному участнику. Если в #8 есть часть, которую "
        "не покрывает ни одно направление и которую можно делать уже сейчас, добавь её как направление (task_update "
        "directions). Если ничего из этого не подходит, оставь #8 как есть: свободное место не обязательно "
        "занимать, и отвечать не нужно.")


# --- listed: held directions -----------------------------------------------------------------------------------------

def test_a_direction_whose_assignee_works_on_another_task_is_named_with_that_task(board):
    engine(board)
    work(board, 14, OLGA)
    work(board, 15, ANNA)
    other = work(board, board.create(PETE, "perft").id, BOB)
    give(board, 16, BOB)  # not held back: BOB works on no direction of #8 and a place is free
    assert gaps(board) == {8: ([14, 15, 16], [(16, BOB, other.id)])}
    assert held_text(board) == held("en", 16, BOB, other.id)
    # the other task set waiting: BOB may take #16 now (its "direction to take" mark tells it), so #8 is left out
    set_raw(board, other.id, status=Status.WAITING)
    assert gaps(board) == {}


def test_the_lowest_other_task_the_assignee_works_on_is_named(board):
    """A direction of another crew task counts as a task it works on; a parent with an open subtask does not."""
    engine(board)
    work(board, 14, OLGA)
    work(board, 15, ANNA)
    parent = work(board, board.create(PETE, "parent").id, BOB)
    work(board, board.create(PETE, "part", parent_id=parent.id).id, CARL)
    other = board.create(PETE, "ui", crew=2, directions=["screens"])
    screens = board.subtasks_of(other.id)[0]
    work(board, other.id, CARL)
    work(board, screens.id, BOB)
    give(board, 16, BOB)
    assert gaps(board)[8] == ([14, 15, 16], [(16, BOB, screens.id)])


def test_the_owner_of_another_crew_task_working_on_none_of_its_directions_works_on_no_other_task(board):
    """System prompt rule 3: a crew task it owns while it works on none of its directions waits for them. So BOB's
    #16 is a direction to take, which keeps #8 out."""
    engine(board)
    work(board, 14, OLGA)
    work(board, 15, ANNA)
    other = board.create(PETE, "ui", crew=2, directions=["screens"])
    work(board, other.id, BOB)
    work(board, board.subtasks_of(other.id)[0].id, CARL)  # BOB's crew task is full: not listed itself
    give(board, 16, BOB)
    assert [d.id for d in board.directions_to_take(BOB)] == [16]
    assert gaps(board) == {}


def test_the_owners_own_held_direction_is_named(board):
    engine(board)
    work(board, 14, OLGA)
    done(board, 15, ANNA)
    give(board, 16, OLGA)
    assert gaps(board) == {8: ([14, 16], [(16, OLGA, 14)])}
    you = prompts.t("en", "crew_gap_held_you", sub=16, m=14)
    assert held_text(board) == you
    # the same form when #14 waits for #16: the owner sees its own dependency
    set_raw(board, 14, depends_on="[16]")
    assert held_text(board) == you


def test_the_owners_own_direction_it_may_take_keeps_the_crew_task_out(board):
    engine(board)
    done(board, 14, OLGA)
    work(board, 15, ANNA)
    give(board, 16, OLGA)
    assert gaps(board) == {}


def test_a_working_direction_that_waits_for_a_held_one_is_named_with_the_wait(board):
    """The deadlock: #15 waits for #16, held for BOB, who works on #15; #8 waiting. Liveness is not asked."""
    engine(board, status=Status.WAITING)
    board.update(PETE, 15, depends_on=[16])
    done(board, 14, OLGA)
    work(board, 15, BOB)
    give(board, 16, BOB)
    assert gaps(board) == {8: ([15, 16], [(16, BOB, 15)])}
    # #15 waits for #16, but only a todo direction is followed by its blockers in the list
    assert board.crew_gap_parts(board.get(8), [], "en")[0] == (
        ' Its directions: #14 [done] "search" @Olga Owner; #15 [in_progress] "eval" @Bob Brown; #16 [todo] "uci" '
        "@Bob Brown.")
    assert held_text(board) == prompts.t("en", "crew_gap_held_wait", sub=16, name=BOB, m=15) == (
        " #16 waits: Bob Brown works on #15, which waits for #16, so neither moves while Bob Brown holds #16: unassign "
        "#16 (task_update assignee='') and a free member is offered it.")
    assert held_text(board, lang="ru") == (
        " #16 пока ждёт: Bob Brown ведёт #15, а #15 ждёт #16, поэтому ни одно из них не сдвинется, пока #16 за Bob "
        "Brown: сними с #16 исполнителя (task_update assignee=''), и #16 предложат свободному участнику.")


def test_another_task_that_waits_for_the_held_direction_gives_the_wait_form(board):
    engine(board)
    work(board, 14, OLGA)
    work(board, 15, ANNA)
    other = work(board, board.create(PETE, "perft", depends_on=[16]).id, BOB)
    give(board, 16, BOB)
    assert gaps(board)[8] == ([14, 15, 16], [(16, BOB, other.id)])
    assert held_text(board) == prompts.t("en", "crew_gap_held_wait", sub=16, name=BOB, m=other.id)


def test_a_held_back_direction_of_an_external_member_is_named(board):
    engine(board)
    work(board, 14, OLGA)
    work(board, 15, EXT)
    give(board, 16, EXT)
    assert gaps(board) == {8: ([14, 15, 16], [(16, EXT, 15)])}


# --- listed: directions that neither keep the crew task out nor are named -------------------------------------------

@pytest.mark.parametrize("assignee", [USER, EXT, "error"])
def test_a_todo_direction_of_user_an_external_member_or_a_member_in_error_is_not_named(swarm, board, assignee):
    engine(board)
    work(board, 14, OLGA)
    work(board, 15, ANNA)
    if assignee == "error":
        swarm.db.execute("UPDATE agents SET status = ? WHERE name = ?", (AgentStatus.ERROR, BOB))
        assignee = BOB
    give(board, 16, assignee)
    assert gaps(board) == {8: ([14, 15, 16], [])}
    task = board.get(8)
    assert board.crew_gap_parts(task, [], "en") == (
        f' Its directions: #14 [in_progress] "search" @Olga Owner; #15 [in_progress] "eval" @Anna Smith; #16 [todo] '
        f'"uci" @{assignee}.', "")


def test_a_sequence_shows_the_blockers_in_the_list(board):
    """#16 waits for #15 by its own depends_on: the list says so, and nothing is named."""
    engine(board)
    board.update(PETE, 16, depends_on=[15])
    done(board, 14, OLGA)
    work(board, 15, BOB)
    assert gaps(board) == {8: ([15, 16], [])}
    task = board.get(8)
    assert board.crew_gap_parts(task, [], "en") == (
        ' Its directions: #14 [done] "search" @Olga Owner; #15 [in_progress] "eval" @Bob Brown; #16 [todo] "uci" '
        "(blocked by #15).", "")
    give(board, 16, ANNA)  # assigned and blocked: still not named; the blockers follow its assignee
    assert gaps(board) == {8: ([15, 16], [])}
    assert board.crew_gap_parts(task, [], "ru")[0] == (
        " Её направления: #14 [done] «search» @Olga Owner; #15 [in_progress] «eval» @Bob Brown; #16 [todo] «uci» "
        "@Anna Smith (ждёт #15).")


def test_a_direction_in_review_neither_works_nor_keeps_the_crew_task_out(board):
    engine(board)
    done(board, 14, OLGA)
    set_raw(board, 15, assignee=BOB, status=Status.REVIEW, reviewer=CARL)
    work(board, 16, ANNA)
    assert gaps(board) == {8: ([15, 16], [])}


def test_crew_tasks_are_listed_id_ascending_with_a_todo_or_waiting_crew_task_too(board):
    engine(board, status=Status.TODO)
    done(board, 14, OLGA)
    work(board, 15, ANNA)
    done(board, 16, CARL)
    other = board.create(PETE, "ui", crew=3, directions=["screens", "theme"], priority=3)
    screens, theme = board.subtasks_of(other.id)
    work(board, other.id, BOB, status=Status.WAITING)
    work(board, screens.id, CARL)
    done(board, theme.id, BOB)
    assert list(gaps(board)) == [8, other.id]


# --- left out -------------------------------------------------------------------------------------------------------

def test_a_free_direction_just_added_keeps_the_crew_task_out(board):
    engine(board)
    done(board, 14, OLGA)
    done(board, 15, BOB)
    work(board, 16, ANNA)
    assert list(gaps(board)) == [8]
    board.update(OLGA, 8, directions=["cache"])
    cache = board.subtasks_of(8)[-1]
    assert (cache.status, cache.assignee) == (Status.TODO, None)  # OLGA has a direction of her own: it is free
    assert gaps(board) == {}


def test_a_direction_to_take_keeps_the_crew_task_out_until_its_assignee_is_in_error(swarm, board):
    engine(board)
    work(board, 14, OLGA)
    work(board, 15, ANNA)
    give(board, 16, BOB)  # BOB is idle: its claim would take #16
    assert gaps(board) == {}
    swarm.db.execute("UPDATE agents SET status = ? WHERE name = ?", (AgentStatus.ERROR, BOB))
    assert gaps(board) == {8: ([14, 15, 16], [])}


def nothing_to_take(board: Board) -> None:
    """The state the next tests change one thing of: #14 and #16 finished, ANNA works on #15, OLGA owns #8 (crew 3,
    in_progress, not blocked) and works on none of its directions. Nothing is free to take, so #8 is listed, and each
    test fails if the rule it names is removed."""
    done(board, 14, OLGA)
    work(board, 15, ANNA)
    done(board, 16, CARL)
    task = board.get(8)
    assert (task.assignee, task.crew, task.status, task.blocked_by) == (OLGA, 3, Status.IN_PROGRESS, [])
    assert gaps(board) == {8: ([15], [])}


def test_a_crew_of_two_with_an_idle_owner_and_one_worker_is_full(board):
    engine(board)
    nothing_to_take(board)
    set_raw(board, 8, crew=2)
    assert board.get(8).crew_working == [OLGA, ANNA]
    assert gaps(board) == {}


@pytest.mark.parametrize("owner", [USER, None])
def test_a_crew_task_held_by_user_or_without_an_owner_is_left_out(board, owner):
    engine(board)
    nothing_to_take(board)
    set_raw(board, 8, assignee=owner, status=Status.IN_PROGRESS if owner else Status.TODO)
    assert len(board.get(8).crew_working) < 3  # a place is still free (USER takes none)
    assert gaps(board) == {}


@pytest.mark.parametrize("case", ["no directions", "all finished", "only in review", "only todo"])
def test_a_crew_task_with_nothing_working_is_left_out(board, case):
    engine(board)
    if case == "no directions":
        for sid in (14, 15, 16):
            set_raw(board, sid, parent_id=None)
    elif case == "all finished":
        for sid, name in ((14, OLGA), (15, BOB), (16, ANNA)):
            done(board, sid, name)
    elif case == "only in review":
        done(board, 14, OLGA)
        set_raw(board, 15, assignee=BOB, status=Status.REVIEW, reviewer=CARL)
        set_raw(board, 16, status=Status.CANCELLED)
    else:  # only todo directions, none of them free or to take by a member: nothing works on #8 yet
        give(board, 14, EXT)
        give(board, 15, USER)
        board.update(PETE, 16, depends_on=[15])
    assert gaps(board) == {}


@pytest.mark.parametrize("case", ["blocked", "review", "done"])
def test_a_crew_task_blocked_in_review_or_closed_is_left_out(board, case):
    engine(board)
    nothing_to_take(board)
    if case == "blocked":
        set_raw(board, 8, depends_on=f"[{board.create(PETE, 'gate').id}]")
        assert board.get(8).blocked_by
    else:
        set_raw(board, 8, status=Status.REVIEW if case == "review" else Status.DONE)
    assert len(board.get(8).crew_working) < 3
    assert gaps(board) == {}


# --- the parts of the notice -------------------------------------------------------------------------------------

def test_the_list_leaves_out_cancelled_directions(board):
    engine(board)
    set_raw(board, 14, status=Status.CANCELLED)
    done(board, 15, BOB)
    work(board, 16, ANNA)
    task, _, named = board.crew_gaps()[0]
    assert board.crew_gap_parts(task, named, "en") == (
        ' Its directions: #15 [done] "eval" @Bob Brown; #16 [in_progress] "uci" @Anna Smith.', "")


def test_at_most_five_held_directions_are_named_in_the_notice(board):
    engine(board)
    done(board, 14, OLGA)  # OLGA has a direction of her own: the ones she adds are free
    board.update(OLGA, 8, directions=[f"part {i}" for i in range(CREW_GAP_HELD_MAX)])
    work(board, 15, BOB)
    held_ids = [s.id for s in board.subtasks_of(8) if s.id > 15]
    for sid in held_ids:
        give(board, sid, BOB)
    task, _, named = board.crew_gaps()[0]
    assert len(held_ids) == CREW_GAP_HELD_MAX + 1
    assert named == [(sid, BOB, 15) for sid in held_ids]  # crew_gaps names them all
    assert board.crew_gap_parts(task, named, "en")[1] == "".join(held("en", sid, BOB, 15)
                                                                 for sid in held_ids[:CREW_GAP_HELD_MAX])


# --- the cost of one call -------------------------------------------------------------------------------------------

def test_one_call_reads_each_assignee_once_so_its_queries_grow_linearly(swarm, board):
    """crew_gaps() runs at each scheduler pass. CARL, idle, is assigned two todo directions in each crew task, which
    he may take, so each crew task is left out: his directions to take and his other tasks are read once per call,
    not once per direction, and twice the crew tasks cost at most twice the queries."""
    def add(n: int) -> None:
        for _ in range(n):
            task, subs = make_crew_raw(board, PETE, 4, ["a", "b", "c"])
            set_raw(board, task.id, assignee=OLGA, status=Status.IN_PROGRESS)
            work(board, subs[0].id, ANNA)
            give(board, subs[1].id, CARL)
            give(board, subs[2].id, CARL)

    def queries() -> int:
        statements: list[str] = []
        swarm.db.conn.set_trace_callback(statements.append)
        try:
            assert board.crew_gaps() == []
        finally:
            swarm.db.conn.set_trace_callback(None)
        return len(statements)

    add(4)
    few = queries()
    add(4)
    many = queries()
    assert many <= 2 * few, (few, many)
