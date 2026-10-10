"""Provider-neutral LLM types shared by every client (docs/SPEC.md §10)."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..config import ModelProfile

STOP_REASONS = ("end_turn", "tool_use", "max_tokens", "refusal", "other")
ERROR_KINDS = ("rate_limit", "auth", "bad_request", "server", "connection", "refusal", "other")


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    parse_error: str | None = None


@dataclass
class Usage:
    input_tokens: int = 0  # uncached prompt tokens
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None  # provider-reported cost, if any

    @property
    def prompt_tokens(self) -> int:
        """Full prompt size of the request (uncached + cache read + cache write)."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass
class LLMResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    stop_reason: str  # one of STOP_REASONS
    model: str
    raw: dict | None = None
    thinking: str = ""

    def to_message(self) -> dict:
        """The neutral assistant history message (SPEC §6.1) for this response."""
        return {
            "role": "assistant",
            "content": self.text,
            "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in self.tool_calls],
            "raw": self.raw,
        }


class LLMError(Exception):
    """A failed LLM call. `retryable` errors are worth retrying with backoff."""

    def __init__(self, message: str, retryable: bool = False, kind: str = "other") -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.kind = kind if kind in ERROR_KINDS else "other"

    def __repr__(self) -> str:
        return f"LLMError({self.message!r}, retryable={self.retryable}, kind={self.kind!r})"


@runtime_checkable
class LLMClient(Protocol):
    profile: ModelProfile

    async def complete(self, system: str, messages: list[dict], tools: list[dict]) -> LLMResponse:
        """tools: [{"name","description","parameters"}]; messages: neutral format (SPEC §6.1)."""
        ...


def estimate_tokens(obj: Any) -> int:
    """Rough token estimate: JSON length / 3.5, rounded up."""
    return math.ceil(len(json.dumps(obj, ensure_ascii=False, default=str)) / 3.5)


def cost_of(profile: ModelProfile, usage: Usage, model: str | None = None) -> float:
    """USD cost of one call: the provider-reported cost, else the profile's per-1M-token prices."""
    if usage.cost_usd is not None:
        return float(usage.cost_usd)
    inp, out, cache_read, cache_write = profile.prices(model)
    total = (
        usage.input_tokens * inp
        + usage.cache_read_tokens * cache_read
        + usage.cache_write_tokens * cache_write
        + usage.output_tokens * out
    )
    return total / 1_000_000


def parse_arguments(raw: Any) -> tuple[dict, str | None]:
    """Parse tool-call arguments (JSON text or dict) into a dict; returns (args, parse_error)."""
    if isinstance(raw, dict):
        return raw, None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, None
    if not isinstance(raw, str):
        return {}, f"Tool arguments must be a JSON object, got {type(raw).__name__}."
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"Invalid JSON in tool arguments: {exc.msg} at position {exc.pos}. Send a valid JSON object."
    if not isinstance(value, dict):
        return {}, f"Tool arguments must be a JSON object, got {type(value).__name__}."
    return value, None


def api_error_text(exc: BaseException) -> str:
    """Readable text of an SDK error: '<status> <type>: <message>' when the body carries an error object.
    Gateways such as OpenRouter wrap the upstream provider's error in `metadata.raw`; it is appended
    because their own message ("Provider returned error") does not say what to fix."""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body["error"] if isinstance(body.get("error"), dict) else body
        message = err.get("message")
        if isinstance(message, str) and message:
            status = getattr(exc, "status_code", None)
            code = err.get("type") or err.get("code")
            parts = [str(p) for p in (status, code) if p]
            if len(parts) == 2 and parts[0] == parts[1]:  # numeric `code` repeating the HTTP status
                parts.pop()
            text = f"{' '.join(parts)}: {message}" if parts else message
            metadata = err.get("metadata")
            raw = metadata.get("raw") if isinstance(metadata, dict) else None
            if isinstance(raw, str) and raw.strip() and raw.strip() not in text:
                text += f" [{metadata.get('provider_name') or 'upstream'}: {raw.strip()[:1000]}]"
            return text
    return getattr(exc, "message", None) or str(exc) or type(exc).__name__


def missing_key_hint(profile: ModelProfile) -> str:
    """' The environment variable X ... is not set.' when the profile names an unset api_key_env."""
    if not profile.api_key and profile.api_key_env and not os.environ.get(profile.api_key_env):
        return (f" The environment variable {profile.api_key_env} (api_key_env of model profile "
                f"'{profile.name}') is not set.")
    return ""


def bad_body_error(provider: str, profile: ModelProfile, body: Any, hint: str = "") -> LLMError:
    """A 2xx response that is not the expected JSON object (usually a wrong base_url, e.g. a web page)."""
    snippet = " ".join(str(body).split())[:200]
    return LLMError(
        f"The {provider} endpoint {profile.base_url or '(default)'} did not return a JSON response "
        f"(profile '{profile.name}', model '{profile.model}'; got: {snippet!r}). Check base_url in "
        f"dreamteam.toml{hint}.",
        retryable=False, kind="other",
    )


def invalid_json_error(provider: str, profile: ModelProfile, exc: Exception) -> LLMError:
    """A response body that could not be decoded (truncated or garbled): worth retrying."""
    return LLMError(f"{provider} returned an unreadable response (profile '{profile.name}', model "
                    f"'{profile.model}'): {exc}", retryable=True, kind="server")


def content_text(value: Any) -> str:
    """Neutral message content as text (non-strings are JSON-encoded deterministically)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
