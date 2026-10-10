"""Triggers: conditions over the swarm state (a tiny whitelisted expression DSL) and the engine
that evaluates them and fires actions on a false -> true edge (docs/SPEC.md §8)."""

from __future__ import annotations

import ast
import asyncio
import collections
import difflib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from .db import dumps, loads
from .models import USER, Status, ToolError

log = logging.getLogger(__name__)

MAX_CONDITION_CHARS = 500
MAX_CONDITION_NODES = 200
MIN_EVERY_SECONDS = 10.0
MAX_NOTE_CHARS = 4000
MAX_ERROR_CHARS = 1000
EVAL_ERROR_PREFIX = "evaluation error: "
DIRTY_PREFIXES = ("task.", "message.", "file.", "goal.", "agent.")
STATUS_NAMES = frozenset(Status.ALL)
MAX_ID = 2 ** 63 - 1                 # SQLite INTEGER range
# a repeating trigger that fires BURST_FIRES times within BURST_SECONDS is in a loop (e.g. two
# message() triggers answering each other) and is deactivated
BURST_FIRES = 20
BURST_SECONDS = 10.0
# functions whose value is relative to the trigger's reference point (creation or last fire)
RESET_FUNCTIONS = frozenset({"message", "comment", "file_changed", "every"})
# the parameter of these functions that names a member (resolved like every tool does at creation)
NAME_PARAMS = {"message": "sender", "comment": "sender", "count": "assignee", "agent_status": "name"}

GRAMMAR = (
    "Condition syntax: a Python-like expression with and/or/not, comparisons (== != < <= > >= in, not in), "
    "literals (numbers, \"strings\", True/False/None, tuples like (1, 2)), bare status names "
    "(todo in_progress waiting review done failed cancelled) and these functions (arguments must be literals):\n"
    "  status(id), done(*ids), any_done(*ids), finished(*ids), failed(*ids), assignee(id),\n"
    "  count(status=None, tag=None, assignee=None, parent=None), all_done(tag=None, parent=None),\n"
    "  message(sender=None, contains=None), comment(task=None, sender=None, contains=None),\n"
    "  file_exists(path), file_changed(path), after(\"2026-10-09 18:00\"), elapsed(seconds),\n"
    "  every(seconds >= 10), agent_status(name), goal_done()\n"
    "Example: done(1, 2) and status(3) in (failed, cancelled)"
)

ACTIONS_HELP = (
    'Actions: {"type": "wake"} | {"type": "message", "to": "Name" or ["A", "B"], "text": "..."} | '
    '{"type": "set_status", "task_id": 1, "status": "todo"} | '
    '{"type": "create_task", "title": "...", "goal"?, "description"?, "assignee"?, "tags"?: [...], "priority"?: 0-3}'
)

DEFAULT_ACTIONS: list[dict[str, Any]] = [{"type": "wake"}]

_FORBIDDEN: dict[type, str] = {
    ast.Attribute: "attribute access (x.y)",
    ast.Subscript: "indexing (x[i])",
    ast.Lambda: "lambda",
    ast.ListComp: "comprehensions",
    ast.SetComp: "comprehensions",
    ast.DictComp: "comprehensions",
    ast.GeneratorExp: "comprehensions",
    ast.JoinedStr: "f-strings",
    ast.FormattedValue: "f-strings",
    ast.NamedExpr: "assignment (:=)",
    ast.BinOp: "arithmetic (+ - * / ...); write the number itself, e.g. 3600",
    ast.IfExp: "conditional expressions (x if c else y)",
    ast.Dict: "dict literals",
    ast.Starred: "starred arguments (*x)",
    ast.Await: "await",
}

_OP_NAMES: dict[type, str] = {ast.Is: "is", ast.IsNot: "is not"}

_COMPARE_OPS: dict[type, Callable[[Any, Any], bool]] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


def _fail(reason: str) -> ToolError:
    return ToolError(f"Invalid condition: {reason}.\n{GRAMMAR}")


def _norm(name: Any) -> str:
    return " ".join(str(name or "").split()).casefold()


@lru_cache(maxsize=256)
def parse_when(text: str) -> float:
    """ISO date/datetime -> timestamp; naive values are local time. ValueError if malformed or out of
    range (on Windows a naive datetime before 1970 or after 3000 raises OSError in timestamp())."""
    moment = datetime.fromisoformat(text.strip())
    try:
        return moment.timestamp()
    except (OverflowError, OSError):
        raise ValueError(f"{text!r} is out of the supported date range") from None


def safe_path(root: Path, path: str) -> Path | None:
    """Resolve `path` inside the workspace root; None if it escapes, touches .git or is malformed."""
    try:
        base = root.resolve()
        target = (base / path).resolve()
        rel = target.relative_to(base)
    except (ValueError, OSError, RuntimeError, TypeError):
        return None
    if ".git" in rel.parts:
        return None
    return target


# ---------------------------------------------------------------------------------------------
# function table


@dataclass(frozen=True)
class _FnSpec:
    usage: str
    params: tuple[str, ...] = ()       # positional-or-keyword parameter names
    kinds: tuple[str, ...] = ()        # value kind per parameter
    required: int = 0                  # how many leading parameters are required
    varargs: str | None = None         # kind of *args (functions over task ids)


