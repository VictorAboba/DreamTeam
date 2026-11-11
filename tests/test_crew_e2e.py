"""Crew tasks end to end (docs/SPEC.md §4.2): four scripted agents, acting on the board's state only, plan a crew task,
take and hand in its directions, finish it and check it as a whole; a failed direction, an ownerless crew task and a
returned one. The tests assert invariants and sets, never which member took which direction (the agents run
concurrently). Then the ownerless chain at the swarm level: a parent without a holder whose subtasks finish is told
to its creator and offered again to every idle member."""

from __future__ import annotations

import json
from typing import Any

import pytest

from dreamteam import prompts
from dreamteam import swarm as swarm_module
from dreamteam.models import USER, AgentKind, Status
from dreamteam.swarm import Swarm
from test_crew_swarm import offer_rounds, offered, texts_for
from test_review import live_team
from test_runtime import shutdown
from test_swarm import call, make_swarm, run_until

TITLE, GOAL = "Build the tool", "the tool parses its input, has a command line and docs"
WORKING = (Status.IN_PROGRESS, Status.WAITING)
ANNA, BOB, CARL = "Anna Smith", "Bob Brown", "Carl Clark"


@pytest.fixture(autouse=True)
def fast_scheduler(monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.1)


def refused_claims(messages: list[dict]) -> set[int]:
    """The task ids the agent's last step claimed in vain (their results are errors): not claimed again at once."""
    for i in range(len(messages) - 1, -1, -1):
        if messages[i]["role"] != "assistant":
            continue
        results = {m.get("tool_call_id"): str(m.get("content") or "") for m in messages[i + 1:] if m["role"] == "tool"}
        ids = set()
        for c in messages[i].get("tool_calls") or []:
            args = c.get("arguments")
            args = json.loads(args) if isinstance(args, str) else (args or {})
            if c.get("name") == "task_claim" and results.get(c.get("id"), "").startswith("Error"):
                ids.add(args.get("task_id"))
        return ids
    return set()


