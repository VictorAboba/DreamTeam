"""Task board: tasks with goals, dependencies and review, plus a discussion thread per task (SPEC §4)."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import replace
from typing import Any, Callable, Iterable

from . import prompts, roles
from .db import Database, dumps, loads
from .models import CREW_MAX as CREW_MAX
from .models import USER, AgentKind, AgentStatus, Status, Task, ToolError
from .tools.fmt import trim

log = logging.getLogger(__name__)

UNSET: Any = object()  # sentinel for "not passed" on nullable fields

MAX_TITLE = 200
MAX_POST = 20000
NOTIFY_TEXT_LIMIT = 1500
# crew tasks (docs/SPEC.md §4.2)
MAX_DIRECTIONS = 20        # directions passed in one call
CREW_DRAFT_CHARS = 8000    # the largest budget of the draft of a crew task's result ("Finishing")
CREW_DRAFT_MIN = 300       # a cut result keeps at least this many characters, else only its task_get pointer
OWNER_LIST_MAX = 20        # directions listed in the owner sentence, then crew_directions_more
MARK_SUBTASKS_DONE = "subtasks done"      # liveness marks (stall_marks)
MARK_FREE_DIRECTIONS = "free directions"

_USER_ALIASES = ("user", "пользователь")
_JSON_FIELDS = ("depends_on", "tags")
_OPEN = tuple(s for s in Status.ALL if s in Status.OPEN)
_WORKING = (Status.IN_PROGRESS, Status.WAITING)            # a direction in these takes a place in the working set
_TAKEABLE = (Status.TODO, Status.IN_PROGRESS, Status.WAITING)  # a crew task whose directions can be taken
_CHUNK = 500
_INDENT = "    "           # the results in the draft of a crew task's result


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


def _reviewer_is_assignee(reviewer: str, task: str) -> ToolError:
    return ToolError(f"{reviewer} is the assignee of {task}: a task is reviewed by someone other than its assignee. "
                     "Choose another member or 'user', or leave the reviewer empty (a reviewer is picked when the "
                     "task is completed).")


def _clip(text: str, limit: int = NOTIFY_TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _quote(title: str, lang: str) -> str:
    """A title in the quotes of the recipient's language: "title" / «title»."""
    return f"«{title}»" if lang == "ru" else f"\"{title}\""


def _indented(text: str) -> str:
    return "\n".join(_INDENT + line for line in text.split("\n"))


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


# --- similar tasks (task_create warns about likely duplicates) ---------------------------------------------
SIMILAR_MAX = 3            # at most this many similar tasks are named
SIMILAR_SCORE = 0.5        # the share of the shorter title's words found in the other title
SIMILAR_SURE = 0.75        # ... from which the titles alone decide; below it the goals must add a shared word
SIMILAR_SHARED = 2         # ... and at least this many title words in common
_STEM = 5                  # crude stemming: a word's first letters ("crashes" ~ "crash", "парсера" ~ "парсер")
_SIMILAR_WORD = re.compile(r"[^\W_]+")
# words that say nothing about what a task is (generic verbs of task titles, function words), en + ru
_SIMILAR_STOP = frozenset("""
the and for with from into onto that this these those are was were will can should must not all any its our your
their has have had been being out off per via use using make add new fix get set run task tasks work done
implement implementation create write update check ensure
что это как для при без над под или его она они оно все всё был была были будет есть нет надо нужно также
только если чтобы через после перед между задача задачу задачи сделать добавить создать написать исправить
обновить реализовать проверить работа работу
""".split())
# a planner splits work by kind and by side, in words the overlap ignores or cannot weigh: titles naming different
# kinds of work ("Optimize the step function" / "Verify the step function") or opposite sides ("lower bound" /
# "upper bound", "staging" / "production") are different tasks. Entries of 4+ letters match as prefixes.
_SIMILAR_KINDS = (
    ("fix", "fixes", "fixing", "repair", "debug", "исправ", "почин", "устран"),
    ("implement", "add", "adds", "adding", "create", "write", "build", "develop", "напис", "добав", "созда",
     "реализ", "разраб"),
    ("verif", "check", "checks", "validat", "review", "audit", "провер", "валид", "ревью"),
    ("optimi", "speed", "tune", "improv", "оптимиз", "ускор", "улучш"),
    ("benchmark", "measur", "profil", "бенчмарк", "замер", "измер"),
    ("research", "search", "investigat", "explor", "survey", "исслед", "изуч", "поиск", "найти", "найди"),
    ("document", "описа", "задокумент"),
    ("remove", "delete", "drop", "удал"),
    ("deploy", "release", "publish", "развер", "выкат", "опубли"),
)
_SIMILAR_OPPOSITE = (
    (("lower",), ("upper",)), (("min", "minim"), ("max", "maxim")), (("нижн",), ("верхн",)),
    (("миним",), ("максим",)), (("staging", "stage"), ("production", "prod")), (("frontend",), ("backend",)),
    (("client",), ("server",)), (("клиент",), ("сервер",)), (("input",), ("output",)),
    (("ввод", "вход"), ("вывод", "выход")), (("import",), ("export",)), (("импорт",), ("экспорт",)),
    (("light",), ("dark",)), (("светл",), ("темн",)),
)


def _raw_words(*texts: str | None) -> list[str]:
    return [w for text in texts for w in _SIMILAR_WORD.findall((text or "").lower().replace("ё", "е"))]


def task_words(*texts: str | None) -> set[str]:
    """The content words of a task's texts: lower-cased, without punctuation, short words, numbers and stop words,
    each cut to its first _STEM letters."""
    return {w[:_STEM] for w in _raw_words(*texts) if len(w) >= 3 and not w.isdigit() and w not in _SIMILAR_STOP}


def _markers(*texts: str | None) -> set[str]:
    """The tokens with a digit (parameters: "N=100" -> "100", "C5", "1e-3" -> "1e", "3"), which task_words drops."""
    return {w for w in _raw_words(*texts) if any(c.isdigit() for c in w)}


def _has(words: list[str], entries: tuple[str, ...]) -> bool:
    return any(w == e or (len(e) >= 4 and w.startswith(e)) for w in words for e in entries)


