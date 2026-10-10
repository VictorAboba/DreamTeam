from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from dreamteam import prompts
from dreamteam.db import dumps
from dreamteam.models import USER, AgentStatus, ToolError
from dreamteam.triggers import (
    BURST_FIRES, BURST_SECONDS, EVAL_ERROR_PREFIX, FUNCTIONS, GRAMMAR, TriggerEngine, _Env,
)

ANNA = "Anna Smith"
BORIS = "Борис Иванов"


# --- helpers -----------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = time.time()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeBoard:
    def __init__(self, swarm: Any):
        self.swarm = swarm
        self.calls: list[tuple] = []
        self.fail: Exception | None = None
        self.on_update = None

    def update(self, actor: str, task_id: int, **kw: Any) -> dict:
        self.calls.append(("update", actor, task_id, kw))
        if self.fail:
            raise self.fail
        if self.on_update:
            self.on_update()
        set_status(self.swarm, task_id, kw["status"])
        self.swarm.bus.emit("task.updated", actor, task={"id": task_id}, changes={})
        return {"id": task_id}

    def create(self, actor: str, title: str, **kw: Any) -> dict:
        self.calls.append(("create", actor, title, kw))
        if self.fail:
            raise self.fail
        task_id = add_task(self.swarm, title)
        self.swarm.bus.emit("task.created", actor, task={"id": task_id})
        return {"id": task_id}


class FakeMail:
    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.fail: Exception | None = None

    def send(self, sender: str, to: Any, text: str, reply_to: int | None = None) -> dict:
        self.sent.append((sender, to, text))
        if self.fail:
            raise self.fail
        return {"id": len(self.sent)}


def add_task(swarm: Any, title: str = "Task", status: str = "todo", assignee: str | None = None,
             tags: tuple[str, ...] = (), parent_id: int | None = None) -> int:
    now = time.time()
    return swarm.db.insert(
        "INSERT INTO tasks(title, status, assignee, created_by, parent_id, tags, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (title, status, assignee, USER, parent_id, dumps(list(tags)), now, now),
    )


def set_status(swarm: Any, task_id: int, status: str) -> None:
    swarm.db.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def add_message(swarm: Any, sender: str, recipients: list[str], text: str, ts: float) -> int:
    mid = swarm.db.insert("INSERT INTO messages(ts, sender, recipients, text) VALUES (?, ?, ?, ?)",
                          (ts, sender, dumps(recipients), text))
    for r in recipients:
        swarm.db.execute("INSERT INTO inbox(message_id, recipient) VALUES (?, ?)", (mid, r))
    return mid


def add_comment(swarm: Any, task_id: int, author: str, text: str, ts: float) -> int:
    return swarm.db.insert("INSERT INTO comments(task_id, author, text, ts) VALUES (?, ?, ?, ?)",
                           (task_id, author, text, ts))


def events(swarm: Any, type_: str) -> list[dict]:
    return swarm.bus.recent(types=(type_,), limit=1000)


@pytest.fixture
def swarm(fake_swarm):
    fake_swarm.add_member(ANNA, language="en")
    fake_swarm.add_member(BORIS, language="ru")
    fake_swarm.board = FakeBoard(fake_swarm)
    fake_swarm.mail = FakeMail()
    fake_swarm.triggers = TriggerEngine(fake_swarm)
    fake_swarm.triggers.clock = Clock()
    return fake_swarm


@pytest.fixture
def engine(swarm) -> TriggerEngine:
    return swarm.triggers


@pytest.fixture
def clock(engine) -> Clock:
    return engine.clock


def ev(engine: TriggerEngine, condition: str, owner: str = USER, created_at: float | None = None,
       reference: float | None = None) -> Any:
    now = engine.clock()
    created = now if created_at is None else created_at
    env = _Env(engine.swarm, owner, created, created if reference is None else reference, now)
    return engine.compile(condition).evaluate(env)


# --- validation --------------------------------------------------------------------------------


@pytest.mark.parametrize("condition", [
    "done(1)",
    "done(1, 2) and status(3) in (failed, cancelled)",
    "status(3) == done or status(3) == 'review'",
    "not any_done(1, 2)",
    "finished(1, 2, 3) or failed(4)",
    "assignee(1) == None",
    "assignee(id=1) != 'Anna Smith'",
    "count() > 3",
    "count(status=(todo, in_progress), tag='api') >= 2",
    "count('todo') == 0",
    "count(status=[todo, waiting]) == 0",
    "count(status={todo}) == 0",
    "count(assignee='Anna Smith', parent=4) < 1",
    "all_done(tag='backend')",
    "all_done(parent=1)",
    "message()",
    "message(sender='Anna Smith', contains='ready')",
    "comment(task=3, sender='user', contains='go')",
    "file_exists('report.md') and file_changed('src/app.py')",
    "after('2026-10-09 18:00')",
    "after('2026-10-09')",
    "after('2026-10-09T18:00:00+03:00')",
    "elapsed(3600)",
    "elapsed(0.5)",
    "every(600)",
    "every(10) and not goal_done()",
    "agent_status('Anna Smith') in ('idle', 'error')",
    "goal_done()",
    "1 < count() <= 5",
    "count() > -1",
    "status(1) not in (done, failed)",
    "(done(1) or done(2)) and not failed(3) and True",
    "status(1) == None or False",
])
def test_validate_accepts(engine, condition):
    engine.validate(condition)