class CrewTeam:
    """The scripted team: one agent plans one crew task (its owner, or none), the others take its directions. Reviews
    come first; a held direction gets its card, then is handed in; the owner completes the crew task once all its
    directions are finished (and handles a failed one), and adds a direction after a return."""

    def __init__(self, directions: list[str], *, crew: int = 3, owner: bool = True, fail: str | None = None,
                 settle: str | None = None, reject: bool = False) -> None:
        self.directions, self.crew, self.owner = directions, crew, owner
        self.fail = fail            # the direction its first taker fails
        self.settle = settle        # what the owner does with the failed direction: "todo" (reopen) or "cancelled"
        self.reject = reject        # the first check of the whole returns the crew task
        self.created = self.failed = self.rejected = self.added = self.tried = False
        self.checks: list[tuple[str, frozenset[str], str | None]] = []  # (reviewer, contributors, owner) per check

    def __call__(self, swarm: Swarm, name: str, system: str, messages: list[dict], tools: list[dict]) -> Any:
        board = swarm.board
        skip = refused_claims(messages)
        for task in board.reviews_of(name):
            if task.crew >= 2:
                self.checks.append((name, frozenset(board.contributors_of(task)), task.assignee))
                if self.reject and not self.rejected:
                    self.rejected = True
                    return call("task_review", task_id=task.id, approve=False,
                                comment="The parts do not fit: add tests.")
            return call("task_review", task_id=task.id, approve=True, comment="checked")
        for task in board.open_tasks_of(name):
            action = self._held(board, name, task, skip)
            if action is not None:
                return action
        for task in board.ready_unassigned():
            if task.id in skip:
                continue
            if task.crew >= 2 and board.finish_states([task.id])[task.id] is None:
                continue  # its directions first: the crew task is taken once they are finished
            if task.parent_id is not None and board.get(task.parent_id).assignee == name:
                continue  # the owner keeps to its own part
            return call("task_claim", task_id=task.id)
        goal = swarm.goal()
        if goal and goal["status"] == "active" and self.created and not board.list():
            return call("goal_complete", summary="The tool is built.")
        return call("wait", reason="nothing to do")

    def _held(self, board, name: str, task, skip: set[int]) -> Any:
        if task.status == Status.REVIEW:
            return None
        if task.status == Status.TODO:  # assigned to it: the crew task it owns
            return None if task.id in skip else call("task_claim", task_id=task.id)
        if "planning" in task.tags:
            if self.created:
                return call("task_complete", task_id=task.id, result="Planned one crew task.")
            self.created = True
            args: dict[str, Any] = {"title": TITLE, "goal": GOAL, "crew": self.crew, "directions": self.directions}
            if self.owner:
                args["assignee"] = "me"
            return call("task_create", **args)
        if task.crew >= 2:
            return self._own(board, task)
        parent = board.get(task.parent_id) if task.parent_id is not None else None
        if parent is not None and parent.crew >= 2:
            if not task.goal:
                return call("task_update", task_id=task.id, goal=f"{task.title} works and is tested",
                            description=f"src/{task.title}.py")
            if task.title == self.fail and not self.failed:
                self.failed = True
                return call("task_update", task_id=task.id, status="failed", comment="it cannot be done this way")
        return call("task_complete", task_id=task.id, result=f"{task.title} done by {name}")

    def _own(self, board, task) -> Any:
        if self.rejected and not self.added:
            self.added = True
            return call("task_update", task_id=task.id, directions=["tests"])
        state = board.finish_states([task.id])[task.id]
        if state == "failed":
            if not self.tried:
                self.tried = True  # refused: a failed direction
                return call("task_complete", task_id=task.id, result="all parts")
            failed = next(s for s in board.subtasks_of(task.id) if s.status == Status.FAILED)
            return call("task_update", task_id=failed.id, status=self.settle)
        if state == "finished":
            titles = ", ".join(s.title for s in board.subtasks_of(task.id) if s.status == Status.DONE)
            return call("task_complete", task_id=task.id, result=f"The tool, from its directions: {titles}.")
        return None


def team_swarm(tmp_path, team: CrewTeam, **swarm_overrides: Any) -> Swarm:
    return make_swarm(tmp_path, team, swarm={"start_agents": 4, "max_agents": 4, **swarm_overrides},
                      workspace={"git": False})


def crew_task(swarm: Swarm):
    return next(t for t in swarm.board.list(include_closed=True) if t.crew >= 2)


def history_texts(swarm: Swarm, name: str) -> list[str]:
    rows = swarm.db.all("SELECT msg FROM history WHERE agent = ? ORDER BY seq", (name,))
    return [str(json.loads(r["msg"]).get("content") or "") for r in rows]


def read_by(swarm: Swarm, name: str) -> list[str]:
    """What `name` was told about its subtasks finishing: its n_subtasks_done notifications (sent, read or not),
    then its history (the results of its own calls)."""
    return texts_for(swarm, name, "n_subtasks_done") + history_texts(swarm, name)


def llm_names(swarm: Swarm) -> set[str]:
    return {m["name"] for m in swarm.members() if m["kind"] == AgentKind.LLM}


def task_events(swarm: Swarm) -> list[dict]:
    return swarm.bus.recent(types=("task.created", "task.updated"), limit=100_000)


def status_change(event: dict) -> list[str] | None:
    """[old, new] of a task.updated event that changed the status, else None."""
    return event["data"].get("changes", {}).get("status")


