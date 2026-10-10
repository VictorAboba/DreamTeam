from __future__ import annotations

import threading

import pytest

from dreamteam import prompts
from dreamteam.board import MAX_POST, NOTIFY_TEXT_LIMIT, UNSET, Board, find_mentions, normalize_status
from dreamteam.models import USER, AgentKind, AgentStatus, Status, Task, ToolError

ANNA, BOB, CARL, DINA = "Anna Smith", "Bob Brown", "Carl Clark", "Dina Evans"


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


def events(swarm, type_: str) -> list[dict]:
    return [e for e in swarm.bus.recent(limit=1000) if e["type"] == type_]


def keys_for(swarm, name: str) -> list[str]:
    return [k for k, _ in swarm.notifications_for(name)]


def clear(swarm) -> None:
    swarm.notified.clear()


def make_done(board: Board, actor: str, title: str = "dep", **kw) -> Task:
    task = board.create(actor, title, **kw)
    board.claim(actor, task.id)
    return board.complete(actor, task.id, "ok")


# --- helpers ---------------------------------------------------------------------------------------

def test_normalize_status():
    assert normalize_status("In Progress") == "in_progress"
    assert normalize_status(" in-progress ") == "in_progress"
    with pytest.raises(ToolError) as exc:
        normalize_status("finished")
    for s in Status.ALL:
        assert s in str(exc.value)


def test_find_mentions_basic_and_longest_first():
    names = ["Anna", "Anna Smith", "Bob Brown"]
    assert find_mentions("hi @Anna Smith and @bob   brown", names) == ["Anna Smith", "Bob Brown"]
    assert find_mentions("hey @Anna Smithson", names) == ["Anna"]
    assert find_mentions("hey @anna, look", names) == ["Anna"]


def test_find_mentions_word_boundaries_and_user():
    names = ["Bob Brown", "Иван Петров"]
    assert find_mentions("@Bob Browning is not Bob", names) == []
    assert find_mentions("mail bob@Bob Brown.com", names) == []
    assert find_mentions("@ИВАН петров, глянь", names) == ["Иван Петров"]
    assert find_mentions("@Иван Петровский", names) == []
    assert find_mentions("ask @user or @Пользователь", names) == [USER]
    assert find_mentions("x@user.com", names) == []
    assert find_mentions("(@Bob Brown)", names) == ["Bob Brown"]
    assert find_mentions("no mentions here", []) == []


# --- create / get ------------------------------------------------------------------------------------

def test_create_basic(swarm, board):
    task = board.create(ANNA, "  Write spec  ", goal="Spec agreed", description="details", tags=["doc", " doc ", "Api"])
    assert task.id == 1 and task.title == "Write spec" and task.goal == "Spec agreed"
    assert task.status == Status.TODO and task.created_by == ANNA and task.assignee is None
    assert task.priority == 1 and task.tags == ["doc", "Api"] and task.depends_on == []
    assert task.blocked_by == [] and task.comments == 0 and task.result is None
    ev = events(swarm, "task.created")
    assert len(ev) == 1 and ev[0]["actor"] == ANNA and ev[0]["data"]["task"] == task.to_dict()
    assert swarm.notified == []


def test_create_with_assignee_notifies(swarm, board):
    task = board.create(ANNA, "Build", assignee="bob  BROWN", reviewer="carl clark")
    assert task.assignee == BOB and task.reviewer == CARL and task.status == Status.TODO
    assert swarm.notifications_for(BOB) == [("n_assigned", {"id": task.id, "title": "Build", "by": ANNA})]
    assert swarm.notifications_for(CARL) == []


def test_create_assigned_to_self_or_user_does_not_notify(swarm, board):
    board.create(ANNA, "Mine", assignee=ANNA)
    t = board.create(ANNA, "For the user", assignee="пользователь")
    assert t.assignee == USER
    assert swarm.notified == []


def test_create_validation(swarm, board):
    with pytest.raises(ToolError, match="empty"):
        board.create(ANNA, "   ")
    with pytest.raises(ToolError, match="too long"):
        board.create(ANNA, "x" * 201)
    assert board.create(ANNA, "x" * 200).title == "x" * 200
    for bad in (-1, 4, "high", True, 1.5):
        with pytest.raises(ToolError, match="priority"):
            board.create(ANNA, "t", priority=bad)
    assert board.create(ANNA, "t", priority="3").priority == 3
    with pytest.raises(ToolError, match="Unknown member"):
        board.create(ANNA, "t", assignee="Nobody Here")
    with pytest.raises(ToolError, match="Unknown task ids in depends_on: #99"):
        board.create(ANNA, "t", depends_on=[1, 99])
    with pytest.raises(ToolError, match="Parent task #42 not found"):
        board.create(ANNA, "t", parent_id=42)
    assert events(swarm, "task.created") and len(events(swarm, "task.created")) == 2


def test_create_retired_assignee_rejected(swarm, board):
    swarm.add_member("Old Timer", status=AgentStatus.RETIRED)
    with pytest.raises(ToolError, match="Unknown member"):
        board.create(ANNA, "t", assignee="Old Timer")


def test_create_with_deps_and_parent(swarm, board):
    a = board.create(ANNA, "a")
    b = board.create(ANNA, "b")
    child = board.create(ANNA, "c", depends_on=[a.id, "#2", a.id], parent_id=b.id, priority=0)
    assert child.depends_on == [a.id, b.id] and child.parent_id == b.id
    assert child.blocked_by == [a.id, b.id]
    assert board.blockers(child) == [a.id, b.id]


def test_get_missing_and_filled(swarm, board):
    with pytest.raises(ToolError, match="Task #5 not found"):
        board.get(5)
    with pytest.raises(ToolError, match="integer"):
        board.get("abc")
    dep = make_done(board, ANNA)
    other = board.create(ANNA, "other")
    t = board.create(ANNA, "t", depends_on=[dep.id, other.id])
    board.post(BOB, t.id, "one")
    board.post(ANNA, t.id, "two")
    got = board.get(str(t.id))
    assert got.blocked_by == [other.id] and got.comments == 2


# --- list / counts -----------------------------------------------------------------------------------

