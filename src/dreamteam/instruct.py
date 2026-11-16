"""What an instruction to an existing session does: the prompt of `dreamteam -p -r <id> "<text>"` (docs/TUI.md §2)
and the text of the HTTP API's `continue` (docs/SPEC.md §21.2). `continue_step` decides it from the session's state
(Swarm.snapshot(), the board's /api/state); `-p` then carries it out over HTTP, the API in process.

Stdlib and models only: hub code imports it, and it never imports dreamteam.tui."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .models import ALL


@dataclass(frozen=True)
class ContinueStep:
    """`action`: "set_goal" (the session has no goal: the text becomes it), "reopen_goal" (the goal is completed: it
    is reopened with the text as the comment) or "message" (the text goes to `to` as a message of the user).
    `to`: the message's one recipient, None for the other actions. `answers_question`: the id of the pending
    question's message when the message is its answer (to the asker), else None. `resume`: the session is paused by
    the user, so it is resumed after the delivery, whatever the action."""

    action: Literal["set_goal", "reopen_goal", "message"]
    to: str | None = None
    resume: bool = False
    answers_question: int | None = None


def coordinator(state: dict) -> str | None:
    """Who gets the instruction in an existing session: the planning task's assignee, else the first LLM agent
    (members that are not retired); None when there is neither."""
    agents = [a for a in state.get("agents") or [] if a.get("status") != "retired"]
    names = {a.get("name") for a in agents}
    for task in state.get("tasks") or []:
        if "planning" in (task.get("tags") or []) and task.get("assignee") in names:
            return task["assignee"]
    return next((a["name"] for a in agents if a.get("kind") == "llm"), None)


def _question(swarm: dict) -> dict | None:
    """The member's question the swarm waits for the user's answer to ({asker, message_id, ...}), else None."""
    question = swarm.get("question")
    if isinstance(question, dict) and question.get("asker") and isinstance(question.get("message_id"), int):
        return question
    return None


def continue_step(state: dict) -> ContinueStep:
    """The step for an instruction to the session whose state this is:
    - no goal: the text becomes the goal;
    - a completed goal: it is reopened with the text as the comment;
    - a pending question (a question pause): the text is the answer, a message to the asker;
    - otherwise a message to the coordinator (`coordinator`), else to "all".
    A session paused by the user is resumed as well. A budget pause is not (it ends a `-p` run, and only a budget
    that covers what was spent lifts it), nor a question pause (the answer ends it by itself, and a resume would drop
    the question)."""
    swarm = state.get("swarm") or {}
    resume = bool(swarm.get("paused")) and swarm.get("pause_reason") not in ("budget", "question")
    goal = swarm.get("goal")
    if not goal:
        return ContinueStep("set_goal", resume=resume)
    if goal.get("status") == "done":
        return ContinueStep("reopen_goal", resume=resume)
    question = _question(swarm)
    if question is not None:
        return ContinueStep("message", to=question["asker"], resume=resume, answers_question=question["message_id"])
    return ContinueStep("message", to=coordinator(state) or ALL, resume=resume)


__all__ = ["ContinueStep", "continue_step", "coordinator"]
