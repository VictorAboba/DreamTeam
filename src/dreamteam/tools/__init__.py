"""Tool registry shared by LLM agents, external MCP agents and the user (UI / hub MCP)."""

from __future__ import annotations

import copy
import difflib
import itertools
import json
import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable

from ..models import USER, ToolError

if TYPE_CHECKING:
    from ..agent import AgentRuntime
    from ..swarm import Swarm

log = logging.getLogger(__name__)

AUDIENCES = ("llm", "external", "user")
_AUDIENCE_NAMES = {"llm": "swarm agents", "external": "external members", "user": "the user"}

# Presentation order of the tool modules. The registry is sorted by (module, definition order) once
# everything is loaded, so the tool list never depends on which module happened to be imported first
# (a reordered tool list would invalidate the LLM prompt cache).
_MODULE_ORDER = ("board_tools", "comm_tools", "file_tools", "job_tools", "mcp_tools", "swarm_tools", "trigger_tools",
                 "trace_tools", "skill_tools")
_seq = itertools.count()


@dataclass
class ToolContext:
    swarm: "Swarm"
    actor: str
    runtime: "AgentRuntime | None" = None
    # the surface the call came through, recorded where the user's changes are audited (skills): "act" (web
    # /api/act: the web UI, the TUI, the console), "mcp" (hub_call), "launcher-mcp"
    via: str = "act"

    @property
    def kind(self) -> str:
        """"user" | "llm" | "external"; "unknown" for a name that is not in the swarm."""
        return self.swarm.kind_of(self.actor) or "unknown"

    @property
    def lang(self) -> str:
        return self.swarm.language_of(self.actor)

    @property
    def is_user(self) -> bool:
        return self.actor == USER

    def has_tool(self, name: str) -> bool:
        """Whether the caller has the tool `name`: an LLM agent has its own frozen tool set (tools/frozen.py),
        which may be older than the registry; other callers see the live registry of their kind. Results that
        point at a tool are tailored with it, so an agent is never told to call a tool it lacks."""
        own = getattr(self.runtime, "tool_names", None)
        if own is not None:
            return name in own
        _load()
        spec = REGISTRY.get(name)
        return spec is not None and self.kind in spec.audience

    @property
    def output_cap(self) -> int:
        """Max chars of a big tool output (shell, MCP results). An LLM agent's runtime cuts every result to
        agents.tool_output_chars for the model itself and traces the full text, so it gets up to the trace
        payload limit; other callers (MCP clients, the UI) receive the result as is, so it is capped here."""
        config = self.swarm.config
        limit = config.agents.tool_output_chars
        if self.runtime is not None:
            tracing = getattr(config, "tracing", None)
            return max(limit, int(getattr(tracing, "max_payload_chars", 0) or 0))
        return limit


Handler = Callable[[ToolContext, dict[str, Any]], Awaitable[str]]


@dataclass
class ToolSpec:
    name: str
    description: dict[str, str]
    parameters: dict[str, Any]
    handler: Handler
    audience: frozenset[str]
    allow_empty: frozenset[str] = frozenset()   # required string args that may be "" (e.g. file content)
    order: tuple[int, int] = (len(_MODULE_ORDER), 0)

    def describe(self, language: str) -> str:
        return self.description.get(language) or self.description["en"]


REGISTRY: dict[str, ToolSpec] = {}


def _module_rank(module: str) -> int:
    leaf = module.rsplit(".", 1)[-1]
    return _MODULE_ORDER.index(leaf) if leaf in _MODULE_ORDER else len(_MODULE_ORDER)


def tool(name: str, en: str, ru: str, properties: dict[str, Any] | None = None,
         required: Iterable[str] = (), audience: Iterable[str] = AUDIENCES,
         allow_empty: Iterable[str] = ()):
    """Register an async handler `(ctx, args) -> str` as a tool."""
    audience = frozenset(audience)
    assert audience <= set(AUDIENCES), audience
    required = list(required)
    assert set(required) <= set(properties or {}), (name, required)

    def decorator(fn: Handler) -> Handler:
        if name in REGISTRY:
            raise ValueError(f"Duplicate tool {name}")
        REGISTRY[name] = ToolSpec(
            name=name,
            description={"en": en.strip(), "ru": ru.strip()},
            parameters={"type": "object", "properties": properties or {}, "required": required},
            handler=fn,
            audience=audience,
            allow_empty=frozenset(allow_empty),
            order=(_module_rank(fn.__module__), next(_seq)),
        )
        return fn

    return decorator


def tools_for(kind: str) -> list[ToolSpec]:
    _load()
    return [spec for spec in REGISTRY.values() if kind in spec.audience]


