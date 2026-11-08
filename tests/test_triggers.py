from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from dreamteam import prompts
from dreamteam.db import dumps, loads
from dreamteam.jobs import fmt_duration
from dreamteam.models import USER, AgentKind, AgentStatus, ToolError
from dreamteam.swarm import QUIET_KEY
from dreamteam.tools.fmt import item_time
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

    def send(self, sender: str, to: Any, text: str, reply_to: int | None = None, *, automatic: bool = False) -> dict:
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


def test_safe_path_resolves_relative_and_rejects_escapes(swarm):
    from dreamteam.triggers import safe_path
    root = swarm.config.workspace_dir
    assert safe_path(root, "docs/report.md") == root.resolve() / "docs" / "report.md"
    assert safe_path(root, "../secret.txt") is None
    assert safe_path(root, ".git/config") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows UNC/device path rules")
def test_safe_path_refuses_unc_and_device_paths_without_contacting_host(swarm, monkeypatch):
    # a UNC or device path is refused before anything resolves it
    from dreamteam.triggers import safe_path
    root = swarm.config.workspace_dir
    seen: list[str] = []
    real = os.path.realpath

    def spy(p, *a, **k):
        seen.append(os.fspath(p))
        return real(p, *a, **k)

    monkeypatch.setattr(os.path, "realpath", spy)
    for bad in (r"\\host.invalid\share\x", "//host.invalid/share/x",
                r"\\?\C:\Windows\win.ini", r"\\.\C:\Windows\win.ini"):
        assert safe_path(root, bad) is None
    assert not any(p.startswith(("\\\\", "//")) for p in seen)


async def test_evaluate_pass_probes_files_off_the_event_loop(engine, swarm, monkeypatch):
    # the trigger loop stats the watched files in a thread, so a slow path cannot block it
    import threading
    import dreamteam.triggers as triggers_mod
    root = swarm.config.workspace_dir
    (root / "watched.txt").write_text("hi", encoding="utf-8")
    trig = engine.create(ANNA, "file_exists('watched.txt')")   # compiled and cached on the loop
    engine.create(ANNA, "file_changed('watched.txt')")
    main = threading.get_ident()
    threads: list[int] = []
    keys: list[int] = []
    real_probe, real_key = triggers_mod._file_probe, triggers_mod._path_key

    def spy(r, p):
        threads.append(threading.get_ident())
        return real_probe(r, p)

    def key_spy(r, p):
        keys.append(threading.get_ident())
        return real_key(r, p)

    monkeypatch.setattr(triggers_mod, "_file_probe", spy)
    monkeypatch.setattr(triggers_mod, "_path_key", key_spy)
    fired = await engine._evaluate_pass()
    assert fired == 1 and engine.get(trig["id"])["fire_count"] == 1
    assert threads and all(t != main for t in threads)   # every file stat ran off the loop thread
    assert keys and all(t != main for t in keys)          # and every path a file_changed() event is matched by


def test_time_functions(engine, clock):
    past = datetime.fromtimestamp(clock.now - 3600).strftime("%Y-%m-%d %H:%M")
    # three hours: more than any clock change (two hours at most), so the hour that repeats when clocks go back
    # cannot make it read back as now
    future = datetime.fromtimestamp(clock.now + 3 * 3600).strftime("%Y-%m-%dT%H:%M:%S")
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
                         "last_fired", "fire_count", "last_error", "held", "told_at", "wake_at"}
    assert (trig["held"], trig["told_at"], trig["wake_at"]) == (0, None, None)
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
    assert engine.evaluate_all() == 0                      # seen, but not settled yet ([agents] file_settle_seconds)
    clock.advance(swarm.config.agents.file_settle_seconds)
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
    swarm.config.agents.file_settle_seconds = 0      # file_changed() at every change, not once it settled
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


# --- file_changed(): one fire per settled run of changes (docs/SPEC.md §8) ----------------------

SETTLE = 60.0   # [agents] file_settle_seconds by default


class Turn:
    """A stand-in runtime of an AI agent: WORKING while it is in a turn (TriggerEngine._editors_done)."""

    def __init__(self, state: str = AgentStatus.WORKING) -> None:
        self.state = state


def edit(swarm: Any, actor: str, rel: str, text: str) -> None:
    """A write_file/edit_file edit as Workspace makes it: the file written, then its file.changed event."""
    path = swarm.config.workspace_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    swarm.bus.emit("file.changed", actor, path=rel, op="edit")


def last_changes(swarm: Any, owner: str) -> str:
    key, kw = swarm.notifications_for(owner)[-1]
    assert key == "n_trigger_files", key
    return kw["changes"]


def test_consecutive_edits_give_one_fire_once_the_file_settled(engine, swarm, clock):
    swarm.config.agents.trigger_wake_seconds = 0       # every fire tells (the wake limit: further below)
    swarm.runtimes[BORIS] = Turn()                     # Boris edits during one long turn
    trig = engine.create(ANNA, "file_changed('docs/AUDIT.md')", once=False, note="Review the audit.")
    for i in range(5):
        edit(swarm, BORIS, "docs/AUDIT.md", "x" * (i + 1))
        clock.advance(10)
        assert engine.evaluate_all() == 0              # still changing
    assert engine.pending(trig["id"]) == [("docs/AUDIT.md", 5, 0.0)]
    clock.advance(SETTLE - 1)
    assert engine.evaluate_all() == 0
    clock.advance(1)
    assert engine.evaluate_all() == 1                  # a minute without a change
    assert swarm.notified == [(ANNA, "n_trigger_files", {
        "id": trig["id"], "condition": "file_changed('docs/AUDIT.md')", "note": "Review the audit.",
        "changes": " File docs/AUDIT.md was changed (changes: 5, by Борис Иванов); the last change was 1 min ago."})]
    text = swarm.db.scalar("SELECT text FROM notifications WHERE recipient = ?", (ANNA,))
    assert text == ("Trigger #1 fired: file_changed('docs/AUDIT.md'). File docs/AUDIT.md was changed (changes: 5, by "
                    "Борис Иванов); the last change was 1 min ago. Review the audit.")
    fired = events(swarm, "trigger.fired")[-1]["data"]
    assert fired["files"] == [{"path": "docs/AUDIT.md", "changes": 5, "by": [BORIS], "settled": True}]
    assert engine.evaluate_all() == 0 and engine.pending(trig["id"]) == []
    edit(swarm, BORIS, "docs/AUDIT.md", "the next run")    # a new run: one more fire once it settled
    assert engine.evaluate_all() == 0
    clock.advance(SETTLE)
    assert engine.evaluate_all() == 1
    assert "(changes: 1, by Борис Иванов)" in last_changes(swarm, ANNA)
    assert engine.get(trig["id"])["fire_count"] == 2


def test_a_file_that_keeps_changing_fires_at_the_cap_then_runs_again(engine, swarm, clock):
    swarm.config.agents.trigger_wake_seconds = 0
    swarm.runtimes[BORIS] = Turn()
    trig = engine.create(ANNA, "file_changed('log.txt')", once=False)
    fired = []
    for i in range(12):                                # a change every 30 s: never a minute of quiet
        edit(swarm, BORIS, "log.txt", "x" * (i + 1))
        clock.advance(30)
        fired.append(engine.evaluate_all())
    # first seen at the first pass; [agents] file_max_wait_seconds (300) later it fires while still changing
    assert fired == [0] * 10 + [1, 0]
    assert swarm.notified[0][2]["changes"] == " File log.txt is still changing (changes: 11, by Борис Иванов in 5 min)."
    assert events(swarm, "trigger.fired")[-1]["data"]["files"][0]["settled"] is False
    clock.advance(SETTLE)
    assert engine.evaluate_all() == 1                  # the next run settled
    assert last_changes(swarm, ANNA) == (" File log.txt was changed (changes: 1, by Борис Иванов);"
                                         " the last change was 1 min ago.")
    swarm.config.agents.file_max_wait_seconds = 0      # 0: no cap
    for i in range(20):
        edit(swarm, BORIS, "log.txt", "y" * (i + 1))
        clock.advance(30)
        assert engine.evaluate_all() == 0
    assert engine.get(trig["id"])["fire_count"] == 2


def test_the_owners_own_edits_do_not_count(engine, swarm, clock):
    trig = engine.create(ANNA, "file_changed('notes.md')", once=False)
    for i in range(3):
        edit(swarm, ANNA, "notes.md", "x" * (i + 1))
        assert engine.evaluate_all() == 0
    clock.advance(SETTLE)
    assert engine.evaluate_all() == 0 and engine.pending(trig["id"]) == []
    swarm.runtimes[BORIS] = Turn()
    edit(swarm, BORIS, "notes.md", "theirs")          # another member's edit counts...
    edit(swarm, ANNA, "notes.md", "mine again")       # ...the owner's in the same pass does not
    assert engine.evaluate_all() == 0
    clock.advance(SETTLE)
    assert engine.evaluate_all() == 1
    assert "(changes: 1, by Борис Иванов)" in last_changes(swarm, ANNA)
    # the user's own edits do not wake a trigger of the user either
    mine = engine.create(USER, "file_changed('notes.md')", once=False)
    edit(swarm, USER, "notes.md", "the user's")
    engine.evaluate_all()
    clock.advance(SETTLE)
    engine.evaluate_all()
    assert engine.get(mine["id"])["fire_count"] == 0


def test_changes_no_event_explains_count_once_per_pass(engine, swarm, clock):
    root = swarm.config.workspace_dir
    engine.create(ANNA, "file_changed('out.log')", once=False)
    _write(root / "out.log", "a", clock.now + 1)
    _write(root / "out.log", "ab", clock.now + 2)     # two writes of a command between two passes: one change
    assert engine.evaluate_all() == 0
    _write(root / "out.log", "abc", clock.now + 3)
    assert engine.evaluate_all() == 0                  # no event: no editor whose turn could end it early
    clock.advance(SETTLE)
    assert engine.evaluate_all() == 1
    assert last_changes(swarm, ANNA) == " File out.log was changed (changes: 2); the last change was 1 min ago."


