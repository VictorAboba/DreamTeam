"""Unit tests of the trace store (dreamteam.tracing)."""

from __future__ import annotations

import json
import time

import pytest

from dreamteam.db import Database, dumps
from dreamteam.events import EventBus
from dreamteam.llm.base import LLMResponse, ToolCall, Usage
from dreamteam.tracing import (
    EXTERNAL_IDLE_SECONDS, Tracer, clip, render_context, render_span, render_trace, task_ids, trace_line,
)


@pytest.fixture
def store(tmp_path):
    db = Database(tmp_path / "swarm.db")
    bus = EventBus(db)
    seen: list[dict] = []
    bus.listen(seen.append)
    tracer = Tracer(db, bus, max_payload_chars=1000)
    db.execute("INSERT INTO agents(name, kind, language, status, system_prompt) VALUES (?, 'llm', 'en', 'idle', ?)",
               ("John Smith", "You are John."))
    yield tracer, db, seen
    db.close()


def _history(db, agent, messages):
    for seq, msg in enumerate(messages):
        db.execute("INSERT INTO history(agent, seq, msg) VALUES (?, ?, ?)", (agent, seq, dumps(msg)))


def _response(text="", calls=(), stop="tool_use", thinking=""):
    return LLMResponse(text=text, tool_calls=list(calls), usage=Usage(100, 20, 300, 0), stop_reason=stop,
                       model="claude-opus-5-5", thinking=thinking)


def test_clip_and_task_ids():
    assert clip("abc", 10) == "abc"
    cut = clip("x" * 50, 12)
    assert "chars cut" in cut and cut.startswith("x" * 8)
    assert clip({"a": ["y" * 30]}, 10)["a"][0] != "y" * 30
    assert clip("z" * 50, 0) == "z" * 50
    assert task_ids({"task_id": "3", "depends_on": [1, 3, "x"], "parent_id": 0}) == [3, 1]
    assert task_ids("nope") == []


def test_trace_lifecycle_counters_and_events(store):
    tracer, db, seen = store
    trace = tracer.begin("John Smith", "New task #3 for you", from_user=True)
    trace.step = 1
    trace.record("input", "wake", output="New task #3 for you")
    span = trace.llm_start(1)
    trace.llm_end(span, _response("Let me claim it", [ToolCall("c1", "task_claim", {"task_id": 3})],
                                  thinking="I should claim #3"), cost=0.01)
    tool = trace.tool_start("c1", "task_claim", {"task_id": 3})
    trace.tool_end(tool, "Claimed task #3." + "!" * 50, False, shown_chars=10)
    trace.step = 2
    span = trace.llm_start(4)
    trace.llm_error(span, "rate_limit: slow down", kind="rate_limit", retry_in=2)
    span = trace.llm_start(4, attempt=2)
    trace.llm_end(span, _response("Done.", stop="end_turn"), cost=0.02)
    trace.finish("done", "No more tool calls.")

    full = tracer.get_trace(trace.id)
    assert full["status"] == "done" and full["outcome"] == "No more tool calls."
    assert full["llm_calls"] == 3 and full["tool_calls"] == 1 and full["errors"] == 1
    assert full["input_tokens"] == 200 and full["cache_read_tokens"] == 600 and full["output_tokens"] == 40
    assert full["cost_usd"] == pytest.approx(0.03)
    assert full["steps"] == 2 and full["tasks"] == [3] and full["from_user"] is True
    assert full["model"] == "claude-opus-5-5"
    kinds = [(s["kind"], s["name"], s["status"]) for s in full["spans"]]
    assert kinds == [("input", "wake", "ok"), ("llm", "claude-opus-5-5", "ok"), ("tool", "task_claim", "ok"),
                     ("llm", "step", "error"), ("llm", "claude-opus-5-5", "ok")]
    llm = full["spans"][1]
    assert llm["output"]["thinking"] == "I should claim #3"
    assert llm["output"]["tool_calls"][0]["arguments"] == {"task_id": 3}
    assert llm["usage"]["cache_read_tokens"] == 300 and llm["meta"]["stop_reason"] == "tool_use"
    assert full["spans"][2]["meta"] == {"call_id": "c1", "chars": 66, "shown_chars": 10}
    assert full["spans"][3]["meta"]["retry_in"] == 2 and "rate_limit" in full["spans"][3]["error"]

    types = [e["type"] for e in seen]
    assert types[0] == "trace.started" and types[-1] == "trace.finished" and "trace.span" in types
    assert all(e["id"] is None for e in seen)  # live-only, not journaled
    assert db.scalar("SELECT COUNT(*) FROM events WHERE type LIKE 'trace.%'") == 0

    assert [t["id"] for t in tracer.list_traces("John Smith")] == [trace.id]
    assert tracer.list_traces("Nobody") == []
    assert [t["id"] for t in tracer.list_traces(task=3)] == [trace.id]
    assert tracer.list_traces(task=4) == []
    assert tracer.list_traces(status="failed") == []

    text = render_trace(full)
    assert "task_claim" in text and "thinking: I should claim #3" in text and "retry in 2s" in text
    assert "John Smith" in trace_line(full)
    assert "Thinking:\nI should claim #3" in render_span(tracer.get_span(llm["id"]))


