"""autoreply.py: the requests to the user and the automatic answers in the user's place (docs/TUI.md §2, docs/SPEC.md
§21.5), and stop_when_done (§21.4), which follows the journal the same way. Real Swarms and SessionManagers on
ScriptedLLM only."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import pytest

import dreamteam
from dreamteam import autoreply, prompts
from dreamteam import settings as st
from dreamteam.autoreply import (AUTO_REPLY, AUTO_REVIEW, BLOCKING_KINDS, ID_MAX, SessionAutoReply, blocking_requests,
                                 follow_journal, journal_head, read_requests, reply_text, request_key, requests_of,
                                 review_key, review_text, wants_reply)
from dreamteam.config import Config
from dreamteam.db import Database, dumps
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, Status
from dreamteam.sessions import SessionManager, read_stats
from dreamteam.swarm import Swarm
from dreamteam.tools import ToolContext, call_tool
from dreamteam.tui import printmode, render


class TestPure:
    @pytest.mark.parametrize("msg, wanted", [
        ({"id": 3, "sender": "Anna Smith", "recipients": ["user"]}, True),
        ({"id": 3, "sender": "Anna Smith", "recipients": ["Boris Lee", "user"]}, True),
        ({"id": 3, "sender": "user", "recipients": ["user"]}, False),          # the user's own
        ({"id": 3, "sender": "Anna Smith", "recipients": ["Boris Lee"]}, False),
        ({"id": 3, "sender": "Anna Smith", "recipients": []}, False),
        ({"id": 3, "sender": "Anna Smith", "recipients": None}, False),
        ({"id": 3, "sender": "Anna Smith"}, False),
        ({"id": "3", "sender": "Anna Smith", "recipients": ["user"]}, False),  # no journaled id
        ({"sender": "Anna Smith", "recipients": ["user"]}, False),
    ])
    def test_wants_reply(self, msg, wanted):
        assert wants_reply(msg) is wanted

    def test_review_key(self):
        task = {"id": 7, "title": "Write", "status": "review", "reviewer": "user", "updated_at": 12.5}
        assert review_key(task) == (7, 12.5)
        assert review_key({**task, "updated_at": 13.0}) != review_key(task)   # submitted again: another round
        assert review_key({**task, "updated_at": None}) == (7, None)
        assert review_key({k: v for k, v in task.items() if k != "updated_at"}) == (7, None)
        assert review_key({**task, "reviewer": "Anna Smith"}) is None        # a member reviews it
        assert review_key({**task, "reviewer": None}) is None
        for status in ("todo", "in_progress", "waiting", "done", "failed", "cancelled"):
            assert review_key({**task, "status": status}) is None
        assert review_key({**task, "id": "7"}) is None
        assert review_key({}) is None

    def test_texts(self):
        assert set(AUTO_REPLY) == set(AUTO_REVIEW) == {"ru", "en"}
        for lang in ("ru", "en"):
            assert reply_text(lang) == AUTO_REPLY[lang] and review_text(lang) == AUTO_REVIEW[lang]
        for lang in (None, "", "de"):
            assert reply_text(lang) == AUTO_REPLY["en"] and review_text(lang) == AUTO_REVIEW["en"]

    def test_print_mode_uses_the_same_texts_and_limit(self):
        assert printmode.AUTO_REPLY is autoreply.AUTO_REPLY and printmode.AUTO_REVIEW is autoreply.AUTO_REVIEW
        assert printmode.AUTO_REPLY_MAX == autoreply.AUTO_REPLY_MAX == 50

    @pytest.mark.parametrize("summary, hub", [
        ({"id": "s", "via": "api", "answers": "auto"}, True),
        ({"id": "s", "via": "api", "answers": "manual"}, False),   # the API client answers
        ({"id": "s", "via": "api"}, False),                        # (the hub's summary always has both keys)
        ({"id": "s", "answers": "auto"}, False),                   # not a session of the API: `answers` alone is not it
        ({"id": "s", "via": "home", "answers": "auto"}, False),
        ({"id": "s"}, False),
        ({}, False),
        (None, False),
        ("api", False),
    ])
    def test_hub_answers(self, summary, hub):
        """Which sessions the hub answers for itself (docs/SPEC.md §21.5), so `-p` sends nothing there."""
        assert autoreply.hub_answers(summary) is hub


def test_the_shared_run_modules_import_no_tui():
    """Hub code imports autoreply, result, instruct and the event texts (eventtext, uitext, symbols): none of them may
    pull in dreamteam.tui, whose package imports prompt_toolkit, or rich (a fresh, isolated interpreter, with this
    test run's sources first on its path). autoreply does not import the swarm module either: the swarm imports it."""
    src = str(Path(dreamteam.__file__).resolve().parent.parent)
    code = (
        "import sys\n"
        f"sys.path.insert(0, {src!r})\n"
        "import dreamteam.autoreply\n"
        "assert 'dreamteam.swarm' not in sys.modules\n"
        "import dreamteam.autoreply, dreamteam.instruct, dreamteam.result\n"
        "import dreamteam.eventtext, dreamteam.uitext, dreamteam.symbols\n"
        f"assert dreamteam.__file__.startswith({src!r}), dreamteam.__file__\n"
        "bad = sorted(m for m in sys.modules if m in ('prompt_toolkit', 'rich') or m.startswith(('prompt_toolkit.',"
        " 'rich.', 'dreamteam.tui')))\n"
        "assert not bad, bad\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_request_key():
    assert request_key("q12") == ("question", 12)
    assert request_key("m15") == ("message", 15)
    assert request_key("r7-418") == ("review", 7, 418)
    assert request_key("r7-0") == ("review", 7, 0)   # a review with no round in the journal
    assert request_key("s-web-3f9a0c1b2d4e") == ("mcp", "web", "3f9a0c1b2d4e")
    assert request_key("s-web-api-3f9a0c1b2d4e") == ("mcp", "web-api", "3f9a0c1b2d4e")
    assert request_key("s-old-") == ("mcp", "old", "")   # a record of an older version has no digest
    assert request_key(f"q{ID_MAX}") == ("question", ID_MAX)
    assert request_key(f"r{ID_MAX}-{ID_MAX}") == ("review", ID_MAX, ID_MAX)
    for wrong in ("q0", "q012", "q12x", "x12", "r7", "r7-", "r-1", "r7-01", "s--", "s-web", "q 12", "", "q12\n",
                  "Q12", "s-web-3F9A0C1B2D4E", "s-web-3f9a0c1b2d4", None, 12,
                  # numbers no SQLite id has; thousands of digits never reach int() (a ValueError past 4300)
                  f"q{ID_MAX + 1}", f"m{'1' * 20}", f"r{ID_MAX + 1}-1", f"r1-{ID_MAX + 1}", "q" + "9" * 5000,
                  "r1-" + "9" * 5000, "m" + "1" * 4400):
        assert request_key(wrong) is None, wrong[:30] if isinstance(wrong, str) else wrong


def test_the_limit_notice():
    """Past AUTO_REPLY_MAX the user is told once (u_auto_reply_limit), a warning in the terminal's feed."""
    for lang in ("en", "ru"):
        text = prompts.t(lang, "u_auto_reply_limit", max=50)
        assert "50" in text and "{" not in text
    assert "u_auto_reply_limit" in render._WARN_NOTES
    assert BLOCKING_KINDS == {"question", "review", "mcp"}


# ---- helpers ----------------------------------------------------------------------------------------------------
ANNA, BORIS, CARL = "Anna Smith", "Boris Petrov", "Carl Jones"
LONG_QUESTION = ("Which database should the service use? " + "Some details. " * 80).strip()   # over 600 characters
NO_GIT = {"workspace": {"git": False}}
MARK = "haiku"


def swarm_config(tmp_path: Path, **overrides: Any) -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Requests", "language": "en", "start_agents": 0, "max_agents": 6, "max_cost_usd": 0,
                  "clarify": True},
        "agents": {"stagger_seconds": 0, "max_steps_per_turn": 10},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    for section, values in overrides.items():
        raw.setdefault(section, {}).update(values)
    return Config.from_dict(tmp_path / "project", raw)


@contextlib.asynccontextmanager
async def team(tmp_path: Path, *names: str, **overrides: Any) -> AsyncIterator[Swarm]:
    """A started swarm without agent loops (the test acts for its members), with these AI members; clarify on."""
    sw = Swarm(config=swarm_config(tmp_path, **overrides),
               llm_factory=lambda profile, agent: ScriptedLLM(lambda *a: "ok", profile))
    await sw.start(run_agents=False)
    try:
        for name in names:
            await sw.hire(USER, "developer", name=name)
        yield sw
    finally:
        await sw.stop()
        sw.close()


async def act(sw: Swarm, actor: str, tool: str, **args: Any) -> str:
    text, is_error = await call_tool(ToolContext(sw, actor, None), tool, args)
    assert not is_error, text
    return text


def newest_from(sw: Swarm, sender: str) -> int:
    return int(sw.db.scalar("SELECT MAX(id) FROM messages WHERE sender = ?", (sender,)))


def message_ts(sw: Swarm, mid: int) -> float:
    return sw.db.scalar("SELECT ts FROM messages WHERE id = ?", (mid,))


def answers_to(sw: Swarm, name: str) -> list[dict[str, Any]]:
    """The user's messages that reached `name` (named or through "all"), oldest first."""
    rows = sw.db.all("SELECT m.id, m.text, m.reply_to FROM messages m JOIN inbox i ON i.message_id = m.id "
                     "WHERE m.sender = ? AND i.recipient = ? ORDER BY m.id", (USER, name))
    return [dict(row) for row in rows]


def user_notes(sw: Swarm, kind: str) -> list[dict[str, Any]]:
    return [json.loads(row["data"]) for row in sw.db.all(
        "SELECT data FROM notifications WHERE recipient = ? AND kind = ? ORDER BY id", (USER, kind))]


async def until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


async def settled() -> None:
    """A few turns of the event loop: the followers read what was journaled."""
    for _ in range(10):
        await asyncio.sleep(0.01)


def to_review(sw: Swarm, title: str, assignee: str = ANNA) -> int:
    """A task of `assignee`'s handed in for the user's review; its id."""
    task = sw.board.create(USER, title, assignee=assignee, reviewer=USER)
    if sw.board.get(task.id).status != Status.IN_PROGRESS:
        sw.board.claim(assignee, task.id)
    sw.board.complete(assignee, task.id, f"{title}: done")
    handed = sw.board.get(task.id)
    assert handed.status == Status.REVIEW and handed.reviewer == USER
    return task.id


def review_event(sw: Swarm, task_id: int) -> int:
    """The newest task.updated that put task `task_id` in review."""
    return int(sw.db.scalar("SELECT MAX(id) FROM events WHERE type = 'task.updated' "
                            "AND json_extract(data, '$.task.id') = ? "
                            "AND json_extract(data, '$.changes.status[1]') = 'review'", (task_id,)))


def event_of_message(sw: Swarm, mid: int) -> dict[str, Any]:
    return next(e for e in sw.bus.recent(types=("message.sent",), limit=1000) if e["data"]["message"]["id"] == mid)


# ---- the requests (requests_of, read_requests, blocking_requests) -------------------------------------------------
async def test_a_question_pause_is_a_blocking_question_with_its_full_text(tmp_path):
    async with team(tmp_path, ANNA, BORIS) as sw:
        await act(sw, ANNA, "send_message", to=["user"], text=LONG_QUESTION, pause_swarm=True)
        asked = newest_from(sw, ANNA)
        assert sw.pending_question()["message_id"] == asked
        requests = requests_of(sw)
        # the whole text (the snapshot's is cut), and the message is not listed again as a message
        assert requests == [{"id": f"q{asked}", "kind": "question", "ts": message_ts(sw, asked), "from": ANNA,
                             "text": LONG_QUESTION, "pauses_swarm": True}]
        assert len(sw.snapshot()["swarm"]["question"]["text"]) < len(LONG_QUESTION)
        assert blocking_requests(requests) == requests
        sw.mail.send(USER, [ANNA], "SQLite.")   # the answer ends the pause
        assert not sw.paused and requests_of(sw) == []


async def test_a_clarify_planner_that_waits_is_a_question_that_does_not_pause(tmp_path):
    async with team(tmp_path, ANNA, BORIS) as sw:
        sw.set_goal(USER, "Build a calculator")
        card = sw.board.list(tag="planning")[0]
        assert prompts.CLARIFY_TAG in card.tags
        sw.board.claim(ANNA, card.id)
        await act(sw, ANNA, "send_message", to=["user"], text="1. Which operations? (default: + - * /)")
        asked = newest_from(sw, ANNA)
        assert sw.snapshot()["swarm"]["clarify_wait"] == {"name": ANNA, "task_id": card.id}
        requests = requests_of(sw)
        assert requests == [{"id": f"q{asked}", "kind": "question", "ts": message_ts(sw, asked), "from": ANNA,
                             "text": "1. Which operations? (default: + - * /)", "pauses_swarm": False}]
        assert not sw.paused and blocking_requests(requests) == requests
        # read from the file: the same, and without [swarm] clarify the planner waits for nothing (a plain message)
        assert read_requests(sw.db.path, clarify=True) == requests
        assert [(r["id"], r["kind"]) for r in read_requests(sw.db.path)] == [(f"m{asked}", "message")]
        sw.mail.send(USER, [ANNA], "All four.")
        assert requests_of(sw) == []


async def test_messages_are_one_per_active_member_until_the_user_writes_to_it(tmp_path):
    async with team(tmp_path, ANNA, BORIS, CARL) as sw:
        sw.mail.send(ANNA, ["user"], "First report")
        sw.mail.send(ANNA, ["user"], "Second report")
        anna = newest_from(sw, ANNA)
        sw.mail.send(BORIS, ["user", CARL], "To both of you")
        boris = newest_from(sw, BORIS)
        sw.mail.send(CARL, ["user"], "Carl's note")
        sw.mail.send(BORIS, [ANNA], "Not for the user")   # never a request
        assert [(r["id"], r["kind"], r["from"], r["text"]) for r in requests_of(sw)] == [
            (f"m{anna}", "message", ANNA, "Second report"), (f"m{boris}", "message", BORIS, "To both of you"),
            (f"m{newest_from(sw, CARL)}", "message", CARL, "Carl's note")]
        assert blocking_requests(requests_of(sw)) == []   # reports to the user do not hold the work
        sw.mail.send(USER, [ANNA], "Thanks")   # the user's message to Anna closes hers
        await sw.retire(USER, CARL)            # a member who left can never be answered
        assert [r["id"] for r in requests_of(sw)] == [f"m{boris}"]
        sw.mail.send(USER, ["all"], "Go on, all of you")   # through "all" too
        assert requests_of(sw) == []


async def test_a_review_request_names_its_round(tmp_path):
    async with team(tmp_path, ANNA) as sw:
        task_id = to_review(sw, "Write the parser")
        first = review_event(sw, task_id)
        stamp = sw.db.scalar("SELECT ts FROM events WHERE id = ?", (first,))
        expected = {"id": f"r{task_id}-{first}", "kind": "review", "ts": stamp, "from": ANNA,
                    "task": {"id": task_id, "title": "Write the parser", "result": "Write the parser: done"}}
        assert requests_of(sw) == [expected]
        assert blocking_requests([expected]) == [expected]
        sw.board.update(USER, task_id, priority=2)   # another write of the row: the same round, the same id
        assert [r["id"] for r in requests_of(sw)] == [expected["id"]]
        sw.board.review(USER, task_id, False, "Handle empty input")
        assert requests_of(sw) == []
        sw.board.complete(ANNA, task_id, "Parser: done, empty input too")   # handed in again: a new round
        again = review_event(sw, task_id)
        assert again > first and [r["id"] for r in requests_of(sw)] == [f"r{task_id}-{again}"]
        sw.board.review(USER, task_id, True)
        assert requests_of(sw) == []
        # each task its own round, whatever came after it in the journal
        lexer, docs = to_review(sw, "Write the lexer"), to_review(sw, "Write the docs")
        sw.board.update(USER, lexer, priority=3)   # a later event of the lexer's that is no new round
        assert [r["id"] for r in requests_of(sw)] == [f"r{lexer}-{review_event(sw, lexer)}",
                                                       f"r{docs}-{review_event(sw, docs)}"]


async def test_an_mcp_server_waiting_for_approval_is_a_request_with_its_digest(tmp_path):
    async with team(tmp_path, ANNA) as sw:
        digest = "3f9a0c1b2d4e" + "5" * 52
        sw.db.execute(
            "INSERT INTO mcp_servers(name, owner, scope, task_id, description, command, cwd, requirements, origin,"
            " status, tools, error, created_at, updated_at, digest, approved_digest)"
            " VALUES (?, ?, 'swarm', NULL, 'Fetches pages', '[]', NULL, '[]', 'workspace', ?, ?, NULL, 1.0, ?, ?, NULL)",
            ("web-api", ANNA, "pending", dumps([{"name": "fetch", "description": "Fetch a page"}, {"name": "head"}]),
             2.5, digest))
        sw.db.execute("INSERT INTO mcp_servers(name, owner, scope, origin, status, tools, created_at, updated_at)"
                      " VALUES ('ready', ?, 'swarm', 'workspace', 'active', '[]', 1.0, 1.0)", (ANNA,))
        request = {"id": "s-web-api-3f9a0c1b2d4e", "kind": "mcp", "ts": 2.5, "from": ANNA,
                   "server": {"name": "web-api", "description": "Fetches pages", "tools": ["fetch", "head"]}}
        assert requests_of(sw) == [request]
        assert request_key(request["id"]) == ("mcp", "web-api", "3f9a0c1b2d4e")
        assert blocking_requests([request]) == [request]
        # a damaged record (its tools are no JSON) is still a request, without its tools, and hides no other one
        sw.db.execute("INSERT INTO mcp_servers(name, owner, scope, origin, status, tools, created_at, updated_at, digest)"
                      " VALUES ('broken', ?, 'swarm', 'workspace', 'pending', '{not json', 1.0, 3.0, ?)",
                      (ANNA, "a" * 64))
        broken = {"id": "s-broken-aaaaaaaaaaaa", "kind": "mcp", "ts": 3.0, "from": ANNA,
                  "server": {"name": "broken", "description": "", "tools": []}}
        assert requests_of(sw) == [request, broken]
        assert read_stats(sw.db.path, sw.db.conn)["requests"] == 2


async def test_a_sessions_requests_are_the_same_running_and_stopped_and_its_summary_counts_them(tmp_path):
    manager = manager_for(home_for(tmp_path), idler)
    try:
        sid = (await manager.create("Parser", "Write a parser", overrides=NO_GIT,
                                    api={"answers": "manual", "stop_when_done": False}))["id"]
        swarm = manager.swarms[sid]
        member = swarm.members()[0]["name"]
        assert manager.summary(sid)["requests"] == 0
        swarm.mail.send(member, ["user"], "Started with the lexer.")
        to_review(swarm, "Lexer", assignee=member)
        live = requests_of(swarm)
        assert [r["kind"] for r in live] == ["message", "review"]
        assert manager.summary(sid)["requests"] == 2
        await manager.stop(sid)
        path = manager.dir_of(sid) / ".dreamteam" / "swarm.db"
        assert read_requests(path) == live and read_stats(path)["requests"] == 2
        assert manager.summary(sid)["requests"] == 2 and sid not in manager.swarms   # read without loading it
        assert read_requests(tmp_path / "missing.db") == [] and read_stats(tmp_path / "missing.db")["requests"] == 0
    finally:
        await manager.shutdown()


# ---- SessionAutoReply ---------------------------------------------------------------------------------------------
async def test_a_members_message_is_answered_once(tmp_path):
    async with team(tmp_path, ANNA, BORIS) as sw:
        sw.mail.send(ANNA, ["user"], "Started: the parser first.")
        waiting = newest_from(sw, ANNA)
        auto = SessionAutoReply(sw)
        auto.start()   # the sweep answers what already waits
        try:
            assert [(a["text"], a["reply_to"]) for a in answers_to(sw, ANNA)] == [(AUTO_REPLY["en"], waiting)]
            sw.mail.send(BORIS, ["user"], "Shall I write the tests too?")
            boris = newest_from(sw, BORIS)
            await until(lambda: len(answers_to(sw, BORIS)) == 1)
            assert answers_to(sw, BORIS)[0]["reply_to"] == boris
            # the same event again, a second sweep: nothing more
            auto.on_event(event_of_message(sw, boris))
            auto.on_event(event_of_message(sw, waiting))
            auto.sweep()
            await settled()
            assert len(answers_to(sw, ANNA)) == 1 and len(answers_to(sw, BORIS)) == 1
            assert auto.replies == 2 and requests_of(sw) == []
            # two messages before the follower reads them: each is answered, as `dreamteam -p` does (this run's own
            # answer to the first is no reason to skip the second)
            sw.mail.send(ANNA, ["user"], "One more thing.")
            sw.mail.send(ANNA, ["user"], "And another.")
            last = newest_from(sw, ANNA)
            await until(lambda: len(answers_to(sw, ANNA)) == 3)
            assert [a["reply_to"] for a in answers_to(sw, ANNA)] == [waiting, last - 1, last]
        finally:
            await auto.stop()
        assert not auto.running and not sw.bus._queues   # its follower ended with it


async def test_a_message_the_user_answered_first_is_skipped(tmp_path):
    """Restart-safe: a member's message that a later message of the user to that member followed is not answered
    (the user on the board, a `-p` client, the run before a restart)."""
    async with team(tmp_path, ANNA) as sw:
        sw.mail.send(ANNA, ["user"], "Report one")
        sw.mail.send(USER, [ANNA], "Noted")
        auto = SessionAutoReply(sw)
        auto.start()
        try:
            sw.mail.send(ANNA, ["user"], "Report two")
            sw.mail.send(USER, ["all"], "Answered on the board")   # before the follower reads Report two
            await settled()
            assert [a["text"] for a in answers_to(sw, ANNA)] == ["Noted", "Answered on the board"]
            assert auto.replies == 0
        finally:
            await auto.stop()


async def test_a_review_is_approved_once_per_round(tmp_path):
    async with team(tmp_path, ANNA) as sw:
        reviews: list[tuple] = []
        real = sw.board.review

        def spy(actor, task_id, approve, comment="", notes=None):
            reviews.append((actor, task_id, approve, comment))
            return real(actor, task_id, approve, comment, notes)

        sw.board.review = spy
        first = to_review(sw, "Parser")
        sw.board.review(USER, first, False, "Handle empty input")   # returned: the next hand-in is a new round
        sw.board.complete(ANNA, first, "Parser: done, empty input too")
        auto = SessionAutoReply(sw)
        auto.start()
        try:
            assert sw.board.get(first).status == Status.DONE   # the sweep approved the waiting round
            second = to_review(sw, "Lexer")
            await until(lambda: sw.board.get(second).status == Status.DONE)
            for event in sw.bus.recent(types=("task.updated",), limit=1000):   # every round's event again
                auto.on_event(event)
            auto.sweep()
            await settled()
            assert reviews == [(USER, first, False, "Handle empty input"), (USER, first, True, AUTO_REVIEW["en"]),
                               (USER, second, True, AUTO_REVIEW["en"])]
            assert auto.replies == 0   # approvals never count against the limit
            # a task the user reviews meanwhile is left alone: it is no longer in review for the user
            third = to_review(sw, "Docs")
            sw.board.review(USER, third, True, "Fine")
            await settled()
            assert [r for r in reviews if r[1] == third] == [(USER, third, True, "Fine")]
        finally:
            await auto.stop()


async def test_a_question_pause_is_answered(tmp_path):
    async with team(tmp_path, ANNA, BORIS) as sw:
        await act(sw, ANNA, "send_message", to=["user"], text="SQLite or Postgres?", pause_swarm=True)
        asked = newest_from(sw, ANNA)
        assert sw.paused and sw.pending_question()["message_id"] == asked
        auto = SessionAutoReply(sw)
        auto.start()
        try:
            assert not sw.paused   # the sweep's answer ended the pause
            assert [(a["text"], a["reply_to"]) for a in answers_to(sw, ANNA)] == [(AUTO_REPLY["en"], asked)]
            assert requests_of(sw) == []
            # a question asked while it runs: the answer to its message ends the pause, once
            await act(sw, BORIS, "send_message", to=["user"], text="Tabs or spaces?", pause_swarm=True)
            assert sw.paused
            await until(lambda: not sw.paused)
            await settled()
            assert [a["reply_to"] for a in answers_to(sw, BORIS)] == [newest_from(sw, BORIS)]
            assert len(answers_to(sw, ANNA)) == 1
        finally:
            await auto.stop()


async def test_past_the_limit_messages_wait_and_the_user_is_told_once(tmp_path):
    """Past the limit the members' messages stay requests and the user is told once; a question pause and a review
    are still answered."""
    async with team(tmp_path, ANNA, BORIS) as sw:
        auto = SessionAutoReply(sw, limit=1)
        auto.start()
        try:
            sw.mail.send(ANNA, ["user"], "Report one")
            await until(lambda: len(answers_to(sw, ANNA)) == 1)
            sw.mail.send(ANNA, ["user"], "Report two")
            sw.mail.send(BORIS, ["user"], "Report three")
            await act(sw, BORIS, "send_message", to=["user"], text="SQLite or Postgres?", pause_swarm=True)
            asked = newest_from(sw, BORIS)
            await until(lambda: not sw.paused)   # the question, past the limit too
            await settled()
            assert [a["reply_to"] for a in answers_to(sw, BORIS)] == [asked]
            assert len(answers_to(sw, ANNA)) == 1
            assert [(r["kind"], r["from"], r["text"]) for r in requests_of(sw)] == [("message", ANNA, "Report two")]
            sw.mail.send(ANNA, ["user"], "Report four")
            task_id = to_review(sw, "Docs", assignee=BORIS)
            await until(lambda: sw.board.get(task_id).status == Status.DONE)   # a review, past the limit too
            assert len(answers_to(sw, ANNA)) == 1
            assert user_notes(sw, "u_auto_reply_limit") == [{"max": 1}]
        finally:
            await auto.stop()


async def test_a_hub_restart_answers_nothing_twice(tmp_path):
    home = home_for(tmp_path)
    first = manager_for(home, reporter)
    try:
        sid = (await first.create("Parser", "Write a parser", overrides=NO_GIT,
                                  api={"answers": "auto", "stop_when_done": False}))["id"]
        swarm = first.swarms[sid]
        member = swarm.members()[0]["name"]
        await until(lambda: len(answers_to(swarm, member)) == 1, timeout=10)
    finally:
        await first.shutdown()
    again = manager_for(home, reporter)
    try:
        assert await again.resume() == [sid]
        swarm = again.swarms[sid]
        await asyncio.sleep(0.2)
        assert len(answers_to(swarm, member)) == 1   # the restart's sweep found it answered
        swarm.mail.send(member, ["user"], "Done with the lexer.")   # a new message is answered as before
        await until(lambda: len(answers_to(swarm, member)) == 2)
    finally:
        await again.shutdown()


async def test_a_session_with_manual_answers_gets_no_automatic_answer(tmp_path):
    manager = manager_for(home_for(tmp_path), reporter)
    try:
        sid = (await manager.create("Parser", "Write a parser", overrides=NO_GIT,
                                    api={"answers": "manual", "stop_when_done": False}))["id"]
        swarm = manager.swarms[sid]
        member = swarm.members()[0]["name"]
        await until(lambda: any(r["kind"] == "message" for r in requests_of(swarm)), timeout=10)
        await asyncio.sleep(0.2)
        assert answers_to(swarm, member) == [] and sid not in manager._run_watch
    finally:
        await manager.shutdown()


async def test_an_api_block_without_answers_counts_as_auto(tmp_path):
    """`answers` defaults to "auto" (API_GIVEN) for a block that lacks it (a hand edit), as `stop_when_done` falls back
    to its default: the summary, the hold and the automatic answers agree."""
    manager = manager_for(home_for(tmp_path), idler)
    try:
        sid = (await manager.create("Edited", "Write a parser", overrides=NO_GIT, start=False,
                                    api={"answers": "manual", "stop_when_done": False}))["id"]
        meta = manager.meta(sid)
        del meta["api"]["answers"]
        manager._write_meta(sid, meta)
        assert manager.summary(sid)["answers"] == "auto"
        await manager.start(sid)
        assert manager._run_watch[sid].auto_reply.running
    finally:
        await manager.shutdown()


async def test_each_message_sent_while_the_run_started_is_answered(tmp_path):
    """SessionManager starts the follower from the journal's head before `session.started`: the sweep answers each
    member's last message, the follower the earlier ones of that run, so each gets its answer, as with `-p`."""
    async with team(tmp_path, ANNA) as sw:
        head = journal_head(sw)
        sw.mail.send(ANNA, ["user"], "Report one")   # (while swarm.start() ran)
        sw.mail.send(ANNA, ["user"], "Report two")
        last = newest_from(sw, ANNA)
        auto = SessionAutoReply(sw)
        auto.start(after=head)
        try:
            assert [a["reply_to"] for a in answers_to(sw, ANNA)] == [last]   # the sweep's
            await until(lambda: len(answers_to(sw, ANNA)) == 2)
            await settled()
            assert sorted(a["reply_to"] for a in answers_to(sw, ANNA)) == [last - 1, last] and auto.replies == 2
        finally:
            await auto.stop()


async def test_an_answer_the_mailbox_refuses_is_logged_and_not_tried_again(tmp_path, caplog):
    async with team(tmp_path, ANNA, BORIS) as sw:
        head = journal_head(sw)
        sw.mail.send(ANNA, ["user"], "I am leaving.")
        mid = newest_from(sw, ANNA)
        await sw.retire(USER, ANNA)   # before the follower reads the message: no answer can reach her
        auto = SessionAutoReply(sw)
        with caplog.at_level(logging.WARNING, logger="dreamteam.autoreply"):
            auto.start(after=head)
            try:
                await settled()
                auto.on_event(event_of_message(sw, mid))   # the same event again
                auto.sweep()
                await settled()
            finally:
                await auto.stop()
        assert answers_to(sw, ANNA) == [] and auto.replies == 0 and mid in auto._answered
        assert len([r for r in caplog.records if f"message #{mid}" in r.getMessage()]) == 1


# ---- the journal, followed by id ----------------------------------------------------------------------------------
async def test_follow_journal_reads_by_id_and_is_woken_by_the_bus(tmp_path):
    async with team(tmp_path, ANNA) as sw:
        after = journal_head(sw)
        sw.bus.emit("goal.set", USER, goal={"title": "before the follower"})   # read from `after` on
        seen: list[tuple[int, str]] = []

        async def follow() -> None:
            async with contextlib.aclosing(follow_journal(sw, after, ("goal.set", "goal.completed"), batch=2)) as ev:
                async for event in ev:
                    seen.append((event["id"], event["type"]))

        task = asyncio.create_task(follow())
        try:
            await until(lambda: len(seen) == 1)
            for i in range(5):   # other types are never given
                sw.bus.emit("agent.activity", ANNA, kind="text", text=f"step {i}")
            for etype in ("goal.set", "goal.completed", "goal.set"):   # more than one batch at once
                sw.bus.emit(etype, USER, goal={})
            await until(lambda: len(seen) == 4)
            ids = [i for i, _ in seen]
            assert ids == sorted(ids) and ids[0] > after
            assert [t for _, t in seen] == ["goal.set", "goal.set", "goal.completed", "goal.set"]
        finally:
            task.cancel()
            await asyncio.wait({task})
        assert not sw.bus._queues   # its wake-up queue went with it


# ---- stop_when_done ------------------------------------------------------------------------------------------------
def call(name: str, **arguments: Any) -> dict:
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


def texts(messages: list[dict]) -> list[str]:
    return [m.get("content") or "" for m in messages if m["role"] in ("user", "tool")]


def finisher(system, messages, tools):
    """Declares the goal achieved as soon as it hears about it."""
    seen = texts(messages)
    last = seen[-1] if seen else ""
    if "marked as achieved" in last or "already marked" in last:
        return call("wait")
    if any(MARK in t for t in seen):
        return call("goal_complete", summary="The haiku is written.")
    return call("wait")


def spender(system, messages, tools):
    return {**call("board"), "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 5.0}}


def reporter(system, messages, tools):
    """Tells the user once that it started, then waits."""
    if not any("Sent message" in t for t in texts(messages)):
        return call("send_message", to=["user"], text="Started: the lexer first.")
    return call("wait")


def idler(system, messages, tools):
    return call("wait")


def home_for(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 60},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    })
    return home