@pytest.mark.parametrize("condition, fragment", [
    ("", "empty"),
    ("   ", "empty"),
    ("done(1) &&", "syntax error"),
    ("done(1) && done(2)", "instead of &&"),
    ("x.y", "attribute access"),
    ("status(1).upper()", "only the listed functions"),
    ("done(1) and tasks[1]", "indexing"),
    ("(lambda: 1)()", "only the listed functions"),
    ("done(1) or (lambda: True)", "lambda"),
    ("done(1) or [x for x in (1, 2)]", "comprehensions"),
    ("done(1) or {x for x in (1, 2)}", "comprehensions"),
    ("done(1) or any(x for x in (1, 2))", "unknown function 'any'"),
    ("done(1) or {1: 2}", "dict literals"),
    ("status(1) == f'{done}'", "f-strings"),
    ("done(*[1, 2])", "starred"),
    ("count(**{'tag': 'x'})", "**kwargs"),
    ("done(1) or (x := 1)", "assignment"),
    ("elapsed(60 * 60)", "arithmetic"),
    ("done(1) if done(2) else done(3)", "conditional"),
    ("status(1) is None", "operator 'is'"),
    ("status(1) is not None", "operator 'is not'"),
    ("done(1) and foo", "unknown name 'foo'"),
    ("status(1) == true", "unknown name 'true'"),
    ("__import__('os')", "unknown function '__import__'"),
    ("eval('1')", "unknown function 'eval'"),
    ("open('x')", "unknown function 'open'"),
    ("is_done(1)", "did you mean done?"),
    ("done", "calls no function"),
    ("True", "calls no function"),
    ("status()", "missing the argument 'id'"),
    ("status(1, 2)", "at most 1 argument"),
    ("goal_done(1)", "at most 0 argument"),
    ("count(state='todo')", "unexpected keyword 'state'"),
    ("count(status=todo, status=done)", "multiple values for 'status'"),
    ("count(todo, status=done)", "multiple values for 'status'"),
    ("done()", "at least one task id"),
    ("done(ids=1)", "only task ids"),
    ("done('1')", "task id"),
    ("done(0)", "task id"),
    ("done(True)", "task id"),
    ("status(1.5)", "task id"),
    ("status(-1)", "task id"),
    ("status(count())", "must be literals"),
    ("done(1, status(2))", "must be literals"),
    ("count(status='doen')", "unknown status 'doen'"),
    ("count(status=())", "tuple of statuses"),
    ("count(status=1)", "tuple of statuses"),
    ("count(tag='')", "non-empty"),
    ("count(tag=1)", "non-empty"),
    ("comment(task='x')", "task id"),
    ("message(sender=5)", "non-empty"),
    ("agent_status(None)", "non-empty"),
    ("after('tomorrow')", "not an ISO date"),
    ("after(1700000000)", "ISO date"),
    ("elapsed(-1)", "non-negative"),
    ("elapsed('60')", "non-negative"),
    ("every(5)", ">= 10"),
    ("every(True)", ">= 10"),
    ("file_exists('../secret.txt')", "inside the workspace"),
    ("file_changed('.git/config')", "inside the workspace"),
    ("file_exists('')", "workspace path"),
    ("status(1) in ((1, 2), 3)", "only literals"),
    ("status(1) in (done(1), 2)", "only literals"),
    ("count() > -count()", "unary +/-"),
    ("count() > ~1", "'~'"),
    ("status(1) == b'x'", "not supported"),
    ("count() > 1j", "not supported"),
    # regression: Windows-style paths turn into control characters (\r \n \t) and never match
    ("file_exists('docs\\report.md')", "forward slashes"),
    ("file_changed('src\\new.py')", "control character"),
    ("file_changed('logs\\today.txt')", "control character"),
    # regression: ids beyond SQLite INTEGER passed validation and then failed on every evaluation
    ("status(9223372036854775808)", "task id"),
    ("done(1, 99999999999999999999)", "task id"),
])
def test_validate_rejects(engine, condition, fragment):
    with pytest.raises(ToolError) as err:
        engine.validate(condition)
    assert fragment in str(err.value)


def test_validate_error_teaches_function_list_and_example(engine):
    with pytest.raises(ToolError) as err:
        engine.validate("status(1) === 2")
    text = str(err.value)
    assert text.startswith("Invalid condition")
    assert GRAMMAR in text
    for name in FUNCTIONS:
        assert f"{name}(" in text
    assert "Example: done(1, 2)" in text


def test_validate_limits(engine):
    too_long = "done(1) or " * 45 + "done(1)"
    assert len(too_long) > 500
    with pytest.raises(ToolError, match="limit is 500"):
        engine.validate(too_long)
    many_nodes = "done(" + ",".join(["1"] * 240) + ")"
    assert len(many_nodes) <= 500
    with pytest.raises(ToolError, match="limit is 200"):
        engine.validate(many_nodes)
    with pytest.raises(ToolError, match="empty"):
        engine.validate(None)  # type: ignore[arg-type]
    # regression: a non-string condition (e.g. a number from an MCP client) was reported as "empty"
    with pytest.raises(ToolError, match="must be a string expression, got int"):
        engine.validate(3)  # type: ignore[arg-type]


def test_validate_never_executes_code(engine, monkeypatch):
    import builtins

    def boom(*a, **k):
        raise AssertionError("eval/exec must not be used")

    monkeypatch.setattr(builtins, "eval", boom)
    monkeypatch.setattr(builtins, "exec", boom)
    engine.validate("done(1) and count(status=(todo, review)) > 2")
    assert ev(engine, "count() == 0") is True


def test_after_out_of_range_date_is_a_tool_error(engine, monkeypatch):
    # regression: on Windows a naive datetime before 1970 / after 3000 raises OSError in timestamp();
    # validate() leaked it as a raw OSError instead of a ToolError that tells the caller what to fix
    import dreamteam.triggers as triggers_module

    real = triggers_module.datetime

    class Moment:
        def timestamp(self) -> float:
            raise OSError(22, "Invalid argument")

    class WindowsLikeDatetime:
        @staticmethod
        def fromisoformat(text: str) -> Moment:
            real.fromisoformat(text)
            return Moment()

    triggers_module.parse_when.cache_clear()
    monkeypatch.setattr(triggers_module, "datetime", WindowsLikeDatetime)
    try:
        with pytest.raises(ToolError) as err:
            engine.validate("after('1969-12-31')")
        assert "out of the supported date range" in str(err.value) and GRAMMAR in str(err.value)
        with pytest.raises(ToolError, match="not an ISO date"):
            engine.validate("after('tomorrow')")
    finally:
        triggers_module.parse_when.cache_clear()


@pytest.mark.skipif(sys.platform != "win32", reason="naive pre-1970 timestamps only fail on Windows")
@pytest.mark.parametrize("when", ["1969-12-31", "1970-01-01", "9999-12-31 23:59"])
def test_after_out_of_range_on_windows(engine, when):
    with pytest.raises(ToolError, match="out of the supported date range"):
        engine.validate(f"after({when!r})")


def test_validate_absolute_path_inside_workspace_ok(engine, swarm):
    inside = swarm.config.workspace_dir / "notes.md"
    engine.validate(f"file_exists({str(inside)!r})")


# --- functions ---------------------------------------------------------------------------------


def test_status_and_task_sets(engine, swarm):
    a = add_task(swarm, status="done")
    b = add_task(swarm, status="failed")
    c = add_task(swarm, status="cancelled")
    d = add_task(swarm, status="in_progress")
    assert ev(engine, f"status({a})") == "done"
    assert ev(engine, "status(999)") == "missing"
    assert ev(engine, f"status({d}) == in_progress") is True
    assert ev(engine, f"done({a})") is True
    assert ev(engine, f"done({a}, {d})") is False
    assert ev(engine, f"done({a}, 999)") is False
    assert ev(engine, f"any_done({d}, {a})") is True
    assert ev(engine, f"any_done({d}, 999)") is False
    assert ev(engine, f"finished({a}, {b}, {c})") is True
    assert ev(engine, f"finished({a}, {d})") is False
    assert ev(engine, f"finished({a}, 999)") is False
    assert ev(engine, f"failed({a}, {b})") is True
    assert ev(engine, f"failed({a}, {c})") is False
    assert ev(engine, f"status({b}) in (failed, cancelled)") is True
    assert ev(engine, f"status({d}) not in (failed, cancelled)") is True


def test_assignee(engine, swarm):
    a = add_task(swarm, assignee=ANNA)
    b = add_task(swarm)
    assert ev(engine, f"assignee({a})") == ANNA
    assert ev(engine, f"assignee({b}) == None") is True
    assert ev(engine, "assignee(999)") is None