def test_payloads_are_capped(store):
    tracer, _, _ = store
    trace = tracer.begin("John Smith", "w" * 5000)
    span = trace.tool_start("c1", "write_file", {"path": "a.txt", "content": "q" * 5000})
    trace.tool_end(span, "r" * 5000, False)
    trace.finish()
    full = tracer.get_trace(trace.id)
    assert len(full["trigger"]) < 1100
    tool = full["spans"][0]
    assert len(tool["input"]["content"]) < 1100 and tool["input"]["path"] == "a.txt"
    assert len(tool["output"]) < 1100 and tool["meta"]["chars"] == 5000
    small = tracer.get_trace(trace.id, payload_chars=100)
    assert len(small["spans"][0]["output"]) < 200


def test_context_reconstruction_across_compaction(store):
    tracer, db, _ = store
    agent = "John Smith"
    first = [{"role": "user", "content": "wake 1"},
             {"role": "assistant", "content": "", "tool_calls": [{"id": "a", "name": "board", "arguments": {}}]},
             {"role": "tool", "tool_call_id": "a", "name": "board", "content": "empty board"}]
    _history(db, agent, first)
    trace = tracer.begin(agent, "wake 1")
    early = trace.llm_start(1)
    trace.llm_end(early, _response("x"), 0.0)
    compaction = trace.llm_start(4, purpose="compaction", extra=[{"role": "user", "content": "Summarise"}])
    trace.llm_end(compaction, _response("summary", stop="end_turn"), 0.0)
    with db.transaction() as tx:  # what AgentRuntime._replace_history does
        epoch = tracer.archive_history(tx.conn, agent)
        tx.conn.execute("DELETE FROM history WHERE agent = ?", (agent,))
        tx.conn.execute("INSERT INTO history(agent, seq, msg) VALUES (?, 0, ?)",
                        (agent, dumps({"role": "user", "content": "Summary of the past"})))
    assert epoch == 0 and tracer.current_epoch(agent) == 1
    late = trace.llm_start(1)
    trace.llm_end(late, _response("y"), 0.0)
    trace.finish()

    ctx = tracer.span_context(early.id, tools=[])
    assert [m["content"] for m in ctx["messages"]] == ["wake 1"] and ctx["complete"]
    assert ctx["system"] == "You are John." and ctx["epoch"] == 0
    ctx = tracer.span_context(compaction.id, tools=[])
    assert len(ctx["messages"]) == 4 and ctx["messages"][-1]["content"] == "Summarise"
    ctx = tracer.span_context(late.id, tools=[{"name": "board"}])
    assert [m["content"] for m in ctx["messages"]] == ["Summary of the past"] and ctx["epoch"] == 1
    assert "SYSTEM:\nYou are John." in render_context(ctx)
    assert tracer.span_context(999) is None

    # a fresh tracer (new process) derives the epoch from the archive
    assert Tracer(db).current_epoch(agent) == 1

    doc = tracer.export(trace.id, contexts=True)
    assert doc["member"]["name"] == agent
    assert [len(s["context"]["messages"]) for s in doc["spans"] if s["kind"] == "llm"] == [1, 4, 1]
    json.dumps(doc)


def test_recover_marks_interrupted(store):
    tracer, db, _ = store
    trace = tracer.begin("John Smith", "wake")
    trace.tool_start("c1", "shell", {"command": "sleep 100"})
    assert Tracer(db).recover() == 1
    full = tracer.get_trace(trace.id)
    assert full["status"] == "interrupted" and full["spans"][0]["status"] == "interrupted"
    assert tracer.list_traces(status="failed")[0]["id"] == trace.id
    assert Tracer(db).recover() == 0