def manager_for(home: Path, policy: Callable) -> SessionManager:
    return SessionManager(home, llm_factory=lambda profile, agent: ScriptedLLM(policy, profile))


def stop_reasons(manager: SessionManager, sid: str) -> list[str]:
    """The reasons of the session's `session.stopped` events, oldest first (its connection while it is loaded)."""
    swarm = manager.swarms.get(sid)
    if swarm is not None:
        rows = [row["data"] for row in swarm.db.all("SELECT data FROM events WHERE type = 'session.stopped' "
                                                    "ORDER BY id")]
    else:
        db = Database(manager.dir_of(sid) / ".dreamteam" / "swarm.db")
        try:
            rows = [row["data"] for row in db.all("SELECT data FROM events WHERE type = 'session.stopped' "
                                                  "ORDER BY id")]
        finally:
            db.close()
    return [json.loads(data)["reason"] for data in rows]


async def test_stop_when_done_stops_the_session_when_its_goal_is_completed(tmp_path):
    manager = manager_for(home_for(tmp_path), finisher)
    try:
        sid = (await manager.create("Haiku", f"Write a {MARK}", overrides=NO_GIT, api={"answers": "auto"}))["id"]
        await until(lambda: manager.is_final(sid), timeout=10)   # stop_when_done is the default
        assert sid not in manager.swarms and manager.meta(sid)["autostart"] is False
        assert stop_reasons(manager, sid) == ["done"]
        assert not manager._run_watch and not manager._stopping
        # a start from the board of the done session keeps it running: only the events of its own run count
        await manager.start(sid)
        await asyncio.sleep(0.3)
        assert sid in manager.running and stop_reasons(manager, sid) == ["done"]
    finally:
        await manager.shutdown()


