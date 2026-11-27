"""Read before write of files (docs/SPEC.md §7.3), both modes: a member overwrites or edits an existing workspace file
only while its file read mark (kv `file_read:<member>:<path key>`) matches the file's bytes; a refusal shows the
current content when it fits and then counts as read. The workspace cases run in a board and in a thread session
with identical results; the runtime cases use ScriptedLLM, the MCP cases the hub's own hub_call. No network."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from dreamteam import db as db_module
from dreamteam import prompts
from dreamteam import swarm as swarm_module
from dreamteam.config import Config
from dreamteam.hub_mcp import hub_call
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.mailbox import sent_as_seen_by
from dreamteam.models import USER, AgentStatus, ReadRefusal, Status, ToolError
from dreamteam.shell import truncate_middle
from dreamteam.swarm import Swarm
from dreamteam.tools import ToolContext, call_tool, frozen
from dreamteam.workspace import (FILE_READ_PREFIX, REFUSAL_RESERVE, UNREAD_FILE_TEXTS, Workspace, file_digest,
                                 format_ranges, format_size, merge_ranges, parse_file_mark, path_key, scratch_rel,
                                 unread_ranges)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

HAS_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not HAS_GIT, reason="git is not installed")

ANNA = "Anna Smith"
BORIS = "Boris King"
CHARS = 2000   # [agents] tool_output_chars of the long-file cases: 120 lines of 30 characters do not fit


def numbered(count: int, width: int = 30) -> str:
    return "".join(f"line {n:03d} ".ljust(width, "x") + "\n" for n in range(1, count + 1))


@pytest.fixture(params=["board", "thread"])
def mode(request) -> str:
    return request.param


@pytest.fixture
def space(make_fake_swarm, mode) -> Callable[..., Any]:
    """An initialised workspace of a FakeSwarm in `mode` with the members ANNA and BORIS (en)."""
    async def factory(chars: int | None = None, git: bool = False, language: str = "en") -> Workspace:
        raw: dict[str, Any] = {"workspace": {"git": git}}
        if chars is not None:
            raw["agents"] = {"tool_output_chars": chars}
        swarm = make_fake_swarm(raw)
        swarm.mode = mode
        w = Workspace(swarm)
        w.commit_events = mode == "thread"
        swarm.workspace = w
        await w.init()
        for name in (ANNA, BORIS):
            swarm.add_member(name, language=language)
        return w

    return factory


def put(ws: Workspace, rel: str, data: bytes | str) -> Path:
    p = ws.root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return p


def mark(ws: Workspace, name: str, rel: str) -> Any:
    return ws.swarm.db.get(f"{FILE_READ_PREFIX}{name}:{path_key(rel)}")


def mark_rows(swarm: Any) -> list[str]:
    return [r["key"] for r in swarm.db.all("SELECT key FROM kv WHERE key LIKE 'file_read:%' ORDER BY key")]


async def refused(awaitable) -> str:
    with pytest.raises(ReadRefusal) as err:
        await awaitable
    return str(err.value)


def head(rel: str, why: str, size: str) -> str:
    return UNREAD_FILE_TEXTS["en"]["head"].format(rel=rel, why=why, size=size)


# ------------------------------------------------------------------------------------------------- helpers
def test_ranges_merge_format_and_gaps():
    assert merge_ranges([(4, 6), (1, 3), (9, 9), (8, 8)]) == [(1, 6), (8, 9)]
    assert format_ranges([(1, 400), (801, 1200), (7, 7)]) == "1-400,801-1200,7"
    assert parse_file_mark("abc") == ("abc", None)
    assert parse_file_mark("abc:1-3,7") == ("abc", [(1, 3), (7, 7)])
    assert parse_file_mark("abc:") == ("abc", [])
    assert parse_file_mark(None) is None and parse_file_mark(3) is None
    assert unread_ranges([(3, 5), (8, 10)], 12) == [(1, 2), (6, 7), (11, 12)]
    assert unread_ranges([(1, 12)], 12) == [] and unread_ranges([], 3) == [(1, 3)]


# ------------------------------------------------------------------------------------------- the refusal
async def test_a_write_over_an_unread_file_shows_its_content_and_then_passes(space):
    ws = await space()
    put(ws, "notes.md", "a\nb\n")
    text = await refused(ws.write(ANNA, "notes.md", "new\n"))
    assert text == (head("notes.md", "exists, and you have not read it", "4 B") + " "
                    + UNREAD_FILE_TEXTS["en"]["fits"] + "\n[notes.md: lines 1-2 of 2]\na\nb")
    assert (ws.root / "notes.md").read_text(encoding="utf-8") == "a\nb\n"
    assert mark(ws, ANNA, "notes.md") == file_digest(b"a\nb\n")   # the refusal counts as reading it
    assert (await ws.write(ANNA, "notes.md", "new\n")).startswith("Updated notes.md")
    assert mark(ws, ANNA, "notes.md") == file_digest(b"new\n")


async def test_the_refusal_is_in_the_members_language(space):
    ws = await space(language="ru")
    put(ws, "notes.md", "a\n")
    text = await refused(ws.edit(ANNA, "notes.md", "a", "b"))
    assert text.startswith("Не записано: 'notes.md' уже существует, а ты его не читал(а) (2 B). ")
    assert UNREAD_FILE_TEXTS["ru"]["fits"] in text and chr(0x451) not in text   # no dotted letter


async def test_a_change_by_anyone_since_the_read_is_refused(space):
    ws = await space()
    put(ws, "plan.md", "x = 1\ny = 2\n")
    ws.read("plan.md", actor=ANNA)
    ws.read("plan.md", actor=BORIS)
    await ws.write(BORIS, "plan.md", "x = 1\ny = 3\n")   # another member's write_file
    text = await refused(ws.write(ANNA, "plan.md", "mine\n"))
    assert text.startswith(head("plan.md", "changed since you last read or wrote it", "12 B"))
    assert text.endswith("[plan.md: lines 1-2 of 2]\nx = 1\ny = 3")
    await ws.write(ANNA, "plan.md", "x = 1\ny = 4\n")    # the refusal showed it: the retry passes
    # a plain write of the test (no event, no commit): an edit whose old_text still matches is refused
    put(ws, "plan.md", "x = 1\ny = 5\n")
    text = await refused(ws.edit(ANNA, "plan.md", "x = 1", "x = 9"))
    assert "changed since you last read or wrote it" in text
    assert (ws.root / "plan.md").read_text(encoding="utf-8") == "x = 1\ny = 5\n"
    assert "Edited plan.md" in await ws.edit(ANNA, "plan.md", "x = 1", "x = 9")


async def test_one_page_lets_edit_pass_and_write_needs_every_page(space):
    ws = await space(chars=CHARS)
    put(ws, "big.txt", numbered(120))
    ws.read("big.txt", offset=41, limit=40, actor=ANNA)
    digest = file_digest((ws.root / "big.txt").read_bytes())
    assert mark(ws, ANNA, "big.txt") == f"{digest}:41-80"
    text = await refused(ws.write(ANNA, "big.txt", "short\n"))
    why = "has lines you have not read since it last changed (1-40, 81-120)"
    assert text == head("big.txt", why, "3.6 KB") + " " + UNREAD_FILE_TEXTS["en"]["long_write"].format(lines=120)
    assert mark(ws, ANNA, "big.txt") == f"{digest}:41-80"   # nothing moves
    # edit_file's exact match guards the region it edits: a partial mark at the current hash is enough
    assert "Edited big.txt" in await ws.edit(ANNA, "big.txt", "line 050 ", "line 05O ")
    edited = file_digest((ws.root / "big.txt").read_bytes())
    assert mark(ws, ANNA, "big.txt") == f"{edited}:"   # the edit moved lines: partial with no ranges
    text = await refused(ws.write(ANNA, "big.txt", "short\n"))
    assert "has lines you have not read since it last changed (all)" in text
    for offset in (81, 1, 41):   # every page, in any order
        ws.read("big.txt", offset=offset, limit=40, actor=ANNA)
    assert mark(ws, ANNA, "big.txt") == edited   # whole
    assert "Updated big.txt" in await ws.write(ANNA, "big.txt", "short\n")


async def test_a_partial_mark_of_a_file_that_fits_is_shown_and_then_passes(space):
    ws = await space()
    put(ws, "ten.txt", numbered(10, 8))
    ws.read("ten.txt", offset=2, limit=2, actor=ANNA)
    text = await refused(ws.write(ANNA, "ten.txt", "x\n"))
    assert "has lines you have not read since it last changed (1, 4-10)" in text
    assert text.endswith("[ten.txt: lines 1-10 of 10]\n" + numbered(10, 8).rstrip("\n"))
    assert "Updated ten.txt" in await ws.write(ANNA, "ten.txt", "x\n")


async def test_the_missing_ranges_are_named_up_to_ten(space):
    ws = await space(chars=CHARS)
    put(ws, "many.txt", numbered(120))
    for offset in range(2, 120, 4):
        ws.read("many.txt", offset=offset, limit=1, actor=ANNA)
    text = await refused(ws.write(ANNA, "many.txt", "x\n"))
    assert "(1, 3-5, 7-9, 11-13, 15-17, 19-21, 23-25, 27-29, 31-33, 35-37, …)" in text


async def test_a_line_fit_cut_counts_as_seen(space):
    ws = await space(chars=500)
    put(ws, "one.txt", "z" * 3000)
    assert "line 1 was cut" in ws.read("one.txt", actor=ANNA)
    assert mark(ws, ANNA, "one.txt") == file_digest(b"z" * 3000)   # its one line counts as seen: whole
    assert "Updated one.txt" in await ws.write(ANNA, "one.txt", "short")
    put(ws, "two.txt", "z" * 3000 + "\nend\n")
    ws.read("two.txt", actor=ANNA)
    assert mark(ws, ANNA, "two.txt").endswith(":1")


async def test_what_needs_no_read(space):
    ws = await space()
    assert (await ws.write(ANNA, "new.md", "x\n")).startswith("Created new.md")   # a new file
    put(ws, "empty.md", b"")
    assert (await ws.write(ANNA, "empty.md", "x\n")).startswith("Updated empty.md")   # 0 bytes: nothing to lose
    put(ws, "gone.md", "a\n")
    ws.read("gone.md", actor=BORIS)
    (ws.root / "gone.md").unlink()
    assert (await ws.write(BORIS, "gone.md", "b\n")).startswith("Created gone.md")   # deleted since the read
    own = scratch_rel(ANNA) + "log.txt"
    put(ws, own, "job output\n")
    assert "Updated" in await ws.write(ANNA, own, "mine\n")   # its own scratch folder records no marks
    assert "Edited" in await ws.edit(ANNA, own, "mine", "ours")
    ws.read(own, actor=ANNA)
    assert mark(ws, ANNA, own) is None
    text = await refused(ws.write(BORIS, own, "theirs\n"))   # another member's scratch follows the rule
    assert "exists, and you have not read it" in text


async def test_a_file_too_long_to_show_gives_the_text_of_its_tool_and_no_mark(space):
    ws = await space(chars=CHARS)
    put(ws, "big.txt", numbered(120))
    text = await refused(ws.write(ANNA, "big.txt", "x\n"))
    assert text.endswith(UNREAD_FILE_TEXTS["en"]["long_write"].format(lines=120))
    assert mark(ws, ANNA, "big.txt") is None
    text = await refused(ws.edit(ANNA, "big.txt", "line 010 ", "line 01O "))
    assert text == (head("big.txt", "exists, and you have not read it", "3.6 KB") + " "
                    + UNREAD_FILE_TEXTS["en"]["long_edit"].format(lines=120))
    assert mark(ws, ANNA, "big.txt") is None and len(text) < CHARS
    ws.read("big.txt", limit=40, actor=ANNA)
    assert "Edited big.txt" in await ws.edit(ANNA, "big.txt", "line 010 ", "line 01O ")
    for offset in (1, 41, 81):
        ws.read("big.txt", offset=offset, limit=40, actor=ANNA)
    assert "Updated big.txt" in await ws.write(ANNA, "big.txt", "x\n")


async def test_the_shown_content_stays_within_the_limit(space):
    """The content is shown when it fits max_chars less the refusal's own lines and REFUSAL_RESERVE."""
    ws = await space(chars=CHARS)

    def fits(size: int) -> bool:
        intro = head("f.txt", "exists, and you have not read it", format_size(size)) + " " + UNREAD_FILE_TEXTS[
            "en"]["fits"]
        return len(intro) + 1 + len("[f.txt: lines 1-1 of 1]") + 1 + size <= CHARS - REFUSAL_RESERVE

    largest = max(size for size in range(1, CHARS) if fits(size))
    for size, shown in ((largest, True), (largest + 1, False)):
        put(ws, "f.txt", "y" * size)
        text = await refused(ws.write(ANNA, "f.txt", "x"))
        assert (UNREAD_FILE_TEXTS["en"]["fits"] in text) is shown, size
        assert len(text) <= CHARS - REFUSAL_RESERVE
        assert (mark(ws, ANNA, "f.txt") is not None) is shown
        ws.swarm.db.execute("DELETE FROM kv WHERE key LIKE 'file_read:%'")