def test_edits_in_one_pass_count_each_and_a_command_with_them_not_apart(engine, swarm, clock):
    """Every event counts; a pass's new signature is a change no event explains only when no event came since the
    previous pass (a command's write between the same two passes as an edit is not counted apart from it)."""
    swarm.runtimes[BORIS] = Turn()
    a = engine.create(ANNA, "file_changed('a.md')", once=False)
    edit(swarm, BORIS, "a.md", "1")
    edit(swarm, BORIS, "a.md", "22")
    engine.evaluate_all()
    assert engine.pending(a["id"]) == [("a.md", 2, 0.0)]
    b = engine.create(ANNA, "file_changed('b.md')", once=False)
    _write(swarm.config.workspace_dir / "b.md", "a command's", clock.now + 1)
    edit(swarm, BORIS, "b.md", "new")
    engine.evaluate_all()
    assert engine.pending(b["id"]) == [("b.md", 1, 0.0)]
    _write(swarm.config.workspace_dir / "b.md", "a command's, apart", clock.now + 2)
    engine.evaluate_all()
    assert engine.pending(b["id"]) == [("b.md", 2, 0.0)]
    clock.advance(SETTLE)
    assert engine.evaluate_all() == 2
    assert [kw["changes"] for _, kw in swarm.notifications_for(ANNA)] == [
        " File a.md was changed (changes: 2, by Борис Иванов); the last change was 1 min ago.",
        " File b.md was changed (changes: 2, by Борис Иванов); the last change was 1 min ago."]


def test_the_end_of_the_editors_turn_settles_the_run(engine, swarm, clock):
    turn = swarm.runtimes[BORIS] = Turn()
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    edit(swarm, BORIS, "a.md", "1")
    assert engine.evaluate_all() == 0                  # Boris is still in his turn
    turn.state = AgentStatus.PAUSED
    assert engine.evaluate_all() == 1                  # out of it: no need to wait for a minute of quiet
    del swarm.runtimes[BORIS]                          # an AI agent without a runtime (it left, it stopped)
    edit(swarm, BORIS, "a.md", "22")
    assert engine.evaluate_all() == 1
    # an external member or the user as an editor, or a change no event explains: the quiet window decides
    swarm.add_member("Ext Client", kind=AgentKind.EXTERNAL)
    swarm.runtimes[BORIS] = Turn(AgentStatus.IDLE)
    for i, actor in enumerate(("Ext Client", USER, None)):
        if actor is None:                              # a command's change, seen by a pass before Boris's edit
            _write(swarm.config.workspace_dir / "a.md", "c" * (i + 5), clock.now + 1)
            assert engine.evaluate_all() == 0
        else:
            edit(swarm, actor, "a.md", "e" * (i + 5))
        edit(swarm, BORIS, "a.md", "b" * (i + 10))     # Boris is idle, but not the only cause
        assert engine.evaluate_all() == 0, actor
        clock.advance(SETTLE)
        assert engine.evaluate_all() == 1, actor
    assert engine.get(trig["id"])["fire_count"] == 5


def test_a_fire_of_another_part_absorbs_an_unsettled_run(engine, swarm, clock):
    swarm.runtimes[BORIS] = Turn()
    trig = engine.create(ANNA, "file_changed('a.md') or message()", once=False)
    edit(swarm, BORIS, "a.md", "1")
    clock.advance(5)
    assert engine.evaluate_all() == 0
    clock.advance(5)
    add_message(swarm, BORIS, [ANNA], "done, see a.md", clock.now)
    assert engine.evaluate_all() == 1
    assert last_changes(swarm, ANNA) == " File a.md is still changing (changes: 1, by Борис Иванов in 5 s)."
    clock.advance(SETTLE)
    assert engine.evaluate_all() == 0                  # told already: no second fire for those edits
    assert engine.get(trig["id"])["fire_count"] == 1 and engine.pending(trig["id"]) == []


def test_file_changed_with_another_condition_and_a_once_trigger(engine, swarm, clock):
    task = add_task(swarm)
    trig = engine.create(ANNA, f"file_changed('a.md') and done({task})")
    edit(swarm, BORIS, "a.md", "1")                    # Boris has no runtime: out of his turn, settled at once
    assert engine.evaluate_all() == 0                  # the task is not done yet
    edit(swarm, BORIS, "a.md", "22")
    assert engine.evaluate_all() == 0
    clock.advance(30)
    set_status(swarm, task, "done")
    assert engine.evaluate_all() == 1
    assert last_changes(swarm, ANNA) == (" File a.md was changed (changes: 2, by Борис Иванов);"
                                         " the last change was 30 s ago.")
    assert engine.get(trig["id"])["active"] is False and trig["id"] not in engine._runs
    # a once trigger on its own waits for the settled run too
    swarm.runtimes[BORIS] = Turn()
    once = engine.create(ANNA, "file_changed('b.md')")
    edit(swarm, BORIS, "b.md", "1")
    assert engine.evaluate_all() == 0
    clock.advance(SETTLE)
    assert engine.evaluate_all() == 1 and engine.get(once["id"])["active"] is False


def test_runs_are_seeded_from_the_journal_after_a_restart(engine, swarm, clock):
    clock.now -= 10                                    # the journal's times (time.time()) come after the creation
    root = swarm.config.workspace_dir
    swarm.runtimes[BORIS] = Turn()
    a = engine.create(ANNA, "file_changed('a.md')", once=False)
    b = engine.create(ANNA, "file_changed('b.md')", once=False)    # only the owner's own edit
    c = engine.create(ANNA, "file_changed('c.log')", once=False)   # a command's change: no event
    for i in range(3):
        edit(swarm, BORIS, "a.md", "x" * (i + 1))
    edit(swarm, ANNA, "b.md", "mine")
    _write(root / "c.log", "out", clock.now + 5)
    other = TriggerEngine(swarm)                       # a restart: none of the first engine's memory
    other.clock = clock
    swarm.triggers = other
    assert other.evaluate_all() == 0                   # the journal's times are clamped to now: not quiet yet
    assert other.pending(a["id"]) == [("a.md", 3, 0.0)] and other.pending(b["id"]) == []
    assert other.pending(c["id"]) == [("c.log", 1, 0.0)]
    clock.advance(SETTLE)
    assert other.evaluate_all() == 2
    assert [kw["id"] for _, kw in swarm.notifications_for(ANNA)] == [a["id"], c["id"]]
    assert swarm.notifications_for(ANNA)[0][1]["changes"] == (
        " File a.md was changed (changes: 3, by Борис Иванов); the last change was 1 min ago.")
    assert swarm.notifications_for(ANNA)[1][1]["changes"] == (" File c.log was changed (changes: 1);"
                                                              " the last change was 1 min ago.")
    assert other.get(b["id"])["fire_count"] == 0


