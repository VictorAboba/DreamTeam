"""prompts.py: ru/en parity, placeholders used by callers, tool references, the guidance that drives
self-organization, and a Russian end-to-end run of a scripted swarm."""

from __future__ import annotations

import ast
import asyncio
import re
import string
import time
from pathlib import Path
from typing import Any

import pytest

from dreamteam import prompts
from dreamteam import swarm as swarm_module
from dreamteam.config import Config
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, Status
from dreamteam.swarm import Swarm
from dreamteam.tools import REGISTRY, tools_for

SRC = Path(prompts.__file__).resolve().parent
LANGS = ("en", "ru")
SYSTEMS = {"en": prompts.SYSTEM_EN, "ru": prompts.SYSTEM_RU}
SYSTEM_KW = dict(name="Ann Lee", swarm="Swarm X", role="Tester", instructions="Check {curly} things",
                 shell="bash", max_agents=7)
_FMT = string.Formatter()
_CODE_IDENT = re.compile(r"`([a-z_]+)`")


def placeholders(template: str) -> set[str]:
    return {field for _, field, _, _ in _FMT.parse(template) if field is not None}


def code_idents(text: str) -> set[str]:
    """Identifiers written in backticks: tool names, statuses, parameter names."""
    return set(_CODE_IDENT.findall(text))


# ------------------------------------------------------------------------------------- parity
def test_languages_have_the_same_keys_placeholders_and_tool_names():
    assert set(prompts.TEXTS) == set(LANGS)
    assert set(prompts.TEXTS["en"]) == set(prompts.TEXTS["ru"])
    for key, en in prompts.TEXTS["en"].items():
        ru = prompts.TEXTS["ru"][key]
        assert placeholders(en) == placeholders(ru), key
        assert code_idents(en) == code_idents(ru), key


def test_system_prompts_have_the_same_placeholders_and_tool_names():
    assert placeholders(prompts.SYSTEM_EN) == placeholders(prompts.SYSTEM_RU) == {
        "name", "swarm", "role", "instructions", "shell", "max_agents"}
    assert code_idents(prompts.SYSTEM_EN) == code_idents(prompts.SYSTEM_RU)


def test_planning_task_and_default_role_exist_in_both_languages():
    for lang in LANGS:
        title, goal, description = prompts.PLANNING_TASK[lang]
        assert title and goal and description and len(title) <= 200
        assert "depends_on" in description
        assert prompts.DEFAULT_ROLE[lang]


def test_texts_reference_only_real_tools():
    llm_tools = {s.name for s in tools_for("llm")}
    params = {p for spec in REGISTRY.values() for p in spec.parameters.get("properties", {})}
    common = set(Status.ALL) | params
    for lang in LANGS:
        unknown = code_idents(SYSTEMS[lang]) - llm_tools - common
        assert not unknown, f"system prompt ({lang}) names unknown tools: {unknown}"
        for key, text in prompts.TEXTS[lang].items():
            # u_* texts go to the user, who has the user tools (e.g. mcp_approve); the rest go to agents
            allowed = (set(REGISTRY) if key.startswith("u_") else llm_tools) | common
            assert not code_idents(text) - allowed, (lang, key, code_idents(text) - allowed)


# ------------------------------------------------------------------------- rendering & callers
def test_system_prompt_renders_all_fields():
    for lang in LANGS:
        text = prompts.system_prompt(lang, **SYSTEM_KW)
        for value in ("Ann Lee", "Swarm X", "Tester", "Check {curly} things", "bash", "7"):
            assert value in text, (lang, value)
        assert not re.search(r"\{[a-z_]+\}", text.replace("{curly}", ""))
        bare = prompts.system_prompt(lang, **{**SYSTEM_KW, "instructions": ""})
        assert prompts.TEXTS[lang]["instructions"].split("{")[0] not in bare