def test_count_and_all_done(engine, swarm):
    parent = add_task(swarm, "Epic", status="in_progress")
    add_task(swarm, status="todo", tags=("API",), parent_id=parent)
    add_task(swarm, status="done", tags=("api", "x"), assignee=ANNA, parent_id=parent)
    add_task(swarm, status="in_progress", assignee=BORIS)
    assert ev(engine, "count()") == 4
    assert ev(engine, "count(todo)") == 1
    assert ev(engine, "count(status=(todo, in_progress))") == 3
    assert ev(engine, "count(tag='api')") == 2
    assert ev(engine, "count(tag='API', status=done)") == 1
    assert ev(engine, "count(assignee='anna  smith')") == 1
    assert ev(engine, "count(assignee='борис иванов')") == 1
    assert ev(engine, f"count(parent={parent})") == 2
    assert ev(engine, "count(parent=999)") == 0
    assert ev(engine, f"all_done(parent={parent})") is False
    assert ev(engine, "all_done(tag='x')") is True
    assert ev(engine, "all_done(tag='nothing')") is False
    assert ev(engine, "all_done()") is False
    for row in swarm.db.all("SELECT id FROM tasks"):
        set_status(swarm, row["id"], "done")
    assert ev(engine, "all_done()") is True
    assert ev(engine, f"all_done(tag='api', parent={parent})") is True


def test_all_done_empty_board_is_false(engine):
    assert ev(engine, "all_done()") is False


def test_message(engine, swarm, clock):
    created = clock.now
    add_message(swarm, ANNA, [BORIS], "old news READY", created - 5)
    assert ev(engine, "message()", owner=BORIS, created_at=created) is False
    add_message(swarm, ANNA, [USER], "for the user", created + 1)
    assert ev(engine, "message()", owner=BORIS, created_at=created) is False
    assert ev(engine, "message(sender='anna smith')", owner=USER, created_at=created) is True
    add_message(swarm, ANNA, [BORIS], "Build is READY now", created + 2)
    assert ev(engine, "message()", owner=BORIS, created_at=created) is True
    assert ev(engine, "message(sender='ANNA SMITH')", owner=BORIS, created_at=created) is True
    assert ev(engine, "message(sender='Someone Else')", owner=BORIS, created_at=created) is False
    assert ev(engine, "message(contains='ready')", owner=BORIS, created_at=created) is True
    assert ev(engine, "message(contains='failed')", owner=BORIS, created_at=created) is False
    assert ev(engine, "message(sender='Anna Smith', contains='build')", owner=BORIS, created_at=created) is True
    # messages before the reference (last fire) do not count
    assert ev(engine, "message()", owner=BORIS, created_at=created, reference=created + 2) is False


def test_message_cyrillic_and_user_alias(engine, swarm, clock):
    created = clock.now
    add_message(swarm, BORIS, [ANNA], "ГОТОВО к проверке", created + 1)
    add_message(swarm, USER, [ANNA], "hello", created + 2)
    assert ev(engine, "message(sender='борис иванов')", owner=ANNA, created_at=created) is True
    assert ev(engine, "message(contains='готово')", owner=ANNA, created_at=created) is True
    assert ev(engine, "message(sender='User')", owner=ANNA, created_at=created) is True
    assert ev(engine, "message(sender='пользователь')", owner=ANNA, created_at=created) is True


def test_comment(engine, swarm, clock):
    created = clock.now
    t1 = add_task(swarm)
    t2 = add_task(swarm)
    add_comment(swarm, t1, ANNA, "early", created - 1)
    assert ev(engine, "comment()", owner=BORIS, created_at=created) is False
    add_comment(swarm, t1, BORIS, "my own post", created + 1)
    assert ev(engine, "comment()", owner=BORIS, created_at=created) is False
    assert ev(engine, "comment()", owner=ANNA, created_at=created) is True
    add_comment(swarm, t2, ANNA, "Please REVIEW", created + 2)
    assert ev(engine, "comment()", owner=BORIS, created_at=created) is True
    assert ev(engine, f"comment(task={t1})", owner=BORIS, created_at=created) is False
    assert ev(engine, f"comment(task={t2})", owner=BORIS, created_at=created) is True
    assert ev(engine, f"comment({t2}, 'anna smith', 'review')", owner=BORIS, created_at=created) is True
    assert ev(engine, "comment(sender='user')", owner=BORIS, created_at=created) is False
    assert ev(engine, "comment(contains='nope')", owner=BORIS, created_at=created) is False
    assert ev(engine, "comment()", owner=BORIS, created_at=created, reference=created + 3) is False
    add_comment(swarm, t2, USER, "go", created + 4)
    assert ev(engine, "comment(sender='USER', contains='GO')", owner=BORIS, created_at=created) is True


def test_file_functions(engine, swarm, clock):
    root = swarm.config.workspace_dir
    created = clock.now
    assert ev(engine, "file_exists('docs/report.md')") is False
    assert ev(engine, "file_changed('docs/report.md')", created_at=created) is False
    target = root / "docs" / "report.md"
    target.parent.mkdir(parents=True)
    target.write_text("x", encoding="utf-8")
    os.utime(target, (created - 10, created - 10))
    assert ev(engine, "file_exists('docs/report.md')") is True
    assert ev(engine, "file_exists('docs')") is True
    assert ev(engine, "file_changed('docs/report.md')", created_at=created) is False
    os.utime(target, (created + 10, created + 10))
    assert ev(engine, "file_changed('docs/report.md')", created_at=created) is True
    assert ev(engine, "file_changed('docs/report.md')", created_at=created, reference=created + 20) is False
    assert ev(engine, f"file_exists({str(target)!r})") is True


def test_file_functions_unsafe_paths_are_false(engine, swarm, clock):
    outside = swarm.config.project_dir / "secret.txt"
    outside.write_text("s", encoding="utf-8")
    git_file = swarm.config.workspace_dir / ".git" / "config"
    git_file.parent.mkdir()
    git_file.write_text("g", encoding="utf-8")
    env = _Env(swarm, USER, clock.now - 100, clock.now - 100, clock.now)
    assert env.fn_file_exists("../secret.txt") is False
    assert env.fn_file_exists(str(outside)) is False
    assert env.fn_file_exists(".git/config") is False
    assert env.fn_file_changed("../secret.txt") is False
    assert env.fn_file_exists("bad\x00name") is False


def test_time_functions(engine, clock):
    past = datetime.fromtimestamp(clock.now - 3600).strftime("%Y-%m-%d %H:%M")
    future = datetime.fromtimestamp(clock.now + 3600).strftime("%Y-%m-%dT%H:%M:%S")
    assert ev(engine, f"after({past!r})") is True
    assert ev(engine, f"after({future!r})") is False
    utc_past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    assert ev(engine, f"after({utc_past!r})") is True
    assert ev(engine, "after('2000-01-01')") is True
    assert ev(engine, "after('2999-01-01')") is False
    created = clock.now
    assert ev(engine, "elapsed(60)", created_at=created) is False
    clock.advance(61)
    assert ev(engine, "elapsed(60)", created_at=created) is True
    assert ev(engine, "every(60)", created_at=created, reference=created) is True
    assert ev(engine, "every(60)", created_at=created, reference=clock.now - 30) is False


