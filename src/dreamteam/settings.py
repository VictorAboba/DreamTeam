"""Global settings of a DreamTeam home (docs/SPEC.md §17.1).

A *home* is a directory with `dreamteam.toml` (the global settings: model endpoints, defaults for new
sessions, agents, shell, MCP, tracing, server) and `sessions/` (one directory per session). The web UI
edits the settings through `public_view()` / `apply_update()`; API keys never leave the server: the
public view replaces them with a hint and an update keeps a stored key unless it sends a new one.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime
import math
import os
import re
import time
import tomllib
from pathlib import Path
from typing import Any

from .config import CONFIG_FILE, Config, ModelProfile, env_var_name, make_profile, render_template, toml_string

HOME_ENV = "DREAMTEAM_HOME"
# `roles` is a list of tables ([[roles]], the role catalog: docs/SPEC.md §17.3); the others are tables
SECTIONS = ("swarm", "agents", "server", "shell", "workspace", "mcp", "skills", "tracing", "roles", "models")
_ROLE_FIELDS = ("name", "description", "instructions", "model", "hidden", "skills")
_PUBLIC_KEY_FIELDS = ("api_key_set", "api_key_hint", "api_key_env_set")
# A hidden secret in the public view (`env` values of [mcp.external.*]): the mask plus the key hint.
# An update that sends a mask back keeps the stored value.
SECRET_MASK = "••••"
# `api_key_env` is a variable name (config.env_var_name). Anything else in that field is an API key
# pasted into the wrong input: an update stores it as the key and the public view never shows it.
env_name = env_var_name

# Endpoint presets offered by the settings UI ("custom" leaves everything to the user).
PROVIDER_CHOICES: list[dict[str, Any]] = [
    {"id": "anthropic", "label": "Anthropic (Claude API)", "provider": "anthropic", "base_url": None,
     "api_key_env": "ANTHROPIC_API_KEY", "model": "claude-opus-5-5", "effort": "medium",
     "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-1"]},
    {"id": "openai", "label": "OpenAI", "provider": "openai", "base_url": None,
     "api_key_env": "OPENAI_API_KEY", "model": "gpt-5.1", "models": []},
    {"id": "openrouter", "label": "OpenRouter", "provider": "openai", "base_url": "https://openrouter.ai/api/v1",
     "api_key_env": "OPENROUTER_API_KEY", "model": "anthropic/claude-opus-5.5", "models": []},
    {"id": "ollama", "label": "Ollama", "provider": "openai", "base_url": "http://localhost:11434/v1",
     "api_key": "ollama", "model": "qwen3:32b", "context_tokens": 32000, "max_tokens": 8000,
     "input_price": 0, "output_price": 0, "models": []},
    {"id": "vllm", "label": "vLLM", "provider": "openai", "base_url": "http://localhost:8000/v1",
     "api_key": "EMPTY", "model": "Qwen/Qwen3-32B", "context_tokens": 32000, "max_tokens": 8000,
     "input_price": 0, "output_price": 0, "models": []},
    {"id": "lmstudio", "label": "LM Studio", "provider": "openai", "base_url": "http://localhost:1234/v1",
     "api_key": "lm-studio", "model": "", "context_tokens": 32000, "max_tokens": 8000,
     "input_price": 0, "output_price": 0, "models": []},
    {"id": "custom", "label": "OpenAI-compatible (custom)", "provider": "openai", "base_url": "",
     "api_key_env": "", "model": "", "models": []},
]


# ---- home layout ------------------------------------------------------------------------------
def default_home() -> Path:
    """$DREAMTEAM_HOME, else ~/DreamTeam."""
    env = os.environ.get(HOME_ENV)
    return Path(env).expanduser().resolve() if env else (Path.home() / "DreamTeam").resolve()


def settings_path(home: str | Path) -> Path:
    return Path(home) / CONFIG_FILE


def ensure_home(home: str | Path, language: str = "ru", provider: str = "anthropic") -> Path:
    """Create the home directory, its settings file (from the template) and `sessions/` if missing."""
    home = Path(home).expanduser().resolve()
    home.mkdir(parents=True, exist_ok=True)
    (home / "sessions").mkdir(exist_ok=True)
    path = settings_path(home)
    if not path.exists():
        path.write_text(render_template("DreamTeam", language, provider), encoding="utf-8")
    return home


def read_raw(home: str | Path) -> dict[str, Any]:
    """The settings file as a plain dict (TOML)."""
    with settings_path(home).open("rb") as f:
        return tomllib.load(f)


def validate(home: str | Path, raw: dict[str, Any]) -> Config:
    """Config.from_dict with the same checks the swarm applies; raises ValueError with a readable text."""
    try:
        return Config.from_dict(home, copy.deepcopy(raw))
    except TypeError as exc:  # wrong value types inside a section
        raise ValueError(str(exc)) from None


def write_raw(home: str | Path, raw: dict[str, Any]) -> Path:
    """Validate and write the settings atomically; the previous file is kept as dreamteam.toml.bak."""
    validate(home, raw)
    path = settings_path(home)
    text = ("# DreamTeam settings. Edited by the web UI (Settings); manual edits are fine too,\n"
            "# but comments are not preserved when the UI saves the file.\n\n" + dump_toml(raw))
    if tomllib.loads(text) != _without_none(raw):  # never write a file that does not read back the same
        raise ValueError("The settings cannot be stored as TOML unchanged")
    data = text.encode("utf-8")  # dump_toml rejects text that cannot be encoded
    if path.exists():
        backup = path.with_name(path.name + ".bak")
        try:
            backup.write_bytes(path.read_bytes())
            _private(backup)
        except OSError:
            pass
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        _private(tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():  # the replace failed (e.g. the file is locked by an editor): no stray temp file
            try:
                tmp.unlink()
            except OSError:
                pass
    return path


def _private(path: Path) -> None:
    """The settings hold API keys: readable by the owner only (POSIX; Windows homes are private by ACL)."""
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _without_none(value: Any) -> Any:
    """`value` as TOML stores it: None values of tables are omitted."""
    if isinstance(value, dict):
        return {k: _without_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, (list, tuple)):
        return [_without_none(v) for v in value]
    return value


# ---- TOML writer (the subset the settings use) ---------------------------------------------------
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(key: Any) -> str:
    key = str(key)
    return key if _BARE_KEY.match(key) else toml_string(key)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        if not -2 ** 63 <= value < 2 ** 63:
            raise ValueError(f"The number {value} is too big for the settings file")
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError("NaN/inf values cannot be stored in settings")
        return repr(value)
    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):  # from a hand-edited file
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_key(k)} = {_value(v)}" for k, v in value.items() if v is not None) + "}"
    raise ValueError(f"Unsupported settings value: {value!r}")


def _table_list(value: Any) -> bool:
    """A non-empty list of tables: written as an array of tables (`[[roles]]` blocks)."""
    return isinstance(value, list) and bool(value) and all(isinstance(v, dict) for v in value)


def dump_toml(data: dict[str, Any]) -> str:
    """Serialize nested dicts as TOML tables (None values are omitted); a top-level list of tables is written
    as an array of tables after all tables."""
    out: list[str] = []

    def table(prefix: list[str], body: dict[str, Any]) -> None:
        scalars = {k: v for k, v in body.items() if v is not None and not isinstance(v, dict)}
        tables = {k: v for k, v in body.items() if isinstance(v, dict)}
        # small inline-able dicts (e.g. env of an external MCP server) stay inline when they are leaves
        inline = {k: v for k, v in tables.items()
                  if prefix and v and all(not isinstance(x, dict) for x in v.values()) and k in ("env", "extra")}
        if prefix and (scalars or inline or not tables):
            out.append(f"[{'.'.join(_key(p) for p in prefix)}]")
        for k, v in scalars.items():
            out.append(f"{_key(k)} = {_value(v)}")
        for k, v in inline.items():
            out.append(f"{_key(k)} = {_value(v)}")
        if prefix and (scalars or inline or not tables):
            out.append("")
        for k, v in tables.items():
            if k not in inline:
                table(prefix + [k], v)

    top_scalars = {k: v for k, v in data.items() if v is not None and not isinstance(v, dict) and not _table_list(v)}
    for k, v in top_scalars.items():
        out.append(f"{_key(k)} = {_value(v)}")
    if top_scalars:
        out.append("")
    for name in sorted((k for k, v in data.items() if isinstance(v, dict)),
                       key=lambda n: SECTIONS.index(n) if n in SECTIONS else len(SECTIONS)):
        table([name], data[name])
    # after every table: a key written below `[[roles]]` would belong to the last role
    for name, items in data.items():
        if _table_list(items):
            for item in items:
                out.append(f"[[{_key(name)}]]")
                out.extend(f"{_key(k)} = {_value(v)}" for k, v in item.items() if v is not None)
                out.append("")
    return "\n".join(out).rstrip() + "\n"


def deep_merge(base: dict[str, Any], override: dict[str, Any] | None) -> dict[str, Any]:
    """`base` updated by `override` recursively (dicts merge, everything else replaces)."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# ---- public view / updates (API keys stay on the server) --------------------------------------------
