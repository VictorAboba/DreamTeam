"""Agent runtime (ReAct loop) and Swarm composition-root tests: wake/notify races, stuck agents,
compaction, restart recovery, budget/pause/resume, scheduler nudges, kickoff, hiring, snapshot.

Every LLM call made by a scripted agent is checked: the history it receives must convert to a valid
Anthropic Messages request and a valid OpenAI Chat Completions request."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from dreamteam import agent as agent_module
from dreamteam import swarm as swarm_module
from dreamteam.agent import AgentRuntime
from dreamteam.config import Config
from dreamteam.llm import anthropic_llm, openai_llm
from dreamteam.llm.base import LLMError, LLMResponse, ToolCall, Usage
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentStatus, Status, ToolError
from dreamteam.swarm import Swarm

Policy = Callable[[Swarm, str, str, list[dict], list[dict]], Any]


# ------------------------------------------------------------------------------------------ helpers
def call(tool_name: str, /, **arguments: Any) -> dict:
    return {"tool_calls": [{"name": tool_name, "arguments": arguments}]}


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Runtime swarm", "language": "en", "start_agents": 1, "max_agents": 4, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 0.2, "max_steps_per_turn": 20},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0,
                               "cache_read_price": 0, "cache_write_price": 0}},
    }
    for section, values in overrides.items():
        raw.setdefault(section, {}).update(values)
    return Config.from_dict(tmp_path / "project", raw)


class Recorder:
    """Wraps a policy: validates every request history and records the calls per agent."""

    def __init__(self, policy: Policy):
        self.policy = policy
        self.calls: dict[str, list[list[dict]]] = {}
        self.violations: list[str] = []

    def __call__(self, swarm: Swarm, name: str, system: str, messages: list[dict], tools: list[dict]) -> Any:
        self.calls.setdefault(name, []).append(copy.deepcopy(messages))  # the runtime passes its live history
        try:
            check_history(messages)
        except AssertionError as exc:
            self.violations.append(f"{name}: {exc}")
        return self.policy(swarm, name, system, messages, tools)

    def count(self, name: str) -> int:
        return len(self.calls.get(name, []))


def make_swarm(tmp_path: Path, policy: Policy, **overrides: Any) -> tuple[Swarm, Recorder]:
    cfg = make_config(tmp_path, **overrides)
    recorder = Recorder(policy)
    holder: dict[str, Swarm] = {}

    def factory(profile, agent: str) -> ScriptedLLM:
        return ScriptedLLM(lambda s, m, t: recorder(holder["swarm"], agent, s, m, t), profile)

    swarm = Swarm(config=cfg, llm_factory=factory)
    holder["swarm"] = swarm
    return swarm, recorder


async def run_until(predicate: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


async def shutdown(swarm: Swarm) -> None:
    await swarm.stop()
    swarm.close()


def last_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m["role"] in ("user", "tool"):
            return m.get("content") or ""
    return ""


def all_text(messages: list[dict]) -> str:
    return "\n".join(str(m.get("content") or "") for m in messages)


def history(swarm: Swarm, name: str) -> list[dict]:
    return [json.loads(r["msg"]) for r in swarm.db.all("SELECT msg FROM history WHERE agent = ? ORDER BY seq", (name,))]


def replies_to_user(swarm: Swarm, name: str) -> list[str]:
    return [m["text"] for m in swarm.mail.conversation(USER, name) if m["sender"] == name]


def kinds_for(swarm: Swarm, recipient: str) -> list[str]:
    return [r["kind"] for r in swarm.db.all("SELECT kind FROM notifications WHERE recipient = ? ORDER BY id",
                                            (recipient,))]


def trace_statuses(swarm: Swarm, name: str) -> list[str]:
    return [t["status"] for t in reversed(swarm.tracer.list_traces(name, limit=200))]


def span_names(swarm: Swarm, name: str, kind: str) -> list[str]:
    return [s["name"] for t in reversed(swarm.tracer.list_traces(name, limit=200))
            for s in swarm.tracer.get_trace(t["id"])["spans"] if s["kind"] == kind]


def check_history(messages: list[dict]) -> None:
    """The neutral history must render to a valid Anthropic request and a valid OpenAI request."""
    assert messages, "empty history"
    req = anthropic_llm.convert_messages(messages)
    assert req[0]["role"] == "user", "the first Anthropic message must be a user message"
    assert req[-1]["role"] == "user", "the request must end with a user turn"
    for i, msg in enumerate(req):
        assert msg["role"] == ("user" if i % 2 == 0 else "assistant"), f"roles do not alternate at {i}"
        assert msg["content"], f"empty content at {i}"
        if msg["role"] == "assistant":
            uses = [b["id"] for b in msg["content"] if b.get("type") == "tool_use"]
            nxt = req[i + 1]["content"]
            results = [b["tool_use_id"] for b in nxt if b.get("type") == "tool_result"]
            assert sorted(results) == sorted(uses), f"tool_use {uses} answered by {results} at {i}"
            kinds = [b.get("type") for b in nxt]
            assert kinds == sorted(kinds, key=lambda k: k != "tool_result"), "tool_result blocks must come first"
        elif i == 0:
            assert not any(b.get("type") == "tool_result" for b in msg["content"]), "tool_result without tool_use"
    out = openai_llm.convert_messages("system", messages)
    pending: set[str] = set()
    for m in out[1:]:
        if m["role"] == "assistant":
            assert not pending, f"tool calls {pending} have no results"
            pending = {c["id"] for c in m.get("tool_calls") or []}
            assert m.get("content") or m.get("tool_calls"), "empty assistant message"
        elif m["role"] == "tool":
            assert m["tool_call_id"] in pending, f"unexpected tool result {m['tool_call_id']}"
            pending.discard(m["tool_call_id"])
        else:
            assert not pending, f"tool calls {pending} are followed by a {m['role']} message"
    assert not pending, f"tool calls {pending} have no results"


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)
    monkeypatch.setattr(agent_module, "RETRY_DELAYS", (0.01, 0.01))
    monkeypatch.setattr(agent_module, "CRASH_PAUSE_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(agent_module, "RECOVERY_DELAYS", (0.05, 0.1), raising=False)


# ------------------------------------------------------------------------- history validity / crashes
async def test_crash_during_tools_leaves_a_valid_history(tmp_path, monkeypatch):
    real = agent_module.call_tool
    state = {"boom": True}

    async def flaky(ctx, name, args):
        if name == "board" and state["boom"]:
            state["boom"] = False
            raise RuntimeError("database is locked")
        return await real(ctx, name, args)

    monkeypatch.setattr(agent_module, "call_tool", flaky)

    def policy(swarm, name, system, messages, tools):
        if "Message #" in last_text(messages) and "board" not in all_text(messages[-3:]):
            return {"tool_calls": [{"name": "board", "arguments": {}}, {"name": "agents", "arguments": {}}]}
        return call("wait")

    swarm, rec = make_swarm(tmp_path, policy)
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.mail.send(USER, agent, "first")
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.ERROR or rec.count(agent) >= 2)
        swarm.mail.send(USER, agent, "second")
        await run_until(lambda: "second" in all_text(rec.calls[agent][-1]))
        assert not rec.violations, rec.violations
        check_history(history(swarm, agent) + [{"role": "user", "content": "x"}])
        # the crashed activation's trace is closed as an error with a crash event, not left running
        await run_until(lambda: "running" not in trace_statuses(swarm, agent))
        assert trace_statuses(swarm, agent)[0] == "error" and "crash" in span_names(swarm, agent, "event")
    finally:
        await shutdown(swarm)


async def test_history_write_failure_keeps_memory_and_db_in_sync(tmp_path, monkeypatch):
    swarm, rec = make_swarm(tmp_path, lambda *a: call("wait"))
    await swarm.start(run_agents=False)
    try:
        agent = swarm.members()[0]["name"]
        runtime = AgentRuntime(swarm, agent)
        runtime._load_history()
        real = swarm.db.execute

        def failing(sql, params=()):
            if sql.startswith("INSERT INTO history"):
                raise RuntimeError("disk I/O error")
            return real(sql, params)

        monkeypatch.setattr(swarm.db, "execute", failing)
        with pytest.raises(RuntimeError):
            runtime._append({"role": "assistant", "content": "", "raw": None,
                             "tool_calls": [{"id": "c1", "name": "board", "arguments": {}}]})
        monkeypatch.setattr(swarm.db, "execute", real)
        runtime._repair_history()
        assert runtime.history == history(swarm, agent)
    finally:
        await shutdown(swarm)


async def test_repair_handles_tool_call_ids_reused_across_turns(tmp_path):
    # some OpenAI-compatible servers number tool calls per response ("call_0" in every turn)
    swarm, rec = make_swarm(tmp_path, lambda *a: call("wait"))
    await swarm.start(run_agents=False)
    try:
        agent = swarm.members()[0]["name"]
        rows = [
            {"role": "user", "content": "wake"},
            {"role": "assistant", "content": "", "raw": None, "tool_calls": [{"id": "call_0", "name": "board", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "call_0", "name": "board", "content": "ok", "is_error": False},
            {"role": "user", "content": "wake again"},
            {"role": "assistant", "content": "", "raw": None, "tool_calls": [{"id": "call_0", "name": "agents", "arguments": {}}]},
        ]
        for seq, msg in enumerate(rows):
            swarm.db.execute("INSERT INTO history(agent, seq, msg) VALUES (?, ?, ?)", (agent, seq, json.dumps(msg)))
        runtime = AgentRuntime(swarm, agent)
        runtime._load_history()
        runtime._repair_history()
        assert runtime.history[-1]["role"] == "tool" and runtime.history[-1]["tool_call_id"] == "call_0"
        assert runtime.history[-1]["is_error"]
        check_history(history(swarm, agent) + [{"role": "user", "content": "next"}])
    finally:
        await shutdown(swarm)


async def test_stray_cancelled_error_from_a_tool_does_not_kill_the_agent(tmp_path, monkeypatch):
    real = agent_module.call_tool
    state = {"boom": True}

    async def cancelling(ctx, name, args):
        if name == "board" and state["boom"]:
            state["boom"] = False
            raise asyncio.CancelledError()  # e.g. a future cancelled by another task (not this agent)
        return await real(ctx, name, args)

    monkeypatch.setattr(agent_module, "call_tool", cancelling)

    def policy(swarm, name, system, messages, tools):
        text = last_text(messages)
        if "first" in text:
            return call("board")
        if "second" in text:
            return "got the second one"
        return call("wait")

    swarm, rec = make_swarm(tmp_path, policy)
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.mail.send(USER, agent, "first")
        await run_until(lambda: rec.count(agent) >= 1 and not state["boom"])
        await asyncio.sleep(0.1)
        swarm.mail.send(USER, agent, "second")
        await run_until(lambda: "got the second one" in replies_to_user(swarm, agent))
        assert not rec.violations, rec.violations
    finally:
        await shutdown(swarm)


# --------------------------------------------------------------------------- pause / resume / budget
async def test_pause_mid_activation_resumes_and_still_answers_the_user(tmp_path):
    def policy(swarm, name, system, messages, tools):
        if not swarm.db.get("paused_once"):
            swarm.db.set("paused_once", True)
            swarm.pause(USER, "paused by the user")
            return call("board")
        return "Here is the answer you asked for."

    swarm, rec = make_swarm(tmp_path, policy)
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.mail.send(USER, agent, "What is on the board?")
        await run_until(lambda: swarm.paused and swarm.member(agent)["status"] != AgentStatus.WORKING)
        await asyncio.sleep(0.2)
        assert rec.count(agent) == 1
        swarm.resume(USER)
        await run_until(lambda: "Here is the answer you asked for." in replies_to_user(swarm, agent))
        assert not rec.violations, rec.violations
        await run_until(lambda: trace_statuses(swarm, agent) == ["stopped", "done"])
        assert "reply_to_user" in span_names(swarm, agent, "event")
    finally:
        await shutdown(swarm)


async def test_budget_pause_mid_activation_resumes_after_raising_the_limit(tmp_path):
    def policy(swarm, name, system, messages, tools):
        if not swarm.db.get("spent_once"):
            swarm.db.set("spent_once", True)
            return {"tool_calls": [{"name": "board", "arguments": {}}],
                    "usage": {"input_tokens": 1_000_000, "output_tokens": 0}}
        return {"text": "Done thinking.", "usage": {"input_tokens": 10, "output_tokens": 0}}

    swarm, rec = make_swarm(tmp_path, policy, swarm={"max_cost_usd": 0.5},
                            models={"default": {"provider": "scripted", "model": "scripted", "input_price": 1.0,
                                                "output_price": 1.0}})
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.mail.send(USER, agent, "Please think.")
        await run_until(lambda: swarm.paused and swarm.member(agent)["status"] != AgentStatus.WORKING)
        assert swarm.pause_reason == "budget"
        with pytest.raises(ToolError):
            swarm.resume(USER)
        swarm.set_limits(USER, max_cost_usd=100)
        assert not swarm.paused
        await run_until(lambda: "Done thinking." in replies_to_user(swarm, agent))
        assert not rec.violations, rec.violations
        await run_until(lambda: trace_statuses(swarm, agent) == ["paused", "done"])
        assert "budget_pause" in span_names(swarm, agent, "event")
    finally:
        await shutdown(swarm)


async def test_agent_woken_while_paused_waits_and_resumes(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda *a: "pong")
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.pause(USER, "paused by the user")
        swarm.mail.send(USER, agent, "ping")
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.PAUSED)
        assert rec.count(agent) == 0
        swarm.resume(USER)
        await run_until(lambda: "pong" in replies_to_user(swarm, agent))
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.IDLE)
        assert rec.count(agent) == 1
    finally:
        await shutdown(swarm)


async def test_backlog_larger_than_one_wake_up_is_delivered_without_new_events(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 2})
    await swarm.start()
    try:
        a, b = [m["name"] for m in swarm.members()]
        swarm.pause(USER, "manual")
        for i in range(105):  # render_wake shows at most 100 messages at once
            swarm.mail.send(b, a, f"update {i}")
        swarm.resume(USER)
        await run_until(lambda: swarm.mail.unread_count(a) == 0)
        assert "update 104" in all_text(rec.calls[a][-1])
    finally:
        await shutdown(swarm)


async def test_resume_returns_paused_idle_agents_to_idle(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 2})
    await swarm.start()
    try:
        a, b = [m["name"] for m in swarm.members()]
        swarm.pause(USER, "manual")
        swarm.signal(a)  # a wake without anything pending
        await asyncio.sleep(0.1)
        swarm.notify(b, "n_restart")
        await run_until(lambda: swarm.member(b)["status"] == AgentStatus.PAUSED)
        swarm.resume(USER)
        await run_until(lambda: swarm.member(b)["status"] == AgentStatus.IDLE and rec.count(b) == 1)
        assert swarm.member(a)["status"] == AgentStatus.IDLE and rec.count(a) == 0
    finally:
        await shutdown(swarm)


# ------------------------------------------------------------------------------------ LLM errors
async def test_retryable_llm_failure_recovers_later_by_itself(tmp_path):
    attempts = {"n": 0}

    def policy(swarm, name, system, messages, tools):
        attempts["n"] += 1
        if attempts["n"] <= 4:
            raise LLMError("overloaded", retryable=True, kind="server")
        return "Back online."

    swarm, rec = make_swarm(tmp_path, policy)
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.mail.send(USER, agent, "Are you there?")
        await run_until(lambda: "Back online." in replies_to_user(swarm, agent))
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.IDLE)
        errors = [r["text"] for r in swarm.db.all(
            "SELECT text FROM notifications WHERE recipient = ? AND kind = 'u_agent_error'", (USER,))]
        assert len(errors) == 1 and "overloaded" in errors[0]
        assert not rec.violations, rec.violations
        await run_until(lambda: trace_statuses(swarm, agent) == ["error", "done"])
        llm = [s for t in swarm.tracer.list_traces(agent) for s in swarm.tracer.get_trace(t["id"])["spans"]
               if s["kind"] == "llm"]
        assert sum(s["status"] == "error" for s in llm) == 4 and sum(s["status"] == "ok" for s in llm) == 1
        assert sorted(s["meta"].get("retry_in") is not None for s in llm if s["status"] == "error") == [
            False, True, True, True]  # the last attempt of the first activation is final
    finally:
        await shutdown(swarm)


async def test_non_retryable_llm_failure_waits_for_the_next_event(tmp_path):
    state = {"broken": True}

    def policy(swarm, name, system, messages, tools):
        if state["broken"]:
            raise LLMError("invalid x-api-key", retryable=False, kind="auth")
        return "fixed"

    swarm, rec = make_swarm(tmp_path, policy)
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.mail.send(USER, agent, "hello")
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.ERROR)
        await asyncio.sleep(0.4)
        assert rec.count(agent) == 1  # no automatic retries of a configuration error
        state["broken"] = False
        swarm.mail.send(USER, agent, "hello again")
        await run_until(lambda: "fixed" in replies_to_user(swarm, agent))
        # the reply owed for the first message is not lost: one reply for the whole conversation
        assert replies_to_user(swarm, agent) == ["fixed"]
        assert not rec.violations, rec.violations
    finally:
        await shutdown(swarm)


async def test_pause_during_retry_backoff_stops_retrying(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "RETRY_DELAYS", (0.1, 0.1, 0.1, 0.1))
    attempts = {"n": 0}

    def policy(swarm, name, system, messages, tools):
        attempts["n"] += 1
        if attempts["n"] == 1:
            swarm.pause(USER, "manual")
        raise LLMError("rate limited", retryable=True, kind="rate_limit")

    swarm, rec = make_swarm(tmp_path, policy)
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.notify(agent, "n_restart")
        await run_until(lambda: attempts["n"] >= 1)
        await asyncio.sleep(0.6)
        assert attempts["n"] == 1
        assert swarm.member(agent)["status"] != AgentStatus.ERROR
    finally:
        await shutdown(swarm)


# ------------------------------------------------------------------------------- auto-forwarding
async def test_long_plain_text_reply_is_forwarded_truncated(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda *a: "x" * 60000)
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.mail.send(USER, agent, "Write me a novel")
        await run_until(lambda: bool(replies_to_user(swarm, agent))
                        or swarm.member(agent)["status"] == AgentStatus.ERROR)
        await run_until(lambda: swarm.member(agent)["status"] != AgentStatus.WORKING)
        assert swarm.member(agent)["status"] == AgentStatus.IDLE
        reply = replies_to_user(swarm, agent)[0]
        assert reply.startswith("xxx") and len(reply) <= 50000
    finally:
        await shutdown(swarm)


async def test_reply_cut_off_by_max_tokens_reaches_the_user_whole(tmp_path):
    answers = [{"text": "Part one.", "stop_reason": "max_tokens"}, "Part two."]

    def policy(swarm, name, system, messages, tools):
        return answers.pop(0) if answers else call("wait")

    swarm, rec = make_swarm(tmp_path, policy)
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.mail.send(USER, agent, "Explain")
        await run_until(lambda: not answers and swarm.member(agent)["status"] == AgentStatus.IDLE)
        replies = replies_to_user(swarm, agent)
        assert len(replies) == 1 and "Part one." in replies[0] and "Part two." in replies[0]
        cut = rec.calls[agent][1][-1]["content"]
        assert "cut off" in cut
    finally:
        await shutdown(swarm)


async def test_plain_text_is_not_forwarded_without_a_user_message(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda *a: "thinking aloud")
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.notify(agent, "n_restart")
        await run_until(lambda: rec.count(agent) >= 1 and swarm.member(agent)["status"] == AgentStatus.IDLE)
        assert replies_to_user(swarm, agent) == []
    finally:
        await shutdown(swarm)


async def test_user_message_arriving_mid_activation_gets_the_plain_reply(tmp_path):
    def policy(swarm, name, system, messages, tools):
        n = len([m for m in messages if m["role"] == "assistant"])
        if n == 0:
            swarm.mail.send(USER, name, "by the way, how are things?")
            return call("board")
        return "All good."

    swarm, rec = make_swarm(tmp_path, policy)
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.notify(agent, "n_restart")
        await run_until(lambda: "All good." in replies_to_user(swarm, agent))
        assert "how are things" in all_text(rec.calls[agent][1])
        assert rec.count(agent) == 2
    finally:
        await shutdown(swarm)


# -------------------------------------------------------------------------------------- compaction
def seed_history(swarm: Swarm, name: str, pairs: int, size: int) -> None:
    seq = 0
    for i in range(pairs):
        msgs = [
            {"role": "user", "content": f"wake {i} " + "w" * size},
            {"role": "assistant", "content": f"working {i}", "raw": None,
             "tool_calls": [{"id": f"seed_{i}", "name": "board", "arguments": {}}]},
            {"role": "tool", "tool_call_id": f"seed_{i}", "name": "board", "content": "r" * size, "is_error": False},
            {"role": "assistant", "content": f"done {i}", "raw": None, "tool_calls": []},
        ]
        for msg in msgs:
            swarm.db.execute("INSERT INTO history(agent, seq, msg) VALUES (?, ?, ?)", (name, seq, json.dumps(msg)))
            seq += 1


def is_compaction_request(messages: list[dict]) -> bool:
    return "replaced by a summary" in last_text(messages)


async def test_compaction_keeps_the_wake_text_that_triggered_it(tmp_path):
    def policy(swarm, name, system, messages, tools):
        if is_compaction_request(messages):
            return "SUMMARY of older work"
        return call("wait")

    swarm, rec = make_swarm(tmp_path, policy, agents={"context_tokens": 20000})
    agent = None
    await swarm.start(run_agents=False)
    agent = swarm.members()[0]["name"]
    seed_history(swarm, agent, pairs=20, size=4000)
    await swarm.stop()
    await swarm.start()
    try:
        swarm.mail.send(USER, agent, "SECRET-42: deploy to staging, not production")
        await run_until(lambda: rec.count(agent) >= 2)
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.IDLE)
        assert is_compaction_request(rec.calls[agent][0])
        after = rec.calls[agent][1]
        assert "SUMMARY of older work" in all_text(after)
        assert "SECRET-42" in all_text(after)
        assert len(all_text(after)) < 10000
        assert not rec.violations, rec.violations
        stored = history(swarm, agent)
        assert "SUMMARY of older work" in stored[0]["content"]
    finally:
        await shutdown(swarm)


async def test_compaction_threshold_counts_the_last_response(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda *a: {
        "tool_calls": [{"name": "write_file", "arguments": {"path": "a.txt", "content": "z" * 3000}}],
        "usage": {"input_tokens": 900, "output_tokens": 1000}})
    await swarm.start(run_agents=False)
    try:
        agent = swarm.members()[0]["name"]
        runtime = AgentRuntime(swarm, agent)
        runtime._load_history()
        runtime.llm.profile.context_tokens = 1500
        runtime._append({"role": "user", "content": "wake"})
        response = await runtime._call_llm(runtime.history)
        runtime._append(runtime._assistant_message(response))
        runtime._append({"role": "tool", "tool_call_id": response.tool_calls[0].id, "name": "write_file",
                         "content": "ok", "is_error": False})
        # the next prompt = 900 (previous prompt) + ~1000 (the response) + the tool result > 1500
        assert runtime._needs_compaction()
    finally:
        await shutdown(swarm)


async def test_compaction_threshold_without_reported_usage_counts_tools_and_system(tmp_path):
    # some OpenAI-compatible servers send no usage: the estimate must include the tool schemas
    swarm, rec = make_swarm(tmp_path, lambda *a: {"tool_calls": [{"name": "board", "arguments": {}}],
                                                  "usage": {"input_tokens": 0, "output_tokens": 0}})
    await swarm.start(run_agents=False)
    try:
        runtime = AgentRuntime(swarm, swarm.members()[0]["name"])
        runtime._load_history()
        runtime._append({"role": "user", "content": "wake"})
        response = await runtime._call_llm(runtime.history)
        runtime._append(runtime._assistant_message(response))
        runtime._append({"role": "tool", "tool_call_id": response.tool_calls[0].id, "name": "board",
                         "content": "ok", "is_error": False})
        assert runtime.last_prompt_tokens == 0
        fixed = runtime._fixed_tokens()
        assert fixed > 1000
        runtime.llm.profile.context_tokens = fixed  # the history alone is far below this
        assert runtime._needs_compaction()
    finally:
        await shutdown(swarm)


async def test_anthropic_raw_turns_with_thinking_stay_valid_through_a_crash(tmp_path, monkeypatch):
    real = agent_module.call_tool
    state = {"boom": True}

    async def flaky(ctx, name, args):
        if name == "agents" and state["boom"]:
            state["boom"] = False
            raise RuntimeError("boom")
        return await real(ctx, name, args)

    monkeypatch.setattr(agent_module, "call_tool", flaky)
    counter = {"n": 0}

    def policy(swarm, name, system, messages, tools):
        counter["n"] += 1
        n = counter["n"]
        if n > 3:
            return call("wait")
        ids = [f"toolu_{n}_a", f"toolu_{n}_b"]
        blocks = [{"type": "thinking", "thinking": "plan", "signature": f"sig{n}"},
                  {"type": "text", "text": "Checking."},
                  {"type": "tool_use", "id": ids[0], "name": "board", "input": {}},
                  {"type": "tool_use", "id": ids[1], "name": "agents", "input": {}}]
        return LLMResponse(text="Checking.", tool_calls=[ToolCall(ids[0], "board", {}), ToolCall(ids[1], "agents", {})],
                           usage=Usage(input_tokens=100, output_tokens=10), stop_reason="tool_use", model="claude",
                           raw={"provider": "anthropic", "model": "claude", "content": blocks}, thinking="plan")

    swarm, rec = make_swarm(tmp_path, policy)
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.mail.send(USER, agent, "go")
        await run_until(lambda: counter["n"] >= 2)
        swarm.mail.send(USER, agent, "again")
        await run_until(lambda: counter["n"] >= 5 and swarm.member(agent)["status"] == AgentStatus.IDLE)
        assert not rec.violations, rec.violations
        request = anthropic_llm.build_request(swarm.config.model(None), "system",
                                              history(swarm, agent) + [{"role": "user", "content": "x"}], [],
                                              use_betas=False)
        assert any(b.get("type") == "thinking" for m in request["messages"] for b in m["content"])
    finally:
        await shutdown(swarm)


async def test_context_overflow_error_falls_back_to_a_mechanical_compaction(tmp_path):
    limit = 30000

    def policy(swarm, name, system, messages, tools):
        if len(json.dumps(messages)) > limit:
            raise LLMError("Anthropic rejected the request: 400 invalid_request_error: prompt is too long: "
                           "250000 tokens > 200000 maximum", retryable=False, kind="bad_request")
        if is_compaction_request(messages):
            return "SUMMARY"
        return "Short answer after overflow."

    swarm, rec = make_swarm(tmp_path, policy, agents={"context_tokens": 1_000_000})
    await swarm.start(run_agents=False)
    agent = swarm.members()[0]["name"]
    seed_history(swarm, agent, pairs=10, size=4000)
    swarm.db.execute("UPDATE agents SET notes = ? WHERE name = ?", ("NOTE: staging only", agent))
    await swarm.stop()
    await swarm.start()
    try:
        swarm.mail.send(USER, agent, "QUESTION-7: what next?")
        await run_until(lambda: "Short answer after overflow." in replies_to_user(swarm, agent)
                        or swarm.member(agent)["status"] == AgentStatus.ERROR)
        assert "Short answer after overflow." in replies_to_user(swarm, agent)
        final = rec.calls[agent][-1]
        assert "QUESTION-7" in all_text(final) and "NOTE: staging only" in all_text(final)
        assert not rec.violations, rec.violations
        kinds = kinds_for(swarm, USER)
        assert "u_agent_error" not in kinds
    finally:
        await shutdown(swarm)


def test_context_overflow_detection():
    overflow = agent_module.is_context_overflow
    assert overflow(LLMError("Anthropic rejected the request: prompt is too long: 210000 tokens > 200000 maximum",
                             kind="bad_request"))
    assert overflow(LLMError("Provider rejected the request: This model's maximum context length is 32768 tokens",
                             kind="bad_request"))
    assert overflow(LLMError("Provider API error: 413 Request Too Large", kind="other"))
    assert not overflow(LLMError("prompt is too long", retryable=True, kind="server"))
    assert not overflow(LLMError("invalid x-api-key", kind="auth"))
    assert not overflow(LLMError("tools.0.name: String should match pattern", kind="bad_request"))
    assert not overflow(None)


async def test_failed_compaction_call_ends_the_activation_once(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_module, "RECOVERY_DELAYS", (1.0,), raising=False)
    state = {"down": True}

    def policy(swarm, name, system, messages, tools):
        if state["down"]:
            raise LLMError("overloaded", retryable=True, kind="server")
        if is_compaction_request(messages):
            return "SUMMARY"
        return call("wait")

    swarm, rec = make_swarm(tmp_path, policy, agents={"context_tokens": 20000})
    await swarm.start(run_agents=False)
    agent = swarm.members()[0]["name"]
    seed_history(swarm, agent, pairs=20, size=4000)
    await swarm.stop()
    await swarm.start()
    try:
        swarm.notify(agent, "n_restart")
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.ERROR)
        assert all(is_compaction_request(m) for m in rec.calls[agent])  # no doomed main call after it
        assert kinds_for(swarm, USER).count("u_agent_error") == 1
        state["down"] = False
        await run_until(lambda: swarm.db.scalar(
            "SELECT COUNT(*) FROM events WHERE type = 'agent.compacted'") >= 1)
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.IDLE)
        assert not rec.violations, rec.violations
    finally:
        await shutdown(swarm)


async def test_compaction_persists_the_new_context_size(tmp_path):
    def policy(swarm, name, system, messages, tools):
        if is_compaction_request(messages):
            return {"text": "SUMMARY", "usage": {"input_tokens": 50000, "output_tokens": 10}}
        raise LLMError("invalid x-api-key", retryable=False, kind="auth")

    swarm, rec = make_swarm(tmp_path, policy, agents={"context_tokens": 20000})
    await swarm.start(run_agents=False)
    agent = swarm.members()[0]["name"]
    seed_history(swarm, agent, pairs=20, size=4000)
    await swarm.stop()
    await swarm.start()
    try:
        swarm.notify(agent, "n_restart")
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.ERROR)
        runtime = swarm.runtimes[agent]
        assert swarm.member(agent)["context_tokens"] == runtime.last_prompt_tokens < 20000
    finally:
        await shutdown(swarm)


# ------------------------------------------------------------------------- stop / retire / hire
async def test_stop_does_not_swallow_the_callers_cancellation(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda *a: call("wait"))
    await swarm.start(run_agents=False)
    try:
        runtime = AgentRuntime(swarm, swarm.members()[0]["name"])

        async def slow_exit():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(0.2)
                raise

        runtime._task = asyncio.create_task(slow_exit())
        reached: list[bool] = []

        async def caller():
            await runtime.stop()
            reached.append(True)

        outer = asyncio.create_task(caller())
        await asyncio.sleep(0.05)
        outer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await outer
        assert not reached
    finally:
        await shutdown(swarm)


async def test_stop_works_in_the_cleanup_of_a_cancelled_task(tmp_path):
    # `dreamteam run`: Ctrl+C cancels the main task, whose `finally` awaits swarm.stop()
    swarm, rec = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 2})
    await swarm.start()
    names = [m["name"] for m in swarm.members()]
    finished: list[bool] = []

    async def main():
        try:
            await asyncio.sleep(10)
        finally:
            await swarm.stop()
            finished.append(True)

    task = asyncio.create_task(main())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    swarm.close()
    assert finished == [True] and swarm.runtimes == {}
    assert len(names) == 2


async def test_stop_closes_the_llm_client(tmp_path):
    closed: list[str] = []

    class ClosableLLM(ScriptedLLM):
        async def aclose(self) -> None:
            closed.append("closed")

    cfg = make_config(tmp_path)
    swarm = Swarm(config=cfg, llm_factory=lambda profile, agent: ClosableLLM(lambda *a: call("wait"), profile))
    await swarm.start()
    agent = swarm.members()[0]["name"]
    swarm.notify(agent, "n_restart")
    await run_until(lambda: swarm.runtimes[agent]._llm is not None
                    and swarm.member(agent)["status"] == AgentStatus.IDLE)
    await shutdown(swarm)
    assert closed == ["closed"]


async def test_self_retire_skips_the_remaining_tool_calls(tmp_path):
    def policy(swarm, name, system, messages, tools):
        if not any(m["role"] == "assistant" for m in messages):
            return {"tool_calls": [{"name": "retire_agent", "arguments": {"reason": "done"}},
                                   {"name": "task_claim", "arguments": {"task_id": 1}}]}
        return call("wait")

    swarm, rec = make_swarm(tmp_path, policy, swarm={"start_agents": 2})
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        task = swarm.board.create(USER, "Free task", goal="g")
        swarm.notify(agent, "n_restart")
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.RETIRED)
        await run_until(lambda: len(history(swarm, agent)) >= 4)
        assert swarm.board.get(task.id).assignee is None
        msgs = history(swarm, agent)
        assert msgs[-1]["role"] == "tool" and msgs[-1]["is_error"]
        assert agent not in swarm.runtimes
        assert rec.count(agent) == 1
    finally:
        await shutdown(swarm)


async def test_retire_another_agent_mid_activation(tmp_path):
    gate = asyncio.Event()

    async def policy(swarm, name, system, messages, tools):
        if name == swarm.db.get("blocked"):
            await gate.wait()
        return call("wait")

    swarm, rec = make_swarm(tmp_path, policy, swarm={"start_agents": 2})
    await swarm.start()
    try:
        a, b = [m["name"] for m in swarm.members()]
        swarm.db.set("blocked", b)
        swarm.notify(b, "n_restart")
        await run_until(lambda: swarm.member(b)["status"] == AgentStatus.WORKING)
        await swarm.retire(USER, b, "not needed")
        assert swarm.member(b)["status"] == AgentStatus.RETIRED and b not in swarm.runtimes
        gate.set()
    finally:
        await shutdown(swarm)


async def test_stopping_the_swarm_mid_activation_leaves_no_working_status(tmp_path):
    gate = asyncio.Event()

    async def policy(swarm, name, system, messages, tools):
        await gate.wait()
        return call("wait")

    swarm, rec = make_swarm(tmp_path, policy)
    await swarm.start()
    agent = swarm.members()[0]["name"]
    swarm.notify(agent, "n_restart")
    await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.WORKING)
    await swarm.stop()
    assert swarm.member(agent)["status"] == AgentStatus.IDLE
    assert trace_statuses(swarm, agent) == ["stopped"]  # not left "running" until the next start
    llm = [s for s in swarm.tracer.get_trace(swarm.tracer.list_traces(agent)[0]["id"])["spans"] if s["kind"] == "llm"]
    # the call cut off by the stop is "interrupted": a cancellation is not a failed attempt (no error counted)
    assert [s["status"] for s in llm] == ["interrupted"]
    assert swarm.tracer.list_traces(agent)[0]["errors"] == 0
    swarm.close()


async def test_hire_validates_names_and_limits(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 1, "max_agents": 3})
    await swarm.start(run_agents=False)
    try:
        boss = swarm.members()[0]["name"]
        for bad in ("Prince", "John 2", "@John Smith", "x" * 30 + " " + "y" * 31, "The User", "John -"):
            with pytest.raises(ToolError):
                await swarm.hire(boss, "Dev", name=bad)
        with pytest.raises(ToolError):
            await swarm.hire(boss, "Dev", name=boss.upper())
        with pytest.raises(ToolError):
            await swarm.hire(boss, "  ")
        with pytest.raises(ToolError):
            await swarm.hire(boss, "Dev", model="missing")
        assert await swarm.hire(boss, "Dev", name="  Mary-Jane   O'Neil ") == "Mary-Jane O'Neil"
        await swarm.hire(USER, "QA")
        with pytest.raises(ToolError, match="maximum"):
            await swarm.hire(USER, "One too many")
        await swarm.retire(USER, "Mary-Jane O'Neil")
        with pytest.raises(ToolError, match="taken"):
            await swarm.hire(USER, "Dev", name="mary-jane o'neil")
        with pytest.raises(ToolError):
            await swarm.hire("Mary-Jane O'Neil", "Dev")  # retired members cannot hire
        assert swarm.runtimes == {}  # start(run_agents=False): hired agents do not run either
    finally:
        await shutdown(swarm)


# ------------------------------------------------------------------------------------ restart
async def test_restart_wakes_agents_with_open_tasks_exactly_once(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 2})
    await swarm.start(run_agents=False)
    a, b = [m["name"] for m in swarm.members()]
    swarm.set_goal(USER, "Goal")
    swarm.db.execute("UPDATE agents SET status = 'working' WHERE name = ?", (a,))
    swarm.board.claim(a, 1)
    # both had undelivered notifications from the kickoff; drain b's
    swarm.render_wake(b)
    await swarm.stop()
    swarm.close()

    # a long idle_nudge_seconds: `a` waits holding its in-progress task, which the scheduler would wake it about
    # once more later (n_stalled, Swarm._wake_idle_holders); here only the restart's own wake-ups are counted
    swarm2, rec2 = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 2},
                              agents={"idle_nudge_seconds": 60})
    await swarm2.start()
    try:
        await run_until(lambda: rec2.count(a) >= 1)
        await asyncio.sleep(0.4)
        assert rec2.count(a) == 1 and rec2.count(b) == 0
        assert "restarted" in all_text(rec2.calls[a][0])
        assert swarm2.member(a)["status"] == AgentStatus.IDLE
    finally:
        await shutdown(swarm2)


def write_history(swarm: Swarm, name: str, msgs: list[dict]) -> None:
    for seq, msg in enumerate(msgs):
        swarm.db.execute("INSERT INTO history(agent, seq, msg) VALUES (?, ?, ?)", (name, seq, json.dumps(msg)))


@pytest.mark.parametrize("paused", [False, True])
async def test_restart_continues_an_activation_cut_short_without_tasks(tmp_path, paused):
    # a reviewer / an agent answering the user has no open task and nothing pending once its wake-up
    # was rendered; if the process dies mid-activation, the restart must still continue that work
    swarm, rec = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 2})
    await swarm.start(run_agents=False)
    a, b = [m["name"] for m in swarm.members()]
    write_history(swarm, a, [
        {"role": "user", "content": "Message #1 from user to you:\nreview task #3 please"},
        {"role": "assistant", "content": "", "raw": None, "tool_calls": [{"id": "c1", "name": "task_get", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "task_get", "content": "task #3 ...", "is_error": False},
    ])
    write_history(swarm, b, [
        {"role": "user", "content": "wake"},
        {"role": "assistant", "content": "", "raw": None, "tool_calls": [{"id": "c2", "name": "wait", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "c2", "name": "wait", "content": "OK, waiting.", "is_error": False},
    ])
    if paused:
        swarm.pause(USER, "manual")
    await shutdown(swarm)

    swarm2, rec2 = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 2})
    await swarm2.start()
    try:
        if paused:
            await asyncio.sleep(0.3)
            assert rec2.count(a) == 0
            swarm2.resume(USER)
        await run_until(lambda: rec2.count(a) >= 1)
        await asyncio.sleep(0.3)
        assert rec2.count(a) == 1 and rec2.count(b) == 0
        assert ("resumed" if paused else "restarted") in all_text(rec2.calls[a][0])
        assert not rec2.violations, rec2.violations
    finally:
        await shutdown(swarm2)


# ------------------------------------------------------------------------- scheduler / kickoff
async def test_kickoff_is_staggered_and_not_preceded_by_ready_nudges(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 3},
                            agents={"stagger_seconds": 0.3, "idle_nudge_seconds": 0.05})
    await swarm.start()
    try:
        names = [m["name"] for m in swarm.members()]
        swarm.set_goal(USER, "Ship it")
        await run_until(lambda: all(rec.count(n) >= 1 for n in names), timeout=5)
        await asyncio.sleep(0.4)
        for name in names:
            assert kinds_for(swarm, name) == ["n_kickoff"], (name, kinds_for(swarm, name))
        first_wakes = {n: swarm.db.scalar("SELECT ts FROM notifications WHERE recipient = ? AND kind = 'n_kickoff'",
                                          (n,)) for n in names}
        times = [first_wakes[n] for n in names]
        assert times[1] - times[0] >= 0.25 and times[2] - times[0] >= 0.55  # (timers may only fire late)
    finally:
        await shutdown(swarm)


async def test_stop_cancels_pending_kickoff_notifications(tmp_path, caplog):
    swarm, rec = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 2},
                            agents={"stagger_seconds": 0.2})
    await swarm.start()
    swarm.set_goal(USER, "Goal")
    await shutdown(swarm)
    with caplog.at_level(logging.ERROR):
        await asyncio.sleep(0.4)
    assert "delayed notification failed" not in caplog.text


async def test_ready_nudges_need_an_active_goal_and_are_not_repeated(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 2},
                            agents={"idle_nudge_seconds": 0.05})
    await swarm.start()
    try:
        names = [m["name"] for m in swarm.members()]
        swarm.board.create(USER, "Orphan task", goal="g")
        await asyncio.sleep(0.4)
        assert all("n_ready" not in kinds_for(swarm, n) for n in names)  # no goal yet
        swarm.db.set("goal", {"title": "G", "description": "", "status": "active", "summary": "",
                              "set_by": USER, "updated_at": time.time()})
        await run_until(lambda: any("n_ready" in kinds_for(swarm, n) for n in names))
        await asyncio.sleep(0.6)
        # every idle agent hears about the task once; nobody is nudged twice about it
        assert all(kinds_for(swarm, n).count("n_ready") <= 1 for n in names)
        swarm.complete_goal(USER, "done")
        swarm.board.create(USER, "Another task", goal="g")
        await asyncio.sleep(0.4)
        assert all(kinds_for(swarm, n).count("n_ready") <= 1 for n in names)
    finally:
        await shutdown(swarm)


async def test_one_ready_task_does_not_wake_the_whole_team(tmp_path):
    async def policy(swarm, name, system, messages, tools):
        await asyncio.sleep(0.3)  # LLM latency longer than a scheduler tick
        if "Ready unassigned tasks are waiting" in last_text(messages):
            ready = swarm.board.ready_unassigned()
            if ready:
                return call("task_claim", task_id=ready[0].id)
        return call("wait")

    swarm, rec = make_swarm(tmp_path, policy, swarm={"start_agents": 4},
                            agents={"idle_nudge_seconds": 0.05})
    await swarm.start()
    try:
        names = [m["name"] for m in swarm.members()]
        swarm.db.set("goal", {"title": "G", "description": "", "status": "active", "summary": "",
                              "set_by": USER, "updated_at": time.time()})
        task = swarm.board.create(USER, "Single task", goal="g")
        await run_until(lambda: swarm.board.get(task.id).assignee is not None)
        await asyncio.sleep(0.5)
        nudged = sum(kinds_for(swarm, n).count("n_ready") for n in names)
        assert nudged == 1, nudged
    finally:
        await shutdown(swarm)


async def test_declined_ready_task_is_offered_to_the_next_agent(tmp_path):
    def policy(swarm, name, system, messages, tools):
        if "Ready unassigned tasks are waiting" in last_text(messages) and name == swarm.db.get("taker"):
            return call("task_claim", task_id=1)
        return call("wait")

    swarm, rec = make_swarm(tmp_path, policy, swarm={"start_agents": 3}, agents={"idle_nudge_seconds": 0.05})
    await swarm.start()
    try:
        names = [m["name"] for m in swarm.members()]
        swarm.db.set("taker", names[2])
        swarm.db.set("goal", {"title": "G", "description": "", "status": "active", "summary": "",
                              "set_by": USER, "updated_at": time.time()})
        swarm.board.create(USER, "Niche task", goal="g")
        await run_until(lambda: swarm.board.get(1).assignee == names[2])
    finally:
        await shutdown(swarm)


async def test_a_busy_agent_does_not_hold_back_offers_of_other_tasks(tmp_path):
    gate = asyncio.Event()
    first: list[str] = []

    async def policy(swarm, name, system, messages, tools):
        if "Ready unassigned tasks are waiting" in last_text(messages) and not first:
            first.append(name)
            await gate.wait()  # busy for a long time without claiming what it was offered
        return call("wait")

    swarm, rec = make_swarm(tmp_path, policy, swarm={"start_agents": 2}, agents={"idle_nudge_seconds": 0.05})
    await swarm.start()
    try:
        names = [m["name"] for m in swarm.members()]
        swarm.db.set("goal", {"title": "G", "description": "", "status": "active", "summary": "",
                              "set_by": USER, "updated_at": time.time()})
        t1 = swarm.board.create(USER, "First", goal="g")
        await run_until(lambda: bool(first))
        swarm.board.update(USER, t1.id, status=Status.CANCELLED)  # the offer is gone
        t2 = swarm.board.create(USER, "Second", goal="g")
        other = next(n for n in names if n != first[0])
        await run_until(lambda: "n_ready" in kinds_for(swarm, other), timeout=3)
        gate.set()
        assert t2.id
    finally:
        gate.set()
        await shutdown(swarm)


async def test_all_done_notifier_fires_once_per_board_state(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 2})
    await swarm.start()
    try:
        a, b = [m["name"] for m in swarm.members()]
        swarm.db.set("goal", {"title": "G", "description": "", "status": "active", "summary": "",
                              "set_by": USER, "updated_at": time.time()})
        t = swarm.board.create(USER, "Only task", goal="g")
        swarm.board.update(USER, t.id, status=Status.DONE)
        await run_until(lambda: "n_all_done" in kinds_for(swarm, a) + kinds_for(swarm, b))
        await asyncio.sleep(0.4)
        assert (kinds_for(swarm, a) + kinds_for(swarm, b)).count("n_all_done") == 1
        t2 = swarm.board.create(USER, "Follow-up", goal="g")
        swarm.board.update(USER, t2.id, status=Status.CANCELLED)
        await run_until(lambda: (kinds_for(swarm, a) + kinds_for(swarm, b)).count("n_all_done") == 2)
        swarm.complete_goal(USER, "ok")
        t3 = swarm.board.create(USER, "After the goal", goal="g")
        swarm.board.update(USER, t3.id, status=Status.DONE)
        await asyncio.sleep(0.4)
        assert (kinds_for(swarm, a) + kinds_for(swarm, b)).count("n_all_done") == 2
    finally:
        await shutdown(swarm)


# ---------------------------------------------------------------------------------- snapshot
async def test_snapshot_is_json_and_its_query_count_does_not_grow_with_members(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 1, "max_agents": 30})
    await swarm.start(run_agents=False)
    try:
        swarm.set_goal(USER, "Goal")
        boss = swarm.members()[0]["name"]
        swarm.board.claim(boss, 1)
        swarm.board.post(boss, 1, "hello")
        swarm.triggers.create(boss, "done(1)", note="n")
        swarm.workspace.lock(boss, "a.txt")
        swarm.mail.send(USER, boss, "hi")

        def count_queries() -> int:
            statements: list[str] = []
            swarm.db.conn.set_trace_callback(statements.append)
            try:
                snap = swarm.snapshot()
            finally:
                swarm.db.conn.set_trace_callback(None)
            json.dumps(snap)
            return len(statements)

        few = count_queries()
        for _ in range(12):
            await swarm.hire(USER, "Dev")
        await swarm.retire(USER, swarm.members()[-1]["name"])
        many = count_queries()
        assert many == few, (few, many)
        snap = swarm.snapshot()
        row = next(a for a in snap["agents"] if a["name"] == boss)
        assert row["open_tasks"] == [1] and row["unread"] == 1
        assert snap["user"]["unread"] == 0 and snap["swarm"]["counts"]["in_progress"] == 1
        assert snap["swarm"]["goal"]["title"] == "Goal"
    finally:
        await shutdown(swarm)


# ------------------------------------------------------------------------- traces / reconfigure
async def test_step_limit_and_refusal_traces(tmp_path):
    def policy(swarm, name, system, messages, tools):
        if "refuse" in last_text(messages):
            return {"text": "[refusal] declined", "stop_reason": "refusal"}
        if "step limit" in last_text(messages):
            return call("wait")
        return call("board")

    swarm, rec = make_swarm(tmp_path, policy, agents={"max_steps_per_turn": 2})
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.notify(agent, "n_restart")
        await run_until(lambda: trace_statuses(swarm, agent) == ["step_limit", "done"])
        assert "step_limit" in span_names(swarm, agent, "event")
        swarm.mail.send(USER, agent, "please refuse")
        await run_until(lambda: trace_statuses(swarm, agent)[-1:] == ["refusal"])
        assert "u_refusal" in kinds_for(swarm, USER)
        assert replies_to_user(swarm, agent) == []
    finally:
        await shutdown(swarm)


async def test_compaction_does_not_thrash_when_the_limit_is_below_the_fixed_prompt(tmp_path):
    # context_tokens smaller than system prompt + tool schemas: a fresh compaction cannot get under the
    # limit, so compacting at every step would only throw the agent's work away
    steps = {"n": 0}

    def policy(swarm, name, system, messages, tools):
        if is_compaction_request(messages):
            return "SUMMARY"
        steps["n"] += 1
        return call("board") if steps["n"] < 8 else call("wait")

    swarm, rec = make_swarm(tmp_path, policy, agents={"context_tokens": 2500, "max_steps_per_turn": 20})
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.notify(agent, "n_restart")
        await run_until(lambda: steps["n"] >= 8 and swarm.member(agent)["status"] == AgentStatus.IDLE)
        compactions = swarm.db.scalar("SELECT COUNT(*) FROM events WHERE type = 'agent.compacted'")
        assert compactions == 1, compactions
        assert swarm.tracer.current_epoch(agent) == 1
        assert not rec.violations, rec.violations
    finally:
        await shutdown(swarm)


async def test_reconfigure_applies_new_settings_to_a_running_swarm(tmp_path):
    built: list[str] = []
    closed: list[str] = []

    class ClosableLLM(ScriptedLLM):
        async def aclose(self) -> None:
            closed.append(self.profile.model)

    def factory(profile, agent):
        built.append(profile.model)
        return ClosableLLM(lambda *a: call("wait"), profile)

    cfg = make_config(tmp_path)
    swarm = Swarm(config=cfg, llm_factory=factory)
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.notify(agent, "n_restart")
        await run_until(lambda: built == ["scripted"] and swarm.member(agent)["status"] == AgentStatus.IDLE)
        old_semaphore = swarm.llm_semaphore
        new = make_config(tmp_path, agents={"max_parallel_llm_calls": 2},
                          tracing={"enabled": False, "max_payload_chars": 1000},
                          swarm={"language": "ru"},
                          models={"default": {"provider": "scripted", "model": "scripted-2"}})
        new = dataclasses.replace(new, project_dir=tmp_path / "elsewhere")
        swarm.reconfigure(new)
        await asyncio.sleep(0.05)
        assert closed == ["scripted"]  # the old client is closed in the background
        assert swarm.project_dir == swarm.config.project_dir == cfg.project_dir
        assert swarm.language == "en" and swarm.llm_semaphore is not old_semaphore
        assert swarm.tracer.enabled is False and swarm.tracer.max_payload_chars == 1000
        assert swarm.snapshot()["swarm"]["tracing"] is False
        swarm.notify(agent, "n_restart")
        await run_until(lambda: built == ["scripted", "scripted-2"])
    finally:
        await shutdown(swarm)


async def test_snapshot_session_fields(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda *a: call("wait"))
    await swarm.start(run_agents=False)
    try:
        assert swarm.snapshot()["swarm"]["session"] is None
        swarm.session_id, swarm.session_title = "001-demo", "Demo"
        assert swarm.snapshot()["swarm"]["session"] == {"id": "001-demo", "title": "Demo", "status": "running",
                                                         "pending_goal": None}
        assert swarm.snapshot()["swarm"]["tracing"] is True
    finally:
        await shutdown(swarm)


async def test_self_waking_agent_with_an_instant_llm_does_not_starve_the_event_loop(tmp_path):
    # with an LLM that answers without awaiting (ScriptedLLM, demos) the step-limit re-wake loop must
    # still yield; a watchdog thread breaks the loop if it does not, so a regression fails, not hangs
    swarm, rec = make_swarm(tmp_path, lambda *a: call("board"), agents={"max_steps_per_turn": 2})
    await swarm.start()
    agent = swarm.members()[0]["name"]
    done, starved = threading.Event(), threading.Event()

    def watchdog() -> None:
        if not done.wait(5):
            starved.set()
            for runtime in list(swarm.runtimes.values()):
                runtime._stopping = True

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        swarm.notify(agent, "n_restart")
        await asyncio.sleep(0.3)  # returns only if the agent loop yields
        done.set()
        assert not starved.is_set()
        assert rec.count(agent) > 2  # it did keep working in the meantime
    finally:
        done.set()
        await shutdown(swarm)


async def test_reconfigure_with_a_larger_budget_resumes_a_budget_pause(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"max_cost_usd": 1.0})
    await swarm.start(run_agents=False)
    try:
        agent = swarm.members()[0]["name"]
        swarm.db.execute("UPDATE agents SET cost_usd = 2 WHERE name = ?", (agent,))
        swarm.pause(USER, "budget")
        swarm.reconfigure(make_config(tmp_path, swarm={"max_cost_usd": 1.5}))
        assert swarm.paused  # still over the new budget
        swarm.reconfigure(make_config(tmp_path, swarm={"max_cost_usd": 5.0}))
        assert not swarm.paused
        swarm.pause(USER, "paused by the user")
        swarm.reconfigure(make_config(tmp_path, swarm={"max_cost_usd": 10.0}))
        assert swarm.paused  # a manual pause is the user's to lift
    finally:
        await shutdown(swarm)


async def test_reconfigure_retries_an_agent_stuck_on_a_configuration_error(tmp_path):
    def factory(profile, agent):
        def handler(system, messages, tools):
            if profile.model == "broken":
                raise LLMError("invalid x-api-key", retryable=False, kind="auth")
            return "Hello again."
        return ScriptedLLM(handler, profile)

    cfg = make_config(tmp_path, models={"default": {"provider": "scripted", "model": "broken"}})
    swarm = Swarm(config=cfg, llm_factory=factory)
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.mail.send(USER, agent, "Hi")
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.ERROR)
        swarm.reconfigure(make_config(tmp_path, models={"default": {"provider": "scripted", "model": "fixed"}}))
        await run_until(lambda: replies_to_user(swarm, agent) == ["Hello again."])
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.IDLE)
    finally:
        await shutdown(swarm)
