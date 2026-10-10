"""End-to-end tests of the Swarm with scripted (deterministic) LLM agents."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import pytest

from dreamteam import prompts, roles
from dreamteam import swarm as swarm_module
from dreamteam.config import Config
from dreamteam.db import loads
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentStatus, Status, ToolError
from dreamteam.swarm import Swarm
from dreamteam.workspace import scratch_rel

Policy = Callable[[Swarm, str, str, list[dict], list[dict]], Any]


def call(tool_name: str, /, **arguments: Any) -> dict:
    return {"tool_calls": [{"name": tool_name, "arguments": arguments}]}


def make_config(tmp_path: Path, **overrides: Any) -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Test swarm", "language": "en", "start_agents": 2, "max_agents": 4, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 0.2, "max_steps_per_turn": 20},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0,
                               "cache_read_price": 0, "cache_write_price": 0}},
    }
    for section, values in overrides.items():
        if isinstance(values, list):  # [[roles]]
            raw[section] = values
        else:
            raw.setdefault(section, {}).update(values)
    return Config.from_dict(tmp_path / "project", raw)


def make_swarm(tmp_path: Path, policy: Policy, **overrides: Any) -> Swarm:
    cfg = make_config(tmp_path, **overrides)
    holder: dict[str, Swarm] = {}

    def factory(profile, agent: str) -> ScriptedLLM:
        return ScriptedLLM(lambda system, messages, tools: policy(holder["swarm"], agent, system, messages, tools),
                           profile)

    swarm = Swarm(config=cfg, llm_factory=factory)
    holder["swarm"] = swarm
    return swarm


async def run_until(predicate: Callable[[], bool], timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached in time")


@pytest.fixture(autouse=True)
def fast_scheduler(monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.1)


def last_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m["role"] in ("user", "tool"):
            return m.get("content") or ""
    return ""


# ------------------------------------------------------------------------------------------------
def worker_policy(created: dict[str, bool], written: set[int]) -> Policy:
    """A tiny but complete agent: reviews the others' work first, plans, claims, writes files, completes,
    declares the goal done."""

    def policy(swarm: Swarm, name: str, system: str, messages: list[dict], tools: list[dict]) -> Any:
        board = swarm.board
        for task in board.reviews_of(name):  # independent review: the agents review each other
            return call("task_review", task_id=task.id, approve=True, comment="checked")
        for task in board.open_tasks_of(name):
            if task.status != Status.IN_PROGRESS:
                continue
            if "planning" in task.tags:
                if not created.get("done"):
                    created["done"] = True
                    return {"tool_calls": [
                        {"name": "task_create", "arguments": {"title": "Write hello", "goal": "hello.txt exists", "tags": ["code"]}},
                        {"name": "task_create", "arguments": {"title": "Write world", "goal": "world.txt exists", "depends_on": [2]}},
                    ]}
                return call("task_complete", task_id=task.id, result="Planned tasks #2 and #3")
            fname = "hello.txt" if "hello" in task.title.lower() else "world.txt"
            if task.id not in written:
                written.add(task.id)
                return call("write_file", path=f"out/{fname}", content=f"{name} was here")
            return call("task_complete", task_id=task.id, result=f"Wrote out/{fname}")
        planning = next((t for t in board.list(include_closed=True) if "planning" in t.tags), None)
        if planning and planning.status == Status.TODO and not planning.assignee:
            return call("task_claim", task_id=planning.id)
        ready = board.ready_unassigned()
        if ready:
            return call("task_claim", task_id=ready[0].id)
        goal = swarm.goal()
        tasks = board.list(include_closed=True)
        if goal and goal["status"] == "active" and len(tasks) >= 3 and all(t.status == Status.DONE for t in tasks):
            return call("goal_complete", summary="Both files are in out/.")
        return call("wait", reason="nothing to do")

    return policy


async def test_swarm_reaches_goal_with_scripted_agents(tmp_path):
    swarm = make_swarm(tmp_path, worker_policy({}, set()))
    await swarm.start()
    try:
        names = [m["name"] for m in swarm.members()]
        assert len(names) == 2 and len(set(names)) == 2
        assert all(len(n.split()) == 2 for n in names)
        swarm.set_goal(USER, "Write two files", "hello and world")
        await run_until(lambda: (swarm.goal() or {}).get("status") == "done", timeout=40)
        tasks = swarm.board.list(include_closed=True)
        assert [t.status for t in tasks] == [Status.DONE] * 3
        assert tasks[0].tags == ["planning"] and tasks[0].goal
        # independent review: every task, the planning task included, was approved by the other agent
        approvals = {e["data"]["task"]["id"]: (e["actor"], e["data"]["task"]["assignee"])
                     for e in swarm.bus.recent(types=("task.updated",), limit=1000)
                     if e["data"]["changes"].get("status") == [Status.REVIEW, Status.DONE]}
        assert sorted(approvals) == [1, 2, 3]
        assert all(reviewer in names and reviewer != executor for reviewer, executor in approvals.values())
        assert (swarm.workspace.root / "out" / "hello.txt").read_text(encoding="utf-8").endswith("was here")
        assert (swarm.workspace.root / "out" / "world.txt").exists()
        notes = [r["kind"] for r in swarm.db.all("SELECT kind FROM notifications WHERE recipient = ?", (USER,))]
        assert "u_goal_done" in notes
        if swarm.config.workspace.git:
            log = subprocess.run(["git", "log", "--format=%an"], cwd=swarm.workspace.root, capture_output=True,
                                 text=True, encoding="utf-8")
            assert any(n in log.stdout for n in names)
        # every agent history is valid: tool results follow tool calls
        for name in names:
            rows = swarm.db.all("SELECT msg FROM history WHERE agent = ? ORDER BY seq", (name,))
            msgs = [json.loads(r["msg"]) for r in rows]
            pending: set[str] = set()
            for m in msgs:
                if m["role"] == "assistant":
                    assert not pending
                    pending = {c["id"] for c in m["tool_calls"]}
                elif m["role"] == "tool":
                    pending.discard(m["tool_call_id"])
    finally:
        await swarm.stop()
        swarm.close()


async def test_user_message_gets_plain_text_reply(tmp_path):
    def policy(swarm, name, system, messages, tools):
        return "Hello, human!"

    swarm = make_swarm(tmp_path, policy, swarm={"start_agents": 1})
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.mail.send(USER, [agent], "Hi, how are you?")
        await run_until(lambda: any(m["sender"] == agent for m in swarm.mail.conversation(USER, agent)))
        reply = [m for m in swarm.mail.conversation(USER, agent) if m["sender"] == agent][0]
        assert reply["text"] == "Hello, human!" and reply["recipients"] == [USER]
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.IDLE)
    finally:
        await swarm.stop()
        swarm.close()


async def test_trigger_wakes_agent(tmp_path):
    seen: list[str] = []

    def policy(swarm, name, system, messages, tools):
        seen.append(last_text(messages))
        return call("wait")

    swarm = make_swarm(tmp_path, policy, swarm={"start_agents": 1})
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        t1 = swarm.board.create(USER, "First", goal="g")
        swarm.triggers.create(agent, f"done({t1.id})", note="start the integration")
        await asyncio.sleep(0.3)
        assert not any("Trigger" in s for s in seen)
        swarm.board.update(USER, t1.id, status=Status.DONE)
        await run_until(lambda: any("Trigger #" in s and "start the integration" in s for s in seen))
    finally:
        await swarm.stop()
        swarm.close()


async def test_discussion_post_wakes_participants(tmp_path):
    seen: dict[str, list[str]] = {}

    def policy(swarm, name, system, messages, tools):
        seen.setdefault(name, []).append(last_text(messages))
        return call("wait")

    swarm = make_swarm(tmp_path, policy, swarm={"start_agents": 2})
    await swarm.start()
    try:
        a, b = [m["name"] for m in swarm.members()]
        task = swarm.board.create(USER, "Discuss the API", goal="Agree on endpoints", assignee=a)
        await run_until(lambda: any("assigned" in s for s in seen.get(a, [])))
        swarm.board.post(USER, task.id, f"@{b} please review the proposal of {a}")
        await run_until(lambda: any("mentioned you" in s for s in seen.get(b, [])))
        await run_until(lambda: any("discussion" in s for s in seen.get(a, [])[1:]))
    finally:
        await swarm.stop()
        swarm.close()


async def test_compaction_replaces_history(tmp_path):
    calls = {"n": 0}

    def policy(swarm, name, system, messages, tools):
        calls["n"] += 1
        if "will now be replaced by a summary" in last_text(messages):
            return "SUMMARY: did a lot of work"
        if calls["n"] < 4:
            return call("board")
        return call("wait")

    swarm = make_swarm(tmp_path, policy, swarm={"start_agents": 1}, agents={"context_tokens": 300})
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.notify(agent, "n_restart")
        await run_until(lambda: swarm.db.scalar("SELECT COUNT(*) FROM events WHERE type = 'agent.compacted'") >= 1)
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.IDLE)
        first = json.loads(swarm.db.one("SELECT msg FROM history WHERE agent = ? ORDER BY seq LIMIT 1", (agent,))["msg"])
        assert "SUMMARY: did a lot of work" in first["content"]
    finally:
        await swarm.stop()
        swarm.close()


async def test_budget_pauses_swarm(tmp_path):
    def policy(swarm, name, system, messages, tools):
        return {"tool_calls": [{"name": "board", "arguments": {}}],
                "usage": {"input_tokens": 1_000_000, "output_tokens": 0}}

    swarm = make_swarm(tmp_path, policy, swarm={"start_agents": 1, "max_cost_usd": 0.5},
                       models={"default": {"provider": "scripted", "model": "scripted", "input_price": 1.0,
                                           "output_price": 1.0}})
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.notify(agent, "n_restart")
        await run_until(lambda: swarm.paused)
        assert swarm.pause_reason == "budget"
        assert swarm.spent_usd() >= 0.5
        kinds = [r["kind"] for r in swarm.db.all("SELECT kind FROM notifications WHERE recipient = ?", (USER,))]
        assert "u_budget" in kinds
        swarm.set_limits(USER, max_cost_usd=100)
        assert not swarm.paused
    finally:
        await swarm.stop()
        swarm.close()


async def test_hire_and_retire(tmp_path):
    def policy(swarm, name, system, messages, tools):
        member = swarm.member(name)
        if member["hired_by"] == USER and "hired" not in str(swarm.db.get("test_flag")):
            swarm.db.set("test_flag", "hired")
            return call("hire_agent", role="QA engineer", instructions="Test everything", name="Grace Hopper")
        return call("wait")

    swarm = make_swarm(tmp_path, policy, swarm={"start_agents": 1})
    await swarm.start()
    try:
        boss = swarm.members()[0]["name"]
        swarm.notify(boss, "n_restart")
        await run_until(lambda: swarm.member("Grace Hopper") is not None)
        assert swarm.member("Grace Hopper")["hired_by"] == boss
        await run_until(lambda: "Grace Hopper" in swarm.runtimes)
        task = swarm.board.create(USER, "Test it", goal="tested", assignee="Grace Hopper")
        released = await swarm.retire(boss, "Grace Hopper", "no longer needed")
        assert released == [task.id]
        assert swarm.board.get(task.id).assignee is None
        assert "Grace Hopper" not in swarm.runtimes
        assert swarm.member("Grace Hopper")["status"] == AgentStatus.RETIRED
    finally:
        await swarm.stop()
        swarm.close()


# ---------------------------------------------------------------- role catalog, goal split, scratch folder
async def test_starting_team_comes_from_the_role_catalog(tmp_path):
    """starting_roles names catalog roles in either language (docs/SPEC.md §17.3): member i gets the canonical
    name in its language and the role's description right after "Your role: …"; the others are generalists.
    Every prompt names the member's own scratch folder."""
    swarm = make_swarm(tmp_path, lambda *a: call("wait"),
                       swarm={"starting_roles": ["developer", "Тестировщик"], "start_agents": 3})
    await swarm.start(run_agents=False)
    try:
        members = swarm.members()
        assert [m["role"] for m in members] == ["Developer", "Tester", "Generalist"]
        for m, rid in zip(members, ("developer", "tester", "generalist")):
            name, description = roles.ROLE_PRESETS["en"][rid]
            assert f"Your role: {name}.\n{description}\n\n## The swarm\n" in m["system_prompt"]
            assert f"`{scratch_rel(m['name'])}`" in m["system_prompt"]
    finally:
        await swarm.stop()
        swarm.close()