FUNCTIONS: dict[str, _FnSpec] = {
    "status": _FnSpec("status(id)", ("id",), ("id",), 1),
    "done": _FnSpec("done(*ids)", varargs="id"),
    "any_done": _FnSpec("any_done(*ids)", varargs="id"),
    "finished": _FnSpec("finished(*ids)", varargs="id"),
    "failed": _FnSpec("failed(*ids)", varargs="id"),
    "assignee": _FnSpec("assignee(id)", ("id",), ("id",), 1),
    "count": _FnSpec("count(status=None, tag=None, assignee=None, parent=None)",
                     ("status", "tag", "assignee", "parent"), ("statuses", "opt_str", "opt_str", "opt_id")),
    "all_done": _FnSpec("all_done(tag=None, parent=None)", ("tag", "parent"), ("opt_str", "opt_id")),
    "message": _FnSpec("message(sender=None, contains=None)", ("sender", "contains"), ("opt_str", "opt_str")),
    "comment": _FnSpec("comment(task=None, sender=None, contains=None)",
                       ("task", "sender", "contains"), ("opt_id", "opt_str", "opt_str")),
    "file_exists": _FnSpec("file_exists(path)", ("path",), ("path",), 1),
    "file_changed": _FnSpec("file_changed(path)", ("path",), ("path",), 1),
    "after": _FnSpec("after(when)", ("when",), ("when",), 1),
    "elapsed": _FnSpec("elapsed(seconds)", ("seconds",), ("seconds",), 1),
    "every": _FnSpec("every(seconds)", ("seconds",), ("period",), 1),
    "agent_status": _FnSpec("agent_status(name)", ("name",), ("str",), 1),
    "goal_done": _FnSpec("goal_done()"),
}


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_id(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= MAX_ID


@dataclass(frozen=True)
class _Marks:
    """What a trigger had already seen at its reference point (creation or last fire). Timestamps
    alone are ambiguous: on Windows time.time() and file mtimes advance in ~15.6 ms steps, so a
    message or file write right after the reference can carry exactly the same timestamp. Row ids
    and file signatures are unambiguous. Kept in memory; without them the functions fall back to
    timestamps (e.g. after a restart)."""

    message_id: int | None = None                                   # max messages.id
    comment_id: int | None = None                                   # max comments.id
    files: dict[str, tuple[int, int] | None] = field(default_factory=dict)  # path -> (mtime_ns, size)


def _file_signature(root: Path, path: str) -> tuple[int, int] | None:
    target = safe_path(root, path)
    if target is None:
        return None
    try:
        st = os.stat(target)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


# ---------------------------------------------------------------------------------------------
# evaluation environment: the functions read the swarm state directly from the DB


class _Env:
    def __init__(self, swarm: Any, owner: str, created_at: float, reference: float, now: float,
                 marks: _Marks | None = None):
        self.swarm = swarm
        self.db = swarm.db
        self.owner = owner
        self.created_at = created_at
        self.reference = reference      # creation or last fire
        self.now = now
        self.marks = marks              # what was already seen at the reference point

    def _statuses(self, ids: tuple[int, ...]) -> dict[int, str]:
        marks = ",".join("?" for _ in ids)
        rows = self.db.all(f"SELECT id, status FROM tasks WHERE id IN ({marks})", ids)
        return {r["id"]: r["status"] for r in rows}

    def _tasks(self, tag: str | None = None, parent: int | None = None) -> list[Any]:
        sql, params = "SELECT id, status, assignee, parent_id, tags FROM tasks", []
        if parent is not None:
            sql += " WHERE parent_id = ?"
            params.append(parent)
        rows = self.db.all(sql, params)
        if tag is not None:
            key = _norm(tag)
            rows = [r for r in rows if key in {_norm(t) for t in loads(r["tags"], []) or []}]
        return rows

    def fn_status(self, id: int) -> str:
        return self._statuses((id,)).get(id, "missing")

    def fn_done(self, *ids: int) -> bool:
        found = self._statuses(ids)
        return all(found.get(i) == Status.DONE for i in ids)

    def fn_any_done(self, *ids: int) -> bool:
        return Status.DONE in self._statuses(ids).values()

    def fn_finished(self, *ids: int) -> bool:
        found = self._statuses(ids)
        return all(found.get(i) in Status.TERMINAL for i in ids)

    def fn_failed(self, *ids: int) -> bool:
        return Status.FAILED in self._statuses(ids).values()

    def fn_assignee(self, id: int) -> str | None:
        return self.db.scalar("SELECT assignee FROM tasks WHERE id = ?", (id,))

    def fn_count(self, status: tuple[str, ...] | None = None, tag: str | None = None,
                 assignee: str | None = None, parent: int | None = None) -> int:
        rows = self._tasks(tag, parent)
        if status is not None:
            rows = [r for r in rows if r["status"] in status]
        if assignee is not None:
            rows = [r for r in rows if _norm(r["assignee"]) == _norm(assignee)]
        return len(rows)

    def fn_all_done(self, tag: str | None = None, parent: int | None = None) -> bool:
        rows = self._tasks(tag, parent)
        return bool(rows) and all(r["status"] == Status.DONE for r in rows)

    def _since(self, mark: int | None, column: str) -> tuple[str, float]:
        """SQL filter for rows that arrived after the reference point: by id when known."""
        if mark is not None:
            return f"{column}id > ?", mark
        return f"{column}ts > ?", self.reference

    def fn_message(self, sender: str | None = None, contains: str | None = None) -> bool:
        where, value = self._since(self.marks.message_id if self.marks else None, "m.")
        rows = self.db.all(
            "SELECT m.sender, m.text FROM inbox i JOIN messages m ON m.id = i.message_id"
            f" WHERE i.recipient = ? AND {where} ORDER BY m.id",
            (self.owner, value),
        )
        return any(_matches(r["sender"], r["text"], sender, contains) for r in rows)

    def fn_comment(self, task: int | None = None, sender: str | None = None, contains: str | None = None) -> bool:
        where, value = self._since(self.marks.comment_id if self.marks else None, "")
        sql, params = f"SELECT author, text FROM comments WHERE {where}", [value]
        if task is not None:
            sql += " AND task_id = ?"
            params.append(task)
        owner = _norm(self.owner)
        return any(
            _norm(r["author"]) != owner and _matches(r["author"], r["text"], sender, contains)
            for r in self.db.all(sql, params)
        )

    def _file(self, path: str) -> Path | None:
        return safe_path(self.swarm.config.workspace_dir, path)

    def fn_file_exists(self, path: str) -> bool:
        target = self._file(path)
        return bool(target and target.exists())

    def fn_file_changed(self, path: str) -> bool:
        target = self._file(path)
        if target is None:
            return False
        try:
            st = os.stat(target)
        except OSError:
            return False
        if self.marks is not None and path in self.marks.files:
            return (st.st_mtime_ns, st.st_size) != self.marks.files[path]   # appeared or modified
        return st.st_mtime > self.reference

    def fn_after(self, when: str) -> bool:
        return self.now >= parse_when(when)

    def fn_elapsed(self, seconds: float) -> bool:
        return self.now - self.created_at >= seconds

    def fn_every(self, seconds: float) -> bool:
        return self.now - self.reference >= seconds

    def fn_agent_status(self, name: str) -> str:
        key = _norm(name)
        for row in self.db.all("SELECT name, status FROM agents"):
            if _norm(row["name"]) == key:
                return row["status"]
        return "missing"

    def fn_goal_done(self) -> bool:
        goal = self.db.get("goal")
        return isinstance(goal, dict) and goal.get("status") == "done"


def _actor_key(name: Any) -> str:
    key = _norm(name)
    return USER if key in (USER, "пользователь") else key


def _matches(author: str | None, text: str | None, sender: str | None, contains: str | None) -> bool:
    if sender is not None and _actor_key(author) != _actor_key(sender):
        return False
    if contains is not None and contains.casefold() not in (text or "").casefold():
        return False
    return True


# ---------------------------------------------------------------------------------------------
# compiler: validated AST -> closure tree

Evaluator = Callable[[_Env], Any]


@dataclass(frozen=True)
class CompiledCondition:
    source: str
    evaluate: Evaluator
    every: tuple[float, ...]          # periods of every() calls; non-empty -> periodic trigger
    functions: frozenset[str]
    files: tuple[str, ...] = ()       # paths watched by file_changed()

    @property
    def periodic(self) -> bool:
        return bool(self.every)

    @property
    def resets(self) -> bool:
        """True when a fire moves the reference point the condition is measured from."""
        return bool(self.functions & RESET_FUNCTIONS)


class _Compiler:
    def __init__(self, workspace_root: Path):
        self.root = workspace_root
        self.every: list[float] = []
        self.files: list[str] = []
        self.functions: set[str] = set()

    def compile(self, source: str) -> CompiledCondition:
        if source is not None and not isinstance(source, str):
            raise _fail(f"it must be a string expression, got {type(source).__name__}, e.g. \"done(3)\"")
        if not source or not source.strip():
            raise _fail("the condition is empty")
        source = source.strip()
        if len(source) > MAX_CONDITION_CHARS:
            raise _fail(f"it is {len(source)} characters long, the limit is {MAX_CONDITION_CHARS}")
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as e:
            where = f" at column {e.offset}" if e.offset else ""
            hint = " (use and / or / not instead of && || !)" if re.search(r"&&|\|\||!(?!=)", source) else ""
            raise _fail(f"syntax error{where}: {e.msg}{hint}") from None
        except (ValueError, RecursionError, MemoryError) as e:
            raise _fail(f"cannot parse it ({e})") from None
        nodes = sum(1 for n in ast.walk(tree) if isinstance(n, (ast.expr, ast.keyword)))
        if nodes > MAX_CONDITION_NODES:
            raise _fail(f"it has {nodes} elements, the limit is {MAX_CONDITION_NODES}")
        fn = self.expr(tree.body)
        if not self.functions:
            raise _fail("it calls no function, so it never changes; use e.g. done(3) or every(600)")
        return CompiledCondition(source, fn, tuple(self.every), frozenset(self.functions),
                                 tuple(dict.fromkeys(self.files)))

    # --- expressions --------------------------------------------------------------------------
    def expr(self, node: ast.expr) -> Evaluator:
        if isinstance(node, ast.BoolOp):
            return self.boolop(node)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            inner = self.expr(node.operand)
            return lambda env: not inner(env)
        if isinstance(node, ast.Compare):
            return self.compare(node)
        if isinstance(node, ast.Call):
            return self.call(node)
        value = self.literal(node)
        return lambda env: value

    def boolop(self, node: ast.BoolOp) -> Evaluator:
        parts = [self.expr(v) for v in node.values]
        if isinstance(node.op, ast.And):
            def and_(env: _Env) -> Any:
                value: Any = True
                for part in parts:
                    value = part(env)
                    if not value:
                        return value
                return value
            return and_

        def or_(env: _Env) -> Any:
            value: Any = False
            for part in parts:
                value = part(env)
                if value:
                    return value
            return value
        return or_

    def compare(self, node: ast.Compare) -> Evaluator:
        left = self.expr(node.left)
        pairs: list[tuple[Callable[[Any, Any], bool], Evaluator]] = []
        for op, right in zip(node.ops, node.comparators):
            func = _COMPARE_OPS.get(type(op))
            if func is None:
                shown = _OP_NAMES.get(type(op), type(op).__name__)
                raise _fail(f"operator '{shown}' is not supported (use == != < <= > >= in, not in)")
            pairs.append((func, self.expr(right)))

        def compare_(env: _Env) -> bool:
            a = left(env)
            for func, right_fn in pairs:
                b = right_fn(env)
                if not func(a, b):
                    return False
                a = b
            return True
        return compare_

    def call(self, node: ast.Call) -> Evaluator:
        if not isinstance(node.func, ast.Name):
            raise _fail("only the listed functions can be called, e.g. done(1)")
        name = node.func.id
        spec = FUNCTIONS.get(name)
        if spec is None:
            close = difflib.get_close_matches(name, list(FUNCTIONS), n=1, cutoff=0.6)
            hint = f" (did you mean {close[0]}?)" if close else ""
            raise _fail(f"unknown function '{name}'{hint}")
        args = []
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                raise _fail(f"{spec.usage}: starred arguments (*x) are not allowed")
            args.append(self.argument(spec, arg))
        kwargs: list[tuple[str, Any]] = []
        for kw in node.keywords:
            if kw.arg is None:
                raise _fail(f"{spec.usage}: **kwargs are not allowed")
            kwargs.append((kw.arg, self.argument(spec, kw.value)))
        pos, named = self.bind(spec, args, kwargs)
        if name == "every":
            self.every.append(float(named["seconds"]))
        elif name == "file_changed":
            self.files.append(named["path"])
        self.functions.add(name)
        method = getattr(_Env, f"fn_{name}")
        return lambda env: method(env, *pos, **named)

    def argument(self, spec: _FnSpec, node: ast.expr) -> Any:
        if isinstance(node, (ast.Call, ast.BoolOp, ast.Compare)) or (
                isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)):
            raise _fail(f"{spec.usage}: arguments must be literals (numbers, \"strings\", status names)")
        return self.literal(node)

    # --- literals -----------------------------------------------------------------------------
    def literal(self, node: ast.expr, nested: bool = False) -> Any:
        if isinstance(node, ast.Constant):
            if node.value is None or isinstance(node.value, (str, int, float, bool)):
                return node.value
            raise _fail(f"literal {node.value!r} is not supported")
        if isinstance(node, ast.Name):
            if node.id in STATUS_NAMES:
                return node.id
            raise _fail(f"unknown name '{node.id}'. Allowed bare names: status names "
                        f"({' '.join(Status.ALL)}), True, False, None; put text in quotes, e.g. \"{node.id}\"")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            operand = node.operand
            if isinstance(operand, ast.Constant) and _is_number(operand.value):
                return -operand.value if isinstance(node.op, ast.USub) else operand.value
            raise _fail("unary +/- is allowed only before a number")
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            if nested:
                raise _fail("collections may contain only literals, not other collections")
            items = [self.literal(e, nested=True) for e in node.elts]
            if isinstance(node, ast.Tuple):
                return tuple(items)
            if isinstance(node, ast.List):
                return items
            return frozenset(items)
        if isinstance(node, ast.Compare | ast.BoolOp | ast.Call) or (
                isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not)):
            raise _fail("collections may contain only literals (numbers, \"strings\", status names)")
        if isinstance(node, ast.UnaryOp):
            raise _fail("operator '~' is not allowed")
        what = _FORBIDDEN.get(type(node), f"'{type(node).__name__}' expressions")
        raise _fail(f"not allowed: {what}")

    # --- call binding and argument checks ----------------------------------------------------
    def bind(self, spec: _FnSpec, args: list[Any], kwargs: list[tuple[str, Any]]) -> tuple[tuple, dict]:
        if spec.varargs:
            if kwargs:
                raise _fail(f"{spec.usage} takes only task ids as positional arguments, e.g. done(1, 2)")
            if not args:
                raise _fail(f"{spec.usage} needs at least one task id, e.g. done(1, 2)")
            for value in args:
                self.check(spec, "ids", spec.varargs, value)
            return tuple(args), {}
        if len(args) > len(spec.params):
            raise _fail(f"{spec.usage} takes at most {len(spec.params)} argument(s), got {len(args)}")
        bound = dict(zip(spec.params, args))
        for key, value in kwargs:
            if key not in spec.params:
                allowed = ", ".join(spec.params) or "none"
                raise _fail(f"{spec.usage} got an unexpected keyword '{key}' (allowed: {allowed})")
            if key in bound:
                raise _fail(f"{spec.usage} got multiple values for '{key}'")
            bound[key] = value
        for param in spec.params[:spec.required]:
            if param not in bound:
                raise _fail(f"{spec.usage} is missing the argument '{param}'")
        for param, kind in zip(spec.params, spec.kinds):
            if param in bound:
                bound[param] = self.check(spec, param, kind, bound[param])
        return (), bound

    def check(self, spec: _FnSpec, param: str, kind: str, value: Any) -> Any:
        bad = f"{spec.usage}: '{param}' "
        if kind in ("opt_id", "opt_str", "statuses") and value is None:
            return None
        if kind in ("id", "opt_id"):
            if not _is_id(value):
                raise _fail(bad + f"must be a task id (positive integer), got {value!r}")
        elif kind in ("str", "opt_str"):
            if not isinstance(value, str) or not value.strip():
                raise _fail(bad + f"must be a non-empty \"string\", got {value!r}")
        elif kind == "statuses":
            return self.check_statuses(bad, value)
        elif kind == "path":
            if not isinstance(value, str) or not value.strip():
                raise _fail(bad + "must be a workspace path in quotes, e.g. \"report.md\"")
            if any(ord(ch) < 32 for ch in value):
                # "docs\report.md" is parsed as a Python string: \r, \n, \t... become control characters
                raise _fail(bad + f"{value!r} contains a control character; write paths with forward "
                            "slashes, e.g. \"docs/report.md\"")
            if safe_path(self.root, value) is None:
                raise _fail(bad + f"{value!r} must be a path inside the workspace (no '..', no .git)")
        elif kind == "when":
            if not isinstance(value, str):
                raise _fail(bad + "must be an ISO date/time in quotes, e.g. \"2026-10-09 18:00\"")
            try:
                datetime.fromisoformat(value.strip())
            except ValueError:
                raise _fail(bad + f"{value!r} is not an ISO date/time, use e.g. \"2026-10-09 18:00\"") from None
            try:
                parse_when(value)
            except ValueError:
                raise _fail(bad + f"{value!r} is out of the supported date range, use a date such as "
                            "\"2026-10-09 18:00\"") from None
        elif kind == "seconds":
            if not _is_number(value) or value < 0:
                raise _fail(bad + f"must be a non-negative number of seconds, got {value!r}")
        elif kind == "period":
            if not _is_number(value) or value < MIN_EVERY_SECONDS:
                raise _fail(bad + f"must be a number of seconds >= {MIN_EVERY_SECONDS:g}, got {value!r}")
        return value

    @staticmethod
    def check_statuses(bad: str, value: Any) -> tuple[str, ...]:
        items = (value,) if isinstance(value, str) else value
        if not isinstance(items, (tuple, list, frozenset)) or not items:
            raise _fail(bad + "must be a status or a tuple of statuses, e.g. (todo, in_progress)")
        unknown = [v for v in items if v not in STATUS_NAMES]
        if unknown:
            raise _fail(bad + f"unknown status {unknown[0]!r} (statuses: {', '.join(Status.ALL)})")
        return tuple(items)


