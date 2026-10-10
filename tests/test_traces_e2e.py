"""Agent traces end to end: a scripted swarm records traces; REST, tool and hub MCP expose them."""

from __future__ import annotations

import asyncio
import json
import urllib.parse

import pytest
from starlette.testclient import TestClient

from dreamteam import swarm as swarm_module
from dreamteam.hub_mcp import hub_call
from dreamteam.llm.base import LLMError
from dreamteam.models import USER, AgentStatus
from dreamteam.tools import ToolContext, call_tool
from dreamteam.web import create_app
from test_swarm import call, last_text, make_config, make_swarm, run_until, worker_policy


@pytest.fixture(autouse=True)
def fast_scheduler(monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.1)


def _traces(swarm, agent=None):
    return swarm.tracer.list_traces(agent, limit=500)


async def test_goal_cycle_is_traced(tmp_path):
    swarm = make_swarm(tmp_path, worker_policy({}, set()))
    await swarm.start()
    try:
        swarm.set_goal(USER, "Write two files", "hello and world")
        await run_until(lambda: (swarm.goal() or {}).get("status") == "done", timeout=40)
        await run_until(lambda: all(t["status"] != "running" for t in _traces(swarm)))
        traces = _traces(swarm)
        assert traces and all(t["status"] in ("done", "stopped") for t in traces)
        names = {m["name"] for m in swarm.members()}
        assert {t["agent"] for t in traces} <= names
        full = [swarm.tracer.get_trace(t["id"]) for t in traces]
        # every trace starts with the wake-up input and has LLM steps with usage and the model name
        for tr in full:
            assert tr["spans"][0]["kind"] == "input" and tr["spans"][0]["name"] == "wake"
            assert tr["trigger"] and tr["llm_calls"] >= 1
            llm = [s for s in tr["spans"] if s["kind"] == "llm"]
            assert all(s["name"] == "scripted" and s["usage"] and s["status"] == "ok" for s in llm)
            assert all(s["input"]["messages"] >= 1 for s in llm)
        tools = [s for tr in full for s in tr["spans"] if s["kind"] == "tool"]
        names_called = {s["name"] for s in tools}
        assert {"task_claim", "task_create", "write_file", "task_complete", "task_review",
                "goal_complete"} <= names_called
        write = next(s for s in tools if s["name"] == "write_file")
        assert write["input"]["path"].startswith("out/") and write["meta"]["call_id"]
        # tasks are linked to traces
        assert any(1 in t["tasks"] for t in traces)
        assert swarm.tracer.list_traces(task=2)
        # the context of an LLM step is the agent history up to that call
        span = next(s for tr in full for s in tr["spans"] if s["kind"] == "llm")
        ctx = swarm.tracer.span_context(span["id"])
        assert ctx["complete"] and ctx["messages"][0]["role"] == "user" and ctx["system"]
        assert len(ctx["messages"]) == span["input"]["messages"]
        assert {t["name"] for t in ctx["tools"]} >= {"board", "wait"}
        # live-only events, nothing journaled
        assert swarm.db.scalar("SELECT COUNT(*) FROM events WHERE type LIKE 'trace.%'") == 0
    finally:
        await swarm.stop()
        swarm.close()


async def test_retries_truncation_and_compaction_are_traced(tmp_path, monkeypatch):
    state = {"n": 0, "failed": False}

    def policy(swarm, name, system, messages, tools):
        if "will now be replaced by a summary" in last_text(messages):
            return "SUMMARY: did a lot of work"
        if not state["failed"]:
            state["failed"] = True
            raise LLMError("overloaded", retryable=True, kind="server")
        state["n"] += 1
        if state["n"] == 1:
            return call("shell", command="python -c \"print('x' * 5000)\"")
        if state["n"] < 5:
            return call("board")
        return call("wait")

    swarm = make_swarm(tmp_path, policy, swarm={"start_agents": 1},
                       agents={"context_tokens": 2500, "tool_output_chars": 1000})
    import dreamteam.agent as agent_module
    monkeypatch.setattr(agent_module, "RETRY_DELAYS", (0.01,) * len(agent_module.RETRY_DELAYS))
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.notify(agent, "n_restart")
        await run_until(lambda: swarm.db.scalar("SELECT COUNT(*) FROM events WHERE type = 'agent.compacted'") >= 1)
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.IDLE and
                        all(t["status"] != "running" for t in _traces(swarm, agent)))
        spans = [s for t in _traces(swarm, agent) for s in swarm.tracer.get_trace(t["id"])["spans"]]
        failed = [s for s in spans if s["kind"] == "llm" and s["status"] == "error"]
        assert failed and failed[0]["meta"]["retry_in"] and "overloaded" in failed[0]["error"]
        shell = next(s for s in spans if s["kind"] == "tool" and s["name"] == "shell")
        assert shell["meta"]["chars"] > 5000 and shell["meta"]["shown_chars"] <= 1000
        assert len(shell["output"]) > 5000  # the trace keeps the full result
        comp = next(s for s in spans if s["kind"] == "compaction")
        assert "SUMMARY" in comp["output"] and comp["status"] == "ok"
        child = next(s for s in spans if s["kind"] == "llm" and s["parent_id"] == comp["id"])
        assert child["meta"]["purpose"] == "compaction" and child["input"]["extra"]
        # a step before the compaction still shows its full context (from the archive)
        before = next(s for s in spans if s["kind"] == "llm" and s["status"] == "ok" and s["id"] < comp["id"])
        ctx = swarm.tracer.span_context(before["id"])
        assert ctx["complete"] and ctx["epoch"] == 0 and len(ctx["messages"]) == before["input"]["upto"]
        assert swarm.tracer.current_epoch(agent) == 1
    finally:
        await swarm.stop()
        swarm.close()