def test_list_order_and_closed_filtering(swarm, board):
    low = board.create(ANNA, "low", priority=0)
    crit = board.create(ANNA, "crit", priority=3)
    norm = board.create(ANNA, "norm")
    crit2 = board.create(ANNA, "crit2", priority=3)
    done = make_done(board, ANNA, "done", priority=3)
    cancelled = board.create(ANNA, "cancelled")
    board.update(ANNA, cancelled.id, status="cancelled")
    assert [t.id for t in board.list()] == [crit.id, crit2.id, norm.id, low.id]
    assert [t.id for t in board.list(include_closed=True)] == [crit.id, crit2.id, done.id, norm.id, cancelled.id,
                                                               low.id]
    assert [t.id for t in board.list(status="done")] == [done.id]
    assert [t.id for t in board.list(status=["done", "cancelled"])] == [done.id, cancelled.id]
    assert [t.id for t in board.list(status="todo,cancelled")] == [crit.id, crit2.id, norm.id, cancelled.id,
                                                                   low.id]
    with pytest.raises(ToolError, match="Valid statuses: todo, in_progress"):
        board.list(status="open")


def test_list_filters(swarm, board):
    parent = board.create(ANNA, "parent", tags=["Backend"])
    a = board.create(ANNA, "a", assignee=BOB, parent_id=parent.id, tags=["backend", "api"])
    b = board.create(ANNA, "b", assignee=CARL, parent_id=parent.id)
    board.create(ANNA, "c", tags=["ui"])
    assert [t.id for t in board.list(assignee="bob brown")] == [a.id]
    assert [t.id for t in board.list(assignee="nobody")] == []
    assert [t.id for t in board.list(tag="BACKEND")] == [parent.id, a.id]
    assert [t.id for t in board.list(parent_id=parent.id)] == [a.id, b.id]
    assert [t.id for t in board.list(parent_id=parent.id, assignee=CARL)] == [b.id]


def test_list_fills_blocked_by_and_comments(swarm, board):
    dep = board.create(ANNA, "dep")
    t = board.create(ANNA, "t", depends_on=[dep.id])
    board.post(BOB, dep.id, "hello")
    by_id = {x.id: x for x in board.list()}
    assert by_id[t.id].blocked_by == [dep.id] and by_id[dep.id].comments == 1 and by_id[t.id].comments == 0


def test_counts(swarm, board):
    board.create(ANNA, "a")
    board.create(ANNA, "b")
    make_done(board, ANNA)
    counts = board.counts()
    assert set(counts) == set(Status.ALL)
    assert counts["todo"] == 2 and counts["done"] == 1 and counts["review"] == 0


def test_ready_unassigned_and_open_tasks_of(swarm, board):
    dep = board.create(ANNA, "dep", priority=0)
    ready = board.create(ANNA, "ready", priority=2)
    board.create(ANNA, "blocked", depends_on=[dep.id])
    board.create(ANNA, "assigned", assignee=BOB)
    taken = board.create(ANNA, "taken")
    board.claim(CARL, taken.id)
    assert [t.id for t in board.ready_unassigned()] == [ready.id, dep.id]
    assert [t.title for t in board.open_tasks_of(BOB)] == ["assigned"]
    assert [t.title for t in board.open_tasks_of(CARL)] == ["taken"]
    board.complete(CARL, taken.id, "done")
    assert board.open_tasks_of(CARL) == []


# --- claim -----------------------------------------------------------------------------------------

def test_claim_happy_path(swarm, board):
    t = board.create(ANNA, "t")
    claimed = board.claim(BOB, t.id)
    assert claimed.assignee == BOB and claimed.status == Status.IN_PROGRESS
    ev = events(swarm, "task.updated")
    assert len(ev) == 1 and ev[0]["actor"] == BOB
    assert ev[0]["data"]["changes"] == {"assignee": [None, BOB], "status": ["todo", "in_progress"]}
    assert ev[0]["data"]["task"] == claimed.to_dict()


def test_claim_idempotent_for_holder(swarm, board):
    t = board.create(ANNA, "t")
    board.claim(BOB, t.id)
    again = board.claim(BOB, t.id)
    assert again.status == Status.IN_PROGRESS and len(events(swarm, "task.updated")) == 1
    board.update(BOB, t.id, status="waiting")
    assert board.claim(BOB, t.id).status == Status.WAITING


def test_claim_assigned_todo_by_assignee(swarm, board):
    t = board.create(ANNA, "t", assignee=BOB)
    assert board.claim(BOB, t.id).status == Status.IN_PROGRESS


def test_claim_errors(swarm, board):
    t = board.create(ANNA, "t", assignee=BOB)
    with pytest.raises(ToolError, match="already assigned to Bob Brown"):
        board.claim(CARL, t.id)
    board.claim(BOB, t.id)
    with pytest.raises(ToolError, match="already assigned to Bob Brown"):
        board.claim(CARL, t.id)
    done = make_done(board, ANNA)
    with pytest.raises(ToolError, match="only 'todo' tasks can be claimed"):
        board.claim(CARL, done.id)
    with pytest.raises(ToolError, match="not found"):
        board.claim(CARL, 777)


def test_claim_blocked(swarm, board):
    d1 = board.create(ANNA, "d1")
    d2 = board.create(ANNA, "d2")
    board.claim(BOB, d2.id)
    t = board.create(ANNA, "t", depends_on=[d1.id, d2.id])
    with pytest.raises(ToolError) as exc:
        board.claim(CARL, t.id)
    assert "#1 [todo], #2 [in_progress]" in str(exc.value) and "done(1, 2)" in str(exc.value)
    assert board.get(t.id).assignee is None
    board.claim(ANNA, d1.id)
    board.complete(ANNA, d1.id, "ok")
    board.complete(BOB, d2.id, "ok")
    assert board.claim(CARL, t.id).assignee == CARL


def test_claim_is_atomic_across_threads(swarm, board):
    t = board.create(ANNA, "contested")
    actors = [ANNA, BOB, CARL, DINA] * 3
    barrier = threading.Barrier(len(actors))
    wins: list[str] = []
    errors: list[str] = []

    def worker(name: str) -> None:
        barrier.wait()
        try:
            task = board.claim(name, t.id)
            if task.assignee == name:
                wins.append(name)
        except ToolError as exc:
            errors.append(str(exc))

    threads = [threading.Thread(target=worker, args=(a,)) for a in actors]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    final = board.get(t.id)
    # one winner; the winner's repeated claims are idempotent, everyone else is refused
    assert len(events(swarm, "task.updated")) == 1
    assert set(wins) == {final.assignee}
    assert len(errors) == len(actors) - wins.count(final.assignee)


