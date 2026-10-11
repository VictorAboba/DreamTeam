"""Swarm configuration: `dreamteam.toml` in the project directory."""

from __future__ import annotations

import json
import math
import os
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable

from . import roles

CONFIG_FILE = "dreamteam.toml"


def toml_string(text: str) -> str:
    """`text` as a TOML basic string. A JSON string is one, except that TOML also forbids a raw DEL; lone
    surrogates cannot be stored at all (ValueError)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"Text is not valid Unicode: {text[:40]!r}") from None
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


# `api_key_env` names a variable; "$NAME", "${NAME}" and "%NAME%" (Windows habit) mean the same
_ENV_NAME = re.compile(r"^(?:\$\{?([A-Za-z_][A-Za-z0-9_]{0,127})\}?|%([A-Za-z_][A-Za-z0-9_]{0,127})%"
                       r"|([A-Za-z_][A-Za-z0-9_]{0,127}))\Z")


def env_var_name(value: Any) -> str | None:
    """The variable name in an `api_key_env` value, or None when the value cannot be one (then it is
    an API key typed into the wrong field)."""
    match = _ENV_NAME.match(value.strip()) if isinstance(value, str) else None
    return next((g for g in match.groups() if g), None) if match else None

# USD per 1M tokens: (input, output, cache_read, cache_write_5m). Used when a profile sets no prices.
KNOWN_PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
    "claude-fable-5": (10.0, 50.0, 1.0, 12.5),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-opus-5": (5.0, 25.0, 0.5, 6.25),
    "claude-opus-4-8": (5.0, 25.0, 0.5, 6.25),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-sonnet-5": (2.0, 10.0, 0.2, 2.5),
    "claude-sonnet-4-6": (3.0, 15.0, 0.3, 3.75),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25),
}


@dataclass
class ModelProfile:
    """One named LLM endpoint. `provider` is `anthropic`, `openai` (any OpenAI-compatible API:
    OpenAI, OpenRouter, vLLM, Ollama, LM Studio...) or `scripted` (tests)."""

    name: str = "default"
    provider: str = "anthropic"
    model: str = "claude-opus-5-5"
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    max_tokens: int = 16000
    # anthropic: output_config.effort; openai: reasoning_effort
    effort: str | None = None
    # anthropic only: "adaptive" (default for current models) or "off" (omit the parameter)
    thinking: str = "adaptive"
    thinking_display: str | None = "summarized"
    # anthropic only: server-side refusal fallbacks ("default"), and what to do with thinking
    # blocks whose conversation prefix changed ("drop_block" | "error" | None)
    fallbacks: str | None = None
    thinking_mismatch: str | None = None
    # openai only: name of the output-limit parameter ("max_tokens" or "max_completion_tokens")
    max_tokens_param: str | None = None
    input_price: float | None = None
    output_price: float | None = None
    cache_read_price: float | None = None
    cache_write_price: float | None = None
    context_tokens: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def resolve_api_key(self) -> str | None:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env) or None
        return None

    def prices(self, model: str | None = None) -> tuple[float, float, float, float]:
        known = KNOWN_PRICES.get(model or self.model) or KNOWN_PRICES.get(self.model)
        inp = self.input_price if self.input_price is not None else (known[0] if known else 0.0)
        out = self.output_price if self.output_price is not None else (known[1] if known else 0.0)
        cr = self.cache_read_price if self.cache_read_price is not None else (known[2] if known else inp * 0.1)
        cw = self.cache_write_price if self.cache_write_price is not None else (known[3] if known else inp * 1.25)
        return inp, out, cr, cw

    @property
    def has_prices(self) -> bool:
        return self.input_price is not None or self.model in KNOWN_PRICES


@dataclass
class SwarmSettings:
    name: str = "DreamTeam"
    language: str = "ru"
    start_agents: int = 3
    max_agents: int = 8
    max_cost_usd: float = 5.0
    goal: str = ""
    goal_description: str = ""
    starting_roles: list[str] = field(default_factory=list)
    # a finished task of an agent goes to review by another member (or the user when nobody else can);
    # false: a task without an explicit reviewer is done at once (docs/SPEC.md §4 "Independent review")
    independent_review: bool = True


@dataclass
class RoleSettings:
    """One [[roles]] entry (docs/SPEC.md §17.3): `name` is a built-in role's id (or its ru/en name; the entry then
    customizes it) or the name of an own role."""

    name: str = ""
    description: str = ""
    instructions: str = ""
    model: str | None = None
    hidden: bool = False


@dataclass
class AgentSettings:
    default_model: str = "default"
    max_steps_per_turn: int = 40
    context_tokens: int = 120_000
    tool_output_chars: int = 12_000
    stagger_seconds: float = 3.0
    idle_nudge_seconds: float = 60.0
    max_parallel_llm_calls: int = 8


@dataclass
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8765
    resume_sessions: bool = True   # restart the sessions that were running when the server stopped


@dataclass
class ShellSettings:
    """The members' `shell` commands and their background jobs (docs/SPEC.md §7.1); read at each use (hot)."""

    program: str = "auto"
    timeout: int = 120                 # seconds a call waits; a command still running then goes on in the background
    max_timeout: int = 1800            # the largest timeout, and the lifetime of a command that went on at its timeout
    background_on_timeout: bool = True  # False: a command still running at its timeout is killed (the old behaviour)
    foreground_max: int = 180          # an AI agent's call waits at most this, whatever its timeout (it then goes on)
    max_jobs_per_member: int = 4       # running background jobs per member; 0 = no background jobs
    max_jobs: int = 16                 # running background jobs per session; 0 = no background jobs
    job_max_minutes: int = 480         # lifetime of jobs started in the background or left running (0 = no limit)
    job_log_mb: int = 2                # output kept per job, in two files
    cpu_threads: int | str = "auto"    # numeric libraries' threads per command: "auto" (cores / AI agents), N, 0 = all
    pass_env: list[str] = field(default_factory=list)


