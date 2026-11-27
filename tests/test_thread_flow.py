"""Stage 2 of the thread mode end to end (docs/SPEC.md §22.3, §22.6 wake-up item 5, §22.7): a thread session of three
LLM agents with real runtimes and the scheduler running (sped up), where a post, a comment, a vote, file changes, the
user's posts and comments, the digest batching (T1) and the quiet check (T2) reach the members and USER as the SPEC
says; a pause stops T1 and T2 and a resume restarts them; a board session's scheduler and notifications stay as
before. The clocks are aged in the database instead of slept; ScriptedLLM only, no network."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from dreamteam import prompts
from dreamteam import swarm as swarm_module
from dreamteam.config import Config
from dreamteam.forum import SETTLE_KEY
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentKind, AgentStatus
from dreamteam.swarm import DIGEST_KEY, QUIET_KEY, USER_PAUSE, Swarm
from dreamteam.tools import ToolContext, call_tool

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

GOAL = "Build the parser"
DIGEST = 60.0    # [agents] digest_seconds
SETTLE = 30.0    # [agents] file_settle_seconds
WAIT = 3600.0    # [agents] idle_nudge_seconds: T2 never fires unless the clocks are aged by it
WAIT_CALL = {"name": "wait", "arguments": {}}
DIGEST_HEAD = "From what you follow (oldest first, votes last):"


def make_config(tmp_path: Path, mode: str = "thread") -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Thread flow", "language": "en", "start_agents": 3, "max_agents": 6, "max_cost_usd": 0,
                  "mode": mode, "clarify": False},
        "agents": {"stagger_seconds": 0, "max_steps_per_turn": 12, "digest_seconds": DIGEST,
                   "file_settle_seconds": SETTLE, "file_max_wait_seconds": 300, "idle_nudge_seconds": WAIT},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    return Config.from_dict(tmp_path / "project", raw)


class Team:
    """The agents' scripts: each activation records its wake-up text and answers with the next batch of tool calls
    queued for the agent (none: just `wait`), always ending the turn with `wait`. A forum write refused for news the
    agent had not read (docs/SPEC.md §22.11) is sent again in the next step: the refusal showed the news, and the
    scripted agent still wants its write."""

    def __init__(self) -> None:
        self.todo: dict[str, list[list[dict]]] = {}
        self.wakes: dict[str, list[str]] = {}

    def factory(self, profile: Any, agent: str) -> ScriptedLLM:
        def handler(system: str, messages: list[dict], tools: list[dict]) -> dict:
            last = messages[-1] if messages else {}
            if last.get("role") == "user":
                self.wakes.setdefault(agent, []).append(str(last.get("content") or ""))
            retry = _refused_writes(messages)
            if retry:
                return {"tool_calls": [*retry, WAIT_CALL]}
            queue = self.todo.get(agent) or []
            batch = queue.pop(0) if queue else []
            return {"tool_calls": [*batch, WAIT_CALL]}

        return ScriptedLLM(handler, profile)

    def count(self, name: str) -> int:
        return len(self.wakes.get(name, []))

    def last(self, name: str) -> str:
        return self.wakes[name][-1]


def call(name: str, **arguments: Any) -> dict:
    return {"name": name, "arguments": arguments}


REFUSALS = ("Error: Not sent:", "Error: Not posted:", "Error: Not changed:")   # the forum's (§22.11)


def _refused_writes(messages: list[dict]) -> list[dict]:
    """The calls of the last response whose results are read-before-write refusals of the forum."""
    results = []
    for message in reversed(messages):
        if message.get("role") != "tool":
            break
        results.append(message)
    refused = {m.get("tool_call_id") for m in results if str(m.get("content") or "").startswith(REFUSALS)}
    if not refused:
        return []
    sent = messages[-len(results) - 1] if len(messages) > len(results) else {}
    return [call(c["name"], **c["arguments"]) for c in sent.get("tool_calls") or () if c.get("id") in refused]


@pytest.fixture(autouse=True)
def fast_scheduler(monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)


@pytest.fixture
def open_swarms():
    made: list[Swarm] = []
    yield made
    for sw in made:
        try:
            sw.close()
        except Exception:  # noqa: BLE001 - closed already
            pass


async def run_until(predicate: Callable[[], bool], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


def count_passes(sw: Swarm) -> list[int]:
    """The scheduler's passes, counted at their first step (it runs in both modes, also while paused)."""
    passes = [0]
    original = sw.workspace.expire_locks

    def counted(*args: Any, **kw: Any) -> Any:
        passes[0] += 1
        return original(*args, **kw)

    sw.workspace.expire_locks = counted
    return passes