# --- update: fields & permissions --------------------------------------------------------------------

def test_update_fields_by_creator_and_changes(swarm, board):
    t = board.create(ANNA, "t", goal="g")
    upd = board.update(ANNA, t.id, title="t2", goal="g2", description="d", priority=2, tags=["x"])
    assert (upd.title, upd.goal, upd.description, upd.priority, upd.tags) == ("t2", "g2", "d", 2, ["x"])
    ev = events(swarm, "task.updated")
    assert len(ev) == 1
    assert ev[0]["data"]["changes"] == {"title": ["t", "t2"], "goal": ["g", "g2"], "description": ["", "d"],
                                        "priority": [1, 2], "tags": [[], ["x"]]}


def test_update_noop_emits_nothing(swarm, board):
    t = board.create(ANNA, "t", goal="g")
    same = board.update(ANNA, t.id, title="t", goal="g")
    assert same.title == "t" and events(swarm, "task.updated") == []
    # unchanged values are not a permission problem either
    board.update(BOB, t.id, title="t")


def test_update_field_permissions(swarm, board):
    t = board.create(ANNA, "t", assignee=BOB)
    with pytest.raises(ToolError, match="Only the creator \\(Anna Smith\\) or the assignee \\(Bob Brown\\)"):
        board.update(CARL, t.id, title="hijack")
    for field, value in (("goal", "x"), ("description", "x"), ("priority", 3), ("tags", ["x"]),
                         ("depends_on", [board.create(ANNA, "d").id]), ("reviewer", CARL), ("parent_id", 2)):
        with pytest.raises(ToolError, match="Only the creator"):
            board.update(CARL, t.id, **{field: value})
    assert board.update(BOB, t.id, title="by assignee").title == "by assignee"
    assert board.update(USER, t.id, title="by user").title == "by user"


def test_update_reviewer_and_parent(swarm, board):
    parent = board.create(ANNA, "parent")
    t = board.create(ANNA, "t")
    upd = board.update(ANNA, t.id, reviewer="carl clark", parent_id=parent.id)
    assert upd.reviewer == CARL and upd.parent_id == parent.id
    upd = board.update(ANNA, t.id, reviewer=None, parent_id=None)
    assert upd.reviewer is None and upd.parent_id is None
    upd = board.update(ANNA, t.id, reviewer="user")
    assert upd.reviewer == USER
    with pytest.raises(ToolError, match="own parent"):
        board.update(ANNA, t.id, parent_id=t.id)
    with pytest.raises(ToolError, match="not found"):
        board.update(ANNA, t.id, parent_id=99)
    board.update(ANNA, t.id, parent_id=parent.id)
    grandchild = board.create(ANNA, "gc", parent_id=t.id)
    with pytest.raises(ToolError, match="subtask"):
        board.update(ANNA, parent.id, parent_id=grandchild.id)


def test_update_depends_on_validation(swarm, board):
    t1 = board.create(ANNA, "1")
    t2 = board.create(ANNA, "2", depends_on=[t1.id])
    t3 = board.create(ANNA, "3", depends_on=[t2.id])
    with pytest.raises(ToolError, match="itself"):
        board.update(ANNA, t1.id, depends_on=[t1.id])
    with pytest.raises(ToolError, match="Unknown task ids in depends_on: #50"):
        board.update(ANNA, t1.id, depends_on=[50])
    with pytest.raises(ToolError, match="Dependency cycle: #1 -> #3 -> #2 -> #1"):
        board.update(ANNA, t1.id, depends_on=[t3.id])
    with pytest.raises(ToolError, match="cycle"):
        board.update(ANNA, t1.id, depends_on=[t2.id])
    upd = board.update(ANNA, t3.id, depends_on=[t1.id, t2.id])
    assert upd.depends_on == [t1.id, t2.id] and upd.blocked_by == [t1.id, t2.id]
    assert board.update(ANNA, t3.id, depends_on=[]).depends_on == []
    assert events(swarm, "task.updated")[-1]["data"]["changes"] == {"depends_on": [[t1.id, t2.id], []]}


def test_update_invalid_priority_and_status(swarm, board):
    t = board.create(ANNA, "t")
    with pytest.raises(ToolError, match="priority"):
        board.update(ANNA, t.id, priority=7)
    with pytest.raises(ToolError, match="Unknown status 'closed'. Valid statuses: todo, in_progress, waiting, "
                                        "review, done, failed, cancelled"):
        board.update(ANNA, t.id, status="closed")
    with pytest.raises(ToolError, match="not found"):
        board.update(ANNA, 99, title="x")


# --- update: assignment ------------------------------------------------------------------------------

def test_assign_unassigned_task_by_anyone(swarm, board):
    t = board.create(ANNA, "t")
    upd = board.update(CARL, t.id, assignee="bob brown")
    assert upd.assignee == BOB and upd.status == Status.TODO
    assert swarm.notifications_for(BOB) == [("n_assigned", {"id": t.id, "title": "t", "by": CARL})]


def test_reassign_permissions(swarm, board):
    t = board.create(ANNA, "t", assignee=BOB)
    with pytest.raises(ToolError, match="assigned to Bob Brown; only they or the creator"):
        board.update(CARL, t.id, assignee=CARL)
    assert board.update(ANNA, t.id, assignee=CARL).assignee == CARL   # creator
    assert board.update(CARL, t.id, assignee=DINA).assignee == DINA   # current assignee
    assert board.update(USER, t.id, assignee=BOB).assignee == BOB     # user


def test_reassign_in_progress_resets_status_and_notifies(swarm, board):
    t = board.create(ANNA, "t")
    board.claim(BOB, t.id)
    clear(swarm)
    upd = board.update(ANNA, t.id, assignee=CARL)
    assert upd.assignee == CARL and upd.status == Status.TODO
    assert swarm.notifications_for(CARL) == [("n_assigned", {"id": t.id, "title": "t", "by": ANNA})]
    assert swarm.notifications_for(BOB) == [("n_unassigned", {"id": t.id, "title": "t", "by": ANNA})]
    assert swarm.notifications_for(ANNA) == []
    assert events(swarm, "task.updated")[-1]["data"]["changes"] == {
        "assignee": [BOB, CARL], "status": ["in_progress", "todo"]}


