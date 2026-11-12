"""Crew tasks through the tool layer (docs/SPEC.md §4.2 "Texts for agents", §12): the board's notes in the results
of task_create, task_claim, task_update, task_complete and task_review, the directions a call made, the task_get
card of a parent, the board listing of subtasks, and the arguments as the registry passes them on."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dreamteam import prompts
from dreamteam.agent import AgentRuntime
from dreamteam.config import Config
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentKind, Status
from dreamteam.swarm import Swarm
from dreamteam.tools import ToolContext, call_tool, frozen, schemas_for
from helpers.crew import finish_raw, set_raw

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

CLEARED_CREW = ("You were its reviewer; since you execute it now, you do not check it as a whole: another member "
                "does, if a check applies.")


def make_config(tmp_path: Path) -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Crew tools", "language": "en", "start_agents": 4, "max_agents": 8, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "max_steps_per_turn": 10},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
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


def members(sw: Swarm) -> list[str]:
    return [m["name"] for m in sw.members() if m["kind"] == AgentKind.LLM]


async def ok(sw: Swarm, actor: str, tool: str, args: Any = None, runtime: Any = None) -> str:
    text, is_error = await call_tool(ToolContext(sw, actor, runtime), tool, args)
    assert not is_error, text
    return text


async def err(sw: Swarm, actor: str, tool: str, args: Any = None) -> str:
    text, is_error = await call_tool(ToolContext(sw, actor), tool, args)
    assert is_error, text
    return text


def spy_notes(monkeypatch, board, name: str) -> list[list[str]]:
    """Record the `notes` list each call of board.<name> filled (what the board wrote for the actor's result)."""
    seen: list[list[str]] = []
    original = getattr(board, name)

    def wrapper(*args, notes=None, **kw):
        task = original(*args, notes=notes, **kw)
        seen.append(list(notes or []))
        return task

    monkeypatch.setattr(board, name, wrapper)
    return seen


def told(sw: Swarm, recipient: str, kind: str) -> list[str]:
    """The texts of the `kind` notifications `recipient` got, oldest first."""
    rows = sw.db.all("SELECT text FROM notifications WHERE recipient = ? AND kind = ? ORDER BY id", (recipient, kind))
    return [r["text"] for r in rows]


def joined(notes: list[str]) -> str:
    """The notes as a result shows them: on a line of their own, as the board wrote them."""
    return "".join(notes).lstrip()


# --- the notes channel -------------------------------------------------------------------------------------------

async def test_the_five_handlers_append_the_boards_notes_unchanged_and_in_order(swarm, monkeypatch):
    a, *_ = members(swarm)
    board = swarm.board
    for name in ("create", "claim", "update", "complete", "review"):
        original = getattr(board, name)

        def wrapper(*args, notes=None, _name=name, _original=original, **kw):
            task = _original(*args, notes=notes, **kw)
            notes.extend([f" {_name} one.", f" {_name} two\nlines."])
            return task

        monkeypatch.setattr(board, name, wrapper)
    text = await ok(swarm, a, "task_create", {"title": "Build the parser", "goal": "parse"})
    assert text.endswith("\ncreate one. create two\nlines.")
    text = await ok(swarm, a, "task_claim", {"task_id": 1})
    assert text.endswith("\nclaim one. claim two\nlines.")
    text = await ok(swarm, a, "task_update", {"task_id": 1, "priority": 2})
    assert text.endswith("\nupdate one. update two\nlines.")
    text = await ok(swarm, a, "task_complete", {"task_id": 1, "result": "parsed"})
    reviewer = swarm.board.get(1).reviewer
    assert text.startswith(f"Task #1 was sent to {reviewer} for review")
    assert text.endswith("\ncomplete one. complete two\nlines.")
    text = await ok(swarm, reviewer, "task_review", {"task_id": 1, "approve": True})
    assert text == f"Task #1 is now done (assignee: {a}).\nreview one. review two\nlines."


async def test_a_call_without_notes_keeps_its_result(swarm):
    a, *_ = members(swarm)
    assert await ok(swarm, a, "task_create", {"title": "Plain", "goal": "g"}) == "Created task #1 [todo] Plain"
    assert await ok(swarm, a, "task_claim", {"task_id": 1}) == f"You now work on #1 [in_progress] Plain @{a}\n    goal: g"


# --- task_create ----------------------------------------------------------------------------------------------

async def test_task_create_names_the_directions_then_the_own_part(swarm, monkeypatch):
    a, *_ = members(swarm)
    seen = spy_notes(monkeypatch, swarm.board, "create")
    text = await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "assignee": "me", "crew": 3,
                                              "directions": ["oauth2", "pkce", "oidc"]})
    own = prompts.t("en", "note_own_part", sub=2, title="oauth2", id=1)
    assert seen == [[own]]
    assert text == (f"Created task #1 [todo] Auth @{a} crew 1/3\n"
                    'Directions: #2 "oauth2", #3 "pkce", #4 "oidc".\n' + own.lstrip())
    sub = swarm.board.get(2)
    assert sub.parent_id == 1 and sub.assignee == a and sub.status == Status.IN_PROGRESS and sub.goal == ""
    assert swarm.board.get(3).assignee is None


