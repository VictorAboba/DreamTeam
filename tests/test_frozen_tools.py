"""Frozen LLM tool sets (tools/frozen.py, docs/SPEC.md §6.1): the released revisions never change and stay
served by the registry, every agent sends exactly the array it was given, and it moves to the current revision
only inside a compaction. Agents run on ScriptedLLM; a later revision is simulated by patching
TOOLSET_REVISION and `frozen.current` and registering a `probe` tool."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from dreamteam import cli, prompts
from dreamteam import swarm as swarm_module
from dreamteam import tools as tools_module
from dreamteam.agent import AgentRuntime
from dreamteam.config import Config
from dreamteam.db import dumps
from dreamteam.llm import anthropic_llm, openai_llm
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentKind, AgentStatus, Status
from dreamteam.swarm import Swarm
from dreamteam.tools import REGISTRY, ToolContext, ToolSpec, call_tool, frozen, schemas_for

# exact-order digests (sha256 of db.dumps) of every released revision: what agents send, byte for byte
RELEASED = {
    1: {"en": "f11401bf332caa959f2adf121fbb9808dc339fd9ed07aab22e54b1d4fcbd4561",
        "ru": "dbf9ca6df4166f761353c267ede532184abda194b1fd7418434bd9950860c270"},
    # background jobs: shell(background), jobs, job_stop; new shell and wait descriptions
    2: {"en": "92f7a5df2601bc26eb4d3414afa6117cfda5eddc45624e741d187266075cba70",
        "ru": "768d9936471b80ec2fec0922e9e853a06d9ef3e689dce186ea3bacba6054b032"},
}
# the digest tests/test_tools.py pinned for the tool set agents had before tool sets were frozen
BASELINE_SORTED = "e3c5cbc798e3aafddcd44284bb515d9655e68b953e6ca707f67063dcad7cda85"
LANGS = ("en", "ru")
LATER = 99  # the simulated later revision


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
    """The code builds revision 99 = revision 1 + a `probe` tool (as a real revision would add a tool)."""
    tools_module._load()
    spec = ToolSpec(name="probe", description={"en": "A tool of a later revision.",
                                               "ru": "Инструмент более поздней ревизии."},
                    parameters={"type": "object", "properties": {}, "required": []}, handler=probe_tool,
                    audience=frozenset({"llm"}))
    monkeypatch.setitem(REGISTRY, "probe", spec)
    monkeypatch.setattr(frozen, "TOOLSET_REVISION", LATER)

    def current(language):
        return frozen.snapshot(1, language) + [{"name": "probe", "description": spec.describe(language or "en"),
                                                "parameters": copy.deepcopy(spec.parameters)}]

    monkeypatch.setattr(frozen, "current", current)
    return spec


@pytest.fixture(autouse=True)
def fast_scheduler(monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)


# ------------------------------------------------------------------------------ released revisions
def test_baseline_snapshot_is_the_tool_set_agents_had():
    en, ru = frozen.snapshot(1, "en"), frozen.snapshot(1, "ru")
    assert sha(json.dumps([en, ru], ensure_ascii=False, sort_keys=True)) == BASELINE_SORTED
    assert sha(dumps(en)) == RELEASED[1]["en"] and sha(dumps(ru)) == RELEASED[1]["ru"]
    assert len(en) == len(ru) == 30 and frozen.names(en) == frozen.names(ru)
    assert frozen.BASELINE_REVISION == 1
    # a language without texts gets the English array, like schemas_for
    assert frozen.snapshot(1, "de") == en and frozen.snapshot(1, None) == en


def test_released_tool_sets_never_change():
    assert frozen.revisions() == list(range(1, frozen.TOOLSET_REVISION + 1)) == sorted(RELEASED)
    for rev, digests in RELEASED.items():
        for lang in LANGS:
            assert sha(dumps(frozen.snapshot(rev, lang))) == digests[lang], (rev, lang)


def test_the_code_builds_the_current_revision():
    for lang in LANGS:
        assert dumps(frozen.current(lang)) == dumps(frozen.snapshot(frozen.TOOLSET_REVISION, lang)), (
            f"The LLM tool schemas ({lang}) differ from revision {frozen.TOOLSET_REVISION}. A schema change is a new "
            "revision: bump TOOLSET_REVISION in tools/frozen.py, run `python -m dreamteam.tools.frozen --write`, "
            "and pin its digests in RELEASED.")
    assert frozen.main(["--check"]) == 0


def _types(schema: dict) -> tuple:
    return schema.get("type"), (schema.get("items") or {}).get("type")


def test_every_released_tool_set_is_still_served():
    """Arguments are validated against the current registry, so it must accept everything a released array
    allows: no tool or property removed or retyped, no new required argument, enums only grow."""
    for rev in frozen.revisions():
        for lang in LANGS:
            for tool in frozen.snapshot(rev, lang):
                where = (rev, lang, tool["name"])
                spec = REGISTRY.get(tool["name"])
                assert spec is not None and "llm" in spec.audience, where
                old, new = tool["parameters"], spec.parameters
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
            schemas = schemas_for(kind, lang)
            assert frozen.normalize(schemas) == schemas
            assert dumps(frozen.normalize(schemas)) == dumps(schemas)


def test_write_refuses_to_change_a_released_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(frozen, "SNAPSHOT_DIR", tmp_path)
    frozen._snapshot_text.cache_clear()
    rev = frozen.TOOLSET_REVISION
    path = tmp_path / f"llm_r{rev}.json"
    try:
        assert frozen.main(["--check"]) == 1  # missing
        assert frozen.main(["--write"]) == 0
        written = path.read_bytes()
        assert b"\r\n" not in written and json.loads(written)["revision"] == rev
        assert frozen.main(["--check"]) == 0 and frozen.main(["--write"]) == 0  # unchanged: rewritten as is
        path.write_text(f'{{"revision": {rev}, "en": [], "ru": []}}', encoding="utf-8")
        assert frozen.main(["--check"]) == 1
        assert frozen.main(["--write"]) == 2  # a released revision is never edited
        assert frozen.main(["--write", "--force"]) == 0 and path.read_bytes() == written
    finally:
        frozen._snapshot_text.cache_clear()


# --------------------------------------------------------------------------------- agents' records
async def test_new_agent_freezes_its_tools_before_the_first_call(tmp_path):
    swarm, rec = make_swarm(tmp_path, lambda m, t: call("wait"))
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        record = frozen.read(swarm.db, agent)
        assert record is not None and record["rev"] == frozen.TOOLSET_REVISION and record["language"] == "en"
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


async def test_agent_created_before_frozen_tool_sets_keeps_the_baseline(tmp_path, later_revision):
    swarm, rec = make_swarm(tmp_path, lambda m, t: call("wait"))
    await swarm.start(run_agents=False)
    agent = swarm.members()[0]["name"]
    seed_history(swarm, agent, pairs=2, size=50)  # it ran before tool sets were frozen: no record
    assert tool_rows(swarm) == 0
    await swarm.stop()
    await swarm.start()
    try:
        swarm.notify(agent, "n_restart")
        await run_until(lambda: len(rec.tools(agent)) >= 1)
        for lang_tools in rec.tools(agent):
            assert dumps(lang_tools) == dumps(frozen.snapshot(1, "en"))
        assert frozen.read(swarm.db, agent)["rev"] == 1
        assert swarm.runtimes[agent].tools_rev == 1
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
        frozen.write(swarm.db, agent, frozen.record(1, "en", frozen.snapshot(1, "en")))
        runtime = AgentRuntime(swarm, agent)
        assert runtime.tools_rev == 1 and "probe" not in runtime.tool_names
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
        record = frozen.record(1, "en", frozen.snapshot(1, "en"))
        for tool in record["tools"]:  # an older revision whose tools lacked these optional parameters
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
    """A baseline agent (history, no record) compacts while the code builds a later revision; after the
    compaction it calls `tool` (a tool of that revision), then `wait`."""

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
        assert dumps(old_tools) == dumps(frozen.snapshot(1, "en"))  # the compaction request sends the old array
        assert frozen.names(new_tools) == frozen.names(frozen.snapshot(1, "en")) + ["probe"]
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


async def test_agents_of_revision_1_keep_it_when_revision_2_is_released(tmp_path):
    """Revision 2 (background jobs) changes nothing for an agent that already ran: one with a history but no record
    (it ran before tool sets were frozen) and one frozen at revision 1 send revision 1 byte for byte, run after run;
    only a new agent gets revision 2."""
    swarm, rec = make_swarm(tmp_path, lambda m, t: call("wait"), swarm={"start_agents": 3})
    await swarm.start(run_agents=False)
    names = [m["name"] for m in swarm.members()]
    before, kept, new = names
    seed_history(swarm, before, pairs=1, size=10)
    frozen.write(swarm.db, kept, frozen.record(1, "en", frozen.snapshot(1, "en")))
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
        for name, rev in ((before, 1), (kept, 1), (new, 2)):
            assert {dumps(t) for t in rec.tools(name)} == {dumps(frozen.snapshot(rev, "en"))}, name
            assert frozen.read(swarm.db, name)["rev"] == rev
        assert "jobs" not in frozen.names(rec.tools(before)[0]) and "jobs" in frozen.names(rec.tools(new)[0])
    finally:
        swarm.close()


async def test_a_compaction_moves_a_revision_1_agent_to_revision_2(tmp_path):
    swarm, rec, agent = await _upgrade_run(tmp_path, tool="jobs")
    try:
        (compaction, old_tools), (after, new_tools) = rec.calls[agent][:2]
        assert is_compaction_request(compaction)
        assert dumps(old_tools) == dumps(frozen.snapshot(1, "en"))  # the compaction request: still revision 1
        assert dumps(new_tools) == dumps(frozen.snapshot(2, "en"))
        assert frozen.read(swarm.db, agent)["rev"] == 2 == swarm.runtimes[agent].tools_rev
        # it is told about the new tools; `background` (a new parameter of shell) is in shell's own description
        assert prompts.t("en", "tools_added", tools="`jobs`, `job_stop`") in after[0]["content"]
        result = next(m for m in rec.calls[agent][2][0] if m.get("role") == "tool" and m.get("name") == "jobs")
        assert result["content"] == "No background jobs are running." and not result["is_error"]
    finally:
        await shutdown(swarm)


async def test_the_upgrade_is_atomic_with_the_history_rewrite(tmp_path, later_revision, monkeypatch):
    swarm, _ = make_swarm(tmp_path, lambda m, t: call("wait"))
    await swarm.start(run_agents=False)
    try:
        agent = swarm.members()[0]["name"]
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
        assert frozen.read(swarm.db, agent)["rev"] == 1 and runtime.tools_rev == 1
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
        assert [s["input"]["tools_rev"] for s in spans[:3]] == [1, LATER, LATER]
        before = swarm.tracer.span_context(spans[0]["id"])
        after = swarm.tracer.span_context(spans[1]["id"])
        assert dumps(before["tools"]) == dumps(rec.calls[agent][0][1]) == dumps(frozen.snapshot(1, "en"))
        assert dumps(after["tools"]) == dumps(rec.calls[agent][1][1])
        # a span recorded before tool sets were frozen has no revision: the agent then sent the baseline
        trace = swarm.tracer.begin(agent, "old")
        old = trace.llm_start(0)
        assert "tools_rev" not in swarm.tracer.get_span(old.id)["input"]
        assert dumps(swarm.tracer.span_context(old.id)["tools"]) == dumps(frozen.snapshot(1, "en"))
        # a revision the record no longer holds comes from the shipped snapshot
        frozen.write(swarm.db, agent, frozen.record(LATER + 1, "en", []))
        assert dumps(swarm.tracer.span_context(spans[0]["id"])["tools"]) == dumps(frozen.snapshot(1, "en"))
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
        snap = frozen.snapshot(frozen.TOOLSET_REVISION, "ru")  # a new agent: the current revision
        assert a[0] == a[1] == json.dumps(anthropic_llm.convert_tools(snap), ensure_ascii=False)
        assert o[0] == o[1] == json.dumps(openai_llm.convert_tools(snap), ensure_ascii=False)
    finally:
        await shutdown(swarm)


async def test_view_only_access_writes_no_tool_record(tmp_path, capsys):
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
    span = swarm.tracer.begin(agent, "x").llm_start(1, tools_rev=1)
    await swarm.stop()
    swarm.close()
    view = Swarm(project)
    try:
        for tool in ("swarm_status", "agents", "board"):
            text, is_error = await call_tool(ToolContext(view, USER), tool, {})
            assert not is_error, text
        assert dumps(view.tracer.span_context(span.id)["tools"]) == dumps(frozen.snapshot(1, "en"))
        assert frozen.member_tool_names(view, view.member(agent)) == frozenset(frozen.names(frozen.snapshot(1, "en")))
        assert tool_rows(view) == 0
    finally:
        view.close()
    capsys.readouterr()
    assert await asyncio.to_thread(cli.main, ["status", str(project)]) == 0  # it runs its own event loop
    assert "View only" in capsys.readouterr().out
    view = Swarm(project)
    try:
        assert tool_rows(view) == 0
    finally:
        view.close()


async def test_has_tool_and_member_tool_names(tmp_path, later_revision):
    swarm, _ = make_swarm(tmp_path, lambda m, t: call("wait"), swarm={"start_agents": 3})
    await swarm.start(run_agents=False)
    try:
        old, new, unstarted = [m["name"] for m in swarm.members()]
        seed_history(swarm, old, pairs=1, size=10)
        old_rt, new_rt = AgentRuntime(swarm, old), AgentRuntime(swarm, new)
        assert (old_rt.tools_rev, new_rt.tools_rev) == (1, LATER)
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
        assert "probe" not in names(swarm, swarm.member(unstarted))
        assert tool_rows(swarm) == rows  # ...and nothing is written
        assert "wait_for_events" in names(swarm, swarm.member("Claude Code"))
        assert names(swarm, {"name": USER, "kind": "user"}) == {s.name for s in tools_module.tools_for("user")}
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
        assert runtime.tools_rev == frozen.TOOLSET_REVISION
        assert frozen.read(swarm.db, agent) == frozen.current_record("en")
    finally:
        await shutdown(swarm)