def key_hint(key: str | None) -> str:
    if not key:
        return ""
    return "…" + key[-4:] if len(key) > 8 else "…"


def _stored_key(profile: dict[str, Any]) -> str | None:
    """The API key of a stored profile, including one that was pasted into `api_key_env`."""
    key = profile.get("api_key")
    env = profile.get("api_key_env")
    if not key and isinstance(env, str) and env.strip() and env_name(env) is None:
        key = env.strip()
    return str(key) if key else None


def _mask(value: Any) -> str:
    return SECRET_MASK + key_hint(str(value))


def public_view(raw: dict[str, Any]) -> dict[str, Any]:
    """Settings for the UI: every section filled with defaults, API keys replaced by
    api_key_set / api_key_hint, api_key_env_set tells whether the named variable exists. Values of
    `env` tables of external MCP servers (usually tokens) are masked too. `roles`: the [[roles]] entries as
    stored, normalized (a built-in role by its id)."""
    cfg = validate(".", raw)
    view: dict[str, Any] = {}
    for name in SECTIONS:
        if name in ("roles", "models"):
            continue
        view[name] = dataclasses.asdict(getattr(cfg, name))
    view["swarm"].pop("goal", None)
    view["swarm"].pop("goal_description", None)
    for spec in (view["mcp"].get("external") or {}).values():
        if isinstance(spec, dict) and isinstance(spec.get("env"), dict):
            spec["env"] = {k: _mask(v) if v not in (None, "") else v for k, v in spec["env"].items()}
    view["roles"] = [{k: getattr(role, k) for k in _ROLE_FIELDS} for role in cfg.roles]
    models: dict[str, Any] = {}
    for name, profile in cfg.models.items():
        data = dataclasses.asdict(profile)
        data.pop("name", None)
        key = _stored_key(data)
        data.pop("api_key", None)
        env = env_name(data.get("api_key_env"))
        data["api_key_env"] = env  # a key pasted into this field is never shown
        data["api_key"] = None
        data["api_key_set"] = bool(key)
        data["api_key_hint"] = key_hint(key)
        data["api_key_env_set"] = bool(os.environ.get(env)) if env else None
        models[name] = data
    view["models"] = models
    return view