def test_reassign_waiting_resets_but_review_keeps(swarm, board):
    t = board.create(ANNA, "t")
    board.claim(BOB, t.id)
    board.update(BOB, t.id, status="waiting")
    assert board.update(BOB, t.id, assignee=CARL).status == Status.TODO
    r = board.create(ANNA, "r", reviewer=DINA)
    board.claim(BOB, r.id)
    board.complete(BOB, r.id, "res")
    assert board.update(ANNA, r.id, assignee=CARL).status == Status.REVIEW


def test_reassign_with_explicit_status_wins(swarm, board):
    t = board.create(ANNA, "t")
    board.claim(BOB, t.id)
    upd = board.update(USER, t.id, assignee=CARL, status="in_progress")
    assert upd.assignee == CARL and upd.status == Status.IN_PROGRESS


def test_unassign_by_empty_string_or_none(swarm, board):
    t = board.create(ANNA, "t")
    board.claim(BOB, t.id)
    clear(swarm)
    upd = board.update(BOB, t.id, assignee="")
    assert upd.assignee is None and upd.status == Status.TODO
    assert swarm.notified == []  # the actor is never notified
    board.update(ANNA, t.id, assignee=CARL)
    clear(swarm)
    assert board.update(USER, t.id, assignee=None).assignee is None
    assert keys_for(swarm, CARL) == ["n_unassigned"]


def test_user_assignee_never_notified(swarm, board):
    t = board.create(ANNA, "t", assignee=USER)
    board.update(ANNA, t.id, assignee=BOB)
    assert swarm.notifications_for(USER) == []
    board.update(ANNA, t.id, assignee="user")
    assert swarm.notifications_for(USER) == []
    assert keys_for(swarm, BOB) == ["n_assigned", "n_unassigned"]


def test_assign_unknown_member(swarm, board):
    t = board.create(ANNA, "t")
    with pytest.raises(ToolError, match="Unknown member 'Zed'"):
        board.update(ANNA, t.id, assignee="Zed")
    assert UNSET is not None


# --- update: status transitions ------------------------------------------------------------------------

def test_assignee_status_transitions(swarm, board):
    t = board.create(ANNA, "t")
    board.claim(BOB, t.id)
    assert board.update(BOB, t.id, status="waiting").status == Status.WAITING
    assert board.update(BOB, t.id, status="in_progress").status == Status.IN_PROGRESS
    clear(swarm)
    upd = board.update(BOB, t.id, status="failed")
    assert upd.status == Status.FAILED and upd.assignee == BOB
    assert swarm.notifications_for(ANNA) == [("n_task_closed", {
        "id": t.id, "title": "t", "status": "failed", "by": BOB, "result": "-"})]


def test_release_by_assignee_clears_assignee(swarm, board):
    t = board.create(ANNA, "t")
    board.claim(BOB, t.id)
    clear(swarm)
    upd = board.update(BOB, t.id, status="todo")
    assert upd.status == Status.TODO and upd.assignee is None
    assert swarm.notified == []
    assert events(swarm, "task.updated")[-1]["data"]["changes"] == {
        "status": ["in_progress", "todo"], "assignee": [BOB, None]}
    # assigned-but-not-started tasks can be released too
    t2 = board.create(ANNA, "t2", assignee=BOB)
    assert board.update(BOB, t2.id, status="todo").assignee is None


def test_release_by_user_notifies_removed_assignee(swarm, board):
    t = board.create(ANNA, "t")
    board.claim(BOB, t.id)
    clear(swarm)
    upd = board.update(USER, t.id, status="todo")
    assert upd.assignee is None and upd.status == Status.TODO
    assert keys_for(swarm, BOB) == ["n_unassigned"]


def test_handover_with_status_todo_and_assignee(swarm, board):
    t = board.create(ANNA, "t")
    board.claim(BOB, t.id)
    upd = board.update(BOB, t.id, status="todo", assignee=CARL)
    assert upd.assignee == CARL and upd.status == Status.TODO


def test_in_progress_requires_no_blockers_for_members(swarm, board):
    dep = board.create(ANNA, "dep")
    t = board.create(ANNA, "t", assignee=BOB)
    board.update(ANNA, t.id, depends_on=[dep.id])
    with pytest.raises(ToolError, match="blocked by unfinished dependencies: #1 \\[todo\\]"):
        board.update(BOB, t.id, status="in_progress")
    assert board.update(USER, t.id, status="in_progress").status == Status.IN_PROGRESS


def test_in_progress_checks_new_dependencies_in_same_call(swarm, board):
    dep = board.create(ANNA, "dep")
    t = board.create(BOB, "t", assignee=BOB)
    with pytest.raises(ToolError, match="blocked"):
        board.update(BOB, t.id, depends_on=[dep.id], status="in_progress")
    assert board.get(t.id).depends_on == []  # rolled back


def test_non_assignee_cannot_set_working_statuses(swarm, board):
    t = board.create(ANNA, "t")
    for status in ("in_progress", "waiting", "failed"):
        with pytest.raises(ToolError, match="Only the assignee of task #1 \\(nobody\\).*task_claim"):
            board.update(ANNA, t.id, status=status)
    board.claim(BOB, t.id)
    with pytest.raises(ToolError, match="Only the assignee of task #1 \\(Bob Brown\\)"):
        board.update(ANNA, t.id, status="waiting")
    with pytest.raises(ToolError, match="Only the assignee"):
        board.update(ANNA, t.id, status="todo")


def test_self_assign_and_start_in_one_call(swarm, board):
    t = board.create(ANNA, "t")
    upd = board.update(CARL, t.id, assignee=CARL, status="in_progress")
    assert upd.assignee == CARL and upd.status == Status.IN_PROGRESS


def test_cancel_permissions(swarm, board):
    t = board.create(ANNA, "t", assignee=BOB)
    with pytest.raises(ToolError, match="cancel"):
        board.update(CARL, t.id, status="cancelled")
    assert board.update(BOB, t.id, status="cancelled").status == Status.CANCELLED
    assert swarm.notifications_for(ANNA)[-1][0] == "n_task_closed"
    t2 = board.create(ANNA, "t2", assignee=BOB)
    clear(swarm)
    assert board.update(ANNA, t2.id, status="cancelled").status == Status.CANCELLED
    assert swarm.notified == []  # creator is the actor; the assignee is not told by the rules