def _distinct(title: str, goal: str, other_title: str, other_goal: str) -> bool:
    """The two tasks are told apart by a parameter, a kind of work or a side, whatever their other words."""
    marks, other_marks = _markers(title), _markers(other_title)
    if marks and other_marks and marks != other_marks:          # N=100 / N=200 (a parameter sweep)
        return True
    marks, other_marks = marks | _markers(goal), other_marks | _markers(other_goal)
    if marks and other_marks and not marks & other_marks:
        return True
    words, other_words = _raw_words(title), _raw_words(other_title)
    kinds = {i for i, kind in enumerate(_SIMILAR_KINDS) if _has(words, kind)}
    other_kinds = {i for i, kind in enumerate(_SIMILAR_KINDS) if _has(other_words, kind)}
    if kinds and other_kinds and not kinds & other_kinds:      # optimize / verify
        return True
    for side, opposite in _SIMILAR_OPPOSITE:                     # lower / upper
        for a, b in ((words, other_words), (other_words, words)):
            if _has(a, side) and not _has(a, opposite) and _has(b, opposite) and not _has(b, side):
                return True
    return False


def _same_word(a: str, b: str) -> bool:
    """Equal stems, or one a prefix of the other of at least 4 letters ("ввод" ~ "вводе")."""
    short, other = (a, b) if len(a) <= len(b) else (b, a)
    return a == b or (len(short) >= 4 and other.startswith(short))


def _overlap(a: set[str], b: set[str]) -> tuple[int, float]:
    """(words of the smaller set found in the other, their share of the smaller set)."""
    if not a or not b:
        return 0, 0.0
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    shared = sum(1 for w in small if any(_same_word(w, x) for x in big))
    return shared, shared / len(small)


def similarity(title: str, goal: str, other_title: str, other_goal: str) -> float:
    """How much two tasks look alike, 0..1. The titles decide: the share of the shorter one's words found in the
    other, with at least SIMILAR_SHARED in common; below SIMILAR_SURE the goals must confirm it (title + goal still
    reach SIMILAR_SCORE with a shared word more). Goals never make a match alone: siblings of one plan repeat the
    swarm goal's words in theirs. Tasks told apart by a parameter, a kind of work or a side score 0."""
    shared, score = _overlap(task_words(title), task_words(other_title))
    if shared < SIMILAR_SHARED or score < SIMILAR_SCORE or _distinct(title, goal, other_title, other_goal):
        return 0.0
    if score < SIMILAR_SURE:
        more, whole = _overlap(task_words(title, goal), task_words(other_title, other_goal))
        if more <= shared or whole < SIMILAR_SCORE:
            return 0.0
    return score