def profile_renames(update: dict[str, Any]) -> dict[str, str]:
    """{old name: new name} of the model profiles an update renames (`renamed_from`)."""
    models = update.get("models") if isinstance(update, dict) else None
    if not isinstance(models, dict):
        return {}
    renames = {}
    for name, profile in models.items():
        old = profile.get("renamed_from") if isinstance(profile, dict) else None
        if isinstance(old, str) and old and old != name and old not in models:
            renames[old] = name
    return renames


def _restore_secrets(mcp: dict[str, Any], old_mcp: Any) -> None:
    """Put the stored values back where an update sends the masks of the public view (in place)."""
    external = mcp.get("external")
    old_external = old_mcp.get("external") if isinstance(old_mcp, dict) else None
    if not isinstance(external, dict):
        return
    old_external = old_external if isinstance(old_external, dict) else {}
    for server, spec in external.items():
        env = spec.get("env") if isinstance(spec, dict) else None
        if not isinstance(env, dict):
            continue
        for key, value in env.items():
            if not (isinstance(value, str) and value.startswith(SECRET_MASK)):
                continue
            # the same server first; a server renamed in the JSON still finds its value
            candidates = [old_external.get(server)] + [s for n, s in old_external.items() if n != server]
            for old in candidates:
                old_env = old.get("env") if isinstance(old, dict) else None
                if isinstance(old_env, dict) and key in old_env and _mask(old_env[key]) == value:
                    env[key] = old_env[key]
                    break
            else:
                raise ValueError(f"[mcp.external.{server}] env {key}: the hidden value is not known; "
                                 "enter the value again")