async def test_task_create_of_a_blocked_crew_task_gives_the_blocked_own_part(swarm):
    a, *_ = members(swarm)
    await ok(swarm, a, "task_create", {"title": "Schema", "goal": "tables"})
    text = await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "assignee": "me", "crew": 2,
                                              "depends_on": [1], "directions": ["parser", "cli"]})
    assert 'Directions: #3 "parser", #4 "cli".\n#3 "parser" is your own part; #2 is blocked by #1' in text
    assert "claim #3 when #2 is no longer blocked (task_claim)." in text
    assert swarm.board.get(3).status == Status.TODO and swarm.board.get(3).assignee == a


async def test_task_create_lists_the_directions_before_the_similar_tasks(swarm):
    a, b, *_ = members(swarm)
    await ok(swarm, b, "task_create", {"title": "Build the login page", "goal": "users log in with a password"})
    text = await ok(swarm, a, "task_create", {"title": "Build the login page", "goal": "users log in with a password",
                                              "crew": 2, "directions": ["form", "session"]})
    lines = text.split("\n")
    assert lines[0] == "Created task #2 [todo] Build the login page crew 0/2"
    assert lines[1] == 'Directions: #3 "form", #4 "session".'
    assert lines[2].startswith("Similar open tasks: #1")


async def test_crew_zero_and_no_directions_make_an_ordinary_task(swarm):
    a, *_ = members(swarm)
    assert await ok(swarm, a, "task_create", {"title": "One", "goal": "g", "crew": 0}) == "Created task #1 [todo] One"
    assert swarm.board.get(1).crew == 0
    text = await ok(swarm, a, "task_create", {"title": "Two", "goal": "g", "crew": 2})
    assert text == "Created task #2 [todo] Two crew 0/2"  # a crew task with no directions yet: no Directions line


async def test_empty_directions_reach_the_board(swarm):
    a, *_ = members(swarm)
    error = 'directions must be a list of 1..20 direction titles, e.g. ["parser", "cli"]'
    assert error in await err(swarm, a, "task_create", {"title": "T", "goal": "g", "crew": 2, "directions": []})
    assert error in await err(swarm, a, "task_create", {"title": "T", "goal": "g", "crew": 2, "directions": ""})
    await ok(swarm, a, "task_create", {"title": "T", "goal": "g", "crew": 2})
    assert error in await err(swarm, a, "task_update", {"task_id": 1, "directions": []})
    assert swarm.board.subtasks_of(1) == []


async def test_empty_directions_on_an_ordinary_call_are_no_value(swarm):
    """A model that sends every optional field empty still creates and changes ordinary tasks: [] with no crew of 2
    or more, on a task that is no crew task, is left out like empty tags and depends_on."""
    a, *_ = members(swarm)
    await ok(swarm, a, "task_create", {"title": "T", "goal": "g", "crew": 0, "directions": [], "tags": []})
    await ok(swarm, a, "task_create", {"title": "U", "goal": "g", "directions": []})
    task = next(t for t in swarm.board.list() if t.title == "U")
    assert task.crew == 0 and swarm.board.subtasks_of(task.id) == []
    await ok(swarm, a, "task_claim", {"task_id": task.id})
    await ok(swarm, a, "task_update", {"task_id": task.id, "status": "waiting", "crew": 0, "directions": []})
    assert swarm.board.get(task.id).status == Status.WAITING