def test_an_emptied_run_and_a_clock_set_back_leave_no_stale_times(engine, swarm, clock):
    """The owner's own edits leave the run empty, so the cap starts at the first counted change (no fire at another
    member's first edit, mid-turn, an hour later). A wall clock set back does not keep a run from settling."""
    swarm.config.agents.trigger_wake_seconds = 0
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    edit(swarm, ANNA, "a.md", "mine")
    clock.advance(1)
    assert engine.evaluate_all() == 0 and engine.pending(trig["id"]) == []
    assert engine._runs[trig["id"]]["a.md"].first is None
    clock.advance(3600)
    swarm.runtimes[BORIS] = Turn()
    edit(swarm, BORIS, "a.md", "the first of many edits")
    clock.advance(1)
    assert engine.evaluate_all() == 0                  # Boris is in his turn and the run is a second old
    clock.advance(-3600)                               # the system clock corrected back by an hour
    for _ in range(int(SETTLE) // 10 + 2):
        clock.advance(10)
        engine.evaluate_all()
    assert engine.get(trig["id"])["fire_count"] == 1
    assert "(changes: 1, by Борис Иванов)" in last_changes(swarm, ANNA)


def test_a_reseeded_run_starts_its_cap_at_the_first_counted_change(engine, swarm, clock):
    """After a restart the cap counts from the first change of others in the journal, not from the owner's own
    earlier edit: another member's edit in its turn does not fire at the first pass."""
    swarm.runtimes[BORIS] = Turn()
    clock.now -= 1000
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    clock.now += 1000
    edit(swarm, ANNA, "a.md", "mine, long ago")
    swarm.db.execute("UPDATE events SET ts = ? WHERE type = 'file.changed'", (clock.now - 900,))
    edit(swarm, BORIS, "a.md", "theirs, just now")
    other = TriggerEngine(swarm)
    other.clock = clock
    swarm.triggers = other
    assert other.evaluate_all() == 0 and other.pending(trig["id"]) == [("a.md", 1, 0.0)]
    clock.advance(SETTLE)
    assert other.evaluate_all() == 1


def test_settle_zero_fires_at_every_change_but_not_at_the_owners(engine, swarm, clock):
    swarm.config.agents.file_settle_seconds = 0
    swarm.runtimes[BORIS] = Turn()
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    for i in range(3):
        edit(swarm, BORIS, "a.md", "x" * (i + 1))
        assert engine.evaluate_all() == 1
    edit(swarm, ANNA, "a.md", "mine")
    assert engine.evaluate_all() == 0
    assert engine.get(trig["id"])["fire_count"] == 3
    assert last_changes(swarm, ANNA) == (" File a.md was changed (changes: 1, by Борис Иванов);"
                                         " the last change was 0 s ago.")


async def _real_workspace(swarm: Any) -> Any:
    from dreamteam.workspace import Workspace

    swarm.config.workspace.git = False
    swarm.workspace = Workspace(swarm)
    await swarm.workspace.init()
    return swarm.workspace


async def _pass_probed_before(engine: Any, write: Any) -> int:
    """A pass whose mark and probes are taken, then `write` (awaited) lands before it takes the events."""
    paths, files = engine._watched_paths()
    engine._write_mark = engine._take_write_mark()             # as _evaluate_pass: the mark, then the probes
    engine._probes, engine._keys = engine._probe_files(paths, files)
    await write
    try:
        return engine.evaluate_all()
    finally:
        engine._probes, engine._keys, engine._write_mark = {}, {}, None


@pytest.mark.parametrize("fire", ["editors' turns", "another part"])
async def test_a_fire_in_the_pass_that_took_a_writes_event_takes_no_stale_baseline(engine, swarm, clock, fire):
    """Boris edits a file four times in his turn, ends it, and the last edit lands after a pass probed the file
    but before that pass took the events. That pass is unsure of the file, so the shortcut waits for the next sure
    probe (changes: 5). A message() that fires the trigger in that pass absorbs the run with a probe that predates
    the write, so the new run takes the next sure probe as its baseline instead of counting a phantom change of the
    write already told."""
    ws = await _real_workspace(swarm)
    turn = swarm.runtimes[BORIS] = Turn()
    swarm.config.agents.trigger_wake_seconds = 0
    (ws.root / "a.md").write_text("v0", encoding="utf-8")
    absorbed = fire == "another part"
    trig = engine.create(ANNA, "file_changed('a.md') or message()" if absorbed else "file_changed('a.md')",
                         once=False)
    for n in range(1, 5):
        await ws.write(BORIS, "a.md", "v" * (n + 1))
        clock.advance(1)
        assert await engine._evaluate_pass() == 0              # still in his turn
    turn.state = AgentStatus.IDLE
    if absorbed:
        add_message(swarm, BORIS, [ANNA], "done, see a.md", clock.now)
        assert await _pass_probed_before(engine, ws.write(BORIS, "a.md", "v" * 6)) == 1
        assert last_changes(swarm, ANNA).startswith(" File a.md is still changing (changes: 5, by Борис Иванов")
    else:
        assert await _pass_probed_before(engine, ws.write(BORIS, "a.md", "v" * 6)) == 0   # event ahead of probe
        assert await engine._evaluate_pass() == 1
        assert last_changes(swarm, ANNA).startswith(" File a.md was changed (changes: 5, by Борис Иванов)")
    for _ in range(2):
        clock.advance(SETTLE + 1)
        assert await engine._evaluate_pass() == 0              # no phantom change of the write already told
    assert engine.get(trig["id"])["fire_count"] == 1 and engine.pending(trig["id"]) == []
    (ws.root / "a.md").write_text("a command's change", encoding="utf-8")   # counted once the base is sure
    assert await engine._evaluate_pass() == 0
    clock.advance(SETTLE)
    assert await engine._evaluate_pass() == 1
    assert last_changes(swarm, ANNA).startswith(" File a.md was changed (changes: 1);")


async def test_an_unsure_pass_does_not_settle_a_run_by_the_editors_turns(engine, swarm, clock):
    """Boris is out of his turn and his edit's event reaches a pass whose probe predates the write: that pass cannot
    know whether a command changed the file as well, so it does not settle the run; the next sure pass does."""
    ws = await _real_workspace(swarm)
    swarm.runtimes[BORIS] = Turn(AgentStatus.IDLE)
    swarm.config.agents.trigger_wake_seconds = 0
    (ws.root / "a.md").write_text("v0", encoding="utf-8")
    engine.create(ANNA, "file_changed('a.md')", once=False)
    assert await _pass_probed_before(engine, ws.write(BORIS, "a.md", "v1")) == 0
    assert await engine._evaluate_pass() == 1
    assert last_changes(swarm, ANNA).startswith(" File a.md was changed (changes: 1, by Борис Иванов)")


async def test_a_change_no_event_explains_keeps_the_shortcut_off_after_a_later_edit(engine, swarm, clock):
    """Once a sure pass has counted a command's change, a later edit and the end of the editor's turn do not settle
    the run: the next sure pass counts the edit too, and the quiet window decides."""
    ws = await _real_workspace(swarm)
    turn = swarm.runtimes[BORIS] = Turn()
    swarm.config.agents.trigger_wake_seconds = 0
    (ws.root / "a.md").write_text("v0", encoding="utf-8")
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    await ws.write(BORIS, "a.md", "v1")
    clock.advance(1)
    assert await engine._evaluate_pass() == 0
    (ws.root / "a.md").write_text("a command's change", encoding="utf-8")
    clock.advance(1)
    assert await engine._evaluate_pass() == 0 and engine.pending(trig["id"]) == [("a.md", 2, 0.0)]
    turn.state = AgentStatus.IDLE
    clock.advance(1)
    assert await _pass_probed_before(engine, ws.write(BORIS, "a.md", "v2, the last edit")) == 0
    clock.advance(1)
    assert await engine._evaluate_pass() == 0                  # sure now: 2 edits + the command's change
    assert engine.pending(trig["id"]) == [("a.md", 3, 0.0)] and not engine._runs[trig["id"]]["a.md"].done
    clock.advance(SETTLE)
    assert await engine._evaluate_pass() == 1
    assert last_changes(swarm, ANNA).startswith(" File a.md was changed (changes: 3, by Борис Иванов)")


@pytest.mark.parametrize("fire", ["quiet window", "editors' turns"])
async def test_a_write_in_flight_at_a_fire_is_told_after_it(engine, swarm, clock, monkeypatch, fire):
    """A pass fires a run while a write of the file is in flight and that write's event lands after the pass: the
    write belongs to the new run, so it is told once (the new baseline is not the file after that write).
    "quiet window": Boris's first edit settled, his second is in flight. "editors' turns": Boris ended his turn,
    and Carol's edit is in flight; the shortcut waits for the sure probe and tells both edits together."""
    import threading

    from dreamteam import workspace as workspace_mod

    carol = swarm.add_member("Carol King", language="en")
    ws = await _real_workspace(swarm)
    boris = swarm.runtimes[BORIS] = Turn()
    swarm.runtimes[carol] = Turn()
    swarm.config.agents.trigger_wake_seconds = 0
    (ws.root / "a.md").write_text("v0", encoding="utf-8")
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    await ws.write(BORIS, "a.md", "boris")
    clock.advance(1)
    assert await engine._evaluate_pass() == 0                  # Boris still in his turn
    if fire == "quiet window":
        clock.advance(SETTLE + 1)
        writer = BORIS
    else:
        boris.state = AgentStatus.IDLE
        writer = carol
    truncated, go = threading.Event(), threading.Event()
    original = workspace_mod._write_inside

    def slow_write(p, rel, data, root):
        with open(p, "r+b") as f:
            f.truncate(0)
        truncated.set()
        go.wait(10)
        original(p, rel, data, root)

    monkeypatch.setattr(workspace_mod, "_write_inside", slow_write)
    task = asyncio.create_task(ws.write(writer, "a.md", "the edit in flight, longer"))
    assert await asyncio.to_thread(truncated.wait, 10)
    first = await engine._evaluate_pass()
    go.set()
    await task
    swarm.runtimes[writer].state = AgentStatus.IDLE
    later = []
    for _ in range(3):
        clock.advance(1)
        later.append(await engine._evaluate_pass())
        clock.advance(SETTLE)
    if fire == "quiet window":
        assert (first, later) == (1, [1, 0, 0])
        assert last_changes(swarm, ANNA).startswith(" File a.md was changed (changes: 1, by Борис Иванов)")
    else:
        assert (first, later) == (0, [1, 0, 0])
        assert last_changes(swarm, ANNA).startswith(
            " File a.md was changed (changes: 2, by Борис Иванов, Carol King)")
    assert engine.pending(trig["id"]) == []


@pytest.mark.parametrize("writer", [BORIS, ANNA])
async def test_a_probe_in_the_middle_of_a_write_is_not_a_change(engine, swarm, clock, monkeypatch, writer):
    """write_file/edit_file opens the file, truncates it, then writes: a pass that probes in between sees a size
    that is neither the old nor the new one. It is not counted (the write's event counts the change), so another
    editor's single edit is one change settled at the end of its turn, and the owner's own edit none."""
    import threading

    from dreamteam import workspace as workspace_mod

    ws = await _real_workspace(swarm)
    swarm.runtimes[BORIS] = Turn(AgentStatus.IDLE)          # out of its turn: the shortcut may settle the run
    swarm.config.agents.trigger_wake_seconds = 0
    (ws.root / "a.md").write_text("old text", encoding="utf-8")
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    assert await engine._evaluate_pass() == 0
    truncated, go = threading.Event(), threading.Event()
    original = workspace_mod._write_inside

    def slow_write(p, rel, data, root):
        with open(p, "r+b") as f:
            f.truncate(0)
        truncated.set()
        go.wait(10)
        original(p, rel, data, root)

    monkeypatch.setattr(workspace_mod, "_write_inside", slow_write)
    task = asyncio.create_task(ws.write(writer, "a.md", "the new, longer text"))
    assert await asyncio.to_thread(truncated.wait, 10)
    assert await engine._evaluate_pass() == 0               # probes the empty file mid-write
    assert (ws.root / "a.md").stat().st_size == 0
    go.set()
    await task
    first = await engine._evaluate_pass()
    clock.advance(SETTLE + 1)
    later = await engine._evaluate_pass()
    if writer == BORIS:  # at once: every change is explained by the event and Boris is out of his turn
        assert (first, later) == (1, 0) and engine.get(trig["id"])["fire_count"] == 1
        assert last_changes(swarm, ANNA).startswith(" File a.md was changed (changes: 1, by Борис Иванов)")
    else:
        assert (first, later) == (0, 0) and engine.pending(trig["id"]) == []


@pytest.mark.parametrize("closed", [False, True])
async def test_a_cancelled_write_stays_in_flight_until_its_thread_ends(swarm, monkeypatch, closed):
    """A write_file whose tool call is cancelled (the member stopped) while its thread writes: the path stays in
    flight until the thread ends, so no probe in between counts the half-written file, and the change it made is
    still this member's file.changed event. When the session closed its database meanwhile (stopped, unloaded),
    the late event is dropped quietly: nothing reaches the loop's exception handler."""
    import threading

    from dreamteam import workspace as workspace_mod
    from dreamteam.workspace import path_key

    errors: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, context: errors.append(context))
    ws = await _real_workspace(swarm)
    (ws.root / "a.md").write_text("old", encoding="utf-8")
    started, go = threading.Event(), threading.Event()
    original = workspace_mod._write_inside

    def slow_write(p, rel, data, root):
        started.set()
        go.wait(10)
        original(p, rel, data, root)

    monkeypatch.setattr(workspace_mod, "_write_inside", slow_write)
    task = asyncio.create_task(ws.write(BORIS, "a.md", "new text"))
    assert await asyncio.to_thread(started.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    key = path_key("a.md")
    assert ws.written_since(ws.write_mark(), key)              # the thread still writes
    if closed:
        swarm.db.close()
    go.set()
    for _ in range(500):
        if not ws.written_since(ws.write_mark(), key):
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0)
    assert not ws.written_since(ws.write_mark(), key)
    assert (ws.root / "a.md").read_text(encoding="utf-8") == "new text"
    assert errors == []
    if not closed:
        assert swarm.db.scalar("SELECT actor FROM events WHERE type = 'file.changed'") == BORIS


async def _held_write(ws: Any, monkeypatch: Any, actor: str, text: str, phase: str = "truncated") -> tuple[Any, Any]:
    """Start `actor`'s write_file of a.md and hold its thread with the file truncated (phase "truncated") or already
    written ("written"), before the write returns: set the returned event to let it finish."""
    import threading

    from dreamteam import workspace as workspace_mod

    reached, go = threading.Event(), threading.Event()
    original = workspace_mod._write_inside

    def held(p, rel, data, root):
        if phase == "truncated":
            with open(p, "r+b") as f:
                f.truncate(0)
        else:
            original(p, rel, data, root)
        reached.set()
        go.wait(10)
        if phase == "truncated":
            original(p, rel, data, root)

    monkeypatch.setattr(workspace_mod, "_write_inside", held)
    task = asyncio.create_task(ws.write(actor, "a.md", text))
    assert await asyncio.to_thread(reached.wait, 10)
    monkeypatch.setattr(workspace_mod, "_write_inside", original)
    return task, go