@dataclass
class WorkspaceSettings:
    git: bool = True
    lock_ttl_minutes: int = 30


@dataclass
class MCPSettings:
    require_approval: bool = False
    library_dir: str = "~/.dreamteam/library"
    startup_timeout: float = 180.0
    call_timeout: float = 300.0
    external: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class TracingSettings:
    enabled: bool = True
    max_payload_chars: int = 200_000   # per string in a span payload
    retention_days: float = 0          # delete finished traces older than this; 0 = keep forever


# TOML section of each settings class (error messages)
_SECTION_OF = {"SwarmSettings": "swarm", "AgentSettings": "agents", "ServerSettings": "server",
               "ShellSettings": "shell", "WorkspaceSettings": "workspace", "MCPSettings": "mcp",
               "TracingSettings": "tracing", "ModelProfile": "models", "RoleSettings": "roles"}
# numbers are >= 0; these must be >= 1 (counts and sizes where 0 breaks the swarm)
_MIN_ONE = frozenset({"max_agents", "max_steps_per_turn", "context_tokens", "tool_output_chars",
                      "max_parallel_llm_calls", "max_tokens", "timeout", "max_timeout", "foreground_max",
                      "job_log_mb"})
_MAX = {"port": 65535}
_INT64 = 2 ** 63


def _bad(where: str, key: str, expected: str, value: Any) -> ValueError:
    shown = f"a {type(value).__name__}" if key == "api_key" else repr(value)[:60]  # never echo a key
    return ValueError(f"[{where}] {key} must be {expected}, got {shown}")


def _checked(where: str, key: str, annotation: str, value: Any) -> Any:
    """`value` checked against the field's annotation (a string: this module postpones annotations).
    A settings file or a UI update with a wrong type must fail here, not later inside a running swarm."""
    kind = annotation.replace(" ", "").removesuffix("|None")
    if kind == "int|str":  # [shell] cpu_threads: "auto" or a count (also written as a string, "4")
        text = value.strip().lower() if isinstance(value, str) else None
        if text == "auto":
            return "auto"
        if text is not None:
            if not text.isdigit():
                raise _bad(where, key, '"auto" or a whole number', value)
            value = int(text)
        return _checked(where, key, "int", value)
    if kind == "bool":
        if not isinstance(value, bool):
            raise _bad(where, key, "true or false", value)
        return value
    if kind in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _bad(where, key, "a number", value)
        if isinstance(value, float) and not math.isfinite(value):
            raise _bad(where, key, "a finite number", value)
        if kind == "int":
            if isinstance(value, float):
                if not value.is_integer():
                    raise _bad(where, key, "a whole number", value)
                value = int(value)
            if not -_INT64 <= value < _INT64:
                raise _bad(where, key, "a 64-bit integer", value)
        low, high = (1 if key in _MIN_ONE else 0), _MAX.get(key)
        if value < low or (high is not None and value > high):
            raise _bad(where, key, f">= {low}" if high is None else f"between {low} and {high}", value)
        return value
    if kind == "str":
        if not isinstance(value, str):
            raise _bad(where, key, "a string", value)
        return value
    if kind == "list[str]":
        if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
            raise _bad(where, key, "a list of strings", value)
        return list(value)
    if kind.startswith("dict["):
        if not isinstance(value, dict):
            raise _bad(where, key, "a table", value)
        if kind == "dict[str,dict[str,Any]]":
            for name, item in value.items():
                if not isinstance(item, dict):
                    raise ValueError(f"[{where}.{key}.{name}] must be a table, got {repr(item)[:60]}")
        return dict(value)
    return value