async def test_a_comma_string_of_directions_is_split(swarm):
    a, *_ = members(swarm)
    text = await ok(swarm, a, "task_create", {"title": "T", "goal": "g", "crew": "2", "directions": "parser, cli"})
    assert 'Directions: #2 "parser", #3 "cli".' in text
    assert [s.title for s in swarm.board.subtasks_of(1)] == ["parser", "cli"]


async def test_the_user_passes_crew_as_a_numeric_string(swarm):
    text = await ok(swarm, USER, "task_create", {"title": "Auth", "goal": "log in", "crew": "3",
                                                 "directions": ["oauth2", "pkce"]})
    assert text == 'Created task #1 [todo] Auth crew 0/3\nDirections: #2 "oauth2", #3 "pkce".'
    assert swarm.board.get(1).crew == 3
    text = await ok(swarm, USER, "task_update", {"task_id": 1, "crew": "4", "directions": '["oidc"]'})
    assert text.startswith("Updated task #1: crew 3 → 4; 1 direction(s) added.\n#1 [todo] Auth crew 0/4\n")
    assert 'Directions: #4 "oidc".' in text
    assert "must be an integer" in await err(swarm, USER, "task_update", {"task_id": 1, "crew": "three"})


# --- task_update ----------------------------------------------------------------------------------------------

async def test_task_update_makes_the_actors_task_a_crew_task(swarm, monkeypatch):
    a, *_ = members(swarm)
    await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in"})
    await ok(swarm, a, "task_claim", {"task_id": 1})
    seen = spy_notes(monkeypatch, swarm.board, "update")
    text = await ok(swarm, a, "task_update", {"task_id": 1, "crew": 3, "directions": ["oauth2", "pkce"]})
    assert text.startswith(f"Updated task #1: crew 0 → 3; 2 direction(s) added.\n#1 [in_progress] Auth @{a} crew 1/3\n"
                           'Directions: #2 "oauth2", #3 "pkce".\n')
    notes = seen[0]
    assert notes[0] == prompts.t("en", "note_own_part", sub=2, title="oauth2", id=1)
    assert any(n.startswith(" You own crew task #1") for n in notes[1:])  # the owner sentence
    assert text.endswith("\n" + joined(notes))


async def test_task_update_adds_directions_to_a_crew_task(swarm):
    a, b, *_ = members(swarm)
    await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "crew": 2, "directions": ["oauth2"]})
    text = await ok(swarm, a, "task_update", {"task_id": 1, "directions": ["pkce", "oidc"], "comment": "two more"})
    assert text.startswith("Updated task #1: 2 direction(s) added.\n#1 [todo] Auth crew 0/2\n"
                           'Directions: #3 "pkce", #4 "oidc".\nPosted comment #1 in its discussion.')
    assert [s.title for s in swarm.board.subtasks_of(1)] == ["oauth2", "pkce", "oidc"]


async def test_crew_zero_reaches_the_board_and_shows_in_the_diff(swarm):
    a, *_ = members(swarm)
    await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "crew": 3})
    text = await ok(swarm, a, "task_update", {"task_id": 1, "crew": 0})
    assert text == "Updated task #1: crew 3 → 0.\n#1 [todo] Auth"
    assert swarm.board.get(1).crew == 0


async def test_the_assignee_changing_the_crew_is_asked_to_say_why(swarm):
    a, b, *_ = members(swarm)
    await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "assignee": b})
    await ok(swarm, b, "task_claim", {"task_id": 1})
    text = await ok(swarm, b, "task_update", {"task_id": 1, "crew": 2, "directions": ["parser", "cli"]})
    assert prompts.t("en", "note_crew_say_why", id=1).lstrip() in text
    assert told(swarm, a, "n_crew_changed") == [prompts.t("en", "n_crew_changed", by=b, id=1, title="Auth", crew=2)]


async def test_task_update_taking_a_direction_gets_the_card_and_the_reviewer_note(swarm):
    a, b, c, *_ = members(swarm)
    await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "reviewer": c, "crew": 3,
                                       "directions": ["oauth2", "pkce"]})
    await ok(swarm, a, "task_claim", {"task_id": 1})
    text = await ok(swarm, c, "task_update", {"task_id": 3, "assignee": "me", "status": "in_progress"})
    card = prompts.t("en", "note_direction_card", sub=3, id=1, owner=f"owner {a}")
    assert text.endswith("\n" + joined([card, prompts.t("en", "note_reviewer_cleared", id=1)]))
    assert swarm.board.get(1).reviewer is None
    assert told(swarm, a, "n_direction_taken") == [
        prompts.t("en", "n_direction_taken", by=c, id=3, title="pkce", parent=1)]