async def test_role_model_instructions_and_frozen_prompts(tmp_path):
    """Model precedence: the explicit profile > the role's > the default. The catalog's instructions come before
    the hirer's. A catalog edit reaches new members only; existing prompts stay byte-identical."""
    fast = {"provider": "scripted", "model": "fast-model"}
    swarm = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"starting_roles": ["tester"], "start_agents": 1},
                       models={"fast": fast},
                       roles=[{"name": "tester", "model": "fast", "instructions": "Use pytest."}])
    await swarm.start(run_agents=False)
    try:
        first = swarm.members()[0]
        assert (first["role"], first["model"], first["instructions"]) == ("Tester", "fast", "")
        assert prompts.t("en", "instructions", text="Use pytest.") in first["system_prompt"]
        name = await swarm.hire(USER, "тестировщик", "Focus on the CLI.", model="default")
        member = swarm.member(name)
        # the agents row keeps the hirer's own instructions; the catalog's live in the frozen prompt
        assert (member["role"], member["model"], member["instructions"]) == ("Tester", "default", "Focus on the CLI.")
        assert prompts.t("en", "instructions", text="Use pytest.\n\nFocus on the CLI.") in member["system_prompt"]
        before = {m["name"]: m["system_prompt"] for m in swarm.members()}
        cfg = make_config(tmp_path, models={"fast": fast},
                          roles=[{"name": "tester", "description": "You test the CLI only."}])
        swarm.reconfigure(dataclasses.replace(cfg, project_dir=swarm.project_dir))
        newer = await swarm.hire(USER, "Tester")
        assert "Your role: Tester.\nYou test the CLI only.\n\n## The swarm" in swarm.member(newer)["system_prompt"]
        assert swarm.member(newer)["model"] == "default"            # the role has no profile of its own any more
        assert {m["name"]: m["system_prompt"] for m in swarm.members() if m["name"] in before} == before
        # a role of one's own: the hirer's instructions are all the member gets
        own = await swarm.hire(USER, "Data  engineer", "Build the ETL.")
        assert swarm.member(own)["role"] == "Data engineer"
        assert "Your role: Data engineer.\n" + prompts.t("en", "instructions", text="Build the ETL.") in \
            swarm.member(own)["system_prompt"]
    finally:
        await swarm.stop()
        swarm.close()