def apply_update(raw: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """New raw settings from a UI update. Sections present in `update` replace the stored ones; `models`
    is the complete list of profiles (missing ones are deleted). For each profile `api_key` absent or
    null keeps the stored key (of the same profile, or of `renamed_from`), "" removes it, a string sets it.
    A renamed default profile stays the default; masked MCP `env` values keep their stored values.
    `roles` is the complete list of [[roles]] entries ([] removes every customization); a role's model profile
    follows a rename. Their rules are checked by `validate` (config.parse_roles)."""
    if not isinstance(update, dict):
        raise ValueError("The settings update must be an object")
    result = copy.deepcopy(raw)
    for name, section in update.items():
        if name not in SECTIONS:
            raise ValueError(f"Unknown settings section: {name}")
        if name == "roles":
            if not isinstance(section, list):
                raise ValueError("Section roles must be a list of roles")
            entries = []
            for i, item in enumerate(section, 1):
                if not isinstance(item, dict):
                    raise ValueError(f"Role {i} must be an object")
                # unset fields are not stored: an untouched field of a built-in role keeps the built-in value
                entries.append({k: (" ".join(v.split()) if k == "name" and isinstance(v, str) else v)
                                for k, v in copy.deepcopy(item).items()
                                if v is not None and v != "" and v != [] and not (k == "hidden" and v is False)})
            if entries:
                result["roles"] = entries
            else:
                result.pop("roles", None)
            continue
        if not isinstance(section, dict):
            raise ValueError(f"Section {name} must be an object")
        section = copy.deepcopy(section)
        if name != "models":
            clean = {k: v for k, v in section.items() if v is not None}
            if name == "swarm":  # the goal belongs to sessions, not to the global settings
                for k in ("goal", "goal_description"):
                    if k in (raw.get("swarm") or {}):
                        clean[k] = raw["swarm"][k]
                    else:
                        clean.pop(k, None)
            if name == "mcp":
                _restore_secrets(clean, raw.get("mcp"))
            result[name] = clean
            continue
        old_models = raw.get("models") if isinstance(raw.get("models"), dict) else {}
        models: dict[str, Any] = {}
        for pname, profile in section.items():
            if not isinstance(profile, dict):
                raise ValueError(f"Model profile {pname} must be an object")
            if not _BARE_KEY.match(pname):
                raise ValueError(f"Model profile name '{pname}' may contain only letters, digits, '-' and '_'")
            data = {k: v for k, v in profile.items() if k not in _PUBLIC_KEY_FIELDS and k not in ("name", "renamed_from")}
            source = old_models.get(profile.get("renamed_from") or pname)
            source = source if isinstance(source, dict) else {}
            key = data.pop("api_key", None)
            env = data.get("api_key_env")
            if isinstance(env, str) and env.strip():
                if env_name(env) is None:  # a key pasted into the variable-name field
                    data.pop("api_key_env")
                    if not (isinstance(key, str) and key):  # an explicit new key wins
                        key = env.strip()
                else:
                    data["api_key_env"] = env_name(env)
            if key is None:
                if _stored_key(source):
                    data["api_key"] = _stored_key(source)
            elif str(key).strip():  # a pasted key may end with a newline (invalid in an HTTP header)
                data["api_key"] = str(key).strip()
            data = {k: v for k, v in data.items()
                    if (v is not None and v != "") or (k == "model" and isinstance(v, str))}
            if data.get("extra") == {}:
                data.pop("extra")
            models[pname] = data
        if not models:
            raise ValueError("At least one model profile is required")
        result["models"] = models
    renames = profile_renames(update)  # after every section: `agents` may come after `models`
    agents = result.get("agents") or {}
    default = agents.get("default_model", "default") if isinstance(agents, dict) else None
    if default in renames:
        result["agents"] = {**agents, "default_model": renames[default]}
    for role in result.get("roles") if isinstance(result.get("roles"), list) else ():
        if isinstance(role, dict) and isinstance(role.get("model"), str) and role["model"] in renames:
            role["model"] = renames[role["model"]]
    return result


def stored_profile(raw: dict[str, Any], name: str) -> ModelProfile | None:
    cfg = validate(".", raw)
    return cfg.models.get(name)


def profile_from_view(raw: dict[str, Any], data: dict[str, Any], name: str | None = None) -> ModelProfile:
    """A ModelProfile from a (possibly unsaved) UI profile; a missing api_key falls back to the stored
    key of profile `name`. Raises ValueError for unknown fields or wrong value types."""
    if not isinstance(data, dict):
        raise ValueError("The model profile must be an object")
    data = {k: v for k, v in data.items()
            if k not in _PUBLIC_KEY_FIELDS and k not in ("renamed_from",) and v is not None and v != ""}
    env = data.get("api_key_env")
    if isinstance(env, str):
        if env_name(env) is None:  # a key typed into the variable-name field
            data.pop("api_key_env")
            data.setdefault("api_key", env.strip())
        else:
            data["api_key_env"] = env_name(env)
    if isinstance(data.get("api_key"), str):
        data["api_key"] = data["api_key"].strip()
    if not data.get("api_key") and name:
        old = (raw.get("models") or {}).get(name)
        if isinstance(old, dict) and _stored_key(old):
            data["api_key"] = _stored_key(old)
    profile_name = str(name or data.get("name") or "check")
    data.pop("name", None)
    known = {f.name for f in dataclasses.fields(ModelProfile)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown model profile fields: {', '.join(sorted(map(str, unknown)))}")
    return make_profile(profile_name, data)


# ---- endpoint checks --------------------------------------------------------------------------
async def list_models(profile: ModelProfile, timeout: float = 20.0) -> list[str]:
    """Model ids offered by the profile's endpoint (GET /models; free). Raises ValueError with a readable
    message on failure."""
    from .llm.base import LLMError, api_error_text, missing_key_hint

    try:
        llm = _client_for(profile)
    except ValueError:
        raise
    except LLMError as exc:  # e.g. Anthropic credentials that cannot be resolved
        raise ValueError(scrub_keys(exc.message, profile)) from None
    except Exception as exc:  # an SDK refusing the profile: still a readable error for the UI
        raise ValueError(scrub_keys(f"{type(exc).__name__}: {exc}", profile)) from None
    try:
        ids: list[str] = []
        page = await llm.client.models.list(timeout=timeout)
        async for model in page:
            ids.append(getattr(model, "id", None) or str(model))
            if len(ids) >= 500:
                break
        return sorted(set(ids))
    except Exception as exc:
        raise ValueError(scrub_keys(api_error_text(exc) + missing_key_hint(profile), profile)) from None
    finally:
        await _close(llm)


async def check_profile(profile: ModelProfile, *, live: bool = False, timeout: float = 30.0) -> dict[str, Any]:
    """Check an endpoint: list its models (free) and, with live=True, send one tiny request (costs a few
    tokens). Returns {"ok", "models", "models_error", "model_found", "error", "reply", "latency_ms",
    "cost_usd"}; `ok` is the live result when live=True, else whether the model list was read."""
    from .llm.base import LLMError, cost_of

    result: dict[str, Any] = {"ok": False, "models": [], "models_error": None, "model_found": None,
                              "error": None, "reply": None, "latency_ms": None, "cost_usd": None}
    try:
        result["models"] = await list_models(profile, timeout=timeout)
        result["model_found"] = (profile.model in result["models"]) if result["models"] else None
        result["ok"] = True
    except ValueError as exc:
        result["models_error"] = str(exc)
        result["error"] = str(exc)
    if not live:
        return result
    # a small answer; effort only when the profile uses it (an endpoint may reject the parameter)
    probe = dataclasses.replace(profile, max_tokens=min(profile.max_tokens or 512, 512),
                                effort="low" if profile.effort else None)
    result["ok"], result["error"] = False, None
    started = time.perf_counter()
    llm = None
    try:
        llm = _client_for(probe)
        response = await llm.complete("Answer with one word.", [{"role": "user", "content": "Say OK."}], [])
        result["reply"] = (response.text or response.thinking or "").strip()[:200] or f"({response.stop_reason})"
        result["cost_usd"] = round(cost_of(probe, response.usage, response.model), 6)
        result["ok"] = True
    except LLMError as exc:
        result["error"] = f"{exc.kind}: {exc.message}"
    except Exception as exc:  # unexpected adapter failure: report, never raise into the UI
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["latency_ms"] = int((time.perf_counter() - started) * 1000)
        if llm is not None:
            await _close(llm)
    for field in ("error", "reply"):  # an endpoint may echo the key it was given
        if isinstance(result[field], str):
            result[field] = scrub_keys(result[field], profile)
    return result


def scrub_keys(text: str, profile: ModelProfile) -> str:
    """`text` without the API keys the profile can send (some endpoints echo a rejected key)."""
    candidates = {profile.api_key, profile.resolve_api_key(), os.environ.get("OPENAI_API_KEY"),
                  os.environ.get("ANTHROPIC_API_KEY")}
    for key in sorted((k for k in candidates if k and len(k) >= 8), key=len, reverse=True):
        text = text.replace(key, key_hint(key) or "…")
    return text


def _client_for(profile: ModelProfile):
    provider = (profile.provider or "").strip().lower()
    if provider == "anthropic":
        from .llm.anthropic_llm import AnthropicLLM

        return AnthropicLLM(profile, max_retries=0)
    if provider in ("openai", "openrouter", "ollama", "vllm"):
        from .llm.openai_llm import OpenAILLM

        return OpenAILLM(profile, max_retries=0)
    raise ValueError(f"Provider '{profile.provider}' cannot be checked (use anthropic or openai).")


async def _close(llm: Any) -> None:
    close = getattr(llm, "aclose", None)
    if close is not None:
        try:
            await close()
        except Exception:
            pass