def test_done_and_review_only_via_complete(swarm, board):
    t = board.create(ANNA, "t")
    board.claim(BOB, t.id)
    with pytest.raises(ToolError, match="task_complete"):
        board.update(BOB, t.id, status="done")
    with pytest.raises(ToolError, match="review"):
        board.update(BOB, t.id, status="review")
    with pytest.raises(ToolError, match="review"):
        board.update(USER, t.id, status="review")
    upd = board.update(USER, t.id, status="done")
    assert upd.status == Status.DONE
    assert keys_for(swarm, ANNA) == ["n_task_closed"]


def test_reopen_terminal(swarm, board):
    t = board.create(ANNA, "t")
    board.claim(BOB, t.id)
    board.complete(BOB, t.id, "result")
    for status in ("in_progress", "failed", "cancelled"):
        with pytest.raises(ToolError, match="reopen"):
            board.update(ANNA, t.id, status=status)
    with pytest.raises(ToolError, match="only its creator \\(Anna Smith\\) or the user can reopen"):
        board.update(BOB, t.id, status="todo")
    clear(swarm)
    upd = board.update(ANNA, t.id, status="todo")
    assert upd.status == Status.TODO and upd.assignee == BOB and upd.result == "result"
    assert swarm.notifications_for(BOB) == [("n_assigned", {"id": t.id, "title": "t", "by": ANNA})]
    board.claim(BOB, t.id)
    board.complete(BOB, t.id, "again")
    assert board.update(USER, t.id, status="todo").status == Status.TODO


def test_user_can_move_terminal_task_anywhere_but_review(swarm, board):
    t = board.create(ANNA, "t")
    board.update(ANNA, t.id, status="cancelled")
    assert board.update(USER, t.id, status="failed").status == Status.FAILED
    assert board.update(USER, t.id, status="done").status == Status.DONE


# --- complete ----------------------------------------------------------------------------------------

def test_complete_without_reviewer(swarm, board):
    t = board.create(ANNA, "t")
    board.claim(BOB, t.id)
    clear(swarm)
    done = board.complete(BOB, t.id, "  built it  ")
    assert done.status == Status.DONE and done.result == "built it"
    assert swarm.notifications_for(ANNA) == [("n_task_closed", {
        "id": t.id, "title": "t", "status": "done", "by": BOB, "result": "built it"})]
    ev = events(swarm, "task.updated")[-1]
    assert ev["data"]["changes"] == {"result": [None, "built it"], "status": ["in_progress", "done"]}


def test_complete_with_reviewer_goes_to_review(swarm, board):
    t = board.create(ANNA, "t", reviewer=CARL)
    board.claim(BOB, t.id)
    clear(swarm)
    upd = board.complete(BOB, t.id, "res")
    assert upd.status == Status.REVIEW
    assert swarm.notifications_for(CARL) == [("n_review_requested", {"id": t.id, "title": "t", "by": BOB})]
    assert swarm.notifications_for(ANNA) == []


def test_complete_reviewer_is_actor_or_user(swarm, board):
    t = board.create(ANNA, "t", reviewer=BOB)
    board.claim(BOB, t.id)
    assert board.complete(BOB, t.id, "res").status == Status.DONE
    t2 = board.create(ANNA, "t2", reviewer=USER)
    board.claim(BOB, t2.id)
    clear(swarm)
    assert board.complete(BOB, t2.id, "res").status == Status.REVIEW
    assert swarm.notified == []  # the user sees reviews in the UI


def test_complete_errors(swarm, board):
    t = board.create(ANNA, "t", reviewer=CARL)
    with pytest.raises(ToolError, match="Only the assignee of task #1 \\(nobody\\)"):
        board.complete(BOB, t.id, "res")
    board.claim(BOB, t.id)
    with pytest.raises(ToolError, match="Only the assignee of task #1 \\(Bob Brown\\)"):
        board.complete(ANNA, t.id, "res")
    with pytest.raises(ToolError, match="Result must not be empty"):
        board.complete(BOB, t.id, "  ")
    board.complete(BOB, t.id, "res")
    with pytest.raises(ToolError, match="already in review"):
        board.complete(BOB, t.id, "res")
    board.review(CARL, t.id, True)
    with pytest.raises(ToolError, match="already done"):
        board.complete(BOB, t.id, "res")


def test_complete_by_user_and_from_todo(swarm, board):
    t = board.create(ANNA, "t")
    assert board.complete(USER, t.id, "user did it").status == Status.DONE
    t2 = board.create(ANNA, "t2", assignee=BOB)
    assert board.complete(BOB, t2.id, "quick").status == Status.DONE


def test_done_notifies_unblocked_dependents(swarm, board):
    d1 = board.create(USER, "d1")
    d2 = board.create(USER, "d2")
    both = board.create(USER, "both", depends_on=[d1.id, d2.id], assignee=CARL)
    only1 = board.create(USER, "only1", depends_on=[d1.id], assignee=DINA)
    unassigned = board.create(USER, "unassigned", depends_on=[d1.id])
    mine = board.create(USER, "mine", depends_on=[d1.id], assignee=BOB)
    board.claim(BOB, d1.id)
    clear(swarm)
    board.complete(BOB, d1.id, "ok")
    assert swarm.notifications_for(DINA) == [("n_unblocked", {"id": only1.id, "title": "only1"})]
    assert swarm.notifications_for(CARL) == []          # still blocked by d2
    assert swarm.notifications_for(BOB) == []           # the actor
    assert swarm.notifications_for(USER) == []          # creator is USER
    assert unassigned.id and mine.id
    board.claim(ANNA, d2.id)
    clear(swarm)
    board.complete(ANNA, d2.id, "ok")
    assert swarm.notifications_for(CARL) == [("n_unblocked", {"id": both.id, "title": "both"})]


def test_closed_dependents_not_notified(swarm, board):
    d = board.create(ANNA, "d")
    t = board.create(ANNA, "t", depends_on=[d.id], assignee=CARL)
    board.update(CARL, t.id, status="cancelled")
    board.claim(BOB, d.id)
    clear(swarm)
    board.complete(BOB, d.id, "ok")
    assert swarm.notifications_for(CARL) == []