async def test_kickoff_names_the_team_and_hiring_notices_carry_the_canonical_role(tmp_path):
    swarm = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"starting_roles": ["architect"], "start_agents": 2})
    await swarm.start(run_agents=False)
    try:
        a, b = [m["name"] for m in swarm.members()]
        swarm.set_goal(USER, "Ship it")
        team = f"{a} — Architect; {b} — Generalist"
        for name in (a, b):
            row = swarm.db.one("SELECT text, data FROM notifications WHERE recipient = ? AND kind = 'n_kickoff'",
                               (name,))
            assert loads(row["data"])["team"] == team and f"\nTeam: {team}.\n" in row["text"]
        hired = await swarm.hire(a, "reviewer", "")
        assert swarm.member(hired)["role"] == "Reviewer"
        joined = swarm.db.one("SELECT data FROM notifications WHERE recipient = ? AND kind = 'u_member_joined'",
                              (USER,))
        assert loads(joined["data"])["role"] == "Reviewer"
        welcome = swarm.db.one("SELECT text FROM notifications WHERE recipient = ? AND kind = 'n_hired'", (hired,))
        assert 'hired you as "Reviewer"' in welcome["text"]
        assert f"`{scratch_rel(hired)}`" in swarm.member(hired)["system_prompt"]
    finally:
        await swarm.stop()
        swarm.close()