async def test_stop_when_done_stops_a_session_the_budget_paused(tmp_path):
    manager = manager_for(home_for(tmp_path), spender)
    try:
        sid = (await manager.create("Spend", f"Write a {MARK}", overrides={"swarm": {"max_cost_usd": 1.0}, **NO_GIT},
                                    api={"answers": "auto"}))["id"]
        await until(lambda: manager.is_final(sid), timeout=10)
        assert stop_reasons(manager, sid) == ["budget"] and manager.meta(sid)["autostart"] is False
    finally:
        await manager.shutdown()


async def test_without_stop_when_done_a_done_session_keeps_running(tmp_path):
    manager = manager_for(home_for(tmp_path), finisher)
    try:
        sid = (await manager.create("Haiku", f"Write a {MARK}", overrides=NO_GIT,
                                    api={"answers": "auto", "stop_when_done": False}))["id"]
        await until(lambda: (manager.swarms[sid].goal() or {}).get("status") == "done", timeout=10)
        await asyncio.sleep(0.3)
        assert sid in manager.running and stop_reasons(manager, sid) == []
        assert manager._run_watch[sid].watcher is None   # (the automatic answers run)
    finally:
        await manager.shutdown()


async def test_a_goal_completed_behind_a_burst_of_events_still_stops_the_session(tmp_path):
    """The watcher reads the journal by id: a bus queue (EventBus.subscribe holds 2000) would drop the event."""
    manager = manager_for(home_for(tmp_path), idler)
    try:
        sid = (await manager.create("Burst", f"Write a {MARK}", overrides=NO_GIT, api={"answers": "manual"}))["id"]
        swarm = manager.swarms[sid]
        member = swarm.members()[0]["name"]
        for i in range(2100):   # with no await between: nothing reads them meanwhile
            swarm.bus.emit("agent.activity", member, kind="text", text=f"step {i}")
        swarm.complete_goal(USER, "Done by hand.")
        await until(lambda: manager.is_final(sid), timeout=5)
        assert stop_reasons(manager, sid) == ["done"]
    finally:
        await manager.shutdown()


