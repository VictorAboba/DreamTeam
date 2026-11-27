"""Read before write end to end (docs/SPEC.md §7.3 files, both modes; §22.11 the forum, thread sessions): running
swarms of ScriptedLLM agents whose responses decide from what their tool results show, as a model would. Two claims
sent at once under one post give one comment and one refusal that shows the first; a reply is checked against its
own thread only; a post made meanwhile refuses `post_create`; a file another member wrote is refused until it is
read; and a refused write holds the response's `wait`, `task_complete` and like on #1. No network."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from dreamteam import prompts
from dreamteam import swarm as swarm_module
from dreamteam.config import Config
from dreamteam.forum import PLAN_POST, post_version
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, Status
from dreamteam.swarm import Swarm
from dreamteam.tools import ToolContext, call_tool
from dreamteam.workspace import UNREAD_FILE_TEXTS, file_digest, format_size

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

WAIT = {"name": "wait", "arguments": {}}
Entry = list | Callable[[list[dict]], Any]


def call(name: str, **arguments: Any) -> dict:
    return {"name": name, "arguments": arguments}


def last_results(messages: list[dict]) -> dict[str, dict]:
    """The tool results of the agent's previous response, by tool name (the last one of a name wins); a message the
    runtime added after them (an incoming notification, a hint) does not hide them."""
    last = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=len(messages))
    return {m["name"]: m for m in messages[last + 1:] if m.get("role") == "tool"}


class Team:
    """Each LLM call of an agent answers with its next queued entry: a response (a list of calls), or a callable of
    the messages that returns one (sync or async), deciding from the results it sees. None queued: `wait`."""

    def __init__(self) -> None:
        self.todo: dict[str, list[Entry]] = {}
        self.calls: dict[str, int] = {}

    def factory(self, profile: Any, agent: str) -> ScriptedLLM:
        async def handler(system: str, messages: list[dict], tools: list[dict]) -> dict:
            self.calls[agent] = self.calls.get(agent, 0) + 1
            queue = self.todo.get(agent) or []
            entry = queue.pop(0) if queue else [WAIT]
            if callable(entry):
                entry = entry(json.loads(json.dumps(messages)))
                if asyncio.iscoroutine(entry):
                    entry = await entry
            return {"tool_calls": entry}

        return ScriptedLLM(handler, profile)

    def count(self, name: str) -> int:
        return self.calls.get(name, 0)


def make_config(tmp_path: Path, mode: str, start_agents: int = 2, **agents: Any) -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "E2E swarm", "language": "en", "start_agents": start_agents, "max_agents": 4,
                  "max_cost_usd": 0, "mode": mode, "clarify": False, "independent_review": False},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 3600, "max_steps_per_turn": 12, **agents},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    return Config.from_dict(tmp_path / "project", raw)


@pytest.fixture(autouse=True)
def fast_scheduler(monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.05)


@pytest.fixture
def swarms():
    made: list[Swarm] = []
    yield made
    for sw in made:
        try:
            sw.close()
        except Exception:  # noqa: BLE001 - closed already
            pass


async def started(tmp_path: Path, swarms: list, mode: str, run_agents: bool = True,
                  **agents: Any) -> tuple[Swarm, Team, str, str]:
    team = Team()
    sw = Swarm(config=make_config(tmp_path, mode, **agents), llm_factory=team.factory)
    swarms.append(sw)
    await sw.start(run_agents=run_agents)
    a, b = (m["name"] for m in sw.members()[:2])
    return sw, team, a, b


async def run_until(predicate: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


async def quiet(team: Team, *names: str) -> None:
    """None of the agents made an LLM call for a moment (their activations ended)."""
    while True:
        seen = [team.count(n) for n in names]
        await asyncio.sleep(0.4)
        if [team.count(n) for n in names] == seen:
            return


def results(sw: Swarm, name: str, tool_name: str) -> list[dict]:
    rows = sw.db.all("SELECT msg FROM history WHERE agent = ? ORDER BY seq", (name,))
    return [m for m in (json.loads(r["msg"]) for r in rows) if m.get("role") == "tool" and m.get("name") == tool_name]


async def tool(sw: Swarm, actor: str, name: str, args: dict, error: bool = False) -> str:
    text, is_error = await call_tool(ToolContext(sw, actor), name, args)
    assert is_error is error, text
    return text


async def read_whole(sw: Swarm, actor: str, path: str) -> None:
    """`actor` reads every line of the file with read_file pages, as a member does."""
    offset = 1
    while True:
        text = await tool(sw, actor, "read_file", {"path": path, "offset": offset})
        found = re.search(r"lines (\d+)-(\d+) of (\d+)", text)
        assert found, text
        end, total = int(found[2]), int(found[3])
        if end >= total:
            return
        offset = end + 1


def marks(sw: Swarm, member: str) -> dict[str, str | None]:
    return {r["target"]: r["version"] for r in sw.db.all("SELECT target, version FROM read_marks WHERE member = ?",
                                                          (member,))}


def held(key: str, refused_tool: str) -> str:
    return "Error: " + prompts.t("en", key, tool=refused_tool)


def held_waits(sw: Swarm, name: str) -> list[str]:
    return [m["content"] for m in results(sw, name, "wait") if m["is_error"]]


def numbered(count: int, tag: str = "line") -> str:
    return "".join(f"{tag} {n:03d} ".ljust(30, "x") + "\n" for n in range(1, count + 1))


# ------------------------------------------------------------------------------------------ the forum (§22.11)
async def test_two_claims_sent_at_once_give_one_comment_and_a_refusal_that_shows_the_first(tmp_path, swarms):
    sw, team, a, b = await started(tmp_path, swarms, "thread")
    try:
        await asyncio.sleep(0.3)
        arrived: list[str] = []
        both = asyncio.Event()

        def claim_at_once(name: str) -> Callable[[list[dict]], Any]:
            async def entry(messages: list[dict]) -> list[dict]:
                arrived.append(name)
                if len(arrived) == 2:
                    both.set()
                await asyncio.wait_for(both.wait(), 10)   # both models answer in the same moment
                return [call("comment", post_id=PLAN_POST, text="I take the lexer."), WAIT]
            return entry

        def decide(messages: list[dict]) -> list[dict]:
            refused = last_results(messages).get("comment")
            if refused is None or not refused["is_error"]:
                return [WAIT]
            # the refusal shows the other claim: this member takes another part instead
            assert "I take the lexer." in refused["content"]
            return [call("comment", post_id=PLAN_POST, text="The lexer is taken: I take the parser.")]

        for name in (a, b):
            team.todo[name] = [claim_at_once(name), decide]
        sw.set_goal(USER, "Build the parser", "A lexer and a parser.")
        await run_until(lambda: len(sw.forum.comments(PLAN_POST)) == 2)
        await quiet(team, a, b)

        first, second = sw.forum.comments(PLAN_POST)
        winner, loser = first["author"], second["author"]
        assert {winner, loser} == {a, b}
        assert (first["text"], second["text"]) == ("I take the lexer.", "The lexer is taken: I take the parser.")
        assert all(not m["is_error"] for m in results(sw, winner, "comment"))
        refusal, retry = results(sw, loser, "comment")
        assert refusal["is_error"] and not retry["is_error"]
        assert refusal["content"].startswith(f'Error: Not sent: #2 "{sw.forum.get(PLAN_POST)["title"]}" has what you '
                                             "have not read (unread comments by others: 1).")
        assert f"c{first['id']} {winner}" in refusal["content"] and "I take the lexer." in refusal["content"]
        assert f"c{first['id']}" in marks(sw, loser)                      # the refusal counts as reading it
        # the loser's activation went on (it decided again) and ended with a `wait` of its own
        assert held_waits(sw, loser) == [held("wait_refused", "comment")]
        assert not results(sw, loser, "wait")[-1]["is_error"]
        assert held_waits(sw, winner) == []
    finally:
        await sw.stop()


async def test_a_reply_is_checked_against_its_own_thread_only(tmp_path, swarms):
    sw, _team, a, b = await started(tmp_path, swarms, "thread", run_agents=False)
    try:
        sw.set_goal(USER, "Build the parser")
        question = sw.forum.comment(USER, PLAN_POST, "Who takes the lexer?")
        for name in (a, b):
            await tool(sw, name, "post_read", {"post_id": PLAN_POST})
        top = await tool(sw, a, "comment", {"post_id": PLAN_POST, "text": "I take the lexer."})
        # a claim sent as a reply passes an unread top-level claim (the limit rule 3's one form answers)
        reply = await tool(sw, b, "comment", {"post_id": PLAN_POST, "text": "Me.", "reply_to": f"c{question['id']}"})
        assert top.startswith("Commented c") and reply.startswith("Replied c")
        b_reply = sw.forum.comments(PLAN_POST)[-1]
        # ... but a reply in that thread is refused while the thread has a reply its author has not read
        text = await tool(sw, a, "comment", {"post_id": PLAN_POST, "text": "I claimed it above.",
                                             "reply_to": f"c{question['id']}"}, error=True)
        assert f"(unread comments in the thread of c{question['id']}: 1)" in text
        assert f"c{b_reply['id']} {b}" in text and "I take the lexer." not in text.split("Unread comments")[1]
        await tool(sw, a, "comment", {"post_id": PLAN_POST, "text": "I claimed it above.",
                                      "reply_to": f"c{question['id']}"})
        # a top-level comment sees every unread comment of the post by others
        text = await tool(sw, b, "comment", {"post_id": PLAN_POST, "text": "Then I take the parser."}, error=True)
        assert "(unread comments by others: 2)" in text
        assert "I take the lexer." in text and "I claimed it above." in text
        await tool(sw, b, "comment", {"post_id": PLAN_POST, "text": "Then I take the parser."})
        # an edit of a post read in an earlier version blocks a reply; a post never read does not
        made = await tool(sw, a, "post_create", {"title": "Lexer plan", "text": "Tokens first."})
        lexer = int(re.search(r"#(\d+)", made)[1])
        await tool(sw, b, "post_read", {"post_id": lexer})
        note = sw.forum.comment(b, lexer, "Keep the tokens small.")
        text = await tool(sw, a, "post_update", {"post_id": lexer, "text": "Tokens first, then the grammar."},
                          error=True)
        assert text.startswith('Error: Not changed: #') and "Keep the tokens small." in text
        await tool(sw, a, "post_update", {"post_id": lexer, "text": "Tokens first, then the grammar."})
        text = await tool(sw, b, "comment", {"post_id": lexer, "text": "Agreed.", "reply_to": f"c{note['id']}"},
                          error=True)
        assert "(the post was edited)" in text and "Tokens first, then the grammar." in text
        await tool(sw, b, "comment", {"post_id": lexer, "text": "Agreed.", "reply_to": f"c{note['id']}"})
        assert marks(sw, b)[f"p{lexer}"] == post_version(sw.forum.get(lexer))
    finally:
        await sw.stop()


async def test_a_post_made_meanwhile_refuses_post_create_and_the_member_comments_there(tmp_path, swarms):
    sw, team, a, b = await started(tmp_path, swarms, "thread")
    try:
        sw.set_goal(USER, "Build the parser")
        await run_until(lambda: team.count(a) >= 1 and team.count(b) >= 1)
        await quiet(team, a, b)
        made: dict[str, int] = {}
        text = "Numbers for every grammar we try. " * 50                # cut in every notification: never read whole

        async def post_meanwhile(messages: list[dict]) -> list[dict]:
            # the other member posts on the same topic while this one is still deciding
            out = await tool(sw, b, "post_create", {"title": "Parser benchmark", "text": text})
            made["id"] = int(re.search(r"#(\d+)", out)[1])
            return [call("post_create", title="Benchmarks", text="I will measure the parser."), WAIT]

        def comment_there(messages: list[dict]) -> list[dict]:
            refused = last_results(messages)["post_create"]
            assert refused["is_error"] and f'#{made["id"]} "Parser benchmark"' in refused["content"]
            return [call("comment", post_id=made["id"], text="I can add the lexer's numbers.")]

        def again(messages: list[dict]) -> list[dict]:
            refused = last_results(messages)["comment"]
            assert refused["is_error"] and "you have not read the post itself" in refused["content"]
            assert text.strip() in refused["content"]                     # the whole text is shown, now read
            return [call("comment", post_id=made["id"], text="I can add the lexer's numbers.")]

        team.todo[a] = [post_meanwhile, comment_there, again]
        await tool(sw, USER, "send_message", {"to": [a], "text": "Measure the parser, please."})
        await run_until(lambda: "id" in made and len(sw.forum.comments(made["id"])) == 1)
        await quiet(team, a)

        assert [p["title"] for p in sw.db.all("SELECT title FROM posts WHERE author = ?", (a,))] == []
        created = results(sw, a, "post_create")
        assert len(created) == 1 and created[0]["content"].startswith(
            "Error: Not posted: the feed has posts by others that you have not seen (1).")
        assert held_waits(sw, a) == [held("wait_refused", "post_create")]
        comments = results(sw, a, "comment")
        assert [m["is_error"] for m in comments] == [True, False]
        (only,) = sw.forum.comments(made["id"])
        assert only["author"] == a and marks(sw, a)[f"p{made['id']}"] == post_version(sw.forum.get(made["id"]))
    finally:
        await sw.stop()


async def test_a_like_on_1_after_a_refused_comment_or_write_is_held(tmp_path, swarms):
    sw, team, a, b = await started(tmp_path, swarms, "thread")
    try:
        sw.set_goal(USER, "Fix the notes")
        await run_until(lambda: team.count(a) >= 1 and team.count(b) >= 1)
        await quiet(team, a, b)
        objection = "The notes miss the install steps. " * 60          # longer than a waking item: not read
        await tool(sw, b, "comment", {"post_id": PLAN_POST, "text": objection})
        (sw.workspace.root / "notes.md").write_text("draft\n", encoding="utf-8")
        await tool(sw, a, "read_file", {"path": "notes.md"})
        await tool(sw, b, "read_file", {"path": "notes.md"})
        await tool(sw, b, "write_file", {"path": "notes.md", "content": "draft with install steps\n"})
        like = call("vote", target="#1", value="like")
        claim = call("comment", post_id=PLAN_POST, text="Notes checked.")
        write = call("write_file", path="notes.md", content="final notes\n")
        team.todo[a] = [[claim, like], [write, like], [claim, write, like]]
        await tool(sw, USER, "send_message", {"to": [a], "text": "Finish the notes."})
        await run_until(lambda: len(results(sw, a, "vote")) >= 3)
        await quiet(team, a)

        votes = results(sw, a, "vote")
        assert [m["content"] for m in votes[:2]] == [held("vote_refused", "comment"),
                                                     held("vote_refused", "write_file")]
        assert not votes[2]["is_error"]
        assert [m["is_error"] for m in results(sw, a, "comment")] == [True, False]
        assert [m["is_error"] for m in results(sw, a, "write_file")] == [True, False]
        assert [(r["voter"], r["value"]) for r in sw.db.all("SELECT voter, value FROM votes WHERE target = 'p1'")] \
            == [(a, 1)]
        assert (sw.workspace.root / "notes.md").read_text(encoding="utf-8") == "final notes\n"
    finally:
        await sw.stop()


# ----------------------------------------------------------------------------------------------- files (§7.3)
async def test_a_file_another_member_wrote_is_refused_until_it_is_read(tmp_path, swarms):
    sw, team, a, b = await started(tmp_path, swarms, "thread", tool_output_chars=2000)
    try:
        await asyncio.sleep(0.3)
        (sw.workspace.root / "plan.md").write_text(numbered(120), encoding="utf-8")
        await read_whole(sw, a, "plan.md")
        await read_whole(sw, b, "plan.md")
        theirs = numbered(120, "step")
        await tool(sw, b, "write_file", {"path": "plan.md", "content": theirs})   # B overwrites it after A read it
        mine = numbered(121, "mine")

        def reader(messages: list[dict]) -> list[dict]:
            last = last_results(messages)
            if "read_file" in last:
                found = re.search(r"lines (\d+)-(\d+) of (\d+)", last["read_file"]["content"])
                end, total = int(found[2]), int(found[3])
                if end < total:
                    return [call("read_file", path="plan.md", offset=end + 1)]
                return [call("write_file", path="plan.md", content=mine), WAIT]
            if "write_file" in last and last["write_file"]["is_error"]:
                return [call("read_file", path="plan.md")]
            return [WAIT]

        team.todo[a] = [[call("write_file", path="plan.md", content=mine), WAIT], *[reader] * 10]
        await tool(sw, USER, "send_message", {"to": [a], "text": "Rewrite the plan."})
        await run_until(lambda: len(results(sw, a, "write_file")) >= 2)
        await quiet(team, a)

        refusal, passed = results(sw, a, "write_file")
        assert refusal["is_error"] and not passed["is_error"]
        texts = UNREAD_FILE_TEXTS["en"]
        assert refusal["content"].startswith("Error: " + texts["head"].format(
            rel="plan.md", why=texts["why_changed"], size=format_size(len(theirs.encode("utf-8")))))
        assert texts["long_write"].format(lines=120) in refusal["content"] and "step 001" not in refusal["content"]
        assert held_waits(sw, a) == [held("wait_refused", "write_file")]
        assert len(results(sw, a, "read_file")) >= 2                     # every page, as the refusal asked
        assert (sw.workspace.root / "plan.md").read_text(encoding="utf-8") == mine
        assert sw.workspace.file_mark(a, "plan.md") == (file_digest(mine.encode("utf-8")), None)
        # now B's mark is stale: its next write is refused, with the reason
        text = await tool(sw, b, "edit_file", {"path": "plan.md", "old_text": "mine 001", "new_text": "first"},
                          error=True)
        assert texts["why_changed"] in text
    finally:
        await sw.stop()


async def test_a_board_write_of_a_changed_file_holds_task_complete(tmp_path, swarms):
    sw, team, a, b = await started(tmp_path, swarms, "board")
    try:
        await asyncio.sleep(0.3)
        await tool(sw, a, "task_create", {"title": "Rewrite the notes", "goal": "The notes say how to install."})
        task_id = int(sw.db.scalar("SELECT MAX(id) FROM tasks"))
        await tool(sw, a, "task_claim", {"task_id": task_id})
        (sw.workspace.root / "notes.md").write_text("draft\n", encoding="utf-8")
        await tool(sw, a, "read_file", {"path": "notes.md"})
        await tool(sw, b, "read_file", {"path": "notes.md"})
        await tool(sw, b, "write_file", {"path": "notes.md", "content": "draft\ninstall: pip install x\n"})
        await quiet(team, a, b)
        write = call("write_file", path="notes.md", content="install: pip install x\nrun: x --help\n")
        complete = call("task_complete", task_id=task_id, result="The notes say how to install and run.")
        seen: list[str] = []

        def retry(messages: list[dict]) -> list[dict]:
            last = last_results(messages)
            seen.append(sw.board.get(task_id).status)
            # the refusal showed B's change; the task was not completed without the write
            assert last["write_file"]["is_error"] and "install: pip install x" in last["write_file"]["content"]
            assert last["task_complete"]["content"] == held("complete_refused", "write_file")
            return [write, complete]

        team.todo[a] = [[write, complete], retry]
        await tool(sw, USER, "send_message", {"to": [a], "text": "Finish the notes task."})
        await run_until(lambda: len(results(sw, a, "task_complete")) >= 2)
        await quiet(team, a)

        assert seen == [Status.IN_PROGRESS]
        first, second = results(sw, a, "task_complete")
        assert first["is_error"] and not second["is_error"]
        assert sw.board.get(task_id).status in (Status.REVIEW, Status.DONE)
        assert (sw.workspace.root / "notes.md").read_text(encoding="utf-8") == "install: pip install x\nrun: x --help\n"
        assert sw.db.scalar("SELECT COUNT(*) FROM sqlite_master WHERE name = 'read_marks'") == 0   # kv only
    finally:
        await sw.stop()