def test_agent_status_and_goal(engine, swarm):
    swarm.db.execute("UPDATE agents SET status = 'working' WHERE name = ?", (ANNA,))
    assert ev(engine, "agent_status('anna smith')") == "working"
    assert ev(engine, "agent_status('БОРИС ИВАНОВ') == 'idle'") is True
    assert ev(engine, "agent_status('Nobody')") == "missing"
    assert ev(engine, "goal_done()") is False
    swarm.set_goal(USER, "Ship it")
    assert ev(engine, "goal_done()") is False
    swarm.complete_goal(ANNA, "shipped")
    assert ev(engine, "goal_done()") is True


def test_boolean_logic_and_comparisons(engine, swarm):
    a = add_task(swarm, status="done")
    add_task(swarm, status="todo")
    assert ev(engine, f"done({a}) and count() == 2") is True
    assert ev(engine, f"not done({a}) or count(todo) == 1") is True
    assert ev(engine, "1 < count() < 3") is True
    assert ev(engine, "1 < count() < 2") is False
    assert ev(engine, "count() in [1, 2]") is True
    assert ev(engine, "count() in {5, 6}") is False
    assert ev(engine, "count() > -1") is True
    # short-circuit: the right side is not evaluated when the left decides
    assert ev(engine, "count() == 2 or status(1) > 5") is True
    assert ev(engine, "count() == 3 and status(1) > 5") is False


# --- create / list / delete --------------------------------------------------------------------


def test_create_returns_trigger_dict_and_emits(engine, swarm, clock):
    trig = engine.create(ANNA, "  done(1)  ", note="check the build")
    assert set(trig) == {"id", "owner", "condition", "note", "actions", "once", "active", "created_at",
                         "last_fired", "fire_count", "last_error"}
    assert trig["owner"] == ANNA and trig["condition"] == "done(1)" and trig["note"] == "check the build"
    assert trig["actions"] == [{"type": "wake"}]
    assert trig["once"] is True and trig["active"] is True
    assert trig["created_at"] == clock.now and trig["last_fired"] is None
    assert trig["fire_count"] == 0 and trig["last_error"] is None
    created = events(swarm, "trigger.created")
    assert len(created) == 1 and created[0]["actor"] == ANNA
    assert created[0]["data"]["trigger"]["id"] == trig["id"]
    assert engine._dirty.is_set()


def test_create_owner_resolution(engine):
    assert engine.create("anna  SMITH", "done(1)")["owner"] == ANNA
    assert engine.create(USER, "done(1)")["owner"] == USER
    with pytest.raises(ToolError, match="Unknown member"):
        engine.create("Ghost", "done(1)")


def test_create_rejects_retired_owner(engine, swarm):
    swarm.add_member("Old Timer", status=AgentStatus.RETIRED)
    with pytest.raises(ToolError):
        engine.create("Old Timer", "done(1)")


def test_create_invalid_inputs_store_nothing(engine, swarm):
    with pytest.raises(ToolError, match="Invalid condition"):
        engine.create(ANNA, "done(")
    with pytest.raises(ToolError, match="Invalid action"):
        engine.create(ANNA, "done(1)", actions=[{"type": "explode"}])
    with pytest.raises(ToolError, match="note is too long"):
        engine.create(ANNA, "done(1)", note="x" * 5000)
    assert swarm.db.scalar("SELECT COUNT(*) FROM triggers") == 0
    assert events(swarm, "trigger.created") == []


def test_create_with_every_is_periodic(engine):
    assert engine.create(ANNA, "every(600)")["once"] is False
    assert engine.create(ANNA, "every(600)", once=True)["once"] is False
    assert engine.create(ANNA, "done(1)", once=False)["once"] is False


def test_actions_are_normalized(engine):
    trig = engine.create(ANNA, "done(1)", actions=[
        {"type": "wake"},
        {"type": "message", "to": ["Борис Иванов", "user"], "text": "Done!"},
        {"type": "set_status", "task_id": "7", "status": "cancelled"},
        {"type": "create_task", "title": "Follow-up", "tags": ["qa"], "priority": 2, "assignee": ""},
    ])
    assert trig["actions"] == [
        {"type": "wake"},
        {"type": "message", "to": ["Борис Иванов", "user"], "text": "Done!"},
        {"type": "set_status", "task_id": 7, "status": "cancelled"},
        {"type": "create_task", "title": "Follow-up", "goal": "", "description": "", "assignee": None,
         "tags": ["qa"], "priority": 2},
    ]
    assert engine.create(ANNA, "done(1)", actions=[])["actions"] == [{"type": "wake"}]


@pytest.mark.parametrize("actions, fragment", [
    ("wake", "must be a list"),
    ({"type": "wake"}, "must be a list"),
    (["wake"], "must be an object"),
    ([{}], "unknown type None"),
    ([{"type": "explode"}], "unknown type 'explode'"),
    ([{"type": "wake", "text": "x"}], "unexpected key"),
    ([{"type": "message", "text": "hi"}], "'to'"),
    ([{"type": "message", "to": "", "text": "hi"}], "'to'"),
    ([{"type": "message", "to": [], "text": "hi"}], "'to'"),
    ([{"type": "message", "to": [ANNA, 3], "text": "hi"}], "'to'"),
    ([{"type": "message", "to": ANNA}], "'text'"),
    ([{"type": "message", "to": ANNA, "text": "  "}], "'text'"),
    ([{"type": "message", "to": ANNA, "text": "x", "cc": "y"}], "unexpected key"),
    ([{"type": "set_status", "status": "done"}], "'task_id'"),
    ([{"type": "set_status", "task_id": 0, "status": "done"}], "'task_id'"),
    ([{"type": "set_status", "task_id": True, "status": "done"}], "'task_id'"),
    ([{"type": "set_status", "task_id": 1, "status": "finished"}], "'status'"),
    ([{"type": "set_status", "task_id": 1}], "'status'"),
    ([{"type": "create_task"}], "'title'"),
    ([{"type": "create_task", "title": "x" * 201}], "longer than 200"),
    ([{"type": "create_task", "title": "T", "priority": 5}], "'priority'"),
    ([{"type": "create_task", "title": "T", "priority": "high"}], "'priority'"),
    ([{"type": "create_task", "title": "T", "tags": "qa"}], "'tags'"),
    ([{"type": "create_task", "title": "T", "goal": 3}], "'goal'"),
    ([{"type": "create_task", "title": "T", "assignee": 3}], "'assignee'"),
    ([{"type": "create_task", "title": "T", "reviewer": ANNA}], "unexpected key"),
    # regression: unhashable / odd values raised TypeError / ValueError instead of ToolError
    ([{"type": ["wake"]}], "unknown type ['wake']"),
    ([{"type": {"name": "wake"}}], "unknown type"),
    ([{"type": "set_status", "task_id": 1, "status": ["done"]}], "'status'"),
    ([{"type": "set_status", "task_id": "²", "status": "done"}], "'task_id'"),
    ([{"type": "set_status", "task_id": 2 ** 63, "status": "done"}], "'task_id'"),
])
def test_invalid_actions(engine, actions, fragment):
    with pytest.raises(ToolError) as err:
        engine.create(ANNA, "done(1)", actions=actions)
    assert fragment in str(err.value)
    assert '"type": "wake"' in str(err.value)  # the error teaches the action shapes


