"""Traces of a thread session (docs/SPEC.md §22.9 "Traces (§16)"): tracing.TASK_ARG_KEYS unchanged, no thread tool
names a task, a thread session's traces list no tasks, agent_traces has no `task` property there and the trace REST
answers a `task` filter with 400; a board session keeps all of it. In-process test client, ScriptedLLM, no network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from dreamteam import web
from dreamteam.config import Config
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentKind
from dreamteam.swarm import Swarm
from dreamteam.tools import REGISTRY, ToolContext, call_tool, tools_for
from dreamteam.tracing import TASK_ARG_KEYS

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

BASE = "http://127.0.0.1:8765"


def make_swarm(tmp_path: Path, mode: str) -> Swarm:
    raw: dict[str, Any] = {
        "swarm": {"name": "Traces", "language": "en", "start_agents": 2, "max_agents": 4, "max_cost_usd": 0,
                  "mode": mode, "clarify": False},
        "agents": {"stagger_seconds": 0},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    config = Config.from_dict(tmp_path / "project", raw)
    return Swarm(config=config, llm_factory=lambda profile, agent: ScriptedLLM(lambda *a: "ok", profile))


@pytest.fixture
async def started(tmp_path, request):
    sw = make_swarm(tmp_path, request.param)
    await sw.start(run_agents=False)
    try:
        yield sw
    finally:
        await sw.stop()
        sw.close()


def _agent(sw: Swarm) -> str:
    return next(m["name"] for m in sw.members() if m["kind"] == AgentKind.LLM)


def test_task_arg_keys_are_unchanged_and_no_thread_tool_names_a_task():
    assert TASK_ARG_KEYS == ("task_id", "parent_id")
    for kind in ("llm", "user", "external"):
        for spec in tools_for(kind, "thread"):
            props = spec.parameters_for("thread").get("properties") or {}
            assert not {"task_id", "parent_id", "depends_on"} & set(props), spec.name
    traces = REGISTRY["agent_traces"]
    assert "task" in traces.parameters["properties"]
    assert "task" not in traces.parameters_for("thread")["properties"]


@pytest.mark.parametrize("started", ["thread"], indirect=True)
async def test_a_thread_session_lists_no_tasks_and_refuses_the_task_filter(started):
    sw = started
    assert sw.mode == "thread"
    agent = _agent(sw)
    trace = sw.tracer.begin(agent, "wake")
    span = trace.tool_start("c1", "comment", {"post_id": 3, "text": "agreed", "reply_to": 4})
    trace.tool_end(span, "Comment c1 on #3.", False)
    span = trace.tool_start("c2", "vote", {"target": "#3", "value": "like"})
    trace.tool_end(span, "Liked #3.", False)
    trace.finish("done", "No more tool calls.")
    assert sw.tracer.get_trace(trace.id)["tasks"] == []
    assert all(t["tasks"] == [] for t in sw.tracer.list_traces(agent))

    # the tool: a task argument is dropped with the usual note, the traces are listed unfiltered
    text, is_error = await call_tool(ToolContext(sw, USER), "agent_traces", {"agent": agent, "task": 3})
    assert not is_error and f"#{trace.id}" in text and "Ignored unknown argument(s): task" in text
    assert "| tasks" not in text

    with TestClient(web.create_app(sw), base_url=BASE) as client:
        for query in ("task=3", "task=0", "task=x", f"agent={agent}&task=3"):
            resp = client.get(f"/api/traces?{query}")
            assert resp.status_code == 400 and resp.json() == {"error": "a thread session has no tasks"}, query
        for query in ("", "task=", f"agent={agent}"):
            resp = client.get(f"/api/traces?{query}")
            assert resp.status_code == 200 and [t["id"] for t in resp.json()["traces"]] == [trace.id], query


@pytest.mark.parametrize("started", ["board"], indirect=True)
async def test_a_board_session_keeps_the_task_filter(started):
    sw = started
    agent = _agent(sw)
    trace = sw.tracer.begin(agent, "wake")
    span = trace.tool_start("c1", "task_claim", {"task_id": 3})
    trace.tool_end(span, "Claimed task #3.", False)
    trace.finish("done", "No more tool calls.")
    other = sw.tracer.begin(agent, "wake 2")
    other.finish("done", "No more tool calls.")
    assert sw.tracer.get_trace(trace.id)["tasks"] == [3]
    text, is_error = await call_tool(ToolContext(sw, USER), "agent_traces", {"agent": agent, "task": 3})
    assert not is_error and f"#{trace.id}" in text and f"#{other.id}" not in text and "Ignored" not in text
    with TestClient(web.create_app(sw), base_url=BASE) as client:
        resp = client.get("/api/traces?task=3")
        assert resp.status_code == 200 and [t["id"] for t in resp.json()["traces"]] == [trace.id]
        assert len(client.get("/api/traces").json()["traces"]) == 2
