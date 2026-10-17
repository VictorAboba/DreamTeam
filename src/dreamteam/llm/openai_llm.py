"""OpenAI-compatible Chat Completions client (OpenAI, OpenRouter, vLLM, Ollama...) — docs/SPEC.md §10."""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import openai

from ..config import ModelProfile
from .base import (
    LLMError,
    LLMResponse,
    ToolCall,
    Usage,
    api_error_text,
    bad_body_error,
    content_text,
    invalid_json_error,
    missing_key_hint,
    parse_arguments,
)

log = logging.getLogger(__name__)

STOP_REASONS = {
    "stop": "end_turn",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "max_tokens",
    "content_filter": "refusal",
}
MAX_TOKENS_PARAMS = ("max_completion_tokens", "max_tokens")
ERROR_PREFIX = "ERROR: "
EMPTY_OUTPUT_TEXT = "(no output)"  # same placeholder as the Anthropic client


def timeout_seconds(max_tokens: int | None) -> float:
    """Local models (Ollama/vLLM) can be slow: allow ~5 tokens/s for the whole output budget."""
    return max(600.0, (max_tokens or 0) / 5.0)


def is_official_openai(base_url: str | None) -> bool:
    return base_url is None or "api.openai.com" in base_url


def max_tokens_param(profile: ModelProfile) -> str:
    if profile.max_tokens_param:
        return profile.max_tokens_param
    return "max_completion_tokens" if is_official_openai(profile.base_url) else "max_tokens"


# --- neutral -> Chat Completions --------------------------------------------------------------