def test_unknown_language_falls_back_to_english():
    # regression: system_prompt() raised KeyError for a language without texts while t() fell back
    assert prompts.system_prompt("de", **SYSTEM_KW) == prompts.system_prompt("en", **SYSTEM_KW)
    assert prompts.system_prompt(None, **SYSTEM_KW) == prompts.system_prompt("en", **SYSTEM_KW)
    assert prompts.t("de", "n_restart") == prompts.t("en", "n_restart")


# positional index of the prompt key in each notification / text call
_KEY_ARG = {"notify": 1, "notify_later": 2, "_notify_member": 2, "t": 1}


def _string_values(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _string_values(node.body) + _string_values(node.orelse)
    return []


def _key_candidates(arg: ast.AST, func: ast.AST | None) -> list[str]:
    found = _string_values(arg)
    if not found and isinstance(arg, ast.Name) and func is not None:
        for node in ast.walk(func):  # key = "a" if x else "b"; notify(..., key, ...)
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == arg.id for t in node.targets):
                found += _string_values(node.value)
    return found


def _prompt_calls() -> list[tuple[str, str, set[str]]]:
    """(location, key, keyword names) for every call that renders a prompt text with a known key."""
    calls = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            name = node.func.attr
            if name not in _KEY_ARG or (name == "t" and not (isinstance(node.func.value, ast.Name)
                                                             and node.func.value.id == "prompts")):
                continue
            index = _KEY_ARG[name]
            if len(node.args) <= index or any(k.arg is None for k in node.keywords):
                continue  # pass-through helpers (**kw) are checked at their own call sites
            owner = next((f for f in funcs if f.lineno <= node.lineno <= (f.end_lineno or f.lineno)
                          and any(c is node for c in ast.walk(f))), None)
            kwargs = {k.arg for k in node.keywords} - {"wake"}
            for key in _key_candidates(node.args[index], owner):
                calls.append((f"{path.relative_to(SRC)}:{node.lineno}", key, kwargs))
    return calls


def test_every_caller_passes_every_placeholder():
    calls = _prompt_calls()
    keys = {key for _, key, _ in calls}
    # the scan must really see the callers (guards against a silently broken AST walk)
    assert {"n_kickoff", "n_assigned", "n_review_approved", "n_review_rejected", "n_trigger", "nudge",
            "wake_header", "incoming_header", "compacted", "u_budget"} <= keys, keys
    assert len(calls) >= 30
    for where, key, kwargs in calls:
        assert key in prompts.TEXTS["en"], f"{where}: unknown prompt key {key!r}"
        for lang in LANGS:
            missing = placeholders(prompts.TEXTS[lang][key]) - kwargs
            assert not missing, f"{where}: {key} ({lang}) needs {missing}, the caller passes {sorted(kwargs)}"
            assert prompts.t(lang, key, **{k: 7.0 for k in kwargs})


# ---------------------------------------------------------------- guidance for self-organization
@pytest.mark.parametrize("lang", LANGS)
def test_texts_steer_agents_to_the_right_tools(lang):
    texts = prompts.TEXTS[lang]
    system = code_idents(SYSTEMS[lang])
    # the core protocol: claim before working, finish with a result, talk in task threads, end turns
    # with wait instead of polling, close the global goal, keep notes
    assert {"task_claim", "task_complete", "task_comment", "send_message", "wait", "goal_complete",
            "update_notes", "board", "task_get"} <= system
    expected = {
        "n_kickoff": {"wait"},          # non-planners wait instead of offering help / planning in parallel
        "n_ready": {"wait"},
        "n_hired": {"wait"},
        "nudge": {"task_complete", "waiting", "wait"},
        "n_stalled": {"waiting", "todo", "failed"},  # an idle holder of in_progress work must move or release it
        "n_all_done": {"goal_complete"},
        "n_goal_reopened": {"goal_complete"},
        "n_review_requested": {"task_review", "task_get"},
        "n_review_rejected": {"task_complete"},
        "n_assigned": {"task_claim"},
        "n_unblocked": {"task_claim"},
        "n_mention": {"task_comment"},
    }
    for key, tools in expected.items():
        assert tools <= code_idents(texts[key]), (key, code_idents(texts[key]))
    # a new member must not greet the hirer: a greeting is a message that wakes someone for nothing
    assert "send_message" not in code_idents(texts["n_hired"])


