"""Task board: tasks with goals, dependencies and review, plus a discussion thread per task (SPEC §4)."""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Iterable

from . import prompts
from .db import Database, dumps, loads
from .models import USER, Status, Task, ToolError

log = logging.getLogger(__name__)

UNSET: Any = object()  # sentinel for "not passed" on nullable fields

MAX_TITLE = 200
MAX_POST = 20000
NOTIFY_TEXT_LIMIT = 1500

_USER_ALIASES = ("user", "пользователь")
_JSON_FIELDS = ("depends_on", "tags")
_OPEN = tuple(s for s in Status.ALL if s in Status.OPEN)
_CHUNK = 500


# --- value helpers -----------------------------------------------------------------------------

def normalize_status(value: Any) -> str:
    key = str(value if value is not None else "").strip().lower().replace("-", "_").replace(" ", "_")
    if key not in Status.ALL:
        raise ToolError(f"Unknown status '{value}'. Valid statuses: {', '.join(Status.ALL)}.")
    return key


def _as_int(value: Any, what: str) -> int:
    if isinstance(value, bool):
        raise ToolError(f"{what} must be an integer, got {value!r}.")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip().lstrip("#")
        if re.fullmatch(r"-?\d+", text):
            return int(text)
    raise ToolError(f"{what} must be an integer, got {value!r}.")


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y", "on")
    return bool(value)


def _check_title(title: Any) -> str:
    text = str(title if title is not None else "").strip()
    if not text:
        raise ToolError("Task title must not be empty.")
    if len(text) > MAX_TITLE:
        raise ToolError(f"Task title is too long ({len(text)} chars, max {MAX_TITLE}). "
                        "Keep it short and put details into the description.")
    return text


def _check_priority(value: Any) -> int:
    if value is None:
        return 1
    try:
        priority = _as_int(value, "priority")
    except ToolError:
        priority = -1
    if not 0 <= priority <= 3:
        raise ToolError(f"Invalid priority {value!r}: use 0 (low), 1 (normal), 2 (high) or 3 (critical).")
    return priority


def _norm_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        items = tags.split(",")
    elif isinstance(tags, (list, tuple, set, frozenset)):
        items = list(tags)
    else:
        raise ToolError(f"tags must be a list of strings, e.g. [\"backend\", \"api\"]; got {tags!r}.")
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        tag = str(item).strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return out


def _check_post(text: Any) -> str:
    body = str(text if text is not None else "").strip()
    if not body:
        raise ToolError("Post text must not be empty.")
    if len(body) > MAX_POST:
        raise ToolError(f"Post is too long ({len(body)} chars, max {MAX_POST}). Put long content into a "
                        "workspace file and post its path.")
    return body