# --- task_claim -----------------------------------------------------------------------------------------------

async def test_claiming_a_direction_gets_its_card_and_clears_the_reviewer(swarm, monkeypatch):
    a, b, c, *_ = members(swarm)
    await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "reviewer": b, "crew": 3,
                                       "directions": ["oauth2", "pkce"]})
    seen = spy_notes(monkeypatch, swarm.board, "claim")
    text = await ok(swarm, b, "task_claim", {"task_id": 2})
    card = prompts.t("en", "note_direction_card", sub=2, id=1, owner="no owner yet")
    cleared = prompts.t("en", "note_reviewer_cleared", id=1)
    assert seen == [[card, cleared]]
    assert text == f'You now work on #2 [in_progress] oauth2 @{b} parent #1\n{joined([card, cleared])}'
    assert swarm.board.get(1).reviewer is None
    # the owner takes another one: its card says so, and it was the taker (no n_direction_taken to itself)
    await ok(swarm, a, "task_claim", {"task_id": 1})
    text = await ok(swarm, a, "task_claim", {"task_id": 3})
    assert prompts.t("en", "note_direction_card", sub=3, id=1, owner="you own #1").lstrip() in text
    assert told(swarm, a, "n_direction_taken") == []
    assert swarm.board.get(1).crew_working == [a, b]
    assert c not in swarm.board.contributors_of(swarm.board.get(1))


async def test_claiming_a_crew_task_gives_the_owner_sentence(swarm, monkeypatch):
    a, b, *_ = members(swarm)
    await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "crew": 3, "directions": ["oauth2", "pkce"]})
    seen = spy_notes(monkeypatch, swarm.board, "claim")
    text = await ok(swarm, b, "task_claim", {"task_id": 1})
    assert len(seen[0]) == 1 and seen[0][0].startswith(" You own crew task #1: take one of its directions yourself")
    assert text == f"You now work on #1 [in_progress] Auth @{b} crew 1/3\n    goal: log in\n" + joined(seen[0])
    assert '#2 [todo] "oauth2"; #3 [todo] "pkce"' in text


async def test_the_designated_reviewer_claiming_a_crew_task_reads_the_crew_ending(swarm, monkeypatch):
    a, b, *_ = members(swarm)
    await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "reviewer": b, "crew": 2,
                                       "directions": ["parser"]})
    seen = spy_notes(monkeypatch, swarm.board, "claim")
    text = await ok(swarm, b, "task_claim", {"task_id": 1})
    assert text == (f"You now work on #1 [in_progress] Auth @{b} crew 1/2\n    goal: log in\n{CLEARED_CREW}\n"
                    + joined(seen[0]))
    assert "another member will review it" not in text
    assert swarm.board.get(1).reviewer is None


async def test_a_claim_of_an_ordinary_parent_with_all_subtasks_finished_gives_the_owner_form(swarm):
    a, b, *_ = members(swarm)
    await ok(swarm, a, "task_create", {"title": "Epic", "goal": "all"})
    await ok(swarm, a, "task_create", {"title": "Part", "goal": "a part", "parent_id": 1})
    finish_raw(swarm.board, 2, Status.DONE, "done it")
    text = await ok(swarm, b, "task_claim", {"task_id": 1})
    assert text.endswith("\nAll its subtasks are finished: assemble their results and complete it.")


# --- finishing: the owner told in the result of its own call ------------------------------------------------------

def finished_text(sw: Swarm, crew_id: int, counts: str, total: int) -> str:
    """n_subtasks_done of a crew task whose directions are all finished, in the holder form with the draft, as the
    board renders it now."""
    task = sw.board.get(crew_id)
    ending = prompts.t("en", "ending_crew_finished", id=crew_id) + sw.board._crew_draft(task, "en")
    return prompts.t("en", "n_subtasks_done", id=crew_id, title=task.title, total=total, counts=counts, ending=ending)