def test_disabled_tracer_is_noop(tmp_path):
    db = Database(tmp_path / "swarm.db")
    tracer = Tracer(db, enabled=False)
    trace = tracer.begin("John Smith", "wake")
    assert not trace.enabled
    span = trace.llm_start(1)
    trace.llm_end(span, _response("x"), 0.1)
    trace.record("input", "wake", output="x")
    trace.finish()
    assert db.scalar("SELECT COUNT(*) FROM traces") == 0
    db.close()


def _age_trace(db, trace_id, seconds):
    """Move a trace and its spans `seconds` into the past (as if recorded then)."""
    db.execute("UPDATE traces SET started_at = started_at - ? WHERE id = ?", (seconds, trace_id))
    db.execute("UPDATE spans SET started_at = started_at - ?, ended_at = ended_at - ? WHERE trace_id = ?",
               (seconds, seconds, trace_id))


def test_external_sessions_and_stats(store):
    tracer, db, _ = store
    now = time.time()
    tracer.external_call("Claude", "board", {}, "board text", False, now - 50)
    tracer.external_call("Claude", "task_claim", {"task_id": 2}, "Claimed.", False, now - 20)
    tracer.external_call("Claude", "task_get", {"task_id": 5}, "Got it.", False, now - 10)
    first = tracer.list_traces("Claude")[0]
    # the session holds every task it touched and starts with its first call (which happened before it was traced)
    assert first["tasks"] == [2, 5]
    assert first["started_at"] == pytest.approx(now - 50)
    _age_trace(db, first["id"], EXTERNAL_IDLE_SECONDS + 100)
    tracer.external_call("Claude", "task_get", {"task_id": 9}, "Error: no task", True, now)
    traces = tracer.list_traces("Claude")
    assert [t["status"] for t in traces] == ["running", "done"]
    assert traces[1]["tool_calls"] == 3 and traces[1]["tasks"] == [2, 5]
    # an idle session ends at its last call, not when the next one arrived
    assert traces[1]["ended_at"] < now - EXTERNAL_IDLE_SECONDS
    assert traces[0]["errors"] == 1 and traces[0]["kind"] == "external"
    tracer.close_external()
    assert all(t["status"] == "done" for t in tracer.list_traces("Claude"))
    assert "external session" in trace_line(traces[0])

    trace = tracer.begin("John Smith", "wake")
    trace.llm_end(trace.llm_start(1), _response("a"), 0.5)
    trace.finish()
    stats = tracer.stats()
    assert stats["traces"] == 3 and stats["cost_usd"] == pytest.approx(0.5)
    assert {t["name"]: t["calls"] for t in stats["tools"]} == {"board": 1, "task_claim": 1, "task_get": 2}
    assert stats["models"][0]["name"] == "claude-opus-5-5" and stats["models"][0]["output_tokens"] == 20
    assert tracer.stats("John Smith")["traces"] == 1


def test_prune(store):
    tracer, db, _ = store
    old = tracer.begin("John Smith", "old")
    old.record("input", "wake", output="old")
    old.finish()
    db.execute("UPDATE traces SET started_at = started_at - 10 * 86400 WHERE id = ?", (old.id,))
    new = tracer.begin("John Smith", "new")
    new.finish()
    assert tracer.prune(0) == 0
    assert tracer.prune(5) == 1
    assert [t["id"] for t in tracer.list_traces()] == [new.id]
    assert db.scalar("SELECT COUNT(*) FROM spans WHERE trace_id = ?", (old.id,)) == 0


def test_model_output_that_sqlite_or_http_json_reject(store):
    """Lone surrogates (TEXT columns are UTF-8) and NaN / Infinity (the HTTP JSON encoder refuses them) come
    straight from model output; tracing must neither break the agent nor make a trace unreadable."""
    from dreamteam.httpjson import json_text

    tracer, _, _ = store
    trace = tracer.begin("John Smith", "goal: \ud800 broken")
    span = trace.tool_start("c1", "bad\udc00tool", {"x": float("nan"), "y": [float("inf")], "s": "\ud800"})
    trace.tool_end(span, "result \ud800", True)
    trace.llm_error(trace.llm_start(2), "provider said \udfff", kind="server", model="m\ud800")
    trace.record("event", "crash", output="x", status="error", error="boom \ud800")
    trace.finish("error", "outcome \ud800")
    full = tracer.get_trace(trace.id)
    assert full["status"] == "error" and "�" in full["trigger"] and "�" in full["outcome"]
    assert full["spans"][0]["name"] == "bad�tool"
    assert full["spans"][0]["input"]["x"] == "nan" and full["spans"][0]["input"]["y"] == ["inf"]
    assert full["spans"][0]["input"]["s"] == "\ud800"  # JSON payloads keep the exact value
    json.loads(json_text(full))  # what /api/traces/{id} sends
    for text in (render_trace(full), render_span(tracer.get_span(span.id)), trace_line(full)):
        text.encode("utf-8")  # the CLI prints these and MCP clients receive them


