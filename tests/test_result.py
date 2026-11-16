"""result.py: the result object of a run, shared by `dreamteam -p` (docs/TUI.md §2) and the HTTP API (docs/SPEC.md
§21.7)."""

from __future__ import annotations

import io
import json

import pytest

from dreamteam.result import build_result
from dreamteam.tui import printmode

# the fields of `dreamteam -p --output-format json`, in its order ("error" last, only when given)
FIELDS = ["type", "status", "session", "goal", "summary", "tasks", "cost_usd", "duration_s", "agents", "jobs"]

GOAL = {"title": "Write a haiku", "description": "", "status": "done", "summary": "The haiku is in haiku.txt."}
TASKS = [
    {"id": 1, "title": "Plan", "status": "done", "assignee": "Anna Smith", "tags": ["planning"], "result": "ok",
     "reviewer": None, "updated_at": 5.0},
    {"id": 2, "title": "Write", "status": "in_progress", "assignee": None, "tags": []},
]
AGENTS = [
    {"name": "Anna Smith", "kind": "llm", "status": "active", "cost_usd": 0.5},
    {"name": "Old Hand", "kind": "llm", "status": "retired"},
    {"name": "Claude Code", "kind": "external", "status": "idle"},
]
JOBS = [
    {"id": 1, "label": "j1", "owner": "Anna Smith", "command": "python -m http.server", "origin": "background",
     "status": "running", "pid": 42, "started_at": 1.0},
    {"id": 2, "label": "j2", "owner": "Anna Smith", "command": "pytest", "origin": "tool", "status": "done"},
    {"id": 3, "label": "j3", "owner": "Anna Smith", "command": "sleep 9", "origin": "background", "status": "killed"},
    "not a job",
]


def snapshot_result(status: str, **kw):
    args = dict(status=status, session="001-write-a-haiku", goal=GOAL,
                summary=GOAL["summary"] if status == "done" else "", tasks=TASKS, agents=AGENTS, jobs=JOBS,
                cost_usd=1.25, duration_s=12.5)
    return build_result(**{**args, **kw})


def test_a_done_result_has_the_fields_of_print_mode_in_its_order():
    result = snapshot_result("done")
    assert list(result) == FIELDS
    assert result == {
        "type": "result", "status": "done", "session": "001-write-a-haiku", "goal": GOAL,
        "summary": "The haiku is in haiku.txt.",
        "tasks": [{"id": 1, "title": "Plan", "status": "done", "assignee": "Anna Smith"},
                  {"id": 2, "title": "Write", "status": "in_progress", "assignee": None}],
        "cost_usd": 1.25, "duration_s": 12.5,
        "agents": ["Anna Smith", "Claude Code"],   # the retired member is left out
        "jobs": [{"id": 1, "label": "j1", "owner": "Anna Smith", "command": "python -m http.server",
                  "origin": "background"}],          # finished jobs (and anything that is no job) are left out
    }
    assert list(result["tasks"][0]) == ["id", "title", "status", "assignee"]
    assert list(result["jobs"][0]) == ["id", "label", "owner", "command", "origin"]


@pytest.mark.parametrize("status, error", [("budget", "The swarm was paused: the budget is exhausted."),
                                           ("error", "Error: the hub said no"), ("timeout", "Timed out")])
def test_an_unfinished_result_ends_with_its_error(status, error):
    result = snapshot_result(status, error=error)
    assert list(result) == FIELDS + ["error"]
    assert result["status"] == status and result["error"] == error and result["summary"] == ""


def test_error_only_when_given():
    assert "error" not in snapshot_result("error")
    assert "error" not in snapshot_result("done", error=None)
    assert snapshot_result("error", error="")["error"] == ""   # given, if empty


def test_an_empty_snapshot():
    result = build_result(status="error", session=None, goal=None, summary="", tasks=[], agents=[], jobs=[],
                          cost_usd=0, duration_s=None)
    assert result == {"type": "result", "status": "error", "session": None, "goal": None, "summary": "",
                      "tasks": [], "cost_usd": 0.0, "duration_s": None, "agents": [], "jobs": []}
    assert isinstance(result["cost_usd"], float)
    assert isinstance(snapshot_result("done", cost_usd=2)["cost_usd"], float)


class _Client:
    mode = "launcher"

    def __init__(self, state: dict):
        self._state = state

    async def state(self, sid):
        return self._state


class _Conn:
    def __init__(self, state: dict):
        self.client = _Client(state)


@pytest.mark.parametrize("status, reason", [("done", ""), ("budget", "Paused: the budget."), ("error", "Error: x"),
                                            ("error", "")])
async def test_print_mode_prints_the_built_result(status, reason):
    """`dreamteam -p --output-format json` prints build_result's object, read from the session's final state."""
    state = {"swarm": {"goal": GOAL, "spent_usd": 1.25, "language": "en"}, "tasks": TASKS, "agents": AGENTS,
             "jobs": JOBS}
    out = io.StringIO()
    run = printmode._PrintRun(_Conn(state), "Goal", session=None, continue_last=False, title=None, overrides=None,
                              output_format="json", verbose=False, timeout=None, auto_reply=True, lang="en", out=out,
                              err=io.StringIO())
    run.sid = "001-write-a-haiku"
    code = await run.report(status, reason)
    assert code == printmode.EXIT_CODES[status]
    printed = json.loads(out.getvalue())
    assert isinstance(printed["duration_s"], float) and printed["duration_s"] >= 0
    want = snapshot_result(status, duration_s=printed["duration_s"],
                           error=reason if status != "done" and reason else None)
    assert printed == want and list(printed) == list(want)