async def test_the_ignored_arguments_note_never_gets_a_marked_page_cut(space):
    """An LLM agent's runtime cuts each result to tool_output_chars in its middle: the note on unknown arguments
    shrinks so a page `read_file` marked as shown reaches the model whole."""
    ws = await space(chars=CHARS)
    put(ws, "big.txt", numbered(120))
    runtime = SimpleNamespace()   # an agent's runtime: a runtime with no frozen tool set of its own
    seen: set[int] = set()
    offset, notes = 1, []
    while offset <= 120:
        text, is_error = await call_tool(ToolContext(ws.swarm, ANNA, runtime), "read_file",
                                         {"path": "big.txt", "offset": offset, "start_line": 1, "end_line": 400})
        assert not is_error, text
        assert len(text) <= CHARS and truncate_middle(text, CHARS) == text   # the runtime cuts nothing
        notes.append(text[text.rindex("\n[Ignored"):] if "\n[Ignored" in text else "")
        seen |= {int(n) for n in re.findall(r"^line (\d{3}) ", text, re.M)}
        offset = int(re.search(r"lines \d+-(\d+) of 120", text).group(1)) + 1
    assert seen == set(range(1, 121))
    assert "\n[Ignored unknown argument(s): start_line, end_line]" in notes   # a full page: the short note
    assert mark(ws, ANNA, "big.txt") == file_digest((ws.root / "big.txt").read_bytes())
    assert "Updated big.txt" in await ws.write(ANNA, "big.txt", "short\n")
    # a caller with no runtime receives the result whole: the full note, whatever the length
    put(ws, "big.txt", numbered(120))
    text, _ = await call_tool(ToolContext(ws.swarm, ANNA), "read_file", {"path": "big.txt", "start_line": 1})
    assert text.endswith("]") and "[Ignored unknown argument(s): start_line. Usage: read_file(" in text