def test_task_closed_truncates_result_and_skips_non_members(swarm, board):
    t = board.create(ANNA, "t")
    board.claim(BOB, t.id)
    clear(swarm)
    board.complete(BOB, t.id, "r" * 5000)
    (key, kw), = swarm.notifications_for(ANNA)
    assert key == "n_task_closed" and len(kw["result"]) == NOTIFY_TEXT_LIMIT
    assert board.get(t.id).result == "r" * 5000
    swarm.add_member("Gone Away")
    t2 = board.create("Gone Away", "t2")
    swarm.db.execute("UPDATE agents SET status = 'retired' WHERE name = ?", ("Gone Away",))
    board.claim(BOB, t2.id)
    clear(swarm)
    board.complete(BOB, t2.id, "ok")
    assert swarm.notified == []


# --- review --------------------------------------------------------------------------------------------

def _in_review(board: Board) -> Task:
    t = board.create(ANNA, "t", reviewer=CARL)
    board.claim(BOB, t.id)
    return board.complete(BOB, t.id, "res")


def test_review_approve(swarm, board):
    t = _in_review(board)
    dependent = board.create(ANNA, "dep", depends_on=[t.id], assignee=DINA)
    clear(swarm)
    upd = board.review(CARL, t.id, True)
    assert upd.status == Status.DONE
    assert swarm.notifications_for(BOB) == [("n_review_approved", {"id": t.id, "title": "t", "by": CARL,
                                                                     "comment": ""})]
    assert swarm.notifications_for(DINA) == [("n_unblocked", {"id": dependent.id, "title": "dep"})]
    assert keys_for(swarm, ANNA) == ["n_task_closed"]
    assert board.discussion(t.id) == []


def test_review_reject_posts_comment(swarm, board):
    t = _in_review(board)
    clear(swarm)
    upd = board.review(CARL, t.id, False, "  Tests are missing  ")
    assert upd.status == Status.IN_PROGRESS
    assert swarm.notifications_for(BOB) == [("n_review_rejected", {"id": t.id, "title": "t", "by": CARL,
                                                                     "comment": "Tests are missing"})]
    posts = board.discussion(t.id)
    assert [(p["author"], p["text"]) for p in posts] == [(CARL, "Tests are missing")]
    # the creator follows the discussion; the assignee already got the comment in the review notice
    assert keys_for(swarm, ANNA) == ["n_discussion"]
    types = [e["type"] for e in swarm.bus.recent(limit=50) if e["type"].startswith("task.")]
    assert types[-2:] == ["task.updated", "task.comment"]
    assert board.complete(BOB, t.id, "fixed").status == Status.REVIEW


def test_review_by_user_and_string_approve(swarm, board):
    t = _in_review(board)
    assert board.review(USER, t.id, "false").status == Status.IN_PROGRESS
    board.complete(BOB, t.id, "again")
    assert board.review(USER, t.id, "true").status == Status.DONE


def test_review_errors(swarm, board):
    t = board.create(ANNA, "t", reviewer=CARL)
    with pytest.raises(ToolError, match="not waiting for review"):
        board.review(CARL, t.id, True)
    board.claim(BOB, t.id)
    board.complete(BOB, t.id, "res")
    with pytest.raises(ToolError, match="Only the reviewer of task #1 \\(Carl Clark\\)"):
        board.review(BOB, t.id, True)
    with pytest.raises(ToolError, match="Only the reviewer"):
        board.review(ANNA, t.id, True)


# --- discussion ------------------------------------------------------------------------------------------

def test_post_and_discussion(swarm, board):
    t = board.create(ANNA, "t")
    p1 = board.post(BOB, t.id, "  first  ")
    p2 = board.post(ANNA, t.id, "second", reply_to=p1["id"])
    assert p1["text"] == "first" and p1["task_id"] == t.id and p1["author"] == BOB and p1["reply_to"] is None
    assert p2["reply_to"] == p1["id"]
    assert set(p1) == {"id", "task_id", "author", "text", "reply_to", "ts"}
    disc = board.discussion(t.id)
    assert disc == [p1, p2]
    assert board.discussion(t.id, since_id=p1["id"]) == [p2]
    ev = events(swarm, "task.comment")
    assert len(ev) == 2 and ev[0]["data"] == {"task_id": t.id, "post": p1} and ev[0]["actor"] == BOB


def test_discussion_paging(swarm, board):
    t = board.create(ANNA, "t")
    posts = [board.post(ANNA, t.id, f"p{i}") for i in range(6)]
    assert [p["text"] for p in board.discussion(t.id, limit=2)] == ["p4", "p5"]          # latest
    assert [p["text"] for p in board.discussion(t.id, since_id=posts[1]["id"], limit=2)] == ["p2", "p3"]
    with pytest.raises(ToolError, match="not found"):
        board.discussion(99)


def test_post_validation(swarm, board):
    t = board.create(ANNA, "t")
    other = board.create(ANNA, "other")
    foreign = board.post(ANNA, other.id, "elsewhere")
    with pytest.raises(ToolError, match="empty"):
        board.post(BOB, t.id, "   ")
    with pytest.raises(ToolError, match="too long"):
        board.post(BOB, t.id, "x" * (MAX_POST + 1))
    board.post(BOB, t.id, "x" * MAX_POST)
    with pytest.raises(ToolError, match="not in the discussion of task #1"):
        board.post(BOB, t.id, "reply", reply_to=foreign["id"])
    with pytest.raises(ToolError, match="not in the discussion"):
        board.post(BOB, t.id, "reply", reply_to=999)
    with pytest.raises(ToolError, match="not found"):
        board.post(BOB, 404, "hello")


def test_participants(swarm, board):
    t = board.create(ANNA, "t", assignee=BOB, reviewer=USER)
    board.post(DINA, t.id, "hi")
    board.post(DINA, t.id, "again")
    assert board.participants(t.id) == {ANNA, BOB, USER, DINA}
    t2 = board.create(USER, "t2")
    assert board.participants(t2.id) == {USER}


def test_post_notifies_participants(swarm, board):
    t = board.create(ANNA, "t", assignee=BOB, reviewer=CARL)
    board.post(DINA, t.id, "hello")
    clear(swarm)
    post = board.post(BOB, t.id, "status update")
    expected = {"by": BOB, "id": t.id, "title": "t", "post_id": post["id"], "reply": "", "text": "status update"}
    for name in (ANNA, CARL, DINA):
        assert swarm.notifications_for(name) == [("n_discussion", expected)]
    assert swarm.notifications_for(BOB) == []
    assert swarm.notifications_for(USER) == []