def record_calls(sw: Swarm, *names: str) -> dict[str, int]:
    """Counts the scheduler's calls of these Swarm methods; each still runs."""
    calls = {name: 0 for name in names}
    for name in names:
        original = getattr(sw, name)

        def counted(*args: Any, _name: str = name, _original: Any = original, **kw: Any) -> Any:
            calls[_name] += 1
            return _original(*args, **kw)

        setattr(sw, name, counted)
    return calls


def agents_of(sw: Swarm) -> list[str]:
    return [m["name"] for m in sw.members() if m["kind"] == AgentKind.LLM]


def calm(sw: Swarm) -> bool:
    """Every agent asleep with nothing pending."""
    return all(sw.runtimes[name].state == AgentStatus.IDLE and not sw.has_pending(name) and not sw.jobs.busy(name)
               for name in agents_of(sw))


async def passes_go_by(sw: Swarm, passes: list[int], n: int = 3) -> None:
    start = passes[0]
    await run_until(lambda: passes[0] >= start + n)


def rows(sw: Swarm, recipient: str | None = None, kind: str | None = None, *, undelivered: bool = False) -> list[dict]:
    out = []
    for r in sw.db.all("SELECT id, recipient, kind, text, data, delivered_at FROM notifications ORDER BY id"):
        if recipient is not None and r["recipient"] != recipient:
            continue
        if kind is not None and r["kind"] != kind:
            continue
        if undelivered and r["delivered_at"] is not None:
            continue
        out.append({"id": r["id"], "recipient": r["recipient"], "kind": r["kind"], "text": r["text"],
                    "data": json.loads(r["data"] or "{}"), "delivered": r["delivered_at"] is not None})
    return out


def age(sw: Swarm, seconds: float) -> None:
    """Every clock T1 and T2 read `seconds` older: the members' activity, the notifications and their settle times,
    the goal's updated_at (T2's `sent`), the digest floor and the quiet clock. Posts, comments and votes keep their
    times: they make the thread key."""
    sw.db.execute("UPDATE agents SET last_active = last_active - ? WHERE last_active IS NOT NULL", (seconds,))
    sw.db.execute("UPDATE notifications SET ts = ts - ?", (seconds,))
    sw.db.execute(f"UPDATE notifications SET data = json_set(data, '$.{SETTLE_KEY}.first', "
                  f"json_extract(data, '$.{SETTLE_KEY}.first') - ?, '$.{SETTLE_KEY}.last', "
                  f"json_extract(data, '$.{SETTLE_KEY}.last') - ?) WHERE json_extract(data, '$.{SETTLE_KEY}') "
                  "IS NOT NULL", (seconds, seconds))
    goal = sw.goal()
    if goal:
        sw.db.set("goal", {**goal, "updated_at": float(goal["updated_at"]) - seconds})
    sw._digest_floor -= seconds
    if sw._thread_quiet is not None:
        key, since = sw._thread_quiet
        sw._thread_quiet = (key, since - seconds)


async def act(sw: Swarm, actor: str, name: str, args: dict) -> str:
    """A tool call of an agent outside its activations, through its own runtime's tool set."""
    text, is_error = await call_tool(ToolContext(sw, actor, sw.runtimes[actor]), name, args)
    assert not is_error, text
    return text


def tool_errors(sw: Swarm) -> list[str]:
    return [e["data"].get("text", "") for e in sw.bus.recent(types=("agent.activity",), limit=10000)
            if e["data"].get("kind") == "tool_result" and e["data"].get("is_error")]


