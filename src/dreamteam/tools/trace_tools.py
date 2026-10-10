"""Agent traces for the user and external members (docs/SPEC.md §16.2)."""

from __future__ import annotations

from typing import Any

from ..models import ToolError
from ..tracing import render_context, render_span, render_trace, trace_line
from . import ToolContext, tool

TRACE_STATUSES = ("running", "done", "error", "step_limit", "refusal", "paused", "stopped", "interrupted", "failed")


@tool(
    "agent_traces",
    en="Inspect what agents did: each wake-up of an agent is a trace of LLM calls (thinking, text, tool calls, "
       "tokens, cost, latency) and tool executions (full arguments and results). Without ids: recent traces "
       "(filter by agent, status — e.g. \"failed\" — or task). trace_id: the steps of one trace. span_id: the "
       "full payload of one step; with context=true for an LLM step: the exact messages the model saw "
       "(page with offset/limit).",
    ru="Посмотреть, что делали агенты: каждое пробуждение агента — трейс из вызовов LLM (мысли, текст, вызовы "
       "инструментов, токены, стоимость, задержка) и выполнений инструментов (полные аргументы и результаты). "
       "Без id — последние трейсы (фильтры: agent, status — например \"failed\" — или task). trace_id — шаги "
       "одного трейса. span_id — полные данные одного шага; с context=true для шага LLM — точные сообщения, "
       "которые видела модель (листать через offset/limit).",
    properties={
        "agent": {"type": "string", "description": "Member name (default: all members)"},
        "trace_id": {"type": "integer"},
        "span_id": {"type": "integer"},
        "context": {"type": "boolean", "description": "With span_id of an LLM step: show the request messages"},
        "status": {"type": "string", "enum": list(TRACE_STATUSES)},
        "task": {"type": "integer", "description": "Only traces that touched this task"},
        "limit": {"type": "integer", "description": "Traces (default 10, max 50) or context messages (default 20)"},
        "offset": {"type": "integer", "description": "First context message (default 0)"},
    },
    audience=("user", "external"),
)
async def agent_traces(ctx: ToolContext, args: dict[str, Any]) -> str:
    tracer = getattr(ctx.swarm, "tracer", None)
    if tracer is None:
        raise ToolError("Tracing is not available in this swarm.")
    if args.get("span_id"):
        span_id = int(args["span_id"])
        if args.get("context"):
            context = tracer.span_context(span_id)
            if context is None:
                raise ToolError(f"Span #{span_id} does not exist or is not an LLM step.")
            limit = max(1, min(int(args.get("limit") or 20), 100))
            return render_context(context, offset=max(0, int(args.get("offset") or 0)), limit=limit)
        span = tracer.get_span(span_id)
        if span is None:
            raise ToolError(f"Span #{span_id} does not exist.")
        return render_span(span)
    if args.get("trace_id"):
        trace = tracer.get_trace(int(args["trace_id"]), payload_chars=600)
        if trace is None:
            raise ToolError(f"Trace #{args['trace_id']} does not exist.")
        return render_trace(trace)
    agent = None
    if args.get("agent"):
        agent = ctx.swarm.resolve_member(args["agent"], allow_user=False, allow_retired=True)
    limit = max(1, min(int(args.get("limit") or 10), 50))
    traces = tracer.list_traces(agent, limit=limit, status=args.get("status") or None,
                                task=int(args["task"]) if args.get("task") else None)
    if not traces:
        return "No traces" + (f" of {agent}" if agent else "") + " yet."
    head = f"Traces{' of ' + agent if agent else ''} (newest first; trace_id=N for the steps):"
    return head + "\n" + "\n".join(trace_line(t) for t in traces)