def test_post_mentions(swarm, board):
    t = board.create(ANNA, "t", assignee=BOB)
    clear(swarm)
    post = board.post(BOB, t.id, "@anna smith and @Carl Clark please check; @Bob Brown is me")
    mention = {"by": BOB, "id": t.id, "title": "t", "post_id": post["id"], "text": post["text"]}
    assert swarm.notifications_for(ANNA) == [("n_mention", mention)]
    assert swarm.notifications_for(CARL) == [("n_mention", mention)]
    assert swarm.notifications_for(BOB) == []
    assert swarm.notifications_for(DINA) == []
    # a mention does not make a member a participant
    clear(swarm)
    board.post(ANNA, t.id, "thanks")
    assert keys_for(swarm, BOB) == ["n_discussion"] and swarm.notifications_for(CARL) == []


def test_post_mentions_cyrillic_and_longest_names(make_fake_swarm):
    swarm = make_fake_swarm()
    swarm.board = Board(swarm)
    for name in ("Анна Смирнова", "Анна Смирнова 2", "Иван Орлов"):
        swarm.add_member(name)
    t = swarm.board.create("Иван Орлов", "задача")
    swarm.board.post("Иван Орлов", t.id, "@анна смирнова 2, посмотри")
    assert [r for r, k, _ in swarm.notified if k == "n_mention"] == ["Анна Смирнова 2"]
    swarm.notified.clear()
    swarm.board.post("Иван Орлов", t.id, "@АННА СМИРНОВА: и ты")
    assert [r for r, k, _ in swarm.notified if k == "n_mention"] == ["Анна Смирнова"]


def test_post_user_notification_rules(swarm, board):
    t = board.create(ANNA, "t", reviewer=USER)
    board.post(USER, t.id, "user posted")
    clear(swarm)
    board.post(BOB, t.id, "plain post")
    assert swarm.notifications_for(USER) == []  # reviewer/poster only
    board.post(BOB, t.id, "hey @user, decide please")
    assert keys_for(swarm, USER) == ["n_mention"]
    board.post(BOB, t.id, "и @Пользователь тоже")
    assert keys_for(swarm, USER) == ["n_mention", "n_mention"]
    by_user = board.create(USER, "by user")
    clear(swarm)
    board.post(BOB, by_user.id, "for the creator")
    assert keys_for(swarm, USER) == ["n_discussion"]
    assigned = board.create(ANNA, "assigned to user", assignee=USER)
    clear(swarm)
    board.post(BOB, assigned.id, "for the assignee")
    assert keys_for(swarm, USER) == ["n_discussion"]
    clear(swarm)
    board.post(USER, assigned.id, "@user talking to myself")
    assert swarm.notifications_for(USER) == []


def test_post_reply_suffix_per_language(swarm, board):
    t = board.create(ANNA, "t", assignee=CARL)  # Anna: en, Carl: ru
    first = board.post(BOB, t.id, "q")
    clear(swarm)
    board.post(BOB, t.id, "a", reply_to=first["id"])
    (_, en_kw), = swarm.notifications_for(ANNA)
    (_, ru_kw), = swarm.notifications_for(CARL)
    assert en_kw["reply"] == f", reply to #{first['id']}"
    assert ru_kw["reply"] == f", ответ на #{first['id']}"
    text = prompts.t("ru", "n_discussion", **ru_kw)
    assert f"ответ на #{first['id']}" in text


def test_post_truncates_notification_text(swarm, board):
    t = board.create(ANNA, "t")
    long_text = "@Bob Brown " + "y" * 4000
    clear(swarm)
    post = board.post(CARL, t.id, long_text)
    assert post["text"] == long_text
    for name in (ANNA, BOB):
        (_, kw), = swarm.notifications_for(name)
        assert len(kw["text"]) == NOTIFY_TEXT_LIMIT and kw["text"].endswith("…")


def test_post_skips_retired_participants(swarm, board):
    swarm.add_member("Old Timer")
    t = board.create(ANNA, "t")
    board.post("Old Timer", t.id, "bye")
    swarm.db.execute("UPDATE agents SET status = 'retired' WHERE name = 'Old Timer'")
    clear(swarm)
    board.post(ANNA, t.id, "@Old Timer are you there?")
    assert swarm.notified == []
    assert "Old Timer" in board.participants(t.id)


def test_post_updates_comment_count(swarm, board):
    t = board.create(ANNA, "t")
    board.post(BOB, t.id, "a")
    assert board.get(t.id).comments == 1


# --- release_all ---------------------------------------------------------------------------------------

def test_release_all(swarm, board):
    a = board.create(ANNA, "a")
    board.claim(BOB, a.id)
    b = board.create(ANNA, "b", assignee=BOB)
    c = board.create(ANNA, "c", reviewer=CARL)
    board.claim(BOB, c.id)
    board.complete(BOB, c.id, "res")
    d = board.create(ANNA, "d")
    board.claim(BOB, d.id)
    board.complete(BOB, d.id, "done")
    other = board.create(ANNA, "other")
    board.claim(CARL, other.id)
    before = len(events(swarm, "task.updated"))
    clear(swarm)
    released = board.release_all(BOB, USER)
    assert released == [a.id, b.id, c.id]
    for tid in released:
        task = board.get(tid)
        assert task.assignee is None and task.status == Status.TODO
    assert board.get(d.id).assignee == BOB and board.get(other.id).assignee == CARL
    ev = events(swarm, "task.updated")[before:]
    assert [e["data"]["changes"] for e in ev] == [
        {"assignee": [BOB, None], "status": ["in_progress", "todo"]},
        {"assignee": [BOB, None]},
        {"assignee": [BOB, None], "status": ["review", "todo"]},
    ]
    assert all(e["actor"] == USER for e in ev)
    assert swarm.notified == []
    assert board.release_all(BOB, USER) == []


# --- regressions -----------------------------------------------------------------------------------------