def tool_results(sw: Swarm) -> list[tuple[str, str, bool]]:
    """Every tool result of the run in order: (agent, text, is_error)."""
    return [(e["data"].get("agent", ""), e["data"].get("text", ""), bool(e["data"].get("is_error")))
            for e in sw.bus.recent(types=("agent.activity",), limit=10000) if e["data"].get("kind") == "tool_result"]


def agent_tool_errors(sw: Swarm) -> list[tuple[str, str]]:
    return [(agent, text) for agent, text, is_error in tool_results(sw) if is_error]


async def kicked_off(tmp_path: Path, open_swarms: list, todo: Callable[[list[str]], dict] | None = None,
                     mode: str = "thread") -> tuple[Swarm, Team, list[int], list[str]]:
    """A started session with three agents, a goal and its kickoff read by everyone (each agent's first activation
    runs `todo`'s batch for it)."""
    team = Team()
    sw = Swarm(config=make_config(tmp_path, mode), llm_factory=team.factory)
    open_swarms.append(sw)
    passes = count_passes(sw)
    await sw.start()
    names = agents_of(sw)
    assert len(names) == 3
    for name, batch in (todo(names) if todo else {}).items():
        team.todo[name] = [batch]
    sw.set_goal(USER, GOAL, "A fast one, with tests.")
    await run_until(lambda: all(team.count(n) >= 1 for n in names) and calm(sw))
    return sw, team, passes, names


