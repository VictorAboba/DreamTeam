"""ScriptedLLM: a deterministic LLMClient driven by a Python handler (tests / demos) — docs/SPEC.md §10."""

from __future__ import annotations

import copy
import inspect
from collections.abc import Callable, Iterable
from dataclasses import asdict
from typing import Any

from ..config import ModelProfile
from .base import LLMResponse, ToolCall, Usage, estimate_tokens, parse_arguments

# handler(system, messages, tools) -> LLMResponse | dict | str | None (or an awaitable of those)
Handler = Callable[[str, list[dict], list[dict]], Any]


def scripted_profile() -> ModelProfile:
    return ModelProfile(name="scripted", provider="scripted", model="scripted", thinking="off",
                        input_price=0.0, output_price=0.0, cache_read_price=0.0, cache_write_price=0.0)


def _tool_call(spec: Any, default_id: str) -> ToolCall:
    if isinstance(spec, ToolCall):
        return spec
    if not isinstance(spec, dict) or not spec.get("name"):
        raise TypeError(f"Scripted tool call must be a ToolCall or a dict with 'name', got {spec!r}")
    args, error = parse_arguments(spec.get("arguments"))
    return ToolCall(id=str(spec.get("id") or default_id), name=str(spec["name"]), arguments=args,
                    parse_error=spec.get("parse_error", error))


def _usage(value: Any, prompt_tokens: int, text: str, calls: list[ToolCall]) -> Usage:
    if isinstance(value, Usage):
        return value
    if isinstance(value, dict):
        return Usage(**value)
    return Usage(input_tokens=prompt_tokens, output_tokens=estimate_tokens([text, [asdict(c) for c in calls]]))


def to_response(result: Any, *, model: str, call_no: int, prompt_tokens: int = 0) -> LLMResponse:
    """Coerce a handler result: LLMResponse as is; str = text only; None = empty text; dict shorthand
    {"text", "tool_calls": [{"name", "arguments", "id"?}], "thinking"?, "stop_reason"?, "usage"?, "raw"?}."""
    if isinstance(result, LLMResponse):
        return result
    if result is None:
        result = ""
    if isinstance(result, str):
        result = {"text": result}
    if not isinstance(result, dict):
        raise TypeError(f"ScriptedLLM handler must return LLMResponse, dict, str or None, got {type(result).__name__}")
    calls = [_tool_call(c, f"call_{call_no}_{i}") for i, c in enumerate(result.get("tool_calls") or [])]
    text = str(result.get("text") or "")
    return LLMResponse(
        text=text,
        tool_calls=calls,
        usage=_usage(result.get("usage"), prompt_tokens, text, calls),
        stop_reason=str(result.get("stop_reason") or ("tool_use" if calls else "end_turn")),
        model=str(result.get("model") or model),
        raw=result.get("raw"),
        thinking=str(result.get("thinking") or ""),
    )


class ScriptedLLM:
    """LLMClient whose answers come from `handler(system, messages, tools)` (sync or async).

    Every call is recorded in `calls` ({"system", "messages", "tools"} deep copies, and "max_tokens": the
    request's output limit, None for the profile's) for assertions.
    Without an explicit usage, tokens are estimated from the request and the answer.
    """

    def __init__(self, handler: Handler, profile: ModelProfile | None = None) -> None:
        self.handler = handler
        self.profile = profile or scripted_profile()
        self.calls: list[dict] = []

    @classmethod
    def sequence(cls, responses: Iterable[Any], profile: ModelProfile | None = None,
                 default: Any = "") -> "ScriptedLLM":
        """Answer with `responses` in order, then with `default` forever."""
        queue = list(responses)

        def handler(system: str, messages: list[dict], tools: list[dict]) -> Any:
            return queue.pop(0) if queue else default

        return cls(handler, profile)

    async def complete(self, system: str, messages: list[dict], tools: list[dict], *,
                       max_tokens: int | None = None) -> LLMResponse:
        self.calls.append({"system": system, "messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools),
                           "max_tokens": max_tokens})
        # Taken before the handler runs: concurrent calls on one instance must not share tool-call ids.
        call_no = len(self.calls)
        prompt_tokens = estimate_tokens([system, messages, tools])
        result = self.handler(system, messages, tools)
        if inspect.isawaitable(result):
            result = await result
        return to_response(result, model=self.profile.model, call_no=call_no, prompt_tokens=prompt_tokens)