async def test_a_binary_file(space):
    ws = await space()
    put(ws, "b.bin", b"\x00\x01data")
    text = await refused(ws.write(ANNA, "b.bin", "text"))
    assert text == head("b.bin", "exists, and you have not read it", "6 B") + " " + UNREAD_FILE_TEXTS["en"]["binary"]
    assert mark(ws, ANNA, "b.bin") == file_digest(b"\x00\x01data")
    assert "Updated b.bin" in await ws.write(ANNA, "b.bin", "text")
    # edit_file of an unread binary file: _apply_edit's own refusal, never a read refusal (nothing would help)
    put(ws, "c.bin", b"\x00\x01data")
    with pytest.raises(ToolError) as err:
        await ws.edit(ANNA, "c.bin", "data", "atad")
    assert not isinstance(err.value, ReadRefusal)
    assert str(err.value) == "'c.bin' is a binary file; it cannot be edited as text."
    assert mark(ws, ANNA, "c.bin") is None


# ----------------------------------------------------------------------------------------- what moves a mark
async def test_own_writes_and_edits_move_the_mark(space):
    ws = await space()
    put(ws, "a.md", "one\ntwo\n")
    assert await ws.write(ANNA, "a.md", "one\ntwo\n") == "a.md unchanged (same content)."
    assert mark(ws, ANNA, "a.md") == file_digest(b"one\ntwo\n")   # the member's content is the file's
    await ws.edit(ANNA, "a.md", "two", "three")
    assert mark(ws, ANNA, "a.md") == file_digest(b"one\nthree\n")   # a whole mark stays whole
    assert "Updated a.md" in await ws.write(ANNA, "a.md", "four\n")
    assert mark(ws, ANNA, "a.md") == file_digest(b"four\n")
    # an edit that changes nothing writes nothing and moves nothing
    put(ws, "b.md", "same\n")
    ws.read("b.md", actor=BORIS)
    assert "unchanged" in await ws.edit(BORIS, "b.md", "same", "same")
    assert mark(ws, BORIS, "b.md") == file_digest(b"same\n")