async def test_a_goal_reopened_before_its_stop_keeps_the_session_running(tmp_path):
    manager = manager_for(home_for(tmp_path), idler)
    try:
        sid = (await manager.create("Again", f"Write a {MARK}", overrides=NO_GIT, api={"answers": "manual"}))["id"]
        swarm = manager.swarms[sid]
        swarm.complete_goal(USER, "Done.")
        swarm.reopen_goal(USER, "One more verse.")   # before the scheduled stop takes the lock
        await asyncio.sleep(0.3)
        assert sid in manager.running and stop_reasons(manager, sid) == []
        swarm.complete_goal(USER, "Done now.")   # still watched
        await until(lambda: manager.is_final(sid), timeout=5)
        assert stop_reasons(manager, sid) == ["done"]
    finally:
        await manager.shutdown()


async def test_a_resume_stops_a_session_whose_job_ended_while_the_hub_was_down(tmp_path):
    """The hub stopped between the goal's completion (or the budget pause) and the stop: the resume stops it at
    once. A session not made through v1 is resumed as before."""
    home = home_for(tmp_path)
    first = manager_for(home, idler)
    try:
        done = (await first.create("Done", f"Write a {MARK}", overrides=NO_GIT, api={"answers": "manual"}))["id"]
        spent = (await first.create("Spent", f"Write a {MARK}", overrides=NO_GIT, api={"answers": "manual"}))["id"]
        board = (await first.create("Board", f"Write a {MARK}", overrides=NO_GIT))["id"]
    finally:
        await first.shutdown()   # each keeps its autostart mark
    for sid, change in ((done, "goal"), (spent, "paused"), (board, "goal")):
        db = Database(first.dir_of(sid) / ".dreamteam" / "swarm.db")
        try:
            if change == "goal":   # its planning task stays open, so it is not `finished`: resume() starts it
                db.set("goal", {**db.get("goal"), "status": "done", "summary": "Written."})
            else:
                db.set("paused", {"paused": True, "reason": "budget"})
        finally:
            db.close()
    again = manager_for(home, idler)
    try:
        assert sorted(await again.resume()) == sorted([done, spent, board])
        await until(lambda: again.is_final(done) and again.is_final(spent), timeout=5)
        assert stop_reasons(again, done) == ["shutdown", "done"]
        assert stop_reasons(again, spent) == ["shutdown", "budget"]
        assert again.meta(done)["autostart"] is False and again.meta(spent)["autostart"] is False
        assert board in again.running
    finally:
        await again.shutdown()