def test_list_and_include_inactive(engine, swarm):
    a = engine.create(ANNA, "done(1)")
    b = engine.create(BORIS, "done(2)")
    c = engine.create(ANNA, "done(3)")
    swarm.db.execute("UPDATE triggers SET active = 0 WHERE id = ?", (c["id"],))
    assert [t["id"] for t in engine.list()] == [a["id"], b["id"]]
    assert [t["id"] for t in engine.list(ANNA)] == [a["id"]]
    assert [t["id"] for t in engine.list("anna smith", include_inactive=True)] == [a["id"], c["id"]]
    assert [t["id"] for t in engine.list(include_inactive=True)] == [a["id"], b["id"], c["id"]]
    assert engine.list(USER) == []


def test_delete_permissions(engine, swarm):
    a = engine.create(ANNA, "done(1)")
    b = engine.create(ANNA, "done(2)")
    with pytest.raises(ToolError, match="only its owner or the user"):
        engine.delete(BORIS, a["id"])
    engine.delete(ANNA, a["id"])
    engine.delete(USER, b["id"])
    assert engine.list(include_inactive=True) == []
    with pytest.raises(ToolError, match="not found"):
        engine.delete(ANNA, a["id"])
    deleted = events(swarm, "trigger.deleted")
    assert [(e["actor"], e["data"]["trigger_id"]) for e in deleted] == [(ANNA, a["id"]), (USER, b["id"])]
    assert deleted[0]["data"]["trigger"]["condition"] == "done(1)"


def test_get_missing(engine):
    with pytest.raises(ToolError, match="#42 not found"):
        engine.get(42)


# --- firing semantics --------------------------------------------------------------------------


def test_once_trigger_fires_on_edge_and_deactivates(engine, swarm, clock):
    task = add_task(swarm)
    trig = engine.create(ANNA, f"done({task})", note="review it")
    assert engine.evaluate_all() == 0
    assert swarm.notified == []
    set_status(swarm, task, "done")
    clock.advance(5)
    assert engine.evaluate_all() == 1
    assert swarm.notified == [(ANNA, "n_trigger", {"id": trig["id"], "condition": f"done({task})",
                                                     "note": "review it"})]
    after = engine.get(trig["id"])
    assert after["active"] is False and after["fire_count"] == 1 and after["last_fired"] == clock.now
    assert engine.list(ANNA) == []
    assert engine.evaluate_all() == 0
    fired = events(swarm, "trigger.fired")
    assert len(fired) == 1 and fired[0]["actor"] == ANNA
    assert fired[0]["data"]["trigger"]["fire_count"] == 1 and fired[0]["data"]["errors"] == []


def test_true_at_creation_fires_on_first_evaluation(engine, swarm):
    task = add_task(swarm, status="done")
    engine.create(BORIS, f"done({task})")
    assert engine.evaluate_all() == 1


def test_repeating_trigger_fires_only_on_edges(engine, swarm, clock):
    task = add_task(swarm)
    trig = engine.create(ANNA, f"status({task}) == review", once=False)
    assert engine.evaluate_all() == 0
    set_status(swarm, task, "review")
    assert engine.evaluate_all() == 1
    assert engine.evaluate_all() == 0      # still true: no new edge
    assert engine.evaluate_all() == 0
    set_status(swarm, task, "in_progress")
    assert engine.evaluate_all() == 0
    set_status(swarm, task, "review")
    assert engine.evaluate_all() == 1
    state = engine.get(trig["id"])
    assert state["active"] is True and state["fire_count"] == 2
    assert swarm.db.scalar("SELECT last_value FROM triggers WHERE id = ?", (trig["id"],)) == 1


def test_last_value_persists_across_engines(engine, swarm):
    task = add_task(swarm, status="done")
    engine.create(ANNA, f"done({task})", once=False)
    assert engine.evaluate_all() == 1
    other = TriggerEngine(swarm)
    assert other.evaluate_all() == 0


def test_every_fires_periodically(engine, swarm, clock):
    trig = engine.create(ANNA, "every(60)")
    assert engine.evaluate_all() == 0
    clock.advance(59)
    assert engine.evaluate_all() == 0
    clock.advance(1)
    assert engine.evaluate_all() == 1
    assert engine.evaluate_all() == 0      # every() resets on fire
    clock.advance(30)
    assert engine.evaluate_all() == 0
    clock.advance(30)
    assert engine.evaluate_all() == 1
    clock.advance(600)
    assert engine.evaluate_all() == 1
    assert engine.get(trig["id"])["fire_count"] == 3
    assert engine.get(trig["id"])["active"] is True


def test_every_with_or_does_not_storm(engine, swarm, clock):
    task = add_task(swarm)
    engine.create(ANNA, f"done({task}) or every(60)")
    clock.advance(10)
    assert engine.evaluate_all() == 0
    set_status(swarm, task, "done")
    assert engine.evaluate_all() == 1      # edge
    for _ in range(5):
        assert engine.evaluate_all() == 0  # true but not due yet
    clock.advance(60)
    assert engine.evaluate_all() == 1      # period elapsed while still true
    assert engine.evaluate_all() == 0


def test_every_with_and_fires_whenever_true(engine, swarm, clock):
    task = add_task(swarm, status="in_progress")
    engine.create(ANNA, f"status({task}) == in_progress and every(30)")
    assert engine.evaluate_all() == 0
    clock.advance(30)
    assert engine.evaluate_all() == 1
    clock.advance(30)
    assert engine.evaluate_all() == 1
    set_status(swarm, task, "done")
    clock.advance(30)
    assert engine.evaluate_all() == 0


def test_message_trigger_resets_after_fire(engine, swarm, clock):
    trig = engine.create(BORIS, f"message(sender={ANNA!r})", once=False)
    assert engine.evaluate_all() == 0
    clock.advance(1)
    add_message(swarm, ANNA, [BORIS], "ping", clock.now)
    clock.advance(1)
    assert engine.evaluate_all() == 1
    assert engine.evaluate_all() == 0     # the old message is before last_fired
    clock.advance(1)
    add_message(swarm, ANNA, [BORIS], "ping again", clock.now)
    clock.advance(1)
    assert engine.evaluate_all() == 1
    assert engine.get(trig["id"])["fire_count"] == 2


def test_once_message_trigger_uses_creation_time(engine, swarm, clock):
    add_message(swarm, ANNA, [BORIS], "before", clock.now - 1)
    engine.create(BORIS, "message()")
    assert engine.evaluate_all() == 0
    clock.advance(1)
    add_message(swarm, ANNA, [BORIS], "after", clock.now)
    assert engine.evaluate_all() == 1


def _write(path, text: str, mtime: float) -> None:
    path.write_text(text, encoding="utf-8")
    ns = int(mtime * 1e9)
    os.utime(path, ns=(ns, ns))