async def test_a_cancelled_write_moves_nothing(space, monkeypatch):
    ws = await space()
    gate = asyncio.Event()
    original = ws._store

    async def slow(p, rel, data):
        await gate.wait()
        await original(p, rel, data)

    monkeypatch.setattr(ws, "_store", slow)
    task = asyncio.ensure_future(ws.write(ANNA, "late.md", "x\n"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    for _ in range(100):
        if (ws.root / "late.md").exists():
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.05)
    assert (ws.root / "late.md").exists() and mark(ws, ANNA, "late.md") is None


async def test_a_write_that_ends_after_the_members_retire_records_no_row(space, monkeypatch):
    ws = await space()
    gate = asyncio.Event()
    original = ws._store

    async def slow(p, rel, data):
        await gate.wait()
        await original(p, rel, data)

    monkeypatch.setattr(ws, "_store", slow)
    task = asyncio.ensure_future(ws.write(BORIS, "leaving.md", "x\n"))
    await asyncio.sleep(0.05)
    ws.swarm.db.execute("UPDATE agents SET status = ? WHERE name = ?", (AgentStatus.RETIRED, BORIS))
    ws.drop_file_reads(BORIS)
    gate.set()
    await task
    assert mark_rows(ws.swarm) == []


async def test_a_non_member_is_never_checked_and_records_nothing(space):
    ws = await space()
    put(ws, "a.md", "a\n")
    for actor in ("Nobody Here", USER):
        ws.read("a.md", actor=actor)
        await ws.write(actor, "a.md", f"{actor}\n")
        await ws.edit(actor, "a.md", actor, actor + "!")
    assert mark_rows(ws.swarm) == []


async def test_seen_as_members_mark_too_and_a_retired_name_never(space):
    ws = await space()
    put(ws, "a.md", "a\n")
    with sent_as_seen_by({ANNA, BORIS}, ANNA):   # a client acting as both members
        ws.read("a.md", actor=ANNA)
    assert mark(ws, ANNA, "a.md") == mark(ws, BORIS, "a.md") == file_digest(b"a\n")
    put(ws, "b.md", "b\n")
    with sent_as_seen_by({ANNA}, USER):   # it reads as the user and listens as its member
        ws.read("b.md", actor=USER)
    assert mark(ws, ANNA, "b.md") == file_digest(b"b\n") and mark(ws, USER, "b.md") is None
    ws.swarm.db.execute("UPDATE agents SET status = ? WHERE name = ?", (AgentStatus.RETIRED, BORIS))
    ws.drop_file_reads(BORIS)
    put(ws, "c.md", "c\n")
    with sent_as_seen_by({ANNA, BORIS}, ANNA):
        ws.read("c.md", actor=ANNA)
        await ws.write(ANNA, "c.md", "cc\n")
    with sent_as_seen_by({ANNA, BORIS}, USER):
        ws.read("b.md", actor=USER)
    assert [k for k in mark_rows(ws.swarm) if BORIS in k] == []


# ------------------------------------------------------------------------------------------------ the check
async def test_a_lock_refusal_comes_first_and_a_read_refusal_records_no_waiter(space):
    ws = await space()
    put(ws, "plan.md", "v1\n")
    ws.lock(ANNA, "plan.md")
    with pytest.raises(ToolError) as err:
        await ws.write(BORIS, "plan.md", "mine\n")
    assert not isinstance(err.value, ReadRefusal) and "locked by Anna Smith" in str(err.value)
    assert ws.lock_waits(BORIS) == ["plan.md"]
    ws.unlock(ANNA, "plan.md")
    text = await refused(ws.write(BORIS, "plan.md", "mine\n"))   # the lock check passed: the waiter row goes
    assert "exists, and you have not read it" in text
    assert ws.lock_waits(BORIS) == [] and not ws.waits_for_lock(BORIS)
    put(ws, "other.md", "o\n")
    await refused(ws.edit(BORIS, "other.md", "o", "p"))
    assert not ws.waits_for_lock(BORIS)


async def test_two_concurrent_writes_after_the_same_read_give_one_write(space):
    ws = await space()
    put(ws, "shared.md", "base\n")
    ws.read("shared.md", actor=ANNA)
    ws.read("shared.md", actor=BORIS)
    outcomes = await asyncio.gather(ws.write(ANNA, "shared.md", "anna\n"), ws.write(BORIS, "shared.md", "boris\n"),
                                    return_exceptions=True)
    assert [type(o) for o in outcomes] == [str, ReadRefusal]
    assert (ws.root / "shared.md").read_text(encoding="utf-8") == "anna\n"
    assert str(outcomes[1]).endswith("[shared.md: lines 1-1 of 1]\nanna")


@needs_git
async def test_a_refused_write_emits_nothing_and_commits_nothing(space):
    ws = await space(git=True)
    put(ws, "a.md", "a\n")
    await ws.autocommit(USER, "seed")
    commits = subprocess.run(["git", "-C", str(ws.root), "rev-list", "--count", "HEAD"], capture_output=True,
                             text=True, check=True).stdout
    events = len(ws.swarm.bus.recent(types=("file.*",), limit=1000))
    await refused(ws.write(ANNA, "a.md", "b\n"))
    put(ws, "a.md", "c\n")
    await refused(ws.edit(ANNA, "a.md", "c", "d"))
    assert len(ws.swarm.bus.recent(types=("file.*",), limit=1000)) == events
    assert subprocess.run(["git", "-C", str(ws.root), "rev-list", "--count", "HEAD"], capture_output=True,
                          text=True, check=True).stdout == commits


# ------------------------------------------------------------------------------------------------- storage
async def test_the_marks_are_kv_rows_of_the_documented_format(space):
    ws = await space()
    put(ws, "Docs/Notes.md", numbered(12, 8))
    digest = file_digest((ws.root / "Docs/Notes.md").read_bytes())
    ws.read("Docs/Notes.md", offset=1, limit=3, actor=ANNA)
    ws.read("Docs/Notes.md", offset=7, limit=3, actor=ANNA)
    key = f"file_read:{ANNA}:{path_key('Docs/Notes.md')}"   # case-folded on Windows
    assert key.endswith(":docs/notes.md" if sys.platform == "win32" else ":Docs/Notes.md")
    row = ws.swarm.db.one("SELECT value FROM kv WHERE key = ?", (key,))
    assert json.loads(row["value"]) == f"{digest}:1-3,7-9"   # a JSON string (Database.set)
    ws.read("Docs/Notes.md", offset=4, limit=3, actor=ANNA)
    assert ws.swarm.db.get(key) == f"{digest}:1-9"
    ws.read("Docs/Notes.md", offset=10, actor=ANNA)
    assert ws.swarm.db.get(key) == digest   # every line seen: whole
    ws.read("Docs/Notes.md", offset=2, limit=1, actor=ANNA)
    assert ws.swarm.db.get(key) == digest   # a whole mark stays whole
    put(ws, "Docs/Notes.md", "other\n")
    ws.read("Docs/Notes.md", actor=ANNA)
    assert ws.swarm.db.get(key) == file_digest(b"other\n")   # another hash replaces the mark
    put(ws, "empty.md", "")
    ws.read("empty.md", actor=ANNA)
    assert mark(ws, ANNA, "empty.md") == file_digest(b"")


async def test_the_rule_adds_no_schema(space):
    ws = await space()
    conn = ws.swarm.db.conn
    before = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    put(ws, "a.md", "a\n")
    ws.read("a.md", actor=ANNA)
    await ws.write(ANNA, "a.md", "b\n")
    await refused(ws.write(BORIS, "a.md", "c\n"))
    assert conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == before
    assert len(mark_rows(ws.swarm)) == 2


async def test_drop_file_reads_deletes_exactly_the_members_rows(space):
    ws = await space()
    ws.swarm.add_member("Anna Smith 2", language="en")
    for name in (ANNA, "Anna Smith 2"):
        for rel in ("a.md", "b.md"):
            put(ws, rel, rel)
            ws.read(rel, actor=name)
    assert ws.drop_file_reads(ANNA) == 2
    assert mark_rows(ws.swarm) == [f"file_read:Anna Smith 2:{path_key('a.md')}",
                                   f"file_read:Anna Smith 2:{path_key('b.md')}"]
    assert ws.drop_file_reads(ANNA) == 0


# ------------------------------------------------------------------------------------------------- the swarm
def make_config(tmp_path: Path, mode: str = "board", language: str = "en", **agents: Any) -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Read swarm", "language": language, "start_agents": 1, "max_agents": 4, "max_cost_usd": 0,
                  "mode": mode, "clarify": False, "independent_review": False},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 3600, "max_steps_per_turn": 12, **agents},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    return Config.from_dict(tmp_path / "project", raw)


