"""Frozen LLM tool sets (tools/frozen.py, docs/SPEC.md §6.1): the shipped revisions never change and stay
served by the registry, every agent sends exactly the array it was given, and it moves to the current revision
only inside a compaction. Agents run on ScriptedLLM; a later revision is simulated by patching
TOOLSET_REVISIONS["board"] and `frozen.current` and registering a `probe` tool. Each revision belongs to one session
mode (docs/SPEC.md §22.5): the board's 5, the thread's 6."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from dreamteam import __version__, cli, prompts
from dreamteam import swarm as swarm_module
from dreamteam import tools as tools_module
from dreamteam.agent import AgentRuntime
from dreamteam.config import Config
from dreamteam.db import dumps
from dreamteam.llm import anthropic_llm, openai_llm
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentKind, AgentStatus, Status
from dreamteam.swarm import Swarm
from dreamteam.tools import REGISTRY, ToolContext, ToolSpec, call_tool, frozen, schemas_for, tools_for

# exact-order digests (sha256 of db.dumps) of every shipped revision: what agents send, byte for byte
RELEASED = {
    # crew tasks: task_create and task_update gain crew and directions
    5: {"en": "06d7f3ebc6d94f9e450256633804a57619bae3859d9e4aa9c21a50b7da9d2187",
        "ru": "d0a9bbe1dc690b54ac30474843e8b53ec8c321997ef11afecfbbbfaf194656fa"},
    # the thread mode's first tool set (docs/SPEC.md §22.5)
    6: {"en": "a90a8caef7ca0727e5c4e48f038c48e5af763e0e3c0437b907221577464e4202",
        "ru": "a341ee9d51bfa9644014fd5e748e472117dfe3f9cbd57549f75d896d26839195"},
}
LANGS = ("en", "ru")
MODES = ("board", "thread")
LATER = frozen.NEWEST_REVISION + 1  # the simulated later revision of the board
OLD = frozen.OLDEST_OPEN_REVISION  # what a simulated later revision upgrades from


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------------------------------ helpers
def call(tool_name: str, /, **arguments: Any) -> dict:
    return {"tool_calls": [{"name": tool_name, "arguments": arguments}]}


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Frozen tools", "language": "en", "start_agents": 1, "max_agents": 4, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 30, "max_steps_per_turn": 10},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0,
                               "cache_read_price": 0, "cache_write_price": 0}},
    }
    for section, values in overrides.items():
        raw.setdefault(section, {}).update(values)
    return Config.from_dict(tmp_path / "project", raw)


class Recorder:
    """Records every LLM request per agent: (messages, tools) as sent."""

    def __init__(self, policy: Callable[[list[dict], list[dict]], Any]):
        self.policy = policy
        self.calls: dict[str, list[tuple[list[dict], list[dict]]]] = {}

    def __call__(self, name: str, messages: list[dict], tools: list[dict]) -> Any:
        self.calls.setdefault(name, []).append((copy.deepcopy(messages), copy.deepcopy(tools)))
        return self.policy(messages, tools)

    def tools(self, name: str) -> list[list[dict]]:
        return [tools for _, tools in self.calls.get(name, [])]


def make_swarm(tmp_path: Path, policy: Callable[[list[dict], list[dict]], Any],
               **overrides: Any) -> tuple[Swarm, Recorder]:
    recorder = Recorder(policy)

    def factory(profile, agent: str) -> ScriptedLLM:
        return ScriptedLLM(lambda s, m, t: recorder(agent, m, t), profile)

    return Swarm(config=make_config(tmp_path, **overrides), llm_factory=factory), recorder


async def run_until(predicate: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


async def shutdown(swarm: Swarm) -> None:
    await swarm.stop()
    swarm.close()


def seed_history(swarm: Swarm, name: str, pairs: int, size: int) -> None:
    seq = 0
    for i in range(pairs):
        for msg in ({"role": "user", "content": f"wake {i} " + "w" * size},
                    {"role": "assistant", "content": f"working {i}", "raw": None,
                     "tool_calls": [{"id": f"seed_{i}", "name": "board", "arguments": {}}]},
                    {"role": "tool", "tool_call_id": f"seed_{i}", "name": "board", "content": "r" * size,
                     "is_error": False},
                    {"role": "assistant", "content": f"done {i}", "raw": None, "tool_calls": []}):
            swarm.db.execute("INSERT INTO history(agent, seq, msg) VALUES (?, ?, ?)", (name, seq, json.dumps(msg)))
            seq += 1


def last_text(messages: list[dict]) -> str:
    return str(messages[-1].get("content") or "") if messages else ""


def is_compaction_request(messages: list[dict]) -> bool:
    return "replaced by a summary" in last_text(messages)


def tool_rows(swarm: Swarm) -> int:
    return int(swarm.db.scalar("SELECT COUNT(*) FROM kv WHERE key LIKE 'tools:%'"))


async def probe_tool(ctx: ToolContext, args: dict) -> str:
    return "probe ok"


@pytest.fixture
def later_revision(monkeypatch):
    """The code builds revision 99 = revision OLD + a `probe` tool (as a real revision would add a tool)."""
    tools_module._load()
    spec = ToolSpec(name="probe", description={"en": "A tool of a later revision.",
                                               "ru": "Инструмент более поздней ревизии."},
                    parameters={"type": "object", "properties": {}, "required": []}, handler=probe_tool,
                    audience=frozenset({"llm"}))
    monkeypatch.setitem(REGISTRY, "probe", spec)
    monkeypatch.setitem(frozen.TOOLSET_REVISIONS, "board", LATER)

    def current(language, mode="board"):
        return frozen.snapshot(OLD, language) + [{"name": "probe", "description": spec.describe(language or "en"),
                                                  "parameters": copy.deepcopy(spec.parameters)}]

    monkeypatch.setattr(frozen, "current", current)
    return spec


@pytest.fixture(autouse=True)
def fast_scheduler(monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)


# ------------------------------------------------------------------------------ released revisions
def test_released_tool_sets_never_change():
    # exactly the revisions a session this version opens can hold: OLD..NEWEST_REVISION, one numbering for both modes
    assert frozen.revisions() == list(range(OLD, frozen.NEWEST_REVISION + 1)) == sorted(RELEASED)
    assert frozen.revisions("board") == [5] and frozen.revisions("thread") == [6]
    assert frozen.NEWEST_REVISION == max(frozen.TOOLSET_REVISIONS.values()) == 6
    for rev, digests in RELEASED.items():
        for lang in LANGS:
            assert sha(dumps(frozen.snapshot(rev, lang))) == digests[lang], (rev, lang)
    # a language without texts gets the English array, like schemas_for
    assert frozen.snapshot(OLD, "de") == frozen.snapshot(OLD, None) == frozen.snapshot(OLD, "en")
    assert frozen.snapshot(6, "de") == frozen.snapshot(6, "en")


def test_each_snapshot_file_names_its_mode():
    """A board file is llm_r<N>.json without "mode" (as 0.4.x wrote it), a thread file llm_thread_r<N>.json with
    "mode": "thread"; no number is in two files."""
    seen: dict[int, str] = {}
    for path in sorted(frozen.SNAPSHOT_DIR.glob("llm_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        rev, mode = data["revision"], data.get("mode", "board")
        assert rev not in seen, (path.name, seen.get(rev))
        seen[rev] = path.name
        assert path.name == (f"llm_r{rev}.json" if mode == "board" else f"llm_{mode}_r{rev}.json")
        assert ("mode" in data) == (mode != "board") and mode in MODES
        assert frozen.mode_of(rev) == mode
    assert sorted(seen) == frozen.revisions()
    with pytest.raises(ValueError, match="not shipped"):
        frozen.mode_of(LATER)


def test_the_oldest_revision_opened_is_a_fixed_5(later_revision):
    """OLDEST_OPEN_REVISION is a literal: a later revision still opens sessions of revision 5."""
    assert frozen.TOOLSET_REVISIONS["board"] == LATER
    assert frozen.OLDEST_OPEN_REVISION == 5 and 5 in frozen.revisions()


def test_valid_rejects_unusable_records():
    assert frozen.valid(frozen.record(OLD, "en", frozen.snapshot(OLD, "en")))
    assert frozen.valid(frozen.current_record("ru")) and frozen.valid({"rev": 5, "tools": []})
    assert frozen.valid(frozen.current_record("en", "thread"))
    assert frozen.valid({"rev": 6, "tools": [], "mode": "thread"})
    for bad in ({"rev": "1", "tools": []}, {"rev": True, "tools": []}, {"rev": 1, "tools": [{"name": "board"}]},
                {"rev": 1, "tools": "board"}, ["board"], None, "(not JSON)", {"rev": 5, "tools": [], "mode": "chat"}):
        assert not frozen.valid(bad), bad


def test_records_carry_their_mode():
    assert frozen.current_record("en") == {"rev": 5, "language": "en", "tools": frozen.current("en"), "mode": "board"}
    thread = frozen.current_record("ru", "thread")
    assert thread["rev"] == 6 and thread["mode"] == "thread" and thread["language"] == "ru"
    assert dumps(thread["tools"]) == dumps(frozen.snapshot(6, "ru"))
    # a record without "mode" (0.4.x) states the board mode, whatever its revision
    assert frozen.record_mode({"rev": 5, "tools": []}) == "board"
    assert frozen.record_mode({"rev": 6, "tools": []}) == "board"
    assert frozen.record_mode({"rev": 6, "tools": [], "mode": "thread"}) == "thread"


def test_read_ignores_a_record_of_the_other_mode(tmp_path, caplog):
    from dreamteam.db import Database

    db = Database(tmp_path / "swarm.db")
    try:
        frozen.write(db, "Anna", frozen.current_record("en"))
        assert frozen.read(db, "Anna") == frozen.read(db, "Anna", "board") == frozen.current_record("en")
        assert frozen.read(db, "Anna", "thread") is None and "not of the session's thread mode" in caplog.text
        # no "mode" (missing = board) on a thread revision: unusable in both modes
        frozen.write(db, "Bob", {"rev": 6, "language": "en", "tools": []})
        assert frozen.read(db, "Bob", "board") is None and frozen.read(db, "Bob", "thread") is None
        frozen.write(db, "Old", {"rev": 5, "language": "en", "tools": []})  # as 0.4.x wrote it
        assert frozen.read(db, "Old", "board") is not None and frozen.read(db, "Old", "thread") is None
        # a stated mode that its revision's does not match: unusable
        frozen.write(db, "Eve", {"rev": 5, "language": "en", "tools": [], "mode": "thread"})
        assert frozen.read(db, "Eve", "thread") is None and frozen.read(db, "Eve", "board") is None
        frozen.write(db, "Max", {"rev": 6, "language": "en", "tools": [], "mode": "board"})
        assert frozen.read(db, "Max", "board") is None and frozen.read(db, "Max", "thread") is None
        frozen.write(db, "Liz", {"rev": 6, "language": "en", "tools": [], "mode": "thread"})
        assert frozen.read(db, "Liz", "thread") is not None and frozen.read(db, "Liz", "board") is None
        # a revision this code does not ship: its record's word
        frozen.write(db, "New", {"rev": 99, "language": "en", "tools": []})
        assert frozen.read(db, "New", "board") is not None and frozen.read(db, "New", "thread") is None
    finally:
        db.close()


@pytest.mark.parametrize("mode", MODES)
def test_the_code_builds_the_current_revision(mode):
    rev = frozen.TOOLSET_REVISIONS[mode]
    assert frozen.mode_of(rev) == mode
    for lang in LANGS:
        assert dumps(frozen.current(lang, mode)) == dumps(frozen.snapshot(rev, lang)), (
            f"The LLM tool schemas ({lang}, {mode}) differ from revision {rev}. A schema change is a new revision: "
            f'set TOOLSET_REVISIONS["{mode}"] to NEWEST_REVISION + 1 in tools/frozen.py, run `python -m '
            "dreamteam.tools.frozen --write`, and pin its digests in RELEASED.")
    assert frozen.main(["--check", "--mode", mode]) == 0
    assert frozen.main(["--check"]) == 0


def test_the_board_array_is_revision_5_without_a_mode():
    """schemas_for without a mode builds the board array, byte for byte revision 5."""
    for lang in LANGS:
        assert dumps(frozen.normalize(schemas_for("llm", lang))) == dumps(frozen.snapshot(5, lang))
        assert dumps(frozen.current(lang)) == dumps(frozen.snapshot(5, lang))


_THREAD_VARIANTS = {"send_message", "wait", "agents", "retire_agent", "mcp_create", "trigger_create"}
_FORUM_TOOLS = ["feed", "post_read", "post_create", "post_update", "comment", "vote", "subscriptions", "subscribe",
                "unsubscribe"]
_BOARD_WORDS = ("task", "board", "todo", "assign", "task_comment", "задач",
                "доск", "назнач")


def _texts(schema: Any) -> list[str]:
    """Every description in a tool or schema, nested ones too."""
    found = []
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "description" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_texts(value))
    elif isinstance(schema, list):
        for value in schema:
            found.extend(_texts(value))
    return found


def test_revision_6_is_the_shared_tools_then_the_forum():
    for lang in LANGS:
        board = {t["name"]: t for t in frozen.snapshot(5, lang)}
        thread = frozen.snapshot(6, lang)
        names = frozen.names(thread)
        shared = [n for n in board if n in names]
        assert names == shared + _FORUM_TOOLS and len(shared) == 23 and len(names) == 32
        assert set(board) - set(names) == {"board", "task_get", "task_create", "task_claim", "task_update",
                                           "task_complete", "task_review", "task_comment", "task_discussion",
                                           "goal_complete"}
        for tool in thread:
            if tool["name"] in board and tool["name"] not in _THREAD_VARIANTS:
                assert dumps(tool) == dumps(board[tool["name"]]), (lang, tool["name"])  # byte for byte
            elif tool["name"] in board:
                assert dumps(tool) != dumps(board[tool["name"]]), (lang, tool["name"])
            for text in [tool["description"], *_texts(tool["parameters"])]:
                low = text.lower()
                assert not any(word in low for word in _BOARD_WORDS), (lang, tool["name"], text)
            if lang == "ru" and (tool["name"] in _THREAD_VARIANTS or tool["name"] in _FORUM_TOOLS):
                assert "\u0451" not in tool["description"], tool["name"]


def _types(schema: dict) -> tuple:
    return schema.get("type"), (schema.get("items") or {}).get("type")


def test_every_released_tool_set_is_still_served():
    """Arguments are validated against the current registry of the revision's mode, so it must accept everything a
    shipped array allows: no tool or property removed or retyped, no new required argument, enums only grow."""
    tools_for("llm")  # the registry is filled (also when this test runs alone)
    for rev in frozen.revisions():
        mode = frozen.mode_of(rev)
        for lang in LANGS:
            for tool in frozen.snapshot(rev, lang):
                where = (rev, lang, tool["name"])
                spec = REGISTRY.get(tool["name"])
                assert spec is not None and "llm" in spec.audience and mode in spec.modes, where
                old, new = tool["parameters"], spec.parameters_for(mode)
                new_props = new.get("properties", {})
                for prop, schema in old.get("properties", {}).items():
                    assert prop in new_props, (*where, prop)
                    assert _types(new_props[prop]) == _types(schema), (*where, prop)
                    for get in (lambda s: s.get("enum"), lambda s: (s.get("items") or {}).get("enum")):
                        if get(schema) is None:
                            assert get(new_props[prop]) is None, (*where, prop, "a free value gained an enum")
                        else:
                            assert set(get(schema)) <= set(get(new_props[prop]) or []), (*where, prop)
                assert set(new.get("required", [])) <= set(old.get("required", [])), where


def test_schemas_are_plain_json():
    for kind in ("llm", "external", "user"):
        for lang in LANGS:
            for mode in MODES:
                schemas = schemas_for(kind, lang, mode)
                assert frozen.normalize(schemas) == schemas
                assert dumps(frozen.normalize(schemas)) == dumps(schemas)
                assert [s["name"] for s in schemas] == [s.name for s in tools_for(kind, mode)]


def test_write_refuses_to_change_a_released_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(frozen, "SNAPSHOT_DIR", tmp_path)
    frozen._snapshot_text.cache_clear()
    paths = {"board": tmp_path / "llm_r5.json", "thread": tmp_path / "llm_thread_r6.json"}
    try:
        assert frozen.main(["--check"]) == 1  # missing
        assert frozen.main(["--write", "--mode", "thread"]) == 0 and not paths["board"].exists()
        assert frozen.main(["--check", "--mode", "thread"]) == 0 and frozen.main(["--check"]) == 1
        assert frozen.main(["--write"]) == 0
        written = {mode: path.read_bytes() for mode, path in paths.items()}
        for mode, rev in frozen.TOOLSET_REVISIONS.items():
            data = json.loads(written[mode])
            assert b"\r\n" not in written[mode] and data["revision"] == rev and data.get("mode", "board") == mode
        assert "mode" not in json.loads(written["board"])  # as llm_r5.json
        assert frozen.main(["--check"]) == 0 and frozen.main(["--write"]) == 0  # unchanged: left as is
        assert {mode: path.read_bytes() for mode, path in paths.items()} == written
        for mode, path in paths.items():
            rev = frozen.TOOLSET_REVISIONS[mode]
            path.write_text(f'{{"revision": {rev}, "en": [], "ru": []}}', encoding="utf-8")
            capsys.readouterr()
            assert frozen.main(["--check"]) == 1
            assert f'set TOOLSET_REVISIONS["{mode}"] to NEWEST_REVISION + 1 (7)' in capsys.readouterr().err
            assert frozen.main(["--write"]) == 2  # a released revision is never edited
            assert frozen.main(["--write", "--force", "--mode", mode]) == 0 and path.read_bytes() == written[mode]
        # a thread file without its "mode" is not the thread revision
        data = json.loads(written["thread"])
        del data["mode"]
        paths["thread"].write_text(json.dumps(data), encoding="utf-8")
        assert frozen.main(["--check", "--mode", "thread"]) == 1
    finally:
        frozen._snapshot_text.cache_clear()


# --------------------------------------------------------------------------------- agents' records
async def test_new_agent_freezes_its_tools_before_the_first_call(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda m, t: call("wait"))
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        record = frozen.read(swarm.db, agent)
        assert record is not None and record["rev"] == frozen.TOOLSET_REVISIONS["board"] and record["language"] == "en"
        assert record["mode"] == "board"
        stored = swarm.db.scalar("SELECT value FROM kv WHERE key = ?", (frozen.kv_key(agent),))
        assert stored == dumps(record)
        swarm.notify(agent, "n_restart")
        await run_until(lambda: len(rec.tools(agent)) >= 1)
        assert dumps(rec.tools(agent)[0]) == dumps(record["tools"])  # byte for byte
        await swarm.stop()
        await swarm.start()
        swarm.notify(agent, "n_restart")
        await run_until(lambda: len(rec.tools(agent)) >= 2)
        assert dumps(rec.tools(agent)[1]) == dumps(record["tools"])
        assert frozen.read(swarm.db, agent) == record
    finally:
        await shutdown(swarm)


async def test_a_runtime_without_a_record_gets_the_current_revision(tmp_path, later_revision):
    """AgentRuntime itself does not check revisions (the session does): an agent without a record gets the current
    revision, written at once, whether or not it has a history."""
    swarm, _ = make_swarm(tmp_path, lambda m, t: call("wait"))
    await swarm.start(run_agents=False)
    try:
        agent = swarm.members()[0]["name"]
        seed_history(swarm, agent, pairs=2, size=50)
        assert tool_rows(swarm) == 0
        runtime = AgentRuntime(swarm, agent)
        assert runtime.tools_rev == LATER and "probe" in runtime.tool_names
        assert frozen.read(swarm.db, agent) == frozen.current_record("en")
    finally:
        await shutdown(swarm)


async def test_empty_history_takes_the_current_revision(tmp_path, later_revision):
    swarm, rec = make_swarm(tmp_path, lambda m, t: call("wait"), swarm={"language": "ru"})
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        record = frozen.read(swarm.db, agent)
        assert record["rev"] == LATER and record["language"] == "ru"
        assert frozen.names(record["tools"])[-1] == "probe"
        assert record["tools"][-1]["description"] == "Инструмент более поздней ревизии."
        swarm.notify(agent, "n_restart")
        await run_until(lambda: len(rec.tools(agent)) >= 1)
        assert dumps(rec.tools(agent)[0]) == dumps(record["tools"])
    finally:
        await shutdown(swarm)


async def test_unknown_tool_lists_only_the_agents_own_tools(tmp_path, later_revision):
    swarm, _ = make_swarm(tmp_path, lambda m, t: call("wait"))
    await swarm.start(run_agents=False)
    try:
        agent = swarm.members()[0]["name"]
        frozen.write(swarm.db, agent, frozen.record(OLD, "en", frozen.snapshot(OLD, "en")))
        runtime = AgentRuntime(swarm, agent)
        assert runtime.tools_rev == OLD and "probe" not in runtime.tool_names
        ctx = ToolContext(swarm, agent, runtime)
        for name in ("probe", "prob"):
            text, is_error = await call_tool(ctx, name, {})
            assert is_error and text.startswith(f"Unknown tool '{name}'."), text
            assert "probe" not in text.split("'.", 1)[1], text  # neither suggested nor listed
            assert "Available tools: board, task_get," in text
        # without a frozen tool set (an external member, the user, a tool test) the registry decides
        text, is_error = await call_tool(ToolContext(swarm, agent), "prob", {})
        assert is_error and "Did you mean: probe" in text
    finally:
        await shutdown(swarm)


async def test_arguments_outside_the_frozen_schema_are_ignored(tmp_path):
    swarm, _ = make_swarm(tmp_path, lambda m, t: call("wait"))
    await swarm.start(run_agents=False)
    try:
        agent = swarm.members()[0]["name"]
        record = frozen.record(OLD, "en", frozen.snapshot(OLD, "en"))
        # a frozen set whose tools lack these optional parameters (as one lacks what a later revision adds)
        for tool in record["tools"]:
            if tool["name"] in ("board", "read_file"):
                drop = {"board": "include_closed", "read_file": "limit"}[tool["name"]]
                del tool["parameters"]["properties"][drop]
        frozen.write(swarm.db, agent, record)
        runtime = AgentRuntime(swarm, agent)
        ctx = ToolContext(swarm, agent, runtime)
        swarm.board.create(USER, "Closed task", goal="g")
        swarm.board.update(USER, 1, status=Status.CANCELLED)
        text, is_error = await call_tool(ctx, "board", {"include_closed": True})
        assert not is_error, text
        assert text.endswith("[Ignored unknown argument(s): include_closed. Usage: board(status?, assignee?, tag?)]")
        assert "Closed task" not in text  # the argument the agent's schema lacks had no effect
        text, is_error = await call_tool(ctx, "read_file", {})
        assert is_error and "Usage: read_file(path, offset?) (? = optional)." in text, text
        text, is_error = await call_tool(ctx, "board", [1])
        assert is_error and text.endswith("Usage: board(status?, assignee?, tag?).")
        # the registry's schema still applies to callers without a frozen tool set
        text, _ = await call_tool(ToolContext(swarm, agent), "board", {"include_closed": True})
        assert "Closed task" in text and "Ignored" not in text
    finally:
        await shutdown(swarm)


# ------------------------------------------------------------------------------ the compaction upgrade
async def _upgrade_run(tmp_path: Path, tool: str = "probe") -> tuple[Swarm, Recorder, str]:
    """An agent frozen at revision OLD compacts while the code builds a later revision; after the compaction it calls
    `tool` (a tool of that revision), then `wait`."""

    def policy(messages, tools):
        if is_compaction_request(messages):
            return "SUMMARY of older work"
        last = messages[-1]
        if last.get("role") == "tool":
            return call("wait")
        return call(tool)

    swarm, rec = make_swarm(tmp_path, policy, agents={"context_tokens": 20000})
    await swarm.start(run_agents=False)
    agent = swarm.members()[0]["name"]
    frozen.write(swarm.db, agent, frozen.record(OLD, "en", frozen.snapshot(OLD, "en")))
    seed_history(swarm, agent, pairs=20, size=4000)
    await swarm.stop()
    await swarm.start()
    swarm.notify(agent, "n_restart")
    await run_until(lambda: len(rec.calls.get(agent, [])) >= 3 and swarm.member(agent)["status"] == AgentStatus.IDLE)
    return swarm, rec, agent


async def test_compaction_moves_an_agent_to_the_current_tool_set(tmp_path, later_revision):
    swarm, rec, agent = await _upgrade_run(tmp_path)
    try:
        (compaction, old_tools), (after, new_tools) = rec.calls[agent][:2]
        assert is_compaction_request(compaction)
        assert dumps(old_tools) == dumps(frozen.snapshot(OLD, "en"))  # the compaction request sends the old array
        assert frozen.names(new_tools) == frozen.names(frozen.snapshot(OLD, "en")) + ["probe"]
        record = frozen.read(swarm.db, agent)
        assert record["rev"] == LATER and dumps(record["tools"]) == dumps(new_tools)
        assert swarm.runtimes[agent].tools_rev == LATER
        # the agent is told about its new tool, and it works
        added = prompts.t("en", "tools_added", tools="`probe`")
        first = after[0]["content"]
        assert "SUMMARY of older work" in first and added in first
        assert first.index(added) < first.index(prompts.t("en", "continue"))
        result = next(m for m in rec.calls[agent][2][0] if m.get("role") == "tool" and m.get("name") == "probe")
        assert result["content"] == "probe ok" and not result["is_error"]
        event = swarm.bus.recent(types=("agent.compacted",))[-1]
        assert event["data"]["tools_revision"] == LATER
        span = next(s for t in swarm.tracer.list_traces(agent) for s in swarm.tracer.get_trace(t["id"])["spans"]
                    if s["kind"] == "compaction")
        assert span["meta"]["tools_revision"] == LATER
        # the floor that keeps compactions apart counts the new, larger tool set
        runtime = swarm.runtimes[agent]
        assert runtime._fixed_tokens() > 0 and runtime.tools is not None and runtime.tools[-1]["name"] == "probe"
    finally:
        await shutdown(swarm)


async def test_an_upgrade_that_adds_only_properties_names_no_tool(tmp_path, monkeypatch):
    """A later revision that only adds a property (here `probe_flag` of `board`) moves the agent at its compaction
    like one that adds a tool, and the summary message names no new tool."""
    monkeypatch.setitem(frozen.TOOLSET_REVISIONS, "board", LATER)

    def current(language, mode="board"):
        tools = frozen.snapshot(OLD, language)
        next(t for t in tools if t["name"] == "board")["parameters"]["properties"]["probe_flag"] = {"type": "boolean"}
        return tools

    monkeypatch.setattr(frozen, "current", current)
    swarm, rec, agent = await _upgrade_run(tmp_path, tool="board")
    try:
        (compaction, old_tools), (after, new_tools) = rec.calls[agent][:2]
        assert is_compaction_request(compaction)
        assert dumps(old_tools) == dumps(frozen.snapshot(OLD, "en"))  # the compaction request sends the old array
        assert dumps(new_tools) == dumps(frozen.current("en")) and frozen.read(swarm.db, agent)["rev"] == LATER
        assert prompts.t("en", "tools_added", tools="x").split("x")[0] not in after[0]["content"]
        assert "probe_flag" in swarm.runtimes[agent].tool_parameters("board")["properties"]
    finally:
        await shutdown(swarm)


async def test_agents_keep_their_revision_when_a_later_one_is_built(tmp_path, later_revision):
    """A later revision changes nothing for an agent that already ran: one frozen at revision OLD with a history and
    one frozen at it without one send revision OLD byte for byte, run after run; only a new agent gets the later
    revision."""
    swarm, rec = make_swarm(tmp_path, lambda m, t: call("wait"), swarm={"start_agents": 3})
    await swarm.start(run_agents=False)
    names = [m["name"] for m in swarm.members()]
    before, kept, new = names
    seed_history(swarm, before, pairs=1, size=10)
    for name in (before, kept):
        frozen.write(swarm.db, name, frozen.record(OLD, "en", frozen.snapshot(OLD, "en")))
    await swarm.stop()
    try:
        counts: dict[str, int] = {}
        for _ in range(2):  # a restart changes nothing
            await swarm.start()
            for name in names:
                swarm.notify(name, "n_restart")
            await run_until(lambda: all(len(rec.tools(n)) > counts.get(n, 0) for n in names))
            counts = {n: len(rec.tools(n)) for n in names}
            await swarm.stop()
        old_tools, later_tools = frozen.snapshot(OLD, "en"), frozen.current("en")
        for name, rev, tools in ((before, OLD, old_tools), (kept, OLD, old_tools), (new, LATER, later_tools)):
            assert {dumps(t) for t in rec.tools(name)} == {dumps(tools)}, name
            assert frozen.read(swarm.db, name)["rev"] == rev
        assert "probe" not in frozen.names(rec.tools(before)[0]) and "probe" not in frozen.names(rec.tools(kept)[0])
        assert frozen.names(rec.tools(new)[0])[-1] == "probe"
    finally:
        swarm.close()


async def test_the_upgrade_is_atomic_with_the_history_rewrite(tmp_path, later_revision, monkeypatch):
    swarm, _ = make_swarm(tmp_path, lambda m, t: call("wait"))
    await swarm.start(run_agents=False)
    try:
        agent = swarm.members()[0]["name"]
        frozen.write(swarm.db, agent, frozen.record(OLD, "en", frozen.snapshot(OLD, "en")))
        seed_history(swarm, agent, pairs=2, size=50)
        runtime = AgentRuntime(swarm, agent)
        runtime._load_history()
        before = swarm.db.all("SELECT seq, msg FROM history WHERE agent = ? ORDER BY seq", (agent,))
        real_write = frozen.write

        def failing_write(db, name, rec):
            real_write(db, name, rec)
            raise RuntimeError("disk full")

        monkeypatch.setattr(frozen, "write", failing_write)
        with pytest.raises(RuntimeError):
            runtime._replace_history([{"role": "user", "content": "summary"}], tools=frozen.current_record("en"))
        after = swarm.db.all("SELECT seq, msg FROM history WHERE agent = ? ORDER BY seq", (agent,))
        assert [tuple(r) for r in after] == [tuple(r) for r in before]
        assert frozen.read(swarm.db, agent)["rev"] == OLD and runtime.tools_rev == OLD
        assert "probe" not in runtime.tool_names
        monkeypatch.setattr(frozen, "write", real_write)
        runtime._replace_history([{"role": "user", "content": "summary"}], tools=frozen.current_record("en"))
        assert frozen.read(swarm.db, agent)["rev"] == LATER and "probe" in runtime.tool_names
    finally:
        await shutdown(swarm)


async def test_traces_show_the_tools_each_call_used(tmp_path, later_revision):
    swarm, rec, agent = await _upgrade_run(tmp_path)
    try:
        spans = [s for t in reversed(swarm.tracer.list_traces(agent)) for s in swarm.tracer.get_trace(t["id"])["spans"]
                 if s["kind"] == "llm"]
        assert [s["input"]["tools_rev"] for s in spans[:3]] == [OLD, LATER, LATER]
        before = swarm.tracer.span_context(spans[0]["id"])
        after = swarm.tracer.span_context(spans[1]["id"])
        assert dumps(before["tools"]) == dumps(rec.calls[agent][0][1]) == dumps(frozen.snapshot(OLD, "en"))
        assert dumps(after["tools"]) == dumps(rec.calls[agent][1][1])
        # a revision the record no longer holds comes from the shipped snapshot
        frozen.write(swarm.db, agent, frozen.record(LATER + 1, "en", []))
        assert dumps(swarm.tracer.span_context(spans[0]["id"])["tools"]) == dumps(frozen.snapshot(OLD, "en"))
    finally:
        await shutdown(swarm)


async def test_provider_requests_are_byte_identical_across_restarts(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda m, t: call("wait"), swarm={"language": "ru"})
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        for n in (1, 2):
            swarm.notify(agent, "n_restart")
            await run_until(lambda: len(rec.tools(agent)) >= n)
            await swarm.stop()
            await swarm.start()
        profile = swarm.config.model(None)
        msgs = [{"role": "user", "content": "x"}]
        sent = rec.tools(agent)
        a = [json.dumps(anthropic_llm.build_request(profile, "s", msgs, t, use_betas=False)["tools"],
                        ensure_ascii=False) for t in sent]
        o = [json.dumps(openai_llm.build_request(profile, "s", msgs, t)["tools"], ensure_ascii=False) for t in sent]
        snap = frozen.snapshot(frozen.TOOLSET_REVISIONS["board"], "ru")  # a new agent: the current revision
        assert a[0] == a[1] == json.dumps(anthropic_llm.convert_tools(snap), ensure_ascii=False)
        assert o[0] == o[1] == json.dumps(openai_llm.convert_tools(snap), ensure_ascii=False)
    finally:
        await shutdown(swarm)


async def test_view_only_access_writes_no_tool_record(tmp_path, capsys):
    """A member with a history and no tool-set record ran under an earlier version (it ran before tool sets were
    frozen): the project is not opened again, neither to view it nor by `dreamteam status`, and nothing is written
    into its data folder (no logs folder, no -wal or -shm next to the database)."""
    project = tmp_path / "swarm"
    assert cli.main(["init", str(project), "--lang", "en", "--name", "View only"]) == 0
    text = (project / "dreamteam.toml").read_text(encoding="utf-8")
    text = text.replace("start_agents = 3", "start_agents = 1").replace(
        'library_dir = "~/.dreamteam/library"', f'library_dir = "{(tmp_path / "lib").as_posix()}"')
    (project / "dreamteam.toml").write_text(text, encoding="utf-8")
    swarm = Swarm(project)
    await swarm.start(run_agents=False)  # members, but no runtime
    agent = swarm.members()[0]["name"]
    seed_history(swarm, agent, pairs=1, size=10)
    assert tool_rows(swarm) == 0
    await swarm.stop()
    swarm.close()
    data = Config.load(project).data_dir
    shutil.rmtree(data / "logs")

    def snapshot() -> dict[str, tuple[int, int]]:
        return {p.relative_to(data).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
                for p in sorted(data.rglob("*"))}

    before = snapshot()
    assert "swarm.db" in before and "swarm.db-wal" not in before and "swarm.db-shm" not in before
    message = swarm_module.PROJECT_EARLIER_VERSION_TEXT.format(dir=Config.load(project).project_dir,
                                                               version=__version__)
    with pytest.raises(swarm_module.EarlierVersionError) as refused:
        Swarm(project)
    assert str(refused.value) == message and isinstance(refused.value, ValueError)
    assert snapshot() == before
    capsys.readouterr()
    assert await asyncio.to_thread(cli.main, ["status", str(project)]) == 1  # (status runs its own event loop)
    out = capsys.readouterr()
    assert out.err.strip() == message and "View only" not in out.out
    assert snapshot() == before and not (data / "logs").exists()


async def test_has_tool_and_member_tool_names(tmp_path, later_revision):
    swarm, _ = make_swarm(tmp_path, lambda m, t: call("wait"), swarm={"start_agents": 3})
    await swarm.start(run_agents=False)
    try:
        old, new, unstarted = [m["name"] for m in swarm.members()]
        frozen.write(swarm.db, old, frozen.record(OLD, "en", frozen.snapshot(OLD, "en")))
        seed_history(swarm, old, pairs=1, size=10)
        old_rt, new_rt = AgentRuntime(swarm, old), AgentRuntime(swarm, new)
        assert (old_rt.tools_rev, new_rt.tools_rev) == (OLD, LATER)
        assert not ToolContext(swarm, old, old_rt).has_tool("probe")
        assert ToolContext(swarm, old, old_rt).has_tool("board")
        assert ToolContext(swarm, new, new_rt).has_tool("probe")
        assert ToolContext(swarm, old).has_tool("probe")  # no runtime: the registry for its kind
        assert ToolContext(swarm, USER).has_tool("set_goal") and not ToolContext(swarm, USER).has_tool("wait")
        assert not ToolContext(swarm, USER).has_tool("no_such_tool")
        await call_tool(ToolContext(swarm, USER), "join_swarm", {"role": "Reviewer", "name": "Claude Code"})
        ext = ToolContext(swarm, "Claude Code")
        assert ext.kind == AgentKind.EXTERNAL and ext.has_tool("wait_for_events") and not ext.has_tool("wait")

        names = frozen.member_tool_names
        swarm.runtimes[old] = old_rt
        try:
            assert names(swarm, swarm.member(old)) == old_rt.tool_names
        finally:
            swarm.runtimes.pop(old)
        assert "probe" not in names(swarm, swarm.member(old))  # from its record
        assert "probe" in names(swarm, swarm.member(new))
        rows = tool_rows(swarm)
        assert "probe" in names(swarm, swarm.member(unstarted))  # what its first start will freeze...
        seed_history(swarm, unstarted, pairs=1, size=10)
        assert "probe" in names(swarm, swarm.member(unstarted))  # a history without a record: still the current set
        assert tool_rows(swarm) == rows  # ...and nothing is written
        assert "wait_for_events" in names(swarm, swarm.member("Claude Code"))
        assert names(swarm, {"name": USER, "kind": "user"}) == {s.name for s in tools_module.tools_for("user", "board")}
    finally:
        await shutdown(swarm)


async def test_an_invalid_record_is_replaced(tmp_path, caplog):
    swarm, _ = make_swarm(tmp_path, lambda m, t: call("wait"))
    await swarm.start(run_agents=False)
    try:
        agent = swarm.members()[0]["name"]
        key = frozen.kv_key(agent)
        for bad in ({"rev": "1", "tools": []}, {"rev": True, "tools": []}, {"rev": 1, "tools": [{"name": "board"}]},
                    {"rev": 1, "tools": "board"}, ["board"]):
            swarm.db.set(key, bad)
            assert frozen.read(swarm.db, agent) is None, bad
        assert "invalid" in caplog.text
        swarm.db.execute("UPDATE kv SET value = ? WHERE key = ?", ("{not json", key))
        assert frozen.read(swarm.db, agent) is None
        runtime = AgentRuntime(swarm, agent)  # a record it cannot use is rebuilt like a missing one
        assert runtime.tools_rev == frozen.TOOLSET_REVISIONS["board"]
        assert frozen.read(swarm.db, agent) == frozen.current_record("en")
    finally:
        await shutdown(swarm)
