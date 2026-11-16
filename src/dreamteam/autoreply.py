"""Automatic answers in the user's place (docs/TUI.md §2, docs/SPEC.md §21.5): what `dreamteam -p` sends when a
member writes to the user (AUTO_REPLY) or a task waits for the user's review (AUTO_REVIEW), and which messages and
tasks call for one. The hub's own automatic answers for an API session with answers "auto" come here too.

Stdlib and models only: hub code imports it, and it never imports dreamteam.tui (whose package imports
prompt_toolkit)."""

from __future__ import annotations

from typing import Any

from .models import USER, Status

AUTO_REPLY_MAX = 50      # a safety net against an endless question / auto-answer ping-pong (not approvals)

AUTO_REPLY = {
    "ru": "Пользователь недоступен (неинтерактивный режим): прими решение сам и продолжай работу.",
    "en": "The user is not available (non-interactive mode): decide yourself and continue.",
}
AUTO_REVIEW = {
    "ru": "Принято автоматически: пользователь недоступен (неинтерактивный режим), задачу никто не проверял.",
    "en": "Approved automatically: the user is not available (non-interactive mode); nobody reviewed the task.",
}


def reply_text(lang: str | None) -> str:
    """AUTO_REPLY in `lang`, English for a language it has no text in."""
    return AUTO_REPLY.get(lang or "en", AUTO_REPLY["en"])


def review_text(lang: str | None) -> str:
    """AUTO_REVIEW in `lang`, English for a language it has no text in."""
    return AUTO_REVIEW.get(lang or "en", AUTO_REVIEW["en"])


def wants_reply(msg: dict) -> bool:
    """A journaled message (a `message.sent` event's `message`) that a member sent to the user: it has an id, and the
    user is among its recipients."""
    return (isinstance(msg.get("id"), int) and msg.get("sender") != USER
            and USER in (msg.get("recipients") or []))


def review_key(task: dict) -> tuple[int, Any] | None:
    """A task (a snapshot's or a `task.updated` event's dict) that waits for the user's review: `(id, updated_at)`,
    the key `-p` approves it once under (a task submitted again has a new `updated_at`, so it is approved again).
    None for any other task.

    `-p`'s client-side key only: every write of the row changes `updated_at`, so it cannot name a review round on
    the hub. SessionAutoReply and requests_of key a round on the `task.updated` event that put the task in review
    for the user (the request id `r<task>-<event>`, docs/SPEC.md §21.5)."""
    if task.get("status") != Status.REVIEW or task.get("reviewer") != USER or not isinstance(task.get("id"), int):
        return None
    return task["id"], task.get("updated_at")


__all__ = ["AUTO_REPLY", "AUTO_REVIEW", "AUTO_REPLY_MAX", "reply_text", "review_text", "wants_reply", "review_key"]