async def test_a_hub_that_stops_waits_for_a_scheduled_stop_and_leaves_an_unread_end_to_the_resume(tmp_path):
    home = home_for(tmp_path)
    first = manager_for(home, idler)
    try:
        waited = (await first.create("Waited", f"Write a {MARK}", overrides=NO_GIT, api={"answers": "manual"}))["id"]
        unread = (await first.create("Unread", f"Write a {MARK}", overrides=NO_GIT, api={"answers": "manual"}))["id"]
        async with first._lock(waited):   # its stop is scheduled and waits for the lock
            first.swarms[waited].complete_goal(USER, "Done.")
            await until(lambda: bool(first._stopping))
        first.swarms[unread].complete_goal(USER, "Done.")   # the hub stops before its watcher reads it
    finally:
        await first.shutdown()
    assert not first._stopping and not first._closing   # nothing outlives shutdown()
    assert first.meta(waited)["autostart"] is False and stop_reasons(first, waited) == ["done"]
    assert first.meta(unread)["autostart"] is True and stop_reasons(first, unread) == ["shutdown"]
    again = manager_for(home, idler)
    try:   # (its planning task is open, so it is not `finished`: resumed, then stopped by the resume check)
        assert await again.resume() == [unread]
        await until(lambda: again.is_final(unread), timeout=5)
        assert stop_reasons(again, unread) == ["shutdown", "done"] and again.meta(unread)["autostart"] is False
    finally:
        await again.shutdown()


