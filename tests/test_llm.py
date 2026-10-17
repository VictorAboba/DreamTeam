"""Tests for dreamteam.llm: neutral types, ScriptedLLM, cost accounting, message conversion and the
full Anthropic / OpenAI-compatible request path against mocked HTTP transports (no network)."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any, Callable

import anthropic
import httpx2
import openai
import pytest

from dreamteam.config import ModelProfile
from dreamteam.llm import (
    LLMClient,
    LLMError,
    LLMResponse,
    ScriptedLLM,
    ToolCall,
    Usage,
    cost_of,
    create_llm,
    estimate_tokens,
)
from dreamteam.llm import anthropic_llm as A
from dreamteam.llm import openai_llm as O
from dreamteam.llm.anthropic_llm import AnthropicLLM
from dreamteam.llm.base import api_error_text, parse_arguments
from dreamteam.llm.openai_llm import OpenAILLM

TOOLS = [
    {"name": "board", "description": "Show the task board.",
     "parameters": {"type": "object", "properties": {"status": {"type": "string"}}, "required": []}},
    {"name": "wait", "description": "End the turn.", "parameters": {"type": "object", "properties": {}}},
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "OPENAI_BASE_URL", "OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID"):
        monkeypatch.delenv(var, raising=False)


# --- mock transport helpers -------------------------------------------------------------------

class Recorder:
    """httpx2 MockTransport handler: replays scripted (status, json) responses and records requests.
    The last scripted item repeats; an item may also be a callable(request) -> Response."""

    def __init__(self, *items: Any) -> None:
        self.items = list(items)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        item = self.items.pop(0) if len(self.items) > 1 else self.items[0]
        if callable(item):
            return item(request)
        status, payload = item
        return httpx2.Response(status, json=payload)

    def body(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)

    def header(self, name: str, index: int = -1) -> str | None:
        return self.requests[index].headers.get(name)


def make_anthropic(recorder: Recorder, **profile_kw: Any) -> AnthropicLLM:
    profile = ModelProfile(api_key="test-key", **profile_kw)
    http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(recorder))
    return AnthropicLLM(profile, http_client=http, max_retries=0)


def make_openai(recorder: Recorder, **profile_kw: Any) -> OpenAILLM:
    profile_kw.setdefault("provider", "openai")
    profile_kw.setdefault("model", "gpt-test")
    profile = ModelProfile(api_key="test-key", **profile_kw)
    http = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(recorder))
    return OpenAILLM(profile, http_client=http, max_retries=0)


def a_msg(content: list[dict], stop_reason: str = "end_turn", *, model: str = "claude-opus-5-5",
          usage: dict | None = None, stop_details: dict | None = None) -> tuple[int, dict]:
    return 200, {
        "id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": content,
        "stop_reason": stop_reason, "stop_sequence": None, "stop_details": stop_details,
        "usage": usage or {"input_tokens": 12, "output_tokens": 7, "cache_read_input_tokens": 100,
                           "cache_creation_input_tokens": 20},
    }


def a_error(status: int, message: str, etype: str = "invalid_request_error") -> tuple[int, dict]:
    return status, {"type": "error", "error": {"type": etype, "message": message}}


def o_msg(message: dict, finish_reason: str = "stop", *, usage: dict | None = None,
          model: str = "gpt-test") -> tuple[int, dict]:
    return 200, {
        "id": "chatcmpl-test", "object": "chat.completion", "created": 1, "model": model,
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": {"role": "assistant", **message}}],
        "usage": usage or {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55},
    }


def o_error(status: int, message: str, code: str | None = None) -> tuple[int, dict]:
    return status, {"error": {"message": message, "type": "invalid_request_error", "code": code}}


def raising(exc_factory: Callable[[httpx2.Request], Exception]) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise exc_factory(request)
    return handler


THINK = {"type": "thinking", "thinking": "Let me look at the board.", "signature": "sig-1"}
TOOL_USE = {"type": "tool_use", "id": "toolu_01", "name": "board", "input": {"status": "todo"}}


def anthropic_turn(text: str = "", blocks: list[dict] | None = None, calls: list[dict] | None = None) -> dict:
    return {"role": "assistant", "content": text, "tool_calls": calls or [],
            "raw": {"provider": "anthropic", "model": "claude-opus-5-5", "content": blocks or []}}


# --- base -------------------------------------------------------------------------------------

def test_estimate_tokens():
    assert estimate_tokens("") == 1  # '""' -> 2 chars
    assert estimate_tokens("привет") == 3  # 8 chars, not escaped
    assert estimate_tokens({"a": [1, 2]}) == 4  # '{"a": [1, 2]}' = 13 chars
    assert estimate_tokens({"x": object()}) > 0  # non-serializable values do not crash


def test_cost_of_uses_provider_cost_first():
    profile = ModelProfile(input_price=1.0, output_price=2.0)
    assert cost_of(profile, Usage(input_tokens=10**6, cost_usd=0.5)) == 0.5
    assert cost_of(profile, Usage(input_tokens=10**6, cost_usd=0.0)) == 0.0


def test_cost_of_prices():
    profile = ModelProfile(model="custom", input_price=3.0, output_price=15.0,
                           cache_read_price=0.3, cache_write_price=3.75)
    usage = Usage(input_tokens=1_000_000, output_tokens=200_000, cache_read_tokens=2_000_000,
                  cache_write_tokens=400_000)
    assert cost_of(profile, usage) == pytest.approx(3.0 + 3.0 + 0.6 + 1.5)
    # known model prices; the response model (e.g. after a fallback) wins
    opus = ModelProfile(model="claude-opus-5-5")
    assert cost_of(opus, Usage(input_tokens=1_000_000)) == pytest.approx(4.0)
    assert cost_of(opus, Usage(output_tokens=1_000_000), "claude-haiku-5-5") == pytest.approx(0.5)
    assert cost_of(ModelProfile(model="unknown-model"), Usage(input_tokens=10**6, output_tokens=10**6)) == 0.0


def test_usage_and_response_helpers():
    usage = Usage(input_tokens=5, cache_read_tokens=100, cache_write_tokens=20, output_tokens=3)
    assert usage.prompt_tokens == 125
    resp = LLMResponse(text="hi", tool_calls=[ToolCall("c1", "board", {"status": "todo"})], usage=usage,
                       stop_reason="tool_use", model="m", raw={"provider": "anthropic"})
    assert resp.to_message() == {"role": "assistant", "content": "hi",
                                 "tool_calls": [{"id": "c1", "name": "board", "arguments": {"status": "todo"}}],
                                 "raw": {"provider": "anthropic"}}
    assert resp.thinking == ""


def test_llm_error():
    err = LLMError("boom", retryable=True, kind="rate_limit")
    assert (err.message, err.retryable, err.kind, str(err)) == ("boom", True, "rate_limit", "boom")
    assert LLMError("x", kind="weird").kind == "other"
    assert LLMError("x").retryable is False


def test_parse_arguments():
    assert parse_arguments({"a": 1}) == ({"a": 1}, None)
    assert parse_arguments('{"a": "б"}') == ({"a": "б"}, None)
    assert parse_arguments("") == ({}, None)
    assert parse_arguments(None) == ({}, None)
    args, err = parse_arguments('{"a": ')
    assert args == {} and "Invalid JSON" in err
    args, err = parse_arguments("[1, 2]")
    assert args == {} and "JSON object" in err
    assert parse_arguments(5)[1] is not None


def test_api_error_text():
    class Fake(Exception):
        status_code = 400
        body = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad thing"}}
    assert api_error_text(Fake()) == "400 invalid_request_error: bad thing"
    assert api_error_text(ValueError("plain")) == "plain"


def test_api_error_text_gateway_upstream_error():
    # OpenRouter: numeric code repeating the status, the real error only in metadata.raw
    class Gateway(Exception):
        status_code = 400
        body = {"message": "Provider returned error", "code": 400,
                "metadata": {"raw": '{"error":"tool_use ids must be unique"}', "provider_name": "Anthropic"}}
    assert api_error_text(Gateway()) == ('400: Provider returned error '
                                         '[Anthropic: {"error":"tool_use ids must be unique"}]')


# --- ScriptedLLM ------------------------------------------------------------------------------

async def test_scripted_str_and_sync_handler():
    llm = ScriptedLLM(lambda system, messages, tools: f"echo:{messages[-1]['content']}")
    assert isinstance(llm, LLMClient)
    resp = await llm.complete("sys", [{"role": "user", "content": "hi"}], TOOLS)
    assert (resp.text, resp.tool_calls, resp.stop_reason, resp.model) == ("echo:hi", [], "end_turn", "scripted")
    assert resp.usage.input_tokens == estimate_tokens(["sys", [{"role": "user", "content": "hi"}], TOOLS])
    assert resp.usage.output_tokens > 0 and resp.raw is None
    assert llm.calls[0]["system"] == "sys" and llm.calls[0]["tools"] == TOOLS
    assert cost_of(llm.profile, resp.usage) == 0.0


async def test_scripted_async_dict_shorthand():
    async def handler(system, messages, tools):
        return {"text": "working", "thinking": "plan",
                "tool_calls": [{"name": "board", "arguments": {"status": "todo"}},
                               {"name": "wait", "arguments": '{"reason": "done"}', "id": "my-id"},
                               {"name": "board", "arguments": "{oops"}]}

    llm = ScriptedLLM(handler)
    resp = await llm.complete("s", [{"role": "user", "content": "go"}], [])
    assert resp.stop_reason == "tool_use" and resp.text == "working" and resp.thinking == "plan"
    first, second, third = resp.tool_calls
    assert (first.id, first.name, first.arguments) == ("call_1_0", "board", {"status": "todo"})
    assert (second.id, second.arguments) == ("my-id", {"reason": "done"})
    assert third.arguments == {} and "Invalid JSON" in third.parse_error
    resp2 = await llm.complete("s", [{"role": "user", "content": "go"}], [])
    assert resp2.tool_calls[0].id == "call_2_0"  # unique across calls


async def test_scripted_passthrough_none_and_errors():
    ready = LLMResponse("x", [], Usage(1, 2), "end_turn", "m")
    assert await ScriptedLLM(lambda *a: ready).complete("", [], []) is ready
    empty = await ScriptedLLM(lambda *a: None).complete("", [], [])
    assert empty.text == "" and empty.stop_reason == "end_turn"
    with pytest.raises(TypeError):
        await ScriptedLLM(lambda *a: 42).complete("", [], [])
    with pytest.raises(TypeError):
        await ScriptedLLM(lambda *a: {"tool_calls": [{"arguments": {}}]}).complete("", [], [])

    def failing(*a):
        raise LLMError("scripted failure", retryable=True, kind="server")
    with pytest.raises(LLMError) as info:
        await ScriptedLLM(failing).complete("", [], [])
    assert info.value.retryable


async def test_scripted_records_deep_copies_and_explicit_usage():
    history = [{"role": "user", "content": "a"}]
    llm = ScriptedLLM(lambda *a: {"text": "ok", "usage": {"input_tokens": 7, "cost_usd": 0.25},
                                  "stop_reason": "max_tokens", "model": "fake-1"})
    resp = await llm.complete("s", history, [])
    history.append({"role": "user", "content": "b"})
    assert llm.calls[0]["messages"] == [{"role": "user", "content": "a"}]
    assert resp.usage == Usage(input_tokens=7, cost_usd=0.25)
    assert (resp.stop_reason, resp.model) == ("max_tokens", "fake-1")
    custom = ModelProfile(name="p", provider="scripted", model="mine")
    assert (await ScriptedLLM(lambda *a: "x", custom).complete("", [], [])).model == "mine"


async def test_scripted_sequence():
    llm = ScriptedLLM.sequence(["one", {"text": "two"}], default="idle")
    texts = [(await llm.complete("", [], [])).text for _ in range(4)]
    assert texts == ["one", "two", "idle", "idle"]


async def test_scripted_concurrent_calls_get_unique_tool_ids():
    # regression: the call number was read after the handler's await, so overlapping calls shared ids
    gate = asyncio.Event()

    async def handler(system, messages, tools):
        await gate.wait()
        return {"tool_calls": [{"name": "wait", "arguments": {}}]}

    llm = ScriptedLLM(handler)
    tasks = [asyncio.create_task(llm.complete("", [], [])) for _ in range(3)]
    while len(llm.calls) < 3:
        await asyncio.sleep(0)
    gate.set()
    ids = [r.tool_calls[0].id for r in await asyncio.gather(*tasks)]
    assert sorted(ids) == ["call_1_0", "call_2_0", "call_3_0"]


# --- create_llm -------------------------------------------------------------------------------

def test_create_llm_providers():
    llm = create_llm(ModelProfile(provider="anthropic", api_key="k", base_url="https://proxy.example/anthropic"))
    assert isinstance(llm, AnthropicLLM) and isinstance(llm, LLMClient)
    assert str(llm.client.base_url).startswith("https://proxy.example/anthropic")
    assert llm.client.api_key == "k" and llm.client.max_retries == 3
    for provider in ("openai", "openrouter", "ollama", "vllm", "OpenAI"):
        client = create_llm(ModelProfile(provider=provider, model="m", base_url="http://localhost:11434/v1"))
        assert isinstance(client, OpenAILLM)
        assert client.client.api_key == "not-needed" and client.client.max_retries == 3
        assert str(client.client.base_url).startswith("http://localhost:11434/v1")


def test_create_llm_api_keys(monkeypatch):
    monkeypatch.setenv("MY_KEY", "from-env")
    assert create_llm(ModelProfile(api_key_env="MY_KEY")).client.api_key == "from-env"
    monkeypatch.setenv("OPENAI_API_KEY", "sk-official")
    assert create_llm(ModelProfile(provider="openai", model="m")).client.api_key == "sk-official"
    # never leak an OpenAI key to a third-party endpoint
    other = create_llm(ModelProfile(provider="openai", model="m", base_url="https://openrouter.ai/api/v1"))
    assert other.client.api_key == "not-needed"


def test_create_llm_errors():
    with pytest.raises(ValueError, match="llm_factory"):
        create_llm(ModelProfile(provider="scripted"))
    with pytest.raises(ValueError, match="unknown provider"):
        create_llm(ModelProfile(provider="gemini"))


# --- Anthropic: conversion (pure) -------------------------------------------------------------

def test_anthropic_convert_tools():
    assert A.convert_tools(TOOLS)[0] == {"name": "board", "description": "Show the task board.",
                                        "input_schema": TOOLS[0]["parameters"]}
    assert A.convert_tools([{"name": "x"}])[0]["input_schema"] == {"type": "object", "properties": {}}


def test_anthropic_merges_user_side_items_tool_results_first():
    history = [
        {"role": "user", "content": "wake up"},
        anthropic_turn("Checking.", [THINK, {"type": "text", "text": "Checking."}, TOOL_USE],
                       [{"id": "toolu_01", "name": "board", "arguments": {"status": "todo"}}]),
        {"role": "user", "content": "new message arrived"},
        {"role": "tool", "tool_call_id": "toolu_01", "name": "board", "content": "no tasks", "is_error": False},
        {"role": "tool", "tool_call_id": "toolu_02", "name": "wait", "content": "bad", "is_error": True},
    ]
    out = A.convert_messages(history)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert out[0]["content"] == [{"type": "text", "text": "wake up"}]
    assert out[1]["content"] == [THINK, {"type": "text", "text": "Checking."}, TOOL_USE]  # verbatim
    assert out[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "toolu_01", "content": "no tasks"},
        {"type": "tool_result", "tool_use_id": "toolu_02", "content": "bad", "is_error": True},
        {"type": "text", "text": "new message arrived"},
    ]


def test_anthropic_non_anthropic_assistant_becomes_text_and_tool_use():
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Sure", "raw": None,
         "tool_calls": [{"id": "call.abc:1", "name": "board", "arguments": {"status": "done"}},
                        {"id": "call_2", "name": "wait", "arguments": '{"reason": "x"}'}]},
        {"role": "tool", "tool_call_id": "call.abc:1", "name": "board", "content": "", "is_error": False},
        {"role": "tool", "tool_call_id": "call_2", "name": "wait", "content": "ok", "is_error": False},
        {"role": "assistant", "content": "", "tool_calls": [], "raw": {"provider": "openai"}},
    ]
    out = A.convert_messages(history)
    assert out[1]["content"] == [
        {"type": "text", "text": "Sure"},
        {"type": "tool_use", "id": "call_abc_1", "name": "board", "input": {"status": "done"}},
        {"type": "tool_use", "id": "call_2", "name": "wait", "input": {"reason": "x"}},
    ]
    assert out[2]["content"][0] == {"type": "tool_result", "tool_use_id": "call_abc_1", "content": "(no output)"}
    assert out[3] == {"role": "assistant", "content": [{"type": "text", "text": "(no output)"}]}


def test_anthropic_first_message_is_user_and_placeholders():
    out = A.convert_messages([
        {"role": "assistant", "content": "I start", "tool_calls": []},
        {"role": "user", "content": "   "},
        {"role": "system", "content": "ignored unknown role"},
    ])
    assert out[0] == {"role": "user", "content": [{"type": "text", "text": A.START_TEXT}]}
    assert out[1]["role"] == "assistant"
    assert out[2] == {"role": "user", "content": [{"type": "text", "text": A.EMPTY_USER_TEXT}]}
    assert A.convert_messages([]) == []


def test_anthropic_conversion_is_deterministic_append_only_and_pure():
    history: list[dict] = [{"role": "user", "content": "start"}]
    snapshots: list[list[dict]] = []
    turns = [
        anthropic_turn("", [THINK, TOOL_USE], [{"id": "toolu_01", "name": "board", "arguments": {}}]),
        {"role": "tool", "tool_call_id": "toolu_01", "name": "board", "content": "1 task", "is_error": False},
        {"role": "user", "content": "message from Bob"},
        anthropic_turn("Done", [{"type": "thinking", "thinking": "", "signature": "s2"},
                                {"type": "text", "text": "Done"}]),
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "plain", "tool_calls": []},
        {"role": "user", "content": "again"},
    ]
    for item in turns:
        history.append(item)
        before = copy.deepcopy(history)
        rendered = A.convert_messages(history)
        assert history == before  # input untouched
        assert json.dumps(rendered) == json.dumps(A.convert_messages(copy.deepcopy(history)))
        snapshots.append(rendered)
    for older, newer in zip(snapshots, snapshots[1:]):
        # every message except a still-open trailing user run is byte-identical later on
        stable = older if older[-1]["role"] == "assistant" else older[:-1]
        assert json.dumps(newer[: len(stable)]) == json.dumps(stable)


def test_anthropic_strip_thinking_and_fallback_blocks():
    redacted = {"type": "redacted_thinking", "data": "xxx"}
    fallback = {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"},
                "trigger": {"type": "refusal", "category": "cyber"}}
    history = [
        {"role": "user", "content": "a"},
        anthropic_turn("x", [THINK, redacted, {"type": "text", "text": "x"}]),
        {"role": "user", "content": "b"},
        anthropic_turn("", [THINK]),
        {"role": "user", "content": "c"},
        anthropic_turn("y", [THINK, fallback, {"type": "text", "text": "y"}]),
        {"role": "user", "content": "d"},
    ]
    out = A.convert_messages(history, strip_thinking_before=4, fallback_blocks=False)
    assert out[1]["content"] == [{"type": "text", "text": "x"}]
    assert out[3]["content"] == [{"type": "text", "text": "(no output)"}]  # only thinking -> neutral fallback
    assert out[5]["content"] == [THINK, {"type": "text", "text": "y"}]  # after the boundary: kept
    full = A.convert_messages(history)
    assert full[5]["content"] == [THINK, fallback, {"type": "text", "text": "y"}]
    assert full[1]["content"][1] == redacted


def test_anthropic_build_request_defaults():
    profile = ModelProfile()
    params = A.build_request(profile, "SYS", [{"role": "user", "content": "hi"}], TOOLS, use_betas=False)
    assert params == {
        "model": "claude-opus-5-5",
        "max_tokens": 16000,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "cache_control": {"type": "ephemeral"},
        "system": "SYS",
        "tools": A.convert_tools(TOOLS),
        "thinking": {"type": "adaptive", "display": "summarized"},
    }
    bare = A.build_request(ModelProfile(thinking="off"), "", [], [], use_betas=False)
    assert set(bare) == {"model", "max_tokens", "messages", "cache_control"}
    no_display = A.build_request(ModelProfile(thinking_display=None), "s", [], [], use_betas=False)
    assert no_display["thinking"] == {"type": "adaptive"}
    between = A.build_request(ModelProfile(thinking="between_tools", thinking_mismatch="drop_block",
                                           fallbacks="default"), "s", [], [], use_betas=True)
    assert between["thinking"] == {"type": "between_tools"}
    assert between["betas"] == [A.FALLBACK_BETA]


def test_anthropic_build_request_options_and_betas():
    profile = ModelProfile(effort="high", extra={"metadata": {"user_id": "u"}}, fallbacks="default",
                           thinking_mismatch="drop_block", max_tokens=8000, model="claude-sonnet-5-5")
    params = A.build_request(profile, "s", [], [], use_betas=True)
    assert params["output_config"] == {"effort": "high"}
    assert params["extra_body"] == {"metadata": {"user_id": "u"}}
    assert params["betas"] == [A.FALLBACK_BETA, A.THINKING_BINDING_BETA]
    assert params["fallbacks"] == "default"
    assert params["thinking"] == {"type": "adaptive", "display": "summarized",
                                  "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert (params["model"], params["max_tokens"]) == ("claude-sonnet-5-5", 8000)
    plain = A.build_request(profile, "s", [], [], use_betas=False)
    assert "betas" not in plain and "fallbacks" not in plain
    assert "block_binding" not in plain["thinking"]
    for forbidden in ("temperature", "top_p", "tool_choice", "budget_tokens"):
        assert forbidden not in json.dumps(params) and forbidden not in json.dumps(plain)
    listed = A.build_request(ModelProfile(fallbacks="claude-opus-5, claude-opus-4-8"), "s", [], [], use_betas=True)
    assert listed["fallbacks"] == [{"model": "claude-opus-5"}, {"model": "claude-opus-4-8"}]
    # regression: the list form used an older beta; the SDK names one header for `fallbacks:`
    assert listed["betas"] == ["server-side-fallback-2026-07-01"]
    assert A.beta_names(ModelProfile(fallbacks="default")) == ["server-side-fallback-2026-07-01"]
    assert A.beta_names(ModelProfile()) == []


def test_anthropic_thinking_updates_display_needs_beta():
    profile = ModelProfile(thinking_display="updates")
    assert A.beta_names(profile) == [A.THINKING_UPDATES_BETA]
    on_beta = A.build_request(profile, "s", [], [], use_betas=True)
    assert on_beta["thinking"] == {"type": "adaptive", "display": "updates"}
    assert on_beta["betas"] == [A.THINKING_UPDATES_BETA] and "fallbacks" not in on_beta
    degraded = A.build_request(profile, "s", [], [], use_betas=False)
    assert degraded["thinking"] == {"type": "adaptive"} and "betas" not in degraded
    assert A.beta_names(ModelProfile(thinking="off", thinking_display="updates", thinking_mismatch="error")) == []


def test_anthropic_error_classifiers():
    sig = ("messages.5.content.0: Invalid `signature` in `thinking` block. The block is bound to a different "
           "conversation. Remove the block, or set `thinking.block_binding.prefix_mismatch_behavior` to "
           "\"drop_block\".")
    assert A.is_signature_error(sig) and not A.is_beta_rejection(sig)
    assert A.is_beta_rejection("fallbacks: Extra inputs are not permitted")
    assert A.is_beta_rejection("Unexpected value(s) `foo` for the `anthropic-beta` header.")
    assert not A.is_beta_rejection("messages: roles must alternate")
    err = A.map_error(anthropic.CredentialsError("nothing configured"), ModelProfile(name="main"))
    assert (err.kind, err.retryable) == ("auth", False) and "main" in err.message


# --- Anthropic: full request path -------------------------------------------------------------

async def test_anthropic_request_json_non_beta():
    rec = Recorder(a_msg([{"type": "text", "text": "Hello!"}]))
    llm = make_anthropic(rec, effort="medium", extra={"metadata": {"user_id": "agent-1"}})
    resp = await llm.complete("You are John.", [{"role": "user", "content": "hi"}], TOOLS)
    req = rec.requests[0]
    assert req.method == "POST" and req.url.path.endswith("/v1/messages") and "beta" not in str(req.url)
    assert rec.header("anthropic-beta") is None and rec.header("x-api-key") == "test-key"
    assert rec.body() == {
        "max_tokens": 16000,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "model": "claude-opus-5-5",
        "cache_control": {"type": "ephemeral"},
        "output_config": {"effort": "medium"},
        "system": "You are John.",
        "thinking": {"type": "adaptive", "display": "summarized"},
        "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                  for t in TOOLS],
        "metadata": {"user_id": "agent-1"},
    }
    assert (resp.text, resp.stop_reason, resp.tool_calls, resp.thinking) == ("Hello!", "end_turn", [], "")
    assert resp.usage == Usage(input_tokens=12, output_tokens=7, cache_read_tokens=100, cache_write_tokens=20)
    assert resp.raw == {"provider": "anthropic", "model": "claude-opus-5-5",
                        "content": [{"type": "text", "text": "Hello!"}]}


async def test_anthropic_beta_request_and_tool_use_parsing():
    content = [
        {"type": "thinking", "thinking": "First idea.", "signature": "s1"},
        {"type": "thinking", "thinking": "", "signature": "s2"},
        {"type": "redacted_thinking", "data": "opaque"},
        {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"},
         "trigger": {"type": "refusal", "category": None}},
        {"type": "text", "text": "Part one. ", "citations": None},
        {"type": "text", "text": "Part two."},
        {"type": "thinking", "thinking": "Second idea.", "signature": "s3"},
        {"type": "text", "text": "Now the board."},
        {"type": "tool_use", "id": "toolu_A", "name": "board", "input": {"status": "todo"}},
        {"type": "tool_use", "id": "toolu_B", "name": "wait", "input": {}},
    ]
    rec = Recorder(a_msg(content, "tool_use", model="claude-opus-5",
                         usage={"input_tokens": 3, "output_tokens": 9, "cache_read_input_tokens": None,
                                "cache_creation_input_tokens": None}))
    llm = make_anthropic(rec, fallbacks="default", thinking_mismatch="drop_block")
    resp = await llm.complete("sys", [{"role": "user", "content": "go"}], TOOLS)
    assert rec.requests[0].url.params.get("beta") == "true"
    assert rec.header("anthropic-beta") == f"{A.FALLBACK_BETA},{A.THINKING_BINDING_BETA}"
    body = rec.body()
    assert body["fallbacks"] == "default" and "betas" not in body
    assert body["thinking"] == {"type": "adaptive", "display": "summarized",
                                "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert resp.stop_reason == "tool_use" and resp.model == "claude-opus-5"
    assert resp.text == "Part one. Part two.\n\nNow the board."
    assert resp.thinking == "First idea.\n\nSecond idea."
    assert resp.tool_calls == [ToolCall("toolu_A", "board", {"status": "todo"}), ToolCall("toolu_B", "wait", {})]
    assert resp.usage == Usage(input_tokens=3, output_tokens=9)
    raw_content = resp.raw["content"]
    assert [b["type"] for b in raw_content] == [b["type"] for b in content]
    assert raw_content[3]["from"] == {"model": "claude-opus-5-5"}  # API field names kept
    assert raw_content[4] == {"type": "text", "text": "Part one. "}  # None fields dropped
    assert resp.raw["model"] == "claude-opus-5"


async def test_anthropic_replays_raw_content_with_stable_prefix():
    first = [THINK, {"type": "text", "text": "Looking"}, TOOL_USE]
    rec = Recorder(a_msg(first, "tool_use"), a_msg([{"type": "text", "text": "All done"}]))
    llm = make_anthropic(rec)
    history = [{"role": "user", "content": "wake"}]
    resp = await llm.complete("sys", history, TOOLS)
    history.append(resp.to_message())
    history.append({"role": "tool", "tool_call_id": "toolu_01", "name": "board", "content": "empty",
                    "is_error": False})
    await llm.complete("sys", history, TOOLS)
    one, two = rec.body(0), rec.body(1)
    assert two["messages"][0] == one["messages"][0]
    assert two["messages"][1] == {"role": "assistant", "content": first}
    assert two["messages"][2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_01", "content": "empty"}]}
    for key in ("system", "tools", "thinking", "cache_control"):
        assert one[key] == two[key]


async def test_anthropic_refusal():
    details = {"type": "refusal", "category": "cyber", "explanation": "Looks like malware development."}
    rec = Recorder(a_msg([{"type": "text", "text": "Sure, here"}, TOOL_USE], "refusal", stop_details=details))
    resp = await make_anthropic(rec).complete("s", [{"role": "user", "content": "x"}], TOOLS)
    assert resp.stop_reason == "refusal" and resp.tool_calls == []
    assert "cyber" in resp.text and "Looks like malware development." in resp.text
    assert resp.text.startswith("Sure, here")
    assert [b["type"] for b in resp.raw["content"]] == ["text"]  # unpaired tool_use dropped
    rec2 = Recorder(a_msg([], "refusal", stop_details=None))
    resp2 = await make_anthropic(rec2).complete("s", [{"role": "user", "content": "x"}], [])
    assert resp2.stop_reason == "refusal" and "unspecified" in resp2.text


@pytest.mark.parametrize("api_stop, expected", [
    ("end_turn", "end_turn"), ("stop_sequence", "end_turn"), ("max_tokens", "max_tokens"),
    ("model_context_window_exceeded", "max_tokens"), ("pause_turn", "other"), (None, "other"),
])
async def test_anthropic_stop_reason_mapping(api_stop, expected):
    rec = Recorder(a_msg([{"type": "text", "text": "t"}], api_stop))
    assert (await make_anthropic(rec).complete("s", [{"role": "user", "content": "x"}], [])).stop_reason == expected


async def test_anthropic_beta_degrade_retries_once_and_sticks():
    rec = Recorder(a_error(400, "fallbacks: Extra inputs are not permitted"),
                   a_msg([{"type": "text", "text": "ok"}]))
    llm = make_anthropic(rec, fallbacks="default", thinking_mismatch="drop_block")
    resp = await llm.complete("s", [{"role": "user", "content": "x"}], [])
    assert resp.text == "ok" and len(rec.requests) == 2
    assert rec.header("anthropic-beta", 0) and rec.header("anthropic-beta", 1) is None
    retry = rec.body(1)
    assert "fallbacks" not in retry and retry["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert llm.betas_enabled is False and llm.uses_betas is False
    await llm.complete("s", [{"role": "user", "content": "y"}], [])
    assert len(rec.requests) == 3 and rec.header("anthropic-beta") is None


async def test_anthropic_beta_degrade_only_once():
    rec = Recorder(a_error(400, "Unexpected value for the `anthropic-beta` header"))
    llm = make_anthropic(rec, fallbacks="default")
    with pytest.raises(LLMError) as info:
        await llm.complete("s", [{"role": "user", "content": "x"}], [])
    assert len(rec.requests) == 2
    assert (info.value.kind, info.value.retryable) == ("bad_request", False)
    assert "anthropic-beta" in info.value.message


async def test_anthropic_non_beta_400_is_not_retried():
    rec = Recorder(a_error(400, "fallbacks: unexpected"))
    with pytest.raises(LLMError):
        await make_anthropic(rec).complete("s", [{"role": "user", "content": "x"}], [])
    assert len(rec.requests) == 1


SIGNATURE_ERROR = ("messages.1.content.0: Invalid `signature` in `thinking` block. The block is bound to a "
                   "different conversation. Remove the block, or set "
                   "`thinking.block_binding.prefix_mismatch_behavior` to \"drop_block\".")


async def test_anthropic_signature_strip_retry_and_persistent_boundary():
    redacted = {"type": "redacted_thinking", "data": "zzz"}
    history = [
        {"role": "user", "content": "start"},
        anthropic_turn("", [THINK, redacted, TOOL_USE], [{"id": "toolu_01", "name": "board", "arguments": {}}]),
        {"role": "tool", "tool_call_id": "toolu_01", "name": "board", "content": "ok", "is_error": False},
    ]
    fresh = [{"type": "thinking", "thinking": "new", "signature": "fresh"}, {"type": "text", "text": "Done"}]
    rec = Recorder(a_error(400, SIGNATURE_ERROR), a_msg(fresh), a_msg([{"type": "text", "text": "again"}]))
    llm = make_anthropic(rec, fallbacks="default")  # on the beta path the message mentions block_binding
    resp = await llm.complete("s", history, [])
    assert resp.text == "Done" and len(rec.requests) == 2
    assert rec.body(0)["messages"][1]["content"] == [THINK, redacted, TOOL_USE]
    assert rec.body(1)["messages"][1]["content"] == [TOOL_USE]
    assert llm.betas_enabled  # a signature error is not a beta rejection

    history += [resp.to_message(), {"role": "user", "content": "more"}]
    await llm.complete("s", history, [])
    later = rec.body(2)["messages"]
    assert later[1]["content"] == [TOOL_USE]  # old blocks stay stripped -> stable prefix
    assert later[3]["content"] == fresh  # new thinking is replayed

    # a replaced (compacted) history resets the boundary
    compacted = [{"role": "user", "content": "summary"}, anthropic_turn("", [THINK, {"type": "text", "text": "k"}]),
                 {"role": "user", "content": "go on"}, {"role": "user", "content": "x"},
                 {"role": "user", "content": "y"}, {"role": "user", "content": "z"}]
    await llm.complete("s", compacted, [])
    assert rec.body()["messages"][1]["content"][0] == THINK


async def test_anthropic_signature_error_without_thinking_raises():
    rec = Recorder(a_error(400, SIGNATURE_ERROR))
    with pytest.raises(LLMError) as info:
        await make_anthropic(rec).complete("s", [{"role": "user", "content": "x"}], [])
    assert len(rec.requests) == 1 and info.value.kind == "bad_request"


async def test_anthropic_signature_strip_only_once():
    history = [{"role": "user", "content": "a"}, anthropic_turn("t", [THINK, {"type": "text", "text": "t"}]),
               {"role": "user", "content": "b"}]
    rec = Recorder(a_error(400, SIGNATURE_ERROR))
    with pytest.raises(LLMError):
        await make_anthropic(rec).complete("s", history, [])
    assert len(rec.requests) == 2


@pytest.mark.parametrize("status, etype, kind, retryable", [
    (429, "rate_limit_error", "rate_limit", True),
    (401, "authentication_error", "auth", False),
    (403, "permission_error", "auth", False),
    (404, "not_found_error", "bad_request", False),
    (400, "invalid_request_error", "bad_request", False),
    (413, "request_too_large", "bad_request", False),
    (500, "api_error", "server", True),
    (503, "api_error", "server", True),
    (529, "overloaded_error", "server", True),
    (418, "teapot", "other", False),
])
async def test_anthropic_status_error_mapping(status, etype, kind, retryable):
    rec = Recorder(a_error(status, f"failure {status}", etype))
    with pytest.raises(LLMError) as info:
        await make_anthropic(rec, name="main").complete("s", [{"role": "user", "content": "x"}], [])
    err = info.value
    assert (err.kind, err.retryable) == (kind, retryable)
    assert f"failure {status}" in err.message and "main" in err.message
    assert isinstance(err.__cause__, anthropic.APIError)


@pytest.mark.parametrize("exc_factory", [
    lambda req: httpx2.ConnectError("connection refused", request=req),
    lambda req: httpx2.ReadTimeout("timed out", request=req),
])
async def test_anthropic_connection_errors(exc_factory):
    rec = Recorder(raising(exc_factory))
    with pytest.raises(LLMError) as info:
        await make_anthropic(rec).complete("s", [{"role": "user", "content": "x"}], [])
    assert (info.value.kind, info.value.retryable) == ("connection", True)


def _no_ant_auth(monkeypatch, tmp_path) -> None:
    """No env credentials and no ant-auth profile on disk (config dir and home point to an empty dir)."""
    for var in ("ANTHROPIC_PROFILE", "ANTHROPIC_CONFIG_DIR", "ANTHROPIC_FEDERATION_RULE_ID",
                "ANTHROPIC_ORGANIZATION_ID", "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE",
                "DT_TEST_MISSING_KEY"):
        monkeypatch.delenv(var, raising=False)
    for var in ("APPDATA", "HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(tmp_path))


async def test_anthropic_missing_credentials_is_an_auth_error(monkeypatch, tmp_path):
    # regression: the SDK raises a bare TypeError at request time, which escaped as a non-LLMError
    _no_ant_auth(monkeypatch, tmp_path)
    rec = Recorder(a_msg([{"type": "text", "text": "never"}]))
    http = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(rec))
    llm = AnthropicLLM(ModelProfile(name="main", api_key_env="DT_TEST_MISSING_KEY"), http_client=http, max_retries=0)
    with pytest.raises(LLMError) as info:
        await llm.complete("s", [{"role": "user", "content": "x"}], [])
    err = info.value
    assert (err.kind, err.retryable) == ("auth", False) and rec.requests == []
    assert "DT_TEST_MISSING_KEY" in err.message and "ant auth login" in err.message and "'main'" in err.message


def test_anthropic_broken_ant_auth_profile_is_an_auth_error(monkeypatch, tmp_path):
    # an explicitly selected config dir without a profile fails in the SDK constructor
    _no_ant_auth(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path / "nowhere"))
    with pytest.raises(LLMError) as info:
        create_llm(ModelProfile(name="main"))
    assert (info.value.kind, info.value.retryable) == ("auth", False)
    assert isinstance(info.value.__cause__, anthropic.CredentialsError)


async def test_anthropic_large_max_tokens_needs_no_streaming():
    # with the SDK's default timeout, max_tokens=128000 raises "Streaming is required" before sending
    rec = Recorder(a_msg([{"type": "text", "text": "ok"}]))
    llm = make_anthropic(rec, max_tokens=128_000)
    assert (await llm.complete("s", [{"role": "user", "content": "x"}], [])).text == "ok"
    assert rec.body()["max_tokens"] == 128_000 and llm.client.timeout.read == 3600


HTML_PAGE = lambda request: httpx2.Response(200, text="<html><body>Ollama is running</body></html>",  # noqa: E731
                                            headers={"content-type": "text/html"})
TRUNCATED_JSON = lambda request: httpx2.Response(200, text='{"id": "x", "cont',  # noqa: E731
                                                 headers={"content-type": "application/json"})


@pytest.mark.parametrize("make", [make_anthropic, make_openai])
async def test_non_json_success_bodies_raise_llm_errors(make):
    # regression: a wrong base_url (web page) leaked AttributeError, a truncated body JSONDecodeError
    with pytest.raises(LLMError) as info:
        await make(Recorder(HTML_PAGE)).complete("s", [{"role": "user", "content": "x"}], [])
    assert (info.value.kind, info.value.retryable) == ("other", False)
    assert "base_url" in info.value.message and "Ollama is running" in info.value.message
    with pytest.raises(LLMError) as info:
        await make(Recorder(TRUNCATED_JSON)).complete("s", [{"role": "user", "content": "x"}], [])
    assert (info.value.kind, info.value.retryable) == ("server", True)


# --- OpenAI-compatible: conversion (pure) -----------------------------------------------------

def test_openai_convert_messages():
    history = [
        {"role": "user", "content": "hi"},
        anthropic_turn("Привет", [THINK, TOOL_USE],
                       [{"id": "toolu_01", "name": "board", "arguments": {"status": "в работе"}}]),
        {"role": "user", "content": "interjection"},
        {"role": "tool", "tool_call_id": "toolu_01", "name": "board", "content": "nope", "is_error": True},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "assistant", "content": "plain", "tool_calls": [], "raw": None},
        {"role": "system", "content": "unknown role is skipped"},
    ]
    out = O.convert_messages("SYS", history)
    assert out == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Привет", "tool_calls": [
            {"id": "toolu_01", "type": "function",
             "function": {"name": "board", "arguments": '{"status": "в работе"}'}}]},
        {"role": "tool", "tool_call_id": "toolu_01", "content": "ERROR: nope"},
        {"role": "user", "content": "interjection"},
        # regression: an empty turn replayed as "" is a permanent 400 for Claude behind OpenRouter
        {"role": "assistant", "content": "(no output)"},
        {"role": "assistant", "content": "plain"},
    ]
    assert O.convert_messages("", [])[:1] == []
    no_text = O.convert_messages("", [{"role": "assistant", "content": " \n",
                                       "tool_calls": [{"id": "c", "name": "wait", "arguments": {}}]}])
    assert no_text[0]["content"] is None and no_text[0]["tool_calls"][0]["function"]["arguments"] == "{}"
    blank = O.convert_messages("", [{"role": "assistant", "content": "  ", "tool_calls": []}])
    assert blank == [{"role": "assistant", "content": "(no output)"}]


def test_openai_build_request():
    official = O.build_request(ModelProfile(provider="openai", model="gpt-x"), "s",
                               [{"role": "user", "content": "q"}], TOOLS)
    assert official == {
        "model": "gpt-x",
        "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}],
        "max_completion_tokens": 16000,
        "tools": O.convert_tools(TOOLS),
        "tool_choice": "auto",
    }
    assert O.convert_tools(TOOLS)[0] == {"type": "function", "function": {
        "name": "board", "description": "Show the task board.", "parameters": TOOLS[0]["parameters"]}}
    local = O.build_request(ModelProfile(provider="openai", base_url="http://localhost:8000/v1", effort="high",
                                         extra={"top_k": 20}, max_tokens=4000), "", [], [])
    assert local == {"model": "claude-opus-5-5", "messages": [], "max_tokens": 4000,
                     "reasoning_effort": "high", "extra_body": {"top_k": 20}}
    explicit = O.build_request(ModelProfile(provider="openai", max_tokens_param="max_tokens"), "", [], [])
    assert explicit["max_tokens"] == 16000 and "max_completion_tokens" not in explicit
    odd = O.build_request(ModelProfile(provider="openai", base_url="https://x.example/v1",
                                       max_tokens_param="num_predict"), "", [], [])
    assert odd["extra_body"] == {"num_predict": 16000} and "max_tokens" not in odd
    assert O.max_tokens_param(ModelProfile(base_url="https://api.openai.com/v1")) == "max_completion_tokens"


# --- OpenAI-compatible: full request path -----------------------------------------------------

async def test_openai_request_and_tool_calls():
    message = {"content": None, "reasoning_content": "thinking out loud", "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "board", "arguments": '{"status": "todo"}'}},
        {"id": "call_2", "type": "function", "function": {"name": "wait", "arguments": "{not json"}},
        {"id": "call_3", "type": "function", "function": {"name": "wait", "arguments": ""}},
    ]}
    usage = {"prompt_tokens": 1000, "completion_tokens": 50, "total_tokens": 1050, "cost": 0.0123,
             "prompt_tokens_details": {"cached_tokens": 600, "cache_write_tokens": 100}}
    rec = Recorder(o_msg(message, "tool_calls", usage=usage, model="gpt-test-2026"))
    llm = make_openai(rec, base_url="https://openrouter.ai/api/v1", effort="low", extra={"provider": {"sort": "price"}})
    resp = await llm.complete("sys", [{"role": "user", "content": "hi"}], TOOLS)
    req = rec.requests[0]
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert rec.header("authorization") == "Bearer test-key"
    assert rec.body() == {
        "model": "gpt-test",
        "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
        "max_tokens": 16000,
        "reasoning_effort": "low",
        "tool_choice": "auto",
        "tools": O.convert_tools(TOOLS),
        "provider": {"sort": "price"},
    }
    assert resp.stop_reason == "tool_use" and resp.text == "" and resp.model == "gpt-test-2026"
    assert resp.thinking == "thinking out loud" and resp.raw is None
    first, second, third = resp.tool_calls
    assert (first.id, first.name, first.arguments, first.parse_error) == ("call_1", "board", {"status": "todo"}, None)
    assert second.arguments == {} and "Invalid JSON" in second.parse_error
    assert third.arguments == {} and third.parse_error is None
    assert resp.usage == Usage(input_tokens=300, output_tokens=50, cache_read_tokens=600, cache_write_tokens=100,
                               cost_usd=0.0123)
    assert cost_of(llm.profile, resp.usage) == pytest.approx(0.0123)


async def test_openai_official_defaults_and_plain_answer():
    rec = Recorder(o_msg({"content": "Hello"}, "stop", usage={"prompt_tokens": 10, "completion_tokens": 2,
                                                              "total_tokens": 12}))
    resp = await make_openai(rec).complete("", [{"role": "user", "content": "hi"}], [])
    body = rec.body()
    assert str(rec.requests[0].url) == "https://api.openai.com/v1/chat/completions"
    assert body == {"model": "gpt-test", "messages": [{"role": "user", "content": "hi"}],
                    "max_completion_tokens": 16000}
    assert (resp.text, resp.stop_reason, resp.tool_calls, resp.thinking) == ("Hello", "end_turn", [], "")
    assert resp.usage == Usage(input_tokens=10, output_tokens=2)


@pytest.mark.parametrize("finish, message, expected", [
    ("length", {"content": "partial"}, "max_tokens"),
    ("content_filter", {"content": None}, "refusal"),
    ("stop", {"content": "x", "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "wait", "arguments": "{}"}}]}, "tool_use"),
    ("function_call", {"content": "x"}, "tool_use"),
])
async def test_openai_finish_reasons(finish, message, expected):
    rec = Recorder(o_msg(message, finish))
    resp = await make_openai(rec).complete("", [{"role": "user", "content": "q"}], [])
    assert resp.stop_reason == expected
    if expected == "refusal":
        assert resp.text and resp.tool_calls == []


async def test_openai_refusal_message():
    rec = Recorder(o_msg({"content": None, "refusal": "I can't help with that.", "tool_calls": [
        {"id": "c", "type": "function", "function": {"name": "wait", "arguments": "{}"}}]}, "stop"))
    resp = await make_openai(rec).complete("", [{"role": "user", "content": "q"}], [])
    assert (resp.stop_reason, resp.text, resp.tool_calls) == ("refusal", "I can't help with that.", [])


async def test_openai_reasoning_field_and_no_usage():
    payload = o_msg({"content": "ok", "reasoning": "short plan"})[1]
    payload["usage"] = None
    resp = await make_openai(Recorder((200, payload))).complete("", [{"role": "user", "content": "q"}], [])
    assert resp.thinking == "short plan" and resp.usage == Usage()


async def test_openai_no_choices():
    payload = o_msg({"content": "x"})[1]
    payload["choices"] = []
    with pytest.raises(LLMError) as info:
        await make_openai(Recorder((200, payload))).complete("", [{"role": "user", "content": "q"}], [])
    assert (info.value.kind, info.value.retryable) == ("server", True)


async def test_openai_error_payloads_with_status_200():
    # regression: the gateway's error text was dropped ("no choices") / a failed generation looked like
    # an empty successful answer that silently ended the agent's turn
    with pytest.raises(LLMError) as info:
        await make_openai(Recorder((200, {"error": {"message": "Upstream timed out", "code": 502}}))).complete(
            "", [{"role": "user", "content": "q"}], [])
    assert (info.value.kind, info.value.retryable) == ("server", True)
    assert "Upstream timed out" in info.value.message
    status, payload = o_msg({"content": ""}, "error")
    payload["choices"][0]["error"] = {"message": "Provider disconnected", "code": 502}
    with pytest.raises(LLMError) as info:
        await make_openai(Recorder((status, payload))).complete("", [{"role": "user", "content": "q"}], [])
    assert (info.value.kind, info.value.retryable) == ("server", True)
    assert "Provider disconnected" in info.value.message


async def test_openai_content_as_list_of_parts():
    # regression: some servers send content as typed parts; a list leaked into LLMResponse.text
    parts = [{"type": "thinking", "thinking": [{"type": "text", "text": "hmm"}]},
             {"type": "text", "text": "Hello "}, {"type": "text", "text": "there"}]
    resp = await make_openai(Recorder(o_msg({"content": parts}))).complete("", [{"role": "user", "content": "q"}], [])
    assert resp.text == "Hello there" and resp.stop_reason == "end_turn"


async def test_openai_missing_key_env_is_named_in_auth_error(monkeypatch):
    monkeypatch.delenv("DT_TEST_MISSING_KEY", raising=False)
    rec = Recorder(o_error(401, "Incorrect API key provided", "invalid_api_key"))
    http = openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(rec))
    llm = OpenAILLM(ModelProfile(name="gpt", provider="openai", model="m", api_key_env="DT_TEST_MISSING_KEY"),
                    http_client=http, max_retries=0)
    with pytest.raises(LLMError) as info:
        await llm.complete("", [{"role": "user", "content": "q"}], [])
    assert info.value.kind == "auth" and "DT_TEST_MISSING_KEY" in info.value.message


@pytest.mark.parametrize("status, code, kind, retryable", [
    (429, "rate_limit_exceeded", "rate_limit", True),
    (429, "insufficient_quota", "rate_limit", False),
    (401, "invalid_api_key", "auth", False),
    (403, None, "auth", False),
    (404, "model_not_found", "bad_request", False),
    (400, None, "bad_request", False),
    (422, None, "bad_request", False),
    (500, None, "server", True),
    (502, None, "server", True),
])
async def test_openai_status_error_mapping(status, code, kind, retryable):
    rec = Recorder(o_error(status, f"failure {status}", code))
    with pytest.raises(LLMError) as info:
        await make_openai(rec).complete("", [{"role": "user", "content": "q"}], [])
    assert (info.value.kind, info.value.retryable) == (kind, retryable)
    assert f"failure {status}" in info.value.message
    assert isinstance(info.value.__cause__, openai.APIError)


async def test_openai_connection_error():
    rec = Recorder(raising(lambda req: httpx2.ConnectError("refused", request=req)))
    with pytest.raises(LLMError) as info:
        await make_openai(rec).complete("", [{"role": "user", "content": "q"}], [])
    assert (info.value.kind, info.value.retryable) == ("connection", True)


# --- per-request output limit (a raised max_tokens after a cut-off reply) ----------------------

def request_read_timeout(rec: Recorder, index: int = -1) -> float | None:
    return (rec.requests[index].extensions.get("timeout") or {}).get("read")


async def test_anthropic_per_request_max_tokens_and_timeout():
    rec = Recorder(a_msg([{"type": "text", "text": "ok"}]))
    llm = make_anthropic(rec, max_tokens=16_000)
    await llm.complete("s", [{"role": "user", "content": "x"}], [])
    assert rec.body()["max_tokens"] == 16_000 and request_read_timeout(rec) == 600
    await llm.complete("s", [{"role": "user", "content": "x"}], [], max_tokens=64_000)
    # a raised limit gets a request timeout of its own (the client's covers only the profile's limit)
    assert rec.body()["max_tokens"] == 64_000 and request_read_timeout(rec) == A.timeout_seconds(64_000) == 1800
    await llm.complete("s", [{"role": "user", "content": "x"}], [], max_tokens=None)
    assert rec.body()["max_tokens"] == 16_000 and request_read_timeout(rec) == 600
    params = A.build_request(ModelProfile(max_tokens=8000), "", [], [], use_betas=False, max_tokens=12_000)
    assert params["max_tokens"] == 12_000


async def test_openai_per_request_max_tokens_and_timeout():
    rec = Recorder(o_msg({"content": "Hello"}))
    llm = make_openai(rec, base_url="http://localhost:8000/v1", max_tokens=8000)
    await llm.complete("", [{"role": "user", "content": "hi"}], [])
    assert rec.body()["max_tokens"] == 8000 and request_read_timeout(rec) == 1600
    await llm.complete("", [{"role": "user", "content": "hi"}], [], max_tokens=16_000)
    assert rec.body()["max_tokens"] == 16_000 and request_read_timeout(rec) == O.timeout_seconds(16_000) == 3200
    official = O.build_request(ModelProfile(provider="openai"), "", [], [], max_tokens=32_000)
    assert official["max_completion_tokens"] == 32_000
    odd_profile = ModelProfile(provider="openai", base_url="https://x.example/v1", max_tokens_param="num_predict",
                               extra={"num_predict": 100})
    odd = O.build_request(odd_profile, "", [], [], max_tokens=32_000)
    assert odd["extra_body"] == {"num_predict": 32_000}  # a raised limit wins over the profile's extra
    plain = O.build_request(odd_profile, "", [], [])
    assert plain["extra_body"] == {"num_predict": 100}


async def test_raised_limit_wins_over_a_limit_key_in_extra():
    # extra_body is merged over the body by the SDK: an old limit there would silently undo the raise
    rec = Recorder(o_msg({"content": "Hello"}))
    llm = make_openai(rec, base_url="http://localhost:8000/v1", max_tokens=1000,
                      extra={"max_tokens": 1000, "max_completion_tokens": 1000, "top_k": 20})
    await llm.complete("", [{"role": "user", "content": "hi"}], [], max_tokens=2000)
    body = rec.body()
    assert body["max_tokens"] == 2000 and body["max_completion_tokens"] == 2000 and body["top_k"] == 20
    await llm.complete("", [{"role": "user", "content": "hi"}], [])
    assert rec.body()["max_tokens"] == 1000  # without a raise the profile's extra stays as it was
    rec = Recorder(a_msg([{"type": "text", "text": "ok"}]))
    llm = make_anthropic(rec, max_tokens=1000, extra={"max_tokens": 1000, "top_k": 20})
    await llm.complete("s", [{"role": "user", "content": "x"}], [], max_tokens=2000)
    assert rec.body()["max_tokens"] == 2000 and rec.body()["top_k"] == 20


async def test_scripted_records_the_request_limit_and_the_keyword_probe():
    from dreamteam.llm.base import accepts_max_tokens

    llm = ScriptedLLM(lambda *a: "ok")
    await llm.complete("s", [], [])
    await llm.complete("s", [], [], max_tokens=4000)
    assert [c["max_tokens"] for c in llm.calls] == [None, 4000]

    class Old:
        async def complete(self, system, messages, tools):
            return None

    class Kw:
        async def complete(self, system, messages, tools, **kwargs):
            return None

    assert accepts_max_tokens(llm) and accepts_max_tokens(Kw()) and not accepts_max_tokens(Old())
    assert not accepts_max_tokens(object())


def test_profile_output_ceiling():
    from dreamteam.config import make_profile

    assert ModelProfile(max_tokens=24_000).output_ceiling == 48_000  # default: twice max_tokens
    assert ModelProfile(max_tokens=24_000, max_tokens_ceiling=64_000).output_ceiling == 64_000
    assert ModelProfile(max_tokens=24_000, max_tokens_ceiling=8000).output_ceiling == 24_000  # never raised
    assert make_profile("p", {"max_tokens_ceiling": 32_000}).max_tokens_ceiling == 32_000
    for bad in (0, -5, "big", 1.5):
        with pytest.raises(ValueError, match="max_tokens_ceiling"):
            make_profile("p", {"max_tokens_ceiling": bad})