def test_failed_attempt_is_named_after_the_model_and_cancellation_is_no_error(store):
    tracer, _, _ = store
    trace = tracer.begin("John Smith", "wake")
    trace.llm_error(trace.llm_start(1), "overloaded", kind="server", retry_in=2, model="claude-opus-5-5")
    trace.llm_error(trace.llm_start(1, attempt=2), "Cancelled", kind="cancelled", model="claude-opus-5-5",
                    status="interrupted")
    trace.finish("stopped")
    full = tracer.get_trace(trace.id)
    assert [(s["name"], s["status"], s["meta"]["purpose"]) for s in full["spans"]] == [
        ("claude-opus-5-5", "error", "step"), ("claude-opus-5-5", "interrupted", "step")]
    assert full["errors"] == 1 and full["llm_calls"] == 2
    assert [(m["name"], m["calls"], m["errors"]) for m in tracer.stats()["models"]] == [("claude-opus-5-5", 2, 1)]


def test_epoch_counter_survives_rollback_prune_and_other_readers(store):
    tracer, db, _ = store
    agent = "John Smith"
    _history(db, agent, [{"role": "user", "content": f"m{i}"} for i in range(3)])
    trace = tracer.begin(agent, "wake")
    early = trace.llm_start(3)
    trace.llm_end(early, _response("x"), 0.0)
    trace.finish()
    with pytest.raises(RuntimeError):
        with db.transaction() as tx:  # a compaction whose transaction fails keeps the history and the epoch
            tracer.archive_history(tx.conn, agent)
            raise RuntimeError("disk full")
    assert tracer.current_epoch(agent) == 0 and tracer.span_context(early.id, tools=[])["complete"]

    reader = Tracer(db)  # e.g. `dreamteam traces` in another process, created before the compaction
    assert reader.current_epoch(agent) == 0
    with db.transaction() as tx:
        assert tracer.archive_history(tx.conn, agent) == 0
        tx.conn.execute("DELETE FROM history WHERE agent = ?", (agent,))
        tx.conn.execute("INSERT INTO history(agent, seq, msg) VALUES (?, 0, ?)",
                        (agent, dumps({"role": "user", "content": "S"})))
    ctx = reader.span_context(early.id, tools=[])
    assert ctx["complete"] and [m["content"] for m in ctx["messages"]] == ["m0", "m1", "m2"]

    # retention removes old traces and old archived history, but the epoch numbering goes on
    db.execute("UPDATE traces SET started_at = started_at - 40 * 86400")
    db.execute("UPDATE history_archive SET archived_at = archived_at - 40 * 86400")
    assert tracer.prune(30) == 1
    assert db.scalar("SELECT COUNT(*) FROM history_archive WHERE msg IS NOT NULL") == 0
    assert Tracer(db).current_epoch(agent) == 1
    # archived history is pruned even when no trace is old enough to go
    with db.transaction() as tx:
        tracer.archive_history(tx.conn, agent)
    db.execute("UPDATE history_archive SET archived_at = archived_at - 40 * 86400 WHERE epoch = 1")
    assert tracer.prune(30) == 0
    assert db.scalar("SELECT COUNT(*) FROM history_archive WHERE msg IS NOT NULL") == 0
    assert Tracer(db).current_epoch(agent) == 2


