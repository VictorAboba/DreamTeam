"""The agent tool layer (docs/SPEC.md §12-13): every tool is called through `tools.call_tool` against a real
Swarm, for each audience (llm agents, external MCP members, the user)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from dreamteam import prompts, roles, triggers
from dreamteam.config import Config, RoleSettings
from dreamteam.db import loads
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentKind, AgentStatus, Status
from dreamteam.shell import resolve_shell
from dreamteam.swarm import Swarm
from dreamteam.tools import REGISTRY, ToolContext, call_tool, prepare_args, schemas_for, tools_for

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Tool swarm", "language": "en", "start_agents": 2, "max_agents": 6, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "max_steps_per_turn": 10},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    for section, values in overrides.items():
        raw.setdefault(section, {}).update(values)
    return Config.from_dict(tmp_path / "project", raw)


@pytest.fixture
async def swarm(tmp_path):
    sw = Swarm(config=make_config(tmp_path), llm_factory=lambda profile, agent: ScriptedLLM(lambda *a: "ok", profile))
    await sw.start(run_agents=False)
    try:
        yield sw
    finally:
        await sw.stop()
        sw.close()


class FakeRuntime:
    def __init__(self) -> None:
        self.ended = 0
        self.sent_to_user = 0

    def end_turn(self) -> None:
        self.ended += 1

    def note_sent_to_user(self) -> None:
        self.sent_to_user += 1


async def run(sw: Swarm, actor: str, tool: str, args: Any = None, runtime: Any = None) -> tuple[str, bool]:
    return await call_tool(ToolContext(sw, actor, runtime), tool, args)


async def ok(sw: Swarm, actor: str, tool: str, args: Any = None, runtime: Any = None) -> str:
    text, is_error = await run(sw, actor, tool, args, runtime)
    assert not is_error, text
    return text


async def err(sw: Swarm, actor: str, tool: str, args: Any = None) -> str:
    text, is_error = await run(sw, actor, tool, args)
    assert is_error, text
    return text


def llm_members(sw: Swarm) -> list[str]:
    return [m["name"] for m in sw.members() if m["kind"] == AgentKind.LLM]


async def join(sw: Swarm, name: str = "Claude Code", role: str = "Reviewer") -> str:
    await ok(sw, USER, "join_swarm", {"role": role, "name": name})
    return name


def notifications(sw: Swarm, recipient: str) -> list[str]:
    return [r["kind"] for r in sw.db.all("SELECT kind FROM notifications WHERE recipient = ? ORDER BY id", (recipient,))]


# ================================================================== registry and schemas
SPEC_AUDIENCE = {  # docs/SPEC.md §12
    "board": "LEU", "task_get": "LEU", "task_create": "LEU", "task_claim": "LE", "task_update": "LEU",
    "task_complete": "LEU", "task_review": "LEU", "task_comment": "LEU", "task_discussion": "LEU",
    "send_message": "LEU", "read_messages": "LEU", "agents": "LEU", "hire_agent": "LEU", "retire_agent": "LEU",
    "update_notes": "L", "wait": "L", "goal_complete": "LE", "trigger_create": "LEU", "trigger_list": "LEU",
    "trigger_delete": "LEU", "read_file": "LEU", "write_file": "LE", "edit_file": "LE", "list_files": "LEU",
    "lock_file": "LE", "shell": "LE", "jobs": "LEU", "job_stop": "LEU",
    "mcp_create": "LE", "mcp_list": "LEU", "mcp_tools": "LEU", "mcp_call": "LE",
    # join_swarm is called before joining, i.e. as the user ("E (MCP only)")
    "join_swarm": "EU", "wait_for_events": "EU", "set_goal": "U", "swarm_status": "EU", "swarm_control": "U",
    "mcp_approve": "U",
    "agent_traces": "EU",  # docs/SPEC.md §16.2
}
_LETTER = {"L": "llm", "E": "external", "U": "user"}


def test_catalogue_and_audiences_match_the_spec():
    tools_for("llm")
    assert set(REGISTRY) == set(SPEC_AUDIENCE)
    for name, letters in SPEC_AUDIENCE.items():
        assert REGISTRY[name].audience == {_LETTER[c] for c in letters}, name


ALLOWED_KEYWORDS = {"type", "description", "enum", "items", "minimum", "maximum", "properties", "required"}
JSON_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}


def _check_property(where: str, schema: dict) -> None:
    assert isinstance(schema, dict), where
    assert set(schema) <= ALLOWED_KEYWORDS, (where, set(schema) - ALLOWED_KEYWORDS)
    kind = schema.get("type")
    assert kind in JSON_TYPES, (where, kind)   # one plain type: no unions, no missing types
    if "enum" in schema:
        assert schema["enum"] and all(isinstance(v, JSON_TYPES[kind]) for v in schema["enum"]), where
        assert len(set(schema["enum"])) == len(schema["enum"]), where
    if "minimum" in schema or "maximum" in schema:
        assert kind in ("integer", "number"), where
    if kind == "array":
        assert isinstance(schema.get("items"), dict), f"{where}: arrays must declare items"
        _check_property(where + "[]", schema["items"])
    else:
        assert "items" not in schema, where
    if kind == "object":
        props = schema.get("properties", {})
        for key, sub in props.items():
            _check_property(f"{where}.{key}", sub)
        assert set(schema.get("required", [])) <= set(props), where
    else:
        assert "properties" not in schema and "required" not in schema, where


def test_schemas_are_valid_for_anthropic_and_openai():
    jsonschema = pytest.importorskip("jsonschema")
    for kind in ("llm", "external", "user"):
        for lang in ("en", "ru"):
            for item in schemas_for(kind, lang):
                name, params = item["name"], item["parameters"]
                assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name)        # both APIs' tool-name pattern
                assert item["description"].strip() and len(item["description"]) <= 2000, name
                assert params["type"] == "object" and isinstance(params["properties"], dict), name
                assert isinstance(params["required"], list), name
                assert set(params) == {"type", "properties", "required"}, name
                assert len(set(params["required"])) == len(params["required"]), name
                _check_property(name, params)
                jsonschema.Draft202012Validator.check_schema(params)
                json.dumps(item, ensure_ascii=False)                       # plain JSON data


def test_descriptions_exist_in_both_languages_and_name_only_real_tools():
    tools_for("llm")
    params: set[str] = set()

    def collect(schema: dict) -> None:
        for key, sub in schema.get("properties", {}).items():
            params.add(key)
            collect(sub)
            collect(sub.get("items") or {})

    for spec in REGISTRY.values():
        collect(spec.parameters)
    dsl = set(re.findall(r"\b([a-z_]+)\(", triggers.GRAMMAR))
    dsl_kwargs = set(re.findall(r"\b([a-z_]+)=", triggers.GRAMMAR))     # count(status=None, ...)
    known = set(REGISTRY) | params | set(Status.ALL) | dsl | {"as_agent", "my_tools", "snake_case"}
    for spec in REGISTRY.values():
        assert spec.description["en"] and spec.description["ru"], spec.name
        assert spec.description["en"] != spec.description["ru"], spec.name
        for lang in ("en", "ru"):
            words = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", spec.description[lang]))
            assert words <= known, (spec.name, lang, words - known)
        # every parameter a description names exists
        for word in re.findall(r"\b([a-z_]+)=", spec.description["en"]):
            assert word in spec.parameters["properties"] or word in dsl_kwargs | {"as_agent"}, (spec.name, word)
    # tools the system prompts and notifications refer to exist for whoever reads them
    llm_tools = {s.name for s in tools_for("llm")}
    user_tools = {s.name for s in tools_for("user")}
    texts = [("system", prompts.SYSTEM_EN), ("system", prompts.SYSTEM_RU)]
    texts += [(key, text) for lang in prompts.TEXTS for key, text in prompts.TEXTS[lang].items()]
    for key, text in texts:
        # `tool`, `status`, `param` or a trigger call `done(1, 5)`; multi-word code (`git add`) is not a tool
        for ident in re.findall(r"`([a-z_]+)(?=[`(])", text):
            if ident in REGISTRY:
                assert ident in (user_tools if key.startswith("u_") else llm_tools), (key, ident)
            else:
                assert ident in params | set(Status.ALL) | dsl, (key, ident)


def test_schemas_for_is_deterministic_and_returns_copies():
    first = schemas_for("llm", "en")
    assert first == schemas_for("llm", "en")
    assert [s["name"] for s in first] == [s.name for s in REGISTRY.values() if "llm" in s.audience]
    first[0]["parameters"]["properties"]["injected"] = {"type": "string"}
    first[0]["parameters"]["required"].append("injected")
    assert "injected" not in REGISTRY[first[0]["name"]].parameters["properties"]
    assert schemas_for("llm", "en") == schemas_for("llm", "en")
    assert [s["name"] for s in schemas_for("llm", "ru")] == [s["name"] for s in schemas_for("llm", "en")]
    # agent kinds see exactly their audience
    assert {s["name"] for s in schemas_for("user", "en")} == {n for n, a in SPEC_AUDIENCE.items() if "U" in a}


# An LLM tool's schema is part of every agent's frozen prefix: a change is released as a new revision of the frozen
# tool sets (tools/frozen.py), whose digests tests/test_frozen_tools.py pins (RELEASED).


def test_tool_order_does_not_depend_on_import_order():
    # importing a tool module first used to put its tools first: another order in another process
    # changes the tool list and with it the LLM prompt cache prefix
    script = ("import json, sys\n"
              "import dreamteam.tools.{first}\n"
              "from dreamteam.tools import schemas_for\n"
              "from dreamteam.hub_mcp import hub_specs\n"
              "print(json.dumps([[s['name'] for s in schemas_for('llm', 'en')], [s.name for s in hub_specs()]]))\n")
    outputs = []
    for first in ("trigger_tools", "board_tools", "swarm_tools"):
        proc = subprocess.run([sys.executable, "-c", script.format(first=first)], capture_output=True, text=True,
                              timeout=120)
        assert proc.returncode == 0, proc.stderr
        outputs.append(json.loads(proc.stdout.strip().splitlines()[-1]))
    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[0][0][0] == "board"


def test_hub_tool_list_does_not_leak_into_the_registry():
    from dreamteam.hub_mcp import list_hub_tools

    class Stub:
        language = "en"

    tools = {t.name: t for t in list_hub_tools(Stub())}
    assert "as_agent" in tools["board"].input_schema["properties"]
    assert "as_agent" not in tools["join_swarm"].input_schema["properties"]
    assert all("as_agent" not in s.parameters["properties"] for s in REGISTRY.values())
    assert "update_notes" not in tools and "wait" not in tools


# ================================================================== argument handling
async def test_json_strings_for_arrays_and_objects_are_parsed(swarm):
    a, b = llm_members(swarm)
    # regression: '["x", "y"]' was split on commas and stored as the tags '["x"' and '"y"]'
    await ok(swarm, a, "task_create", {"title": "One", "goal": "g", "tags": '["x", "y"]'})
    assert swarm.board.get(1).tags == ["x", "y"]
    await ok(swarm, a, "task_create", {"title": "Two", "goal": "g", "depends_on": "[1]"})
    await ok(swarm, a, "task_create", {"title": "Three", "goal": "g", "depends_on": "#1, 2", "tags": "api, cli"})
    assert swarm.board.get(2).depends_on == [1]
    assert swarm.board.get(3).depends_on == [1, 2] and swarm.board.get(3).tags == ["api", "cli"]
    await ok(swarm, a, "task_create", {"title": "Four", "goal": "g", "depends_on": 3})
    assert swarm.board.get(4).depends_on == [3]

    text = await ok(swarm, a, "send_message", {"to": f'["{b}", "user"]', "text": "hi"})
    assert swarm.mail.get(1)["recipients"] == [b, USER], text
    await ok(swarm, a, "send_message", {"to": b, "text": "single name as a string"})
    assert swarm.mail.get(2)["recipients"] == [b]

    text = await ok(swarm, a, "trigger_create", {"condition": "done(1)", "actions": '[{"type": "wake"}]'})
    assert "Trigger #1 created" in text
    await ok(swarm, a, "trigger_create", {"condition": "done(2)", "actions": {"type": "message", "to": [b], "text": "x"}})
    assert swarm.triggers.get(2)["actions"] == [{"type": "message", "to": [b], "text": "x"}]


async def test_mcp_call_accepts_arguments_as_a_json_string(swarm, monkeypatch):
    a = llm_members(swarm)[0]
    seen: list[Any] = []

    async def fake_call(actor, server, tool, arguments=None, timeout=None, max_chars=None):
        seen.append(arguments)
        return "42", False

    monkeypatch.setattr(swarm.forge, "call", fake_call)
    assert await ok(swarm, a, "mcp_call", {"server": "calc", "tool": "add", "arguments": '{"a": 1, "b": 2}'}) == "42"
    assert await ok(swarm, a, "mcp_call", {"server": "calc", "tool": "add"}) == "42"
    assert seen == [{"a": 1, "b": 2}, {}]
    text = await err(swarm, a, "mcp_call", {"server": "calc", "tool": "add", "arguments": "a=1"})
    assert "'arguments' must be a JSON object" in text


async def test_scalar_coercion(swarm):
    a, b = llm_members(swarm)
    await ok(swarm, USER, "task_create", {"title": "T", "goal": "g", "priority": "3", "reviewer": b})
    assert swarm.board.get(1).priority == 3
    assert "Task #1" in await ok(swarm, a, "task_get", {"task_id": "#1"})
    assert "Task #1" in await ok(swarm, a, "task_get", {"task_id": 1.0})
    await ok(swarm, a, "task_claim", {"task_id": " 1 "})
    await ok(swarm, a, "task_update", {"task_id": 1, "status": "In Progress"})   # enum, case/space tolerant
    await ok(swarm, a, "task_update", {"task_id": 1, "status": "WAITING"})
    assert swarm.board.get(1).status == Status.WAITING
    await ok(swarm, a, "task_complete", {"task_id": 1, "result": 42})              # numbers become text
    assert swarm.board.get(1).result == "42"
    await ok(swarm, b, "task_review", {"task_id": 1, "approve": 0, "comment": "redo"})
    assert swarm.board.get(1).status == Status.IN_PROGRESS
    await ok(swarm, a, "task_complete", {"task_id": 1, "result": "again"})
    await ok(swarm, b, "task_review", {"task_id": 1, "approve": "yes"})
    assert swarm.board.get(1).status == Status.DONE
    await ok(swarm, USER, "swarm_control", {"action": "PAUSE"})
    assert swarm.paused
    await ok(swarm, USER, "swarm_control", {"action": "resume", "max_cost_usd": "12.5"})
    assert not swarm.paused and swarm.limits()["max_cost_usd"] == 12.5


async def test_type_errors_say_how_to_fix_the_call(swarm):
    a = llm_members(swarm)[0]
    assert "'task_id' must be an integer, e.g. 5" in await err(swarm, a, "task_get", {"task_id": "first"})
    assert "'approve' must be true or false" in await err(swarm, a, "task_review", {"task_id": 1, "approve": "maybe"})
    assert "must be an array" in await err(swarm, a, "task_create", {"title": "t", "goal": "g", "tags": {"a": 1}})
    text = await err(swarm, a, "trigger_create", {"condition": "done(1)", "actions": '[{"type": wake}]'})
    assert "'actions' must be an array" in text
    text = await err(swarm, USER, "swarm_control", {"action": "stop", "max_agents": 3})
    assert "'action' must be one of: pause, resume" in text
    assert swarm.limits()["max_agents"] == 6          # nothing was applied
    assert "'max_cost_usd' must be a number" in await err(swarm, USER, "swarm_control", {"max_cost_usd": "nan"})
    assert "'max_cost_usd' must be a number" in await err(swarm, USER, "swarm_control", {"max_cost_usd": float("inf")})
    assert swarm.limits()["max_cost_usd"] == 0
    text = await err(swarm, a, "task_update", {"task_id": 1, "status": "finished"})
    assert "must be one of: todo, in_progress" in text
    text = await err(swarm, a, "board", ["status", "todo"])
    assert "must be a JSON object" in text and "board(status?" in text


async def test_required_arguments_and_allowed_empty_strings(swarm):
    a = llm_members(swarm)[0]
    await ok(swarm, a, "write_file", {"path": "keep.txt", "content": "precious\n"})
    # regression: a call without content (e.g. a truncated tool call) used to wipe the file
    text = await err(swarm, a, "write_file", {"path": "keep.txt"})
    assert "Missing required argument(s) for write_file: content" in text and "write_file(path, content)" in text
    assert (swarm.workspace.root / "keep.txt").read_text(encoding="utf-8") == "precious\n"
    # ...and edit_file without new_text deleted the matched text
    assert "new_text" in await err(swarm, a, "edit_file", {"path": "keep.txt", "old_text": "precious"})
    assert (swarm.workspace.root / "keep.txt").read_text(encoding="utf-8") == "precious\n"
    # empty strings are fine where they mean something
    await ok(swarm, a, "write_file", {"path": "pkg/__init__.py", "content": ""})
    assert (swarm.workspace.root / "pkg" / "__init__.py").read_bytes() == b""
    await ok(swarm, a, "edit_file", {"path": "keep.txt", "old_text": "precious", "new_text": ""})
    assert (swarm.workspace.root / "keep.txt").read_text(encoding="utf-8") == "\n"
    await ok(swarm, a, "update_notes", {"content": "remember"})
    assert await ok(swarm, a, "update_notes", {"content": ""}) == "Notes cleared."
    assert swarm.member(a)["notes"] == ""
    # ...but not for ordinary required text
    assert "Missing required argument(s) for shell: command" in await err(swarm, a, "shell", {"command": "   "})
    assert "title" in await err(swarm, a, "task_create", {"title": "", "goal": "g"})
    assert "goal" in await err(swarm, a, "task_create", {"title": "No goal"})
    assert swarm.board.list(include_closed=True) == []


async def test_unknown_arguments_are_reported_not_silently_dropped(swarm):
    a = llm_members(swarm)[0]
    await ok(swarm, USER, "task_create", {"title": "Open", "goal": "g"})
    text = await ok(swarm, a, "board", {"state": "done"})
    assert "#1 [todo] Open" in text
    assert "Ignored unknown argument(s): state" in text and "board(status?, assignee?, tag?, include_closed?)" in text
    text = await err(swarm, a, "task_update", {"task_id": 1, "assigned_to": a})
    assert "Nothing to change" in text and "Ignored unknown argument(s): assigned_to" in text


async def test_unknown_tool_and_audience_errors(swarm):
    a = llm_members(swarm)[0]
    text, is_error = await run(swarm, a, "task_lst")
    assert is_error and "Unknown tool 'task_lst'" in text and "Did you mean" in text and "board" in text
    text, is_error = await run(swarm, USER, "task_claim", {"task_id": 1})
    assert is_error and "not available to the user" in text and "join_swarm" in text
    ext = await join(swarm)
    text, is_error = await run(swarm, ext, "set_goal", {"title": "x"})
    assert is_error and "not available to external members" in text and "omit as_agent" in text
    text, is_error = await run(swarm, a, "wait_for_events", {})
    assert is_error and "not available to swarm agents" in text
    text, is_error = await run(swarm, ext, "update_notes", {"content": "x"})
    assert is_error and "not available" in text


async def test_only_active_members_and_the_user_can_act(swarm):
    a, b = llm_members(swarm)
    # regression: an unknown name was treated as the user and could e.g. set the goal
    text = await err(swarm, "Ghost Person", "set_goal", {"title": "pwn"})
    assert "not a member of this swarm" in text and swarm.goal() is None
    await ok(swarm, USER, "retire_agent", {"name": b})
    text = await err(swarm, b, "task_create", {"title": "late", "goal": "g"})
    assert "has left the swarm" in text and swarm.board.list() == []
    # an agent that retires itself cannot keep acting with the remaining calls of its response
    await ok(swarm, a, "retire_agent", {})
    assert "has left the swarm" in await err(swarm, a, "board")


def test_prepare_args_is_the_validation_entry_point():
    tools_for("llm")
    spec = REGISTRY["task_create"]
    args = prepare_args(spec, {"title": "T", "goal": "G", "priority": "2", "extra": 1, "tags": "a,b"})
    assert args == {"title": "T", "goal": "G", "priority": 2, "tags": ["a", "b"]}


# ================================================================== board tools
async def test_board_filters_and_output(swarm):
    a, b = llm_members(swarm)
    await ok(swarm, USER, "set_goal", {"title": "Ship the CLI"})                  # creates planning task #1
    await ok(swarm, a, "task_create", {"title": "Parser", "goal": "CSV is read", "tags": ["api"], "assignee": "me"})
    await ok(swarm, a, "task_create", {"title": "Docs", "goal": "README", "assignee": b, "priority": 0})
    await ok(swarm, USER, "task_update", {"task_id": 3, "status": "done"})
    text = await ok(swarm, a, "board")
    assert text.startswith("Global goal [active]: Ship the CLI")
    assert "Counts: todo=2, done=1" in text
    assert f"#2 [todo] Parser @{a} tags:api\n    goal: CSV is read" in text and "Docs" not in text
    assert "Docs" in await ok(swarm, a, "board", {"include_closed": True})
    mine = await ok(swarm, a, "board", {"assignee": "me"})
    assert "Parser" in mine and "Decompose" not in mine
    assert "Docs" in await ok(swarm, a, "board", {"status": "done, todo"})
    assert "No matching tasks." in await ok(swarm, a, "board", {"tag": "nothing"})
    assert "Unknown status 'closed'" in await err(swarm, a, "board", {"status": "closed"})
    assert "Unknown member" in await err(swarm, a, "board", {"assignee": "Nobody Here"})
    # closed tasks of a retired member stay searchable
    await ok(swarm, USER, "retire_agent", {"name": b})
    assert "Docs" in await ok(swarm, USER, "board", {"assignee": b, "include_closed": True})


async def test_board_stays_compact_for_long_lists(swarm):
    for i in range(1, 31):
        swarm.board.create(USER, f"Task {i}", goal=f"goal number {i}")
    text = await ok(swarm, USER, "board")
    assert "goal number" not in text and "goals are omitted" in text and "#30 [todo] Task 30" in text
    for i in range(31, 106):
        swarm.board.create(USER, f"Task {i}", goal="g")
    text = await ok(swarm, USER, "board")
    assert "#100 [todo]" in text and "#101 [todo]" not in text
    assert "... and 5 more tasks" in text
    assert len(text) < 6000


async def test_task_get_shows_structure_and_latest_posts(swarm):
    a, b = llm_members(swarm)
    await ok(swarm, a, "task_create", {"title": "Epic", "goal": "everything", "description": "Long text"})
    await ok(swarm, a, "task_create", {"title": "Part", "goal": "a part", "parent_id": 1})
    await ok(swarm, a, "task_create", {"title": "Later", "goal": "after", "depends_on": [1]})
    for i in range(7):
        await ok(swarm, b, "task_comment", {"task_id": 1, "text": f"post {i}"})
    text = await ok(swarm, a, "task_get", {"task_id": 1})
    assert "Task #1: Epic" in text and "Goal: everything" in text and "Description:\nLong text" in text
    assert "Subtasks: #2 [todo] Part" in text and "Required by (open): #3 [todo]" in text
    assert "Discussion: 7 post(s)" in text and "post 6" in text and "post 1" not in text
    assert "Parent: #1" in await ok(swarm, a, "task_get", {"task_id": 2})
    assert "Task #9 not found" in await err(swarm, a, "task_get", {"task_id": 9})


async def test_task_create_options(swarm):
    a, b = llm_members(swarm)
    text = await ok(swarm, a, "task_create", {"title": "Build", "goal": "binary", "assignee": b, "reviewer": "me",
                                               "priority": 2, "parent_id": 0, "depends_on": []})
    assert text == f"Created task #1 [todo] Build @{b} (high)"
    task = swarm.board.get(1)
    assert task.reviewer == a and task.parent_id is None and task.created_by == a
    assert "n_assigned" in notifications(swarm, b)
    assert "Parent task #7 not found" in await err(swarm, a, "task_create", {"title": "x", "goal": "g", "parent_id": 7})
    assert "Invalid priority" in await err(swarm, a, "task_create", {"title": "x", "goal": "g", "priority": 9})
    assert "Unknown task ids in depends_on: #5" in await err(swarm, a, "task_create",
                                                              {"title": "x", "goal": "g", "depends_on": [5]})
    await ok(swarm, USER, "task_create", {"title": "For the user", "goal": "g", "assignee": "me"})
    assert swarm.board.get(2).assignee == USER


async def test_task_update_reports_what_changed(swarm):
    a, b = llm_members(swarm)
    await ok(swarm, a, "task_create", {"title": "Epic", "goal": "g"})
    await ok(swarm, a, "task_create", {"title": "Work", "goal": "g"})
    await ok(swarm, a, "task_claim", {"task_id": 2})
    text = await ok(swarm, a, "task_update", {"task_id": 2, "status": "waiting", "priority": 3, "tags": ["x"],
                                               "parent_id": 1, "comment": "blocked by the API"})
    assert text.startswith("Updated task #2: status in_progress → waiting; priority normal → critical; "
                           "tags - → x; parent_id - → #1.")
    assert "Posted comment #1 in its discussion." in text
    assert swarm.board.discussion(2)[0]["text"] == "blocked by the API"
    # no-op updates say so instead of claiming an update
    assert "No changes: task #2 already has these values." in await ok(swarm, a, "task_update",
                                                                       {"task_id": 2, "priority": 3})
    assert "No fields changed" in await ok(swarm, a, "task_update", {"task_id": 2, "comment": "note"})
    # parent_id 0 detaches; goal/description changes are named
    text = await ok(swarm, a, "task_update", {"task_id": 2, "parent_id": 0, "goal": "new goal"})
    assert "parent_id #1 → -" in text and "goal changed" in text and swarm.board.get(2).parent_id is None
    # assignee "" unassigns, "me" assigns oneself
    text = await ok(swarm, a, "task_update", {"task_id": 2, "assignee": ""})
    assert f"assignee {a} → -" in text and "status waiting → todo" in text
    await ok(swarm, b, "task_update", {"task_id": 2, "assignee": "me"})
    assert swarm.board.get(2).assignee == b
    # permissions and finishing rules come from the board, with hints
    assert "Only the creator" in await err(swarm, b, "task_update", {"task_id": 1, "title": "Mine now"})
    assert "task_complete" in await err(swarm, b, "task_update", {"task_id": 2, "status": "done"})
    assert "task_complete" in await err(swarm, b, "task_update", {"task_id": 2, "status": "review"})
    assert "Nothing to change" in await err(swarm, b, "task_update", {"task_id": 2})
    # an oversized comment is rejected before anything changes
    text = await err(swarm, b, "task_update", {"task_id": 2, "status": "failed", "comment": "x" * 20001})
    assert "comment is too long" in text and swarm.board.get(2).status == Status.TODO
    # the user may close a task directly
    assert "status todo → done" in await ok(swarm, USER, "task_update", {"task_id": 2, "status": "done"})


async def test_claim_complete_and_review_flow(swarm):
    a, b = llm_members(swarm)
    ext = await join(swarm)
    await ok(swarm, USER, "task_create", {"title": "Feature", "goal": "works", "reviewer": b})
    await ok(swarm, USER, "task_create", {"title": "Next", "goal": "after", "depends_on": [1]})
    assert "is blocked by unfinished dependencies: #1 [todo]" in await err(swarm, a, "task_claim", {"task_id": 2})
    assert "You now work on #1 [in_progress] Feature" in await ok(swarm, a, "task_claim", {"task_id": 1})
    assert f"already assigned to {a}" in await err(swarm, ext, "task_claim", {"task_id": 1})
    assert "Only the assignee" in await err(swarm, ext, "task_complete", {"task_id": 1, "result": "x"})
    assert "Missing required argument(s) for task_complete: result" in await err(
        swarm, a, "task_complete", {"task_id": 1, "result": " \n"})
    assert await ok(swarm, a, "task_complete", {"task_id": 1, "result": "done in src/"}) == \
        f"Task #1 was sent to {b} for review."
    assert "n_review_requested" in notifications(swarm, b)
    assert "Only the reviewer" in await err(swarm, ext, "task_review", {"task_id": 1, "approve": True})
    text = await ok(swarm, b, "task_review", {"task_id": 1, "approve": False, "comment": "add tests"})
    assert text == f"Task #1 is now in_progress (assignee: {a})."
    await ok(swarm, a, "task_complete", {"task_id": 1, "result": "with tests"})
    assert await ok(swarm, b, "task_review", {"task_id": 1, "approve": True}) == f"Task #1 is now done (assignee: {a})."
    assert "n_unblocked" not in notifications(swarm, ext)
    await ok(swarm, ext, "task_claim", {"task_id": 2})
    # no reviewer: the board picks an agent that neither created (the user did) nor did it; both are idle and free
    picked = min(a, b)
    assert await ok(swarm, ext, "task_complete", {"task_id": 2, "result": "ok"}) == \
        f"Task #2 was sent to {picked} for review: a member other than you checks it, and you will be told the verdict."
    # the user may complete and review anything
    await ok(swarm, USER, "task_create", {"title": "Mine", "goal": "g"})
    assert await ok(swarm, USER, "task_complete", {"task_id": 3, "result": "did it myself"}) == "Task #3 is done."


async def test_task_complete_texts_in_independent_mode(swarm):
    """The result of task_complete says where the work went (SPEC §4 "Independent review")."""
    a, b = llm_members(swarm)
    ext = await join(swarm)
    # the task's own reviewer: the text is unchanged
    await ok(swarm, USER, "task_create", {"title": "Explicit", "goal": "g", "reviewer": ext})
    await ok(swarm, a, "task_claim", {"task_id": 1})
    assert await ok(swarm, a, "task_complete", {"task_id": 1, "result": "r"}) == f"Task #1 was sent to {ext} for review."
    # no reviewer: the board picks another agent (here the creator, the only one left)
    await ok(swarm, a, "task_create", {"title": "Picked", "goal": "g"})
    await ok(swarm, b, "task_claim", {"task_id": 2})
    assert await ok(swarm, b, "task_complete", {"task_id": 2, "result": "r"}) == \
        f"Task #2 was sent to {a} for review: a member other than you checks it, and you will be told the verdict."
    assert "n_review_requested" in notifications(swarm, a)
    await ok(swarm, a, "task_review", {"task_id": 2, "approve": True})
    # nobody else can review (external members are never picked): the user
    await ok(swarm, USER, "retire_agent", {"name": a})
    await ok(swarm, USER, "task_create", {"title": "Alone", "goal": "g"})
    await ok(swarm, b, "task_claim", {"task_id": 3})
    assert await ok(swarm, b, "task_complete", {"task_id": 3, "result": "r"}) == (
        "Task #3 was sent to the user for review: nobody else in the team can review it. You will be told the "
        "verdict; meanwhile take the next task or call wait.")
    assert swarm.board.get(3).reviewer == USER and "u_review_fallback" in notifications(swarm, USER)
    assert "your own work is reviewed by someone else (the user)" in await err(
        swarm, b, "task_review", {"task_id": 3, "approve": True})
    assert await ok(swarm, USER, "task_review", {"task_id": 3, "approve": True}) == \
        f"Task #3 is now done (assignee: {b})."


async def test_claiming_as_designated_reviewer_says_so(swarm):
    a, b = llm_members(swarm)
    await ok(swarm, USER, "task_create", {"title": "Review me", "goal": "g", "reviewer": a})
    text = await ok(swarm, a, "task_claim", {"task_id": 1})
    assert text == ("You now work on #1 [in_progress] Review me @" + a + "\n    goal: g\n"
                    "You were its reviewer; since you execute it now, another member will review it.")
    assert swarm.board.get(1).reviewer is None
    assert (await ok(swarm, a, "task_complete", {"task_id": 1, "result": "r"})).startswith(
        f"Task #1 was sent to {b} for review: a member other than you")
    await ok(swarm, USER, "task_create", {"title": "Plain", "goal": "g"})
    assert "You were its reviewer" not in await ok(swarm, b, "task_claim", {"task_id": 2})


async def test_goal_complete_waits_for_reviews(swarm):
    a, b = llm_members(swarm)
    await ok(swarm, USER, "set_goal", {"title": "Ship"})                       # planning task #1
    await ok(swarm, a, "task_claim", {"task_id": 1})
    await ok(swarm, a, "task_complete", {"task_id": 1, "result": "planned"})   # reviewed by b
    text = await err(swarm, a, "goal_complete", {"summary": "done"})
    assert text.startswith(f"Error: Tasks are still waiting for review: #1 ({b}). The goal can be completed only "
                           "after every task is reviewed: call `wait`")
    assert swarm.goal()["status"] == "active" and "u_goal_done" not in notifications(swarm, USER)
    await ok(swarm, USER, "task_review", {"task_id": 1, "approve": True})
    assert (await ok(swarm, a, "goal_complete", {"summary": "done"})).startswith("The global goal is marked as achieved")
    # without independent review a task in review does not hold the goal back (the switch applies at once)
    await ok(swarm, USER, "swarm_control", {"reopen_goal_comment": "one more thing"})
    await ok(swarm, USER, "task_create", {"title": "More", "goal": "g", "reviewer": b})
    await ok(swarm, a, "task_claim", {"task_id": 2})
    await ok(swarm, a, "task_complete", {"task_id": 2, "result": "r"})
    assert "waiting for review" in await err(swarm, a, "goal_complete", {"summary": "done again"})
    swarm.config.swarm.independent_review = False
    text = await ok(swarm, a, "goal_complete", {"summary": "done again"})
    assert "1 task(s) are still open (#2)" in text


async def test_task_comment_and_discussion(swarm):
    a, b = llm_members(swarm)
    await ok(swarm, a, "task_create", {"title": "API", "goal": "agreed"})
    assert await ok(swarm, a, "task_comment", {"task_id": 1, "text": f"@{b} please look", "reply_to": 0}) == \
        "Posted #1 in the discussion of task #1."
    assert "n_mention" in notifications(swarm, b)
    await ok(swarm, b, "task_comment", {"task_id": 1, "text": "looks fine", "reply_to": 1})
    text = await ok(swarm, a, "task_discussion", {"task_id": 1})
    assert 'Discussion of task #1 "API" (2 post(s)):' in text and "(reply to #1)" in text
    assert "please look" not in await ok(swarm, a, "task_discussion", {"task_id": 1, "since_id": 1})
    assert "reply_to must be the id of a post of the same task" in await err(
        swarm, a, "task_comment", {"task_id": 1, "text": "x", "reply_to": 99})
    await ok(swarm, a, "task_create", {"title": "Quiet", "goal": "g"})
    assert await ok(swarm, a, "task_discussion", {"task_id": 2}) == 'Discussion of task #2 "Quiet" (0 post(s)): empty.'


# ================================================================== messages
async def test_send_and_read_messages(swarm):
    a, b = llm_members(swarm)
    ext = await join(swarm)
    runtime = FakeRuntime()
    assert await ok(swarm, a, "send_message", {"to": ["all"], "text": "hello team"}) == \
        f"Sent message #1 to {b}, {ext}."
    await ok(swarm, a, "send_message", {"to": "все", "text": "ru alias"})
    await ok(swarm, a, "send_message", {"to": ["user"], "text": "report"}, runtime=runtime)
    assert runtime.sent_to_user == 1
    await ok(swarm, a, "send_message", {"to": [b], "text": "no user"}, runtime=runtime)
    assert runtime.sent_to_user == 1
    assert "only to yourself" in await err(swarm, a, "send_message", {"to": [a], "text": "x"})
    assert "Unknown member" in await err(swarm, a, "send_message", {"to": ["Nobody Here"], "text": "x"})
    assert "Missing required argument(s) for send_message: text" in await err(
        swarm, a, "send_message", {"to": [b], "text": " \t"})
    await ok(swarm, b, "send_message", {"to": [a], "text": "reply", "reply_to": 1})
    await ok(swarm, b, "send_message", {"to": [a], "text": "no reply", "reply_to": 0})
    assert swarm.mail.get(6)["reply_to"] is None

    text = await ok(swarm, b, "read_messages")
    assert text.startswith("Unread messages (3):") and "hello team" in text and f"{a} → you" in text
    assert await ok(swarm, b, "read_messages") == "Unread messages: none."
    text = await ok(swarm, b, "read_messages", {"with": a})
    assert text.startswith(f"Conversation with {a} (") and "reply" in text and "report" not in text
    assert "(4)" in await ok(swarm, b, "read_messages", {"include_read": True, "limit": "4"})
    assert "That is you" in await err(swarm, b, "read_messages", {"with": b})
    # the user reads its own inbox
    text = await ok(swarm, USER, "read_messages")
    assert "report" in text
    await ok(swarm, USER, "send_message", {"to": [a], "text": "from the human"})
    assert "from the human" in await ok(swarm, a, "read_messages", {"with": "user"})


# ================================================================== files and console
async def test_file_tools(swarm):
    a, b = llm_members(swarm)
    assert await ok(swarm, a, "write_file", {"path": "src/app.py", "content": "print('hi')\nx = 1\n"}) == \
        "Created src/app.py (2 lines, 18 B)."
    assert "Updated src/app.py" in await ok(swarm, a, "write_file", {"path": "src/app.py",
                                                                      "content": "print('hi')\nx = 2\n"})
    text = await ok(swarm, b, "read_file", {"path": "src/app.py", "offset": 2, "limit": "1"})
    assert text == "[src/app.py: lines 2-2 of 2]\nx = 2"
    assert "Edited src/app.py: replaced 1 occurrence" in await ok(
        swarm, b, "edit_file", {"path": "src/app.py", "old_text": "x = 2", "new_text": "x = 3"})
    assert "old_text was not found" in await err(swarm, b, "edit_file", {"path": "src/app.py", "old_text": "nope",
                                                                         "new_text": ""})
    text = await ok(swarm, a, "list_files", {"depth": 9})
    assert "src/" in text and "app.py (18 B)" in text
    assert "outside the workspace" in await err(swarm, a, "read_file", {"path": "../secret.txt"})
    assert "File not found" in await err(swarm, a, "read_file", {"path": "missing.txt"})
    # locks
    assert "Locked src/app.py" in await ok(swarm, a, "lock_file", {"path": "src/app.py", "minutes": "5"})
    text = await err(swarm, b, "write_file", {"path": "src/app.py", "content": "mine"})
    assert f"locked by {a}" in text
    assert "not by you" in await err(swarm, b, "lock_file", {"path": "src/app.py", "unlock": True})
    assert await ok(swarm, a, "lock_file", {"path": "src/app.py", "unlock": "true"}) == "Unlocked src/app.py."
    await ok(swarm, b, "write_file", {"path": "src/app.py", "content": "mine"})
    # the user reads files but does not write them
    assert "mine" in await ok(swarm, USER, "read_file", {"path": "src/app.py"})
    assert "not available to the user" in await err(swarm, USER, "write_file", {"path": "x", "content": "y"})


async def test_shell_tool(swarm):
    a = llm_members(swarm)[0]
    shell_name = resolve_shell(swarm.config.shell.program)[0]
    text = await ok(swarm, a, "shell", {"command": "echo hello"})
    assert text.startswith(f"exit code: 0 [{shell_name}]\n") and "hello" in text
    sleep = "Start-Sleep -Seconds 5" if shell_name in ("powershell", "pwsh") else "sleep 5"
    # still running at its timeout: the command goes on as a background job (docs/SPEC.md §7.1)
    text = await ok(swarm, a, "shell", {"command": sleep, "timeout": "1"})
    assert text.startswith(f"STILL RUNNING after 1s: continues in the background as job j1 [{shell_name}]\n"), text
    assert swarm.jobs.get(1)["status"] == "running" and "job_stop stops it" in text
    assert (await ok(swarm, USER, "job_stop", {"job_id": "j1"})).startswith(f"Stopped job j1 ({sleep}) of {a} after ")
    assert swarm.jobs.get(1)["status"] == "stopped"
    # without background_on_timeout it is killed at its timeout: exactly the old result
    swarm.config.shell.background_on_timeout = False
    text = await ok(swarm, a, "shell", {"command": sleep, "timeout": "1"})
    assert text.startswith(f"TIMEOUT after 1s (process tree killed) [{shell_name}]\n") and "[It was" not in text
    assert swarm.jobs.get(2) is None
    text = await ok(swarm, a, "shell", {"command": "exit 3"})
    assert text.startswith("exit code: 3") and "(no output)" in text


# ================================================================== team, goal and control
async def test_agents_and_status_show_the_budget_honestly(swarm):
    a, b = llm_members(swarm)
    text = await ok(swarm, a, "agents")
    # regression: an unlimited budget (0) was shown as "Spent $0.00 of $0.00"
    assert text.startswith("AI agents: 2 of max 6. Spent $0.00 (no budget limit).")
    assert f"- {a} (you): Generalist [idle], model default, tasks: none" in text
    ext = await join(swarm)
    await ok(swarm, USER, "task_create", {"title": "Mine", "goal": "g", "assignee": ext})
    await ok(swarm, USER, "swarm_control", {"max_cost_usd": 3})
    swarm.db.execute("UPDATE agents SET cost_usd = 1.25 WHERE name = ?", (a,))
    text = await ok(swarm, b, "agents")
    assert "Spent $1.25 of $3.00." in text and f"{ext}: Reviewer [idle, external], tasks: #1" in text
    assert text.startswith("AI agents: 2 of max 6, plus 1 external member (not counted).")
    text = await ok(swarm, USER, "swarm_status")
    assert 'Swarm "Tool swarm" — running' in text
    assert "Spent $1.25 of $3.00. AI agents: 2 of max 6, plus 1 external member (not counted)." in text
    assert "Global goal: not set." in text and "Tasks: todo=1" in text
    text = await ok(swarm, ext, "swarm_status")
    assert f"You are {ext}. Your open tasks: #1 [todo] Mine" in text
    assert "New events are pending: call wait_for_events." in text      # the assignment notification
    await ok(swarm, ext, "wait_for_events", {"timeout": 0})
    assert "pending" not in await ok(swarm, ext, "swarm_status")


async def test_agents_lists_the_role_catalog(swarm):
    """The `agents` output carries the role catalog (docs/SPEC.md §17.3): what hire_agent offers, in the caller's
    language, with the first sentence of each description; hidden roles are not offered."""
    a = llm_members(swarm)[0]
    text = await ok(swarm, a, "agents")
    assert ("\nRole catalog (hire_agent role=<name>; a catalog role brings its description and model profile, and "
            "instructions may then be empty; any other name is a new role you describe in instructions):\n") in text
    assert ("\n- Generalist: You take whichever task is most useful now: code, texts, analysis, research or "
            "checks.\n") in text
    assert "\n- Researcher: " in text and "(model" not in text
    swarm.config.models["fast"] = dataclasses.replace(swarm.config.models["default"], name="fast")
    swarm.config.roles = [RoleSettings(name="tester", model="fast"), RoleSettings(name="researcher", hidden=True),
                          RoleSettings(name="Data engineer", description="You build data pipelines. Mostly ETL.")]
    text = await ok(swarm, a, "agents")
    assert "\n- Tester (model fast): You check that what others built really works" in text
    assert "Researcher" not in text
    assert text.endswith("\n- Data engineer: You build data pipelines.")
    swarm.db.execute("UPDATE agents SET language = 'ru' WHERE name = ?", (a,))
    assert "\n- Универсал: Ты берёшь ту задачу, которая сейчас полезнее всего" in await ok(swarm, a, "agents")


async def test_hire_and_retire(swarm):
    a, b = llm_members(swarm)
    text = await ok(swarm, a, "hire_agent", {"role": "QA engineer", "instructions": "Test it", "name": "Grace Hopper"})
    assert text.startswith('Hired Grace Hopper as "QA engineer". "QA engineer" is a new role (not in the role '
                           'catalog): your instructions are all they know about it.')
    member = swarm.member("Grace Hopper")
    assert member["hired_by"] == a and member["kind"] == AgentKind.LLM and "n_hired" in notifications(swarm, "Grace Hopper")
    assert "Unknown model profile 'gpt'" in await err(swarm, a, "hire_agent", {"role": "x", "instructions": "y",
                                                                              "model": "gpt"})
    assert "already taken" in await err(swarm, a, "hire_agent", {"role": "x", "instructions": "y",
                                                                "name": "Grace Hopper"})
    assert "Missing required" in await err(swarm, a, "hire_agent", {"role": "Dev"})   # the schema is unchanged...
    assert REGISTRY["hire_agent"].allow_empty == {"instructions"}   # ...but "" passes: a catalog role needs none
    text = await err(swarm, a, "hire_agent", {"role": "Dev", "instructions": ""})
    assert text.startswith("Error: 'Dev' is not in the role catalog: describe the new role's responsibilities in "
                           "instructions, or pick a catalog role (`agents` lists them).")
    assert "Similar catalog roles: Tester." in await err(swarm, a, "hire_agent", {"role": "Testr", "instructions": ""})
    text = await ok(swarm, a, "hire_agent", {"role": " tester ", "instructions": "", "name": "Alan Turing"})
    assert text == ('Hired Alan Turing as "Tester". They got the catalog description of the role; model profile '
                    'default. They are woken now: assign them a task (task_update assignee) or leave ready tasks for '
                    'them to claim; no welcome message is needed.')
    assert swarm.member("Alan Turing")["role"] == "Tester"
    assert ("n_hired", "Tester") in [(r["kind"], loads(r["data"])["role"]) for r in swarm.db.all(
        "SELECT kind, data FROM notifications WHERE recipient = 'Alan Turing'")]
    await ok(swarm, a, "retire_agent", {"name": "Alan Turing"})
    # the user (the board's hire form, the TUI, the console) gets texts about the user's own tools
    text = await err(swarm, USER, "hire_agent", {"role": "Dev", "instructions": ""})
    assert text == ("Error: 'Dev' is not in the role catalog: describe the new role in the instructions (in the "
                    "terminal: /hire Dev | instructions), or pick a catalog role (the list of the board's hire form, "
                    "/roles in the terminal).")
    assert await ok(swarm, USER, "hire_agent", {"role": "tester", "instructions": "", "name": "Ada King"}) ==         'Hired Ada King as "Tester" (catalog role, model profile default).'
    assert await ok(swarm, USER, "hire_agent", {"role": "Data analyst", "instructions": "Charts", "name": "Ed Codd"}) ==         ('Hired Ed Codd as "Data analyst": a new role, not in the role catalog; your instructions are all they know '
         'about it. Similar catalog roles: Analyst.')
    for hired in ("Ada King", "Ed Codd"):
        await ok(swarm, USER, "retire_agent", {"name": hired})
    # retire: hirer or self or the user
    assert "Only the user, Grace Hopper or whoever hired them" in await err(swarm, b, "retire_agent",
                                                                             {"name": "Grace Hopper"})
    await ok(swarm, USER, "task_create", {"title": "Hers", "goal": "g", "assignee": "Grace Hopper"})
    assert await ok(swarm, a, "retire_agent", {"name": "grace hopper", "reason": "done"}) == \
        "Grace Hopper left the swarm. Released tasks: #1."
    assert "Pass the name" in await err(swarm, USER, "retire_agent", {})
    assert "cannot be used here" in await err(swarm, USER, "retire_agent", {"name": "user"})
    assert await ok(swarm, b, "retire_agent", {}) == f"{b} left the swarm."
    assert swarm.member(b)["status"] == AgentStatus.RETIRED
    # the member limit
    await ok(swarm, USER, "swarm_control", {"max_agents": 1})
    assert "maximum of 1 AI agents" in await err(swarm, a, "hire_agent", {"role": "x", "instructions": "y"})


async def test_update_notes_and_wait(swarm):
    a = llm_members(swarm)[0]
    assert await ok(swarm, a, "update_notes", {"content": "  plan: A then B  "}) == "Notes saved (14 characters)."
    assert swarm.member(a)["notes"] == "plan: A then B"
    assert "limited to 20000" in await err(swarm, a, "update_notes", {"content": "x" * 20001})
    runtime = FakeRuntime()
    assert "waiting" in await ok(swarm, a, "wait", {"reason": "nothing to do"}, runtime=runtime)
    assert runtime.ended == 1


async def test_goal_complete(swarm):
    a, b = llm_members(swarm)
    assert "no global goal" in await err(swarm, a, "goal_complete", {"summary": "x"})
    await ok(swarm, USER, "set_goal", {"title": "Ship"})
    text = await ok(swarm, a, "goal_complete", {"summary": "Shipped: see dist/"})
    assert text.startswith("The global goal is marked as achieved") and "1 task(s) are still open (#1)" in text
    assert swarm.goal()["status"] == "done" and notifications(swarm, USER).count("u_goal_done") == 1
    # regression: a second call notified the user again
    assert "already marked as achieved" in await err(swarm, b, "goal_complete", {"summary": "again"})
    assert notifications(swarm, USER).count("u_goal_done") == 1
    assert "not available to the user" in await err(swarm, USER, "goal_complete", {"summary": "x"})


async def test_join_swarm(swarm):
    """SPEC §12-13: the MCP client acts as the user until it joins; then it is an external member."""
    a = llm_members(swarm)[0]
    text = await ok(swarm, USER, "join_swarm", {"role": "reviewer", "name": "Claude Code", "instructions": "review"})
    assert text.startswith('You joined the swarm as Claude Code (role: Reviewer). Pass as_agent="Claude Code"')
    # an external member has no system prompt: the catalog's description of its role comes with the result
    assert text.endswith("\nYour role (Reviewer): " + roles.ROLE_PRESETS["en"]["reviewer"][1])
    member = swarm.member("Claude Code")
    assert member["kind"] == AgentKind.EXTERNAL and member["hired_by"] == USER and member["system_prompt"] == ""
    assert "Claude Code" not in swarm.runtimes
    # a reconnecting client joins again under its name and continues as that member
    text = await ok(swarm, USER, "join_swarm", {"role": "Reviewer", "name": " claude  code "})
    assert text.startswith("Welcome back: you continue as Claude Code")
    assert len(swarm.members()) == 3
    assert "already a member: Claude Code" in await ok(swarm, "Claude Code", "join_swarm", {"role": "x"})
    assert "internal swarm agent" in await err(swarm, USER, "join_swarm", {"role": "x", "name": a})
    generated = await ok(swarm, USER, "join_swarm", {"role": "Helper"})
    name = re.search(r"as (.+?) \(role", generated).group(1)
    assert swarm.kind_of(name) == AgentKind.EXTERNAL
    # external members neither count toward max_agents (a client joining must not block hiring) nor are limited
    await ok(swarm, USER, "swarm_control", {"max_agents": 2})
    assert "You do not count toward the agent limit" in await ok(swarm, USER, "join_swarm", {"role": "One more"})
    assert "maximum of 2 AI agents (members who joined through MCP do not count)" in await err(
        swarm, USER, "hire_agent", {"role": "Tester", "instructions": "t"})
    await ok(swarm, USER, "swarm_control", {"max_agents": 3})
    await ok(swarm, USER, "hire_agent", {"role": "Tester", "instructions": "t"})
    assert swarm.agent_count() == 3 and len(swarm.members()) == 6
    assert swarm.snapshot()["swarm"]["agent_count"] == 3
    text = await ok(swarm, USER, "swarm_status")
    assert "AI agents: 3 of max 3, plus 3 external members (not counted)." in text
    # members act through the tools: claim and talk
    await ok(swarm, USER, "task_create", {"title": "Review", "goal": "g"})
    await ok(swarm, "Claude Code", "task_claim", {"task_id": 1})
    assert swarm.board.get(1).assignee == "Claude Code"


async def test_wait_for_events(swarm):
    a = llm_members(swarm)[0]
    ext = await join(swarm)
    # nothing pending: timeout 0 returns at once
    assert await ok(swarm, ext, "wait_for_events", {"timeout": 0}) == \
        "No new events in 0s. Call wait_for_events again to keep listening."
    # pending items return immediately (even with a long timeout) and are consumed
    swarm.mail.send(a, [ext], "ping")
    started = time.monotonic()
    text = await ok(swarm, ext, "wait_for_events", {"timeout": 600})
    assert "Message #1 from" in text and "ping" in text and time.monotonic() - started < 1
    assert not swarm.has_pending(ext)
    # regression: a stale signal (the message was read with read_messages) ended the wait at once while
    # claiming "No new events in 2s"
    swarm.mail.send(a, [ext], "read elsewhere")
    await ok(swarm, ext, "read_messages")
    started = time.monotonic()
    text = await ok(swarm, ext, "wait_for_events", {"timeout": 1})
    assert text.startswith("No new events in 1s") and time.monotonic() - started >= 0.9
    # an event during the wait wakes the caller

    async def later():
        await asyncio.sleep(0.2)
        swarm.mail.send(a, [ext], "during the wait")

    sender = asyncio.create_task(later())
    started = time.monotonic()
    text = await ok(swarm, ext, "wait_for_events", {"timeout": 10})
    await sender
    assert "during the wait" in text and time.monotonic() - started < 5
    # the user waits for its own notifications (e.g. goal done) and messages
    await ok(swarm, USER, "set_goal", {"title": "G"})
    await ok(swarm, a, "goal_complete", {"summary": "all done"})
    text = await ok(swarm, USER, "wait_for_events", {"timeout": "5"})
    assert "reports that the global goal is achieved: all done" in text
    # negative timeouts mean "do not wait"
    assert "No new events in 0s" in await ok(swarm, USER, "wait_for_events", {"timeout": -5})


def board_progress(text: str) -> list[str]:
    return text.split("Board progress:\n", 1)[1].splitlines() if "Board progress:\n" in text else []


async def test_wait_for_events_with_progress(swarm):
    """progress=true (external members and the user): board progress made by others also ends the wait, as short
    lines batched with the other events and capped; the caller's own changes are no news; the default is as
    before (only what is addressed to the caller)."""
    from dreamteam.tools.swarm_tools import PROGRESS_MAX

    assert REGISTRY["wait_for_events"].parameters["properties"]["progress"]["type"] == "boolean"
    a, b = llm_members(swarm)
    ext = await join(swarm)
    await ok(swarm, USER, "set_goal", {"title": "Ship"})  # before the first progress call: not news
    assert await ok(swarm, ext, "wait_for_events", {"timeout": 0, "progress": True}) == \
        "No new events in 0s. Call wait_for_events again to keep listening."

    async def later(actor: str, tool: str, args: dict) -> None:
        await asyncio.sleep(0.2)
        await ok(swarm, actor, tool, args)

    # progress during the wait ends it
    mover = asyncio.create_task(later(USER, "task_create", {"title": "Parser", "goal": "g", "assignee": b}))
    started = time.monotonic()
    text = await ok(swarm, ext, "wait_for_events", {"timeout": 10, "progress": True})
    await mover
    assert time.monotonic() - started < 5 and text.startswith("=== New events while you were working (")
    assert text.splitlines()[1:] == ["Board progress:", f"• #2 Parser: created by user (assignee {b})"]
    # changes are batched with the messages; the caller's own changes and edits that change no status are left out
    await ok(swarm, b, "task_update", {"task_id": 2, "status": "in_progress"})
    await ok(swarm, ext, "task_create", {"title": "Mine", "goal": "g"})
    await ok(swarm, USER, "task_update", {"task_id": 2, "description": "more details"})
    swarm.mail.send(a, [ext], "hello")
    text = await ok(swarm, ext, "wait_for_events", {"timeout": 0, "progress": True})
    assert "hello" in text and text.index("hello") < text.index("Board progress:")
    assert board_progress(text) == [f"• #2 Parser: todo → in_progress ({b})"]
    assert (await ok(swarm, ext, "wait_for_events", {"timeout": 0, "progress": True})).startswith("No new events")
    # the goal and the team
    await ok(swarm, USER, "hire_agent", {"role": "Tester", "instructions": "t", "name": "Ada King"})
    await join(swarm, "Bob Stone", "Helper")
    await ok(swarm, USER, "retire_agent", {"name": "Ada King"})
    await ok(swarm, a, "goal_complete", {"summary": "done"})
    await ok(swarm, USER, "swarm_control", {"reopen_goal_comment": "more"})
    assert board_progress(await ok(swarm, ext, "wait_for_events", {"timeout": 0, "progress": True})) == [
        '• Ada King hired as "Tester" by user', '• Bob Stone joined as "Helper" (external member)',
        "• Ada King left the swarm (user)", f"• goal achieved ({a})", "• goal reopened by user"]
    # without progress, board progress neither ends the wait nor is consumed
    mover = asyncio.create_task(later(USER, "task_update", {"task_id": 2, "status": "waiting"}))
    started = time.monotonic()
    text = await ok(swarm, ext, "wait_for_events", {"timeout": 1})
    await mover
    assert text.startswith("No new events in 1s") and time.monotonic() - started >= 0.9
    assert board_progress(await ok(swarm, ext, "wait_for_events", {"timeout": 0, "progress": True})) == [
        "• #2 Parser: in_progress → waiting (user)"]
    # a cap on the lines of one result
    for i in range(PROGRESS_MAX + 5):
        swarm.board.create(USER, f"T{i}", goal="g")
    lines = board_progress(await ok(swarm, ext, "wait_for_events", {"timeout": 0, "progress": True}))
    assert len(lines) == PROGRESS_MAX + 1 and lines[0] == "• #4 T0: created by user"
    assert lines[-1] == "• … and 5 more: see `board`"
    # in the caller's language
    swarm.db.execute("UPDATE agents SET language = 'ru' WHERE name = ?", (ext,))
    await ok(swarm, USER, "task_update", {"task_id": 2, "status": "cancelled"})
    text = await ok(swarm, ext, "wait_for_events", {"timeout": 0, "progress": True})
    assert text.splitlines()[1:] == ["Прогресс на доске:", "• #2 Parser: waiting → cancelled (user)"]
    # the user has a cursor of its own
    await ok(swarm, USER, "wait_for_events", {"timeout": 0})  # what was addressed to the user so far
    assert (await ok(swarm, USER, "wait_for_events", {"timeout": 0, "progress": True})).startswith("No new events")
    await ok(swarm, b, "task_claim", {"task_id": 3})
    assert board_progress(await ok(swarm, USER, "wait_for_events", {"timeout": 0, "progress": True})) == [
        f"• #3 Mine: todo → in_progress ({b})"]


async def test_wait_for_events_with_progress_ends_when_the_swarm_stops(tmp_path):
    sw = Swarm(config=make_config(tmp_path), llm_factory=lambda profile, agent: ScriptedLLM(lambda *a: "ok", profile))
    await sw.start(run_agents=False)
    try:
        poll = asyncio.create_task(call_tool(ToolContext(sw, USER), "wait_for_events",
                                             {"timeout": 120, "progress": True}))
        await asyncio.sleep(0.3)
        assert len(sw.bus._queues) == 1  # it listens to the board's events
        started = time.monotonic()
        await sw.stop()
        text, is_error = await asyncio.wait_for(poll, 10)
        assert is_error and "stopped while waiting" in text and time.monotonic() - started < 5
        assert not sw.bus._queues  # and stopped listening
    finally:
        sw.close()


def test_progress_texts_match_in_both_languages():
    from dreamteam.tools.swarm_tools import _PROGRESS

    assert _PROGRESS["en"].keys() == _PROGRESS["ru"].keys()
    for key, en in _PROGRESS["en"].items():
        ru = _PROGRESS["ru"][key]
        assert sorted(re.findall(r"\{(\w+)\}", en)) == sorted(re.findall(r"\{(\w+)\}", ru)), key
        assert re.findall(r"`[^`]+`", en) == re.findall(r"`[^`]+`", ru), key


async def test_set_goal_is_for_the_user(swarm):
    a = llm_members(swarm)[0]
    ext = await join(swarm)
    text = await ok(swarm, USER, "set_goal", {"title": "Build a CLI", "description": "small and tested"})
    assert text == "Goal set. Global goal [active]: Build a CLI\n  small and tested"
    planning = swarm.board.get(1)
    assert "planning" in planning.tags and planning.created_by == USER
    assert "not available to swarm agents" in await err(swarm, a, "set_goal", {"title": "x"})
    assert "not available to external members" in await err(swarm, ext, "set_goal", {"title": "x"})
    assert swarm.goal()["title"] == "Build a CLI"
    # a goal typed as one text: its first line is the title, the other lines open the description
    text = await ok(swarm, USER, "set_goal", {"title": "Snake game\nPython, pygame\n\n- score", "description": "Tests"})
    assert text == "Goal set. Global goal [active]: Snake game\n  Python, pygame - score Tests"
    assert (swarm.goal()["title"], swarm.goal()["description"]) == ("Snake game", "Python, pygame\n\n- score\n\nTests")


async def test_swarm_control(swarm):
    a = llm_members(swarm)[0]
    assert await ok(swarm, USER, "swarm_control", {"action": "pause"}) == "Done: paused."
    assert swarm.paused and swarm.pause_reason == "paused by the user"
    assert await ok(swarm, USER, "swarm_control", {"action": "resume"}) == "Done: resumed."
    text = await ok(swarm, USER, "swarm_control", {"max_cost_usd": 0, "max_agents": "5"})
    assert text == "Done: limits: no budget limit, at most 5 AI agents."
    assert "max_agents must be at least 1" in await err(swarm, USER, "swarm_control", {"max_agents": 0})
    assert "Nothing to do" in await err(swarm, USER, "swarm_control", {})
    # resume is refused while the budget is exhausted; raising the limit resumes
    swarm.db.execute("UPDATE agents SET cost_usd = 2 WHERE name = ?", (a,))
    await ok(swarm, USER, "swarm_control", {"max_cost_usd": 1})
    swarm.pause(USER, "budget")
    assert "raise max_cost_usd" in await err(swarm, USER, "swarm_control", {"action": "resume"})
    assert "Done: limits: $10.00" in await ok(swarm, USER, "swarm_control", {"max_cost_usd": 10, "action": "resume"})
    assert not swarm.paused
    # reopen only a completed goal
    assert "There is no global goal" in await err(swarm, USER, "swarm_control", {"reopen_goal_comment": "more"})
    await ok(swarm, USER, "set_goal", {"title": "G"})
    assert "not completed" in await err(swarm, USER, "swarm_control", {"reopen_goal_comment": "more"})
    await ok(swarm, a, "goal_complete", {"summary": "done"})
    assert await ok(swarm, USER, "swarm_control", {"reopen_goal_comment": "add docs"}) == "Done: goal reopened."
    assert swarm.goal()["status"] == "active"
    assert "n_goal_reopened" in notifications(swarm, a)
    assert "not available to swarm agents" in await err(swarm, a, "swarm_control", {"action": "pause"})


# ================================================================== triggers
async def test_trigger_tools(swarm):
    a, b = llm_members(swarm)
    await ok(swarm, USER, "task_create", {"title": "First", "goal": "g"})
    assert await ok(swarm, a, "trigger_create", {"condition": "done(1)", "note": "start the integration"}) == \
        "Trigger #1 created: done(1)."
    assert await ok(swarm, a, "trigger_create", {"condition": "every(600)", "once": True}) == \
        "Trigger #2 created: every(600) (repeating)."
    text = await ok(swarm, b, "trigger_create", {"condition": "status(1) == failed", "once": "false",
                                                  "actions": [{"type": "message", "to": ["user"], "text": "#1 failed"}]})
    assert text == "Trigger #3 created: status(1) == failed (repeating); actions: message."
    text = await err(swarm, a, "trigger_create", {"condition": "done(1) +"})
    assert "Invalid condition" in text and "Condition syntax" in text
    assert "Invalid action #1" in await err(swarm, a, "trigger_create", {"condition": "done(1)",
                                                                         "actions": [{"type": "explode"}]})
    text = await ok(swarm, a, "trigger_list")
    assert "#1 [active] owner=" in text and "start the integration" in text and "#3" not in text
    assert "#2 [active, repeating]" in text
    everyone = await ok(swarm, a, "trigger_list", {"all": True})
    assert "#3 [active, repeating]" in everyone and 'actions=[{"type": "message"' in everyone
    assert "belongs to" in await err(swarm, b, "trigger_delete", {"trigger_id": 1})
    assert await ok(swarm, a, "trigger_delete", {"trigger_id": "#2"}) == "Trigger #2 deleted."
    assert "not found" in await err(swarm, a, "trigger_delete", {"trigger_id": 2})
    assert await ok(swarm, USER, "trigger_delete", {"trigger_id": 3}) == "Trigger #3 deleted."   # the user may
    # a trigger fires and wakes its owner with the note
    swarm.board.update(USER, 1, status=Status.DONE)
    swarm.triggers.evaluate_all()
    texts = [r["text"] for r in swarm.db.all("SELECT text FROM notifications WHERE recipient = ?", (a,))]
    assert any("Trigger #1 fired" in t and "start the integration" in t for t in texts)
    text = await ok(swarm, a, "trigger_list", {"all": True})
    assert "#1 [inactive]" in text and "fired 1x" in text
    assert await ok(swarm, b, "trigger_list") == "You have no active triggers (all=true lists everyone's)."
    # the user may own triggers too
    await ok(swarm, USER, "trigger_create", {"condition": "goal_done()"})


# ================================================================== MCP workshop
SERVER = '''from mcp.server.mcpserver import MCPServer

mcp = MCPServer("calc")

@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

if __name__ == "__main__":
    mcp.run()
'''


async def test_mcp_tools_end_to_end(swarm):
    a, b = llm_members(swarm)
    text = await ok(swarm, a, "mcp_create", {"name": "calc", "description": "Arithmetic", "code": SERVER,
                                              "task_id": 0})
    assert "MCP server 'calc' is active" in text and "add(a, b)" in text
    text = await ok(swarm, b, "mcp_list")
    assert f"calc [active, workspace, scope=swarm] by {a}: Arithmetic\n    tools: add" in text
    assert "No MCP servers match" in await ok(swarm, b, "mcp_list", {"query": "pdf"})
    text = await ok(swarm, b, "mcp_tools", {"server": "calc"})
    assert text.startswith("Tools of calc:\n- add: Add two numbers.") and '"a"' in text
    assert await ok(swarm, b, "mcp_call", {"server": "calc", "tool": "add", "arguments": '{"a": 2, "b": 3}'}) == "5"
    assert "has no tool 'mul'" in await err(swarm, b, "mcp_call", {"server": "calc", "tool": "mul"})
    assert "calc.add returned an error" in await err(swarm, b, "mcp_call", {"server": "calc", "tool": "add",
                                                                            "arguments": {"a": "x"}})
    # the user can look but not call
    assert "add" in await ok(swarm, USER, "mcp_tools", {"server": "calc"})
    assert "not available to the user" in await err(swarm, USER, "mcp_call", {"server": "calc", "tool": "add"})
    # regression: a server that fails its smoke test was reported as a successful tool result
    text = await err(swarm, a, "mcp_create", {"name": "broken", "description": "Broken",
                                               "code": "raise SystemExit('boom')\n"})
    assert "FAILED its smoke test" in text and "call mcp_create again" in text
    text = await ok(swarm, a, "mcp_list", {"query": "broken"})
    assert "broken [failed" in text and "error:" in text
    assert "Invalid MCP server name" in await err(swarm, a, "mcp_create", {"name": "Bad Name", "description": "x",
                                                                           "code": SERVER})


async def test_mcp_approval_by_the_user(swarm):
    a = llm_members(swarm)[0]
    swarm.config.mcp.require_approval = True
    text = await ok(swarm, a, "mcp_create", {"name": "calc_two", "description": "Arithmetic", "code": SERVER,
                                              "scope": "PRIVATE"})
    assert "waiting for the user's approval" in text
    assert "u_mcp_pending" in notifications(swarm, USER)
    assert "waiting for the user's approval" in await err(swarm, a, "mcp_call", {"server": "calc_two", "tool": "add",
                                                                                "arguments": {"a": 1, "b": 1}})
    assert "not available to swarm agents" in await err(swarm, a, "mcp_approve", {"name": "calc_two", "approve": True})
    assert await ok(swarm, USER, "mcp_approve", {"name": "calc_two", "approve": "true"}) == \
        "MCP server 'calc_two' approved and active."
    assert "n_mcp_approved" in notifications(swarm, a)
    assert await ok(swarm, a, "mcp_call", {"server": "calc_two", "tool": "add", "arguments": {"a": 1, "b": 1}}) == "2"
    assert "not waiting for approval" in await err(swarm, USER, "mcp_approve", {"name": "calc_two", "approve": False})


# ================================================================== through a real agent loop
async def test_scripted_agent_uses_the_tools_through_its_runtime(tmp_path):
    seen_tools: list[list[dict]] = []
    script = iter([
        {"tool_calls": [{"name": "task_create", "arguments": {"title": "Write docs", "goal": "README exists",
                                                              "tags": '["docs"]', "assignee": "me"}},
                        {"name": "write_file", "arguments": {"path": "README.md", "content": "# Docs\n"}},
                        {"name": "send_message", "arguments": {"to": "user", "text": "README is written"}}]},
        {"tool_calls": [{"name": "task_get", "arguments": {"task_id": "#1"}}, {"name": "bogus", "arguments": {}}]},
        {"tool_calls": [{"name": "wait", "arguments": {}}]},
    ])

    def handler(system, messages, tools):
        seen_tools.append(tools)
        return next(script, {"tool_calls": [{"name": "wait", "arguments": {}}]})

    cfg = make_config(tmp_path, swarm={"start_agents": 1})
    sw = Swarm(config=cfg, llm_factory=lambda profile, agent: ScriptedLLM(handler, profile))
    await sw.start()
    try:
        agent = sw.members()[0]["name"]
        sw.notify(agent, "n_restart")

        def tool_messages() -> dict[str, dict]:
            rows = sw.db.all("SELECT msg FROM history WHERE agent = ? ORDER BY seq", (agent,))
            return {m["name"]: m for m in map(lambda r: json.loads(r["msg"]), rows) if m["role"] == "tool"}

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and "wait" not in tool_messages():
            await asyncio.sleep(0.05)
        tool_msgs = tool_messages()
        assert "wait" in tool_msgs
        assert not tool_msgs["task_create"]["is_error"] and sw.board.get(1).tags == ["docs"]
        assert sw.board.get(1).assignee == agent
        assert (sw.workspace.root / "README.md").read_text(encoding="utf-8") == "# Docs\n"
        assert not tool_msgs["task_get"]["is_error"] and "Task #1: Write docs" in tool_msgs["task_get"]["content"]
        assert tool_msgs["bogus"]["is_error"] and "Unknown tool 'bogus'" in tool_msgs["bogus"]["content"]
        reply = sw.mail.conversation(USER, agent)
        assert [m["text"] for m in reply] == ["README is written"]
        # the tool list is the constant schemas_for("llm", ...) on every call
        assert seen_tools and all(t == schemas_for("llm", "en") for t in seen_tools)
    finally:
        await sw.stop()
        sw.close()