# ------------------------------------------------------------------- end to end, in Russian
@pytest.fixture
def fast_scheduler(monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.1)


async def _until(predicate, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached in time")


def _call(name: str, **arguments: Any) -> dict:
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


async def test_russian_swarm_gets_russian_guidance_and_reaches_the_goal(tmp_path, fast_scheduler):
    raw = {
        "swarm": {"name": "Рой", "language": "ru", "start_agents": 3, "max_agents": 4, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 0.2, "max_steps_per_turn": 20},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    systems: dict[str, set[str]] = {}
    seen: list[tuple[str, str]] = []
    stalled: set[int] = set()
    holder: dict[str, Swarm] = {}

    def policy(name: str, system: str, messages: list[dict]) -> Any:
        swarm = holder["swarm"]
        systems.setdefault(name, set()).add(system)
        if messages[-1]["role"] == "user":
            seen.append((name, messages[-1]["content"]))
        board = swarm.board
        for task in board.open_tasks_of(name):
            if task.status != Status.IN_PROGRESS:
                continue
            if "planning" in task.tags:
                if len(board.list(include_closed=True)) == 1:
                    return {"tool_calls": [
                        {"name": "task_create", "arguments": {"title": "Парсер", "goal": "CSV читается"}},
                        {"name": "task_create", "arguments": {"title": "CLI", "goal": "команда работает"}},
                    ]}
                return _call("task_complete", task_id=task.id, result="План: #2, #3")
            if not stalled:  # a typical weak-model slip (once): announce the work instead of doing it
                stalled.add(task.id)
                return "Сейчас сделаю."
            return _call("task_complete", task_id=task.id, result="Готово")
        ready = board.ready_unassigned()
        if ready:
            return _call("task_claim", task_id=ready[0].id)
        tasks = board.list(include_closed=True)
        goal = swarm.goal()
        if goal and goal["status"] == "active" and len(tasks) == 3 and all(t.status == Status.DONE for t in tasks):
            return _call("goal_complete", summary="Всё сделано")
        return _call("wait")

    def factory(profile, agent: str) -> ScriptedLLM:
        return ScriptedLLM(lambda system, messages, tools: policy(agent, system, messages), profile)

    swarm = Swarm(config=Config.from_dict(tmp_path / "project", raw), llm_factory=factory)
    holder["swarm"] = swarm
    await swarm.start()
    try:
        swarm.set_goal(USER, "Конвертер CSV в JSON", "с тестами")
        await _until(lambda: (swarm.goal() or {}).get("status") == "done")
        planning = swarm.board.get(1)
        assert planning.title == prompts.PLANNING_TASK["ru"][0] and planning.status == Status.DONE
        # every agent ran on its own frozen Russian system prompt
        for member in swarm.members():
            assert systems[member["name"]] == {member["system_prompt"]}
            assert member["system_prompt"].startswith(f"Ты — {member['name']}, участник роя ИИ-агентов «Рой»")
        texts = [text for _, text in seen]
        wakes = [t for t in texts if t.startswith("===")]
        assert wakes and all(t.startswith(("=== Пробуждение", "=== Новые события")) for t in wakes)
        kickoff = prompts.t("ru", "n_kickoff", by=USER, title="Конвертер CSV в JSON", description="с тестами",
                            task_id=1, task_title=planning.title)
        assert any(kickoff in t for t in wakes)
        nudges = [t for t in texts if t.startswith(prompts.TEXTS["ru"]["nudge"].split("{")[0])]
        assert nudges and all("`task_complete`" in t for t in nudges)
    finally:
        await swarm.stop()
        swarm.close()