async def test_the_owner_completing_the_last_direction_reads_the_finished_ending_in_its_result(swarm):
    a, b, *_ = members(swarm)
    swarm.config.swarm.independent_review = False   # no review: the completion finishes the direction at once
    await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "assignee": "me", "crew": 2,
                                       "directions": ["parser", "cli"]})
    await ok(swarm, b, "task_claim", {"task_id": 3})
    await ok(swarm, b, "task_update", {"task_id": 3, "goal": "a cli"})
    assert await ok(swarm, b, "task_complete", {"task_id": 3, "result": "cli in src/cli.py"}) == "Task #3 is done."
    await ok(swarm, a, "task_update", {"task_id": 2, "goal": "a parser"})
    text = await ok(swarm, a, "task_complete", {"task_id": 2, "result": "parser in src/parser.py"})
    expected = finished_text(swarm, 1, "2 done", 2)
    assert text == f"Task #2 is done.\n{expected}"
    assert "Draft for #1's result" in text and "parser in src/parser.py" in text and "cli in src/cli.py" in text
    assert told(swarm, a, "n_subtasks_done") == []      # the actor reads it in its result, no notification


async def test_the_owner_approving_the_last_direction_reads_the_finished_ending_in_its_result(swarm):
    a, b, c, *_ = members(swarm)
    board = swarm.board
    await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "assignee": "me", "crew": 2,
                                       "directions": ["parser", "cli"]})
    finish_raw(board, 2, Status.DONE, "parser in src/parser.py")
    await ok(swarm, b, "task_claim", {"task_id": 3})
    await ok(swarm, b, "task_update", {"task_id": 3, "goal": "a cli"})
    board.update(USER, 3, reviewer=a)                    # the owner reviews the direction
    text = await ok(swarm, b, "task_complete", {"task_id": 3, "result": "cli"})
    assert text == f"Task #3 was sent to {a} for review."
    text = await ok(swarm, a, "task_review", {"task_id": 3, "approve": True})
    assert text == f"Task #3 is now done (assignee: {b}).\n{finished_text(swarm, 1, '2 done', 2)}"
    assert told(swarm, a, "n_subtasks_done") == []
    # a reviewer that is not the owner reads nothing about the crew task: the owner is told
    await ok(swarm, a, "task_update", {"task_id": 1, "directions": ["docs"]})
    board.update(USER, 4, assignee=b, status=Status.IN_PROGRESS, goal="docs")
    board.update(USER, 4, reviewer=c)
    await ok(swarm, b, "task_complete", {"task_id": 4, "result": "docs"})
    text = await ok(swarm, c, "task_review", {"task_id": 4, "approve": True})
    assert text == f"Task #4 is now done (assignee: {b})."
    assert told(swarm, a, "n_subtasks_done") == [finished_text(swarm, 1, "3 done", 3)]


async def test_the_owner_cancelling_the_last_open_direction_reads_the_finished_ending_in_its_result(swarm):
    a, b, *_ = members(swarm)
    board = swarm.board
    await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "assignee": "me", "crew": 3,
                                       "directions": ["parser", "cli", "docs"]})
    finish_raw(board, 2, Status.DONE, "parser in src/parser.py")
    finish_raw(board, 3, Status.DONE, "cli in src/cli.py", assignee=b)
    text = await ok(swarm, a, "task_update", {"task_id": 4, "status": "cancelled"})
    expected = finished_text(swarm, 1, "2 done, 1 cancelled", 3)
    assert text.startswith("Updated task #4: status todo → cancelled.") and text.endswith("\n" + expected)
    assert '- #4 [cancelled] "docs"\n' not in expected and expected.endswith('- #4 [cancelled] "docs"')
    assert told(swarm, a, "n_subtasks_done") == [] and told(swarm, a, "n_task_closed") == []


async def test_a_returned_crew_task_tells_its_owner_to_fix_it_or_add_directions(swarm):
    a, b, c, *_ = members(swarm)
    board = swarm.board
    await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "assignee": "me", "reviewer": c, "crew": 2,
                                       "directions": ["parser", "cli"]})
    finish_raw(board, 2, Status.DONE, "parser")
    finish_raw(board, 3, Status.DONE, "cli", assignee=b)
    await ok(swarm, a, "task_claim", {"task_id": 1})
    assert await ok(swarm, a, "task_complete", {"task_id": 1, "result": "auth works"}) == (
        f"Task #1 was sent to {c} for review.")
    assert told(swarm, c, "n_crew_review_requested") == [
        prompts.t("en", "n_crew_review_requested", by=a, id=1, title="Auth", n=2)]
    text = await ok(swarm, c, "task_review", {"task_id": 1, "approve": False, "comment": "the cli does not parse"})
    assert text == f"Task #1 is now in_progress (assignee: {a})."
    rejected = told(swarm, a, "n_review_rejected")
    assert rejected == [prompts.t("en", "n_review_rejected", id=1, title="Auth", by=c,
                                  comment="the cli does not parse",
                                  ending=prompts.t("en", "review_rejected_crew", id=1))]
    assert "Draft for" not in rejected[0]