def check_invariants(swarm: Swarm, crew_id: int, crew: int) -> None:
    """Replayed from the event stream: the working set of the crew task never exceeds `crew` (the team never lowers
    `crew` nor withdraws work from review), and its status is changed only by its holder or its reviewer as they were
    before the change, or by the member a claim makes its holder (it never changes by itself)."""
    rows: dict[int, dict] = {}
    for event in task_events(swarm):
        task = event["data"]["task"]
        before = rows.get(task["id"])
        rows[task["id"]] = task
        if task["id"] == crew_id and status_change(event):
            allowed = {before["assignee"], before["reviewer"], task["assignee"]} - {None, USER}
            assert event["actor"] in allowed, event
        parent = rows.get(crew_id)
        if parent is None:
            continue
        working = [parent["assignee"]] if parent["assignee"] and parent["status"] in Status.OPEN else []
        for row in sorted(rows.values(), key=lambda r: r["id"]):
            if row["parent_id"] == crew_id and row["status"] in WORKING and row["assignee"] not in working:
                working.append(row["assignee"])
        assert len([n for n in working if n != USER]) <= crew, (event, working)


async def run_team(swarm: Swarm) -> None:
    await swarm.start()
    swarm.set_goal(USER, "Build the tool", "parser, command line and docs")
    await run_until(lambda: (swarm.goal() or {}).get("status") == "done", timeout=40)


# ================================================================== scripted agents

async def test_a_crew_task_from_creation_to_the_check_of_the_whole(tmp_path):
    team = CrewTeam(["parser", "cli", "docs"])
    swarm = team_swarm(tmp_path, team)
    try:
        await run_team(swarm)
        board = swarm.board
        crew = crew_task(swarm)
        subs = board.subtasks_of(crew.id)
        assert crew.status == Status.DONE and crew.crew == 3
        assert [(s.title, s.status) for s in subs] == [("parser", "done"), ("cli", "done"), ("docs", "done")]
        owner = crew.assignee
        assert subs[0].assignee == owner and subs[0].created_by == owner          # its own part
        assert all(s.goal and s.description for s in subs)                         # the cards the takers wrote
        assert owner not in {s.assignee for s in subs[1:]}
        # each direction was approved by a member who did not execute it
        approvals = {e["data"]["task"]["id"]: e["actor"] for e in task_events(swarm)
                     if status_change(e) == [Status.REVIEW, Status.DONE]}
        assert all(approvals[s.id] not in (None, USER, s.assignee) for s in subs)
        # the owner read the finished ending with the draft (a notification, or the result of its own review)
        assert any(f"Draft for #{crew.id}'s result" in text for text in read_by(swarm, owner))
        # the check of the whole went to a member outside the crew, told it is one
        assert len(team.checks) == 1
        reviewer, contributors, checked_owner = team.checks[0]
        assert checked_owner == owner and reviewer != owner and reviewer not in contributors
        assert reviewer not in board.contributors_of(crew) and approvals[crew.id] == reviewer
        assert any(f'#{crew.id} "{TITLE}"' in text for text in texts_for(swarm, reviewer, "n_crew_review_requested"))
        check_invariants(swarm, crew.id, 3)
        assert {e["actor"] for e in task_events(swarm)
                if e["data"]["task"]["id"] == crew.id and status_change(e)} == {owner, reviewer}
    finally:
        await shutdown(swarm)


@pytest.mark.parametrize("settle", ["todo", "cancelled"])
async def test_a_failed_direction_blocks_the_whole_until_reopened_or_cancelled(tmp_path, settle):
    team = CrewTeam(["parser", "cli", "docs"], fail="cli", settle=settle)
    swarm = team_swarm(tmp_path, team)
    try:
        await run_team(swarm)
        board = swarm.board
        crew = crew_task(swarm)
        owner = crew.assignee
        cli = board.subtasks_of(crew.id)[1]
        assert crew.status == Status.DONE and team.failed and team.tried
        assert cli.status == (Status.DONE if settle == "todo" else Status.CANCELLED)
        # the owner was told: n_direction_failed while another direction was open, else the failed ending
        failed_ending = prompts.t("en", "ending_crew_failed", id=crew.id, failed=cli.id)
        assert ("n_direction_failed" in [r["kind"] for r in swarm.db.all(
                    "SELECT kind FROM notifications WHERE recipient = ?", (owner,))]
                or any(failed_ending in text for text in texts_for(swarm, owner, "n_subtasks_done")))
        # its completion was refused while the direction stood failed
        assert any(text.startswith(f"Error: Crew task #{crew.id} has failed directions: #{cli.id}")
                   for text in history_texts(swarm, owner))
        statuses = [status_change(e) for e in task_events(swarm)
                    if e["data"]["task"]["id"] == cli.id and status_change(e)]
        assert [Status.IN_PROGRESS, Status.FAILED] in statuses and [Status.FAILED, settle] in statuses
        if settle == "todo":   # reopened: unassigned, taken again and done
            assert [Status.TODO, Status.IN_PROGRESS] in statuses[statuses.index([Status.FAILED, settle]):]
        reviewer = team.checks[-1][0]
        check_invariants(swarm, crew.id, 3)
        contributors = board.contributors_of(crew)
        assert reviewer != owner
        if llm_names(swarm) - contributors:  # (a reopened direction may have had a fourth taker)
            assert reviewer not in contributors
    finally:
        await shutdown(swarm)