# ---------------------------------------------------------------------------------------------
# actions


def _action_error(index: int, reason: str) -> ToolError:
    return ToolError(f"Invalid action #{index}: {reason}. {ACTIONS_HELP}")


def _task_id(index: int, value: Any) -> int:
    if isinstance(value, str) and value.strip().isdecimal():
        value = int(value.strip())
    if not _is_id(value):
        raise _action_error(index, f"'task_id' must be a task id (positive integer), got {value!r}")
    return value


def _text(index: int, key: str, value: Any, required: bool = True, limit: int | None = None) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise _action_error(index, f"'{key}' must be a {'non-empty ' if required else ''}string")
    if limit and len(value) > limit:
        raise _action_error(index, f"'{key}' is longer than {limit} characters")
    return value


def _normalize_action(index: int, action: Any) -> dict[str, Any]:
    if not isinstance(action, dict):
        raise _action_error(index, "each action must be an object with a 'type'")
    kind = action.get("type")
    allowed = {"wake": set(), "message": {"to", "text"}, "set_status": {"task_id", "status"},
               "create_task": {"title", "goal", "description", "assignee", "tags", "priority"}}
    if not isinstance(kind, str) or kind not in allowed:
        raise _action_error(index, f"unknown type {kind!r} (types: {', '.join(allowed)})")
    extra = set(action) - allowed[kind] - {"type"}
    if extra:
        raise _action_error(index, f"unexpected key(s) for '{kind}': {', '.join(sorted(extra))}")
    if kind == "wake":
        return {"type": "wake"}
    if kind == "message":
        return {"type": "message", "to": _recipients(index, action.get("to")),
                "text": _text(index, "text", action.get("text"))}
    if kind == "set_status":
        status = action.get("status")
        if not isinstance(status, str) or status not in STATUS_NAMES:
            raise _action_error(index, f"'status' must be one of {', '.join(Status.ALL)}, got {status!r}")
        return {"type": "set_status", "task_id": _task_id(index, action.get("task_id")), "status": status}
    return _normalize_create_task(index, action)