# On Windows (Python < 3.13) time.time() and file mtimes advance in 15.6 ms steps, so a message, a
# post or a file write right after the trigger's creation / last fire often carries exactly the same
# timestamp. The fake clock makes that tie deterministic.


def test_message_in_same_clock_tick_as_creation_is_seen(engine, swarm, clock):
    add_message(swarm, ANNA, [BORIS], "sent just before", clock.now)
    trig = engine.create(BORIS, f"message(sender={ANNA!r})")
    assert engine.evaluate_all() == 0          # same timestamp, but it arrived before the trigger
    add_message(swarm, ANNA, [BORIS], "sent just after", clock.now)
    assert engine.evaluate_all() == 1
    assert engine.get(trig["id"])["fire_count"] == 1


def test_comment_in_same_clock_tick_as_creation_is_seen(engine, swarm, clock):
    task = add_task(swarm)
    add_comment(swarm, task, ANNA, "before", clock.now)
    engine.create(BORIS, f"comment(task={task})")
    assert engine.evaluate_all() == 0
    add_comment(swarm, task, ANNA, "after", clock.now)
    assert engine.evaluate_all() == 1


def test_file_written_in_same_clock_tick_as_creation_is_seen(engine, swarm, clock):
    root = swarm.config.workspace_dir
    _write(root / "old.txt", "v1", clock.now - 100)
    _write(root / "same.txt", "s", clock.now)              # written in the creation tick, before it
    new = engine.create(ANNA, "file_changed('new.txt')")
    old = engine.create(ANNA, "file_changed('old.txt')")
    same = engine.create(ANNA, "file_changed('same.txt')")
    assert engine.evaluate_all() == 0
    _write(root / "new.txt", "created", clock.now)         # same tick as the creation
    _write(root / "old.txt", "v2", clock.now)
    assert engine.evaluate_all() == 2
    assert engine.get(new["id"])["fire_count"] == engine.get(old["id"])["fire_count"] == 1
    assert engine.get(same["id"])["fire_count"] == 0


def test_message_from_another_trigger_in_the_same_pass_is_seen(engine, swarm, clock):
    swarm.mail = InsertingMail(swarm, clock)
    task = add_task(swarm)
    listener = engine.create(BORIS, f"message(sender={ANNA!r})", once=False)
    engine.create(ANNA, f"done({task})", actions=[{"type": "message", "to": BORIS, "text": "task done"}])
    clock.advance(1)
    add_message(swarm, ANNA, [BORIS], "first", clock.now)
    set_status(swarm, task, "done")
    clock.advance(1)
    # Boris's trigger fires at T, then Anna's trigger sends him a message stamped T in the same pass
    assert engine.evaluate_all() == 2
    assert engine.evaluate_all() == 1
    assert engine.get(listener["id"])["fire_count"] == 2


@pytest.mark.parametrize("kind", ["message", "comment", "file"])
def test_repeating_trigger_does_not_get_stuck_after_a_fire(engine, swarm, clock, kind):
    # regression: after a fire last_value stayed 1, so an event arriving before the next pass made the
    # condition true without an edge - and it stayed true (and silent) forever
    root = swarm.config.workspace_dir
    task = add_task(swarm)
    condition = {"message": "message()", "comment": f"comment(task={task})", "file": "file_changed('log.txt')"}
    trig = engine.create(BORIS, condition[kind], once=False)
    assert engine.evaluate_all() == 0

    def happen(i: int) -> None:
        clock.advance(1)
        if kind == "message":
            add_message(swarm, ANNA, [BORIS], f"ping {i}", clock.now)
        elif kind == "comment":
            add_comment(swarm, task, ANNA, f"post {i}", clock.now)
        else:
            _write(root / "log.txt", "x" * (i + 1), clock.now)
        clock.advance(1)

    for i in range(4):
        happen(i)                              # no evaluation pass in between the events
        assert engine.evaluate_all() == 1, f"event {i} was swallowed"
    assert engine.evaluate_all() == 0          # nothing new: no re-fire
    assert engine.get(trig["id"])["fire_count"] == 4


def test_rearm_keeps_edge_semantics_for_conditions_still_true(engine, swarm, clock):
    task = add_task(swarm, status="done")
    trig = engine.create(BORIS, f"message() or done({task})", once=False)
    assert engine.evaluate_all() == 1          # true at creation
    clock.advance(1)
    add_message(swarm, ANNA, [BORIS], "hello", clock.now)
    assert engine.evaluate_all() == 0          # the condition never became false: no new edge
    assert engine.evaluate_all() == 0
    assert engine.get(trig["id"])["fire_count"] == 1


class InsertingMail:
    """Mail that really stores the message (so message() triggers see it), stamped by the fake clock."""

    def __init__(self, swarm: Any, clock: Clock) -> None:
        self.swarm, self.clock, self.sent = swarm, clock, 0

    def send(self, sender: str, to: Any, text: str, reply_to: int | None = None) -> dict:
        self.sent += 1
        recipients = [to] if isinstance(to, str) else list(to)
        return {"id": add_message(self.swarm, sender, recipients, text, self.clock.now)}


def test_message_ping_pong_between_repeating_triggers_is_stopped(engine, swarm, clock):
    # two repeating message() triggers answering each other would fire on every pass forever
    swarm.mail = InsertingMail(swarm, clock)
    a = engine.create(ANNA, f"message(sender={BORIS!r})", once=False,
                      actions=[{"type": "message", "to": BORIS, "text": "ack"}])
    b = engine.create(BORIS, f"message(sender={ANNA!r})", once=False,
                      actions=[{"type": "message", "to": ANNA, "text": "ack"}])
    add_message(swarm, BORIS, [ANNA], "hello", clock.now)
    fired = 0
    for _ in range(200):
        n = engine.evaluate_all()
        fired += n
        if not n:
            break
    assert fired <= 2 * BURST_FIRES and swarm.mail.sent < 2 * BURST_FIRES
    assert engine.evaluate_all() == 0                     # the loop is over
    first, second = engine.get(a["id"]), engine.get(b["id"])
    # the first one to reach the limit is deactivated without running its action; that ends the loop
    assert first["active"] is False and "deactivated" in first["last_error"]
    assert first["fire_count"] == BURST_FIRES
    assert second["active"] is True and second["last_error"] is None
    assert swarm.notified == [(ANNA, "n_trigger", {"id": a["id"], "condition": a["condition"],
                                                    "note": swarm.notified[0][2]["note"]})]
    assert "looks like a loop" in swarm.notified[0][2]["note"]
    assert a["id"] not in engine._fires and a["id"] not in engine._marks


@pytest.mark.parametrize("actions, spacing", [
    (None, 0.0),                                                       # wake-only: cannot feed a loop
    ([{"type": "message", "to": ANNA, "text": "seen"}], BURST_SECONDS / (BURST_FIRES - 2)),  # slower than the limit
])
def test_frequent_but_not_runaway_repeating_trigger_stays_active(engine, swarm, clock, actions, spacing):
    trig = engine.create(BORIS, "message()", once=False, actions=actions)
    for i in range(BURST_FIRES + 5):
        clock.advance(spacing)
        add_message(swarm, ANNA, [BORIS], f"m{i}", clock.now)
        assert engine.evaluate_all() == 1
    state = engine.get(trig["id"])
    assert state["active"] is True and state["last_error"] is None
    assert state["fire_count"] == BURST_FIRES + 5