def schemas_for(kind: str, language: str) -> list[dict[str, Any]]:
    """Deterministic tool list for an actor kind (kept constant for prompt caching). The schemas are
    copies: callers may adapt them for a provider without touching the registry."""
    return [
        {"name": s.name, "description": s.describe(language), "parameters": copy.deepcopy(s.parameters)}
        for s in tools_for(kind)
    ]


# --- arguments --------------------------------------------------------------------------------------

_TYPE_HINTS = {
    "string": "a string",
    "integer": "an integer, e.g. 5",
    "number": "a number, e.g. 2.5",
    "boolean": "true or false",
    "array": 'an array, e.g. ["a", "b"]',
    "object": 'a JSON object, e.g. {"key": "value"}',
}
_TRUE = ("true", "yes", "y", "on", "1")
_FALSE = ("false", "no", "n", "off", "0")


def _as_int(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip().lstrip("#").strip()
        sign = text[:1] if text[:1] in "+-" else ""
        if text[len(sign):].isdecimal():
            return int(text)
    elif isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _parse_json(value: str, opener: str) -> Any:
    text = value.strip()
    if text.startswith(opener):
        try:
            return json.loads(text)
        except ValueError:
            return value
    return value


def _coerce(value: Any, schema: dict[str, Any]) -> Any:
    """Light, unambiguous coercion of what LLMs commonly send ("5" for 5, a JSON string for an array...)."""
    expected = schema.get("type")
    if value is None or isinstance(expected, list):
        return value
    if expected == "integer":
        return value if isinstance(value, bool) else _as_int(value)
    if expected == "number" and isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return value
    if expected == "boolean":
        if isinstance(value, str):
            low = value.strip().lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
        elif isinstance(value, int) and not isinstance(value, bool) and value in (0, 1):
            return bool(value)
        return value
    if expected == "string":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        enum = schema.get("enum")
        if isinstance(value, str) and enum:
            key = value.strip().lower()
            keys = (key, key.replace("-", "_").replace(" ", "_"))
            return next((e for e in enum if isinstance(e, str) and e.lower() in keys), value)
        return value
    if expected == "object" and isinstance(value, str):
        if not value.strip():
            return None
        return _parse_json(value, "{")
    if expected == "array":
        items = schema.get("items") or {}
        if isinstance(value, str):
            parsed = _parse_json(value, "[")
            if isinstance(parsed, list):
                value = parsed
            elif items.get("type") in ("object", "array"):
                return value          # a broken JSON string: let the type check explain the format
            else:
                value = [p.strip() for p in value.split(",") if p.strip()]
        elif isinstance(value, dict) and items.get("type") == "object":
            value = [value]
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            value = [value]
        if isinstance(value, list) and items:
            value = [_coerce(v, items) for v in value]
        return value
    return value


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def _check_type(name: str, value: Any, schema: dict[str, Any]) -> None:
    expected = schema.get("type")
    if expected is None or isinstance(expected, list) or value is None:
        return
    if not _type_ok(value, expected):
        got = type(value).__name__
        raise ToolError(f"Argument '{name}' must be {_TYPE_HINTS.get(expected, expected)}; got {got} {value!r:.80}.")
    enum = schema.get("enum")
    if enum and value not in enum:
        raise ToolError(f"Argument '{name}' must be one of: {', '.join(map(str, enum))}; got {value!r:.80}.")
    if expected == "array" and schema.get("items"):
        for i, item in enumerate(value):
            _check_type(f"{name}[{i}]", item, schema["items"])


def _signature(spec: ToolSpec, parameters: dict[str, Any] | None = None) -> str:
    """`name(a, b?)` from `parameters` (the caller's own schema of the tool; default: the registry's)."""
    parameters = parameters or spec.parameters
    props = parameters.get("properties", {})
    required = set(parameters.get("required", []))
    return f"{spec.name}(" + ", ".join(p if p in required else f"{p}?" for p in props) + ")"


def prepare_args(spec: ToolSpec, args: dict[str, Any] | None,
                 usage: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validated and lightly coerced copy of `args`; unknown keys are dropped (see `unknown_args`). `usage`:
    the parameters the caller knows the tool by (an agent's frozen schema), for the usage line of errors."""
    args = dict(args or {})
    props = spec.parameters.get("properties", {})
    for key in [k for k in args if k not in props]:
        args.pop(key)
    for key, schema in props.items():
        if key in args:
            args[key] = _coerce(args[key], schema)
            _check_type(key, args[key], schema)
    missing = []
    for key in spec.parameters.get("required", []):
        value = args.get(key)
        if value is None or (isinstance(value, str) and not value.strip() and key not in spec.allow_empty):
            missing.append(key)
    if missing:
        raise ToolError(f"Missing required argument(s) for {spec.name}: {', '.join(missing)}. "
                        f"Usage: {_signature(spec, usage)} (? = optional).")
    return args


def unknown_args(spec: ToolSpec, args: dict[str, Any] | None,
                 parameters: dict[str, Any] | None = None) -> list[str]:
    """Keys of `args` that `parameters` (the caller's own schema of the tool; default: the registry's) lacks."""
    props = (parameters or spec.parameters).get("properties", {})
    return [k for k in (args or {}) if k not in props]


# --- calling ----------------------------------------------------------------------------------------

def _unknown_tool(name: str, kind: str, names: list[str] | None = None) -> str:
    """`names`: the tools the caller has (an LLM agent's frozen set); default: the registry for its kind. Only
    those are suggested, so an agent is never pointed at a tool its tool set lacks."""
    if names is None:
        names = [s.name for s in tools_for(kind)]
    close = difflib.get_close_matches(str(name), names, n=3, cutoff=0.5)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    return f"Unknown tool '{name}'.{hint} Available tools: {', '.join(names)}."


def _not_available(spec: ToolSpec, kind: str) -> str:
    text = f"Tool '{spec.name}' is not available to {_AUDIENCE_NAMES.get(kind, kind)}"
    if kind == "user" and "external" in spec.audience:
        text += "; it is for swarm members: call join_swarm, then pass as_agent=\"<your name>\""
    elif kind == "external" and "user" in spec.audience:
        text += "; it is for the user: omit as_agent"
    return text + "."


async def call_tool(ctx: ToolContext, name: str, args: dict[str, Any] | None) -> tuple[str, bool]:
    """Run a tool for `ctx.actor`. Returns (text, is_error); never raises for tool-level problems."""
    _load()
    if ctx.actor != USER and not ctx.swarm.is_member(ctx.actor):
        known = ctx.swarm.member(ctx.actor) is not None
        reason = "has left the swarm (retired)" if known else "is not a member of this swarm"
        return f"Error: '{ctx.actor}' {reason}; tools cannot be used on its behalf.", True
    kind = ctx.kind
    # An LLM agent calls the tools of its own frozen tool set (tools/frozen.py), with the parameters they have
    # there: the registry may already be a later revision. (getattr: tool tests pass simpler runtimes.)
    lookup = getattr(ctx.runtime, "tool_parameters", None)
    frozen_params = lookup(name) if lookup is not None else None
    if lookup is not None and frozen_params is None:
        own = [t.get("name") for t in getattr(ctx.runtime, "tools", None) or []]
        return _unknown_tool(name, kind, names=own), True
    spec = REGISTRY.get(name)
    if spec is None:
        return _unknown_tool(name, kind), True
    if kind not in spec.audience:
        return _not_available(spec, kind), True
    params = frozen_params or spec.parameters
    if args is not None and not isinstance(args, dict):
        return (f"Error: the arguments of {name} must be a JSON object, got {type(args).__name__}. "
                f"Usage: {_signature(spec, params)}."), True
    ignored = unknown_args(spec, args, params)
    note = ""
    if ignored:
        note = (f"\n[Ignored unknown argument(s): {', '.join(ignored)}. Usage: {_signature(spec, params)}]")
        args = {k: v for k, v in args.items() if k not in ignored}  # also those only a later revision has
    try:
        prepared = prepare_args(spec, args, usage=params)
        result = await spec.handler(ctx, prepared)
        return (result if isinstance(result, str) else str(result)) + note, False
    except ToolError as exc:
        return f"Error: {exc}{note}", True
    except UnicodeEncodeError as exc:  # e.g. "\ud800" in a text stored by sqlite3 (UTF-8 only)
        return (f"Error: an argument of {name} contains invalid Unicode (an unpaired surrogate, "
                f"{exc.object[exc.start:exc.end]!r}); remove or replace that character and retry.{note}"), True
    except Exception as exc:  # tool bugs must not kill the agent loop
        log.exception("tool %s failed", name)
        return f"Internal error in {name}: {type(exc).__name__}: {exc}", True


_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    from . import (board_tools, comm_tools, file_tools, job_tools, mcp_tools, skill_tools, swarm_tools,  # noqa: F401
                   trace_tools, trigger_tools)

    ordered = sorted(REGISTRY.values(), key=lambda s: s.order)
    REGISTRY.clear()
    REGISTRY.update((s.name, s) for s in ordered)
    _loaded = True
