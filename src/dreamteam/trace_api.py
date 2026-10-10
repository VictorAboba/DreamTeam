"""REST routes for agent traces of one swarm (docs/SPEC.md §16.3); web.create_app mounts them."""

from __future__ import annotations

import asyncio
import urllib.parse
from typing import TYPE_CHECKING, AsyncIterator

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from .httpjson import JSONResponse, json_text

if TYPE_CHECKING:
    from .swarm import Swarm

JSONL_LIMIT = 500


def _int(request: Request, name: str, default: int | None, lo: int = 0, hi: int = 10**12) -> int | None:
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return default
    try:
        return max(lo, min(int(raw), hi))
    except ValueError:
        return default


def _disposition(filename: str, fallback: str) -> dict[str, str]:
    """An attachment header. Header values are latin-1, so a member name (Cyrillic by default in Russian
    sessions) goes into the RFC 5987 `filename*`, with the ASCII `fallback` as `filename`."""
    if all(ch.isascii() and (ch.isalnum() or ch in "-_.") for ch in filename):
        value = f'attachment; filename="{filename}"'
    else:
        value = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{urllib.parse.quote(filename, safe='')}"
    return {"Content-Disposition": value, "Cache-Control": "no-store"}


def _download(data: str, filename: str, fallback: str, media_type: str) -> Response:
    return Response(data, media_type=media_type, headers=_disposition(filename, fallback))


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)[:60] or "agent"


def trace_routes(swarm: "Swarm") -> list[Route]:
    def tracer():
        return swarm.tracer

    async def traces(request: Request):
        q = request.query_params
        items = tracer().list_traces(q.get("agent") or None, limit=_int(request, "limit", 50, 1, 500),
                                     before=_int(request, "before", None), status=q.get("status") or None,
                                     task=_int(request, "task", None), kind=q.get("kind") or None)
        return JSONResponse({"traces": items})

    async def stats(request: Request):
        return JSONResponse(tracer().stats(request.query_params.get("agent") or None))

    async def trace(request: Request):
        data = tracer().get_trace(request.path_params["trace_id"], payload_chars=_int(request, "payload", 20000))
        if data is None:
            return JSONResponse({"error": "Trace not found."}, status_code=404)
        return JSONResponse(data)

    async def export(request: Request):
        trace_id = request.path_params["trace_id"]
        doc = tracer().export(trace_id, contexts=request.query_params.get("contexts") in ("1", "true", "yes"))
        if doc is None:
            return JSONResponse({"error": "Trace not found."}, status_code=404)
        return _download(json_text(doc, indent=2, default=str),
                         f"trace-{trace_id}-{_safe(doc['agent'])}.json", f"trace-{trace_id}.json", "application/json")

    async def span(request: Request):
        data = tracer().get_span(request.path_params["span_id"])
        if data is None:
            return JSONResponse({"error": "Span not found."}, status_code=404)
        return JSONResponse(data)

    async def context(request: Request):
        data = tracer().span_context(request.path_params["span_id"])
        if data is None:
            return JSONResponse({"error": "No such LLM span."}, status_code=404)
        return JSONResponse(data)

    async def agent_jsonl(request: Request):
        name = request.path_params["name"]
        ids = [item["id"] for item in reversed(tracer().list_traces(name, limit=JSONL_LIMIT))]

        async def lines() -> AsyncIterator[str]:
            # one trace at a time (up to 500 with full payloads would not fit in memory at once), yielding
            # to the event loop between them, where the agents run
            for trace_id in ids:
                doc = tracer().export(trace_id)
                if doc is not None:  # pruned meanwhile
                    yield json_text(doc, default=str) + "\n"
                await asyncio.sleep(0)

        return StreamingResponse(lines(), media_type="application/x-ndjson",
                                 headers=_disposition(f"traces-{_safe(name)}.jsonl", "traces.jsonl"))

    return [
        Route("/api/traces", traces),
        Route("/api/traces/stats", stats),
        Route("/api/traces/{trace_id:int}", trace),
        Route("/api/traces/{trace_id:int}/export", export),
        Route("/api/spans/{span_id:int}", span),
        Route("/api/spans/{span_id:int}/context", context),
        Route("/api/agents/{name}/traces.jsonl", agent_jsonl),
    ]