async def test_with_crew_review_off_task_complete_says_done_or_names_the_tasks_own_reviewer(swarm):
    a, b, c, *_ = members(swarm)
    board = swarm.board
    swarm.config.swarm.crew_review = False
    assert board.independent_review
    await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "assignee": "me", "crew": 2,
                                       "directions": ["parser", "cli"]})
    finish_raw(board, 2, Status.DONE, "parser")
    finish_raw(board, 3, Status.DONE, "cli", assignee=b)
    await ok(swarm, a, "task_claim", {"task_id": 1})
    assert await ok(swarm, a, "task_complete", {"task_id": 1, "result": "auth works"}) == "Task #1 is done."
    await ok(swarm, a, "task_create", {"title": "Docs", "goal": "documented", "assignee": "me", "reviewer": c,
                                       "crew": 2, "directions": ["guide", "api"]})
    finish_raw(board, 5, Status.DONE, "guide")
    finish_raw(board, 6, Status.DONE, "api", assignee=b)
    await ok(swarm, a, "task_claim", {"task_id": 4})
    assert await ok(swarm, a, "task_complete", {"task_id": 4, "result": "docs written"}) == (
        f"Task #4 was sent to {c} for review.")
    assert told(swarm, c, "n_review_requested") == [prompts.t("en", "n_review_requested", by=a, id=4, title="Docs")]
    assert told(swarm, c, "n_crew_review_requested") == []


# --- task_get -------------------------------------------------------------------------------------------------

async def test_task_get_lists_the_subtasks_and_the_crew(swarm):
    a, b, c, *_ = members(swarm)
    board = swarm.board
    await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "assignee": "me", "crew": 3,
                                       "directions": ["oauth2", "pkce", "jose", "oidc"]})
    long_line = "x" * 200
    finish_raw(board, 4, Status.DONE, f"\n{long_line}\nsecond line", assignee=b)
    set_raw(board, 3, assignee=c, status=Status.IN_PROGRESS)
    text = await ok(swarm, b, "task_get", {"task_id": 1})
    subtasks = text.split("\nSubtasks:\n", 1)[1].split("\n")
    assert subtasks[:5] == [
        f'#2 [in_progress] "oauth2" @{a}',
        f'#3 [in_progress] "pkce" @{c}',
        f'#4 [done] "jose" @{b} — ' + "x" * 119 + "…",
        '#5 [todo] "oidc"',
        f'Crew task: up to 3 members at once, the owner included; 2 working now (owner {a} #2; {c} #3). '
        'Free directions: #5 "oidc".',
    ]
    assert "second line" not in text
    # a working member's finished direction is not one it works on now
    set_raw(board, 4, assignee=c)
    text = await ok(swarm, b, "task_get", {"task_id": 1})
    assert f"2 working now (owner {a} #2; {c} #3)." in text


async def test_the_crew_line_without_workers_or_free_directions(swarm):
    a, *_ = members(swarm)
    await ok(swarm, a, "task_create", {"title": "Schema", "goal": "tables"})
    await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "crew": 2, "depends_on": [1],
                                       "directions": ["parser"]})
    text = await ok(swarm, a, "task_get", {"task_id": 2})
    assert ('\nSubtasks:\n#3 [todo] "parser"\nCrew task: up to 2 members at once, the owner included; 0 working now. '
            "Free directions: none.") in text  # blocked: nobody can take it now
    await ok(swarm, a, "task_create", {"title": "Docs", "goal": "write", "crew": 2})
    text = await ok(swarm, a, "task_get", {"task_id": 4})
    assert "Subtasks:" not in text
    assert text.endswith("Crew task: up to 2 members at once, the owner included; 0 working now. Free directions: none.")