def _fill(cls, data: dict[str, Any] | None, where: str | None = None):
    where = where or _SECTION_OF.get(cls.__name__, cls.__name__)
    data = {} if data is None else data
    if not isinstance(data, dict):
        raise ValueError(f"[{where}] must be a table, got {repr(data)[:60]}")
    types = {f.name: f.type for f in fields(cls)}
    unknown = set(data) - set(types)
    if unknown:
        raise ValueError(f"Unknown keys for [{where}]: {', '.join(sorted(map(str, unknown)))}")
    # None means "not set" (as everywhere in the settings): the default applies
    return cls(**{k: _checked(where, k, str(types[k]), v) for k, v in data.items() if v is not None})


def make_profile(name: str, spec: dict[str, Any]) -> ModelProfile:
    """A checked ModelProfile; the table name wins over a `name` key inside it (lookups use the table name)."""
    if not isinstance(spec, dict):  # the value is not shown: it may be a key written in the wrong place
        raise ValueError(f"[models.{name}] must be a table of profile settings, got a {type(spec).__name__}")
    spec = {**spec, "name": name}
    env = spec.get("api_key_env")
    if isinstance(env, str):
        # normalized here, so that every reader (the LLM clients, the shell's secret scrubbing, error
        # hints) sees a plain variable name and never treats a key as one
        if env_var_name(env) is not None:
            spec["api_key_env"] = env_var_name(env)
        else:
            spec["api_key_env"] = None
            if env.strip() and not spec.get("api_key"):  # a key written into the variable-name field
                spec["api_key"] = env.strip()
    return _fill(ModelProfile, spec, f"models.{name}")