async def test_set_goal_splits_a_multi_line_goal(tmp_path):
    """Every goal goes through Swarm.set_goal: the first line is the title, the rest opens the description, so
    the goal line of every wake-up is one line; the planning task still holds the whole text."""
    swarm = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 1})
    await swarm.start(run_agents=False)
    try:
        name = swarm.members()[0]["name"]
        goal = swarm.set_goal(USER, "Snake\nPython\n\n- tests")
        assert (goal["title"], goal["description"]) == ("Snake", "Python\n\n- tests")
        assert swarm.board.get(1).description.endswith("\n\nSnake\nPython\n\n- tests")
        assert "\nGlobal goal: Snake [active]\n" in swarm.render_wake(name)
        assert swarm.set_goal(USER, "", "A\nB")["title"] == "A"
        with pytest.raises(ToolError, match="title is empty"):
            swarm.set_goal(USER, " \n ")
    finally:
        await swarm.stop()
        swarm.close()


async def test_restart_restores_members_and_repairs_history(tmp_path):
    def policy(swarm, name, system, messages, tools):
        return call("wait")

    swarm = make_swarm(tmp_path, policy, swarm={"start_agents": 2})
    await swarm.start()
    names = [m["name"] for m in swarm.members()]
    agent = names[0]
    # simulate a crash in the middle of a tool round
    swarm.db.execute("INSERT INTO history(agent, seq, msg) VALUES (?, ?, ?)",
                     (agent, 0, json.dumps({"role": "user", "content": "wake"})))
    swarm.db.execute("INSERT INTO history(agent, seq, msg) VALUES (?, ?, ?)",
                     (agent, 1, json.dumps({"role": "assistant", "content": "", "raw": None,
                                            "tool_calls": [{"id": "c1", "name": "board", "arguments": {}}]})))
    await swarm.stop()
    swarm.close()

    swarm2 = make_swarm(tmp_path, policy, swarm={"start_agents": 2})
    await swarm2.start()
    try:
        assert [m["name"] for m in swarm2.members()] == names
        await run_until(lambda: swarm2.db.scalar("SELECT COUNT(*) FROM history WHERE agent = ?", (agent,)) >= 3)
        msgs = [json.loads(r["msg"]) for r in swarm2.db.all("SELECT msg FROM history WHERE agent = ? ORDER BY seq", (agent,))]
        assert msgs[2]["role"] == "tool" and msgs[2]["tool_call_id"] == "c1" and msgs[2]["is_error"]
    finally:
        await swarm2.stop()
        swarm2.close()