def test_marks_are_dropped_with_the_trigger(engine, swarm, clock):
    a = engine.create(BORIS, "message()", once=False)
    b = engine.create(BORIS, "message()")
    assert set(engine._marks) == {a["id"], b["id"]}
    engine.delete(BORIS, a["id"])
    assert set(engine._marks) == {b["id"]}
    add_message(swarm, ANNA, [BORIS], "x", clock.now)
    assert engine.evaluate_all() == 1          # the once trigger fires and is deactivated
    assert engine._marks == {}
    engine.create(ANNA, "done(1)")
    engine.create(ANNA, "done(2)")                 # active triggers without marks
    c = engine.create(BORIS, "message()", once=False)
    swarm.db.execute("UPDATE triggers SET active = 0 WHERE id = ?", (c["id"],))  # e.g. Swarm.retire
    engine.evaluate_all()
    assert engine._marks == {}


def test_evaluation_error_is_stored_and_not_fired(engine, swarm):
    task = add_task(swarm, status="todo")
    trig = engine.create(ANNA, f"status({task}) > 5")
    assert engine.evaluate_all() == 0
    state = engine.get(trig["id"])
    assert state["last_error"].startswith(EVAL_ERROR_PREFIX) and "TypeError" in state["last_error"]
    assert state["active"] is True and state["fire_count"] == 0
    assert swarm.notified == []
    assert engine.evaluate_all() == 0     # same error again: still stored, still no fire
    assert engine.get(trig["id"])["last_error"] == state["last_error"]


def test_evaluation_error_clears_when_condition_evaluates(engine, swarm):
    task = add_task(swarm)
    trig = engine.create(ANNA, f"assignee({task}) in 'Anna Smith'", once=False)
    assert engine.evaluate_all() == 0
    assert engine.get(trig["id"])["last_error"].startswith(EVAL_ERROR_PREFIX)
    swarm.db.execute("UPDATE tasks SET assignee = 'Bob' WHERE id = ?", (task,))
    assert engine.evaluate_all() == 0
    assert engine.get(trig["id"])["last_error"] is None
    swarm.db.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (ANNA, task))
    assert engine.evaluate_all() == 1
    assert engine.get(trig["id"])["last_error"] is None


def test_corrupted_stored_condition_is_an_evaluation_error(engine, swarm):
    trig = engine.create(ANNA, "done(1)")
    swarm.db.execute("UPDATE triggers SET condition = 'done(1) and os.sep' WHERE id = ?", (trig["id"],))
    assert engine.evaluate_all() == 0
    assert "attribute access" in engine.get(trig["id"])["last_error"]


def test_triggers_of_retired_owners_are_skipped(engine, swarm):
    engine.create(ANNA, "count() == 0")
    swarm.db.execute("UPDATE agents SET status = 'retired' WHERE name = ?", (ANNA,))
    assert engine.evaluate_all() == 0
    assert swarm.notified == []


def test_user_owned_trigger_notifies_user(engine, swarm):
    trig = engine.create(USER, "count() == 0", note="board is empty")
    assert engine.evaluate_all() == 1
    assert swarm.notified == [(USER, "n_trigger", {"id": trig["id"], "condition": "count() == 0",
                                                    "note": "board is empty"})]


@pytest.mark.parametrize("owner, lang", [(ANNA, "en"), (BORIS, "ru")])
def test_wake_notification_renders_in_both_languages(engine, swarm, owner, lang):
    trig = engine.create(owner, "count() == 0", note="N")
    engine.evaluate_all()
    text = swarm.db.scalar("SELECT text FROM notifications WHERE recipient = ?", (owner,))
    expected = prompts.TEXTS[lang]["n_trigger"].format(id=trig["id"], condition="count() == 0", note="N")
    assert text == expected


# --- actions -----------------------------------------------------------------------------------


def test_message_action(engine, swarm):
    engine.create(ANNA, "count() == 0", actions=[{"type": "message", "to": BORIS, "text": "Board is empty"}])
    assert engine.evaluate_all() == 1
    assert swarm.mail.sent == [(ANNA, BORIS, "Board is empty")]
    assert swarm.notified == []           # no wake action and no errors -> no notification


def test_set_status_action(engine, swarm):
    a = add_task(swarm, status="done")
    b = add_task(swarm, status="waiting", assignee=BORIS)
    engine.create(BORIS, f"done({a})", actions=[{"type": "set_status", "task_id": b, "status": "in_progress"},
                                                 {"type": "wake"}])
    assert engine.evaluate_all() == 1
    assert swarm.board.calls == [("update", BORIS, b, {"status": "in_progress"})]
    assert [k for _, k, _ in swarm.notified] == ["n_trigger"]


def test_create_task_action(engine, swarm):
    engine.create(USER, "goal_done()", actions=[{"type": "create_task", "title": "Retrospective",
                                                 "goal": "Lessons learned", "assignee": ANNA,
                                                 "tags": ["retro"], "priority": 0}])
    swarm.set_goal(USER, "G")
    swarm.complete_goal(ANNA, "ok")
    assert engine.evaluate_all() == 1
    assert swarm.board.calls == [("create", USER, "Retrospective", {
        "goal": "Lessons learned", "description": "", "assignee": ANNA, "tags": ["retro"], "priority": 0})]


def test_action_errors_are_stored_and_reported(engine, swarm):
    swarm.board.fail = ToolError("Only the assignee can change the status.")
    trig = engine.create(ANNA, "count() >= 0", note="heads up",
                         actions=[{"type": "set_status", "task_id": 1, "status": "failed"},
                                  {"type": "message", "to": BORIS, "text": "hi"}])
    assert engine.evaluate_all() == 1
    state = engine.get(trig["id"])
    assert state["last_error"] == "set_status: Only the assignee can change the status."
    assert state["fire_count"] == 1 and state["active"] is False
    assert swarm.mail.sent == [(ANNA, BORIS, "hi")]       # other actions still run
    assert len(swarm.notified) == 1
    recipient, key, kw = swarm.notified[0]
    assert recipient == ANNA and key == "n_trigger" and kw["id"] == trig["id"]
    assert kw["note"].startswith("heads up\n") and "Only the assignee" in kw["note"]
    fired = events(swarm, "trigger.fired")[0]["data"]
    assert fired["errors"] == ["set_status: Only the assignee can change the status."]
    assert fired["trigger"]["last_error"] == state["last_error"]


def test_action_error_with_wake_sends_single_notification(engine, swarm):
    swarm.mail.fail = ToolError("Unknown member 'Ghost'.")
    engine.create(BORIS, "count() >= 0",
                  actions=[{"type": "wake"}, {"type": "message", "to": "Ghost", "text": "x"}])
    engine.evaluate_all()
    assert len(swarm.notified) == 1
    assert "Unknown member 'Ghost'" in swarm.notified[0][2]["note"]