def parse_roles(raw: Any, profiles: Iterable[str]) -> list[RoleSettings]:
    """The checked [[roles]] entries (docs/SPEC.md §17.3), normalized: a built-in role by its id, texts stripped,
    `model = ""` as None. `profiles`: the names of the model profiles a role may use."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("[[roles]] must be a list of role tables (write each role as a [[roles]] table)")
    if len(raw) > roles.MAX_ROLES:
        raise ValueError(f"At most {roles.MAX_ROLES} roles")
    profiles = set(profiles)
    seen: dict[str, int] = {}
    out: list[RoleSettings] = []
    for i, item in enumerate(raw, 1):
        where = f"roles.{i}"
        entry = _fill(RoleSettings, item, where)
        name = roles.normalize(entry.name)
        if not name:
            raise ValueError(f"[{where}] name is empty")
        if len(name) > roles.NAME_LIMIT:
            raise ValueError(f"[{where}] name must be at most {roles.NAME_LIMIT} characters")
        if "," in name or "*" in name:  # they separate the items of a team (--team, /team)
            raise ValueError(f"[{where}] name cannot contain ',' or '*'")
        key = roles.role_key(name)
        if key in seen:
            raise ValueError(f"[{where}] the role '{name}' is listed twice (also as role {seen[key]})")
        seen[key] = i
        for text in ("description", "instructions"):
            if len(getattr(entry, text).strip()) > roles.TEXT_LIMIT:
                raise ValueError(f"[{where}] {text} must be at most {roles.TEXT_LIMIT} characters")
        builtin = roles.preset_id(name)
        description = entry.description.strip()
        if not builtin and not description and not entry.hidden:
            raise ValueError(f"[{where}] '{name}': a role of your own needs a description")
        model = entry.model or None
        if model is not None and model not in profiles:
            raise ValueError(f"[{where}] '{name}' uses the model profile '{model}', which is not defined in [models]")
        out.append(RoleSettings(name=builtin or name, description=description,
                                instructions=entry.instructions.strip(), model=model, hidden=entry.hidden))
    return out


@dataclass
class Config:
    project_dir: Path
    swarm: SwarmSettings = field(default_factory=SwarmSettings)
    agents: AgentSettings = field(default_factory=AgentSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    shell: ShellSettings = field(default_factory=ShellSettings)
    workspace: WorkspaceSettings = field(default_factory=WorkspaceSettings)
    mcp: MCPSettings = field(default_factory=MCPSettings)
    tracing: TracingSettings = field(default_factory=TracingSettings)
    models: dict[str, ModelProfile] = field(default_factory=lambda: {"default": ModelProfile()})
    # Not settings keys: a launcher session that works in another folder (sessions.py, session.json
    # "workspace"). None = `<project>/workspace`. `workspace_adopted`: the folder was the user's before
    # (its git repository configuration is never touched).
    workspace_path: Path | None = None
    workspace_adopted: bool = False
    # [[roles]]: the role catalog's entries (docs/SPEC.md §17.3), checked and normalized by parse_roles
    roles: list[RoleSettings] = field(default_factory=list)

    @property
    def data_dir(self) -> Path:
        return self.project_dir / ".dreamteam"

    @property
    def workspace_dir(self) -> Path:
        return self.workspace_path if self.workspace_path is not None else self.project_dir / "workspace"

    @property
    def library_dir(self) -> Path:
        return Path(os.path.expanduser(self.mcp.library_dir))

    def model(self, name: str | None) -> ModelProfile:
        key = name or self.agents.default_model
        if key not in self.models:
            raise KeyError(f"Unknown model profile '{key}'. Known: {', '.join(self.models)}")
        return self.models[key]

    @classmethod
    def load(cls, project_dir: str | Path) -> "Config":
        project_dir = Path(project_dir).resolve()
        path = project_dir / CONFIG_FILE
        if not path.exists():
            raise FileNotFoundError(f"{path} not found. Run `dreamteam init {project_dir}` first.")
        with path.open("rb") as f:
            raw = tomllib.load(f)
        return cls.from_dict(project_dir, raw)

    @classmethod
    def from_dict(cls, project_dir: str | Path, raw: dict[str, Any]) -> "Config":
        if not isinstance(raw, dict):
            raise ValueError("The settings must be a table of sections")
        raw = dict(raw)
        models_raw = raw.pop("models", None) or {}
        if not isinstance(models_raw, dict):
            raise ValueError("[models] must be a table of model profiles")
        mcp_raw = raw.pop("mcp", None) or {}
        if not isinstance(mcp_raw, dict):
            raise ValueError("[mcp] must be a table")
        mcp_raw = dict(mcp_raw)
        external = mcp_raw.pop("external", None) or {}
        roles_raw = raw.pop("roles", None)  # checked once the model profiles are known
        cfg = cls(
            project_dir=Path(project_dir).resolve(),
            swarm=_fill(SwarmSettings, raw.pop("swarm", None)),
            agents=_fill(AgentSettings, raw.pop("agents", None)),
            server=_fill(ServerSettings, raw.pop("server", None)),
            shell=_fill(ShellSettings, raw.pop("shell", None)),
            workspace=_fill(WorkspaceSettings, raw.pop("workspace", None)),
            mcp=_fill(MCPSettings, {**mcp_raw, "external": external}),
            tracing=_fill(TracingSettings, raw.pop("tracing", None)),
        )
        if raw:
            raise ValueError(f"Unknown config sections: {', '.join(sorted(raw))}")
        if models_raw:
            cfg.models = {name: make_profile(name, spec) for name, spec in models_raw.items()}
        if cfg.agents.default_model not in cfg.models:
            raise ValueError(f"agents.default_model = '{cfg.agents.default_model}' is not defined in [models]")
        cfg.roles = parse_roles(roles_raw, cfg.models)
        if cfg.swarm.language not in ("ru", "en"):
            raise ValueError("swarm.language must be 'ru' or 'en'")
        return cfg


PROVIDER_PRESETS: dict[str, str] = {
    "anthropic": """[models.default]
provider = "anthropic"
model = "claude-opus-5-5"
api_key_env = "ANTHROPIC_API_KEY"   # если переменной нет, SDK возьмёт ключ из `ant auth login`
effort = "medium"                    # low | medium | high | xhigh | max
thinking = "adaptive"
thinking_display = "summarized"      # краткие «мысли» агента видны в UI
fallbacks = "default"                # серверный fallback при отказе модели (beta)
thinking_mismatch = "drop_block"     # не падать, если префикс диалога изменился (beta)
max_tokens = 16000

# Более дешёвый профиль для нанятых агентов: hire_agent(..., model="fast")
[models.fast]
provider = "anthropic"
model = "claude-haiku-5-5"
api_key_env = "ANTHROPIC_API_KEY"
effort = "low"
""",
    "openai": """[models.default]
provider = "openai"
model = "gpt-5.1"
api_key_env = "OPENAI_API_KEY"
input_price = 1.25     # $ за 1M входных токенов — укажите актуальные цены
output_price = 10.0
""",
    "openrouter": """[models.default]
