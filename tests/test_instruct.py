"""instruct.py: what an instruction to an existing session does, for `dreamteam -p -r` (docs/TUI.md §2) and the HTTP
API's continue (docs/SPEC.md §21.2)."""

from __future__ import annotations

import pytest

from dreamteam.instruct import ContinueStep, continue_step, coordinator

ANNA = {"name": "Anna Smith", "kind": "llm", "status": "active"}
BORIS = {"name": "Boris Lee", "kind": "llm", "status": "idle"}
GONE = {"name": "Old Hand", "kind": "llm", "status": "retired"}
CLAUDE = {"name": "Claude Code", "kind": "external", "status": "active"}
PLANNING = {"id": 1, "title": "Plan", "status": "in_progress", "assignee": "Boris Lee", "tags": ["planning"]}
QUESTION = {"asker": "Anna Smith", "message_id": 12, "since": 1.0, "text": "Which colour?"}
WORKING = {"title": "Write a haiku", "status": "active"}
DONE = {"title": "Write a haiku", "status": "done", "summary": "Done."}


def state(goal=None, *, paused=False, reason="", question=None, agents=(ANNA, BORIS), tasks=(PLANNING,)) -> dict:
    return {"swarm": {"goal": goal, "paused": paused, "pause_reason": reason, "question": question},
            "agents": list(agents), "tasks": list(tasks)}


@pytest.mark.parametrize("snapshot, step", [
    # no goal: the text becomes it
    (state(), ContinueStep("set_goal")),
    (state(paused=True, reason="user"), ContinueStep("set_goal", resume=True)),
    # a completed goal: reopened with the text as the comment
    (state(DONE), ContinueStep("reopen_goal")),
    (state(DONE, paused=True, reason="user"), ContinueStep("reopen_goal", resume=True)),
    # a goal in work: a message to the coordinator (the planning task's assignee)
    (state(WORKING), ContinueStep("message", to="Boris Lee")),
    # a question pause: the answer, a message to the asker; never resumed (the answer ends the pause)
    (state(WORKING, paused=True, reason="question", question=QUESTION),
     ContinueStep("message", to="Anna Smith", answers_question=12)),
    # ... but no goal or a completed one comes first, and a question pause without a question goes to the
    # coordinator (none of them resumed)
    (state(DONE, paused=True, reason="question", question=QUESTION), ContinueStep("reopen_goal")),
    (state(None, paused=True, reason="question", question=QUESTION), ContinueStep("set_goal")),
    (state(WORKING, paused=True, reason="question", question=None), ContinueStep("message", to="Boris Lee")),
    # paused by the user: resumed after the delivery
    (state(WORKING, paused=True, reason="user"), ContinueStep("message", to="Boris Lee", resume=True)),
    (state(WORKING, paused=True, reason=""), ContinueStep("message", to="Boris Lee", resume=True)),
    # a budget pause is not resumed
    (state(WORKING, paused=True, reason="budget"), ContinueStep("message", to="Boris Lee")),
    # a pause reason left over without the pause
    (state(WORKING, paused=False, reason="user"), ContinueStep("message", to="Boris Lee")),
    # a question that is no question (no asker, no message id) goes to the coordinator
    (state(WORKING, question={"asker": "", "message_id": 12}), ContinueStep("message", to="Boris Lee")),
    (state(WORKING, question={"asker": "Anna Smith", "message_id": "12"}), ContinueStep("message", to="Boris Lee")),
    (state(WORKING, question="Anna Smith"), ContinueStep("message", to="Boris Lee")),
    # no LLM member: to everyone
    (state(WORKING, agents=(CLAUDE,), tasks=()), ContinueStep("message", to="all")),
    (state(WORKING, agents=(), tasks=()), ContinueStep("message", to="all")),
])
def test_continue_step(snapshot, step):
    assert continue_step(snapshot) == step


def test_continue_step_of_an_empty_state():
    assert continue_step({}) == ContinueStep("set_goal")
    assert continue_step({"swarm": None}) == ContinueStep("set_goal")


def test_coordinator():
    # the planning task's assignee first, else the first LLM member
    assert coordinator(state(WORKING)) == "Boris Lee"
    assert coordinator(state(WORKING, tasks=())) == "Anna Smith"
    assert coordinator(state(WORKING, tasks=({**PLANNING, "tags": ["backend"]},))) == "Anna Smith"
    assert coordinator(state(WORKING, tasks=({**PLANNING, "assignee": None},))) == "Anna Smith"
    # retired members are passed over, as the planning task's assignee too
    assert coordinator(state(WORKING, agents=(GONE, BORIS), tasks=())) == "Boris Lee"
    assert coordinator(state(WORKING, agents=(GONE, ANNA), tasks=({**PLANNING, "assignee": "Old Hand"},))) == \
        "Anna Smith"
    # an external member coordinates only as the planning task's assignee
    assert coordinator(state(WORKING, agents=(CLAUDE, BORIS), tasks=())) == "Boris Lee"
    assert coordinator(state(WORKING, agents=(CLAUDE, BORIS), tasks=({**PLANNING, "assignee": "Claude Code"},))) == \
        "Claude Code"
    assert coordinator(state(WORKING, agents=(CLAUDE,), tasks=())) is None
    assert coordinator({}) is None