# ===================================================================== the whole flow
async def test_a_thread_session_delivers_batches_and_watches_its_quiet(tmp_path, open_swarms):
    def subscriptions(names: list[str]) -> dict:
        _, b, c = names
        return {b: [call("subscribe", what="feed", mode="wake")],
                c: [call("subscribe", what="file", target="src", mode="wake"),
                    call("subscribe", what="post", target="2", mode="wake")]}

    sw, team, passes, (a, b, c) = await kicked_off(tmp_path, open_swarms, subscriptions)
    try:
        assert all("There is no task board and no leader" in team.wakes[n][0] for n in (a, b, c))
        modes = {(s["kind"], s["target"]): s["mode"] for s in sw.forum.subscriptions(b)}
        assert modes[("feed", "")] == "wake"
        modes = {(s["kind"], s["target"]): s["mode"] for s in sw.forum.subscriptions(c)}
        assert modes[("file", "src")] == "wake" and modes[("post", "2")] == "wake" and modes[("feed", "")] == "digest"
        assert team.count(a) == team.count(b) == team.count(c) == 1

        # ---- an agent's post wakes the feed's `wake` subscriber; the others get a digest item
        team.todo[b] = [[call("comment", post_id=2, text=f"Plan: {a} writes the parser, I review it."),
                         call("vote", target="#3", value="like")]]
        await act(sw, a, "post_create", {"title": "Parser plan", "text": "I write src/parser.py and src/lexer.py."})
        post = sw.db.scalar("SELECT MAX(id) FROM posts")
        assert post == 3
        # B's comment on #2 (in the activation the post woke it for) wakes C through its `post 2` row in wake
        await run_until(lambda: team.count(b) == 2 and team.count(c) == 2 and calm(sw))
        assert f'{a} posted #3 "Parser plan": I write src/parser.py' in team.last(b)
        wake_c = team.last(c)
        assert f'{b} commented on #2 "' in wake_c
        # ... with C's digest item for the post in its digest group, after the news
        assert DIGEST_HEAD in wake_c and wake_c.index(DIGEST_HEAD) < wake_c.index(f"{a} posted #3")
        assert wake_c.index(f"{b} commented on #2") < wake_c.index(DIGEST_HEAD)
        assert not rows(sw, c, undelivered=True)
        # A follows #2 and its reactions in digest: a comment and a vote wait, quiet, and A sleeps on
        waiting = rows(sw, a, undelivered=True)
        assert [r["kind"] for r in waiting] == ["f_comment", "f_vote"]
        assert all(r["data"].get(QUIET_KEY) and r["data"].get(DIGEST_KEY) for r in waiting)
        await passes_go_by(sw, passes)
        assert team.count(a) == 1 and not sw.team_asleep() and sw._thread_quiet is None
        assert sw.digest_deadline(a) is not None

        # ---- T1: digest_seconds after the comment, A is woken once with both, the vote last
        team.todo[a] = [[call("write_file", path="src/parser.py", content="def parse(): ...\n"),
                         call("write_file", path="src/lexer.py", content="def lex(): ...\n")]]
        age(sw, DIGEST)
        await run_until(lambda: team.count(a) == 2 and calm(sw))
        wake_a = team.last(a)
        comment_line, vote_line = f"{b} commented on #2", f'{b} rated your #3 "Parser plan": like.'
        assert DIGEST_HEAD in wake_a and wake_a.index(DIGEST_HEAD) < wake_a.index(comment_line)
        assert wake_a.index(comment_line) < wake_a.index(vote_line)
        assert not rows(sw, a, undelivered=True) and team.count(b) == 2

        # ---- the file subscriber: A's two writes in one turn make one f_file, held until the changes settle
        held = rows(sw, c, "f_file", undelivered=True)
        assert len(held) == 1 and not rows(sw, b, "f_file")
        item = held[0]
        assert item["data"]["paths"] == ["src/parser.py", "src/lexer.py"] and item["data"]["editors"] == [a]
        assert item["text"] == f"Files changed ({a}): src/parser.py, src/lexer.py."
        assert item["data"].get(QUIET_KEY) and SETTLE_KEY in item["data"] and DIGEST_KEY not in item["data"]
        await passes_go_by(sw, passes)
        assert team.count(c) == 2 and not sw.team_asleep()  # a wake is set
        age(sw, SETTLE)
        await run_until(lambda: team.count(c) == 3 and calm(sw))
        assert f"Files changed ({a}): src/parser.py, src/lexer.py." in team.last(c)
        assert len(rows(sw, c, "f_file")) == 1 and not rows(sw, c, undelivered=True)

        # ---- the user's post wakes every member, whatever it follows; USER hears of comments on its post
        team.todo[a] = [[call("comment", post_id=4, text="Will do.")]]
        team.todo[b] = [[call("comment", post_id=4, text="Agreed, @user.")]]
        counts = {n: team.count(n) for n in (a, b, c)}
        sw.forum.create(USER, "Style", f"@{a} please add type hints.")
        await run_until(lambda: all(team.count(n) == counts[n] + 1 for n in (a, b, c)) and calm(sw))
        assert f'user mentioned you on #4 "Style": @{a} please add type hints.' in team.last(a)
        assert 'user posted #4 "Style"' in team.last(b) and 'user posted #4 "Style"' in team.last(c)
        told = sorted((r["kind"], r["data"]["by"]) for r in rows(sw, USER) if r["kind"].startswith("f_"))
        assert told == sorted([("f_comment", a), ("f_mention", b)])

        # ---- the user's comment wakes every member too; a reply to it reaches USER as f_reply
        question = sw.forum.comment(USER, 3, "Tests too?")
        team.todo[a] = [[call("comment", post_id=3, text="Yes, with tests.", reply_to=f"c{question['id']}")]]
        counts = {n: team.count(n) for n in (a, b, c)}
        await run_until(lambda: all(team.count(n) == counts[n] + 1 for n in (a, b, c)) and calm(sw))
        assert f'user commented on #3 "Parser plan" (c{question["id"]}): Tests too?' in team.last(a)
        assert all(f'user commented on #3 "Parser plan"' in team.last(n) for n in (b, c))
        user_items = [r for r in rows(sw, USER) if r["kind"].startswith("f_")]
        assert sorted(r["kind"] for r in user_items) == ["f_comment", "f_mention", "f_reply"]
        reply = next(r for r in user_items if r["kind"] == "f_reply")
        assert reply["data"]["by"] == a and reply["data"]["reply_to"] == f"c{question['id']}"
        assert reply["text"].startswith(f"{a} replied to your comment c{question['id']} on #3")
        # never votes, edits, new posts or file changes for USER
        assert not {r["kind"] for r in rows(sw, USER)} & {"f_post", "f_vote", "f_edit", "f_file"}

        # ---- a vote alone never starts a batch
        await act(sw, c, "vote", {"target": "#3", "value": "like"})
        vote = rows(sw, a, "f_vote", undelivered=True)
        assert len(vote) == 1 and vote[0]["data"].get(DIGEST_KEY)
        assert sw.digest_deadline(a) is None
        counts = {n: team.count(n) for n in (a, b, c)}
        age(sw, 2 * DIGEST)
        await passes_go_by(sw, passes)
        assert {n: team.count(n) for n in (a, b, c)} == counts
        assert rows(sw, a, "f_vote", undelivered=True)
        for name in (b, c):
            assert not rows(sw, name, undelivered=True)

        # ---- T2: the quiet team; one idle member nudged, then (with the second nudge) the user told
        await run_until(lambda: sw._thread_quiet is not None and sw.team_asleep())
        order = sorted((a, b, c), key=lambda n: float(sw.member(n)["last_active"] or 0))
        assert not rows(sw, kind="n_thread_quiet")
        age(sw, WAIT)
        await run_until(lambda: len(rows(sw, kind="n_thread_quiet")) == 1 and team.count(order[0]) == counts[order[0]]
                        + 1 and calm(sw))
        assert rows(sw, kind="n_thread_quiet")[0]["recipient"] == order[0]
        assert "The thread is quiet, and nothing is set to wake the team" in team.last(order[0])
        assert not rows(sw, USER, "u_thread_quiet")
        if order[0] == a:  # the vote rode along with A's next wake-up
            assert not rows(sw, a, "f_vote", undelivered=True)
        # the nudged member's activation restarted the clock; the second nudge comes after twice as long
        active = float(sw.member(order[0])["last_active"])
        await run_until(lambda: sw._thread_quiet is not None and sw._thread_quiet[1] >= active)
        age(sw, WAIT)
        await passes_go_by(sw, passes)
        assert len(rows(sw, kind="n_thread_quiet")) == 1
        age(sw, WAIT)
        await run_until(lambda: len(rows(sw, kind="n_thread_quiet")) == 2 and calm(sw))
        assert rows(sw, kind="n_thread_quiet")[1]["recipient"] == order[1]  # round the team
        told = rows(sw, USER, "u_thread_quiet")
        assert len(told) == 1 and set(told[0]["data"]) >= {"vote", "elapsed"}
        assert told[0]["text"].startswith("Nobody in the team has worked for ")
        assert not told[0]["data"].get(QUIET_KEY)  # waking
        # at most TEAM_ASLEEP_MAX nudges per key, USER told once per key
        age(sw, 10 * WAIT)
        await passes_go_by(sw, passes, 5)
        assert len(rows(sw, kind="n_thread_quiet")) == swarm_module.TEAM_ASLEEP_MAX
        assert len(rows(sw, USER, "u_thread_quiet")) == 1
        # the only errors: the second of the two comments on #4 refused for the first one, which its author had not
        # read (both answered the user's post at once, docs/SPEC.md §22.11), and the `wait` of that same response
        # held after it; the comment sent again went through (`told` above has both)
        held = "Error: " + prompts.t("en", "wait_refused", tool="comment")
        errors = agent_tool_errors(sw)
        assert len(errors) == 2, errors
        (late, refusal), (waiter, wait_error) = errors
        assert late in (a, b) and waiter == late
        assert refusal.startswith('Error: Not sent: #4 "Style" has what you have not read') and wait_error == held
        results = [r for r in tool_results(sw) if r[0] == late]
        at = results.index((late, refusal, True))
        assert results[at + 1] == (late, held, True)  # consecutive: the held `wait` of the refused response
        assert sw.goal()["status"] == "active"
    finally:
        await sw.stop()


