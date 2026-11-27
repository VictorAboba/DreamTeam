"""The result of a run: the object `dreamteam -p --output-format json` prints at its end (docs/TUI.md §2) and the
HTTP API's `result` route answers (docs/SPEC.md §21.7). Stdlib only: hub code imports it, and it never imports
dreamteam.tui."""

from __future__ import annotations

from typing import Any

POSTS_MAX = 200   # the posts a result lists at most (docs/SPEC.md §22.9 "Result"), in the feed's order
POST_FIELDS = ("id", "kind", "title", "author", "likes", "dislikes", "comments", "created_at", "active_at")


def build_result(*, status: str, session: str | None, goal: dict | None, summary: str, tasks: list, agents: list,
                 jobs: list, cost_usd: float, duration_s: float | None, error: str | None = None,
                 mode: str = "board", posts: list | None = None, decision: dict | None = None) -> dict[str, Any]:
    """The result object, from a session's snapshot lists (Swarm.snapshot(), the board's /api/state) as they are.

    Its fields, in this order: `type` ("result"), `status`, `session`, `goal`, `summary`, `tasks`
    (`{id, title, status, assignee}` each), `cost_usd`, `duration_s`, `agents` (the names of the members not
    retired), `jobs` (the background jobs still running, `{id, label, owner, command, origin}` each), `mode` ("board"
    | "thread"), `posts` (a thread session's posts without their texts, `{id, kind, title, author, likes, dislikes,
    comments, created_at, active_at}` each, in the feed's order as given, at most POSTS_MAX; [] in a board session),
    `decision` (how a thread session's goal was decided, `goal.decision`; null in a board session) and `error` only
    when it is given (not None). The caller passes its own words for `status`, and as `summary` the goal's summary
    when the status is "done", else "" (docs/SPEC.md §21.7, §22.9)."""
    result: dict[str, Any] = {
        "type": "result", "status": status, "session": session, "goal": goal, "summary": summary,
        "tasks": [{"id": t.get("id"), "title": t.get("title"), "status": t.get("status"),
                   "assignee": t.get("assignee")} for t in tasks or []],
        "cost_usd": float(cost_usd or 0.0),
        "duration_s": duration_s,
        "agents": [a.get("name") for a in agents or [] if a.get("status") != "retired"],
        # the agents' background jobs still running (e.g. a dev server after the goal): they go on until the session
        # stops
        "jobs": [{"id": j.get("id"), "label": j.get("label"), "owner": j.get("owner"), "command": j.get("command"),
                  "origin": j.get("origin")}
                 for j in jobs or [] if isinstance(j, dict) and j.get("status") == "running"],
        "mode": "thread" if mode == "thread" else "board",
        "posts": [{key: p.get(key) for key in POST_FIELDS} for p in (posts or [])[:POSTS_MAX] if isinstance(p, dict)]
        if mode == "thread" else [],
        "decision": decision if mode == "thread" and isinstance(decision, dict) else None,
    }
    if error is not None:
        result["error"] = error
    return result


__all__ = ["POSTS_MAX", "POST_FIELDS", "build_result"]
