"""Anthropic Messages API client (`anthropic.AsyncAnthropic`, SDK 1.x) — docs/SPEC.md §10."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

import anthropic

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

# The SDK documents this one header for the `fallbacks` request param, in both of its forms
# ("default" and a list of models); see anthropic/lib/middleware/_fallbacks.py.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
THINKING_UPDATES_BETA = "thinking-display-updates-2026-08-18"

# Fragments of a 400 that mean "this endpoint does not accept our beta parameters".
BETA_REJECTION_MARKERS = ("fallbacks", "block_binding", "betas", "anthropic-beta", "extra inputs are not permitted")
THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})

STOP_REASONS = {
    "end_turn": "end_turn",
    "stop_sequence": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "model_context_window_exceeded": "max_tokens",
    "refusal": "refusal",
    "pause_turn": "other",
}

START_TEXT = "(conversation start)"
EMPTY_USER_TEXT = "(empty message)"
EMPTY_OUTPUT_TEXT = "(no output)"

_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


# --- neutral -> Messages API ------------------------------------------------------------------

def safe_tool_id(value: Any) -> str:
    """Tool ids must match ^[A-Za-z0-9_-]+$; ids from other providers are mapped deterministically."""
    return _ID_RE.sub("_", str(value or "")) or "call"


def convert_tools(tools: list[dict]) -> list[dict]:
    return [
        {"name": t["name"], "description": t.get("description", ""), "input_schema": t.get("parameters")
         or {"type": "object", "properties": {}}}
        for t in tools
    ]


def _text_block(text: str, placeholder: str) -> dict:
    return {"type": "text", "text": text if text.strip() else placeholder}


def _tool_result_block(msg: dict) -> dict:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": safe_tool_id(msg.get("tool_call_id")),
        "content": _text_block(content_text(msg.get("content")), EMPTY_OUTPUT_TEXT)["text"],
    }
    if msg.get("is_error"):
        block["is_error"] = True
    return block


def _keep_block(block: Any, strip_thinking: bool, fallback_blocks: bool) -> bool:
    kind = block.get("type") if isinstance(block, dict) else None
    if strip_thinking and kind in THINKING_TYPES:
        return False
    if not fallback_blocks and kind == "fallback":
        return False
    return True


def _neutral_assistant_blocks(msg: dict) -> list[dict]:
    blocks: list[dict] = []
    text = content_text(msg.get("content"))
    if text.strip():
        blocks.append({"type": "text", "text": text})
    for call in msg.get("tool_calls") or []:
        args, _ = parse_arguments(call.get("arguments"))
        blocks.append({"type": "tool_use", "id": safe_tool_id(call.get("id")),
                       "name": call.get("name") or "unknown", "input": args})
    return blocks


def _assistant_blocks(msg: dict, strip_thinking: bool, fallback_blocks: bool) -> list[dict]:
    raw = msg.get("raw")
    if isinstance(raw, dict) and raw.get("provider") == "anthropic" and isinstance(raw.get("content"), list):
        blocks = [b for b in raw["content"] if _keep_block(b, strip_thinking, fallback_blocks)]
        if blocks:
            return blocks
    return _neutral_assistant_blocks(msg) or [{"type": "text", "text": EMPTY_OUTPUT_TEXT}]


def convert_messages(messages: list[dict], *, strip_thinking_before: int = 0,
                     fallback_blocks: bool = True) -> list[dict]:
    """Neutral history -> Messages API `messages`. Pure and deterministic: the same history always
    renders to the same bytes, and appending to the history never changes earlier messages.

    Consecutive user-side items (tool results, user texts) become ONE user message, tool_result
    blocks first. Anthropic assistant turns replay `raw.content` verbatim, except that thinking
    blocks of the first `strip_thinking_before` neutral messages are removed (signature recovery)
    and `fallback` blocks are dropped when the request is not on the fallback beta.
    """
    out: list[dict] = []
    results: list[dict] = []
    texts: list[dict] = []

    def flush() -> None:
        if results or texts:
            out.append({"role": "user", "content": results + texts})
            results.clear()
            texts.clear()

    for index, msg in enumerate(messages):
        role = msg.get("role")
        if role == "tool":
            results.append(_tool_result_block(msg))
        elif role == "user":
            texts.append(_text_block(content_text(msg.get("content")), EMPTY_USER_TEXT))
        elif role == "assistant":
            flush()
            if not out:  # the first message must be a user message
                out.append({"role": "user", "content": [{"type": "text", "text": START_TEXT}]})
            out.append({"role": "assistant",
                        "content": _assistant_blocks(msg, index < strip_thinking_before, fallback_blocks)})
        else:
            log.warning("Skipping history message with unknown role %r", role)
    flush()
    return out


def thinking_mode(profile: ModelProfile) -> str:
    mode = (profile.thinking or "adaptive").strip().lower()
    return mode if mode in ("off", "between_tools") else "adaptive"


def thinking_config(profile: ModelProfile, *, betas: list[str]) -> dict | None:
    """`thinking` parameter; beta-only fields are included only when their beta is in `betas`."""
    mode = thinking_mode(profile)
    if mode == "off":
        return None
    if mode == "between_tools":  # takes no other field
        return {"type": "between_tools"}
    cfg: dict[str, Any] = {"type": "adaptive"}
    display = profile.thinking_display
    if display == "updates" and THINKING_UPDATES_BETA not in betas:
        display = None  # progress-update display needs its beta; fall back to the API default
    if display:
        cfg["display"] = display
    if THINKING_BINDING_BETA in betas and profile.thinking_mismatch:
        cfg["block_binding"] = {"prefix_mismatch_behavior": profile.thinking_mismatch}
    return cfg


def fallbacks_param(profile: ModelProfile) -> str | list[dict] | None:
    value = (profile.fallbacks or "").strip()
    if not value:
        return None
    if value == "default":
        return "default"
    return [{"model": m.strip()} for m in value.split(",") if m.strip()]


def beta_names(profile: ModelProfile) -> list[str]:
    betas: list[str] = []
    if fallbacks_param(profile):
        betas.append(FALLBACK_BETA)
    if thinking_mode(profile) == "adaptive":
        if profile.thinking_mismatch:
            betas.append(THINKING_BINDING_BETA)
        if profile.thinking_display == "updates":
            betas.append(THINKING_UPDATES_BETA)
    return betas


def build_request(profile: ModelProfile, system: str, messages: list[dict], tools: list[dict], *,
                  use_betas: bool, strip_thinking_before: int = 0, max_tokens: int | None = None) -> dict:
    """Keyword arguments for `messages.create` (or `beta.messages.create` when use_betas). `max_tokens`: this
    request's output limit (None: the profile's)."""
    betas = beta_names(profile) if use_betas else []
    params: dict[str, Any] = {
        "model": profile.model,
        "max_tokens": max_tokens or profile.max_tokens,
        "messages": convert_messages(
            messages,
            strip_thinking_before=strip_thinking_before,
            fallback_blocks=FALLBACK_BETA in betas,
        ),
        "cache_control": {"type": "ephemeral"},
    }
    if system:
        params["system"] = system
    if tools:
        params["tools"] = convert_tools(tools)
    thinking = thinking_config(profile, betas=betas)
    if thinking:
        params["thinking"] = thinking
    if profile.effort:
        params["output_config"] = {"effort": profile.effort}
    if betas:
        params["betas"] = betas
        fallbacks = fallbacks_param(profile)
        if fallbacks is not None:
            params["fallbacks"] = fallbacks
    extra = dict(profile.extra)
    if max_tokens:  # a raised limit wins over max_tokens in extra (extra_body is merged over the body)
        extra.pop("max_tokens", None)
    if extra:
        params["extra_body"] = extra
    return params


# --- Messages API -> neutral ------------------------------------------------------------------

def _join_text(blocks: list[Any]) -> str:
    """Adjacent text blocks (citations) join directly; text separated by other blocks gets a blank line."""
    out, prev_text = "", False
    for block in blocks:
        if getattr(block, "type", None) == "text":
            if out and not prev_text:
                out += "\n\n"
            out += block.text or ""
            prev_text = True
        else:
            prev_text = False
    return out


def refusal_text(category: str | None, explanation: str | None, partial: str = "") -> str:
    line = f"[refusal] The model declined to respond (category: {category or 'unspecified'})."
    if explanation:
        line += f" {explanation}"
    return f"{partial.strip()}\n\n{line}" if partial.strip() else line


def parse_usage(usage: Any) -> Usage:
    if usage is None:
        return Usage()
    return Usage(
        input_tokens=getattr(usage, "input_tokens", None) or 0,
        output_tokens=getattr(usage, "output_tokens", None) or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
    )


def parse_response(response: Any) -> LLMResponse:
    """Message / BetaMessage -> LLMResponse; every content block is kept in `raw` for replay."""
    blocks = list(response.content or [])
    stop = STOP_REASONS.get(response.stop_reason or "", "other")
    if stop == "refusal":  # unpaired tool_use blocks of a refused turn must not be replayed
        blocks = [b for b in blocks if getattr(b, "type", None) != "tool_use"]
    calls: list[ToolCall] = []
    thinking: list[str] = []
    for block in blocks:
        kind = getattr(block, "type", None)
        if kind == "thinking" and block.thinking:
            thinking.append(block.thinking)
        elif kind == "tool_use":
            args, error = parse_arguments(block.input)
            calls.append(ToolCall(id=block.id, name=block.name, arguments=args, parse_error=error))
    text = _join_text(blocks)
    if stop == "refusal":
        details = getattr(response, "stop_details", None)
        text = refusal_text(getattr(details, "category", None), getattr(details, "explanation", None), text)
        log.info("Model %s refused: %s", response.model, text[-300:])
    return LLMResponse(
        text=text,
        tool_calls=calls,
        usage=parse_usage(response.usage),
        stop_reason=stop,
        model=response.model,
        raw={"provider": "anthropic", "model": response.model,
             "content": [b.to_dict(mode="json", exclude_none=True) for b in blocks]},
        thinking="\n\n".join(thinking),
    )


# --- errors -----------------------------------------------------------------------------------

def is_signature_error(text: str) -> bool:
    return "signature" in text.lower()


def is_beta_rejection(text: str) -> bool:
    low = text.lower()
    return not is_signature_error(text) and any(marker in low for marker in BETA_REJECTION_MARKERS)


def map_error(exc: BaseException, profile: ModelProfile | None = None) -> LLMError:
    """Anthropic SDK exception -> LLMError (most specific class first)."""
    detail = api_error_text(exc).rstrip(". ")
    where = f" (profile '{profile.name}', model '{profile.model}')" if profile else ""
    hint = missing_key_hint(profile) if profile else ""
    if isinstance(exc, anthropic.RateLimitError):
        return LLMError(f"Anthropic rate limit{where}: {detail}", retryable=True, kind="rate_limit")
    if isinstance(exc, anthropic.AuthenticationError):
        return LLMError(f"Anthropic authentication failed{where}: {detail}. Check api_key / api_key_env "
                        f"in dreamteam.toml (or run `ant auth login`).{hint}", retryable=False, kind="auth")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return LLMError(f"Anthropic permission denied{where}: {detail}. The key has no access to this "
                        "model or feature.", retryable=False, kind="auth")
    if isinstance(exc, anthropic.NotFoundError):
        return LLMError(f"Anthropic API returned 404{where}: {detail}. Check the model name and base_url.",
                        retryable=False, kind="bad_request")
    if isinstance(exc, (anthropic.BadRequestError, anthropic.UnprocessableEntityError,
                        anthropic.RequestTooLargeError)):
        return LLMError(f"Anthropic rejected the request{where}: {detail}", retryable=False, kind="bad_request")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500 or exc.status_code in (408, 409):
            return LLMError(f"Anthropic server error{where}: {detail}", retryable=True, kind="server")
        return LLMError(f"Anthropic API error{where}: {detail}", retryable=False, kind="other")
    if isinstance(exc, anthropic.APIConnectionError):  # includes APITimeoutError
        return LLMError(f"Cannot reach the Anthropic API{where}: {detail}", retryable=True, kind="connection")
    if isinstance(exc, anthropic.CredentialsError) or is_missing_auth(exc):
        return LLMError(f"No Anthropic credentials{where}: {detail}. Set api_key or api_key_env (e.g. "
                        f"ANTHROPIC_API_KEY) in dreamteam.toml, or run `ant auth login`.{hint}",
                        retryable=False, kind="auth")
    return LLMError(f"Anthropic client error{where}: {detail}", retryable=False, kind="other")


def is_missing_auth(exc: BaseException) -> bool:
    """The SDK raises a plain TypeError at request time when no key / token / ant-auth profile is found."""
    return isinstance(exc, TypeError) and "Could not resolve authentication method" in str(exc)


# --- client -----------------------------------------------------------------------------------

def timeout_seconds(max_tokens: int | None) -> float:
    """Read timeout of a non-streaming request with this output limit (~35 tokens/s for the whole budget)."""
    return max(600.0, 3600.0 * (max_tokens or 0) / 128_000)


def _digest(msg: dict) -> str:
    return hashlib.sha256(json.dumps(msg, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _has_thinking(messages: list[dict], start: int) -> bool:
    for msg in messages[start:]:
        raw = msg.get("raw") if msg.get("role") == "assistant" else None
        if isinstance(raw, dict) and raw.get("provider") == "anthropic":
            if any(isinstance(b, dict) and b.get("type") in THINKING_TYPES for b in raw.get("content") or []):
                return True
    return False


class AnthropicLLM:
    """LLMClient over the Anthropic Messages API (non-streaming)."""

    def __init__(self, profile: ModelProfile, *, http_client: Any = None, max_retries: int = 3,
                 client: anthropic.AsyncAnthropic | None = None) -> None:
        self.profile = profile
        self.client = client or self._make_client(http_client, max_retries)
        self.betas_enabled = True
        # Thinking blocks of history[:_strip_before] are dropped after a signature 400; the anchor
        # (digest of the first message) detects a replaced (compacted) history.
        self._strip_before = 0
        self._strip_anchor: str | None = None

    async def aclose(self) -> None:
        """Release the HTTP connection pool."""
        try:
            await self.client.close()
        except Exception:  # closing must never fail a shutdown
            pass

    def _make_client(self, http_client: Any, max_retries: int) -> anthropic.AsyncAnthropic:
        # A non-default timeout also disables the SDK's "streaming required" guard for big max_tokens.
        seconds = timeout_seconds(self.profile.max_tokens)
        kwargs: dict[str, Any] = {"max_retries": max_retries, "timeout": anthropic.Timeout(seconds, connect=10.0)}
        key = self.profile.resolve_api_key()
        if key:
            kwargs["api_key"] = key
        if self.profile.base_url:
            kwargs["base_url"] = self.profile.base_url
        if http_client is not None:
            kwargs["http_client"] = http_client
        try:
            return anthropic.AsyncAnthropic(**kwargs)
        except anthropic.CredentialsError as exc:  # e.g. ANTHROPIC_PROFILE names a missing ant-auth profile
            raise map_error(exc, self.profile) from exc

    @property
    def uses_betas(self) -> bool:
        return self.betas_enabled and bool(beta_names(self.profile))

    def _strip_boundary(self, messages: list[dict]) -> int:
        if self._strip_before and messages and len(messages) >= self._strip_before \
                and _digest(messages[0]) == self._strip_anchor:
            return self._strip_before
        self._strip_before, self._strip_anchor = 0, None
        return 0

    def _strip_all(self, messages: list[dict]) -> None:
        self._strip_before = len(messages)
        self._strip_anchor = _digest(messages[0]) if messages else None

    async def _send(self, params: dict, use_betas: bool) -> Any:
        if use_betas:
            return await self.client.beta.messages.create(**params)
        return await self.client.messages.create(**params)

    async def complete(self, system: str, messages: list[dict], tools: list[dict], *,
                       max_tokens: int | None = None) -> LLMResponse:
        degraded = stripped = False
        while True:
            use_betas = self.uses_betas
            boundary = self._strip_boundary(messages)
            params = build_request(self.profile, system, messages, tools, use_betas=use_betas,
                                   strip_thinking_before=boundary, max_tokens=max_tokens)
            if max_tokens and max_tokens > self.profile.max_tokens:  # a raised limit may take longer to write
                params["timeout"] = anthropic.Timeout(timeout_seconds(max_tokens), connect=10.0)
            try:
                response = await self._send(params, use_betas)
            except anthropic.BadRequestError as exc:
                text = api_error_text(exc)
                if not stripped and is_signature_error(text) and _has_thinking(messages, boundary):
                    stripped = True
                    self._strip_all(messages)
                    log.warning("Profile '%s': thinking signature rejected; retrying without earlier thinking "
                                "blocks (%s)", self.profile.name, text)
                    continue
                if use_betas and not degraded and is_beta_rejection(text):
                    degraded = True
                    self.betas_enabled = False
                    log.warning("Profile '%s': the API rejected beta parameters; betas disabled for this "
                                "client (%s)", self.profile.name, text)
                    continue
                raise map_error(exc, self.profile) from exc
            except anthropic.AnthropicError as exc:
                raise map_error(exc, self.profile) from exc
            except json.JSONDecodeError as exc:  # truncated / garbled body
                raise invalid_json_error("The Anthropic API", self.profile, exc) from exc
            except TypeError as exc:
                if not is_missing_auth(exc):
                    raise
                raise map_error(exc, self.profile) from exc
            if isinstance(response, (str, bytes)) or response is None:  # non-JSON 2xx: wrong base_url
                raise bad_body_error("Anthropic Messages API", self.profile, response)
            return parse_response(response)
