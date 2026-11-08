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

from . import prompts
from .db import dumps, loads
from .jobs import fmt_duration
from .models import USER, AgentKind, AgentStatus, Status, ToolError
from .tools.fmt import item_time
from .workspace import lexically_inside, network_anchor, path_key, shown_list

log = logging.getLogger(__name__)

MAX_CONDITION_CHARS = 500
MAX_CONDITION_NODES = 200
MIN_EVERY_SECONDS = 10.0
MAX_NOTE_CHARS = 4000
MAX_ERROR_CHARS = 1000
MAX_EDIT_EVENTS = 10_000             # file.changed events buffered between two evaluation passes
MAX_EDITORS_SHOWN = 5                # editors a file_changed() notice names
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
    "  file_exists(path), file_changed(path) [once the file has stopped changing], after(\"2026-10-09 18:00\"),\n"
    "  elapsed(seconds), every(seconds >= 10), agent_status(name), goal_done()\n"
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
    """Resolve `path` inside the workspace root; None if it escapes, touches .git or is malformed. A UNC or
    Windows device anchor (\\\\host\\share, \\\\?\\..., \\\\.\\...) outside the workspace is refused before any
    resolve(), so no other host is contacted and nothing stalls (as in Workspace.resolve)."""
    if "\x00" in path:          # resolve() lets it through on Windows with Python 3.13, then stat() raises
        return None
    try:
        base = root.resolve()
        given = Path(path)
        if given.anchor and network_anchor(given) and not lexically_inside(given, base, root):
            return None
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
    files: dict[str, tuple[int, int] | None] = field(default_factory=dict)  # path -> (mtime_ns, size), None: no file


@dataclass
class _FileRun:
    """The changes of one file_changed() path since the trigger's reference point (creation or last fire): the
    file.changed events of write_file/edit_file, which name the editor, and the changes no event explains. A sure
    pass (no write_file/edit_file of the path in flight, Workspace.write_mark) shows every write whose event came
    since the previous sure pass, finished: its new signature is a command's or a job's change only when no event of
    the path came in between. Times are the engine's clock."""

    sig: tuple[int, int] | None       # signature seen by the last sure pass
    cursor: int                       # MAX(events.id) when the run started: older file.changed events are not its own
    first: float | None = None        # when the count first became > 0
    last: float | None = None         # the latest change seen (the owner's included)
    edits: int = 0                    # file.changed events of the path, every actor
    own: int = 0                      # ... of the trigger's owner
    unseen: int = 0                   # ... counted since the last sure pass: the next sure probe shows their writes
    unexplained: int = 0              # sure passes that saw a new signature (file present) with no event since the
                                      # previous one: a command's or a job's change
    editors: dict[str, None] = field(default_factory=dict)   # the other actors, first seen first
    done: bool = False                # every change explained by an event, every editor an AI agent out of its turn
    adopt: bool = False               # `sig` was probed while the file was being written: the next sure probe is
                                      # taken as the baseline, not counted (the write's event counts the change)
    fresh: bool = False               # created while a pass was stat'ing: `sig` is newer than that pass's probe, which
                                      # this run skips (until that pass ends; a fire in it adopts the next sure probe)

    @property
    def count(self) -> int:
        """The changes reported: the others' edits plus the changes no event explains (commands, jobs)."""
        return self.edits - self.own + self.unexplained

    def stopped(self, now: float, settle: float) -> bool:
        """The run has changes and they stopped: no change for `settle` seconds (0: at once), or the editors are
        done."""
        return self.count > 0 and (settle <= 0 or self.done or (self.last is not None and now - self.last >= settle))

    def settled(self, now: float, settle: float, max_wait: float) -> bool:
        """stopped(), or `max_wait` seconds (0: no cap) passed since the first change of a file that keeps changing."""
        if self.stopped(now, settle):
            return True
        return self.count > 0 and max_wait > 0 and self.first is not None and now - self.first >= max_wait