# ===================================================================== pause, resume
async def test_a_pause_stops_the_batches_and_the_quiet_check_and_a_resume_restarts_them(tmp_path, open_swarms):
    sw, team, passes, (a, b, c) = await kicked_off(tmp_path, open_swarms)
    calls = record_calls(sw, "_deliver_digests", "_check_quiet")
    try:
        await act(sw, a, "post_create", {"title": "Parser plan", "text": "I write src/parser.py."})
        for name in (b, c):  # the automatic feed row is in digest
            assert [r["kind"] for r in rows(sw, name, undelivered=True)] == ["f_post"]
        await passes_go_by(sw, passes)
        assert team.count(b) == team.count(c) == 1

        assert sw.pause(USER, USER_PAUSE)
        await passes_go_by(sw, passes, 1)
        seen = dict(calls)
        age(sw, 10 * DIGEST + 10 * WAIT)
        await passes_go_by(sw, passes, 5)
        assert calls == seen  # neither T1 nor T2 while paused
        assert team.count(b) == team.count(c) == 1 and not rows(sw, kind="n_thread_quiet")

        sw.resume(USER)
        assert not rows(sw, kind="n_resumed")  # digest rows are no pending news
        await passes_go_by(sw, passes)
        assert calls["_deliver_digests"] > seen["_deliver_digests"] and calls["_check_quiet"] > seen["_check_quiet"]
        # the resume moved the digest floor: the rows are not due yet, and the team is not asleep meanwhile
        assert team.count(b) == team.count(c) == 1 and sw._thread_quiet is None
        age(sw, DIGEST)
        await run_until(lambda: team.count(b) == 2 and team.count(c) == 2 and calm(sw))
        for name in (b, c):
            wake = team.last(name)
            assert DIGEST_HEAD in wake and f'{a} posted #3 "Parser plan"' in wake
            assert not rows(sw, name, undelivered=True)
        assert team.count(a) == 1

        # T2 runs again: a new quiet period after the resume, so the first nudge tells USER nothing
        await run_until(lambda: sw._thread_quiet is not None)
        age(sw, WAIT)
        await run_until(lambda: len(rows(sw, kind="n_thread_quiet")) == 1 and calm(sw))
        assert rows(sw, kind="n_thread_quiet")[0]["recipient"] == a  # the least recently active
        assert team.count(a) == 2 and not rows(sw, USER, "u_thread_quiet")

        # a pause in the middle of a quiet period drops its clock: USER is never told because of it
        await run_until(lambda: sw._thread_quiet is not None and sw._thread_quiet[1] >= float(
            sw.member(a)["last_active"]))
        assert sw.pause(USER, USER_PAUSE)
        age(sw, 100 * WAIT)
        await passes_go_by(sw, passes, 3)
        sw.resume(USER)
        assert sw._thread_quiet is None and sw._thread_quiet_woken is None
        await passes_go_by(sw, passes, 3)
        assert len(rows(sw, kind="n_thread_quiet")) == 1 and not rows(sw, USER, "u_thread_quiet")
        assert not tool_errors(sw)
    finally:
        await sw.stop()