def similar_tasks(title: str, goal: str, candidates: Iterable[Task], limit: int = SIMILAR_MAX) -> list[Task]:
    """The candidates that look like a task with this title and goal (similarity >= SIMILAR_SCORE), best first
    (then the older one), at most `limit`."""
    scored = [(similarity(title, goal, t.title, t.goal), t) for t in candidates]
    hits = sorted(((s, t) for s, t in scored if s >= SIMILAR_SCORE), key=lambda st: (-st[0], st[1].id))
    return [t for _, t in hits[:limit]]


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

    def similar_open(self, task: Task) -> list[Task]:
        """Open tasks of other creators that look like `task` (similar_tasks): not the task itself, its parent or
        the tasks it depends on, which share its words by design, nor the creator's own tasks (a planner splits
        work into alike tasks on purpose; run 006's duplicates came from different members)."""
        skip = {task.id, task.parent_id, *task.depends_on}
        return similar_tasks(task.title, task.goal,
                             [t for t in self.list() if t.id not in skip and t.created_by != task.created_by])

    def ready_unassigned(self) -> list[Task]:
        """Todo tasks without an assignee or blockers; a direction only while its crew task is todo, in_progress or
        waiting, not blocked, and has a free place (docs/SPEC.md §4.2 "Offers")."""
        ready = [t for t in self.list(status=Status.TODO) if t.assignee is None and not t.blocked_by]
        parents = {p.id: p for p in self._load_many(t.parent_id for t in ready if t.parent_id is not None)}
        return [t for t in ready if t.parent_id not in parents or self._takes_directions(parents[t.parent_id])]

    def open_tasks_of(self, name: str) -> list[Task]:
        tasks = self._query(["assignee = ?", f"status IN ({_qmarks(len(_OPEN))})"], [name, *_OPEN])
        self._fill(tasks)
        return tasks

    # --- reads: subtasks and crew tasks (docs/SPEC.md §4.2) -----------------------------------------------------

    def subtasks_of(self, task_id: int, open_only: bool = False) -> list[Task]:
        """The subtasks of a task (parent_id == task_id) in the order they were created (id ascending), filled."""
        sql = "SELECT * FROM tasks WHERE parent_id = ?"
        params: list[Any] = [_as_int(task_id, "task_id")]
        if open_only:
            sql += f" AND status IN ({_qmarks(len(_OPEN))})"
            params += _OPEN
        tasks = [Task.from_row(r) for r in self.db.all(sql + " ORDER BY id ASC", params)]
        self._fill(tasks)
        return tasks

    def crew_working(self, task: Task) -> list[str]:
        """The working set of a crew task: its assignee while it is open, then the assignees of its in_progress or
        waiting subtasks (id ascending), each once, never USER. Read from the database now (inside a transaction:
        the state the transaction sees)."""
        return self._working_sets([task]).get(task.id, [])

    def contributors_of(self, task: Task) -> set[str]:
        """The assignees of its subtasks, in any status."""
        rows = self.db.all("SELECT DISTINCT assignee FROM tasks WHERE parent_id = ? AND assignee IS NOT NULL",
                           (task.id,))
        return {r["assignee"] for r in rows}

    def finish_states(self, ids: Iterable[int]) -> dict[int, str | None]:
        """The finish state of each parent: None while it has no subtask or an open one, else "failed" for a crew
        task with a failed direction, else "finished" (one grouped query per chunk of ids)."""
        return {tid: state for tid, (state, _) in self._finish_info(ids).items()}

    def _finish_info(self, ids: Iterable[int]) -> dict[int, tuple[str | None, dict[str, int]]]:
        """(finish state, subtasks per status) of each id; ids of missing tasks get (None, {})."""
        wanted = _unique(ids)
        crews: dict[int, int] = {}
        counts: dict[int, dict[str, int]] = {}
        for i in range(0, len(wanted), _CHUNK):
            chunk = wanted[i:i + _CHUNK]
            rows = self.db.all("SELECT p.id AS id, p.crew AS crew, s.status AS status, COUNT(s.id) AS n FROM tasks p"
                               f" LEFT JOIN tasks s ON s.parent_id = p.id WHERE p.id IN ({_qmarks(len(chunk))})"
                               " GROUP BY p.id, s.status", chunk)
            for row in rows:
                crews[row["id"]] = row["crew"] or 0
                if row["status"] is not None:
                    counts.setdefault(row["id"], {})[row["status"]] = row["n"]
        out: dict[int, tuple[str | None, dict[str, int]]] = {}
        for tid in wanted:
            per_status = counts.get(tid, {})
            state: str | None = None
            if per_status and not any(s in Status.OPEN for s in per_status):
                state = "failed" if crews.get(tid, 0) >= 2 and per_status.get(Status.FAILED) else "finished"
            out[tid] = (state, per_status)
        return out

    def free_directions(self, task: Task) -> list[Task]:
        """The directions of a crew task anyone may take now: todo, without an assignee, while the crew task is
        todo, in_progress or waiting and not blocked (no free place needed: the owner's claim takes none)."""
        if task.crew < 2 or task.status not in _TAKEABLE or self._blockers_of(task.depends_on):
            return []
        return [s for s in self.subtasks_of(task.id, open_only=True) if s.status == Status.TODO and s.assignee is None]

    def has_open_directions(self, task: Task) -> bool:
        """A crew task with an open direction: it waits for them (liveness leaves it out of the stalled ids)."""
        if task.crew < 2:
            return False
        return self.db.one(f"SELECT 1 FROM tasks WHERE parent_id = ? AND status IN ({_qmarks(len(_OPEN))}) LIMIT 1",
                           (task.id, *_OPEN)) is not None

    def stall_marks(self, name: str) -> frozenset[tuple[int, str]]:
        """The liveness marks of `name` (docs/SPEC.md §4.2 "Finishing", Liveness): (N, "subtasks done") for each of
        its tasks in in_progress or waiting with at least one subtask, all of them terminal; (N, "free directions")
        for each crew task it holds that has a free direction while it holds none of its directions in in_progress
        or waiting."""
        held = self._query(["assignee = ?", f"status IN ({_qmarks(len(_TAKEABLE))})"], [name, *_TAKEABLE])
        states = self.finish_states(t.id for t in held if t.status in _WORKING)
        marks = {(tid, MARK_SUBTASKS_DONE) for tid, state in states.items() if state is not None}
        for task in held:
            if task.crew < 2 or (task.id, MARK_SUBTASKS_DONE) in marks:
                continue
            subs = self.subtasks_of(task.id, open_only=True)
            if any(s.assignee == name and s.status in _WORKING for s in subs):
                continue
            if self.free_directions(task):
                marks.add((task.id, MARK_FREE_DIRECTIONS))
        return frozenset(marks)

    def mark_text(self, name: str, marks: Iterable[tuple[int, str]]) -> str:
        """One sentence per mark, in `name`'s language, sorted by (id, kind), each starting with a space: the
        owner form of the ending that applies now for "subtasks done" (the drafts of several crew tasks share one
        budget, each built with draft_budget() // their number), the free directions for "free directions". A mark
        that no longer applies gives no sentence."""
        lang = self.swarm.language_of(name)
        marks = sorted(set(marks))
        tasks = {t.id: t for t in self._load_many(tid for tid, _ in marks)}
        info = self._finish_info(tid for tid, kind in marks if kind == MARK_SUBTASKS_DONE)
        drafts = sum(1 for tid, (state, counts) in info.items()
                     if tid in tasks and tasks[tid].crew >= 2 and state == "finished" and counts.get(Status.DONE))
        share = self.draft_budget() // drafts if drafts else None
        parts: list[str] = []
        for tid, kind in marks:
            task = tasks.get(tid)
            if task is None:
                continue
            if kind == MARK_SUBTASKS_DONE and info[tid][0] is not None:
                ending = self._ending(task, info[tid][0], "owner", lang, budget=share)
                parts.append(prompts.t(lang, "mark_subtasks_done", id=task.id, title=task.title, ending=ending))
            elif kind == MARK_FREE_DIRECTIONS:
                free = self.free_directions(task)
                if free:
                    parts.append(prompts.t(lang, "mark_free_directions", id=task.id,
                                           directions=self._direction_names(free, lang)))
        return "".join(parts)

    @property
    def independent_review(self) -> bool:
        """[swarm] independent_review (default true): an agent never approves work it executed (SPEC §4). Read on
        every call, so reconfiguration and session overrides apply at once."""
        return bool(getattr(getattr(self.swarm.config, "swarm", None), "independent_review", True))

    @property
    def crew_review(self) -> bool:
        """[swarm] crew_review (default true): the review of a crew task with a done direction is its check of the
        whole (docs/SPEC.md §4.2 "Finishing"). Read on every call, like independent_review."""
        return bool(getattr(getattr(self.swarm.config, "swarm", None), "crew_review", True))

    def reviews_of(self, name: str) -> list[Task]:
        """Tasks waiting for `name`'s review (status review, reviewer == name): priority desc, id asc, filled."""
        tasks = self._query(["reviewer = ?", "status = ?"], [name, Status.REVIEW])
        self._fill(tasks)
        return tasks

    def pick_reviewer(self, task: Task, exclude: Iterable[str] = ()) -> str:
        """The reviewer the board chooses for `task` (SPEC §4 "Independent review"): an active LLM member other than
        its assignee and `exclude`; members who did not create the task first, then the reviewer role, then idle
        ones, then the fewest open tasks + pending reviews, then by name. USER when nobody qualifies. External
        members are never picked (they may be offline), only designated."""
        banned = {task.assignee, *exclude} - {None}
        pool = [m for m in self.swarm.members() if m["kind"] == AgentKind.LLM and m["name"] not in banned]
        if not pool:
            return USER
        load: dict[str, int] = {}
        for row in self.db.all(f"SELECT assignee, reviewer, status FROM tasks WHERE status IN ({_qmarks(len(_OPEN))})",
                               _OPEN):
            if row["assignee"]:
                load[row["assignee"]] = load.get(row["assignee"], 0) + 1
            if row["status"] == Status.REVIEW and row["reviewer"]:
                load[row["reviewer"]] = load.get(row["reviewer"], 0) + 1
        rank = {AgentStatus.IDLE: 0, AgentStatus.WORKING: 1}
        best = min(pool, key=lambda m: (m["name"] == task.created_by, self._role_rank(m), rank.get(m["status"], 2),
                                        load.get(m["name"], 0), m["name"]))
        return best["name"]

    @staticmethod
    def _role_rank(member: dict) -> int:
        """0 for a member in the catalog's reviewer role («Ревьюер»/Reviewer), which is preferred for reviews; 1
        otherwise (testers are not preferred: their own test tasks would starve)."""
        return 0 if roles.role_key(member.get("role")) == "reviewer" else 1

    def _valid_reviewer(self, task: Task, name: str | None) -> bool:
        return name is not None and name != task.assignee and (name == USER or self.swarm.is_member(name))

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
        if self.independent_review and reviewer is not None and reviewer != USER and reviewer == assignee:
            raise _reviewer_is_assignee(reviewer, "the task")
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
            self._raise_if_blocked(old.id, old.depends_on, assignee=old.assignee == actor)
            values: dict[str, Any] = {"assignee": actor, "status": Status.IN_PROGRESS}
            if old.reviewer == actor and self.independent_review:
                values["reviewer"] = None  # the designated reviewer executes it now: someone else will review it
            return values

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
            if self.independent_review:
                self._plan_review_fields(actor, old, values)
            return values

        old, task, changes = self._mutate(actor, task_id, plan)
        if changes:
            self._notify_assignment(actor, old, task, changes)
            if task.status == Status.REVIEW and ("reviewer" in changes or old.status != Status.REVIEW):
                # the task already waits for review: the new reviewer must learn about it now
                self._request_review(actor, task, auto=False)
            if old.status == Status.REVIEW and task.status == Status.DONE:
                # the user set done: an approval, which wakes the assignee like any verdict
                self._notify_member(task.assignee, actor, "n_review_approved", id=task.id,
                                    title=task.title, by=actor, comment="")
            self._review_dropped(actor, old, task)
            self._on_status_change(actor, old.status, task)
            if "depends_on" in changes:
                self._deps_changed(actor, old, task)
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
            if actor == USER or not self.independent_review:
                to_review = old.reviewer is not None and old.reviewer != actor
                return {"result": text, "status": Status.REVIEW if to_review else Status.DONE}
            # an agent's work always goes to review, never by its executor (old.assignee == actor here)
            reviewer = old.reviewer if self._valid_reviewer(old, old.reviewer) else self.pick_reviewer(old)
            return {"result": text, "status": Status.REVIEW, "reviewer": reviewer}

        old, task, changes = self._mutate(actor, task_id, plan)
        if task.status == Status.REVIEW:
            self._request_review(actor, task, auto="reviewer" in changes)
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
            if actor != USER and actor == old.assignee and self.independent_review:
                # also for a row written while the switch was off with the executor as its reviewer
                other = old.reviewer if old.reviewer not in (USER, actor) else None
                raise ToolError(f"You are the assignee of task #{old.id}: your own work is reviewed by someone else "
                                f"({other or 'the user'}). Wait for the verdict; you will be notified.")
            if actor != USER and actor != old.reviewer:
                raise ToolError(f"Only the reviewer of task #{old.id} ({old.reviewer or 'nobody'}) or the user can "
                                "review it.")
            if approved:
                return {"status": Status.DONE}
            # rejected work goes back to its assignee; without one it must be claimable again
            return {"status": Status.IN_PROGRESS if old.assignee else Status.TODO}

        old, task, _ = self._mutate(actor, task_id, plan)
        # every verdict wakes the assignee: task_complete told it that it will be told the verdict, so an
        # executor that ended its turn with `wait` is waiting for exactly this (a rejection must be fixed now)
        if approved:
            self._notify_member(task.assignee, actor, "n_review_approved", id=task.id, title=task.title, by=actor,
                                comment=_clip(comment))
        else:
            # what a returned task asks of its assignee, in the assignee's language
            ending = prompts.t(self.swarm.language_of(task.assignee), "review_rejected_plain")
            self._notify_member(task.assignee, actor, "n_review_rejected", id=task.id, title=task.title, by=actor,
                                comment=_clip(comment), ending=ending)
        self._on_status_change(actor, old.status, task)
        if comment:
            # the assignee already got the comment inside the review notification
            self._post(actor, task, comment, None, skip={task.assignee})
        self._review_dropped(actor, old, task)
        return task

    def post(self, actor: str, task_id: int, text: str, reply_to: int | None = None) -> dict:
        return self._post(actor, self._load(task_id), text, reply_to)

    def release_all(self, name: str, actor: str) -> list[int]:
        """`name` leaves (it is still an active member while this runs): its open tasks go back to todo
        unassigned (their ids are returned). With independent review, its tasks waiting in review stay there
        without an assignee (done work: the review goes on, they are not returned), and the reviews it was to
        do go to the next pick, or USER. Without it, open tasks it was to review lose their reviewer, and one
        waiting in review goes back to its assignee, who is told to complete it again."""
        independent = self.independent_review
        updates: list[tuple[Task, dict[str, Any]]] = []
        with self.db.transaction():
            olds = self._query(["assignee = ?", f"status IN ({_qmarks(len(_OPEN))})"], [name, *_OPEN])
            for old in olds:
                if independent and old.status == Status.REVIEW:
                    values: dict[str, Any] = {"assignee": None}
                    if old.reviewer == name:  # only a row written while the switch was off
                        values["reviewer"] = self.pick_reviewer(old, exclude={name})
                    updates.append((old, values))
                    continue
                values = {"assignee": None, "status": Status.TODO}
                if old.reviewer == name:
                    values["reviewer"] = None
                updates.append((old, values))
            reviewed = self._query(["reviewer = ?", "(assignee IS NULL OR assignee != ?)",
                                    f"status IN ({_qmarks(len(_OPEN))})"], [name, name, *_OPEN])
            for old in reviewed:
                values = {"reviewer": None}
                if old.status == Status.REVIEW:
                    if independent:
                        values["reviewer"] = self.pick_reviewer(old, exclude={name})
                    else:
                        values["status"] = Status.IN_PROGRESS if old.assignee else Status.TODO
                updates.append((old, values))
            for old, values in updates:
                self._write(old.id, values)
        for old, values in updates:
            changes = {f: [getattr(old, f), v] for f, v in values.items() if getattr(old, f) != v}
            task = self.get(old.id)
            self.swarm.bus.emit("task.updated", actor, task=task.to_dict(), changes=changes)
            if old.status == Status.REVIEW and values.get("reviewer"):  # re-picked
                # the review comes from its executor; from the member that left when the executor has gone too
                self._request_review(old.assignee or old.reviewer, task, auto=True,
                                     handed_over_from=None if old.assignee else old.reviewer)
            elif not independent and old.status == Status.REVIEW and old.assignee and old.assignee != name:
                self._notify_member(old.assignee, actor, "n_reviewer_left", id=task.id, title=task.title,
                                    reviewer=name)
        return [t.id for t in olds if not (independent and t.status == Status.REVIEW)]

    def hand_over_review(self, task_id: int, exclude: Iterable[str]) -> Task | None:
        """The reviewer of a task in review did not review it (ignored the reminder, or its runtime is in error):
        give the review to the next pick, or USER when nobody else qualifies (SPEC §4 "Independent review").
        Scheduler-driven: the task.updated event has no actor. None when the task is no longer in review."""
        def plan(old: Task) -> dict[str, Any]:
            if old.status != Status.REVIEW or not old.reviewer:
                return {}
            return {"reviewer": self.pick_reviewer(old, exclude={*exclude, old.reviewer})}

        old, task, changes = self._mutate(None, task_id, plan)
        if not changes:
            return None
        # the review comes from its executor, or from the reviewer that let it lapse when the executor has left
        self._request_review(task.assignee or old.reviewer, task, auto=True, handed_over_from=old.reviewer)
        self._notify_member(old.reviewer, "", "n_review_reassigned", wake=False, id=task.id, title=task.title,
                            reviewer=task.reviewer)
        return task

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
            if field_name == "depends_on":
                value = self._plan_deps(actor, old, value)
            if value != getattr(old, field_name):
                self._require_owner(actor, old, field_name)
                values[field_name] = value
        return values

    @staticmethod
    def _plan_deps(actor: str, old: Task, deps: list[int]) -> list[int]:
        """depends_on: its creator, its assignee and the user change it. When an assignee who did not create the
        task removes entries (a dependency it can do without, e.g. an optional task a report need not wait for),
        the discussion records it and the creator is told (_deps_changed). Removing a done entry is harmless."""
        if deps == old.depends_on or actor in (USER, old.created_by):
            return deps
        if old.assignee and actor == old.assignee:
            if set(deps) <= set(old.depends_on):  # removals keep the plan's order (a new order alone is no change)
                return [d for d in old.depends_on if d in deps]
            return deps
        raise ToolError(f"Only the creator ({old.created_by}) or the assignee ({old.assignee or 'nobody'}) of task "
                        f"#{old.id} can change its depends_on (the assignee may remove dependencies the task can do "
                        "without). Suggest the change in the discussion.")

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

    def _plan_review_fields(self, actor: str, old: Task, values: dict[str, Any]) -> None:
        """Independent review on an update: the assignee of a task never becomes its reviewer, and a task in
        review keeps its executor as assignee (only the user may reassign it) and always has a reviewer. Raises,
        or adjusts `values` in place."""
        status = values.get("status", old.status)
        assignee = values.get("assignee", old.assignee)
        reviewer = values.get("reviewer", old.reviewer)
        in_review = old.status == Status.REVIEW and status == Status.REVIEW
        if in_review and "assignee" in values and actor != USER:
            # otherwise the executor could hand its task to the reviewer and be picked to review it itself
            raise ToolError(f"Task #{old.id} is waiting for review, so its assignee cannot change now: the reviewer "
                            f"approves it or returns it to {old.assignee or 'the board'}.")
        if in_review and "reviewer" in values and reviewer is None:
            raise ToolError(f"Task #{old.id} is waiting for review, so it needs a reviewer: pass another member or "
                            "'user' instead of clearing it.")
        if reviewer is None or reviewer == USER or reviewer != assignee:
            return
        if "reviewer" in values:
            raise _reviewer_is_assignee(reviewer, f"task #{old.id}")
        if "assignee" not in values:
            return  # a row written while the switch was off: complete() and review() handle it
        # the assignee changed to the designated reviewer
        if in_review:  # only the user gets here
            values["reviewer"] = self.pick_reviewer(replace(old, assignee=assignee), exclude={old.assignee} - {None})
        else:
            values["reviewer"] = None

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
            # the removal hint only for the current assignee: one becoming it in this call cannot drop deps yet
            self._raise_if_blocked(old.id, deps, assignee=actor == old.assignee)

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
        if task_id is not None and any(row["id"] == task_id for row in self._ancestors(parent)):
            raise ToolError(f"Task #{parent} is a subtask of #{task_id}; it cannot become its parent.")
        return parent

    def _ancestors(self, parent_id: int | None) -> list[Any]:
        """The rows (id, crew, status, created_by, assignee, parent_id) from `parent_id` up to the top, nearest
        first; the walk stops at a missing row or a loop."""
        rows: list[Any] = []
        seen: set[int] = set()
        node = parent_id
        while node is not None and node not in seen:
            row = self.db.one("SELECT id, crew, status, created_by, assignee, parent_id FROM tasks WHERE id = ?",
                              (node,))
            if row is None:
                break
            seen.add(node)
            rows.append(row)
            node = row["parent_id"]
        return rows

    def _has_crew_below(self, task_id: int) -> bool:
        """A crew task among the descendants of `task_id` (one query, walked in memory)."""
        children: dict[int, list[tuple[int, int]]] = {}
        for row in self.db.all("SELECT id, parent_id, crew FROM tasks WHERE parent_id IS NOT NULL"):
            children.setdefault(row["parent_id"], []).append((row["id"], row["crew"] or 0))
        stack, seen = [task_id], {task_id}
        while stack:
            for child, crew in children.get(stack.pop(), []):
                if crew >= 2:
                    return True
                if child not in seen:
                    seen.add(child)
                    stack.append(child)
        return False

    def _raise_if_blocked(self, task_id: int, deps: list[int], assignee: bool = False) -> None:
        """`assignee`: the actor is the task's assignee, who may remove dependencies it can do without."""
        blocked = self._blockers_of(deps)
        if blocked:
            statuses = self._statuses(blocked)
            listed = ", ".join(f"#{d} [{statuses[d]}]" for d in blocked)
            hint = (" If the task can be done without some of them, remove those from its depends_on (task_update) "
                    "and say why in its discussion." if assignee else "")
            raise ToolError(f"Task #{task_id} is blocked by unfinished dependencies: {listed}. Wait until they "
                            f"are done (e.g. a trigger `done({', '.join(map(str, blocked))})`).{hint}")

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

    def _load_many(self, ids: Iterable[int]) -> list[Task]:
        """The tasks with these ids that exist, id ascending, filled."""
        wanted = sorted(set(ids) - {None})
        tasks: list[Task] = []
        for i in range(0, len(wanted), _CHUNK):
            chunk = wanted[i:i + _CHUNK]
            tasks += [Task.from_row(r) for r in self.db.all(
                f"SELECT * FROM tasks WHERE id IN ({_qmarks(len(chunk))}) ORDER BY id ASC", chunk)]
        self._fill(tasks)
        return tasks

    def _working_sets(self, tasks: list[Task]) -> dict[int, list[str]]:
        """The working set of each of `tasks` (crew_working), with one query per chunk."""
        out = {t.id: [t.assignee] if t.status in Status.OPEN and t.assignee not in (None, USER) else [] for t in tasks}
        ids = list(out)
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i:i + _CHUNK]
            for row in self.db.all(f"SELECT parent_id, assignee FROM tasks WHERE parent_id IN ({_qmarks(len(chunk))})"
                                   f" AND status IN ({_qmarks(len(_WORKING))}) AND assignee IS NOT NULL ORDER BY id",
                                   [*chunk, *_WORKING]):
                working = out[row["parent_id"]]
                if row["assignee"] != USER and row["assignee"] not in working:
                    working.append(row["assignee"])
        return out

    @staticmethod
    def _takes_directions(parent: Task) -> bool:
        """`parent` (filled) is no crew task, or a crew task whose free directions may be offered now: todo,
        in_progress or waiting, not blocked, with a free place."""
        if parent.crew < 2:
            return True
        return (parent.status in _TAKEABLE and not parent.blocked_by
                and len(parent.crew_working) < parent.crew)

    def _write(self, task_id: int, values: dict[str, Any]) -> None:
        cols = {**values, "updated_at": time.time()}
        sql = "UPDATE tasks SET " + ", ".join(f"{c} = ?" for c in cols) + " WHERE id = ?"
        params = [dumps(v) if c in _JSON_FIELDS else v for c, v in cols.items()]
        self.db.execute(sql, [*params, task_id])

    def _mutate(self, actor: str | None, task_id: Any,
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
        working = self._working_sets([t for t in tasks if t.crew >= 2])
        for task in tasks:
            task.blocked_by = [d for d in task.depends_on if d in statuses and statuses[d] != Status.DONE]
            task.comments = counts.get(task.id, 0)
            task.crew_working = working.get(task.id, [])

    # --- discussion ----------------------------------------------------------------------------------

    def _participants(self, task: Task) -> list[str]:
        """Its creator, assignee, reviewer and posters; for a crew task also the assignees of its open directions
        (in review too): its discussion is the crew's channel."""
        posters = [r["author"] for r in self.db.all(
            "SELECT author FROM comments WHERE task_id = ? GROUP BY author ORDER BY MIN(id)", (task.id,))]
        crew: list[str] = []
        if task.crew >= 2:
            crew = [r["assignee"] for r in self.db.all(
                f"SELECT assignee FROM tasks WHERE parent_id = ? AND assignee IS NOT NULL AND status IN "
                f"({_qmarks(len(_OPEN))}) ORDER BY id", (task.id, *_OPEN))]
        return _unique([task.created_by, task.assignee, task.reviewer, *posters, *crew])

    def _post(self, actor: str, task: Task, text: Any, reply_to: Any, skip: set[str | None] | None = None,
              notify: bool = True) -> dict:
        """notify=False: a record the board writes on the actor's behalf (its own notification tells whom it
        concerns), so participants are not woken by it."""
        body = _check_post(text)
        reply = self._check_reply(task.id, reply_to)
        ts = time.time()
        post_id = self.db.insert("INSERT INTO comments(task_id, author, text, reply_to, ts) VALUES (?, ?, ?, ?, ?)",
                                 (task.id, actor, body, reply, ts))
        post = {"id": post_id, "task_id": task.id, "author": actor, "text": body, "reply_to": reply, "ts": ts}
        self.swarm.bus.emit("task.comment", actor, task_id=task.id, post=post)
        if notify:
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

    # --- crew tasks: texts for agents (docs/SPEC.md §4.2), in the recipient's language ---------------------------

    def draft_budget(self) -> int:
        """B, the budget of the draft of a crew task's result: min(CREW_DRAFT_CHARS, agents.tool_output_chars // 2),
        the setting read at each call."""
        chars = getattr(getattr(self.swarm.config, "agents", None), "tool_output_chars", 2 * CREW_DRAFT_CHARS)
        return min(CREW_DRAFT_CHARS, int(chars) // 2)

    def _crew_draft(self, task: Task, lang: str, budget: int | None = None) -> str:
        """The draft of #N's result ("Finishing"): a line break, the crew_draft header and one item per direction in
        creation order (a done one followed by its indented result), at most `budget` characters in all (default
        draft_budget()). Pass 1 takes the directions while the header, their lines, the least pieces of the done
        ones and, when not all fit, crew_draft_more fit; pass 2 shares what is left among the done results, the
        shortest first. "" when not even crew_draft_more fits."""
        from .shell import truncate_middle

        limit = max(0, self.draft_budget() if budget is None else budget)
        subs = self.subtasks_of(task.id)
        if not subs:
            return ""
        head = "\n" + prompts.t(lang, "crew_draft", id=task.id, items="")
        more_all = prompts.t(lang, "crew_draft_more", k=len(subs), id=task.id)
        lines = [self._draft_line(s, lang) for s in subs]
        results = {i: (s.result or "") for i, s in enumerate(subs) if s.status == Status.DONE}
        cuts = {i: _INDENT + prompts.t(lang, "crew_draft_cut", id=subs[i].id) for i in results}
        no_result = _INDENT + prompts.t(lang, "crew_draft_no_result")
        whole = {i: _indented(r) if r else no_result for i, r in results.items()}
        least = {i: whole[i] if not results[i] or len(whole[i]) <= len(cuts[i]) else cuts[i] for i in results}

        def size(lengths: list[int]) -> int:  # the elements joined by line breaks, after the header
            return len(head) + sum(lengths) + max(0, len(lengths) - 1)

        def item(i: int) -> list[int]:
            return [len(lines[i])] + ([len(least[i])] if i in results else [])

        # pass 1: which items
        taken: list[int] = []
        count = 0
        if size([n for i in range(len(subs)) for n in item(i)]) <= limit:
            count = len(subs)
        else:
            while count < len(subs) and size([*taken, *item(count), len(more_all)]) <= limit:
                taken += item(count)
                count += 1
        if count == 0:
            if size([len(more_all)]) <= limit:
                return head + more_all
            return "\n" + more_all if 1 + len(more_all) <= limit else ""
        more = prompts.t(lang, "crew_draft_more", k=len(subs) - count, id=task.id) if count < len(subs) else ""
        # pass 2: the results of the items, the shortest first
        shown = range(count)
        fixed = [len(lines[i]) for i in shown] + [0 for i in shown if i in results] + ([len(more)] if more else [])
        left = limit - size(fixed)
        served = sorted((i for i in shown if i in results and results[i]), key=lambda i: (len(results[i]), i))
        pieces = {i: no_result for i in shown if i in results and not results[i]}
        left -= sum(len(p) for p in pieces.values())
        reserve = sum(len(least[i]) for i in served)
        for pos, i in enumerate(served):
            reserve -= len(least[i])
            # each share leaves the least pieces of the ones still to serve (pass 1 kept room for all of them)
            share = min(NOTIFY_TEXT_LIMIT, left // (len(served) - pos), left - reserve)
            piece = self._draft_piece(whole[i], cuts[i], share, truncate_middle)
            if len(piece) > share:
                piece = least[i]
            pieces[i] = piece
            left -= len(piece)
        elements: list[str] = []
        for i in shown:
            elements.append(lines[i])
            if i in pieces:
                elements.append(pieces[i])
        if more:
            elements.append(more)
        return "\n" + prompts.t(lang, "crew_draft", id=task.id, items="\n".join(elements))

    @staticmethod
    def _draft_piece(whole: str, cut: str, share: int, truncate_middle: Callable[[str, int], str]) -> str:
        """A done direction's indented result within `share` characters: whole, or cut in the middle with its
        task_get pointer after it, or the pointer alone when the cut would keep fewer than CREW_DRAFT_MIN."""
        if len(whole) <= share:
            return whole
        marker = len(f"\n... [{len(whole)} characters skipped] ...\n")
        keep = share - (1 + len(cut)) - marker
        if keep < CREW_DRAFT_MIN:
            return cut
        return truncate_middle(whole, keep) + "\n" + cut

    @staticmethod
    def _draft_line(sub: Task, lang: str) -> str:
        line = f"- #{sub.id} [{sub.status}] {_quote(trim(sub.title, 80), lang)}"
        return line + (f" @{sub.assignee}" if sub.assignee else "")

    @staticmethod
    def _counts_text(counts: dict[str, int], lang: str) -> str:
        """The finished subtasks per status: "5 done, 1 cancelled" (n_subtasks_done's {counts})."""
        parts = []
        if counts.get(Status.DONE):
            parts.append(prompts.t(lang, "count_done", n=counts[Status.DONE]))
        if counts.get(Status.CANCELLED):
            parts.append(prompts.t(lang, "count_cancelled", n=counts[Status.CANCELLED]))
        if counts.get(Status.FAILED):
            parts.append(prompts.t(lang, "count_failed", n=counts[Status.FAILED]))
        return ", ".join(parts)

    def _ending(self, task: Task, state: str | None, form: str, lang: str, budget: int | None = None) -> str:
        """What a parent in finish state `state` asks of its holder ("Finishing"): the plain ending, or for a crew
        task the first crew ending that applies (a direction failed, all were cancelled, all are finished). `form`:
        "holder", "creator" (asked to take it first) or "owner" (a prefix that says what happened, then the
        holder's ending). The all finished ending of the holder and owner forms is followed by the draft of the
        result (`budget`: its characters, default draft_budget()). "" for state None."""
        if form not in ("holder", "creator", "owner"):
            raise ValueError(f"unknown ending form {form!r}")
        if state is None:
            return ""
        creator = form == "creator"
        if task.crew < 2:
            text = prompts.t(lang, "ending_plain_creator" if creator else "ending_plain", id=task.id)
            return prompts.t(lang, "owner_prefix_plain") + text if form == "owner" else text
        subs = self.subtasks_of(task.id)
        failed = next((s.id for s in subs if s.status == Status.FAILED), None)
        if state == "failed" and failed is not None:
            text = prompts.t(lang, "ending_crew_failed_creator" if creator else "ending_crew_failed", id=task.id,
                             failed=failed)
            prefix = prompts.t(lang, "owner_prefix_failed")
        elif not any(s.status == Status.DONE for s in subs):
            text = prompts.t(lang, "ending_crew_cancelled_creator" if creator else "ending_crew_cancelled",
                             id=task.id)
            prefix = prompts.t(lang, "owner_prefix_cancelled")
        else:
            text = prompts.t(lang, "ending_crew_finished_creator" if creator else "ending_crew_finished", id=task.id)
            prefix = prompts.t(lang, "owner_prefix_finished")
            if not creator:
                text += self._crew_draft(task, lang, budget)
        return prefix + text if form == "owner" else text

    def _owner_sentence(self, task: Task, holder: str | None, lang: str,
                        budget: int | None = None) -> tuple[str, str]:
        """(instructions, directions) told to the holder of crew task `task` ("The owner"; n_crew_owner's
        placeholders, and the results of the calls that make the actor the holder). instructions: the owner form of
        the crew ending when all its directions are finished (a finish state), else the owner sentence (its own
        direction, take one, all taken, or no directions yet). directions: the directions but the cancelled ones,
        id ascending, at most OWNER_LIST_MAX, then crew_directions_more; "" with none, and "" with the all finished
        ending, whose draft lists them. For a task that is no crew task: the owner form of the plain ending when
        all its subtasks are finished, else ("", "")."""
        state = self.finish_states([task.id])[task.id]
        if task.crew < 2:
            return (" " + self._ending(task, state, "owner", lang, budget) if state else ""), ""
        subs = self.subtasks_of(task.id)
        listed = [s for s in subs if s.status != Status.CANCELLED]
        if state is not None:
            instructions = " " + self._ending(task, state, "owner", lang, budget)
            if state == "finished" and any(s.status == Status.DONE for s in subs):
                return instructions, ""
            return instructions, self._directions_text(task, listed, lang)
        if not subs:
            return prompts.t(lang, "crew_owner_none", id=task.id), ""
        own = [s for s in listed if holder is not None and s.assignee == holder]
        if own:
            mine = next((s for s in own if s.status in Status.OPEN), own[0])
            instructions = prompts.t(lang, "crew_owner_own", id=task.id, sub=mine.id, status=mine.status,
                                     crew=task.crew)
        elif any(s.status == Status.TODO and s.assignee is None for s in subs):
            instructions = prompts.t(lang, "crew_owner_take", id=task.id, crew=task.crew)
        else:
            instructions = prompts.t(lang, "crew_owner_all_taken", id=task.id, crew=task.crew)
        return instructions, self._directions_text(task, listed, lang)

    def _directions_text(self, task: Task, listed: list[Task], lang: str) -> str:
        """crew_directions with '#15 [in_progress] "pkce" @Anna Smith; #17 [todo] "oidc"' (at most OWNER_LIST_MAX,
        then crew_directions_more), or "" with none."""
        if not listed:
            return ""
        items = "; ".join(self._draft_line(s, lang)[2:] for s in listed[:OWNER_LIST_MAX])
        text = prompts.t(lang, "crew_directions", items=items)
        if len(listed) > OWNER_LIST_MAX:
            text += prompts.t(lang, "crew_directions_more", k=len(listed) - OWNER_LIST_MAX, id=task.id)
        return text

    @staticmethod
    def _direction_names(subs: list[Task], lang: str) -> str:
        """'#17 "oidc", #18 "jose"' (at most OWNER_LIST_MAX, then "…")."""
        names = [f"#{s.id} {_quote(trim(s.title, 80), lang)}" for s in subs[:OWNER_LIST_MAX]]
        return ", ".join(names + (["…"] if len(subs) > OWNER_LIST_MAX else []))

    @staticmethod
    def _open_list(subs: list[Task], viewer: str | None, lang: str = "en") -> str:
        """The open ones of `subs` for the board's errors: '#15 [in_progress, you], #16 [in_progress, Anna Smith],
        #17 [review]' (the holder named unless the subtask is in review, `viewer` as "you"). The board's errors are
        English, so `lang` changes nothing yet."""
        parts = []
        for sub in subs:
            if sub.status not in Status.OPEN:
                continue
            who = ""
            if sub.assignee and sub.status != Status.REVIEW:
                who = ", you" if sub.assignee == viewer else f", {sub.assignee}"
            parts.append(f"#{sub.id} [{sub.status}{who}]")
        return ", ".join(parts)

    # --- notifications -------------------------------------------------------------------------------

    def _notify_member(self, name: str | None, actor: str, key: str, **kw: Any) -> None:
        """Task notifications go to active members only: never the actor, never USER (the UI shows tasks; only
        the reviews the user owns are announced to it, by _request_review)."""
        if not name or name == actor or name == USER or not self.swarm.is_member(name):
            return
        self.swarm.notify(name, key, **kw)

    def _request_review(self, by: str, task: Task, auto: bool, handed_over_from: str | None = None) -> None:
        """`task` waits for its reviewer: tell them. `auto`: the board chose the reviewer just now (USER chosen
        by the board means that nobody else qualifies). `handed_over_from`: the reviewer the board took the review
        from (it let the review lapse, or it left while the executor had left too): the user, when the board chose
        it, is told that no agent took the review over rather than who finished the task."""
        if task.reviewer == USER:
            if auto and handed_over_from:
                self.swarm.notify(USER, "u_review_handed_over", id=task.id, title=task.title,
                                  reviewer=handed_over_from)
            elif by != USER:
                self.swarm.notify(USER, "u_review_fallback" if auto else "u_review_requested",
                                  by=by, id=task.id, title=task.title)
            return
        self._notify_member(task.reviewer, by, "n_review_requested", id=task.id, title=task.title, by=by)

    def _review_dropped(self, actor: str, old: Task, task: Task) -> None:
        """The review `old.reviewer` was asked for is no longer theirs (settled or moved by someone else, or the
        task left review): news for their next wake-up, so they do not start a review that is not needed."""
        if old.status != Status.REVIEW or not old.reviewer or old.reviewer == actor:
            return
        if task.status == Status.REVIEW and task.reviewer == old.reviewer:
            return
        self._notify_member(old.reviewer, actor, "n_review_cancelled", wake=False, id=task.id, title=task.title,
                            by=actor)

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

    def _deps_changed(self, actor: str, old: Task, task: Task) -> None:
        """depends_on changed: an assignee that removed dependencies its creator planned leaves a record in the
        discussion and tells the creator, quietly (the assignee is told to post why there, and that post wakes the
        creator, who is a participant); a task that lost its last blocker wakes its assignee as a dependency done
        does, but with its own text (n_unblocked_removed): the removed ones are not done."""
        removed = [d for d in old.depends_on if d not in task.depends_on]
        if removed and actor not in (USER, old.created_by):
            statuses = self._statuses(removed)
            listed = ", ".join(f"#{d} [{statuses.get(d, '?')}]" for d in removed)
            record = prompts.t(self.swarm.language, "post_deps_removed", by=actor, deps=listed)
            self._post(actor, task, record, None, notify=False)
            self._notify_member(old.created_by, actor, "n_deps_removed", wake=False, id=task.id, title=task.title,
                                by=actor, deps=listed)
        if task.status in Status.OPEN and self._blockers_of(old.depends_on) and not task.blocked_by:
            # not n_unblocked: the removed blockers are not done, their results do not exist yet
            listed = ", ".join(f"#{d}" for d in self._blockers_of(removed))
            self._notify_member(task.assignee, actor, "n_unblocked_removed", id=task.id, title=task.title,
                                by=actor, deps=listed)

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