def _file_probe(root: Path, path: str) -> tuple[int, int, float] | None:
    """(mtime_ns, size, mtime) of the workspace file, or None if the path is unsafe or cannot be stat'd."""
    target = safe_path(root, path)
    if target is None:
        return None
    try:
        st = os.stat(target)
    except (OSError, ValueError):
        return None
    return st.st_mtime_ns, st.st_size, st.st_mtime


def _cached_probe(cache: dict[str, tuple[int, int, float] | None], root: Path, path: str
                  ) -> tuple[int, int, float] | None:
    """_file_probe() of `path` from a pass's cache, or a direct stat for a path that pass did not probe."""
    return cache[path] if path in cache else _file_probe(root, path)


def _within_interval(told: float | None, now: float, interval: float) -> bool:
    """Told less than `interval` ago (a clock gone back since then does not count as within it)."""
    return interval > 0 and told is not None and 0 <= now - told < interval


def _path_key(root: Path, path: str) -> str | None:
    """The key the file.changed events of `path` carry (workspace-relative as Workspace.rel gives it, in path_key
    form), or None if the path is unsafe."""
    target = safe_path(root, path)
    if target is None:
        return None
    try:
        return path_key(target.relative_to(root.resolve()).as_posix())
    except (ValueError, OSError, RuntimeError):
        return None


# ---------------------------------------------------------------------------------------------
# evaluation environment: the functions read the swarm state directly from the DB


class _Env:
    def __init__(self, swarm: Any, owner: str, created_at: float, reference: float, now: float,
                 marks: _Marks | None = None, fs: dict[str, tuple[int, int, float] | None] | None = None,
                 runs: dict[str, _FileRun] | None = None, settle: float = 0.0, max_wait: float = 0.0):
        self.swarm = swarm
        self.db = swarm.db
        self.owner = owner
        self.created_at = created_at
        self.reference = reference      # creation or last fire
        self.now = now
        self.marks = marks              # what was already seen at the reference point
        self.fs = fs or {}              # file stats probed off the loop; empty for a direct (sync) call
        self.runs = runs                # file_changed() runs (TriggerEngine._advance); None: any change counts at once
        self.settle = settle            # [agents] file_settle_seconds, file_max_wait_seconds
        self.max_wait = max_wait

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

    def _probe(self, path: str) -> tuple[int, int, float] | None:
        """The file's (mtime_ns, size, mtime) from the pass cache, or a direct stat when the engine is
        driven synchronously (tests); None if the path is unsafe or cannot be stat'd."""
        return _cached_probe(self.fs, self.swarm.config.workspace_dir, path)

    def fn_file_exists(self, path: str) -> bool:
        return self._probe(path) is not None

    def fn_file_changed(self, path: str) -> bool:
        """Changed since the reference point and, with the engine's runs, that run of changes has settled and holds
        changes other than the owner's own edits."""
        probe = self._probe(path)
        if probe is None:
            return False
        mtime_ns, size, mtime = probe
        run = None if self.runs is None else self.runs.get(path)
        if self.marks is not None and path in self.marks.files:
            # appeared or modified; a run followed since the reference point counted every change, also one that left
            # the signature as it was (a coarse mtime, the same size)
            changed = (mtime_ns, size) != self.marks.files[path] or (run is not None and run.count > 0)
        else:
            changed = mtime > self.reference
        if not changed or self.runs is None:
            return changed
        return run is not None and run.settled(self.now, self.settle, self.max_wait)

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


def _count_edit(run: _FileRun, actor: str | None, owner_key: str) -> None:
    """One file.changed event of the run's path: the owner's own edit, or another editor's."""
    run.edits += 1
    if _actor_key(actor) == owner_key:
        run.own += 1
    else:
        run.editors.setdefault(actor or "?", None)


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
    probes: tuple[str, ...] = ()      # workspace paths probed by file_exists()/file_changed()

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
        self.probes: list[str] = []
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
                                 tuple(dict.fromkeys(self.files)), tuple(dict.fromkeys(self.probes)))

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
        if name in ("file_exists", "file_changed"):
            self.probes.append(named["path"])
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