async def test_llm_error_notifies_user(tmp_path):
    from dreamteam.llm.base import LLMError

    def policy(swarm, name, system, messages, tools):
        raise LLMError("invalid x-api-key", retryable=False, kind="auth")

    swarm = make_swarm(tmp_path, policy, swarm={"start_agents": 1})
    await swarm.start()
    try:
        agent = swarm.members()[0]["name"]
        swarm.notify(agent, "n_restart")
        await run_until(lambda: swarm.member(agent)["status"] == AgentStatus.ERROR)
        texts = [r["text"] for r in swarm.db.all("SELECT text FROM notifications WHERE recipient = ?", (USER,))]
        assert any("invalid x-api-key" in t for t in texts)
    finally:
        await swarm.stop()
        swarm.close()


# ---------------------------------------------------------------- hub MCP (in-process)
async def test_hub_mcp_user_and_member(tmp_path):
    from mcp import Client

    from dreamteam.hub_mcp import build_hub_server

    swarm = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 1})
    await swarm.start()
    try:
        async with Client(build_hub_server(swarm)) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            for name in ("board", "set_goal", "join_swarm", "wait_for_events", "task_claim", "send_message"):
                assert name in tools
            assert "as_agent" in tools["board"].input_schema["properties"]
            assert "shell" in tools and "update_notes" not in tools and "wait" not in tools

            res = await client.call_tool("set_goal", {"title": "Build a CLI", "description": "small"})
            assert not res.is_error and "Goal set" in res.content[0].text
            res = await client.call_tool("task_claim", {"task_id": 1})
            assert res.is_error and "not available" in res.content[0].text

            res = await client.call_tool("join_swarm", {"role": "Reviewer", "name": "Claude Code"})
            assert not res.is_error and "Claude Code" in res.content[0].text
            assert swarm.member("Claude Code")["kind"] == "external"
            res = await client.call_tool("task_claim", {"task_id": 1, "as_agent": "Claude Code"})
            assert not res.is_error, res.content[0].text
            assert swarm.board.get(1).assignee == "Claude Code"

            internal = swarm.members()[0]["name"]
            res = await client.call_tool("board", {"as_agent": internal})
            assert res.is_error and "join_swarm" in res.content[0].text

            swarm.mail.send(internal, ["Claude Code"], "ping")
            res = await client.call_tool("wait_for_events", {"timeout": 5, "as_agent": "Claude Code"})
            assert "ping" in res.content[0].text
            res = await client.call_tool("wait_for_events", {"timeout": 0, "as_agent": "Claude Code"})
            assert "No new events" in res.content[0].text
    finally:
        await swarm.stop()
        swarm.close()


# ---------------------------------------------------------------- web server
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


