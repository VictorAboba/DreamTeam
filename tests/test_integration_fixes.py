"""Regression tests for the problems found by the contract audit and the area owners, fixed during
integration: /api/inbox paging, lone surrogates in LLM output, compaction at a real context window, the
per-task in-progress nudge and the stalled-agent wake, member names in triggers, the shared name resolver
of the test double, a retiring reviewer, read_messages marking read, goal complete/reopen guards, notes
after compaction and in wake-ups, big tool outputs, the launcher's unknown-tool answer, the Docker files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeSwarm
from dreamteam import agent as agent_module
from dreamteam import prompts
from dreamteam import swarm as swarm_module
from dreamteam.agent import AgentRuntime
from dreamteam.console import Console
from dreamteam.llm.base import LLMError, estimate_tokens
from dreamteam.models import USER, AgentStatus, Status, ToolError
from dreamteam.tools import REGISTRY, ToolContext, call_tool
from dreamteam.web import create_app
from test_runtime import (call, check_history, history, is_compaction_request, kinds_for, last_text, make_swarm,
                          replies_to_user, run_until, shutdown)

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)
    monkeypatch.setattr(agent_module, "RETRY_DELAYS", (0.01, 0.01))
    monkeypatch.setattr(agent_module, "CRASH_PAUSE_SECONDS", 0.01)
    monkeypatch.setattr(agent_module, "RECOVERY_DELAYS", (0.05, 0.1))


async def asgi_get(app: Any, path: str, query: str = "") -> tuple[int, Any]:
    """One GET through the ASGI app (guard included), in this event loop."""
    scope = {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(), "root_path": "",
             "query_string": query.encode(), "headers": [(b"host", b"127.0.0.1:8765")], "scheme": "http",
             "server": ("127.0.0.1", 8765), "client": ("127.0.0.1", 50000), "http_version": "1.1"}
    status: list[int] = []
    body: list[bytes] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status.append(message["status"])
        elif message["type"] == "http.response.body":
            body.append(message.get("body", b""))

    await app(scope, receive, send)
    raw = b"".join(body).decode("utf-8")
    return status[0], json.loads(raw) if raw else None


async def team(tmp_path: Path, *names: str, **overrides: Any):
    """A started swarm without running agents, with LLM members of these exact names."""
    swarm, rec = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 0, **overrides.pop("swarm", {})},
                            **overrides)
    await swarm.start(run_agents=False)
    for name in names:
        await swarm.hire(USER, "Developer", name=name)
    return swarm, rec


# ------------------------------------------------------------------------------------------ /api/inbox
async def test_inbox_returns_the_newest_page_and_accepts_since(tmp_path):
    swarm, _ = await team(tmp_path, "Anna Smith")
    try:
        ids = [swarm.mail.send("Anna Smith", USER, f"msg {i}")["id"] for i in range(1, 601)]
        for i in range(520):
            swarm.notify(USER, "u_member_joined", name=f"N{i}", role="r", by="x", wake=False)
        app = create_app(swarm)
        status, data = await asgi_get(app, "/api/inbox")
        assert status == 200
        got = [m["id"] for m in data["messages"]]
        assert len(got) == 500 and got[-1] == ids[-1] and got[0] == ids[100] and got == sorted(got)
        notes = [n["id"] for n in data["notifications"]]
        newest_note = swarm.db.scalar("SELECT MAX(id) FROM notifications WHERE recipient = ?", (USER,))
        assert len(notes) == 500 and notes[-1] == newest_note and notes == sorted(notes)
        for query in (f"since_message={ids[589]}", f"since={ids[589]}"):
            status, data = await asgi_get(app, "/api/inbox", query)
            assert [m["text"] for m in data["messages"]] == [f"msg {i}" for i in range(591, 601)], query
        # the console primes itself with the latest messages and polls only what is new
        _, primed = await asgi_get(app, "/api/inbox")
        con = Console("http://127.0.0.1:1")
        con.hub.get = lambda path, **params: primed
        con._prime()
        assert con.since_message == ids[-1] and con.since_note == newest_note
    finally:
        await shutdown(swarm)


# ---------------------------------------------------------------------------------- lone surrogates
async def test_lone_surrogate_in_llm_output_keeps_history_valid_and_the_agent_alive(tmp_path):
    def policy(swarm, name, system, messages, tools):
        text = last_text(messages)
        if "first question" in text and not any(m["role"] == "tool" for m in messages):
            return {"text": "Writing \ud800 now", "tool_calls": [
                {"name": "write_file", "arguments": {"path": "a.txt", "content": "bad \ud800 char"}},
                {"name": "send_message", "arguments": {"to": ["user"], "text": "hi \udfff"}}]}
        if "second question" in text:
            return call("wait")
        return "Done \ud800 here"

    swarm, rec = make_swarm(tmp_path, policy)
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.mail.send(USER, agent, "first question")
        await run_until(lambda: rec.count(agent) >= 2 and swarm.member(agent)["status"] == AgentStatus.IDLE)
        runtime = swarm.runtimes[agent]
        stored = history(swarm, agent)
        assert stored == runtime.history  # memory and the database stay identical
        assert stored[1]["tool_calls"][0]["arguments"]["content"] == "bad \ud800 char"  # replayed verbatim
        check_history(stored + [{"role": "user", "content": "next"}])  # every tool call has its result
        results = [m["content"] for m in stored if m["role"] == "tool"]
        assert all("invalid Unicode" in r for r in results), results
        assert replies_to_user(swarm, agent) == ["Done � here"]
        # the board UI still renders the events that carry the surrogate (agent.activity)
        status, data = await asgi_get(create_app(swarm), "/api/events", "limit=2000")
        assert status == 200 and any("Writing" in str(e["data"].get("text", "")) for e in data["events"])
        swarm.mail.send(USER, agent, "second question")
        await run_until(lambda: rec.count(agent) >= 3 and swarm.member(agent)["status"] == AgentStatus.IDLE)
        assert "u_agent_error" not in kinds_for(swarm, USER)
        assert not rec.violations, rec.violations
    finally:
        await shutdown(swarm)


def test_dumps_round_trips_lone_surrogates_as_storable_text():
    from dreamteam.db import dumps, loads, utf8_safe

    value = {"a": "x\ud800y", "b": ["\udfff"], "c": "ok ✓"}
    text = dumps(value)
    text.encode("utf-8")  # storable by sqlite3
    assert loads(text) == value
    assert dumps({"c": "ok ✓"}) == '{"c":"ok ✓"}'  # unchanged when there is no surrogate
    assert utf8_safe("a\ud800b") == "a�b"


# ------------------------------------------------------------------------------ compaction at the window
def window_policy(state: dict, *, count_output: bool):
    """A provider whose context window is state["W"]: anything larger is a 400 with a wording that
    is_context_overflow() does not know (vLLM also counts max_tokens against the window)."""

    def policy(swarm, name, system, messages, tools):
        prompt = estimate_tokens([system, messages, tools])
        if prompt + (state["max_out"] if count_output else 0) > state["W"]:
            state["rejected"] += 1
            raise LLMError("Provider rejected the request: 400 Bad Request", retryable=False, kind="bad_request")
        if is_compaction_request(messages):
            state["summaries"] += 1
            return "SUMMARY of the reads"
        if state["reads"] < 16:
            state["reads"] += 1
            return call("read_file", path="big.txt")
        return call("wait")

    return policy


@pytest.mark.parametrize("room", [6000, 4000])
@pytest.mark.parametrize("count_output", [False, True])
async def test_agent_compacts_when_context_tokens_is_the_real_window(tmp_path, count_output, room):
    state = {"W": 10**9, "max_out": 2000, "reads": 0, "rejected": 0, "summaries": 0}
    models = {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0,
                          "cache_read_price": 0, "cache_write_price": 0, "max_tokens": state["max_out"]}}
    swarm, rec = make_swarm(tmp_path, window_policy(state, count_output=count_output), models=models,
                            agents={"max_steps_per_turn": 60})
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        (swarm.workspace.root / "big.txt").write_text("x" * 2000, encoding="utf-8")
        runtime = swarm.runtimes[agent]
        # room for a few reads beyond the fixed prompt; with 4000 a fresh compaction fills more than 3/4 of the
        # limit, so the anti-thrash rule of _needs_compaction applies: it must not let the prompt outgrow the window
        state["W"] = runtime._fixed_tokens() + room
        swarm.config.model(None).context_tokens = state["W"]  # the profile's context_tokens = the window
        swarm.notify(agent, "n_restart")
        await run_until(lambda: state["reads"] >= 16 and swarm.member(agent)["status"] == AgentStatus.IDLE, 30)
        assert swarm.db.scalar("SELECT COUNT(*) FROM events WHERE type = 'agent.compacted'") >= 1
        assert swarm.member(agent)["status"] == AgentStatus.IDLE
        assert "u_agent_error" not in kinds_for(swarm, USER)
        if not count_output:
            # the threshold leaves room for the summary call itself: the LLM wrote the summary
            assert state["summaries"] >= 1 and state["rejected"] == 0, state
        assert not rec.violations, rec.violations
    finally:
        await shutdown(swarm)


def test_overflow_wordings_of_local_servers_are_recognised():
    overflow = agent_module.is_context_overflow
    assert overflow(LLMError("the request exceeds the available context size, try increasing it", kind="bad_request"))
    assert overflow(LLMError("Input validation error: `inputs` tokens + `max_new_tokens` must be <= 4096",
                             kind="bad_request"))


async def test_notes_survive_compaction_and_are_not_repeated_in_every_wake_up(tmp_path):
    swarm, _ = await team(tmp_path, "Anna Smith")
    try:
        swarm.db.execute("UPDATE agents SET notes = ? WHERE name = ?", ("PLAN: A then B", "Anna Smith"))
        first = swarm.render_wake("Anna Smith")
        assert "PLAN: A then B" in first
        again = swarm.render_wake("Anna Smith")
        assert "PLAN: A then B" not in again and prompts.t("en", "notes_unchanged") in again
        swarm.db.execute("UPDATE agents SET notes = ? WHERE name = ?", ("PLAN: B only", "Anna Smith"))
        assert "PLAN: B only" in swarm.render_wake("Anna Smith")
        runtime = AgentRuntime(swarm, "Anna Smith")
        runtime._load_history()
        runtime._append({"role": "user", "content": "wake"})
        swarm.db.execute("UPDATE agents SET notes = ? WHERE name = ?", ("PLAN: C", "Anna Smith"))
        runtime._replace_with_summary("SUMMARY")
        compacted = runtime.history[0]["content"]
        assert "SUMMARY" in compacted and "PLAN: C" in compacted  # visible right after the compaction
        assert prompts.t("en", "notes_unchanged") in swarm.render_wake("Anna Smith")
    finally:
        await shutdown(swarm)


# ---------------------------------------------------------------------------- nudges / stalled agents
async def test_each_new_in_progress_task_gets_a_nudge_and_a_stalled_agent_is_woken(tmp_path):
    ids: dict[str, int] = {}
    step = {"n": 0}

    def policy(swarm, name, system, messages, tools):
        step["n"] += 1
        n = step["n"]
        script = {1: call("task_claim", task_id=ids["a"]), 2: "I will do it now.",
                  3: call("task_complete", task_id=ids["a"], result="A done"),
                  4: call("task_claim", task_id=ids["b"]), 5: "On it.", 6: "Still thinking.",
                  7: call("task_complete", task_id=ids["b"], result="B done")}
        return script.get(n, call("wait"))

    # about nudges: with independent review a single agent would send both tasks to the user
    swarm, rec = make_swarm(tmp_path, policy, swarm={"independent_review": False})
    ids["a"] = swarm.board.create(USER, "Task A", goal="a").id
    ids["b"] = swarm.board.create(USER, "Task B", goal="b").id
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.mail.send(USER, agent, "please do A and B")
        await run_until(lambda: swarm.board.get(ids["b"]).status == Status.DONE, 20)
        calls = rec.calls[agent]
        nudge_marker = "stopped without calling a tool"
        assert nudge_marker in last_text(calls[2]) and f"#{ids['a']}" in last_text(calls[2])
        # the second task gets its own nudge in the same activation (it used to end the turn silently)
        assert nudge_marker in last_text(calls[5]) and f"#{ids['b']}" in last_text(calls[5])
        # after plain text even then, the scheduler wakes the idle holder once (n_stalled)
        assert kinds_for(swarm, agent).count("n_stalled") == 1
        assert f"#{ids['b']}" in last_text(calls[6])
        assert not rec.violations, rec.violations
    finally:
        await shutdown(swarm)


# ------------------------------------------------------------------------------------- trigger names
async def test_trigger_names_resolve_like_tools_and_unknown_members_are_rejected(tmp_path):
    swarm, _ = await team(tmp_path, "Anna Smith", "Boris Ivanov")
    try:
        trig = swarm.triggers.create("Boris Ivanov", 'message(sender="Anna")', once=False)
        assert "Anna Smith" in trig["condition"]
        exact = swarm.triggers.create("Boris Ivanov", 'message(sender="Anna Smith")', once=False)
        assert exact["condition"] == 'message(sender="Anna Smith")'  # nothing to rewrite: kept as written
        swarm.mail.send("Anna Smith", "Boris Ivanov", "ready")
        swarm.triggers.evaluate_all()
        assert swarm.triggers.get(trig["id"])["fire_count"] == 1
        assert swarm.triggers.get(trig["id"])["last_error"] in (None, "")
        assert "Anna Smith" in swarm.triggers.create(USER, "count(assignee='@anna') > 0")["condition"]
        assert "Boris Ivanov" in swarm.triggers.create(USER, "comment(1, 'boris')")["condition"]
        assert "Anna Smith" in swarm.triggers.create(USER, "agent_status('ANNA') == 'idle'")["condition"]
        with pytest.raises(ToolError, match="Unknown member 'Nobody'"):
            swarm.triggers.create("Boris Ivanov", 'message(sender="Nobody")')
        with pytest.raises(ToolError, match="user cannot be used"):
            swarm.triggers.create(USER, "agent_status('user') == 'idle'")
        assert swarm.triggers.create(USER, "message(sender='пользователь')")["condition"] == "message(sender='user')"
    finally:
        await shutdown(swarm)


# ------------------------------------------------------------------------------- the test double
def test_fake_swarm_resolves_names_and_goals_like_the_real_swarm(tmp_path):
    fake = FakeSwarm(tmp_path / "p")
    try:
        fake.add_member("Anna Smith")
        assert fake.resolve_member("@anna") == "Anna Smith"
        assert fake.resolve_member("  ANNA   smith ") == "Anna Smith"
        assert fake.resolve_member("human") == USER and fake.resolve_member("the user") == USER
        with pytest.raises(ToolError, match="Empty member name"):
            fake.resolve_member(" @ ")
        with pytest.raises(ToolError, match="user cannot be used"):
            fake.resolve_member("user", allow_user=False)
        with pytest.raises(ToolError, match="no global goal"):
            fake.complete_goal("Anna Smith", "done")
        with pytest.raises(ToolError, match="title is empty"):
            fake.set_goal(USER, "  ")
        fake.set_goal(USER, "Ship it")
        fake.complete_goal("Anna Smith", "shipped")
        assert ("u_goal_done", {"by": "Anna Smith", "summary": "shipped"}) in fake.notifications_for(USER)
        with pytest.raises(ToolError, match="already marked as achieved"):
            fake.complete_goal("Anna Smith", "again")
    finally:
        fake.db.close()


# ------------------------------------------------------------------------------- retiring a reviewer
async def test_retiring_the_reviewer_without_independent_review_returns_tasks_to_their_assignee(tmp_path):
    # the independent counterpart (the review goes to the next pick) is in tests/test_review.py
    swarm, _ = await team(tmp_path, "Anna Smith", "Boris Ivanov", swarm={"independent_review": False})
    try:
        board = swarm.board
        t = board.create(USER, "Build", goal="built", assignee="Anna Smith", reviewer="Boris Ivanov")
        later = board.create(USER, "Later", goal="later", reviewer="Boris Ivanov")
        board.claim("Anna Smith", t.id)
        assert board.complete("Anna Smith", t.id, "built it").status == Status.REVIEW
        released = await swarm.retire(USER, "Boris Ivanov")
        assert released == []
        task = board.get(t.id)
        assert task.status == Status.IN_PROGRESS and task.reviewer is None and task.assignee == "Anna Smith"
        assert board.get(later.id).reviewer is None and board.get(later.id).status == Status.TODO
        assert "n_reviewer_left" in kinds_for(swarm, "Anna Smith")
        assert board.complete("Anna Smith", t.id, "built it").status == Status.DONE
        changes = [e["data"]["changes"] for e in swarm.bus.recent(types=("task.updated",), limit=100)
                   if e["data"]["task"]["id"] == t.id and "reviewer" in e["data"]["changes"]]
        assert changes == [{"reviewer": ["Boris Ivanov", None], "status": [Status.REVIEW, Status.IN_PROGRESS]}]
    finally:
        await shutdown(swarm)


async def test_a_task_done_is_quiet_news_for_its_creator_unless_the_creator_must_act(tmp_path):
    # pins the quiet n_task_closed of a task done: with independent review Part 1 would wake Anna as its reviewer
    swarm, _ = await team(tmp_path, "Anna Smith", "Boris Ivanov", swarm={"independent_review": False})
    try:
        board = swarm.board
        swarm.render_wake("Anna Smith")  # deliver the hiring notifications
        done = board.create("Anna Smith", "Part 1", goal="p1", assignee="Boris Ivanov")
        board.claim("Boris Ivanov", done.id)
        board.complete("Boris Ivanov", done.id, "part 1 ready")
        assert "n_task_closed" in kinds_for(swarm, "Anna Smith")
        assert not swarm.has_pending("Anna Smith")  # no full-history LLM call just for this news
        assert "part 1 ready" in swarm.render_wake("Anna Smith")  # delivered at the next wake-up
        failed = board.create("Anna Smith", "Part 2", goal="p2", assignee="Boris Ivanov")
        board.claim("Boris Ivanov", failed.id)
        board.update("Boris Ivanov", failed.id, status=Status.FAILED)
        assert swarm.has_pending("Anna Smith")  # a failure needs the creator: it wakes
        swarm.render_wake("Anna Smith")
        mine = board.create("Anna Smith", "Integrate", goal="all parts", assignee="Anna Smith")
        board.claim("Anna Smith", mine.id)
        board.update("Anna Smith", mine.id, status=Status.WAITING)
        part3 = board.create("Anna Smith", "Part 3", goal="p3", assignee="Boris Ivanov")
        board.claim("Boris Ivanov", part3.id)
        board.complete("Boris Ivanov", part3.id, "part 3 ready")
        assert swarm.has_pending("Anna Smith")  # the creator waits on something: it wakes
    finally:
        await shutdown(swarm)


# ------------------------------------------------------------------------------------- read_messages
async def test_read_messages_with_a_member_marks_the_shown_messages_read(tmp_path):
    swarm, _ = await team(tmp_path, "Anna Smith", "Boris Ivanov")
    try:
        swarm.render_wake("Boris Ivanov")  # deliver the hiring notifications: only the messages are pending
        swarm.mail.send("Anna Smith", "Boris Ivanov", "first")
        swarm.mail.send("Anna Smith", "Boris Ivanov", "second")
        assert swarm.has_pending("Boris Ivanov")
        text, is_error = await call_tool(ToolContext(swarm, "Boris Ivanov"), "read_messages", {"with": "Anna"})
        assert not is_error and "first" in text and "second" in text
        assert swarm.mail.unread_count("Boris Ivanov") == 0 and not swarm.has_pending("Boris Ivanov")
    finally:
        await shutdown(swarm)


# ---------------------------------------------------------------------------------- goal guards
async def test_goal_cannot_be_completed_twice_and_reopen_wakes_one_planner(tmp_path):
    swarm, _ = await team(tmp_path, "Anna Smith", "Boris Ivanov")
    try:
        swarm.set_goal(USER, "Ship it")
        with pytest.raises(ToolError, match="not completed"):
            swarm.reopen_goal(USER, "more")
        swarm.complete_goal("Boris Ivanov", "shipped")
        with pytest.raises(ToolError, match="already marked as achieved"):
            swarm.complete_goal("Anna Smith", "shipped again")
        assert kinds_for(swarm, USER).count("u_goal_done") == 1
        swarm.reopen_goal(USER, "add docs")
        assert swarm.goal()["status"] == "active"
        assert "n_goal_reopened" in kinds_for(swarm, "Boris Ivanov")  # the one who declared it achieved
        assert "n_goal_reopened" not in kinds_for(swarm, "Anna Smith")
    finally:
        await shutdown(swarm)


# ---------------------------------------------------------------------------------- big outputs
async def test_shell_output_is_capped_for_mcp_callers_but_traced_in_full_for_agents(tmp_path):
    swarm, _ = await team(tmp_path, agents={"tool_output_chars": 1000})
    try:
        ext = await swarm.hire(USER, "Helper", kind="external")
        command = "python -c \"print('x' * 5000)\""
        text, is_error = await call_tool(ToolContext(swarm, ext), "shell", {"command": command})
        assert not is_error and 900 < len(text) < 1300, len(text)
        assert ToolContext(swarm, ext).output_cap == 1000
        assert ToolContext(swarm, ext, runtime=object()).output_cap == swarm.config.tracing.max_payload_chars
    finally:
        await shutdown(swarm)


# ---------------------------------------------------------------------------------- launcher MCP
async def test_launcher_names_unknown_tools_and_lets_call_tool_explain_the_rest(tmp_path):
    from dreamteam.launcher_mcp import dispatch_launcher_call
    from test_launcher import _manager

    manager = _manager(tmp_path)
    try:
        text, is_error = await dispatch_launcher_call(manager, "boardd", {})
        assert is_error and "Unknown tool 'boardd'" in text and "Did you mean: board" in text
        text, is_error = await dispatch_launcher_call(manager, "session_create", {"title": "T", "goal": "G"})
        assert not is_error, text
        text, is_error = await dispatch_launcher_call(manager, "wait", {})
        assert is_error and "not available" in text, text  # the registry's audience error, not "unknown"
    finally:
        await manager.shutdown()


# ---------------------------------------------------------------------------------- texts and docker
def test_tool_descriptions_steer_away_from_wasteful_calls():
    from dreamteam.tools import tools_for

    tools_for("llm")
    assert "End every turn" in REGISTRY["wait"].description["en"]
    assert "Заканчивай им каждый ход" in REGISTRY["wait"].description["ru"]
    assert "wakes its recipients" in REGISTRY["send_message"].description["en"]
    assert "duplicate" in REGISTRY["task_create"].description["en"]
    assert "reused" in REGISTRY["mcp_create"].description["en"]


def test_docker_compose_context_sends_only_the_sources():
    rules = [line.strip() for line in (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
             if line.strip() and not line.startswith("#")]
    assert rules[0] == "*"
    assert {"!pyproject.toml", "!README.md", "!src/"} <= set(rules)
    assert "**/.dreamteam" in rules and "**/__pycache__" in rules