def _to_dict(row: Any, interval: float = 0.0) -> dict[str, Any]:
    """The public trigger dict; `interval` ([agents] trigger_wake_seconds) gives `wake_at`, when the fires held back
    since the owner was last told (`held`, `told_at`) wake it."""
    held, told = row["held"] or 0, row["told_at"]
    wake_at = told + interval if held and row["active"] and interval > 0 and told is not None else None
    return {
        "id": row["id"], "owner": row["owner"], "condition": row["condition"], "note": row["note"] or "",
        "actions": loads(row["actions"], []), "once": bool(row["once"]), "active": bool(row["active"]),
        "created_at": row["created_at"], "last_fired": row["last_fired"],
        "fire_count": row["fire_count"] or 0, "last_error": row["last_error"],
        "held": held, "told_at": told, "wake_at": wake_at,
    }


def _held_files(row: Any) -> dict[str, dict[str, Any]]:
    held = loads(row["held_files"], {})
    return {p: v for p, v in held.items() if isinstance(v, dict)} if isinstance(held, dict) else {}


def _merge_files(held: dict[str, dict[str, Any]], files: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """held_files with the file_changed() changes of one more fire (TriggerEngine._file_changes): the counts summed,
    the editors joined, the first and the latest change, the state of the latest fire."""
    merged = {path: dict(item) for path, item in held.items()}
    for f in files:
        item = merged.setdefault(f["path"], {"count": 0, "by": [], "first": None, "last": None})
        item["count"] = int(item.get("count") or 0) + f["changes"]
        item["by"] = list(dict.fromkeys([*(item.get("by") or []), *f["by"]]))
        firsts = [t for t in (item.get("first"), f["first"]) if t is not None]
        lasts = [t for t in (item.get("last"), f["last"]) if t is not None]
        item["first"], item["last"] = min(firsts, default=None), max(lasts, default=None)
        item["settled"] = f["settled"]
    return merged


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
        self._probes: dict[str, tuple[int, int, float] | None] = {}   # file stats for the current pass (off loop)
        self._keys: dict[str, str | None] = {}  # path keys of the file_changed() paths for the current pass (off loop)
        self._write_mark: tuple[int, frozenset[str]] | None = None   # Workspace.write_mark before the pass's probes
        self._runs: dict[int, dict[str, _FileRun]] = {}   # trigger id -> its file_changed() runs (memory only)
        # (event id, actor, path key) of the file.changed events since the last pass
        self._edits: collections.deque[tuple[int, str | None, str]] = collections.deque(maxlen=MAX_EDIT_EVENTS)
        # owners whose active triggers hold back fires ([agents] trigger_wake_seconds): render_wake asks no query for
        # the others
        self._held_owners: set[str] = {r["owner"] for r in swarm.db.all(
            "SELECT DISTINCT owner FROM triggers WHERE active = 1 AND held > 0")}
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
        return _to_dict(row, self._wake_interval())

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
        interval = self._wake_interval()
        return [_to_dict(r, interval) for r in rows]

    # --- evaluation ---------------------------------------------------------------------------
    def mark_dirty(self) -> None:
        self._dirty.set()

    def _on_event(self, event: dict[str, Any]) -> None:
        kind = str(event.get("type", ""))
        if kind == "file.changed" and event.get("id") is not None:
            path = (event.get("data") or {}).get("path")
            if isinstance(path, str):   # a write_file/edit_file edit: who changed which file (file_changed runs)
                self._edits.append((event["id"], event.get("actor"), path_key(path)))
        if kind.startswith(DIRTY_PREFIXES):
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
            # the file.changed events since the last pass, for every trigger of this one
            taken, self._edits = self._edits, collections.deque(maxlen=MAX_EDIT_EVENTS)
            edits = list(taken)
            owners: dict[str, bool] = {}
            rows = self.swarm.db.all("SELECT * FROM triggers WHERE active = 1 ORDER BY id")
            for row in rows:
                if self._owner_alive(owners, row["owner"]):
                    fired += self._evaluate_one(row, edits)
            if self._marks or self._fires or self._runs:      # forget triggers deactivated or deleted elsewhere
                active = {row["id"] for row in rows}
                for stale in [k for k in {*self._marks, *self._fires, *self._runs} if k not in active]:
                    self._forget(stale)
            self._release_due(owners)
            return fired
        finally:
            self._evaluating = False

    def _owner_alive(self, known: dict[str, bool], owner: str) -> bool:
        """The owner is the user or a member (`known`: this pass's answers)."""
        if owner not in known:
            known[owner] = owner == USER or bool(self.swarm.is_member(owner))
        return known[owner]

    def _watched_paths(self) -> tuple[set[str], set[str]]:
        """Workspace paths the active triggers probe (file_exists/file_changed) and those file_changed() watches,
        resolved together off the loop before a pass."""
        paths: set[str] = set()
        files: set[str] = set()
        for row in self.swarm.db.all("SELECT condition FROM triggers WHERE active = 1"):
            try:
                compiled = self.compile(row["condition"])
            except ToolError:
                continue
            paths.update(compiled.probes)
            files.update(compiled.files)
        return paths, files

    def _probe_files(self, paths: set[str], files: set[str]
                     ) -> tuple[dict[str, tuple[int, int, float] | None], dict[str, str | None]]:
        root = self.swarm.config.workspace_dir
        return {path: _file_probe(root, path) for path in paths}, {path: _path_key(root, path) for path in files}

    async def _evaluate_pass(self) -> int:
        """Stat the watched files off the event loop, then evaluate on it: no file resolve/stat blocks the
        loop, so a slow path cannot stall the swarm."""
        paths, files = self._watched_paths()
        self._write_mark = self._take_write_mark()
        try:
            self._probes, self._keys = await asyncio.to_thread(self._probe_files, paths, files) if paths else ({}, {})
            return self.evaluate_all()
        finally:
            self._probes, self._keys, self._write_mark = {}, {}, None
            for runs in self._runs.values():
                for run in runs.values():
                    run.fresh = False

    def _take_write_mark(self) -> tuple[int, frozenset[str]] | None:
        mark = getattr(getattr(self.swarm, "workspace", None), "write_mark", None)
        return mark() if callable(mark) else None

    def _unsure(self, path: str) -> bool:
        """A write_file/edit_file write of `path` was in flight when this pass started probing, or started since:
        the probe may show the file before or in the middle of that write (Workspace.write_mark). Without a pass's
        mark (trigger_create's direct stat, a direct evaluate_all, both on the loop): a write in flight now."""
        mark = self._write_mark or self._take_write_mark()
        if mark is None:
            return False
        key = self._key_of(path)
        return key is not None and self.swarm.workspace.written_since(mark, key)

    def _file_settings(self) -> tuple[float, float]:
        """[agents] file_settle_seconds and file_max_wait_seconds, read at each pass (session overrides apply)."""
        agents = self.swarm.config.agents
        return max(0.0, float(agents.file_settle_seconds)), max(0.0, float(agents.file_max_wait_seconds))

    def _wake_interval(self) -> float:
        """[agents] trigger_wake_seconds, read at each use (session overrides apply)."""
        return max(0.0, float(self.swarm.config.agents.trigger_wake_seconds))

    # --- the wake limit of repeating triggers ([agents] trigger_wake_seconds) -------------------
    @staticmethod
    def _held_back(row: Any, now: float, interval: float) -> bool:
        """The notice of this fire waits: a member's repeating trigger that told its owner less than `interval` ago
        (a clock gone back since then does not hold it)."""
        return not row["once"] and row["owner"] != USER and _within_interval(row["told_at"], now, interval)

    def _hold(self, row: Any, now: float, files: list[dict[str, Any]]) -> None:
        held = _merge_files(_held_files(row), files)
        self.swarm.db.execute("UPDATE triggers SET held = COALESCE(held, 0) + 1, held_last = ?, held_files = ?"
                              " WHERE id = ?", (now, dumps(held) if held else None, row["id"]))
        self._held_owners.add(row["owner"])

    def _told(self, trigger_id: int, now: float) -> None:
        self.swarm.db.execute("UPDATE triggers SET told_at = ?, held = 0, held_last = NULL, held_files = NULL"
                              " WHERE id = ?", (now, trigger_id))

    def _notify_held(self, row: Any, now: float, *, count: int, last: float, files: dict[str, dict[str, Any]],
                     note: str, wake: bool = True) -> None:
        """The one notice of `count` fires, the latest at `last`, since the owner was last told."""
        lang = self.swarm.language_of(row["owner"])
        interval = self._wake_interval()
        key = "n_trigger_held" if interval > 0 else "n_trigger_held_off"   # the limit switched off with fires held
        self.swarm.notify(row["owner"], key, wake=wake, id=row["id"], condition=row["condition"], count=count,
                          since=item_time(row["told_at"] or now, now), last=item_time(last, now),
                          interval=fmt_duration(interval, lang), changes=self._held_changes_text(files, lang, now),
                          note=note)

    def _tell(self, row: Any, now: float, note: str, files: list[dict[str, Any]]) -> None:
        """Wake the owner with this fire, merged with the fires held back since it was last told."""
        trigger_id, owner = row["id"], row["owner"]
        held = row["held"] or 0
        if held:
            self._notify_held(row, now, count=held + 1, last=now, files=_merge_files(_held_files(row), files),
                              note=note)
        else:
            changes = self._changes_text(files, self.swarm.language_of(owner), now)
            if changes:
                self.swarm.notify(owner, "n_trigger_files", id=trigger_id, condition=row["condition"],
                                  changes=changes, note=note)
            else:
                self.swarm.notify(owner, "n_trigger", id=trigger_id, condition=row["condition"], note=note)
        self._told(trigger_id, now)

    def _release(self, row: Any, now: float, *, wake: bool) -> None:
        """The held fires of a trigger in one notice, without a new fire; their last action error is in its note."""
        note = row["note"] or ""
        error = row["last_error"]
        if error and not error.startswith(EVAL_ERROR_PREFIX):
            note = (note + "\n" if note else "") + "Trigger action errors: " + error
        self._notify_held(row, now, count=row["held"], last=row["held_last"] or now, files=_held_files(row), note=note,
                          wake=wake)
        self._told(row["id"], now)

    def _held_changes_text(self, held: dict[str, dict[str, Any]], lang: str, now: float) -> str:
        """The n_trigger_files fragments of merged held_files: a file counts as settled when it has been quiet for
        the settle window by now."""
        settle, _ = self._file_settings()
        files = [{"path": path, "changes": int(item.get("count") or 0), "by": list(item.get("by") or []),
                  "first": item.get("first"), "last": item.get("last"),
                  "settled": bool(item.get("settled")) or (item.get("last") is not None
                                                           and now - item["last"] >= settle)}
                 for path, item in held.items() if item.get("count")]
        return self._changes_text(files, lang, now)

    def _release_due(self, owners: dict[str, bool]) -> None:
        """Wake the owners whose triggers held fires back for a whole interval (or the limit is off now)."""
        if not self._held_owners:
            return
        now, interval = self.clock(), self._wake_interval()
        waiting: set[str] = set()
        for row in self.swarm.db.all("SELECT * FROM triggers WHERE active = 1 AND held > 0 ORDER BY id"):
            owner = row["owner"]
            if not self._owner_alive(owners, owner):
                continue
            if _within_interval(row["told_at"], now, interval):
                waiting.add(owner)
                continue
            try:
                self._release(row, now, wake=True)
            except Exception:  # noqa: BLE001 - one owner's notice must not stop the others'
                log.exception("trigger #%s: the held notice failed", row["id"])
                waiting.add(owner)
        self._held_owners = waiting

    def release_held(self, owner: str) -> None:
        """Tell `owner` now, quietly, about the fires its repeating triggers held back: it is reading its news anyway
        (Swarm.render_wake: a wake-up, the news between two steps, wait_for_events), so they need no wake-up of their
        own. Never raises."""
        if owner not in self._held_owners:
            return
        try:
            now = self.clock()
            for row in self.swarm.db.all("SELECT * FROM triggers WHERE owner = ? AND active = 1 AND held > 0"
                                         " ORDER BY id", (owner,)):
                self._release(row, now, wake=False)
            self._held_owners.discard(owner)
        except Exception:  # noqa: BLE001 - the wake-up goes on without them; the interval's end tells it
            log.exception("could not tell %s about the held trigger fires", owner)

    def _evaluate_one(self, row: Any, edits: list[tuple[int, str | None, str]]) -> int:
        now = self.clock()
        reference = row["last_fired"] if row["last_fired"] is not None else row["created_at"]
        try:
            compiled = self.compile(row["condition"])
            runs = self._advance(row, compiled, reference, now, edits) if compiled.files else None
            settle, max_wait = self._file_settings()
            env = _Env(self.swarm, row["owner"], row["created_at"], reference, now, self._marks.get(row["id"]),
                       self._probes, runs, settle, max_wait)
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

    def _probe(self, path: str) -> tuple[int, int, float] | None:
        """The file's (mtime_ns, size, mtime), from the pass cache when one populated it, else a direct stat."""
        return _cached_probe(self._probes, self.swarm.config.workspace_dir, path)

    def _signature(self, path: str) -> tuple[int, int] | None:
        """file_changed() mark for `path`."""
        probe = self._probe(path)
        return None if probe is None else probe[:2]

    def _key_of(self, path: str) -> str | None:
        if path in self._keys:
            return self._keys[path]
        return _path_key(self.swarm.config.workspace_dir, path)

    def _event_cursor(self) -> int:
        return int(self.swarm.db.scalar("SELECT COALESCE(MAX(id), 0) FROM events") or 0)

    def _advance(self, row: Any, compiled: CompiledCondition, reference: float, now: float,
                 edits: list[tuple[int, str | None, str]]) -> dict[str, _FileRun]:
        """The trigger's file_changed() runs brought up to this pass: the file.changed events since the last pass,
        the signature the pass probed and whether the editors are done."""
        runs = self._runs.get(row["id"])
        if runs is None or runs.keys() != set(compiled.files):
            runs = self._runs[row["id"]] = self._seed(row, compiled, reference, now)
        owner = _actor_key(row["owner"])
        for path, run in runs.items():
            key = self._key_of(path)
            for event_id, actor, event_key in edits:
                if event_key == key and event_id > run.cursor:
                    _count_edit(run, actor, owner)
                    run.unseen += 1
                    run.last = now
            # a write in flight: the next sure pass probes it finished (its event counts it); a run created after this
            # pass's stat: that stat is older than the run's baseline and says nothing about the changes since
            unsure = self._unsure(path) or run.fresh
            if not unsure:
                sig = self._signature(path)
                if run.adopt:            # the baseline was taken during a write (a fire, trigger_create, a reseed):
                    run.adopt = False    # as the file is now, with the writes the run counted so far
                elif sig != run.sig and sig is not None:
                    run.last = now
                    if not run.unseen:   # no write_file/edit_file event since the last sure pass: a command, a job
                        run.unexplained += 1
                run.sig, run.unseen = sig, 0
            if run.first is None and run.count > 0:
                run.first = now
            # a wall clock set back: the times are at most now, so the quiet window and the cap still end
            run.first = None if run.first is None else min(run.first, now)
            run.last = None if run.last is None else min(run.last, now)
            # the write in flight, and a command's change since the last sure probe, are not seen yet: the next sure
            # pass decides
            run.done = not unsure and self._editors_done(run)
        return runs

    def _seed(self, row: Any, compiled: CompiledCondition, reference: float, now: float) -> dict[str, _FileRun]:
        """Runs of a trigger this engine has not followed (after a restart): the file.changed events since its
        reference point from the journal, and an mtime after it as one change that no event may explain. Their
        times are the journal's, at most now; a change seen only by the mtime starts its quiet window and its cap now.
        A write in flight is no such change: its event counts it."""
        cursor = self._event_cursor()
        journal = [(r["actor"], min(r["ts"] or now, now), path_key(str(loads(r["data"], {}).get("path") or "")))
                   for r in self.swarm.db.all("SELECT actor, ts, data FROM events WHERE type = 'file.changed'"
                                              " AND ts > ? AND id <= ? ORDER BY id", (reference, cursor))]
        owner = _actor_key(row["owner"])
        runs: dict[str, _FileRun] = {}
        for path in compiled.files:
            probe = self._probe(path)
            run = runs[path] = _FileRun(sig=None if probe is None else probe[:2], cursor=cursor,
                                        adopt=self._unsure(path))
            key = self._key_of(path)
            times, others = [], []       # every change's time; those of the changes counted (not the owner's)
            for actor, ts, event_key in journal:
                if event_key == key:
                    _count_edit(run, actor, owner)
                    times.append(ts)
                    if _actor_key(actor) != owner:
                        others.append(ts)
            if probe is not None and probe[2] > reference and not times and not run.adopt:
                run.unexplained = 1      # a change no event explains: its quiet window and cap start now
                times = others = [now]
            if times:
                run.last = max(times)
                if run.count > 0:
                    run.first = min(others)
        return runs

    def _editors_done(self, run: _FileRun) -> bool:
        """Every change of the run is explained by a write_file/edit_file event, and every editor other than the
        owner is an AI agent out of its turn (one without a runtime, e.g. retired, is out of it). An external
        member or the user as an editor, or a change no event explains, leaves the quiet window to decide."""
        if run.count <= 0 or run.unexplained:
            return False
        runtimes = getattr(self.swarm, "runtimes", None) or {}
        for name in run.editors:
            if self.swarm.kind_of(name) != AgentKind.LLM:
                return False
            runtime = runtimes.get(name)
            if runtime is not None and runtime.state == AgentStatus.WORKING:
                return False
        return True

    def pending(self, trigger_id: int) -> list[tuple[str, int, float | None]]:
        """(path, changes, seconds since the latest change) of the trigger's file_changed() files changed since its
        last fire (as far as the last pass saw)."""
        now = self.clock()
        return [(path, run.count, None if run.last is None else max(0.0, now - run.last))
                for path, run in (self._runs.get(trigger_id) or {}).items() if run.count > 0]

    def create_notes(self, trigger: dict) -> list[str]:
        """How the trigger fires under the settings in force now, for the trigger_create result (English)."""
        notes = []
        if self.compile(trigger["condition"]).files:
            settle, max_wait = self._file_settings()
            if settle <= 0:
                notes.append("file_changed() does not count your own write_file/edit_file edits")
            else:
                cap = f", at the latest {fmt_duration(max_wait)} after the first change" if max_wait > 0 else ""
                notes.append(f"file_changed() becomes true once the file has stopped changing: after "
                             f"{fmt_duration(settle)} without a change or once the agents who changed it have ended "
                             f"their turns{cap}, and your own write_file/edit_file edits do not count")
        interval = self._wake_interval()
        if (not trigger["once"] and trigger["owner"] != USER and interval > 0
                and any(a.get("type") == "wake" for a in trigger["actions"])):
            notes.append(f"it wakes you at most once every {fmt_duration(interval)}, with the fires in between in one "
                         "notice")
        return notes

    def _file_changes(self, trigger_id: int, now: float) -> list[dict[str, Any]]:
        """The changes the trigger's file_changed() runs saw up to now: one item per file with changes; `settled`:
        they stopped (a run fired by the cap, or absorbed by another part of the condition, is still changing)."""
        settle, _ = self._file_settings()
        return [{"path": path, "changes": run.count, "by": list(run.editors), "first": run.first, "last": run.last,
                 "settled": run.stopped(now, settle)}
                for path, run in (self._runs.get(trigger_id) or {}).items() if run.count > 0]

    @staticmethod
    def _changes_text(files: list[dict[str, Any]], lang: str, now: float) -> str:
        """The fragments of n_trigger_files for `files` (_file_changes), in the owner's language."""
        parts = []
        for item in files:
            names = shown_list(item["by"], MAX_EDITORS_SHOWN)
            by = prompts.t(lang, "trigger_file_by", names=names) if item["by"] else ""
            if item["settled"]:
                quiet = fmt_duration(now - (item["last"] or now), lang)
                parts.append(prompts.t(lang, "trigger_file_settled", path=item["path"], count=item["changes"], by=by,
                                       quiet=quiet))
            else:
                duration = fmt_duration(now - (item["first"] or now), lang)
                parts.append(prompts.t(lang, "trigger_file_changing", path=item["path"], count=item["changes"],
                                       by=by, duration=duration))
        return "".join(parts)

    def _capture_marks(self, compiled: CompiledCondition) -> _Marks | None:
        functions = compiled.functions
        if "message" not in functions and "comment" not in functions and not compiled.files:
            return None
        db = self.swarm.db
        return _Marks(
            message_id=db.scalar("SELECT COALESCE(MAX(id), 0) FROM messages") if "message" in functions else None,
            comment_id=db.scalar("SELECT COALESCE(MAX(id), 0) FROM comments") if "comment" in functions else None,
            files={path: self._signature(path) for path in compiled.files},
        )

    def _forget(self, trigger_id: int) -> None:
        self._marks.pop(trigger_id, None)
        self._fires.pop(trigger_id, None)
        self._runs.pop(trigger_id, None)

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
        if marks is not None and compiled.files:
            # new runs from here: a run still unsettled at a fire is absorbed by it (its notice names it)
            cursor = self._event_cursor()
            # trigger_create while a pass waits for its stat thread: that pass's probe predates this baseline
            fresh = self._write_mark is not None and not self._evaluating
            # (a file being written now: its probe may predate the write, and so may a fire's in the pass that skipped
            # the probe of a fresh run: the next sure pass gives the base)
            old = self._runs.get(trigger_id) or {}
            self._runs[trigger_id] = {path: _FileRun(sig=marks.files[path], cursor=cursor, fresh=fresh,
                                                     adopt=self._unsure(path) or (path in old and old[path].fresh))
                                      for path in compiled.files}
        else:
            self._runs.pop(trigger_id, None)

    def _rearm(self, row: Any, compiled: CompiledCondition, fired_at: float) -> None:
        """After a repeating trigger fired, message()/comment()/file_changed()/every() are measured
        from the new reference point, so the edge state is the condition's value right now. Keeping
        last_value = 1 would swallow the next message that arrives before the following pass and
        leave the trigger true (and silent) forever."""
        try:
            settle, max_wait = self._file_settings()
            env = _Env(self.swarm, row["owner"], row["created_at"], fired_at, max(self.clock(), fired_at),
                       self._marks.get(row["id"]), self._probes, self._runs.get(row["id"]), settle, max_wait)
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
        files = self._file_changes(trigger_id, now)   # before the actions: what they cause belongs to the next run
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
            interval = self._wake_interval()
            try:
                if not runaway and self._held_back(row, now, interval):
                    self._hold(row, now, files)   # told at the owner's next wake-up, or at the interval's end
                else:
                    self._tell(row, now, note, files)
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
        extra = {"files": [{k: f[k] for k in ("path", "changes", "by", "settled")} for f in files]} if files else {}
        self.swarm.bus.emit("trigger.fired", owner, trigger=trigger, errors=errors, **extra)

    def _run_action(self, owner: str, action: dict[str, Any]) -> str | None:
        kind = action.get("type")
        try:
            if kind == "message":
                # automatic: a user-owned trigger's message never answers a member's question (Mailbox.send)
                self._require("mail").send(owner, action["to"], action["text"], automatic=True)
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
                await self._evaluate_pass()
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