async def test_web_api_and_websocket(tmp_path):
    import websockets

    from dreamteam.web import serve

    swarm = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 1})
    await swarm.start()
    port = free_port()
    server = asyncio.create_task(serve(swarm, "127.0.0.1", port))
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                status, _ = await asyncio.to_thread(http, "GET", base + "/api/state")
                if status == 200:
                    break
            except OSError:
                pass
            await asyncio.sleep(0.05)
        status, page = await asyncio.to_thread(http, "GET", base + "/")
        assert status == 200 and "DreamTeam" in page
        status, js = await asyncio.to_thread(http, "GET", base + "/static/app.js")
        assert status == 200

        async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
            jhead = {"Content-Type": "application/json"}
            status, res = await asyncio.to_thread(http, "POST", base + "/api/act/set_goal",
                                                  {"title": "Ship it"}, jhead)
            assert status == 200 and res["ok"], res
            got = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert got["type"] in ("goal.set", "task.created", "notification")

        status, state = await asyncio.to_thread(http, "GET", base + "/api/state")
        assert state["swarm"]["goal"]["title"] == "Ship it"
        assert state["tasks"][0]["tags"] == ["planning"]
        assert state["swarm"]["mcp_url"].endswith("/mcp")

        status, detail = await asyncio.to_thread(http, "GET", base + "/api/tasks/1")
        assert status == 200 and detail["task"]["id"] == 1 and isinstance(detail["discussion"], list)
        status, res = await asyncio.to_thread(http, "POST", base + "/api/act/task_comment",
                                              {"task_id": 1, "text": "Looks good"}, {"Content-Type": "application/json"})
        assert res["ok"]
        status, detail = await asyncio.to_thread(http, "GET", base + "/api/tasks/1")
        assert detail["discussion"][0]["text"] == "Looks good"

        # guards
        status, _ = await asyncio.to_thread(http, "POST", base + "/api/act/set_goal", {"title": "x"},
                                            {"Content-Type": "text/plain"})
        assert status == 415
        status, _ = await asyncio.to_thread(http, "GET", base + "/api/state", None, {"Host": "evil.example.com"})
        assert status == 403
        status, res = await asyncio.to_thread(http, "POST", base + "/api/act/shell", {"command": "dir"},
                                              {"Content-Type": "application/json"})
        assert res["ok"] is False

        # the hub MCP endpoint over HTTP
        from mcp import Client

        async with Client(base + "/mcp") as client:
            res = await client.call_tool("swarm_status", {})
            assert "Ship it" in res.content[0].text
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
        await swarm.stop()
        swarm.close()


async def test_member_whose_profile_was_removed_uses_the_default(tmp_path, caplog):
    """agents.model keeps a profile name (hire_agent(model="fast")); after the profile was renamed or deleted in
    the settings, Config.model() raised KeyError at every LLM call and the agent crashed at every activation."""
    seen: list[str] = []

    def policy(swarm, name, system, messages, tools):
        seen.append(swarm.runtimes[name].llm.profile.name)
        return call("wait")

    swarm = make_swarm(tmp_path, policy, swarm={"start_agents": 0},
                       models={"fast": {"provider": "scripted", "model": "fast-model"}})
    await swarm.start()
    try:
        name = await swarm.hire(USER, "Tester", "Test things.", model="fast")
        await run_until(lambda: seen)
        assert seen == ["fast"]
        cfg = make_config(tmp_path)  # the settings without "fast"
        swarm.reconfigure(dataclasses.replace(cfg, project_dir=swarm.project_dir))
        swarm.notify(name, "n_restart")
        await run_until(lambda: len(seen) == 2 and swarm.member(name)["status"] == AgentStatus.IDLE)
        assert seen[1] == "default" and swarm.member(name)["status"] != AgentStatus.ERROR
        assert "no longer exists" in caplog.text
    finally:
        await swarm.stop()
        swarm.close()


async def test_wait_for_events_ends_when_the_swarm_stops(tmp_path):
    """A wait_for_events long poll (REST /api/act, a session's MCP) running when the swarm stopped waited out its
    whole timeout and then failed with 'Cannot operate on a closed database'."""
    from dreamteam.tools import ToolContext, call_tool

    swarm = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 0})
    await swarm.start()
    poll = asyncio.create_task(call_tool(ToolContext(swarm, USER), "wait_for_events", {"timeout": 120}))
    await asyncio.sleep(0.2)
    assert not poll.done()
    started = time.monotonic()
    await swarm.stop()
    text, is_error = await asyncio.wait_for(poll, 5)
    assert is_error and "stopped" in text and "database" not in text
    assert time.monotonic() - started < 5
    await swarm.start()  # started again, it waits normally
    try:
        text, is_error = await call_tool(ToolContext(swarm, USER), "wait_for_events", {"timeout": 0})
        assert not is_error and "No new events" in text
    finally:
        await swarm.stop()
        swarm.close()