# ===================================================================== a board session as before
async def test_a_board_session_runs_its_own_scheduler_and_notifications(tmp_path, open_swarms):
    sw, team, passes, (a, b, c) = await kicked_off(tmp_path, open_swarms, mode="board")
    calls = record_calls(sw, "_deliver_digests", "_check_quiet", "_nudge_idle", "_wake_team_asleep")
    try:
        assert sw.mode == "board" and sw.forum is None
        assert not any(getattr(listener, "__self__", None).__class__.__name__ == "Forum"
                       for listener in sw.bus._listeners)
        assert any("planning" in t.tags for t in sw.board.list())
        await act(sw, a, "write_file", {"path": "src/parser.py", "content": "def parse(): ...\n"})
        sw.notify(b, "n_wrap_up_end", wake=False)
        await passes_go_by(sw, passes)
        assert calls["_deliver_digests"] == calls["_check_quiet"] == 0
        assert calls["_nudge_idle"] > 0 and calls["_wake_team_asleep"] > 0
        assert not any(r["kind"].startswith("f_") for r in rows(sw))
        for r in rows(sw):  # no digest or settle row, a quiet notice as before
            assert DIGEST_KEY not in r["data"] and SETTLE_KEY not in r["data"]
        assert rows(sw, b, "n_wrap_up_end")[0]["data"] == {QUIET_KEY: True}
        assert sw.digest_deadline(a) is None and sw._thread_quiet is None and sw._thread_quiet_woken is None
        assert "From what you follow" not in "".join(w for n in (a, b, c) for w in team.wakes.get(n, []))
    finally:
        await sw.stop()