def test_archive_with_tracing_disabled(tmp_path):
    db = Database(tmp_path / "swarm.db")
    tracer = Tracer(db, enabled=False)
    _history(db, "Ann Lee", [{"role": "user", "content": "hi"}])
    with db.transaction() as tx:  # never traced: only the epoch is marked
        assert tracer.archive_history(tx.conn, "Ann Lee") == 0
    assert db.scalar("SELECT COUNT(*) FROM history_archive WHERE msg IS NOT NULL") == 0
    assert tracer.current_epoch("Ann Lee") == 1

    # traced, then tracing was turned off: the LLM calls of that epoch keep their context
    tracer.enabled = True
    trace = tracer.begin("Ann Lee", "wake")
    span = trace.llm_start(1)
    trace.llm_end(span, _response("x"), 0.0)
    trace.finish()
    tracer.enabled = False
    with db.transaction() as tx:
        assert tracer.archive_history(tx.conn, "Ann Lee") == 1
        tx.conn.execute("DELETE FROM history WHERE agent = ?", ("Ann Lee",))
    ctx = tracer.span_context(span.id, tools=[])
    assert ctx["complete"] and ctx["epoch"] == 1 and ctx["messages"] == [{"role": "user", "content": "hi"}]
    with db.transaction() as tx:  # the next epoch had no traced call
        assert tracer.archive_history(tx.conn, "Ann Lee") == 2
    assert db.scalar("SELECT COUNT(*) FROM history_archive WHERE epoch = 2") == 1
    db.close()


def test_external_sessions_close_on_idle_and_recover_as_done(store):
    tracer, db, _ = store
    now = time.time()
    tracer.external_call("Ann Lee", "board", {}, "board", False, now - 5)
    tracer.external_call("Bob Ray", "board", {}, "board", False, now - 5)
    ann, bob = tracer.list_traces("Ann Lee")[0], tracer.list_traces("Bob Ray")[0]
    _age_trace(db, ann["id"], EXTERNAL_IDLE_SECONDS + 60)
    assert tracer.close_external(idle_seconds=EXTERNAL_IDLE_SECONDS) == 1
    ann = tracer.trace_summary(ann["id"])
    assert ann["status"] == "done" and ann["ended_at"] < now - EXTERNAL_IDLE_SECONDS
    assert tracer.trace_summary(bob["id"])["status"] == "running"
    # a restart closes an external member's session as done, not as an interrupted (failed) activation
    assert Tracer(db).recover() == 1
    bob = tracer.trace_summary(bob["id"])
    assert bob["status"] == "done" and bob["ended_at"] == pytest.approx(now, abs=5)
    assert tracer.list_traces(status="failed") == []


def test_export_contexts_match_span_context_and_stats_use_an_index(store):
    tracer, db, _ = store
    agent = "John Smith"
    _history(db, agent, [{"role": "user", "content": f"m{i}"} for i in range(5)])
    trace = tracer.begin(agent, "wake")
    spans = []
    for n in (1, 3, 5):
        span = trace.llm_start(n)
        trace.llm_end(span, _response("x"), 0.0)
        spans.append(span)
    trace.finish()
    doc = tracer.export(trace.id, contexts=True)
    for span, exported in zip(spans, [s for s in doc["spans"] if s["kind"] == "llm"]):
        assert exported["context"]["messages"] == tracer.span_context(span.id, tools=[])["messages"]
    assert [len(s["context"]["messages"]) for s in doc["spans"]] == [1, 3, 5]
    plan = " ".join(r["detail"] for r in db.all(
        "EXPLAIN QUERY PLAN SELECT name, SUM(cost_usd), SUM(json_extract(usage, '$.output_tokens')) FROM spans "
        "WHERE agent = ? AND kind = 'llm' GROUP BY name", (agent,)))
    assert "COVERING INDEX spans_stats" in plan  # big payloads are not read for stats


def test_disabled_writer_does_not_query(tmp_path):
    db = Database(tmp_path / "swarm.db")
    tracer = Tracer(db, enabled=False)
    seen: list[str] = []
    db.conn.set_trace_callback(seen.append)
    trace = tracer.begin("John Smith", "wake")
    trace.llm_end(trace.llm_start(3), _response("x"), 0.1)
    trace.tool_end(trace.tool_start("c", "board", {"task_id": 1}), "ok", False)
    trace.llm_error(trace.llm_start(3), "x", model="m")
    trace.finish()
    db.conn.set_trace_callback(None)
    assert seen == []
    db.close()


def test_render_context_pages():
    ctx = {"span_id": 7, "agent": "A B", "model": "m", "system": "sys", "tools": [], "note": "",
           "messages": [{"role": "user", "content": "one"},
                        {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "arguments": {}}]}]}
    assert "showing 1-2" in render_context(ctx)
    page = render_context(ctx, offset=5)
    assert "none from offset 5" in page and "SYSTEM" not in page
    assert "→ None {}" in render_context(ctx)  # a call without a name does not break the page