def _recipients(index: int, to: Any) -> str | list[str]:
    if isinstance(to, str) and to.strip():
        return to
    if isinstance(to, list) and to and all(isinstance(t, str) and t.strip() for t in to):
        return list(to)
    raise _action_error(index, "'to' must be a member name, \"all\", \"user\" or a list of names")


def _normalize_create_task(index: int, action: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "create_task", "title": _text(index, "title", action.get("title"), limit=200)}
    out["goal"] = _text(index, "goal", action.get("goal"), required=False)
    out["description"] = _text(index, "description", action.get("description"), required=False)
    assignee = action.get("assignee")
    if assignee not in (None, "") and (not isinstance(assignee, str) or not assignee.strip()):
        raise _action_error(index, "'assignee' must be a member name")
    out["assignee"] = assignee or None
    tags = action.get("tags")
    if tags is not None and (not isinstance(tags, list) or not all(isinstance(t, str) for t in tags)):
        raise _action_error(index, "'tags' must be a list of strings")
    out["tags"] = list(tags) if tags else None
    priority = action.get("priority", 1)
    if not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority <= 3:
        raise _action_error(index, f"'priority' must be 0..3, got {priority!r}")
    out["priority"] = priority
    return out


def normalize_actions(actions: Any) -> list[dict[str, Any]]:
    if actions is None or actions == []:
        return [dict(a) for a in DEFAULT_ACTIONS]
    if not isinstance(actions, list):
        raise ToolError(f"'actions' must be a list of action objects. {ACTIONS_HELP}")
    return [_normalize_action(i, a) for i, a in enumerate(actions, start=1)]