async def test_trace_rest_tool_and_external_member(tmp_path):
    swarm = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 1})
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.notify(agent, "n_restart")
        await run_until(lambda: any(t["status"] == "done" for t in _traces(swarm, agent)))
        trace_id = _traces(swarm, agent)[0]["id"]

        text, err = await call_tool(ToolContext(swarm, USER), "agent_traces", {"agent": agent})
        assert not err and f"#{trace_id}" in text
        text, err = await call_tool(ToolContext(swarm, USER), "agent_traces", {"trace_id": trace_id})
        assert not err and "wait" in text
        llm_span = next(s for s in swarm.tracer.get_trace(trace_id)["spans"] if s["kind"] == "llm")
        text, err = await call_tool(ToolContext(swarm, USER), "agent_traces", {"span_id": llm_span["id"], "context": True})
        assert not err and "SYSTEM:" in text

        text, err = await hub_call(swarm, "join_swarm", {"role": "Reviewer"})
        assert not err, text
        ext = next(m["name"] for m in swarm.members() if m["kind"] == "external")
        await hub_call(swarm, "board", {"as_agent": ext})
        await hub_call(swarm, "wait_for_events", {"as_agent": ext, "timeout": 0})
        ext_traces = _traces(swarm, ext)
        assert len(ext_traces) == 1 and ext_traces[0]["kind"] == "external" and ext_traces[0]["tool_calls"] == 1

        with TestClient(create_app(swarm), base_url="http://127.0.0.1:8765") as client:
            data = client.get(f"/api/traces?agent={agent}").json()
            assert data["traces"][0]["id"] == trace_id
            detail = client.get(f"/api/traces/{trace_id}").json()
            assert detail["spans"] and detail["agent"] == agent
            assert client.get("/api/traces/999999").status_code == 404
            assert client.get(f"/api/spans/{llm_span['id']}").json()["kind"] == "llm"
            ctx = client.get(f"/api/spans/{llm_span['id']}/context").json()
            assert ctx["messages"] and ctx["system"]
            first_span = detail["spans"][0]["id"]
            assert client.get(f"/api/spans/{first_span}/context").status_code == 404
            export = client.get(f"/api/traces/{trace_id}/export?contexts=1")
            assert "attachment" in export.headers["content-disposition"]
            doc = export.json()
            assert doc["member"]["name"] == agent and any("context" in s for s in doc["spans"])
            jsonl = client.get(f"/api/agents/{agent}/traces.jsonl")
            lines = [json.loads(x) for x in jsonl.text.splitlines() if x]
            assert lines and lines[-1]["id"] == trace_id
            stats = client.get(f"/api/traces/stats?agent={agent}").json()
            assert stats["traces"] >= 1 and stats["models"][0]["name"] == "scripted"
            state = client.get("/api/state").json()
            assert state["swarm"]["tracing"] is True and "session" in state["swarm"]
    finally:
        await swarm.stop()
        swarm.close()


async def test_exports_of_a_russian_member_and_nan_arguments(tmp_path):
    """Russian sessions name members in Cyrillic (HTTP header values are latin-1), and a local model may send
    NaN in tool arguments (json.loads accepts it, the HTTP JSON encoder does not)."""
    def policy(swarm, name, system, messages, tools):
        if not any(m["role"] == "tool" for m in messages):
            return {"tool_calls": [{"name": "board", "arguments": '{"note": NaN, "limit": Infinity}'}]}
        return call("wait")

    swarm = make_swarm(tmp_path, policy, swarm={"start_agents": 1, "language": "ru"})
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        assert not agent.isascii()
        swarm.notify(agent, "n_restart")
        await run_until(lambda: any(t["status"] == "done" for t in _traces(swarm, agent)))
        trace_id = _traces(swarm, agent)[0]["id"]
        with TestClient(create_app(swarm), base_url="http://127.0.0.1:8765") as client:
            detail = client.get(f"/api/traces/{trace_id}")
            assert detail.status_code == 200
            board = next(s for s in detail.json()["spans"] if s["kind"] == "tool")
            assert board["input"]["note"] == "nan"
            export = client.get(f"/api/traces/{trace_id}/export?contexts=1")
            assert export.status_code == 200 and export.json()["agent"] == agent
            disposition = export.headers["content-disposition"]
            assert f'filename="trace-{trace_id}.json"' in disposition
            assert "filename*=UTF-8''" + urllib.parse.quote(f"trace-{trace_id}-{agent.replace(' ', '_')}.json",
                                                            safe="") in disposition
            jsonl = client.get(f"/api/agents/{urllib.parse.quote(agent)}/traces.jsonl")
            assert jsonl.status_code == 200 and jsonl.headers["content-type"].startswith("application/x-ndjson")
            assert 'filename="traces.jsonl"' in jsonl.headers["content-disposition"]
            assert [json.loads(x)["id"] for x in jsonl.text.splitlines()] == [trace_id]
        text, err = await call_tool(ToolContext(swarm, USER), "agent_traces", {"trace_id": trace_id})
        assert not err and "board" in text
    finally:
        await swarm.stop()
        swarm.close()