def test_review_with_too_long_comment_changes_nothing(swarm, board):
    # the comment used to be validated only when posted, i.e. after the review was committed and announced
    t = _in_review(board)
    clear(swarm)
    before = len(events(swarm, "task.updated"))
    with pytest.raises(ToolError, match="too long"):
        board.review(CARL, t.id, False, "x" * (MAX_POST + 1))
    assert board.get(t.id).status == Status.REVIEW
    assert swarm.notified == [] and len(events(swarm, "task.updated")) == before
    assert board.discussion(t.id) == []
    assert board.review(CARL, t.id, False, "x" * MAX_POST).status == Status.IN_PROGRESS


def test_review_reject_without_assignee_returns_to_todo(swarm, board):
    # an in_progress task nobody holds is neither claimable nor in anyone's open tasks
    t = board.create(ANNA, "t", reviewer=CARL)
    board.complete(USER, t.id, "user did it")
    upd = board.review(CARL, t.id, False, "not good enough")
    assert upd.status == Status.TODO and upd.assignee is None
    assert [x.id for x in board.ready_unassigned()] == [t.id]
    assert board.claim(BOB, t.id).status == Status.IN_PROGRESS
    # same when the assignee was removed while the task waited for review
    r = _in_review(board)
    board.update(ANNA, r.id, assignee="")
    assert board.review(CARL, r.id, False).status == Status.TODO


def test_review_comment_truncated_in_notification(swarm, board):
    t = _in_review(board)
    clear(swarm)
    board.review(CARL, t.id, False, "z" * 5000)
    (key, kw), = swarm.notifications_for(BOB)
    assert key == "n_review_rejected" and len(kw["comment"]) == NOTIFY_TEXT_LIMIT and kw["comment"].endswith("…")
    assert board.discussion(t.id)[0]["text"] == "z" * 5000


def test_review_error_names_missing_reviewer(swarm, board):
    t = _in_review(board)
    board.update(ANNA, t.id, reviewer=None)
    with pytest.raises(ToolError, match="Only the reviewer of task #1 \\(nobody\\) or the user"):
        board.review(CARL, t.id, True)
    assert board.review(USER, t.id, True).status == Status.DONE


def test_new_reviewer_of_task_in_review_is_notified(swarm, board):
    t = board.create(ANNA, "t", reviewer=CARL)
    clear(swarm)
    board.update(ANNA, t.id, reviewer=DINA)  # not in review yet: nothing to do for Dina
    assert swarm.notified == []
    board.claim(BOB, t.id)
    board.complete(BOB, t.id, "res")
    clear(swarm)
    board.update(ANNA, t.id, reviewer=CARL)
    assert swarm.notifications_for(CARL) == [("n_review_requested", {"id": t.id, "title": "t", "by": ANNA})]
    assert swarm.notifications_for(DINA) == []
    clear(swarm)
    board.update(ANNA, t.id, reviewer=USER)
    assert swarm.notified == []  # the user sees reviews in the UI


def test_user_reopen_to_open_status_wakes_assignee(swarm, board):
    t = board.create(ANNA, "t")
    board.claim(BOB, t.id)
    board.complete(BOB, t.id, "res")
    clear(swarm)
    upd = board.update(USER, t.id, status="in_progress")
    assert upd.status == Status.IN_PROGRESS and upd.assignee == BOB
    assert swarm.notifications_for(BOB) == [("n_assigned", {"id": t.id, "title": "t", "by": USER})]


def test_mention_of_retired_member_does_not_hit_shorter_name(swarm, board):
    swarm.add_member("Old")
    swarm.add_member("Old Timer", status=AgentStatus.RETIRED)
    t = board.create(ANNA, "t")
    clear(swarm)
    board.post(BOB, t.id, "@Old Timer are you there?")
    assert swarm.notifications_for("Old") == [] and swarm.notifications_for("Old Timer") == []
    board.post(BOB, t.id, "@Old then")
    assert keys_for(swarm, "Old") == ["n_mention"]


def test_find_mentions_when_lowercasing_disagrees_with_case_insensitive_match():
    assert find_mentions("@σοφοσ hi", ["Σοφος"]) == ["Σοφος"]           # final sigma
    assert find_mentions("@ilker ak hi", ["İlker Ak"]) == ["İlker Ak"]   # dotted capital I
    assert find_mentions("@İlker Ak hi", ["Ilker Ak"]) == ["Ilker Ak"]


def test_malformed_tags_and_dependencies_are_tool_errors(swarm, board):
    with pytest.raises(ToolError, match="tags must be a list of strings"):
        board.create(ANNA, "t", tags=5)
    with pytest.raises(ToolError, match="depends_on entry .* must be an integer, got 5.5"):
        board.create(ANNA, "t", depends_on=5.5)
    a = board.create(ANNA, "a")
    b = board.create(ANNA, "b")
    assert board.create(ANNA, "c", depends_on="#1, 2").depends_on == [a.id, b.id]
    with pytest.raises(ToolError, match="tags must be a list"):
        board.update(ANNA, a.id, tags={"x": 1})


def test_creator_changing_status_of_assigned_task_gets_actionable_hint(swarm, board):
    t = board.create(ANNA, "t", assignee=BOB)
    with pytest.raises(ToolError, match="As its creator you may reassign it .*'' to unassign.* or cancel it"):
        board.update(ANNA, t.id, status="todo")


# --- notification texts render in both languages -------------------------------------------------------

def test_all_notifications_render_in_both_languages(swarm, board):
    t = board.create(ANNA, "t", assignee=BOB, reviewer=CARL)
    dep = board.create(ANNA, "dep", depends_on=[t.id], assignee=DINA)
    board.update(ANNA, t.id, assignee=CARL)
    board.update(CARL, t.id, assignee=BOB)
    board.claim(BOB, t.id)
    first = board.post(BOB, t.id, "@Carl Clark look")
    board.post(CARL, t.id, "ok", reply_to=first["id"])
    board.complete(BOB, t.id, "res")
    board.review(CARL, t.id, False, "redo")
    board.complete(BOB, t.id, "res2")
    board.review(CARL, t.id, True, "nice")
    board.update(ANNA, dep.id, status="cancelled")
    kinds = {k for _, k, _ in swarm.notified}
    assert {"n_assigned", "n_unassigned", "n_mention", "n_discussion", "n_review_requested",
            "n_review_rejected", "n_review_approved", "n_unblocked", "n_task_closed"} <= kinds
    for _, key, kw in swarm.notified:
        for lang in ("en", "ru"):
            assert prompts.t(lang, key, **kw)