async def test_an_ownerless_crew_task_is_taken_when_its_directions_finish(tmp_path):
    team = CrewTeam(["parser", "cli"], crew=2, owner=False)
    swarm = team_swarm(tmp_path, team)
    try:
        await run_team(swarm)
        board = swarm.board
        crew = crew_task(swarm)
        creator = crew.created_by
        assert crew.status == Status.DONE and crew.assignee is not None
        # the creator was told with the creator form (asked to take it; no draft)
        creator_ending = prompts.t("en", "ending_crew_finished_creator", id=crew.id)
        assert any(creator_ending in text and "Draft for" not in text for text in read_by(swarm, creator))
        # nobody took the crew task before its directions finished
        claims = [e for e in task_events(swarm) if e["data"]["task"]["id"] == crew.id
                  and status_change(e) == [Status.TODO, Status.IN_PROGRESS]]
        assert len(claims) == 1
        last_direction_done = max(e["id"] for e in task_events(swarm) if e["data"]["task"]["parent_id"] == crew.id
                                  and status_change(e) == [Status.REVIEW, Status.DONE])
        assert claims[0]["id"] > last_direction_done
        # its claimer read the owner form of the finished ending with the draft in its claim's result
        prefix = prompts.t("en", "owner_prefix_finished")
        assert any(text.startswith(f"You now work on #{crew.id} ") and prefix in text
                   and f"Draft for #{crew.id}'s result" in text for text in history_texts(swarm, crew.assignee))
        check_invariants(swarm, crew.id, 2)
    finally:
        await shutdown(swarm)


async def test_a_returned_crew_task_is_completed_again_after_new_directions(tmp_path):
    team = CrewTeam(["parser", "cli", "docs"], reject=True)
    swarm = team_swarm(tmp_path, team)
    try:
        await run_team(swarm)
        board = swarm.board
        crew = crew_task(swarm)
        owner = crew.assignee
        subs = board.subtasks_of(crew.id)
        assert crew.status == Status.DONE and team.rejected and team.added
        assert [(s.title, s.status) for s in subs] == [("parser", "done"), ("cli", "done"), ("docs", "done"),
                                                       ("tests", "done")]
        assert subs[3].created_by == owner and subs[3].assignee != owner
        # the return told the owner to fix it or add directions, without a draft
        rejected = texts_for(swarm, owner, "n_review_rejected")
        assert len(rejected) == 1 and rejected[0].endswith(prompts.t("en", "review_rejected_crew", id=crew.id))
        assert "Draft for" not in rejected[0]
        # the last draft the owner read lists every direction in creation order
        drafts = [t for t in read_by(swarm, owner) if f"Draft for #{crew.id}'s result" in t and '"tests"' in t]
        last = drafts[-1]
        positions = [last.index(f'- #{s.id} [done] "{s.title}"') for s in subs]
        assert positions == sorted(positions)
        checks = [e for e in task_events(swarm) if e["data"]["task"]["id"] == crew.id
                  and (status_change(e) or [None])[0] == Status.REVIEW]
        assert [status_change(e)[1] for e in checks] == [Status.IN_PROGRESS, Status.DONE]
        assert len(team.checks) == 2 and all(owner != r for r, _, _ in team.checks)
        check_invariants(swarm, crew.id, 3)
    finally:
        await shutdown(swarm)