async def test_stop_mid_call_is_not_an_error_and_failed_calls_carry_the_model(tmp_path):
    gate = asyncio.Event()
    state = {"calls": 0}

    async def policy(swarm, name, system, messages, tools):
        state["calls"] += 1
        if state["calls"] == 1:
            raise LLMError("bad request", retryable=False, kind="bad_request")
        await gate.wait()
        return call("wait")

    swarm = make_swarm(tmp_path, policy, swarm={"start_agents": 1})
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.notify(agent, "n_restart")
        await run_until(lambda: any(t["status"] == "error" for t in _traces(swarm, agent)))
        failed = swarm.tracer.get_trace(_traces(swarm, agent)[0]["id"])
        llm = [s for s in failed["spans"] if s["kind"] == "llm"]
        assert [(s["name"], s["status"], s["meta"]["purpose"]) for s in llm] == [("scripted", "error", "step")]
        assert [m["name"] for m in swarm.tracer.stats(agent)["models"]] == ["scripted"]
        swarm.notify(agent, "n_restart")
        await run_until(lambda: state["calls"] == 2)
    finally:
        await swarm.stop()
    stopped = swarm.tracer.get_trace(_traces(swarm, agent)[0]["id"])
    assert stopped["status"] == "stopped" and stopped["errors"] == 0
    assert [s["status"] for s in stopped["spans"] if s["kind"] == "llm"] == ["interrupted"]
    swarm.close()


async def test_external_session_spans_tasks_and_closes_when_idle(tmp_path, monkeypatch):
    monkeypatch.setattr(swarm_module, "EXTERNAL_IDLE_SECONDS", 1.5)
    swarm = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 0})
    await swarm.start()
    try:
        text, err = await hub_call(swarm, "join_swarm", {"role": "Reviewer"})
        assert not err, text
        ext = next(m["name"] for m in swarm.members() if m["kind"] == "external")
        for title in ("one", "two"):
            text, err = await hub_call(swarm, "task_create", {"as_agent": ext, "title": title, "goal": "g"})
            assert not err, text
        await hub_call(swarm, "task_get", {"as_agent": ext, "task_id": 1})
        await hub_call(swarm, "task_get", {"as_agent": ext, "task_id": 2})
        [session] = _traces(swarm, ext)
        assert session["status"] == "running" and session["tasks"] == [1, 2] and session["tool_calls"] == 4
        first = swarm.tracer.get_trace(session["id"])["spans"][0]
        assert session["started_at"] <= first["started_at"]
        await run_until(lambda: _traces(swarm, ext)[0]["status"] == "done", timeout=10)
        assert _traces(swarm, ext)[0]["outcome"] == "Session closed after inactivity."
    finally:
        await swarm.stop()
        swarm.close()


async def test_reconfigure_toggles_tracing(tmp_path):
    swarm = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 1})
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.reconfigure(make_config(tmp_path, swarm={"start_agents": 1}, tracing={"enabled": False}))
        assert swarm.snapshot()["swarm"]["tracing"] is False
        swarm.notify(agent, "n_restart")
        await run_until(lambda: swarm.db.scalar("SELECT COUNT(*) FROM history WHERE agent = ?", (agent,)) >= 3)
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.IDLE)
        assert _traces(swarm) == []
        swarm.reconfigure(make_config(tmp_path, swarm={"start_agents": 1}, tracing={"max_payload_chars": 50}))
        swarm.notify(agent, "n_restart")
        await run_until(lambda: any(t["status"] == "done" for t in _traces(swarm, agent)))
        wake = swarm.tracer.get_trace(_traces(swarm, agent)[0]["id"])["spans"][0]
        assert wake["name"] == "wake" and "chars cut" in wake["output"]
    finally:
        await swarm.stop()
        swarm.close()


async def test_tracing_can_be_disabled(tmp_path):
    swarm = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 1}, tracing={"enabled": False})
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.notify(agent, "n_restart")
        await run_until(lambda: swarm.db.scalar("SELECT COUNT(*) FROM history WHERE agent = ?", (agent,)) >= 3)
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.IDLE)
        assert _traces(swarm) == []
    finally:
        await swarm.stop()
        swarm.close()