def test_missing_components_and_unexpected_exceptions(engine, swarm):
    swarm.mail = None
    swarm.board.fail = RuntimeError("db exploded")
    trig = engine.create(ANNA, "count() >= 0", actions=[
        {"type": "message", "to": BORIS, "text": "x"},
        {"type": "create_task", "title": "T"},
    ])
    assert engine.evaluate_all() == 1
    error = engine.get(trig["id"])["last_error"]
    assert "message: the swarm has no mail component" in error
    assert "create_task: RuntimeError: db exploded" in error
    assert len(swarm.notified) == 1


def test_successful_fire_clears_previous_action_error(engine, swarm):
    a = add_task(swarm)
    swarm.board.fail = ToolError("nope")
    trig = engine.create(ANNA, f"status({a}) == todo", once=False,
                         actions=[{"type": "set_status", "task_id": a, "status": "in_progress"}])
    engine.evaluate_all()
    assert engine.get(trig["id"])["last_error"] == "set_status: nope"
    swarm.board.fail = None
    engine.evaluate_all()                                  # still true: no edge, error kept
    assert engine.get(trig["id"])["last_error"] == "set_status: nope"
    set_status(swarm, a, "waiting")
    engine.evaluate_all()
    set_status(swarm, a, "todo")
    assert engine.evaluate_all() == 1
    assert engine.get(trig["id"])["last_error"] is None


def test_evaluate_all_is_not_reentrant(engine, swarm):
    a = add_task(swarm, status="done")
    b = add_task(swarm)
    nested: list[int] = []
    swarm.board.on_update = lambda: nested.append(engine.evaluate_all())
    engine.create(ANNA, f"done({a})", actions=[{"type": "set_status", "task_id": b, "status": "done"}])
    engine.create(ANNA, f"done({b})")
    engine._dirty.clear()
    assert engine.evaluate_all() == 2      # the second trigger sees the action's change in the same pass
    assert nested == [0]
    assert engine._dirty.is_set()


def test_action_events_mark_dirty_for_next_iteration(engine, swarm):
    a = add_task(swarm, status="done")
    b = add_task(swarm)
    engine.create(ANNA, f"done({b})")        # evaluated before the action below changes b
    engine.create(ANNA, f"done({a})", actions=[{"type": "set_status", "task_id": b, "status": "done"}])
    engine._dirty.clear()
    assert engine.evaluate_all() == 1
    assert engine._dirty.is_set()
    assert engine.evaluate_all() == 1


# --- bus & loop --------------------------------------------------------------------------------


@pytest.mark.parametrize("event_type, dirty", [
    ("task.updated", True), ("task.comment", True), ("message.sent", True), ("file.changed", True),
    ("goal.completed", True), ("agent.status", True), ("notification", False), ("trigger.fired", False),
    ("mcp.status", False), ("swarm.paused", False),
])
def test_bus_events_mark_dirty(engine, swarm, event_type, dirty):
    engine._dirty.clear()
    swarm.bus.emit(event_type, None)
    assert engine._dirty.is_set() is dirty


async def test_run_loop_reacts_to_events_and_cancels(swarm):
    engine = TriggerEngine(swarm)
    engine.tick_seconds = 30
    task = add_task(swarm)
    trig = engine.create(ANNA, f"done({task})")
    runner = asyncio.create_task(engine.run())
    await asyncio.sleep(0.05)
    assert engine.get(trig["id"])["fire_count"] == 0
    set_status(swarm, task, "done")
    swarm.bus.emit("task.updated", USER, task={"id": task}, changes={})
    for _ in range(100):
        if engine.get(trig["id"])["fire_count"]:
            break
        await asyncio.sleep(0.01)
    assert engine.get(trig["id"])["fire_count"] == 1
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert runner.cancelled()


async def test_run_loop_ticks_without_events(swarm):
    engine = TriggerEngine(swarm)
    engine.tick_seconds = 0.02
    clock = Clock()
    engine.clock = clock
    trig = engine.create(ANNA, "elapsed(100)")
    runner = asyncio.create_task(engine.run())
    try:
        await asyncio.sleep(0.1)
        assert engine.get(trig["id"])["fire_count"] == 0
        clock.advance(100)
        for _ in range(100):
            if engine.get(trig["id"])["fire_count"]:
                break
            await asyncio.sleep(0.01)
        assert engine.get(trig["id"])["fire_count"] == 1
    finally:
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner


async def test_run_loop_stops_when_cancelled_together_with_a_state_change(swarm):
    # regression: on Python 3.11 asyncio.wait_for swallowed a cancel() arriving in the same loop
    # iteration as the dirty event (e.g. board.create(...); await swarm.stop()) and stop() hung forever
    engine = TriggerEngine(swarm)
    engine.tick_seconds = 30
    runner = asyncio.create_task(engine.run())
    await asyncio.sleep(0.02)
    swarm.bus.emit("task.created", USER, task={"id": 1})
    runner.cancel()
    done, _ = await asyncio.wait({runner}, timeout=1)
    if not done:
        runner.cancel()
        await asyncio.wait({runner}, timeout=1)
    assert runner in done and runner.cancelled()


async def test_run_loop_yields_while_actions_keep_it_dirty(swarm, monkeypatch):
    # regression: Event.wait() does not suspend when the event is already set, so passes whose actions
    # kept marking the engine dirty ran back to back and starved every other task in the swarm
    engine = TriggerEngine(swarm)
    engine.tick_seconds = 30
    progressed_at: list[int] = []
    calls: list[int] = []

    def busy_pass() -> int:
        calls.append(1)
        if len(calls) < 2000:
            engine.mark_dirty()        # e.g. an action emitted task.updated
        return 1

    async def other_task() -> None:
        await asyncio.sleep(0)
        progressed_at.append(len(calls))

    monkeypatch.setattr(engine, "evaluate_all", busy_pass)
    engine.mark_dirty()
    runner = asyncio.create_task(engine.run())
    other = asyncio.create_task(other_task())
    await asyncio.sleep(0.05)
    runner.cancel()
    await asyncio.wait({runner, other}, timeout=1)
    assert progressed_at and progressed_at[0] < 10


def test_run_loop_works_in_successive_event_loops(engine, swarm):
    # regression: the asyncio.Event bound to the first loop made run() die with RuntimeError
    # ("bound to a different event loop") when the engine was started again in a new loop
    engine.tick_seconds = 0.01

    async def session() -> int:
        trig = engine.create(USER, "count() == 0", note="session")
        runner = asyncio.create_task(engine.run())
        for _ in range(100):
            if engine.get(trig["id"])["fire_count"] or runner.done():
                break
            await asyncio.sleep(0.01)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner
        return engine.get(trig["id"])["fire_count"]

    assert asyncio.run(session()) == 1
    assert asyncio.run(session()) == 1


async def test_run_loop_survives_evaluation_failures(swarm, monkeypatch):
    engine = TriggerEngine(swarm)
    engine.tick_seconds = 0.01
    calls: list[int] = []

    def flaky() -> int:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return 0

    monkeypatch.setattr(engine, "evaluate_all", flaky)
    runner = asyncio.create_task(engine.run())
    for _ in range(100):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.01)
    runner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await runner
    assert len(calls) >= 3