# ================================================================== the ownerless chain (swarm level)

def take_and_finish(swarm: Swarm, name: str, task_id: int) -> None:
    """`name` claims the subtask, writes its goal, hands it in, and its reviewer approves it."""
    board = swarm.board
    board.claim(name, task_id)
    board.update(name, task_id, goal="works")
    task = board.complete(name, task_id, "done it")
    board.review(task.reviewer, task_id, True)


def finished_for_creator(swarm: Swarm, task, total: int, plain: bool = False) -> str:
    ending = prompts.t("en", "ending_plain_creator" if plain else "ending_crew_finished_creator", id=task.id)
    return prompts.t("en", "n_subtasks_done", id=task.id, title=task.title, total=total, counts=f"{total} done",
                     ending=ending)


@pytest.mark.parametrize("kind", ["crew", "ordinary", "released"])
async def test_a_parent_without_a_holder_is_offered_again_when_its_subtasks_finish(tmp_path, kind):
    """A parent nobody holds while its subtasks run (a crew task created without an owner, an ordinary parent
    without an assignee, a crew task its owner released): the members it was offered to take its subtasks; at the
    last approval its creator is told with the creator form, and the next pass offers it to every idle member
    again; the claimer reads the owner form."""
    swarm = await live_team(tmp_path, ANNA, BOB, CARL)
    try:
        board = swarm.board
        if kind == "ordinary":
            parent = board.create(ANNA, "Report", goal="g")
            subs = [board.create(ANNA, title, parent_id=parent.id) for title in ("intro", "body")]
        else:
            parent = board.create(ANNA, "Tool", goal="g", crew=2, directions=["parser", "cli"],
                                  assignee=ANNA if kind == "released" else None)
            subs = board.subtasks_of(parent.id)
        swarm.set_goal(USER, "Ship it")                      # the board is not empty: no planning card
        if kind == "released":
            assert subs[0].assignee == ANNA and subs[0].status == Status.IN_PROGRESS   # its own part
            board.update(ANNA, parent.id, assignee="")       # released while its directions run
        offer_rounds(swarm, 4)
        before = [offered(swarm, n, parent) for n in (ANNA, BOB, CARL)]
        assert before == [1, 1, 1]
        if kind == "released":
            board.update(ANNA, subs[0].id, goal="works")
            board.review(board.complete(ANNA, subs[0].id, "parsed").reviewer, subs[0].id, True)
        else:
            take_and_finish(swarm, BOB, subs[0].id)
        assert texts_for(swarm, ANNA, "n_subtasks_done") == []
        take_and_finish(swarm, CARL, subs[1].id)
        parent = board.get(parent.id)
        assert parent.status == Status.TODO and parent.assignee is None          # never changed by itself
        assert texts_for(swarm, ANNA, "n_subtasks_done") == [finished_for_creator(swarm, parent, 2,
                                                                                  plain=kind == "ordinary")]
        offer_rounds(swarm, 4)
        assert [offered(swarm, n, parent) for n in (ANNA, BOB, CARL)] == [2, 2, 2]
        assert swarm._nudged[(parent.id, "finished")] == {ANNA, BOB, CARL}
        notes: list[str] = []
        board.claim(BOB, parent.id, notes=notes)
        if kind == "ordinary":
            assert notes == [" " + prompts.t("en", "owner_prefix_plain") + prompts.t("en", "ending_plain",
                                                                                       id=parent.id)]
        else:
            assert len(notes) == 1 and notes[0].startswith(" " + prompts.t("en", "owner_prefix_finished"))
            assert f"Draft for #{parent.id}'s result" in notes[0]
    finally:
        await shutdown(swarm)
