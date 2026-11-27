"""The calls held after a read-before-write refusal (docs/SPEC.md §7.3 "A `wait` after a refusal", §22.11): a
`ReadRefusal` flags the LLM agent's response, and its later `wait`, `task_complete`, `goal_complete` and like on #1 are
not run; the flag lasts one response, a refusal of another kind sets none, and a history that ends with a held `wait`
is continued after a restart (n_restart). The file and forum checks are stood in for by a `write_file` that refuses
the text REFUSE, so this file tests the shared machinery only. ScriptedLLM, no network."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from dreamteam import prompts
from dreamteam import swarm as swarm_module
from dreamteam.agent import AgentRuntime
from dreamteam.config import Config
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, ReadRefusal, Status, ToolError
from dreamteam.swarm import Swarm
from dreamteam.tools import REGISTRY, ToolContext, call_tool, frozen, hold_after_refusal, tools_for

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

WAIT = {"name": "wait", "arguments": {}}
REFUSED = {"name": "write_file", "arguments": {"path": "notes.md", "content": "REFUSE"}}


def call(name: str, **arguments: Any) -> dict:
    return {"name": name, "arguments": arguments}


def make_config(tmp_path: Path, mode: str = "board", language: str = "en") -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Hold swarm", "language": language, "start_agents": 1, "max_agents": 4, "max_cost_usd": 0,
                  "mode": mode, "clarify": False, "independent_review": False},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 3600, "max_steps_per_turn": 12},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    return Config.from_dict(tmp_path / "project", raw)


class Script:
    """Each LLM call of an agent answers with the next queued response (none queued: just `wait`) and records the
    messages it was given."""

    def __init__(self) -> None:
        self.todo: dict[str, list[list[dict]]] = {}
        self.calls: dict[str, list[list[dict]]] = {}

    def factory(self, profile: Any, agent: str) -> ScriptedLLM:
        def handler(system: str, messages: list[dict], tools: list[dict]) -> dict:
            self.calls.setdefault(agent, []).append(json.loads(json.dumps(messages)))
            queue = self.todo.get(agent) or []
            return {"tool_calls": queue.pop(0) if queue else [WAIT]}

        return ScriptedLLM(handler, profile)

    def count(self, name: str) -> int:
        return len(self.calls.get(name, []))


@pytest.fixture(autouse=True)
def fast_scheduler(monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)


@pytest.fixture(autouse=True)
def refusing_write(monkeypatch):
    """`write_file` with the content REFUSE raises a ReadRefusal (the file check's stand-in); any other content
    writes as always."""
    tools_for("llm")   # loads the registry
    spec = REGISTRY["write_file"]
    original = spec.handler

    async def handler(ctx: ToolContext, args: dict[str, Any]) -> str:
        if args.get("content") == "REFUSE":
            raise ReadRefusal("Not written: 'notes.md' changed since you last read or wrote it.")
        return await original(ctx, args)

    monkeypatch.setattr(spec, "handler", handler)


@pytest.fixture
def open_swarms():
    made: list[Swarm] = []
    yield made
    for sw in made:
        try:
            sw.close()
        except Exception:  # noqa: BLE001 - closed already
            pass


async def run_until(predicate: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


async def started(tmp_path: Path, open_swarms: list, mode: str = "board", language: str = "en",
                  ) -> tuple[Swarm, Script, str]:
    script = Script()
    sw = Swarm(config=make_config(tmp_path, mode, language), llm_factory=script.factory)
    open_swarms.append(sw)
    await sw.start()
    return sw, script, sw.members()[0]["name"]


def results(sw: Swarm, name: str, tool: str) -> list[dict]:
    rows = sw.db.all("SELECT msg FROM history WHERE agent = ? ORDER BY seq", (name,))
    return [m for m in (json.loads(r["msg"]) for r in rows) if m.get("role") == "tool" and m.get("name") == tool]


async def poke(sw: Swarm, name: str) -> None:
    text, is_error = await call_tool(ToolContext(sw, USER), "send_message", {"to": [name], "text": "Go on."})
    assert not is_error, text


async def settled(script: Script, name: str, n: int) -> None:
    """The agent made n LLM calls and then stayed asleep for a moment."""
    await run_until(lambda: script.count(name) >= n)
    await asyncio.sleep(0.3)
    assert script.count(name) == n, script.count(name)


# ------------------------------------------------------------------------------------------------ the wait
@pytest.mark.parametrize("mode", ["board", "thread"])
@pytest.mark.parametrize("language", ["en", "ru"])
async def test_a_wait_after_a_refusal_keeps_the_activation_running(tmp_path, open_swarms, mode, language):
    sw, script, a = await started(tmp_path, open_swarms, mode, language)
    try:
        await settled(script, a, 0)   # no goal: nobody is woken
        script.todo[a] = [[REFUSED, WAIT], [call("write_file", path="notes.md", content="retry"), WAIT]]
        await poke(sw, a)
        # the held wait did not end the turn: the same activation made its next step, whose wait ended it
        await settled(script, a, 2)
        waits = results(sw, a, "wait")
        assert waits[0]["is_error"] and waits[0]["content"] == "Error: " + prompts.t(language, "wait_refused",
                                                                                      tool="write_file")
        assert not waits[1]["is_error"]
        writes = results(sw, a, "write_file")
        assert writes[0]["is_error"] and not writes[1]["is_error"]
        assert (sw.workspace.root / "notes.md").read_text(encoding="utf-8") == "retry"
        assert sw.runtimes[a].read_refused is None
        assert not sw.runtimes[a]._ended_mid_activation()
    finally:
        await sw.stop()


async def test_a_wait_after_another_error_ends_the_turn_as_before(tmp_path, open_swarms):
    sw, script, a = await started(tmp_path, open_swarms)
    try:
        script.todo[a] = [[call("read_file", path="missing.md"), WAIT]]
        await poke(sw, a)
        await settled(script, a, 1)
        assert results(sw, a, "read_file")[0]["is_error"]
        assert not results(sw, a, "wait")[0]["is_error"]
        assert sw.runtimes[a].read_refused is None
    finally:
        await sw.stop()


async def test_the_flag_lasts_one_response(tmp_path, open_swarms):
    sw, script, a = await started(tmp_path, open_swarms)
    try:
        # the refused write ends its response; the next response's wait runs and ends the turn
        script.todo[a] = [[REFUSED], [WAIT]]
        await poke(sw, a)
        await settled(script, a, 2)
        assert [w["is_error"] for w in results(sw, a, "wait")] == [False]
        # a held wait, then a refusal and a wait in one response again: held again
        script.todo[a] = [[REFUSED, WAIT], [REFUSED, WAIT], [WAIT]]
        await poke(sw, a)
        await settled(script, a, 5)
        assert [w["is_error"] for w in results(sw, a, "wait")] == [False, True, True, False]
    finally:
        await sw.stop()


# ------------------------------------------------------------------------------------- the board's completions
async def test_task_complete_after_a_refusal_is_held(tmp_path, open_swarms):
    sw, script, a = await started(tmp_path, open_swarms)
    try:
        task = sw.board.create(USER, "Write the notes", goal="notes.md exists")
        sw.board.claim(a, task.id)
        script.todo[a] = [[REFUSED, call("task_complete", task_id=task.id, result="done"), WAIT],
                          [call("task_complete", task_id=task.id, result="done"), WAIT]]
        await poke(sw, a)
        await run_until(lambda: len(results(sw, a, "task_complete")) >= 1)
        first = results(sw, a, "task_complete")[0]
        assert first["is_error"] and first["content"] == "Error: " + prompts.t("en", "complete_refused",
                                                                               tool="write_file")
        await run_until(lambda: len(results(sw, a, "task_complete")) >= 2)
        assert not results(sw, a, "task_complete")[1]["is_error"]
        assert sw.board.get(task.id).status == Status.DONE
        # the held call left the task in progress until the next response
        assert results(sw, a, "wait")[0]["is_error"]
    finally:
        await sw.stop()


async def test_task_complete_after_a_lock_refusal_runs(tmp_path, open_swarms):
    sw, script, a = await started(tmp_path, open_swarms)
    try:
        await sw.hire(USER, "Generalist", name="Boris Lee", kind="external")
        text, is_error = await call_tool(ToolContext(sw, "Boris Lee"), "lock_file", {"path": "notes.md"})
        assert not is_error, text
        task = sw.board.create(USER, "Write the notes", goal="notes.md exists")
        sw.board.claim(a, task.id)
        script.todo[a] = [[call("write_file", path="notes.md", content="x"),
                           call("task_complete", task_id=task.id, result="done"), WAIT]]
        await poke(sw, a)
        await run_until(lambda: len(results(sw, a, "task_complete")) >= 1)
        assert results(sw, a, "write_file")[0]["is_error"]
        assert not results(sw, a, "task_complete")[0]["is_error"]
        assert sw.board.get(task.id).status == Status.DONE
    finally:
        await sw.stop()


async def test_goal_complete_after_a_refusal_is_held(tmp_path, open_swarms):
    sw, script, a = await started(tmp_path, open_swarms)
    try:
        script.todo[a] = [[REFUSED, call("goal_complete", summary="All done."), WAIT],
                          [call("goal_complete", summary="All done."), WAIT]]
        sw.set_goal(USER, "Write the notes")
        await run_until(lambda: len(results(sw, a, "goal_complete")) >= 1)
        first = results(sw, a, "goal_complete")[0]
        assert first["is_error"] and first["content"] == "Error: " + prompts.t("en", "goal_complete_refused",
                                                                               tool="write_file")
        await run_until(lambda: len(results(sw, a, "goal_complete")) >= 2)
        assert not results(sw, a, "goal_complete")[1]["is_error"]
        assert sw.goal()["status"] == "done"
    finally:
        await sw.stop()


# ------------------------------------------------------------------------------------------- the thread's like
async def test_a_like_on_1_after_a_refusal_is_held(tmp_path, open_swarms):
    sw, script, a = await started(tmp_path, open_swarms, mode="thread")
    try:
        # the only agent's like on #1 would complete the goal (democracy: more than half of one)
        script.todo[a] = [[REFUSED, call("vote", target="#1", value="like"), call("vote", target="#2", value="like"),
                           WAIT],
                          [REFUSED, call("vote", target="#1", value="dislike"), WAIT]]
        sw.set_goal(USER, "Write the notes")
        await settled(script, a, 3)   # the held waits kept the activation running; the default wait ended it
        votes = results(sw, a, "vote")
        assert votes[0]["is_error"] and votes[0]["content"] == "Error: " + prompts.t("en", "vote_refused",
                                                                                     tool="write_file")
        assert not votes[1]["is_error"]   # a like on another post runs
        # the dislike on #1 after a refusal runs: it cannot complete the goal
        assert not votes[2]["is_error"]
        assert sw.db.scalar("SELECT value FROM votes WHERE target = 'p1' AND voter = ?", (a,)) == -1
        assert sw.db.scalar("SELECT value FROM votes WHERE target = 'p2' AND voter = ?", (a,)) == 1
        assert sw.goal()["status"] == "active"
        # a like on #1 alone in a later response runs, and completes the goal
        script.todo[a] = [[call("vote", target="#1", value="like"), WAIT]]
        await poke(sw, a)
        await run_until(lambda: len(results(sw, a, "vote")) >= 4)
        assert not results(sw, a, "vote")[3]["is_error"]
        await run_until(lambda: sw.goal()["status"] == "done")
    finally:
        await sw.stop()


# --------------------------------------------------------------------------------------------------- restart
def write_history(sw: Swarm, name: str, msgs: list[dict]) -> None:
    for seq, msg in enumerate(msgs):
        sw.db.execute("INSERT INTO history(agent, seq, msg) VALUES (?, ?, ?)", (name, seq, json.dumps(msg)))
    frozen.write(sw.db, name, frozen.current_record("en"))


async def test_a_history_ending_with_a_held_wait_is_continued_after_a_restart(tmp_path, open_swarms):
    script = Script()
    sw = Swarm(config=make_config(tmp_path), llm_factory=script.factory)
    open_swarms.append(sw)
    await sw.start(run_agents=False)
    a = sw.members()[0]["name"]
    write_history(sw, a, [
        {"role": "user", "content": "wake"},
        {"role": "assistant", "content": "", "raw": None,
         "tool_calls": [{"id": "c1", "name": "write_file", "arguments": REFUSED["arguments"]},
                        {"id": "c2", "name": "wait", "arguments": {}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "write_file", "content": "Error: Not written", "is_error": True},
        {"role": "tool", "tool_call_id": "c2", "name": "wait", "content": "Error: Not waiting", "is_error": True},
    ])
    await sw.stop()
    sw.close()

    script2 = Script()
    sw2 = Swarm(config=make_config(tmp_path), llm_factory=script2.factory)
    open_swarms.append(sw2)
    script2.todo[a] = [[call("write_file", path="notes.md", content="retry"), WAIT]]
    await sw2.start()
    try:
        await settled(script2, a, 1)
        assert "restarted" in "\n".join(str(m.get("content") or "") for m in script2.calls[a][0])
        kinds = [r["kind"] for r in sw2.db.all("SELECT kind FROM notifications WHERE recipient = ?", (a,))]
        assert kinds == ["n_restart"], kinds   # no open task, no pending event: only the restart woke it
        assert (sw2.workspace.root / "notes.md").read_text(encoding="utf-8") == "retry"
    finally:
        await sw2.stop()


@pytest.mark.parametrize(("last", "mid"), [
    ({"role": "tool", "name": "wait", "content": "OK, waiting.", "is_error": False}, False),
    ({"role": "tool", "name": "wait", "content": "Error: Not waiting", "is_error": True}, True),
    ({"role": "tool", "name": "wait", "content": "OK, waiting."}, False),
    ({"role": "tool", "name": "read_file", "content": "x", "is_error": False}, True),
])
async def test_ended_mid_activation_counts_an_errored_wait(tmp_path, open_swarms, last, mid):
    sw = Swarm(config=make_config(tmp_path), llm_factory=Script().factory)
    open_swarms.append(sw)
    await sw.start(run_agents=False)
    try:
        a = sw.members()[0]["name"]
        write_history(sw, a, [{"role": "user", "content": "wake"},
                              {"role": "assistant", "content": "", "raw": None,
                               "tool_calls": [{"id": "c1", "name": last["name"], "arguments": {}}]},
                              {**last, "tool_call_id": "c1"}])
        assert AgentRuntime(sw, a)._ended_mid_activation() is mid
    finally:
        await sw.stop()


# ------------------------------------------------------------------------------------------------- call_tool
async def test_call_tool_flags_only_a_read_refusal_and_only_on_a_runtime(tmp_path, open_swarms):
    sw, script, a = await started(tmp_path, open_swarms)
    try:
        assert issubclass(ReadRefusal, ToolError)
        runtime = SimpleNamespace(read_refused=None)
        text, is_error = await call_tool(ToolContext(sw, a, runtime), "write_file",
                                         {"path": "notes.md", "content": "REFUSE"})
        assert is_error and text.startswith("Error: Not written") and runtime.read_refused == "write_file"
        # another ToolError sets no flag
        other = SimpleNamespace(read_refused=None)
        text, is_error = await call_tool(ToolContext(sw, a, other), "read_file", {"path": "missing.md"})
        assert is_error and other.read_refused is None
        # a runtime without the attribute (a simpler test double) and no runtime (the hub, /api/act) get none
        bare = SimpleNamespace()
        text, is_error = await call_tool(ToolContext(sw, a, bare), "write_file",
                                         {"path": "notes.md", "content": "REFUSE"})
        assert is_error and not hasattr(bare, "read_refused")
        text, is_error = await call_tool(ToolContext(sw, a), "write_file", {"path": "notes.md", "content": "REFUSE"})
        assert is_error and text.startswith("Error: Not written")
    finally:
        await sw.stop()


async def test_hold_after_refusal(tmp_path, open_swarms):
    sw, script, a = await started(tmp_path, open_swarms, language="ru")
    try:
        hold_after_refusal(ToolContext(sw, a), "wait_refused")   # no runtime: nothing held
        hold_after_refusal(ToolContext(sw, a, SimpleNamespace()), "wait_refused")
        hold_after_refusal(ToolContext(sw, a, SimpleNamespace(read_refused=None)), "wait_refused")
        with pytest.raises(ToolError) as exc:
            hold_after_refusal(ToolContext(sw, a, SimpleNamespace(read_refused="comment")), "vote_refused")
        assert not isinstance(exc.value, ReadRefusal)
        assert str(exc.value) == prompts.t("ru", "vote_refused", tool="comment")
        # without a runtime (an external member through the hub) task_complete is never held
        task = sw.board.create(USER, "Write the notes", goal="notes.md exists")
        sw.board.claim(a, task.id)
        text, is_error = await call_tool(ToolContext(sw, a), "task_complete", {"task_id": task.id, "result": "done"})
        assert not is_error, text
    finally:
        await sw.stop()