def _clip(text: str, limit: int = NOTIFY_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _unique(items: Iterable[Any]) -> list[Any]:
    out: list[Any] = []
    for item in items:
        if item is not None and item not in out:
            out.append(item)
    return out


def _qmarks(n: int) -> str:
    return ", ".join("?" * n)


def find_mentions(text: str, names: Iterable[str]) -> list[str]:
    """Members mentioned in `text` as `@Full Name` (case-insensitive, whitespace-tolerant, longest names
    first, the name must end at a word boundary). `@user` / `@пользователь` mention USER."""
    lookup: dict[str, tuple[str, list[str]]] = {}  # lower-cased name -> (name, words as spelled)
    for name in names:
        words = name.split()
        if words:
            lookup[" ".join(words).lower()] = (name, words)
    for alias in _USER_ALIASES:
        lookup[alias] = (USER, [alias])
    alternatives = sorted(lookup, key=len, reverse=True)
    # one group per name, spelled as stored: the matched group tells which name it was, even where
    # case-insensitive matching and str.lower() disagree (final sigma, dotted I...)
    groups = "|".join("(" + r"\s+".join(map(re.escape, lookup[k][1])) + ")" for k in alternatives)
    found: list[str] = []
    for match in re.finditer(r"(?<!\w)@(?:" + groups + r")(?!\w)", text, re.IGNORECASE):
        name = lookup[alternatives[match.lastindex - 1]][0]
        if name not in found:
            found.append(name)
    return found


def _post_dict(row: Any) -> dict[str, Any]:
    return {"id": row["id"], "task_id": row["task_id"], "author": row["author"], "text": row["text"],
            "reply_to": row["reply_to"], "ts": row["ts"]}


class Board:
    def __init__(self, swarm: Any) -> None:
        self.swarm = swarm

    @property
    def db(self) -> Database:
        return self.swarm.db

    # --- reads -------------------------------------------------------------------------------------

    def get(self, task_id: int) -> Task:
        task = self._load(task_id)
        self._fill([task])
        return task

    def list(self, *, status: str | list[str] | None = None, assignee: str | None = None,
             tag: str | None = None, parent_id: int | None = None, include_closed: bool = False) -> list[Task]:
        where: list[str] = []
        params: list[Any] = []
        statuses = self._status_filter(status)
        if statuses:
            where.append(f"status IN ({_qmarks(len(statuses))})")
            params += statuses
        elif not include_closed:
            where.append(f"status IN ({_qmarks(len(_OPEN))})")
            params += _OPEN
        if assignee:
            where.append("assignee = ?")
            params.append(self._lenient_member(assignee))
        if parent_id is not None:
            where.append("parent_id = ?")
            params.append(_as_int(parent_id, "parent_id"))
        tasks = self._query(where, params)
        if tag:
            key = str(tag).strip().lower()
            tasks = [t for t in tasks if key in {x.lower() for x in t.tags}]
        self._fill(tasks)
        return tasks

    def blockers(self, task: Task) -> list[int]:
        return self._blockers_of(task.depends_on)

    def ready_unassigned(self) -> list[Task]:
        return [t for t in self.list(status=Status.TODO) if t.assignee is None and not t.blocked_by]

    def open_tasks_of(self, name: str) -> list[Task]:
        tasks = self._query(["assignee = ?", f"status IN ({_qmarks(len(_OPEN))})"], [name, *_OPEN])
        self._fill(tasks)
        return tasks

    def counts(self) -> dict[str, int]:
        counts = {s: 0 for s in Status.ALL}
        for row in self.db.all("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"):
            counts[row["status"]] = row["n"]
        return counts

    def discussion(self, task_id: int, since_id: int = 0, limit: int = 200) -> list[dict]:
        """Posts with id > since_id, ascending. With since_id == 0: the latest `limit` posts."""
        task = self._load(task_id)
        since = _as_int(since_id or 0, "since_id")
        limit = max(1, _as_int(limit, "limit"))
        if since:
            rows = self.db.all("SELECT * FROM comments WHERE task_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                               (task.id, since, limit))
        else:
            rows = list(reversed(self.db.all(
                "SELECT * FROM comments WHERE task_id = ? ORDER BY id DESC LIMIT ?", (task.id, limit))))
        return [_post_dict(r) for r in rows]

    def participants(self, task_id: int) -> set[str]:
        return set(self._participants(self._load(task_id)))

    # --- writes ------------------------------------------------------------------------------------

    def create(self, actor: str, title: str, *, goal: str = "", description: str = "",
               assignee: str | None = None, reviewer: str | None = None, parent_id: int | None = None,
               depends_on: list[int] | None = None, priority: int = 1, tags: list[str] | None = None) -> Task:
        title = _check_title(title)
        priority = _check_priority(priority)
        tags = _norm_tags(tags)
        assignee = self._resolve_opt(assignee)
        reviewer = self._resolve_opt(reviewer)
        with self.db.transaction():
            deps = self._check_deps(depends_on or [])
            parent = self._check_parent(parent_id)
            now = time.time()
            task_id = self.db.insert(
                "INSERT INTO tasks(title, goal, description, status, assignee, reviewer, created_by, parent_id,"
                " depends_on, priority, tags, result, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                (title, str(goal or "").strip(), str(description or ""), Status.TODO, assignee, reviewer, actor,
                 parent, dumps(deps), priority, dumps(tags), now, now),
            )
        task = self.get(task_id)
        log.info("task #%s created by %s", task.id, actor)
        self.swarm.bus.emit("task.created", actor, task=task.to_dict())
        self._notify_member(assignee, actor, "n_assigned", id=task.id, title=task.title, by=actor)
        return task

    def claim(self, actor: str, task_id: int) -> Task:
        def plan(old: Task) -> dict[str, Any]:
            if old.assignee == actor and old.status in (Status.IN_PROGRESS, Status.WAITING):
                return {}
            if old.assignee and old.assignee != actor and old.status in Status.OPEN:
                raise ToolError(f"Task #{old.id} is already assigned to {old.assignee} (status {old.status}). "
                                "Pick another ready task (or call wait if there is none); post in its discussion only if "
                                "you have something the assignee needs.")
            if old.status != Status.TODO:
                raise ToolError(f"Task #{old.id} has status '{old.status}'; only 'todo' tasks can be claimed.")
            self._raise_if_blocked(old.id, old.depends_on)
            return {"assignee": actor, "status": Status.IN_PROGRESS}

        _, task, _ = self._mutate(actor, task_id, plan)
        return task

    def update(self, actor: str, task_id: int, *, title: Any = None, goal: Any = None, description: Any = None,
               status: Any = None, assignee: Any = UNSET, reviewer: Any = UNSET, parent_id: Any = UNSET,
               priority: Any = None, tags: Any = None, depends_on: Any = None) -> Task:
        def plan(old: Task) -> dict[str, Any]:
            values = self._plan_fields(actor, old, title=title, goal=goal, description=description,
                                       reviewer=reviewer, parent_id=parent_id, priority=priority, tags=tags,
                                       depends_on=depends_on)
            values.update(self._plan_assignment(actor, old, assignee, status,
                                                values.get("depends_on", old.depends_on)))
            return values

        old, task, changes = self._mutate(actor, task_id, plan)
        if changes:
            self._notify_assignment(actor, old, task, changes)
            if "reviewer" in changes and task.status == Status.REVIEW:
                # the task already waits for review: the new reviewer must learn about it now
                self._notify_member(task.reviewer, actor, "n_review_requested", id=task.id, title=task.title,
                                    by=actor)
            self._on_status_change(actor, old.status, task)
        return task

    def complete(self, actor: str, task_id: int, result: str) -> Task:
        text = str(result if result is not None else "").strip()
        if not text:
            raise ToolError("Result must not be empty: say what was done, where it is and what is left.")

        def plan(old: Task) -> dict[str, Any]:
            if actor != USER and actor != old.assignee:
                raise ToolError(f"Only the assignee of task #{old.id} ({old.assignee or 'nobody'}) can complete "
                                "it. Claim it first with task_claim.")
            if old.status == Status.REVIEW:
                raise ToolError(f"Task #{old.id} is already in review (reviewer: {old.reviewer or 'the user'}). "
                                "Wait for task_review.")
            if old.status in Status.TERMINAL:
                raise ToolError(f"Task #{old.id} is already {old.status}.")
            to_review = old.reviewer is not None and old.reviewer != actor
            return {"result": text, "status": Status.REVIEW if to_review else Status.DONE}

        old, task, _ = self._mutate(actor, task_id, plan)
        if task.status == Status.REVIEW:
            self._notify_member(task.reviewer, actor, "n_review_requested", id=task.id, title=task.title, by=actor)
        else:
            self._on_status_change(actor, old.status, task)
        return task

    def review(self, actor: str, task_id: int, approve: bool, comment: str = "") -> Task:
        approved = _as_bool(approve)
        comment = str(comment or "").strip()
        if comment:
            _check_post(comment)  # before the review is committed: the comment becomes a post afterwards

        def plan(old: Task) -> dict[str, Any]:
            if old.status != Status.REVIEW:
                raise ToolError(f"Task #{old.id} has status '{old.status}', it is not waiting for review.")
            if actor != USER and actor != old.reviewer:
                raise ToolError(f"Only the reviewer of task #{old.id} ({old.reviewer or 'nobody'}) or the user can "
                                "review it.")
            if approved:
                return {"status": Status.DONE}
            # rejected work goes back to its assignee; without one it must be claimable again
            return {"status": Status.IN_PROGRESS if old.assignee else Status.TODO}

        old, task, _ = self._mutate(actor, task_id, plan)
        key = "n_review_approved" if approved else "n_review_rejected"
        self._notify_member(task.assignee, actor, key, id=task.id, title=task.title, by=actor,
                            comment=_clip(comment))
        self._on_status_change(actor, old.status, task)
        if comment:
            # the assignee already got the comment inside the review notification
            self._post(actor, task, comment, None, skip={task.assignee})
        return task

    def post(self, actor: str, task_id: int, text: str, reply_to: int | None = None) -> dict:
        return self._post(actor, self._load(task_id), text, reply_to)

    def release_all(self, name: str, actor: str) -> list[int]:
        """`name` leaves: its open tasks go back to todo unassigned (their ids are returned), and open tasks it
        was to review lose their reviewer; one waiting in review goes back to its assignee, who is told to
        complete it again (otherwise it would wait for a review that never comes)."""
        updates: list[tuple[Task, dict[str, Any]]] = []
        with self.db.transaction():
            olds = self._query(["assignee = ?", f"status IN ({_qmarks(len(_OPEN))})"], [name, *_OPEN])
            for old in olds:
                values: dict[str, Any] = {"assignee": None, "status": Status.TODO}
                if old.reviewer == name:
                    values["reviewer"] = None
                updates.append((old, values))
            reviewed = self._query(["reviewer = ?", "(assignee IS NULL OR assignee != ?)",
                                    f"status IN ({_qmarks(len(_OPEN))})"], [name, name, *_OPEN])
            for old in reviewed:
                values = {"reviewer": None}
                if old.status == Status.REVIEW:
                    values["status"] = Status.IN_PROGRESS if old.assignee else Status.TODO
                updates.append((old, values))
            for old, values in updates:
                self._write(old.id, values)
        for old, values in updates:
            changes = {f: [getattr(old, f), v] for f, v in values.items() if getattr(old, f) != v}
            task = self.get(old.id)
            self.swarm.bus.emit("task.updated", actor, task=task.to_dict(), changes=changes)
            if old.status == Status.REVIEW and old.assignee and old.assignee != name:
                self._notify_member(old.assignee, actor, "n_reviewer_left", id=task.id, title=task.title,
                                    reviewer=name)
        return [t.id for t in olds]

    # --- update planning -----------------------------------------------------------------------------

    def _plan_fields(self, actor: str, old: Task, **kw: Any) -> dict[str, Any]:
        """Validated values of the owner-only fields that actually change."""
        candidates: dict[str, Callable[[], Any]] = {}
        if kw["title"] is not None:
            candidates["title"] = lambda: _check_title(kw["title"])
        if kw["goal"] is not None:
            candidates["goal"] = lambda: str(kw["goal"]).strip()
        if kw["description"] is not None:
            candidates["description"] = lambda: str(kw["description"])
        if kw["priority"] is not None:
            candidates["priority"] = lambda: _check_priority(kw["priority"])
        if kw["tags"] is not None:
            candidates["tags"] = lambda: _norm_tags(kw["tags"])
        if kw["depends_on"] is not None:
            candidates["depends_on"] = lambda: self._check_deps(kw["depends_on"], old.id)
        if kw["reviewer"] is not UNSET:
            candidates["reviewer"] = lambda: self._resolve_opt(kw["reviewer"])
        if kw["parent_id"] is not UNSET:
            candidates["parent_id"] = lambda: self._check_parent(kw["parent_id"], old.id)
        values: dict[str, Any] = {}
        for field_name, compute in candidates.items():
            value = compute()
            if value != getattr(old, field_name):
                self._require_owner(actor, old, field_name)
                values[field_name] = value
        return values

    def _plan_assignment(self, actor: str, old: Task, assignee: Any, status: Any,
                         deps: list[int]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        new_assignee = old.assignee
        if assignee is not UNSET:
            target = self._resolve_opt(assignee)
            if target != old.assignee:
                if (actor != USER and old.assignee is not None
                        and actor not in (old.assignee, old.created_by)):
                    raise ToolError(f"Task #{old.id} is assigned to {old.assignee}; only they or the creator "
                                    f"({old.created_by}) can reassign it. Ask them in the task discussion.")
                values["assignee"] = new_assignee = target
                if status is None and old.status in (Status.IN_PROGRESS, Status.WAITING):
                    values["status"] = Status.TODO
        if status is None:
            return values
        new_status = normalize_status(status)
        release = (new_status == Status.TODO and old.status not in Status.TERMINAL
                   and assignee is UNSET and old.assignee is not None)
        if new_status == old.status and not release:
            return values
        self._check_transition(actor, old, new_status, new_assignee, deps)
        values["status"] = new_status
        if release:
            values["assignee"] = None
        return values

    def _check_transition(self, actor: str, old: Task, new: str, new_assignee: str | None,
                          deps: list[int]) -> None:
        if new == Status.REVIEW:
            raise ToolError("Status 'review' cannot be set directly: task_complete sends a task with a "
                            "reviewer to review.")
        if new == Status.DONE and actor != USER:
            raise ToolError(f"Use task_complete with a result to finish task #{old.id}.")
        if actor == USER:
            return
        holder = actor in {old.assignee, new_assignee} - {None}
        if old.status in Status.TERMINAL:
            if new == Status.TODO and actor == old.created_by:
                return
            raise ToolError(f"Task #{old.id} is {old.status}; only its creator ({old.created_by}) or the user "
                            "can reopen it (status todo).")
        if new == Status.CANCELLED:
            if holder or actor == old.created_by:
                return
            raise ToolError(f"Only the creator ({old.created_by}) or the assignee "
                            f"({old.assignee or 'nobody'}) can cancel task #{old.id}.")
        if not holder:
            hint = "Claim the task first with task_claim."
            if old.assignee and actor == old.created_by:
                hint = ("As its creator you may reassign it (assignee: another member, or '' to unassign) "
                        "or cancel it.")
            raise ToolError(f"Only the assignee of task #{old.id} ({old.assignee or 'nobody'}) can set status "
                            f"'{new}'. {hint}")
        if new == Status.IN_PROGRESS:
            self._raise_if_blocked(old.id, deps)

    @staticmethod
    def _require_owner(actor: str, task: Task, field_name: str) -> None:
        if actor in (USER, task.created_by) or (task.assignee and actor == task.assignee):
            return
        raise ToolError(f"Only the creator ({task.created_by}) or the assignee ({task.assignee or 'nobody'}) "
                        f"of task #{task.id} can change its {field_name}. Suggest the change in the discussion.")

    # --- validation ----------------------------------------------------------------------------------

    def _resolve_opt(self, name: Any) -> str | None:
        if name is None or not str(name).strip():
            return None
        return self.swarm.resolve_member(str(name), allow_user=True)

    def _lenient_member(self, name: str) -> str:
        try:
            return self.swarm.resolve_member(name, allow_user=True, allow_retired=True)
        except ToolError:
            return name

    def _status_filter(self, status: str | list[str] | None) -> list[str]:
        if status is None:
            return []
        items = status.split(",") if isinstance(status, str) else list(status)
        return _unique(normalize_status(s) for s in items if str(s).strip())

    def _check_deps(self, values: Any, task_id: int | None = None) -> list[int]:
        if isinstance(values, str):
            values = [v for v in values.split(",") if v.strip()]
        elif not isinstance(values, (list, tuple, set, frozenset)):
            values = [values]
        deps = _unique(_as_int(v, "each depends_on entry (a task id)") for v in values)
        if task_id is not None and task_id in deps:
            raise ToolError(f"Task #{task_id} cannot depend on itself.")
        known = self._statuses(deps)
        missing = [d for d in deps if d not in known]
        if missing:
            raise ToolError("Unknown task ids in depends_on: " + ", ".join(f"#{d}" for d in missing) + ".")
        if task_id is not None and deps:
            self._check_no_cycle(task_id, deps)
        return deps

    def _check_no_cycle(self, task_id: int, deps: list[int]) -> None:
        graph = {r["id"]: loads(r["depends_on"], []) for r in self.db.all("SELECT id, depends_on FROM tasks")}
        for dep in deps:
            path = _find_path(graph, dep, task_id)
            if path:
                chain = " -> ".join(f"#{n}" for n in [task_id, *path])
                raise ToolError(f"Dependency cycle: {chain}. Remove one of these dependencies.")

    def _check_parent(self, value: Any, task_id: int | None = None) -> int | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        parent = _as_int(value, "parent_id")
        if parent == task_id:
            raise ToolError(f"Task #{task_id} cannot be its own parent.")
        if self.db.one("SELECT id FROM tasks WHERE id = ?", (parent,)) is None:
            raise ToolError(f"Parent task #{parent} not found.")
        # walk up from the new parent: reaching task_id would make the hierarchy a loop
        seen: set[int] = set()
        node: int | None = parent
        while task_id is not None and node is not None and node not in seen:
            if node == task_id:
                raise ToolError(f"Task #{parent} is a subtask of #{task_id}; it cannot become its parent.")
            seen.add(node)
            node = self.db.scalar("SELECT parent_id FROM tasks WHERE id = ?", (node,))
        return parent

    def _raise_if_blocked(self, task_id: int, deps: list[int]) -> None:
        blocked = self._blockers_of(deps)
        if blocked:
            statuses = self._statuses(blocked)
            listed = ", ".join(f"#{d} [{statuses[d]}]" for d in blocked)
            raise ToolError(f"Task #{task_id} is blocked by unfinished dependencies: {listed}. Wait until they "
                            f"are done (e.g. a trigger `done({', '.join(map(str, blocked))})`).")

    # --- storage -------------------------------------------------------------------------------------

    def _load(self, task_id: Any) -> Task:
        tid = _as_int(task_id, "task_id")
        row = self.db.one("SELECT * FROM tasks WHERE id = ?", (tid,))
        if row is None:
            raise ToolError(f"Task #{tid} not found. Use `board` to see task ids.")
        return Task.from_row(row)

    def _query(self, where: list[str], params: list[Any]) -> list[Task]:
        sql = "SELECT * FROM tasks" + (" WHERE " + " AND ".join(where) if where else "")
        rows = self.db.all(sql + " ORDER BY priority DESC, id ASC", params)
        return [Task.from_row(r) for r in rows]

    def _write(self, task_id: int, values: dict[str, Any]) -> None:
        cols = {**values, "updated_at": time.time()}
        sql = "UPDATE tasks SET " + ", ".join(f"{c} = ?" for c in cols) + " WHERE id = ?"
        params = [dumps(v) if c in _JSON_FIELDS else v for c, v in cols.items()]
        self.db.execute(sql, [*params, task_id])

    def _mutate(self, actor: str, task_id: Any,
                plan: Callable[[Task], dict[str, Any]]) -> tuple[Task, Task, dict[str, list[Any]]]:
        """Atomic read-modify-write; emits one `task.updated` if anything changed."""
        with self.db.transaction():
            old = self._load(task_id)
            values = plan(old)
            changes = {f: [getattr(old, f), v] for f, v in values.items() if getattr(old, f) != v}
            if changes:
                self._write(old.id, {f: c[1] for f, c in changes.items()})
        task = self.get(old.id)
        if changes:
            log.info("task #%s updated by %s: %s", task.id, actor, ", ".join(changes))
            self.swarm.bus.emit("task.updated", actor, task=task.to_dict(), changes=changes)
        return old, task, changes

    def _statuses(self, ids: Iterable[int]) -> dict[int, str]:
        ids = list(ids)
        out: dict[int, str] = {}
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            for row in self.db.all(f"SELECT id, status FROM tasks WHERE id IN ({_qmarks(len(chunk))})", chunk):
                out[row["id"]] = row["status"]
        return out

    def _blockers_of(self, deps: list[int]) -> list[int]:
        statuses = self._statuses(deps)
        return [d for d in deps if d in statuses and statuses[d] != Status.DONE]

    def _fill(self, tasks: list[Task]) -> None:
        if not tasks:
            return
        statuses = self._statuses({d for t in tasks for d in t.depends_on})
        counts: dict[int, int] = {}
        ids = [t.id for t in tasks]
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            for row in self.db.all(f"SELECT task_id, COUNT(*) AS n FROM comments WHERE task_id IN "
                                   f"({_qmarks(len(chunk))}) GROUP BY task_id", chunk):
                counts[row["task_id"]] = row["n"]
        for task in tasks:
            task.blocked_by = [d for d in task.depends_on if d in statuses and statuses[d] != Status.DONE]
            task.comments = counts.get(task.id, 0)

    # --- discussion ----------------------------------------------------------------------------------

    def _participants(self, task: Task) -> list[str]:
        posters = [r["author"] for r in self.db.all(
            "SELECT author FROM comments WHERE task_id = ? GROUP BY author ORDER BY MIN(id)", (task.id,))]
        return _unique([task.created_by, task.assignee, task.reviewer, *posters])

    def _post(self, actor: str, task: Task, text: Any, reply_to: Any, skip: set[str | None] | None = None) -> dict:
        body = _check_post(text)
        reply = self._check_reply(task.id, reply_to)
        ts = time.time()
        post_id = self.db.insert("INSERT INTO comments(task_id, author, text, reply_to, ts) VALUES (?, ?, ?, ?, ?)",
                                 (task.id, actor, body, reply, ts))
        post = {"id": post_id, "task_id": task.id, "author": actor, "text": body, "reply_to": reply, "ts": ts}
        self.swarm.bus.emit("task.comment", actor, task_id=task.id, post=post)
        self._notify_post(actor, task, post, skip or set())
        return post

    def _check_reply(self, task_id: int, reply_to: Any) -> int | None:
        if reply_to is None or (isinstance(reply_to, str) and not reply_to.strip()):
            return None
        rid = _as_int(reply_to, "reply_to")
        row = self.db.one("SELECT task_id FROM comments WHERE id = ?", (rid,))
        if row is None or row["task_id"] != task_id:
            raise ToolError(f"Post #{rid} is not in the discussion of task #{task_id}; reply_to must be the id "
                            "of a post of the same task (see task_discussion).")
        return rid

    def _notify_post(self, actor: str, task: Task, post: dict, skip: set[str | None]) -> None:
        # retired names take part in matching so "@Old Timer" never reaches an active "Old"; retired
        # members themselves are skipped below
        mentioned = find_mentions(post["text"], [m["name"] for m in self.swarm.members(include_retired=True)])
        text = _clip(post["text"])
        for name in _unique([*self._participants(task), *mentioned]):
            if name == actor or name in skip:
                continue
            if name == USER:
                if name not in mentioned and name not in (task.created_by, task.assignee):
                    continue
            elif not self.swarm.is_member(name):
                continue
            if name in mentioned:
                self.swarm.notify(name, "n_mention", by=actor, id=task.id, title=task.title,
                                  post_id=post["id"], text=text)
                continue
            reply = ""
            if post["reply_to"]:
                reply = prompts.t(self.swarm.language_of(name), "reply_suffix", reply_to=post["reply_to"])
            self.swarm.notify(name, "n_discussion", by=actor, id=task.id, title=task.title,
                              post_id=post["id"], reply=reply, text=text)

    # --- notifications -------------------------------------------------------------------------------

    def _notify_member(self, name: str | None, actor: str, key: str, **kw: Any) -> None:
        """Task notifications go to active members only: never the actor, never USER (the UI shows tasks)."""
        if not name or name == actor or name == USER or not self.swarm.is_member(name):
            return
        self.swarm.notify(name, key, **kw)

    def _notify_assignment(self, actor: str, old: Task, task: Task, changes: dict[str, list[Any]]) -> None:
        if "assignee" in changes:
            removed, added = changes["assignee"]
            self._notify_member(added, actor, "n_assigned", id=task.id, title=task.title, by=actor)
            self._notify_member(removed, actor, "n_unassigned", id=task.id, title=task.title, by=actor)
        elif old.status in Status.TERMINAL and task.status in Status.OPEN:
            # reopened (creator: to todo; the user: to any open status): back on its assignee's plate
            self._notify_member(task.assignee, actor, "n_assigned", id=task.id, title=task.title, by=actor)

    def _on_status_change(self, actor: str, old_status: str, task: Task) -> None:
        if task.status == old_status:
            return
        if task.status == Status.DONE:
            self._notify_unblocked(actor, task.id)
        if task.status in Status.TERMINAL:
            # The creator (usually the planner) must act when a task failed or was cancelled, or when it
            # waits on something; a task done is news for its next wake-up, not one more full-history LLM
            # call per finished task (n_all_done wakes someone when everything is finished).
            creator = task.created_by
            wake = task.status != Status.DONE or any(t.status == Status.WAITING for t in self.open_tasks_of(creator))
            self._notify_member(creator, actor, "n_task_closed", wake=wake, id=task.id, title=task.title,
                                status=task.status, by=actor, result=_clip(task.result or "-"))

    def _notify_unblocked(self, actor: str, done_id: int) -> None:
        dependents = [t for t in self._query([f"status IN ({_qmarks(len(_OPEN))})"], list(_OPEN))
                      if done_id in t.depends_on]
        for task in dependents:
            if task.assignee and not self._blockers_of(task.depends_on):
                self._notify_member(task.assignee, actor, "n_unblocked", id=task.id, title=task.title)


def _find_path(graph: dict[int, list[int]], start: int, target: int) -> list[int] | None:
    """DFS along depends_on edges; the path start..target if target is reachable."""
    stack: list[tuple[int, list[int]]] = [(start, [start])]
    seen: set[int] = set()
    while stack:
        node, path = stack.pop()
        if node == target:
            return path
        if node in seen:
            continue
        seen.add(node)
        for nxt in graph.get(node, []):
            stack.append((nxt, [*path, nxt]))
    return None