@pytest.mark.parametrize("failure, shown", [(sqlite3.OperationalError("database is locked"), True),
                                            (TypeError("a bug"), True),
                                            (sqlite3.ProgrammingError("Cannot operate on a closed database."), False)])
async def test_a_late_write_event_that_cannot_be_recorded(swarm, monkeypatch, caplog, failure, shown):
    """The file.changed event of a cancelled write_file fails when it lands: a closed database is the expected end of
    a stopped session, anything else is a lost event of a live one and is logged. Neither reaches the loop's handler."""
    import threading

    from dreamteam import workspace as workspace_mod
    from dreamteam.workspace import path_key

    errors: list[dict] = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, context: errors.append(context))
    ws = await _real_workspace(swarm)
    (ws.root / "a.md").write_text("old", encoding="utf-8")
    started, go = threading.Event(), threading.Event()
    original = workspace_mod._write_inside

    def slow_write(p, rel, data, root):
        started.set()
        go.wait(10)
        original(p, rel, data, root)

    def failing_emit(*_args, **_kw):
        raise failure

    monkeypatch.setattr(workspace_mod, "_write_inside", slow_write)
    task = asyncio.create_task(ws.write(BORIS, "a.md", "new text"))
    assert await asyncio.to_thread(started.wait, 10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.setattr(swarm.bus, "emit", failing_emit)
    go.set()
    key = path_key("a.md")
    for _ in range(500):
        if not ws.written_since(ws.write_mark(), key):
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0)
    assert not ws.written_since(ws.write_mark(), key)
    assert errors == []
    logged = [r for r in caplog.records if r.levelno >= logging.ERROR and "late write" in r.getMessage()]
    assert bool(logged) is shown


def _restarted(swarm: Any, clock: Clock) -> TriggerEngine:
    """A new engine over the same swarm: it has followed nothing yet."""
    other = TriggerEngine(swarm)
    other.clock = clock
    swarm.triggers = other
    return other


@pytest.mark.parametrize("watched", ["file_changed('a.md')", "file_exists('a.md')"])
@pytest.mark.parametrize("settle", [0.0, SETTLE])
async def test_a_trigger_created_during_a_passs_stat_takes_nothing_from_that_stat(engine, swarm, clock, monkeypatch,
                                                                                 watched, settle):
    """A command changes the file after a pass's stat thread looked at it, then a trigger is created while that pass
    still waits for the thread. The trigger's baseline is newer than the pass's probe of the same path (which another
    trigger asked for): no phantom change, and no phantom fire at settle 0."""
    import threading

    ws = await _real_workspace(swarm)
    swarm.config.agents.file_settle_seconds = settle
    swarm.config.agents.trigger_wake_seconds = 0
    (ws.root / "a.md").write_text("v0", encoding="utf-8")
    first = engine.create(ANNA, watched, once=False)
    await engine._evaluate_pass()
    loop = asyncio.get_running_loop()
    original = engine._probe_files
    created: list[dict] = []

    def stat_then_create(paths, files):
        probes = original(paths, files)
        done = threading.Event()

        def in_the_gap():
            (ws.root / "a.md").write_text("a command's change", encoding="utf-8")
            created.append(engine.create(BORIS, "file_changed('a.md')", once=False))
            done.set()

        loop.call_soon_threadsafe(in_the_gap)
        assert done.wait(10)
        return probes

    monkeypatch.setattr(engine, "_probe_files", stat_then_create)
    await engine._evaluate_pass()
    monkeypatch.setattr(engine, "_probe_files", original)
    trig = created[0]
    assert engine.pending(trig["id"]) == []
    for _ in range(3):
        clock.advance(SETTLE + 1)
        await engine._evaluate_pass()
        assert engine.pending(trig["id"]) == [] and engine.get(trig["id"])["fire_count"] == 0
    if watched.startswith("file_changed"):                     # the trigger that was there does see the change
        assert engine.get(first["id"])["fire_count"] == 1
    (ws.root / "a.md").write_text("a later change", encoding="utf-8")   # the new trigger's own run works
    await engine._evaluate_pass()
    clock.advance(SETTLE + 1)
    await engine._evaluate_pass()
    assert engine.get(trig["id"])["fire_count"] == 1
    assert "(изменений: 1)" in swarm.notifications_for(BORIS)[-1][1]["changes"]


@pytest.mark.parametrize("condition", ["file_changed('a.md') or file_exists('b.md')",
                                       "file_changed('a.md') or every(30)"])
async def test_a_fire_in_the_pass_a_trigger_was_created_in_takes_nothing_from_that_stat(engine, swarm, clock,
                                                                                       monkeypatch, condition):
    """As above, and another part of the new trigger's condition fires it in that same pass: the fire's new run does
    not take its baseline from the pass's older stat either, so the command's change, made before the trigger existed,
    is not told later. The next sure probe is the baseline; a change after it is counted."""
    import threading

    ws = await _real_workspace(swarm)
    swarm.config.agents.trigger_wake_seconds = 0
    (ws.root / "a.md").write_text("v0", encoding="utf-8")
    (ws.root / "b.md").write_text("there", encoding="utf-8")
    engine.create(ANNA, "file_changed('a.md')", once=False)
    await engine._evaluate_pass()
    loop = asyncio.get_running_loop()
    original = engine._probe_files
    created: list[dict] = []

    def stat_then_create(paths, files):
        probes = original(paths, files)
        done = threading.Event()

        def in_the_gap():
            (ws.root / "a.md").write_text("a command's change", encoding="utf-8")
            created.append(engine.create(BORIS, condition, once=False))
            clock.advance(31)                                  # every(30) is due in this pass
            done.set()

        loop.call_soon_threadsafe(in_the_gap)
        assert done.wait(10)
        return probes

    monkeypatch.setattr(engine, "_probe_files", stat_then_create)
    await engine._evaluate_pass()
    monkeypatch.setattr(engine, "_probe_files", original)
    trig = created[0]
    assert engine.get(trig["id"])["fire_count"] == 1
    for _ in range(3):
        clock.advance(SETTLE + 1)
        await engine._evaluate_pass()
        assert engine.pending(trig["id"]) == []
    assert all(key == "n_trigger" for key, _ in swarm.notifications_for(BORIS))   # none lists a file
    (ws.root / "a.md").write_text("a later change", encoding="utf-8")
    await engine._evaluate_pass()
    assert [(path, count) for path, count, _ in engine.pending(trig["id"])] == [("a.md", 1)]


async def test_a_flag_of_a_run_created_during_a_stat_ends_with_that_pass(engine, swarm, clock, monkeypatch):
    """The pass's stat fails, so it never evaluates: the trigger created in the gap still compares the next pass's
    probe with its baseline."""
    import threading

    ws = await _real_workspace(swarm)
    swarm.config.agents.trigger_wake_seconds = 0
    (ws.root / "a.md").write_text("v0", encoding="utf-8")
    engine.create(ANNA, "file_exists('a.md')")
    loop = asyncio.get_running_loop()
    original = engine._probe_files
    created: list[dict] = []

    def create_then_fail(paths, files):
        done = threading.Event()

        def in_the_gap():
            created.append(engine.create(BORIS, "file_changed('a.md')", once=False))
            done.set()

        loop.call_soon_threadsafe(in_the_gap)
        assert done.wait(10)
        raise OSError("the stat failed")

    monkeypatch.setattr(engine, "_probe_files", create_then_fail)
    with pytest.raises(OSError):
        await engine._evaluate_pass()
    monkeypatch.setattr(engine, "_probe_files", original)
    (ws.root / "a.md").write_text("a command's change", encoding="utf-8")
    await engine._evaluate_pass()
    assert [(path, count) for path, count, _ in engine.pending(created[0]["id"])] == [("a.md", 1)]


@pytest.mark.parametrize("writer", [ANNA, BORIS])
async def test_a_write_in_flight_at_the_first_pass_after_a_restart_is_counted_by_its_event(engine, swarm, clock,
                                                                                          monkeypatch, writer):
    """The first pass after a restart sees the newer mtime of a write in flight: that is no change of its own. The
    owner's edit alone does not fire; another member's counts once."""
    ws = await _real_workspace(swarm)
    swarm.runtimes[BORIS] = Turn()
    swarm.config.agents.trigger_wake_seconds = 0
    _write(ws.root / "a.md", "v0", clock.now - 100)
    clock.advance(-10)                                         # the write's mtime is newer than the trigger
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    clock.advance(10)
    task, go = await _held_write(ws, monkeypatch, writer, "the edit")
    other = _restarted(swarm, clock)
    assert await other._evaluate_pass() == 0
    assert other.pending(trig["id"]) == []
    go.set()
    await task
    clock.advance(1)
    assert await other._evaluate_pass() == 0
    assert [count for _, count, _ in other.pending(trig["id"])] == ([] if writer == ANNA else [1])
    clock.advance(SETTLE + 1)
    fired = await other._evaluate_pass()
    if writer == ANNA:
        assert fired == 0 and swarm.notifications_for(ANNA) == []
    else:
        assert fired == 1 and last_changes(swarm, ANNA).startswith(" File a.md was changed (changes: 1, by Борис")


async def test_a_reseed_with_a_write_landing_after_its_probe_counts_no_phantom_change(engine, swarm, clock):
    """A write lands, with its event, between the probe of the first pass after a restart and that pass's evaluation:
    the journal counts it, and the next sure probe, which shows it, is the baseline and no change of its own."""
    ws = await _real_workspace(swarm)
    swarm.runtimes[BORIS] = Turn()
    _write(ws.root / "a.md", "v0", clock.now - 100)
    clock.advance(-10)
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    clock.advance(10)
    other = _restarted(swarm, clock)
    assert await _pass_probed_before(other, ws.write(BORIS, "a.md", "the edit")) == 0
    assert [count for _, count, _ in other.pending(trig["id"])] == [1]
    clock.advance(1)
    assert await other._evaluate_pass() == 0
    assert [count for _, count, _ in other.pending(trig["id"])] == [1]