async def test_a_budget_raised_before_its_stop_keeps_the_session_running(tmp_path):
    manager = manager_for(home_for(tmp_path), idler)
    try:
        sid = (await manager.create("Spend", f"Write a {MARK}", overrides={"swarm": {"max_cost_usd": 1.0}, **NO_GIT},
                                    api={"answers": "manual"}))["id"]
        swarm = manager.swarms[sid]
        async with manager._lock(sid):   # its stop is scheduled and waits for the lock
            swarm.pause(USER, "budget")
            await until(lambda: bool(manager._stopping))
            swarm.set_limits(USER, max_cost_usd=5.0)   # (which ends the budget pause)
        await until(lambda: not manager._stopping)
        assert sid in manager.running and not swarm.paused and stop_reasons(manager, sid) == []
    finally:
        await manager.shutdown()


async def test_an_automatic_stop_keeps_a_start_announced_meanwhile(tmp_path):
    """It ends the run it watched and nothing more: a start that waits for the lock (a `continue`) still starts."""
    manager = manager_for(home_for(tmp_path), idler)
    try:
        sid = (await manager.create("Again", f"Write a {MARK}", overrides=NO_GIT, api={"answers": "manual"}))["id"]
        async with manager._lock(sid):
            manager.swarms[sid].complete_goal(USER, "Done.")
            await until(lambda: bool(manager._stopping))
            mark = manager.mark_starting(sid)
        await until(lambda: not manager._stopping)
        assert sid not in manager.running and stop_reasons(manager, sid) == ["done"]
        assert sid in manager.starting and not manager.is_final(sid)
        assert await manager.start(sid, marked=mark) is not None
        assert sid in manager.running and sid not in manager.starting
    finally:
        await manager.shutdown()


async def test_a_stop_ends_the_automatic_answers_and_the_watcher(tmp_path):
    manager = manager_for(home_for(tmp_path), idler)
    try:
        sid = (await manager.create("Stop", f"Write a {MARK}", overrides=NO_GIT, api={"answers": "auto"}))["id"]
        swarm = manager.swarms[sid]
        watch = manager._run_watch[sid]
        assert watch.auto_reply.running and not watch.watcher.done()
        await manager.stop(sid)
        assert sid not in manager._run_watch and not watch.auto_reply.running and watch.watcher.done()
        assert not swarm.bus._queues   # their wake-up queues went with them
        assert stop_reasons(manager, sid) == ["user"]
    finally:
        await manager.shutdown()
