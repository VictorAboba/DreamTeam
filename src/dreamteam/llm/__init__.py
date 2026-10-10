"""LLM clients behind one provider-neutral interface (docs/SPEC.md §10)."""

from __future__ import annotations

from typing import Any

from ..config import ModelProfile
from .base import (
    LLMClient,
    LLMError,
    LLMResponse,
    ToolCall,
    Usage,
    cost_of,
    estimate_tokens,
)
from .scripted import ScriptedLLM

ANTHROPIC_PROVIDERS = frozenset({"anthropic"})
OPENAI_PROVIDERS = frozenset({"openai", "openrouter", "ollama", "vllm"})

__all__ = [
    "AnthropicLLM",
    "LLMClient",
    "LLMError",
    "LLMResponse",
    "OpenAILLM",
    "ScriptedLLM",
    "ToolCall",
    "Usage",
    "cost_of",
    "create_llm",
    "estimate_tokens",
]


def create_llm(profile: ModelProfile) -> LLMClient:
    """Build the client for a model profile. SDKs are imported lazily."""
    provider = (profile.provider or "").strip().lower()
    if provider in ANTHROPIC_PROVIDERS:
        from .anthropic_llm import AnthropicLLM

        return AnthropicLLM(profile)
    if provider in OPENAI_PROVIDERS:
        from .openai_llm import OpenAILLM

        return OpenAILLM(profile)
    if provider == "scripted":
        raise ValueError(
            f"Model profile '{profile.name}' uses provider 'scripted', which needs a handler: "
            "pass Swarm(llm_factory=lambda profile, agent: ScriptedLLM(handler, profile)) instead."
        )
    known = ", ".join(sorted(ANTHROPIC_PROVIDERS | OPENAI_PROVIDERS | {"scripted"}))
    raise ValueError(f"Model profile '{profile.name}': unknown provider '{profile.provider}'. Known: {known}.")


def __getattr__(name: str) -> Any:
    # Lazy access to the SDK-backed clients keeps `import dreamteam.llm` light.
    if name == "AnthropicLLM":
        from .anthropic_llm import AnthropicLLM

        return AnthropicLLM
    if name == "OpenAILLM":
        from .openai_llm import OpenAILLM

        return OpenAILLM
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