def test_an_edit_that_leaves_the_signature_as_it_was_is_told(engine, swarm, clock):
    """Another member's write of the same size within the same mtime tick: its event counts it, so it fires and the
    trigger does not stay pending until the signature moves again."""
    swarm.config.agents.trigger_wake_seconds = 0
    swarm.runtimes[BORIS] = Turn(AgentStatus.IDLE)
    path = swarm.config.workspace_dir / "a.md"
    path.write_text("v0", encoding="utf-8")
    before = os.stat(path)
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    assert engine.evaluate_all() == 0
    edit(swarm, BORIS, "a.md", "v1")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert engine.evaluate_all() == 1
    assert last_changes(swarm, ANNA).startswith(" File a.md was changed (changes: 1, by Борис Иванов)")
    assert engine.pending(trig["id"]) == []


def test_a_commands_changes_that_end_on_the_old_signature_are_told(engine, swarm, clock):
    """Each sure pass saw its own signature, so the changes are counted, also when the last one restores the file's
    signature at the reference point (a tool that keeps mtimes): they are told after the quiet window."""
    swarm.config.agents.trigger_wake_seconds = 0
    path = swarm.config.workspace_dir / "a.md"
    path.write_text("v0", encoding="utf-8")
    before = os.stat(path)
    engine.create(ANNA, "file_changed('a.md')", once=False)
    path.write_text("a command's longer text", encoding="utf-8")
    assert engine.evaluate_all() == 0
    path.write_text("v0", encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert engine.evaluate_all() == 0
    clock.advance(SETTLE)
    assert engine.evaluate_all() == 1
    assert last_changes(swarm, ANNA).startswith(" File a.md was changed (changes: 2);")


def test_a_clock_set_back_does_not_hold_the_cap_of_a_file_that_keeps_changing(engine, swarm, clock):
    """Boris edits every 30 s in his turn and the clock is set back by an hour after his first edit: the run's first
    change is at most now, so the cap fires about 5 min after the jump, not an hour later."""
    swarm.config.agents.trigger_wake_seconds = 0
    swarm.runtimes[BORIS] = Turn()
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    edit(swarm, BORIS, "a.md", "x")
    clock.advance(1)
    assert engine.evaluate_all() == 0
    clock.advance(-3600)
    for n in range(12):                                        # 360 s: the cap (300 s) and one pass more
        edit(swarm, BORIS, "a.md", "x" * (n + 2))
        clock.advance(30)
        engine.evaluate_all()
    assert engine.get(trig["id"])["fire_count"] == 1
    assert last_changes(swarm, ANNA).startswith(" File a.md is still changing (changes: 12, by Борис Иванов")


def test_an_event_between_a_restart_and_the_first_pass_counts_once(engine, swarm, clock):
    """It is in the journal the reseed reads and in the events the new engine buffered: the run's cursor counts it
    once. The next pass adds nothing."""
    swarm.runtimes[BORIS] = Turn()
    clock.advance(-10)
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    edit(swarm, BORIS, "a.md", "1")
    other = _restarted(swarm, clock)
    edit(swarm, BORIS, "a.md", "22")
    other.evaluate_all()
    assert [count for _, count, _ in other.pending(trig["id"])] == [2]
    other.evaluate_all()
    assert [count for _, count, _ in other.pending(trig["id"])] == [2]


def test_a_trigger_does_not_count_an_event_buffered_before_its_creation(engine, swarm, clock):
    swarm.runtimes[BORIS] = Turn()
    edit(swarm, BORIS, "a.md", "1")                            # no pass has taken it yet
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    engine.evaluate_all()
    assert engine.pending(trig["id"]) == []


def test_a_deletion_is_no_change_and_a_recreated_file_is_one(engine, swarm, clock):
    swarm.config.agents.trigger_wake_seconds = 0
    path = swarm.config.workspace_dir / "a.md"
    path.write_text("v0", encoding="utf-8")
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    path.unlink()
    assert engine.evaluate_all() == 0 and engine.pending(trig["id"]) == []
    clock.advance(SETTLE + 1)
    assert engine.evaluate_all() == 0
    path.write_text("v1", encoding="utf-8")
    assert engine.evaluate_all() == 0
    assert [count for _, count, _ in engine.pending(trig["id"])] == [1]
    clock.advance(SETTLE)
    assert engine.evaluate_all() == 1
    assert last_changes(swarm, ANNA).startswith(" File a.md was changed (changes: 1);")


def test_a_counted_edit_waits_while_the_file_is_missing(engine, swarm, clock):
    """file_changed() is false without the file, past the quiet window and the cap, and the run stays pending; the
    next pass that sees the file again tells every change."""
    swarm.config.agents.trigger_wake_seconds = 0
    swarm.runtimes[BORIS] = Turn(AgentStatus.IDLE)
    path = swarm.config.workspace_dir / "a.md"
    path.write_text("v0", encoding="utf-8")
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    edit(swarm, BORIS, "a.md", "theirs")
    path.unlink()
    assert engine.evaluate_all() == 0
    clock.advance(600)
    assert engine.evaluate_all() == 0
    assert [count for _, count, _ in engine.pending(trig["id"])] == [1]
    edit(swarm, BORIS, "a.md", "theirs again")
    assert engine.evaluate_all() == 1
    assert last_changes(swarm, ANNA).startswith(" File a.md was changed (changes: 2, by Борис Иванов)")
    assert engine.pending(trig["id"]) == []


@pytest.mark.parametrize("wait, fired", [(61, 0), (301, 1)])
def test_a_missing_file_brought_back_by_a_command_is_one_more_change(engine, swarm, clock, wait, fired):
    """A command restores the file. Its return is a change no event explains, told by the same rule as any other:
    within the cap it restarts the quiet window and turns the editors' shortcut off, past the cap the run is told at
    once."""
    swarm.config.agents.trigger_wake_seconds = 0
    swarm.runtimes[BORIS] = Turn(AgentStatus.IDLE)
    path = swarm.config.workspace_dir / "a.md"
    path.write_text("v0", encoding="utf-8")
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    edit(swarm, BORIS, "a.md", "theirs")
    path.unlink()
    assert engine.evaluate_all() == 0
    clock.advance(wait)
    assert engine.evaluate_all() == 0
    assert [count for _, count, _ in engine.pending(trig["id"])] == [1]
    path.write_text("restored", encoding="utf-8")
    assert engine.evaluate_all() == fired
    if not fired:
        assert engine.pending(trig["id"]) == [("a.md", 2, 0.0)]
        clock.advance(SETTLE - 1)
        assert engine.evaluate_all() == 0                      # the editor is out of his turn: the window decides
        clock.advance(1)
        assert engine.evaluate_all() == 1
    assert "(changes: 2, by Борис Иванов" in last_changes(swarm, ANNA)


@pytest.mark.parametrize("writer", ["owner", "another member"])
async def test_a_command_change_after_a_fire_during_a_write_is_counted(engine, swarm, clock, monkeypatch, writer):
    """The quiet window fires Boris's edit while another write of the file is in flight; that write's event lands
    after the fire, the next sure pass takes the file as the new run's baseline, then a command changes the file.
    The write's event does not explain the command's change: it is counted, and the editor's turn end does not settle
    the run before the quiet window (the owner's write: changes: 1; Carol's: changes: 2)."""
    carol = swarm.add_member("Carol King", language="en")
    ws = await _real_workspace(swarm)
    swarm.runtimes[BORIS] = Turn()
    swarm.runtimes[carol] = Turn()
    swarm.config.agents.trigger_wake_seconds = 0
    (ws.root / "a.md").write_text("v0", encoding="utf-8")
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    await ws.write(BORIS, "a.md", "boris")
    clock.advance(1)
    assert await engine._evaluate_pass() == 0
    clock.advance(SETTLE + 1)
    task, go = await _held_write(ws, monkeypatch, ANNA if writer == "owner" else carol, "the edit in flight, longer")
    assert await engine._evaluate_pass() == 1                  # Boris's edit, by the quiet window
    go.set()
    await task
    clock.advance(1)
    assert await engine._evaluate_pass() == 0                  # sure: the new run's baseline
    (ws.root / "a.md").write_text("a command's change, of another size", encoding="utf-8")
    clock.advance(1)
    assert await engine._evaluate_pass() == 0
    swarm.runtimes[carol].state = AgentStatus.IDLE
    clock.advance(1)
    assert await engine._evaluate_pass() == 0                  # a change no event explains: the quiet window decides
    clock.advance(SETTLE)
    assert await engine._evaluate_pass() == 1
    expected = " File a.md was changed (changes: 1);" if writer == "owner" else (
        " File a.md was changed (changes: 2, by Carol King);")
    assert last_changes(swarm, ANNA).startswith(expected)
    clock.advance(SETTLE + 1)
    assert await engine._evaluate_pass() == 0 and engine.pending(trig["id"]) == []


@pytest.mark.parametrize("editor", [BORIS, ANNA])
async def test_an_event_taken_by_an_unsure_pass_does_not_cancel_a_command_change(engine, swarm, clock, editor):
    """A command's change, then message() fires the trigger in a pass that takes an edit's event before its probe
    shows the write: the absorbed notice still counts the command's change (Boris's edit: 2 changes; the owner's own:
    1), and nothing is left for later."""
    ws = await _real_workspace(swarm)
    swarm.runtimes[BORIS] = Turn()
    swarm.config.agents.trigger_wake_seconds = 0
    (ws.root / "a.md").write_text("v0", encoding="utf-8")
    trig = engine.create(ANNA, "file_changed('a.md') or message()", once=False)
    clock.advance(1)
    (ws.root / "a.md").write_text("a command's change", encoding="utf-8")
    assert await engine._evaluate_pass() == 0 and engine.pending(trig["id"]) == [("a.md", 1, 0.0)]
    clock.advance(1)
    add_message(swarm, BORIS, [ANNA], "see a.md", clock.now)
    assert await _pass_probed_before(engine, ws.write(editor, "a.md", "an edit")) == 1
    expected = "(changes: 2, by Борис Иванов" if editor == BORIS else "(changes: 1 "
    assert last_changes(swarm, ANNA).startswith(" File a.md is still changing " + expected)
    for _ in range(2):
        clock.advance(SETTLE + 1)
        assert await engine._evaluate_pass() == 0
    assert engine.pending(trig["id"]) == []


async def test_the_owners_edits_in_unsure_passes_do_not_restart_the_cap(engine, swarm, clock):
    """A command's change, then the owner edits the file every 50 s, each event taken by a pass unsure of the file:
    the command's change is told by the cap, [agents] file_max_wait_seconds after it."""
    ws = await _real_workspace(swarm)
    swarm.config.agents.trigger_wake_seconds = 0
    (ws.root / "a.md").write_text("v0", encoding="utf-8")
    engine.create(ANNA, "file_changed('a.md')", once=False)
    (ws.root / "a.md").write_text("a command's change", encoding="utf-8")
    assert await engine._evaluate_pass() == 0
    fired_at = None
    for i in range(1, 13):
        clock.advance(48)
        fired = await _pass_probed_before(engine, ws.write(ANNA, "a.md", f"mine {i}"))
        clock.advance(2)
        if fired + await engine._evaluate_pass():
            fired_at = i * 50
            break
    assert fired_at == 300
    assert last_changes(swarm, ANNA).startswith(" File a.md is still changing (changes: 1 ")


async def test_two_edits_between_two_passes_do_not_hide_a_command_change(engine, swarm, clock):
    """A command's change seen by a pass, then Boris's and the owner's edits before the next one: the count is
    Boris's edit plus the command's change, and the end of Boris's turn does not settle a run holding a change no
    event explains."""
    ws = await _real_workspace(swarm)
    turn = swarm.runtimes[BORIS] = Turn()
    swarm.config.agents.trigger_wake_seconds = 0
    (ws.root / "a.md").write_text("v0", encoding="utf-8")
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    (ws.root / "a.md").write_text("a command's change", encoding="utf-8")
    assert await engine._evaluate_pass() == 0
    await ws.write(BORIS, "a.md", "boris")
    await ws.write(ANNA, "a.md", "anna, right after")
    turn.state = AgentStatus.IDLE
    clock.advance(1)
    assert await engine._evaluate_pass() == 0 and engine.pending(trig["id"]) == [("a.md", 2, 0.0)]
    clock.advance(SETTLE)
    assert await engine._evaluate_pass() == 1
    assert last_changes(swarm, ANNA).startswith(" File a.md was changed (changes: 2, by Борис Иванов)")


async def test_a_trigger_created_while_a_write_is_in_flight_tells_that_write(engine, swarm, clock, monkeypatch):
    """Boris's write has put its bytes in the file but not yet emitted its event when the trigger is created: the
    creation's stat is not taken as the baseline, so that edit is told."""
    ws = await _real_workspace(swarm)
    swarm.runtimes[BORIS] = Turn(AgentStatus.IDLE)
    swarm.config.agents.trigger_wake_seconds = 0
    (ws.root / "a.md").write_text("v0", encoding="utf-8")
    task, go = await _held_write(ws, monkeypatch, BORIS, "boris's edit", phase="written")
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    go.set()
    await task
    clock.advance(1)
    assert await engine._evaluate_pass() == 1
    assert last_changes(swarm, ANNA).startswith(" File a.md was changed (changes: 1, by Борис Иванов)")
    clock.advance(SETTLE + 1)
    assert await engine._evaluate_pass() == 0 and engine.pending(trig["id"]) == []


# --- a randomized model check of the file_changed() accounting ------------------------------------
# Random schedules of write_file edits (whose stores are stepped by hand: started, truncated, written, returned,
# event), command writes, turns, messages and passes split around their probe, against the real Workspace and
# engine. The oracle follows the intervals between sure probes (no write in flight at the pass's mark or since):
# every other actor's event is told, every interval with a command's write and no event is one change, nothing is
# told twice, the editors' shortcut never settles a run holding such an interval, and nothing is left pending.

_MC_ACTIVE = ("started", "truncated", "written", "returning")


class _ModelCheck:
    def __init__(self, root: Any, seed: int, monkeypatch: Any):
        import random

        self.rnd = random.Random(seed)
        self.root, self.monkeypatch = root, monkeypatch
        self.settle, self.max_wait = self.rnd.choice([(SETTLE, 300.0), (10.0, 30.0), (0.0, 300.0), (SETTLE, 0.0)])
        self.with_msg = self.rnd.random() < 0.35
        self.violations: list[str] = []

    async def setup(self) -> None:
        from conftest import FakeSwarm

        from dreamteam import workspace as workspace_mod

        self.swarm = s = FakeSwarm(self.root)
        self.clock = Clock()
        for name in (ANNA, BORIS, "Carol"):
            s.add_member(name, language="en")
        s.runtimes = {BORIS: Turn(AgentStatus.IDLE), "Carol": Turn(AgentStatus.IDLE)}
        s.config.agents.file_settle_seconds = self.settle
        s.config.agents.file_max_wait_seconds = self.max_wait
        s.config.agents.trigger_wake_seconds = 0
        s.workspace = self.ws = await _real_workspace(s)
        self.recs: list[dict] = []
        check = self

        async def store(_ws, p, rel, data):
            rec = next(r for r in check.recs if r["data"] == data)
            for state, content in (("started", None), ("truncated", b""), ("written", data)):
                rec["state"] = state
                if check.pass_ is not None:
                    check.pass_["started"] = True
                await rec["gate"].wait()
                rec["gate"].clear()
                if content is not None:
                    p.write_bytes(content)
                    check.touch()
            rec["state"] = "returning"

        self.monkeypatch.setattr(workspace_mod.Workspace, "_store", store)
        self.path = self.ws.root / "a.md"
        self.path.write_bytes(b"init\n")
        self.touch()
        self.engine = s.triggers = TriggerEngine(s)
        self.engine.clock = self.clock
        s.bus.listen(self.on_event)
        self.fires: list[dict] = []
        self.others: list[int] = []      # event ids of other actors' edits
        self.commands = 0
        self.open = [0, 0]               # since the last sure probe: events (any actor), command writes
        self.runs: list[list[tuple[int, int]]] = [[]]
        self.last_sig = self.sig()
        self.pass_: dict | None = None
        self.msg = False
        self.info: dict | None = None
        trig = self.engine.create(ANNA, "file_changed('a.md')" + (" or message()" if self.with_msg else ""),
                                  once=False)
        self.tid = trig["id"]
        self.prev_fire = s.db.scalar("SELECT MAX(id) FROM events")
        original = self.engine._fire

        def fire(row, now, compiled):
            run = (self.engine._runs.get(row["id"]) or {}).get("a.md")
            self.info = None if run is None else {
                "done": run.done, "count": run.count,
                "quiet": run.last is not None and now - run.last >= self.settle,
                "cap": self.max_wait > 0 and run.first is not None and now - run.first >= self.max_wait}
            return original(row, now, compiled)

        self.monkeypatch.setattr(self.engine, "_fire", fire)

    def touch(self) -> None:
        ns = int(round(self.clock.now * 1e7)) * 100
        os.utime(self.path, ns=(ns, ns))

    def sig(self) -> tuple[int, int]:
        st = os.stat(self.path)
        return st.st_mtime_ns, st.st_size

    def active(self) -> list[dict]:
        return [r for r in self.recs if r["state"] in _MC_ACTIVE]

    def on_event(self, event: dict) -> None:
        if event.get("type") == "file.changed":
            rec = next((r for r in self.recs if r["state"] == "returning" and r["actor"] == event["actor"]), None)
            if rec is None:
                self.violations.append(f"event {event['id']} of no write returning")
            else:
                rec["state"] = "done"
            self.open[0] += 1
            if event["actor"] != ANNA:
                self.others.append(event["id"])
        elif event.get("type") == "trigger.fired":
            files = {f["path"]: f for f in event["data"].get("files") or []}
            self.fires.append({"id": event["id"], "item": files.get("a.md"), "info": self.info, "msg": self.msg})

    async def drain(self) -> None:
        for _ in range(40):
            await asyncio.sleep(0)

    async def step(self) -> None:
        self.clock.advance(0.001)
        r = self.rnd.random()
        if r < 0.20:
            actor = self.rnd.choice([BORIS, BORIS, "Carol", ANNA])
            if actor != ANNA and self.swarm.runtimes[actor].state != AgentStatus.WORKING:
                return
            data = f"{actor[0]}{len(self.recs)}:".encode() + b"x" * (len(self.recs) % 7 + 1)
            rec = {"actor": actor, "data": data, "state": "requested", "gate": asyncio.Event()}
            self.recs.append(rec)
            rec["task"] = asyncio.ensure_future(self.ws.write(actor, "a.md", data.decode()))
            rec["task"].add_done_callback(lambda task, rec=rec: self.write_done(task, rec))
            await self.drain()
        elif r < 0.40:
            act = self.active()
            if act and act[0]["state"] != "returning":
                act[0]["gate"].set()
            await self.drain()
        elif r < 0.47:
            self.commands += 1
            self.path.write_bytes(f"cmd{self.commands}\n".encode() * (self.commands % 3 + 1))
            self.touch()
            self.open[1] += 1
        elif r < 0.53:
            self.swarm.runtimes[self.rnd.choice([BORIS, "Carol"])].state = AgentStatus.WORKING
        elif r < 0.59:
            actor = self.rnd.choice([BORIS, "Carol"])
            if not any(rec["actor"] == actor and rec["state"] != "done" for rec in self.recs):
                self.swarm.runtimes[actor].state = AgentStatus.IDLE
        elif r < 0.66:
            self.begin()
        elif r < 0.73:
            self.probe()
        elif r < 0.80:
            self.end()
        elif r < 0.88:
            self.whole_pass()
        elif r < 0.97:
            self.clock.advance(self.rnd.choice([0.5, 2, 5, 20, self.settle + 1, self.settle / 2, 150, 299, 301]))
        elif self.with_msg:
            add_message(self.swarm, BORIS, [ANNA], "see the file", self.clock.now)
            self.msg = True

    def write_done(self, task: Any, rec: dict) -> None:
        if task.cancelled() or task.exception() is not None:
            self.violations.append(f"write {rec['data']!r} failed: {task.exception()!r}")
            rec["state"] = "done"

    def whole_pass(self) -> None:
        self.begin()
        self.probe()
        self.end()

    def begin(self) -> None:
        if self.pass_ is None:
            paths, files = self.engine._watched_paths()
            self.engine._write_mark = self.engine._take_write_mark()
            self.pass_ = {"paths": paths, "files": files, "started": bool(self.active()), "probed": False}

    def probe(self) -> None:
        if self.pass_ is not None and not self.pass_["probed"]:
            self.engine._probes, self.engine._keys = self.engine._probe_files(self.pass_["paths"], self.pass_["files"])
            self.pass_.update(probed=True, snap=(list(self.open), self.sig()))

    def end(self) -> None:
        if self.pass_ is None or not self.pass_["probed"]:
            return
        ps, self.pass_ = self.pass_, None
        if not ps["started"]:                                  # a sure pass: close the interval it probed
            (k, u), sig = ps["snap"]
            if sig != self.last_sig:
                self.runs[-1].append((k, u))
                self.open = [self.open[0] - k, self.open[1] - u]
                self.last_sig = sig
        before = len(self.fires)
        self.engine.evaluate_all()
        self.engine._probes, self.engine._keys, self.engine._write_mark = {}, {}, None
        for fire in self.fires[before:]:
            self.check_fire(fire)
            self.runs.append([])
            self.msg = False

    def check_fire(self, fire: dict) -> None:
        count = fire["item"]["changes"] if fire["item"] else 0
        window = [i for i in self.others if self.prev_fire < i < fire["id"]]
        if count < len(window):
            self.violations.append(f"fire {fire['id']}: changes {count} < {len(window)} edits of others")
        if count == 0 and not fire["msg"]:
            self.violations.append(f"fire {fire['id']}: no change and no message")
        info = fire["info"]
        if (info and info["done"] and info["count"] > 0 and not info["quiet"] and not info["cap"]
                and self.settle > 0 and any(k == 0 and u > 0 for k, u in self.runs[-1])):
            self.violations.append(f"fire {fire['id']}: editors' shortcut over a command's change")
        self.prev_fire = fire["id"]

    async def finish(self) -> None:
        if self.pass_ is not None:
            self.probe()
            self.end()
        for _ in range(500):
            act = [r for r in self.recs if r["state"] != "done"]
            if not act:
                break
            if act[0]["state"] in _MC_ACTIVE[:3]:
                act[0]["gate"].set()
            await self.drain()
        for name in (BORIS, "Carol"):
            self.swarm.runtimes[name].state = AgentStatus.IDLE
        for advance in (0, 0, self.settle + 1, 0, self.max_wait + 1, 0, self.settle + 1, 0):
            self.clock.advance(advance)
            self.whole_pass()
        told = sum(f["item"]["changes"] for f in self.fires if f["item"])
        ideal = len(self.others) + sum(1 for run in self.runs for k, u in run if k == 0 and u > 0)
        if told < ideal:
            self.violations.append(f"told {told} < {ideal} (edits of others + command-only intervals)")
        if told > len(self.others) + self.commands:
            self.violations.append(f"told {told} > {len(self.others)} edits of others + {self.commands} commands")
        if self.engine.pending(self.tid):
            self.violations.append(f"pending at the end: {self.engine.pending(self.tid)}")
        self.swarm.db.close()


async def test_a_randomized_model_check_of_the_file_changed_accounting(tmp_path, monkeypatch):
    """200 seeded random schedules (see above); any violation names its seed and schedule's findings."""
    failures = []
    for seed in range(200):
        check = _ModelCheck(tmp_path / f"s{seed}", seed, monkeypatch)
        await check.setup()
        for _ in range(check.rnd.randint(15, 80)):
            await check.step()
        await check.finish()
        if check.violations:
            failures.append((seed, check.violations))
    assert failures == []


@pytest.mark.parametrize("written",["./docs/x.md", "docs/sub/../x.md", "absolute"]
                         + (["DOCS/X.md"] if sys.platform == "win32" else []))
def test_the_condition_path_matches_the_path_of_the_events(engine, swarm, clock, written):
    path = str(swarm.config.workspace_dir / "docs" / "x.md") if written == "absolute" else written
    swarm.runtimes[BORIS] = Turn()
    trig = engine.create(ANNA, f"file_changed({path!r})", once=False)
    edit(swarm, BORIS, "docs/x.md", "1")
    edit(swarm, BORIS, "docs/y.md", "1")               # another file
    engine.evaluate_all()
    assert engine.pending(trig["id"]) == [(path, 1, 0.0)]


@pytest.mark.parametrize("owner, editor, lang", [(ANNA, BORIS, "en"), (BORIS, ANNA, "ru")])
def test_file_notice_renders_in_both_languages(engine, swarm, clock, owner, editor, lang):
    swarm.runtimes[editor] = Turn()
    trig = engine.create(owner, "file_changed('a.md')", note="N")
    edit(swarm, editor, "a.md", "1")
    engine.evaluate_all()
    clock.advance(90)
    assert engine.evaluate_all() == 1
    text = swarm.db.scalar("SELECT text FROM notifications WHERE recipient = ?", (owner,))
    by = prompts.t(lang, "trigger_file_by", names=editor)
    changes = prompts.t(lang, "trigger_file_settled", path="a.md", count=1, by=by, quiet=fmt_duration(90, lang))
    assert text == prompts.TEXTS[lang]["n_trigger_files"].format(id=trig["id"], condition="file_changed('a.md')",
                                                                 changes=changes, note="N")
    assert text == {"en": "Trigger #1 fired: file_changed('a.md'). File a.md was changed (changes: 1, by Борис "
                          "Иванов); the last change was 1 min ago. N",
                    "ru": "Сработал триггер #1: file_changed('a.md'). Файл a.md изменён (изменений: 1; кто менял: "
                          "Anna Smith); последнее изменение — 1 мин назад. N"}[lang]
    changing = prompts.t(lang, "trigger_file_changing", path="a.md", count=4, by=by, duration="3 min")
    assert changing == {"en": " File a.md is still changing (changes: 4, by Борис Иванов in 3 min).",
                        "ru": " Файл a.md всё ещё меняется (изменений за 3 min: 4; кто менял: Anna Smith)."}[lang]


def test_a_notice_names_at_most_five_editors(engine, swarm, clock):
    names = [swarm.add_member(f"Member {i}", language="en") for i in range(7)]
    for name in names:
        swarm.runtimes[name] = Turn()
    engine.create(ANNA, "file_changed('a.md')", once=False)
    for i, name in enumerate(names):
        edit(swarm, name, "a.md", "x" * (i + 1))
    engine.evaluate_all()
    clock.advance(SETTLE)
    assert engine.evaluate_all() == 1
    assert last_changes(swarm, ANNA) == (" File a.md was changed (changes: 7, by Member 0, Member 1, Member 2, "
                                         "Member 3, Member 4 (+2)); the last change was 1 min ago.")


class InsertingMail:
    """Mail that really stores the message (so message() triggers see it), stamped by the fake clock."""

    def __init__(self, swarm: Any, clock: Clock) -> None:
        self.swarm, self.clock, self.sent = swarm, clock, 0

    def send(self, sender: str, to: Any, text: str, reply_to: int | None = None, *, automatic: bool = False) -> dict:
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
    d = engine.create(BORIS, "file_changed('a.md')", once=False)
    assert set(engine._runs) == {d["id"]}
    swarm.db.execute("UPDATE triggers SET active = 0 WHERE id = ?", (d["id"],))
    engine.evaluate_all()
    assert engine._runs == {} and engine._marks == {}


# --- the wake limit of repeating triggers ([agents] trigger_wake_seconds, docs/SPEC.md §8) -------

WAKE = 180.0   # [agents] trigger_wake_seconds by default


def fire(swarm: Any, clock: Clock, owner: str = ANNA, after: float = 10.0) -> int:
    """A message to `owner` `after` seconds later, then a pass: one fire of its message() trigger."""
    clock.advance(after)
    add_message(swarm, BORIS, [owner], "ping", clock.now)
    return swarm.triggers.evaluate_all()


def kinds(swarm: Any, owner: str = ANNA) -> list[str]:
    return [k for k, _ in swarm.notifications_for(owner)]


def test_a_repeating_trigger_wakes_its_owner_at_most_once_per_interval(engine, swarm, clock):
    start = clock.now
    trig = engine.create(ANNA, "message()", once=False, note="Answer them.")
    assert fire(swarm, clock, after=0) == 1                # the first fire tells at once
    assert kinds(swarm) == ["n_trigger"] and swarm.signals == [ANNA]
    assert fire(swarm, clock) == 1 and fire(swarm, clock) == 1    # within the interval: fired, the notice held
    state = engine.get(trig["id"])
    assert (state["fire_count"], state["held"], state["told_at"], state["wake_at"]) == (3, 2, start, start + WAKE)
    assert kinds(swarm) == ["n_trigger"] and swarm.signals == [ANNA]
    assert events(swarm, "trigger.fired")[-1]["data"]["trigger"]["held"] == 2
    clock.advance(WAKE - 21)
    assert engine.evaluate_all() == 0 and kinds(swarm) == ["n_trigger"]   # 179 s since it told
    clock.advance(1)
    assert engine.evaluate_all() == 0                      # the interval's end: one merged notice, which wakes
    assert kinds(swarm) == ["n_trigger", "n_trigger_held"] and swarm.signals == [ANNA, ANNA]
    assert swarm.notified[-1][2] == {"id": trig["id"], "condition": "message()", "count": 2,
                                     "since": item_time(start, clock.now), "last": item_time(start + 20, clock.now),
                                     "interval": "3 min", "changes": "", "note": "Answer them."}
    text = swarm.db.scalar("SELECT text FROM notifications WHERE kind = 'n_trigger_held'")
    assert text == (f"Trigger #{trig['id']} fired again since it last told you at {item_time(start, clock.now)} "
                    f"(fires: 2, the last at {item_time(start + 20, clock.now)}): message(). A repeating trigger "
                    "wakes you at most once every 3 min. Answer them.")
    state = engine.get(trig["id"])
    assert (state["held"], state["told_at"], state["wake_at"]) == (0, clock.now, None)
    assert fire(swarm, clock) == 1 and engine.get(trig["id"])["held"] == 1   # counted from the last notice


def test_a_fire_after_the_interval_tells_at_once_with_the_held_ones(engine, swarm, clock):
    trig = engine.create(ANNA, "message()", once=False)
    fire(swarm, clock, after=0)
    fire(swarm, clock)                                     # held
    assert fire(swarm, clock, after=WAKE) == 1            # due: this fire tells, the held one with it
    assert kinds(swarm) == ["n_trigger", "n_trigger_held"]
    kw = swarm.notified[-1][2]
    assert kw["count"] == 2 and kw["last"] == item_time(clock.now, clock.now)
    assert engine.get(trig["id"])["held"] == 0 and engine.evaluate_all() == 0 and len(swarm.notified) == 2


def test_interval_zero_tells_every_fire_and_releases_the_held_ones(engine, swarm, clock):
    trig = engine.create(ANNA, "message()", once=False)
    fire(swarm, clock, after=0)
    fire(swarm, clock)                                     # held
    swarm.config.agents.trigger_wake_seconds = 0          # the limit is switched off (a hot setting)
    assert engine.get(trig["id"])["wake_at"] is None
    engine.evaluate_all()
    assert kinds(swarm) == ["n_trigger", "n_trigger_held_off"]
    text = swarm.db.scalar("SELECT text FROM notifications WHERE kind = 'n_trigger_held_off'")
    assert "(fires: 1, the last at " in text and "at most once" not in text and " 0 s" not in text
    fire(swarm, clock)
    fire(swarm, clock)
    assert kinds(swarm) == ["n_trigger", "n_trigger_held_off", "n_trigger", "n_trigger"]
    # a fire that tells with fires held while the limit is off: the same text without an interval
    swarm.config.agents.trigger_wake_seconds = WAKE
    fire(swarm, clock)                                     # held again
    swarm.config.agents.trigger_wake_seconds = 0
    assert fire(swarm, clock) == 1
    assert kinds(swarm)[-1] == "n_trigger_held_off" and swarm.notifications_for(ANNA)[-1][1]["count"] == 2
    ru = prompts.t("ru", "n_trigger_held_off", id=1, condition="message()", count=2, since="18:40", last="18:46",
                   interval="0 с", changes="", note="N")
    assert ru == ("Триггер #1 снова срабатывал после прошлого уведомления в 18:40 (срабатываний: 2, последнее в "
                  "18:46): message(). N")


def test_user_owned_and_once_triggers_are_never_held_external_members_are(engine, swarm, clock):
    engine.create(USER, "message()", once=False)
    for _ in range(3):
        fire(swarm, clock, owner=USER)
    assert kinds(swarm, USER) == ["n_trigger"] * 3        # the user asked for them and pays nothing
    once = engine.create(ANNA, "message()")
    swarm.db.execute("UPDATE triggers SET told_at = ? WHERE id = ?", (clock.now, once["id"]))
    fire(swarm, clock)
    assert kinds(swarm) == ["n_trigger"]
    ext = swarm.add_member("Ext Client", kind=AgentKind.EXTERNAL)
    trig = engine.create(ext, "message()", once=False)
    fire(swarm, clock, owner=ext)
    fire(swarm, clock, owner=ext)
    assert kinds(swarm, ext) == ["n_trigger"] and engine.get(trig["id"])["held"] == 1


def test_the_runaway_notice_is_never_held_and_counts_the_held_fires(engine, swarm, clock):
    trig = engine.create(ANNA, "message()", once=False,
                         actions=[{"type": "wake"}, {"type": "message", "to": BORIS, "text": "seen"}])
    for _ in range(BURST_FIRES):
        fire(swarm, clock, after=0.1)
    state = engine.get(trig["id"])
    assert state["active"] is False and "looks like a loop" in state["last_error"]
    assert kinds(swarm) == ["n_trigger", "n_trigger_held"]
    kw = swarm.notified[-1][2]
    assert kw["count"] == BURST_FIRES - 1 and "looks like a loop" in kw["note"]
    assert state["held"] == 0 and state["wake_at"] is None
    assert len(swarm.mail.sent) == BURST_FIRES - 1     # only the notice was held: the actions ran at every fire


def test_every_is_limited_like_any_repeating_trigger(engine, swarm, clock):
    trig = engine.create(ANNA, "every(60)")
    for _ in range(5):
        clock.advance(60)
        assert engine.evaluate_all() == 1
    # told at 60 s; the fire at 240 s comes after the interval and tells, with the two held at 120 and 180 s
    assert kinds(swarm) == ["n_trigger", "n_trigger_held"]
    assert swarm.notified[-1][2]["count"] == 3
    state = engine.get(trig["id"])
    assert state["fire_count"] == 5 and state["held"] == 1


def test_release_held_tells_quietly_and_only_once(engine, swarm, clock):
    trig = engine.create(ANNA, "message()", once=False)
    fire(swarm, clock, after=0)
    fire(swarm, clock)
    fire(swarm, clock)
    assert engine._held_owners == {ANNA}
    signals = list(swarm.signals)
    engine.release_held(BORIS)                             # holds nothing
    engine.release_held(ANNA)                              # e.g. it woke up for a message (Swarm.render_wake)
    assert swarm.signals == signals                        # quiet: no wake-up of its own
    row = swarm.db.one("SELECT kind, data FROM notifications ORDER BY id DESC LIMIT 1")
    assert row["kind"] == "n_trigger_held" and loads(row["data"])[QUIET_KEY] is True
    assert swarm.notified[-1][2]["count"] == 2
    assert engine.get(trig["id"])["held"] == 0 and engine._held_owners == set()
    clock.advance(WAKE)
    engine.evaluate_all()
    engine.release_held(ANNA)
    assert kinds(swarm) == ["n_trigger", "n_trigger_held"]


def test_the_interval_end_and_a_wake_up_in_the_same_tick_tell_once(engine, swarm, clock):
    engine.create(ANNA, "message()", once=False)
    fire(swarm, clock, after=0)
    fire(swarm, clock)
    clock.advance(WAKE)
    engine.release_held(ANNA)                              # the owner's wake-up first...
    engine.evaluate_all()                                  # ...then the pass of the same tick
    assert kinds(swarm) == ["n_trigger", "n_trigger_held"]
    fire(swarm, clock)                                     # held again
    clock.advance(WAKE)
    engine.evaluate_all()                                  # the pass first...
    engine.release_held(ANNA)                              # ...then the wake-up
    assert kinds(swarm) == ["n_trigger", "n_trigger_held", "n_trigger_held"]


def test_held_fires_survive_a_restart(engine, swarm, clock):
    trig = engine.create(ANNA, "message()", once=False)
    fire(swarm, clock, after=0)
    fire(swarm, clock)
    other = TriggerEngine(swarm)                           # a restart: rebuilt from the triggers table
    other.clock = clock
    swarm.triggers = other
    assert other._held_owners == {ANNA} and other.get(trig["id"])["held"] == 1
    clock.advance(WAKE)
    other.evaluate_all()
    assert kinds(swarm) == ["n_trigger", "n_trigger_held"]


def test_a_clock_gone_back_neither_holds_nor_keeps_held(engine, swarm, clock):
    engine.create(ANNA, "message()", once=False)
    fire(swarm, clock, after=0)
    fire(swarm, clock)
    clock.advance(-3600)
    engine.evaluate_all()                                  # told "in the future": released now
    assert kinds(swarm) == ["n_trigger", "n_trigger_held"]
    clock.advance(-3600)
    fire(swarm, clock, after=0)
    assert kinds(swarm) == ["n_trigger", "n_trigger_held", "n_trigger"]


def test_held_fires_of_a_member_who_left_are_not_released(engine, swarm, clock):
    trig = engine.create(ANNA, "message()", once=False)
    fire(swarm, clock, after=0)
    fire(swarm, clock)
    swarm.db.execute("UPDATE agents SET status = 'retired' WHERE name = ?", (ANNA,))
    swarm.db.execute("UPDATE triggers SET active = 0 WHERE owner = ?", (ANNA,))   # as Swarm.retire does
    clock.advance(WAKE)
    engine.evaluate_all()
    engine.release_held(ANNA)
    state = engine.get(trig["id"])
    assert kinds(swarm) == ["n_trigger"] and state["held"] == 1 and state["wake_at"] is None


def test_held_file_changes_add_up_in_the_merged_notice(engine, swarm, clock):
    """Items 2 and 3 together: each held fire of a file_changed() trigger adds its changes and editors to
    held_files, and the merged notice shows their sum (the editors have no runtime: each run settles at once)."""
    carl = swarm.add_member("Carl Lee", language="en")
    trig = engine.create(ANNA, "file_changed('a.md')", once=False)
    edit(swarm, BORIS, "a.md", "1")
    assert engine.evaluate_all() == 1                      # told
    clock.advance(10)
    first = clock.now
    edit(swarm, BORIS, "a.md", "22")
    edit(swarm, BORIS, "a.md", "333")
    assert engine.evaluate_all() == 1                      # held
    clock.advance(10)
    edit(swarm, carl, "a.md", "4444")
    assert engine.evaluate_all() == 1                      # held
    assert kinds(swarm) == ["n_trigger_files"] and engine.get(trig["id"])["held"] == 2
    held = loads(swarm.db.scalar("SELECT held_files FROM triggers WHERE id = ?", (trig["id"],)), {})
    assert held == {"a.md": {"count": 3, "by": [BORIS, carl], "first": first, "last": clock.now, "settled": True}}
    clock.advance(WAKE - 20)
    engine.evaluate_all()
    kw = swarm.notified[-1][2]
    assert kinds(swarm) == ["n_trigger_files", "n_trigger_held"] and kw["count"] == 2
    assert kw["changes"] == (" File a.md was changed (changes: 3, by Борис Иванов, Carl Lee);"
                             " the last change was 2 min ago.")
    # a fire after the interval tells with the held changes merged into its own
    clock.advance(10)
    edit(swarm, BORIS, "a.md", "55555")
    assert engine.evaluate_all() == 1                      # held (told 10 s ago)
    clock.advance(WAKE)
    edit(swarm, carl, "a.md", "666666")
    assert engine.evaluate_all() == 1
    kw = swarm.notified[-1][2]
    assert kinds(swarm)[-1] == "n_trigger_held" and kw["count"] == 2
    assert kw["changes"] == (" File a.md was changed (changes: 2, by Борис Иванов, Carl Lee);"
                             " the last change was 0 s ago.")
    assert swarm.db.scalar("SELECT held_files FROM triggers WHERE id = ?", (trig["id"],)) is None


@pytest.mark.parametrize("owner, lang", [(ANNA, "en"), (BORIS, "ru")])
def test_held_notice_renders_in_both_languages(engine, swarm, clock, owner, lang):
    start = clock.now
    trig = engine.create(owner, "message()", once=False, note="N")
    fire(swarm, clock, owner=owner, after=0)
    fire(swarm, clock, owner=owner)
    clock.advance(WAKE)
    engine.evaluate_all()
    text = swarm.db.scalar("SELECT text FROM notifications WHERE kind = 'n_trigger_held'")
    since, last = item_time(start, clock.now), item_time(start + 10, clock.now)
    assert text == prompts.TEXTS[lang]["n_trigger_held"].format(
        id=trig["id"], condition="message()", count=1, since=since, last=last, interval=fmt_duration(WAKE, lang),
        changes="", note="N")
    assert text == {"en": f"Trigger #1 fired again since it last told you at {since} (fires: 1, the last at {last}): "
                          "message(). A repeating trigger wakes you at most once every 3 min. N",
                    "ru": f"Триггер #1 снова срабатывал после прошлого уведомления в {since} (срабатываний: 1, "
                          f"последнее в {last}): message(). Повторяющийся триггер будит не чаще раза в 3 мин. N"}[lang]


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