# ---------------------------------------------------------------------------------------------
# engine


def _to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"], "owner": row["owner"], "condition": row["condition"], "note": row["note"] or "",
        "actions": loads(row["actions"], []), "once": bool(row["once"]), "active": bool(row["active"]),
        "created_at": row["created_at"], "last_fired": row["last_fired"],
        "fire_count": row["fire_count"] or 0, "last_error": row["last_error"],
    }


class TriggerEngine:
    tick_seconds: float = 2.0

    def __init__(self, swarm: Any):
        self.swarm = swarm
        self.clock: Callable[[], float] = time.time
        self._dirty = asyncio.Event()
        self._evaluating = False
        self._cache: dict[str, CompiledCondition] = {}
        self._marks: dict[int, _Marks] = {}     # trigger id -> what it had seen at its reference point
        self._fires: dict[int, collections.deque[float]] = {}   # recent fire times of repeating triggers
        swarm.bus.listen(self._on_event)

    # --- conditions ---------------------------------------------------------------------------
    def compile(self, condition: str) -> CompiledCondition:
        key = condition.strip() if isinstance(condition, str) else condition
        cached = self._cache.get(key) if isinstance(key, str) else None
        if cached is None:
            cached = _Compiler(self.swarm.config.workspace_dir).compile(condition)
            if len(self._cache) > 1000:
                self._cache.clear()
            self._cache[cached.source] = cached
        return cached

    def validate(self, condition: str) -> None:
        self.compile(condition)

    def canonical_names(self, condition: str) -> str:
        """Member names in the condition (sender=, assignee=, agent_status(name)) resolved like every tool
        resolves them (a unique first name, '@Name', any case) and rewritten to the canonical full name, so
        message(sender="Anna") fires on messages of Anna Smith. An unknown member is an error now instead of
        a trigger that silently never fires. Returns the condition unchanged when no name changed."""
        source = self.compile(condition).source
        tree = ast.parse(source, mode="eval")
        changed = False
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in NAME_PARAMS):
                continue
            fn, param = node.func.id, NAME_PARAMS[node.func.id]
            index = FUNCTIONS[fn].params.index(param)
            values = [kw.value for kw in node.keywords if kw.arg == param]
            if len(node.args) > index:
                values.append(node.args[index])
            for value in values:
                if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                    continue
                try:
                    name = self.swarm.resolve_member(value.value, allow_user=fn != "agent_status",
                                                     allow_retired=True)
                except ToolError as exc:
                    raise ToolError(f"Invalid condition: {fn}() {param}: {exc}") from None
                if name != value.value:
                    value.value, changed = name, True
        return ast.unparse(tree) if changed else source

    # --- CRUD ---------------------------------------------------------------------------------
    def create(self, owner: str, condition: str, *, note: str = "", actions: list[dict] | None = None,
               once: bool = True) -> dict:
        owner = self.swarm.resolve_member(owner, allow_user=True)
        compiled = self.compile(self.canonical_names(condition))
        normalized = normalize_actions(actions)
        note = "" if note is None else str(note).strip()
        if len(note) > MAX_NOTE_CHARS:
            raise ToolError(f"The note is too long ({len(note)} characters, max {MAX_NOTE_CHARS}).")
        once = bool(once) and not compiled.periodic   # every() makes the trigger periodic
        trigger_id = self.swarm.db.insert(
            "INSERT INTO triggers(owner, condition, note, actions, once, active, created_at, last_value,"
            " fire_count) VALUES (?, ?, ?, ?, ?, 1, ?, 0, 0)",
            (owner, compiled.source, note, dumps(normalized), int(once), self.clock()),
        )
        self._remember(trigger_id, compiled)
        trigger = self.get(trigger_id)
        self.swarm.bus.emit("trigger.created", owner, trigger=trigger)
        self.mark_dirty()
        return trigger

    def get(self, trigger_id: int) -> dict:
        row = self.swarm.db.one("SELECT * FROM triggers WHERE id = ?", (trigger_id,))
        if row is None:
            raise ToolError(f"Trigger #{trigger_id} not found. Use trigger_list to see your triggers.")
        return _to_dict(row)

    def delete(self, actor: str, trigger_id: int) -> None:
        trigger = self.get(trigger_id)
        if actor != USER and _norm(actor) != _norm(trigger["owner"]):
            raise ToolError(f"Trigger #{trigger_id} belongs to {trigger['owner']}; only its owner or the user "
                            "can delete it.")
        self.swarm.db.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))
        self._forget(trigger_id)
        self.swarm.bus.emit("trigger.deleted", actor, trigger_id=trigger_id, trigger=trigger)

    def list(self, owner: str | None = None, include_inactive: bool = False) -> list[dict]:
        sql = "SELECT * FROM triggers" + ("" if include_inactive else " WHERE active = 1") + " ORDER BY id"
        rows = self.swarm.db.all(sql)
        if owner is not None:
            rows = [r for r in rows if _norm(r["owner"]) == _norm(owner)]
        return [_to_dict(r) for r in rows]

    # --- evaluation ---------------------------------------------------------------------------
    def mark_dirty(self) -> None:
        self._dirty.set()

    def _on_event(self, event: dict[str, Any]) -> None:
        if str(event.get("type", "")).startswith(DIRTY_PREFIXES):
            self.mark_dirty()

    def evaluate_all(self) -> int:
        """Evaluate every active trigger once; returns how many fired. Not reentrant: a nested call
        (from an action) only marks the engine dirty."""
        if self._evaluating:
            self.mark_dirty()
            return 0
        self._evaluating = True
        try:
            fired = 0
            owners: dict[str, bool] = {}
            rows = self.swarm.db.all("SELECT * FROM triggers WHERE active = 1 ORDER BY id")
            for row in rows:
                owner = row["owner"]
                if owner not in owners:
                    owners[owner] = owner == USER or bool(self.swarm.is_member(owner))
                if owners[owner]:
                    fired += self._evaluate_one(row)
            if self._marks or self._fires:      # forget triggers deactivated or deleted elsewhere
                active = {row["id"] for row in rows}
                for stale in [k for k in {*self._marks, *self._fires} if k not in active]:
                    self._forget(stale)
            return fired
        finally:
            self._evaluating = False

    def _evaluate_one(self, row: Any) -> int:
        now = self.clock()
        reference = row["last_fired"] if row["last_fired"] is not None else row["created_at"]
        try:
            compiled = self.compile(row["condition"])
            env = _Env(self.swarm, row["owner"], row["created_at"], reference, now, self._marks.get(row["id"]))
            value = bool(compiled.evaluate(env))
        except Exception as e:  # any failure of a user condition is reported, never raised
            self._set_error(row["id"], f"{EVAL_ERROR_PREFIX}{_describe(e)}")
            return 0
        error = row["last_error"]
        if error and error.startswith(EVAL_ERROR_PREFIX):
            error = None
        due = compiled.periodic and now - reference >= min(compiled.every)
        if value and (not row["last_value"] or due):
            self._fire(row, now, compiled)
            return 1
        if int(value) != row["last_value"] or error != row["last_error"]:
            self.swarm.db.execute("UPDATE triggers SET last_value = ?, last_error = ? WHERE id = ?",
                                  (int(value), error, row["id"]))
        return 0

    def _set_error(self, trigger_id: int, error: str) -> None:
        error = error[:MAX_ERROR_CHARS]
        self.swarm.db.execute("UPDATE triggers SET last_error = ? WHERE id = ? AND last_error IS NOT ?",
                              (error, trigger_id, error))

    def _capture_marks(self, compiled: CompiledCondition) -> _Marks | None:
        functions = compiled.functions
        if "message" not in functions and "comment" not in functions and not compiled.files:
            return None
        db, root = self.swarm.db, self.swarm.config.workspace_dir
        return _Marks(
            message_id=db.scalar("SELECT COALESCE(MAX(id), 0) FROM messages") if "message" in functions else None,
            comment_id=db.scalar("SELECT COALESCE(MAX(id), 0) FROM comments") if "comment" in functions else None,
            files={path: _file_signature(root, path) for path in compiled.files},
        )

    def _forget(self, trigger_id: int) -> None:
        self._marks.pop(trigger_id, None)
        self._fires.pop(trigger_id, None)

    def _runaway(self, trigger_id: int, now: float) -> bool:
        """Record a fire of a repeating trigger; True if it now fired BURST_FIRES times within BURST_SECONDS."""
        times = self._fires.setdefault(trigger_id, collections.deque(maxlen=BURST_FIRES))
        times.append(now)
        return len(times) == BURST_FIRES and now - times[0] < BURST_SECONDS

    def _remember(self, trigger_id: int, compiled: CompiledCondition) -> None:
        marks = self._capture_marks(compiled)
        if marks is None:
            self._marks.pop(trigger_id, None)
        else:
            self._marks[trigger_id] = marks

    def _rearm(self, row: Any, compiled: CompiledCondition, fired_at: float) -> None:
        """After a repeating trigger fired, message()/comment()/file_changed()/every() are measured
        from the new reference point, so the edge state is the condition's value right now. Keeping
        last_value = 1 would swallow the next message that arrives before the following pass and
        leave the trigger true (and silent) forever."""
        try:
            env = _Env(self.swarm, row["owner"], row["created_at"], fired_at, max(self.clock(), fired_at),
                       self._marks.get(row["id"]))
            value = bool(compiled.evaluate(env))
        except Exception:  # reported by the next regular evaluation
            return
        if not value:
            self.swarm.db.execute("UPDATE triggers SET last_value = 0 WHERE id = ?", (row["id"],))

    def _fire(self, row: Any, now: float, compiled: CompiledCondition) -> None:
        trigger_id, owner = row["id"], row["owner"]
        actions = loads(row["actions"], []) or DEFAULT_ACTIONS
        # only actions that change the swarm state can feed a loop (a wake just queues a notification)
        acts = any(a.get("type") != "wake" for a in actions)
        runaway = not row["once"] and acts and self._runaway(trigger_id, now)
        once = bool(row["once"]) or runaway
        self.swarm.db.execute(
            "UPDATE triggers SET last_value = 1, last_fired = ?, fire_count = fire_count + 1, active = ?,"
            " last_error = NULL WHERE id = ?",
            (now, 0 if once else 1, trigger_id),
        )
        if runaway:   # stop the loop: no more actions, tell the owner
            errors = [f"deactivated: it fired {BURST_FIRES} times within {BURST_SECONDS:g} s, which looks like "
                      "a loop (e.g. triggers whose actions keep re-firing each other); its actions were not run "
                      "this time. Fix the condition or actions and create it again"]
        else:
            errors = [e for e in (self._run_action(owner, a) for a in actions if a.get("type") != "wake") if e]
        if errors or any(a.get("type") == "wake" for a in actions):
            note = row["note"] or ""
            if errors:
                note = (note + "\n" if note else "") + "Trigger action errors: " + "; ".join(errors)
            try:
                self.swarm.notify(owner, "n_trigger", id=trigger_id, condition=row["condition"], note=note)
            except Exception as e:
                log.exception("trigger #%s: notification failed", trigger_id)
                errors.append(f"wake: {_describe(e)}")
        if errors:
            self._set_error(trigger_id, "; ".join(errors))
        if once:
            self._forget(trigger_id)
        else:
            # taken after the actions: whatever they caused counts as already seen
            self._remember(trigger_id, compiled)
            if compiled.resets:
                self._rearm(row, compiled, now)
        trigger = self.get(trigger_id)
        log.info("trigger #%s fired (%s)", trigger_id, row["condition"])
        self.swarm.bus.emit("trigger.fired", owner, trigger=trigger, errors=errors)

    def _run_action(self, owner: str, action: dict[str, Any]) -> str | None:
        kind = action.get("type")
        try:
            if kind == "message":
                self._require("mail").send(owner, action["to"], action["text"])
            elif kind == "set_status":
                self._require("board").update(owner, action["task_id"], status=action["status"])
            elif kind == "create_task":
                self._require("board").create(
                    owner, action["title"], goal=action.get("goal") or "",
                    description=action.get("description") or "", assignee=action.get("assignee"),
                    tags=action.get("tags"), priority=action.get("priority", 1))
            else:
                raise ToolError(f"unknown action type {kind!r}")
        except ToolError as e:
            return f"{kind}: {e}"
        except Exception as e:
            log.exception("trigger action %s failed", kind)
            return f"{kind}: {_describe(e)}"
        return None

    def _require(self, attr: str) -> Any:
        component = getattr(self.swarm, attr, None)
        if component is None:
            raise ToolError(f"the swarm has no {attr} component")
        return component

    # --- loop ---------------------------------------------------------------------------------
    async def run(self) -> None:
        """Evaluate whenever the state changed (bus events) or every tick_seconds; cancel to stop."""
        # An asyncio.Event binds to the first loop that waits on it; the engine may outlive a loop
        # (start/stop in successive asyncio.run calls), so wait on a fresh one bound to this loop.
        dirty = asyncio.Event()
        if self._dirty.is_set():
            dirty.set()
        self._dirty = dirty
        while True:
            # asyncio.timeout, not wait_for: on Python 3.11 wait_for swallows a cancel() that arrives
            # in the same loop iteration as the event being set (e.g. a state change followed by
            # Swarm.stop()), so the loop would never stop.
            try:
                async with asyncio.timeout(self.tick_seconds):
                    await self._dirty.wait()
            except TimeoutError:
                pass
            self._dirty.clear()
            try:
                self.evaluate_all()
            except Exception:
                log.exception("trigger evaluation failed")
            # Event.wait() returns without suspending when the event is already set, and actions mark
            # the engine dirty again: yield so a busy pass can never starve the rest of the swarm.
            await asyncio.sleep(0)


def _describe(e: BaseException) -> str:
    text = str(e)
    if isinstance(e, ToolError):
        return text.split("\n", 1)[0]
    return f"{type(e).__name__}: {text}" if text else type(e).__name__