WAIT = {"name": "wait", "arguments": {}}


def call(name: str, **arguments: Any) -> dict:
    return {"name": name, "arguments": arguments}


class Script:
    """Each LLM call of an agent answers with the next queued response (none queued: just `wait`)."""

    def __init__(self, summary: str | None = None) -> None:
        self.todo: dict[str, list[list[dict]]] = {}
        self.calls: dict[str, list[list[dict]]] = {}
        self.summary = summary

    def factory(self, profile: Any, agent: str) -> ScriptedLLM:
        def handler(system: str, messages: list[dict], tools: list[dict]) -> Any:
            self.calls.setdefault(agent, []).append(json.loads(json.dumps(messages)))
            if self.summary is not None and "replaced by a summary" in str(messages[-1].get("content") or ""):
                return self.summary
            queue = self.todo.get(agent) or []
            return {"tool_calls": queue.pop(0) if queue else [WAIT]}

        return ScriptedLLM(handler, profile)

    def count(self, name: str) -> int:
        return len(self.calls.get(name, []))


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


async def run_until(predicate: Callable[[], bool], timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not reached in time")


async def started(tmp_path: Path, open_swarms: list, mode: str = "board", language: str = "en",
                  run_agents: bool = True, script: Script | None = None, **agents: Any) -> tuple[Swarm, Script, str]:
    script = script or Script()
    sw = Swarm(config=make_config(tmp_path, mode, language, **agents), llm_factory=script.factory)
    open_swarms.append(sw)
    await sw.start(run_agents=run_agents)
    return sw, script, sw.members()[0]["name"]


def results(sw: Swarm, name: str, tool: str) -> list[dict]:
    rows = sw.db.all("SELECT msg FROM history WHERE agent = ? ORDER BY seq", (name,))
    return [m for m in (json.loads(r["msg"]) for r in rows) if m.get("role") == "tool" and m.get("name") == tool]


async def tool(sw: Swarm, actor: str, name: str, args: dict, error: bool = False) -> str:
    text, is_error = await call_tool(ToolContext(sw, actor), name, args)
    assert is_error is error, text
    return text


async def poke(sw: Swarm, name: str) -> None:
    await tool(sw, USER, "send_message", {"to": [name], "text": "Go on."})


async def settled(script: Script, name: str, n: int) -> None:
    await run_until(lambda: script.count(name) >= n)
    await asyncio.sleep(0.3)
    assert script.count(name) == n, script.count(name)


async def quiet(script: Script, name: str) -> None:
    """The agent made no LLM call for a moment (its activation ended)."""
    while True:
        seen = script.count(name)
        await asyncio.sleep(0.4)
        if script.count(name) == seen:
            return


def seed(sw: Swarm, rel: str = "notes.md", text: str = "first draft\n") -> None:
    (sw.workspace.root / rel).write_bytes(text.encode("utf-8"))


@pytest.mark.parametrize("swarm_mode", ["board", "thread"])
@pytest.mark.parametrize("language", ["en", "ru"])
async def test_a_refused_write_then_wait_keeps_the_activation_running(tmp_path, open_swarms, swarm_mode, language):
    sw, script, a = await started(tmp_path, open_swarms, swarm_mode, language)
    try:
        await settled(script, a, 0)
        seed(sw)
        script.todo[a] = [[call("write_file", path="notes.md", content="mine\n"), WAIT],
                          [call("write_file", path="notes.md", content="mine\n"), WAIT]]
        await poke(sw, a)
        await settled(script, a, 2)
        writes, waits = results(sw, a, "write_file"), results(sw, a, "wait")
        assert writes[0]["is_error"] and writes[0]["content"].startswith(
            "Error: " + UNREAD_FILE_TEXTS[language]["head"].split("'")[0])
        assert waits[0]["is_error"] and waits[0]["content"] == "Error: " + prompts.t(language, "wait_refused",
                                                                                      tool="write_file")
        assert not writes[1]["is_error"] and not waits[1]["is_error"]
        assert (sw.workspace.root / "notes.md").read_text(encoding="utf-8") == "mine\n"
    finally:
        await sw.stop()


async def test_a_wait_after_a_lock_refusal_ends_the_turn(tmp_path, open_swarms):
    sw, script, a = await started(tmp_path, open_swarms)
    try:
        await sw.hire(USER, "Generalist", name="Boris Lee", kind="external")
        seed(sw)
        await tool(sw, "Boris Lee", "lock_file", {"path": "notes.md"})
        script.todo[a] = [[call("write_file", path="notes.md", content="x"), WAIT]]
        await poke(sw, a)
        await settled(script, a, 1)
        assert "locked by Boris Lee" in results(sw, a, "write_file")[0]["content"]
        assert not results(sw, a, "wait")[0]["is_error"]
    finally:
        await sw.stop()


async def test_a_refused_edit_holds_task_complete(tmp_path, open_swarms):
    sw, script, a = await started(tmp_path, open_swarms)
    try:
        seed(sw)
        task = sw.board.create(USER, "Fix the notes", goal="notes.md fixed")
        sw.board.claim(a, task.id)
        edit = call("edit_file", path="notes.md", old_text="first", new_text="second")
        script.todo[a] = [[edit, call("task_complete", task_id=task.id, result="done"), WAIT]]
        await poke(sw, a)
        await run_until(lambda: len(results(sw, a, "task_complete")) >= 1)
        await quiet(script, a)
        first = results(sw, a, "task_complete")[0]
        assert first["is_error"] and first["content"] == "Error: " + prompts.t("en", "complete_refused",
                                                                               tool="edit_file")
        assert sw.board.get(task.id).status == Status.IN_PROGRESS   # the board was not touched
        # the refusal showed the file: the next response's edit passes, and task_complete with it
        script.todo[a] = [[edit, call("task_complete", task_id=task.id, result="done"), WAIT]]
        await poke(sw, a)
        await run_until(lambda: len(results(sw, a, "task_complete")) >= 2)
        assert not results(sw, a, "edit_file")[-1]["is_error"] and not results(sw, a, "task_complete")[1]["is_error"]
        assert sw.board.get(task.id).status == Status.DONE
    finally:
        await sw.stop()


async def test_a_refused_edit_holds_goal_complete(tmp_path, open_swarms):
    sw, script, a = await started(tmp_path, open_swarms)
    try:
        seed(sw)
        edit = call("edit_file", path="notes.md", old_text="first", new_text="second")
        script.todo[a] = [[edit, call("goal_complete", summary="All done."), WAIT]]
        sw.set_goal(USER, "Fix the notes")
        await run_until(lambda: len(results(sw, a, "goal_complete")) >= 1)
        await quiet(script, a)
        first = results(sw, a, "goal_complete")[0]
        assert first["is_error"] and first["content"] == "Error: " + prompts.t("en", "goal_complete_refused",
                                                                               tool="edit_file")
        assert sw.goal()["status"] == "active"
        script.todo[a] = [[edit, call("goal_complete", summary="All done."), WAIT]]
        await poke(sw, a)
        await run_until(lambda: len(results(sw, a, "goal_complete")) >= 2)
        assert not results(sw, a, "goal_complete")[1]["is_error"]
        assert sw.goal()["status"] == "done"
        assert (sw.workspace.root / "notes.md").read_text(encoding="utf-8") == "second draft\n"
    finally:
        await sw.stop()


async def test_a_refused_write_holds_a_like_on_1_but_not_a_dislike(tmp_path, open_swarms):
    sw, script, a = await started(tmp_path, open_swarms, mode="thread")
    try:
        seed(sw)
        write = call("write_file", path="notes.md", content="mine\n")
        script.todo[a] = [[write, call("vote", target="#1", value="like"), WAIT]]
        sw.set_goal(USER, "Fix the notes")
        await run_until(lambda: len(results(sw, a, "vote")) >= 1)
        await quiet(script, a)
        vote = results(sw, a, "vote")[0]
        assert vote["is_error"] and vote["content"] == "Error: " + prompts.t("en", "vote_refused", tool="write_file")
        assert sw.db.scalar("SELECT COUNT(*) FROM votes WHERE target = 'p1'") == 0
        assert sw.goal()["status"] == "active"
        seed(sw, text="changed again\n")
        script.todo[a] = [[write, call("vote", target="#1", value="dislike"), WAIT]]
        await poke(sw, a)
        await run_until(lambda: len(results(sw, a, "vote")) >= 2)
        assert not results(sw, a, "vote")[1]["is_error"]
        assert sw.db.scalar("SELECT value FROM votes WHERE target = 'p1' AND voter = ?", (a,)) == -1
    finally:
        await sw.stop()


async def test_a_shell_change_is_found_by_content(tmp_path, open_swarms):
    sw, _script, a = await started(tmp_path, open_swarms, run_agents=False)
    try:
        seed(sw)
        await tool(sw, a, "read_file", {"path": "notes.md"})
        out = await tool(sw, a, "shell", {"command": "echo changed > notes.md"})
        assert (sw.workspace.root / "notes.md").read_bytes() != b"first draft\n", out
        text = await tool(sw, a, "write_file", {"path": "notes.md", "content": "mine\n"}, error=True)
        assert "changed since you last read or wrote it" in text
        await tool(sw, a, "write_file", {"path": "notes.md", "content": "mine\n"})
    finally:
        await sw.stop()


async def test_a_thread_session_keeps_its_file_marks_in_kv(tmp_path, open_swarms):
    sw, _script, a = await started(tmp_path, open_swarms, mode="thread", run_agents=False)
    try:
        seed(sw)
        await tool(sw, a, "read_file", {"path": "notes.md"})
        assert sw.db.get(f"file_read:{a}:{path_key('notes.md')}") == file_digest(b"first draft\n")
        assert sw.db.scalar("SELECT COUNT(*) FROM read_marks") == 0
    finally:
        await sw.stop()


async def test_a_board_database_gets_no_table_from_the_rule(tmp_path, open_swarms):
    sw, _script, a = await started(tmp_path, open_swarms, run_agents=False)
    try:
        seed(sw)
        await tool(sw, a, "read_file", {"path": "notes.md"})
        await tool(sw, a, "write_file", {"path": "notes.md", "content": "x\n"})
        tables = {r[0] for r in sw.db.all("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert not tables & {"posts", "read_marks", "file_reads"}
        assert not db_module.unknown_columns(sw.db) and not db_module.foreign_objects(sw.db)
    finally:
        await sw.stop()


async def test_a_restart_a_compaction_and_a_retire(tmp_path, open_swarms):
    sw, _script, a = await started(tmp_path, open_swarms, run_agents=False)
    seed(sw)
    await tool(sw, a, "read_file", {"path": "notes.md"})
    key = f"file_read:{a}:{path_key('notes.md')}"
    await sw.stop()
    sw.close()
    # a restart keeps the marks
    summary = Script(summary="SUMMARY of older work")
    sw2 = Swarm(config=make_config(tmp_path, context_tokens=20000), llm_factory=summary.factory)
    open_swarms.append(sw2)
    await sw2.start(run_agents=False)
    try:
        assert sw2.db.get(key) == file_digest(b"first draft\n")
        # a compaction keeps them: seed a long history, the next activation compacts first
        seq = int(sw2.db.scalar("SELECT COALESCE(MAX(seq), -1) FROM history WHERE agent = ?", (a,))) + 1
        for i in range(20):
            for msg in ({"role": "user", "content": f"wake {i} " + "w" * 4000},
                        {"role": "assistant", "content": f"working {i}", "raw": None,
                         "tool_calls": [{"id": f"seed_{i}", "name": "board", "arguments": {}}]},
                        {"role": "tool", "tool_call_id": f"seed_{i}", "name": "board", "content": "r" * 4000,
                         "is_error": False},
                        {"role": "assistant", "content": f"done {i}", "raw": None, "tool_calls": []}):
                sw2.db.execute("INSERT INTO history(agent, seq, msg) VALUES (?, ?, ?)", (a, seq, json.dumps(msg)))
                seq += 1
        await sw2.stop()
        await sw2.start()
        await poke(sw2, a)
        await run_until(lambda: summary.count(a) >= 2)
        assert any("replaced by a summary" in str(m[-1].get("content") or "") for m in summary.calls[a])
        assert sw2.db.get(key) == file_digest(b"first draft\n")
        await tool(sw2, a, "write_file", {"path": "notes.md", "content": "after\n"})
        # retire deletes exactly its rows
        await sw2.hire(USER, "Generalist", name="Zoe Park", kind="external")
        await tool(sw2, "Zoe Park", "read_file", {"path": "notes.md"})
        await sw2.retire(USER, a)
        assert [r["key"] for r in sw2.db.all("SELECT key FROM kv WHERE key LIKE 'file_read:%'")] == [
            f"file_read:Zoe Park:{path_key('notes.md')}"]
    finally:
        await sw2.stop()


# ------------------------------------------------------------------------------------------------------- MCP
async def test_external_members_through_the_hub(tmp_path, open_swarms):
    sw, _script, a = await started(tmp_path, open_swarms, run_agents=False)
    try:
        seed(sw)
        for name in ("Ann Lee", "Bob Stone"):
            text, is_error = await hub_call(sw, "join_swarm", {"role": "Researcher", "name": name}, client="client:a")
            assert not is_error, text
        ann = f"file_read:Ann Lee:{path_key('notes.md')}"
        bob = f"file_read:Bob Stone:{path_key('notes.md')}"
        # an external member is refused and marks like an AI agent; the client acting as both marks for both
        text, is_error = await hub_call(sw, "write_file", {"path": "notes.md", "content": "x\n", "as_agent": "Ann Lee"},
                                        client="client:a")
        assert is_error and "exists, and you have not read it" in text and "first draft" in text
        assert sw.db.get(ann) == sw.db.get(bob) == file_digest(b"first draft\n")
        text, is_error = await hub_call(sw, "write_file", {"path": "notes.md", "content": "x\n", "as_agent": "Ann Lee"},
                                        client="client:a")
        assert not is_error, text
        assert sw.db.get(ann) == sw.db.get(bob) == file_digest(b"x\n")
        # the user's read_file through the client marks for its members
        seed(sw, text="by hand\n")
        text, is_error = await hub_call(sw, "read_file", {"path": "notes.md"}, client="client:a")
        assert not is_error and sw.db.get(ann) == sw.db.get(bob) == file_digest(b"by hand\n")
        assert sw.db.get(f"file_read:{USER}:{path_key('notes.md')}") is None
        # a welcome back keeps the marks
        text, is_error = await hub_call(sw, "join_swarm", {"role": "Researcher", "name": "Ann Lee"}, client="client:b")
        assert not is_error and "Welcome back" in text and sw.db.get(ann) == file_digest(b"by hand\n")
        # a member the client acted as and that has since retired gets no row
        await sw.retire(USER, "Bob Stone")
        assert sw.db.get(bob) is None
        seed(sw, text="again\n")
        await hub_call(sw, "read_file", {"path": "notes.md", "as_agent": "Ann Lee"}, client="client:a")
        await hub_call(sw, "read_file", {"path": "notes.md"}, client="client:a")
        assert sw.db.get(bob) is None and sw.db.get(ann) == file_digest(b"again\n")
    finally:
        await sw.stop()


# ----------------------------------------------------------------------------------------- export and import
async def test_file_marks_travel_with_an_export(tmp_path):
    from test_transfer import _db, _fill_db, _home_named, _import, _manager, _raw, _raw_b, _session
    from test_transfer_thread import _fill_forum, _known_to_041, _schema_of, _thread_overrides

    from dreamteam.transfer import export_home

    home_a = _home_named(tmp_path, "a", _raw())
    manager = _manager(home_a)
    board = await _session(manager, "Board")
    thread = await _session(manager, "Forum", overrides=_thread_overrides())
    await manager.shutdown()
    _fill_db(home_a, board)
    _fill_forum(home_a, thread)
    marks = {"file_read:alice:src/app.py": "ab" * 32, "file_read:alice:docs/notes.md": "cd" * 32 + ":1-40,81-120"}
    for sid in (board, thread):
        db = _db(home_a, sid)
        try:
            for key, value in marks.items():
                db.set(key, value)
        finally:
            db.close()
    dest = tmp_path / "usb"
    assert export_home(home_a, dest, include=["sessions"]).ok
    copied = dest / "sessions" / board / ".dreamteam" / "swarm.db"
    columns, objects = _schema_of(copied)
    known, known_objects = _known_to_041()
    assert columns <= known and objects <= known_objects   # 0.4.x still imports the board session

    def kv_marks(path: Path) -> dict[str, str]:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?immutable=1", uri=True)
        try:
            return {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM kv WHERE key LIKE 'file_read:%'")}
        finally:
            conn.close()

    assert kv_marks(copied) == marks
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager_b, _plan, result = await _import(home_b, dest)
    try:
        assert result.ok
        for sid in (board, thread):
            assert kv_marks(home_b / "sessions" / sid / ".dreamteam" / "swarm.db") == marks, sid
    finally:
        await manager_b.shutdown()


# --------------------------------------------------------------------------------------------------- texts
def test_the_refusal_texts_match_in_both_languages():
    en, ru = UNREAD_FILE_TEXTS["en"], UNREAD_FILE_TEXTS["ru"]
    assert set(en) == set(ru)
    tools = {mode: {spec["name"] for spec in frozen.current_record("en", mode)["tools"]}
             for mode in ("board", "thread")}
    for key in en:
        assert set(re.findall(r"\{(\w+)\}", en[key])) == set(re.findall(r"\{(\w+)\}", ru[key])), key
        ticks = re.findall(r"`([^`]+)`", en[key])
        assert ticks == re.findall(r"`([^`]+)`", ru[key]), key
        assert all(name in tools["board"] and name in tools["thread"] for name in ticks), key
        assert chr(0x451) not in ru[key] and chr(0x401) not in ru[key], key   # the dotted letter, either case