# --- the board listing --------------------------------------------------------------------------------------

async def test_the_board_lists_a_subtask_under_its_listed_parent(swarm):
    a, b, *_ = members(swarm)
    await ok(swarm, a, "task_create", {"title": "Epic", "goal": "all"})
    await ok(swarm, a, "task_create", {"title": "Other", "goal": "else"})
    await ok(swarm, a, "task_create", {"title": "Part", "goal": "a part", "parent_id": 1})
    await ok(swarm, a, "task_create", {"title": "Detail", "goal": "a detail", "parent_id": 3})
    text = await ok(swarm, a, "board")
    assert text.split("\n")[2:] == [
        "#1 [todo] Epic", "    goal: all",
        "  ↳ #3 [todo] Part parent #1", "        goal: a part",
        "    ↳ #4 [todo] Detail parent #3", "          goal: a detail",
        "#2 [todo] Other", "    goal: else",
    ]
    # the parent not listed: the subtask is at the top level
    await ok(swarm, b, "task_claim", {"task_id": 3})
    text = await ok(swarm, a, "board", {"assignee": b})
    assert text.split("\n")[2:] == [f"#3 [in_progress] Part @{b} parent #1", "    goal: a part"]


async def test_the_board_shows_the_crew_of_a_crew_task(swarm):
    a, *_ = members(swarm)
    await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "assignee": "me", "crew": 3,
                                       "directions": ["oauth2", "pkce"]})
    lines = (await ok(swarm, a, "board")).split("\n")[2:]
    assert lines[0] == f"#1 [todo] Auth @{a} crew 1/3"
    assert lines[1:] == ["    goal: log in", f"  ↳ #2 [in_progress] oauth2 @{a} parent #1", "  ↳ #3 [todo] pkce parent #1"]


# --- audiences and revisions ------------------------------------------------------------------------------------

async def test_an_external_member_sees_and_uses_crew_and_directions(swarm):
    for name in ("task_create", "task_update"):
        for lang in ("en", "ru"):
            props = next(s for s in schemas_for("external", lang) if s["name"] == name)["parameters"]["properties"]
            assert props["crew"]["type"] == "integer" and props["directions"]["type"] == "array"
    await ok(swarm, USER, "join_swarm", {"role": "Helper", "name": "Ext Member"})
    text = await ok(swarm, "Ext Member", "task_create", {"title": "Auth", "goal": "log in", "assignee": "me",
                                                         "crew": 2, "directions": ["parser", "cli"]})
    assert 'Directions: #2 "parser", #3 "cli".\n#2 "parser" is your own part' in text  # a member: its own part


async def test_an_agent_frozen_at_revision_4_has_its_crew_arguments_ignored(swarm):
    a, b, *_ = members(swarm)
    frozen.write(swarm.db, a, frozen.record(4, "en", frozen.snapshot(4, "en")))
    runtime = AgentRuntime(swarm, a)
    text = await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "crew": 3,
                                              "directions": ["parser"]}, runtime=runtime)
    assert text.startswith("Created task #1 [todo] Auth\n[Ignored unknown argument(s): crew, directions. Usage: "
                           "task_create(title, goal, description?")
    assert swarm.board.get(1).crew == 0 and swarm.board.subtasks_of(1) == []
    text = await ok(swarm, b, "task_create", {"title": "Auth 2", "goal": "log in", "crew": 2},
                    runtime=AgentRuntime(swarm, b))
    assert text == "Created task #2 [todo] Auth 2 crew 0/2"


async def test_the_wake_up_offers_free_directions(swarm):
    a, b, *_ = members(swarm)
    await ok(swarm, a, "task_create", {"title": "Auth", "goal": "log in", "crew": 2, "directions": ["parser", "cli"]})
    await ok(swarm, a, "task_claim", {"task_id": 1})
    await ok(swarm, a, "task_claim", {"task_id": 2})
    text = swarm.render_wake(b)
    assert "#3 cli" in text and "#2 parser" not in text
    await ok(swarm, b, "task_claim", {"task_id": 3})
    await ok(swarm, a, "task_update", {"task_id": 1, "directions": ["docs"]})
    assert swarm.board.get(4).assignee is None  # the owner has its own direction already
    assert "#4 docs" not in (swarm.render_wake(b) or "")  # the crew is full: nothing more is offered
