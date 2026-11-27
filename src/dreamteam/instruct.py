"""What an instruction to an existing session does: the prompt of `dreamteam -p -r <id> "<text>"` (docs/TUI.md §2)
and the text of the HTTP API's `continue` (docs/SPEC.md §21.2). `continue_step` decides it from the session's state
(Swarm.snapshot(), the board's /api/state); `-p` then carries it out over HTTP, the API in process.
`without_api_sessions` is which sessions `-c` (and a command's other picks the user did not name) choose from.
`post_parts` is how a thread session's instruction becomes a post (docs/SPEC.md §22.9 "Instructions from outside").

Stdlib, models and goal_text only: hub code imports it, and it never imports dreamteam.tui."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Literal

from .goal_text import GOAL_TITLE_MAX, _block, shorten_title
from .models import ALL, API_VIA


@dataclass(frozen=True)
class ContinueStep:
    """`action`: "set_goal" (the session has no goal: the text becomes it), "reopen_goal" (the goal is completed: it
    is reopened with the text as the comment), "message" (the text goes to `to` as a message of the user) or "post"
    (a thread session: the text becomes a post of the user in the feed, `post_parts`).
    `to`: the message's one recipient, None for the other actions. `answers_question`: the id of the pending
    question's message when the message is its answer (to the asker), else None. `resume`: the session is paused by
    the user, so it is resumed after the delivery, whatever the action."""

    action: Literal["set_goal", "reopen_goal", "message", "post"]
    to: str | None = None
    resume: bool = False
    answers_question: int | None = None


def coordinator(state: dict) -> str | None:
    """Who gets the instruction in an existing board session: the planning task's assignee, else the first LLM agent
    (members that are not retired); None when there is neither. Never used in a thread session, whose instruction is a
    post for everyone (continue_step)."""
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
    - otherwise, in a board session, a message to the coordinator (`coordinator`), else to "all"; in a thread session
      (the state's `swarm.mode`, missing = board) a post of the user (`post_parts`), which wakes every member.
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
    if swarm.get("mode") == "thread":
        return ContinueStep("post", resume=resume)
    return ContinueStep("message", to=coordinator(state) or ALL, resume=resume)


def post_parts(text: str) -> tuple[str, str]:
    """(title, text) of the post an instruction becomes in a thread session (docs/SPEC.md §22.9): the title is its
    first line, shortened as a goal's title is when it is longer than GOAL_TITLE_MAX characters
    (goal_text.shorten_title), the text the rest. The text is the whole instruction instead when it has one line, when
    the rest is blank, or when the first line is longer than GOAL_TITLE_MAX characters, so nothing of it is lost.
    `-p` and the HTTP API v1 both call it."""
    block = _block(text)
    first, _, rest = block.partition("\n")
    head = " ".join(first.split())
    rest = _block(rest)
    if not rest or len(head) > GOAL_TITLE_MAX:
        return shorten_title(head), block
    return head, rest


def without_api_sessions(sessions: Iterable[Any]) -> list[dict]:
    """The listed sessions (summaries, the hub's GET /api/sessions) that a pick the user did not name may choose:
    `dreamteam -c` and `-p -c` (the most recent one), `console` and `traces` without `--session`. Never one created
    through the HTTP API v1 (summary `via` "api", docs/SPEC.md §21.4): a busy API client would otherwise make its own
    sessions the most recent ones, and the owner's next `-c` would go on with the client's job. `-r <id>` and
    `--session` still open any session."""
    return [s for s in sessions if isinstance(s, dict) and s.get("via") != API_VIA]


__all__ = ["ContinueStep", "continue_step", "coordinator", "post_parts", "without_api_sessions"]