provider = "openai"
base_url = "https://openrouter.ai/api/v1"
model = "anthropic/claude-opus-5.5"
api_key_env = "OPENROUTER_API_KEY"
# OpenRouter сам сообщает стоимость запроса; цены можно не указывать
""",
    "ollama": """[models.default]
provider = "openai"
base_url = "http://localhost:11434/v1"
model = "qwen3:32b"
api_key = "ollama"
max_tokens = 8000
context_tokens = 32000   # окно контекста локальной модели
input_price = 0
output_price = 0
""",
    "vllm": """[models.default]
provider = "openai"
base_url = "http://localhost:8000/v1"
model = "Qwen/Qwen3-32B"
api_key = "EMPTY"
max_tokens = 8000
context_tokens = 32000
input_price = 0
output_price = 0
""",
}


def render_template(name: str, language: str = "ru", provider: str = "anthropic", goal: str = "") -> str:
    models = PROVIDER_PRESETS[provider]
    # quotes, backslashes (Windows paths) and newlines in a folder name or a goal must stay valid TOML
    return f'''# DreamTeam — конфигурация роя
[swarm]
name = {toml_string(name)}
language = "{language}"        # язык промптов агентов: ru | en
start_agents = 3              # сколько агентов стартует
max_agents = 8                # максимум ИИ-агентов (агенты могут нанимать новых; участники, подключившиеся через MCP, не считаются)
max_cost_usd = 5.0            # бюджет роя в долларах; при исчерпании рой встаёт на паузу
goal = {toml_string(goal)}          # глобальная задача (можно задать позже в веб-интерфейсе)
starting_roles = []           # стартовая команда по порядку, например ["architect", "developer", "developer", "tester"]; недостающим до start_agents — «Универсал»
independent_review = true     # задачу агента принимает другой агент (или вы, если больше некому); false — без ревьюера задача сразу готова

[agents]
default_model = "default"
max_steps_per_turn = 40       # шагов ReAct за одно «пробуждение»
context_tokens = 120000       # при превышении история агента сжимается в резюме
stagger_seconds = 3.0         # задержка между стартами агентов

[server]
host = "127.0.0.1"
port = 8765

[shell]
program = "auto"              # auto | bash | sh | powershell | pwsh | cmd
timeout = 120                 # сколько секунд вызов shell ждёт команду; не завершившаяся за это время продолжает работать в фоне
max_timeout = 1800            # наибольший timeout команды; столько же (от запуска) живёт команда, ушедшая в фон по тайм-ауту (само ожидание ИИ-агента ограничивает foreground_max, по MCP — 600 с)
background_on_timeout = true  # false — команду, не уложившуюся в timeout, убивать, как раньше
foreground_max = 180          # ИИ-агент ждёт команду не дольше этого, какой бы timeout он ни задал: дальше она работает в фоне, а агент видит сообщения и комментарии
max_jobs_per_member = 4       # фоновых процессов у одного участника; 0 — без фоновых процессов
max_jobs = 16                 # фоновых процессов во всей сессии; 0 — без фоновых процессов
job_max_minutes = 480         # сколько живёт процесс, запущенный в фоне намеренно или оставленный командой (0 — без предела)
job_log_mb = 2                # сколько вывода фонового процесса хранить (.scratch/<участник>/jobs/), МБ
cpu_threads = "auto"          # потоков у numpy, BLAS, OpenMP и т. п. на команду: "auto" — ядра поровну между ИИ-агентами, число, 0 — не ограничивать
pass_env = []                 # переменные окружения с секретами, которые можно передать в консоль агентов

[workspace]
git = true                    # общий workspace под git с автокоммитами
lock_ttl_minutes = 30

[mcp]
require_approval = false      # true — новые MCP агентов ждут одобрения пользователя
library_dir = "~/.dreamteam/library"   # общая библиотека MCP между запусками и роями

[tracing]
enabled = true                # трейсы агентов: каждый вызов LLM и инструмента с полными данными
max_payload_chars = 200000
retention_days = 0            # 0 — хранить всегда

# Внешние MCP-серверы, доступные рою:
# [mcp.external.fetch]
# command = ["uvx", "mcp-server-fetch"]
# description = "Загрузка веб-страниц"

# Каталог ролей. Встроенные роли (generalist «Универсал», developer «Разработчик», tester «Тестировщик»,
# reviewer «Ревьюер», analyst «Аналитик», architect «Архитектор», writer «Технический писатель»,
# researcher «Исследователь») можно изменить или скрыть, а также добавить свои:
# [[roles]]
# name = "tester"              # встроенная роль: меняются только указанные поля
# model = "fast"
# [[roles]]
# name = "Дата-инженер"
# description = "Ты строишь конвейеры данных: ..."

{models}'''