def convert_tools(tools: list[dict]) -> list[dict]:
    return [
        {"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                          "parameters": t.get("parameters") or {"type": "object", "properties": {}}}}
        for t in tools
    ]


def _assistant_message(msg: dict) -> dict:
    text = content_text(msg.get("content"))
    calls = msg.get("tool_calls") or []
    out: dict[str, Any] = {"role": "assistant", "content": text if text.strip() else None}
    if calls:
        out["tool_calls"] = [
            {"id": str(c.get("id") or ""), "type": "function",
             "function": {"name": c.get("name") or "unknown",
                          "arguments": json.dumps(parse_arguments(c.get("arguments"))[0], ensure_ascii=False)}}
            for c in calls
        ]
    elif out["content"] is None:
        # An empty turn (e.g. reasoning only, or cut off by max_tokens) must not replay as "": Claude
        # behind OpenRouter & co. rejects empty non-final assistant messages with a permanent 400.
        out["content"] = EMPTY_OUTPUT_TEXT
    return out


def _tool_message(msg: dict) -> dict:
    text = content_text(msg.get("content"))
    if msg.get("is_error"):
        text = ERROR_PREFIX + text
    return {"role": "tool", "tool_call_id": str(msg.get("tool_call_id") or ""), "content": text}


def convert_messages(system: str, messages: list[dict]) -> list[dict]:
    """Neutral history -> Chat Completions `messages` (system prompt first). Deterministic; within a run
    of user-side items, tool results come first so they directly follow the assistant tool calls."""
    out: list[dict] = [{"role": "system", "content": system}] if system else []
    results: list[dict] = []
    texts: list[dict] = []

    def flush() -> None:
        out.extend(results + texts)
        results.clear()
        texts.clear()

    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            results.append(_tool_message(msg))
        elif role == "user":
            texts.append({"role": "user", "content": content_text(msg.get("content"))})
        elif role == "assistant":
            flush()
            out.append(_assistant_message(msg))
        else:
            log.warning("Skipping history message with unknown role %r", role)
    flush()
    return out


def build_request(profile: ModelProfile, system: str, messages: list[dict], tools: list[dict], *,
                  max_tokens: int | None = None) -> dict:
    """Keyword arguments for `chat.completions.create`. `max_tokens`: this request's output limit (None: the
    profile's)."""
    params: dict[str, Any] = {"model": profile.model, "messages": convert_messages(system, messages)}
    extra = dict(profile.extra)
    limit = max_tokens or profile.max_tokens
    limit_name = max_tokens_param(profile)
    if limit_name in MAX_TOKENS_PARAMS:
        params[limit_name] = limit
    elif not max_tokens:  # an unusual provider-specific name goes into the body as is
        extra.setdefault(limit_name, limit)
    if max_tokens:
        # A raised limit wins over a limit key in extra: extra_body is merged over the body, so the old value
        # would reach the provider while the agent and the trace believe the limit was raised.
        for name in {*MAX_TOKENS_PARAMS, limit_name}:
            if name in extra or (name == limit_name and name not in MAX_TOKENS_PARAMS):
                extra[name] = limit
    if tools:
        params["tools"] = convert_tools(tools)
        params["tool_choice"] = "auto"
    if profile.effort:
        params["reasoning_effort"] = profile.effort
    if extra:
        params["extra_body"] = extra
    return params


# --- Chat Completions -> neutral --------------------------------------------------------------

def _extra(obj: Any) -> dict:
    return getattr(obj, "model_extra", None) or {}


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_usage(usage: Any) -> Usage:
    if usage is None:
        return Usage()
    prompt = usage.prompt_tokens or 0
    details = usage.prompt_tokens_details
    cached = (details.cached_tokens or 0) if details else 0
    written = (getattr(details, "cache_write_tokens", None) or 0) if details else 0
    cost = getattr(usage, "cost", None)  # OpenRouter reports the request cost in usage.cost
    if cost is None:
        cost = _extra(usage).get("cost")
    return Usage(
        input_tokens=max(prompt - cached - written, 0),
        output_tokens=usage.completion_tokens or 0,
        cache_read_tokens=cached,
        cache_write_tokens=written,
        cost_usd=_as_float(cost),
    )


def _message_text(content: Any) -> str:
    """Message content as text: some compatible servers (e.g. Mistral reasoning models) send a list of
    typed parts instead of a string; only the text parts are the answer."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and part.get("type") in (None, "text") and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "".join(parts)
    return str(content)


def _error_message(obj: Any) -> str:
    error = _extra(obj).get("error")
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or error)
    return str(error) if error else ""


def _reasoning(message: Any) -> str:
    extra = _extra(message)
    for key in ("reasoning_content", "reasoning"):
        value = extra.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _tool_calls(message: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for tc in message.tool_calls or []:
        fn = getattr(tc, "function", None)
        if fn is None:  # custom (non-function) tool calls are not used by DreamTeam
            continue
        args, error = parse_arguments(fn.arguments)
        calls.append(ToolCall(id=tc.id or f"call_{uuid.uuid4().hex[:16]}", name=fn.name,
                              arguments=args, parse_error=error))
    return calls


def parse_response(response: Any, profile: ModelProfile) -> LLMResponse:
    if not getattr(response, "choices", None):
        error = _error_message(response)
        raise LLMError(f"The provider returned no choices for model '{profile.model}' (profile "
                       f"'{profile.name}')" + (f": {error}" if error else "."), retryable=True, kind="server")
    choice = response.choices[0]
    if choice.finish_reason == "error":  # OpenRouter & co.: the upstream generation failed mid-way
        error = _error_message(choice) or "no details"
        raise LLMError(f"The provider reported a failed generation for model '{profile.model}' (profile "
                       f"'{profile.name}'): {error}", retryable=True, kind="server")
    message = choice.message
    text = _message_text(message.content)
    calls = _tool_calls(message)
    stop = STOP_REASONS.get(choice.finish_reason or "", "other")
    if calls and stop in ("end_turn", "other"):  # some servers report "stop" with tool calls
        stop = "tool_use"
    if message.refusal:
        stop, text = "refusal", message.refusal
    if stop == "refusal":
        calls = []
        text = text or "[refusal] The provider's content filter blocked the response."
    return LLMResponse(
        text=text,
        tool_calls=calls,
        usage=parse_usage(response.usage),
        stop_reason=stop,
        model=response.model or profile.model,
        raw=None,
        thinking=_reasoning(message),
    )


# --- errors -----------------------------------------------------------------------------------

def map_error(exc: BaseException, profile: ModelProfile | None = None) -> LLMError:
    """OpenAI SDK exception -> LLMError (most specific class first)."""
    detail = api_error_text(exc).rstrip(". ")
    where = f" (profile '{profile.name}', model '{profile.model}')" if profile else ""
    hint = missing_key_hint(profile) if profile else ""
    if isinstance(exc, openai.RateLimitError):
        if getattr(exc, "code", None) == "insufficient_quota" or "insufficient_quota" in detail:
            return LLMError(f"Provider quota exhausted{where}: {detail}", retryable=False, kind="rate_limit")
        return LLMError(f"Provider rate limit{where}: {detail}", retryable=True, kind="rate_limit")
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return LLMError(f"Provider authentication failed{where}: {detail}. Check api_key / api_key_env "
                        f"in dreamteam.toml.{hint}", retryable=False, kind="auth")
    if isinstance(exc, openai.NotFoundError):
        return LLMError(f"Provider returned 404{where}: {detail}. Check the model name and base_url.",
                        retryable=False, kind="bad_request")
    if isinstance(exc, (openai.BadRequestError, openai.UnprocessableEntityError)):
        return LLMError(f"Provider rejected the request{where}: {detail}", retryable=False, kind="bad_request")
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500 or exc.status_code in (408, 409):
            return LLMError(f"Provider server error{where}: {detail}", retryable=True, kind="server")
        return LLMError(f"Provider API error{where}: {detail}", retryable=False, kind="other")
    if isinstance(exc, openai.APIConnectionError):  # includes APITimeoutError
        return LLMError(f"Cannot reach the provider{where}: {detail}", retryable=True, kind="connection")
    return LLMError(f"OpenAI client error{where}: {detail}", retryable=False, kind="other")


# --- client -----------------------------------------------------------------------------------

class OpenAILLM:
    """LLMClient over any OpenAI-compatible Chat Completions endpoint."""

    def __init__(self, profile: ModelProfile, *, http_client: Any = None, max_retries: int = 3,
                 client: openai.AsyncOpenAI | None = None) -> None:
        self.profile = profile
        self.client = client or self._make_client(http_client, max_retries)

    def _make_client(self, http_client: Any, max_retries: int) -> openai.AsyncOpenAI:
        key = self.profile.resolve_api_key()
        if not key and is_official_openai(self.profile.base_url):
            key = os.environ.get("OPENAI_API_KEY")
        timeout = timeout_seconds(self.profile.max_tokens)
        kwargs: dict[str, Any] = {"api_key": key or "not-needed", "max_retries": max_retries, "timeout": timeout}
        if self.profile.base_url:
            kwargs["base_url"] = self.profile.base_url
        if http_client is not None:
            kwargs["http_client"] = http_client
        return openai.AsyncOpenAI(**kwargs)

    async def aclose(self) -> None:
        """Release the HTTP connection pool."""
        try:
            await self.client.close()
        except Exception:  # closing must never fail a shutdown
            pass

    async def complete(self, system: str, messages: list[dict], tools: list[dict], *,
                       max_tokens: int | None = None) -> LLMResponse:
        params = build_request(self.profile, system, messages, tools, max_tokens=max_tokens)
        if max_tokens and max_tokens > (self.profile.max_tokens or 0):  # a raised limit may take longer to write
            params["timeout"] = timeout_seconds(max_tokens)
        try:
            response = await self.client.chat.completions.create(**params)
        except openai.OpenAIError as exc:
            raise map_error(exc, self.profile) from exc
        except json.JSONDecodeError as exc:  # truncated / garbled body
            raise invalid_json_error("The provider", self.profile, exc) from exc
        if isinstance(response, (str, bytes)) or response is None:  # non-JSON 2xx: wrong base_url
            raise bad_body_error("Chat Completions", self.profile, response,
                                 " (OpenAI-compatible base URLs usually end with /v1)")
        return parse_response(response, self.profile)
