"""The external HTTP API v1 in thread mode (docs/SPEC.md §22.9 "HTTP API v1", "Instructions from outside"): create with
`mode` and `goal_rule` (session.json and the forum tables before the first start; 400 for another mode or rule and
for a rule with an effective board mode), the options' modes and rules, a thread summary (`mode`, `goal_vote`, `posts`,
`tasks` {}), a thread result (`posts`, `decision`, `tasks` []) of a running and of a stopped session, the timeline's
forum events, PATCH `goal_rule` and `complete_goal`, `continue` as a post of the user, the automatic answers with
clarify on, and a clarify asker's `question` request. Every request goes through the launcher's app and its guard on
the test's own loop (test_api_v1.Hub): no port, ScriptedLLM only."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

import pytest

from dreamteam import api_v1, forum, instruct, sessionopts, tools
from dreamteam.board import MAX_POST
from dreamteam.forum import GOAL_POST, PLAN_POST
from dreamteam.goal_text import GOAL_TITLE_MAX
from dreamteam.models import USER
from dreamteam.result import build_result
from dreamteam.sessions import read_posts, session_uid
from dreamteam.swarm import Swarm
from test_api_v1 import MANUAL, Hub, _from_user, _home, _manager, _until, no_start  # noqa: F401 - a fixture
from test_api_v1_guard import api_token, hub_token  # noqa: F401 - fixtures
from test_forum_delivery import f_rows, waking
from test_tui_print import MARK, call, launch, run, texts  # noqa: F401 - a fixture

THREAD = {"mode": "thread", **MANUAL}   # a thread session that keeps running, nobody answering for it


async def running(hub: Hub, **body: Any) -> tuple[str, str, Swarm]:
    """A running v1 thread session: (its id, its folder name, its swarm)."""
    status, answer = await hub.create({"goal": "Write a haiku\nAbout spring.", **THREAD, **body})
    assert status == 202, answer
    await hub.settled()
    made = answer["session"]
    return made["id"], made["name"], hub.manager.swarms[made["name"]]


async def summary_of(hub: Hub, uid: str) -> dict[str, Any]:
    status, answer = await hub.call("GET", f"/api/v1/sessions/{uid}")
    assert status == 200, answer
    return answer["session"]


async def result_of(hub: Hub, uid: str) -> dict[str, Any]:
    status, answer = await hub.call("GET", f"/api/v1/sessions/{uid}/result")
    assert status == 200, answer
    return answer


def llm_names(swarm: Swarm) -> list[str]:
    return [m["name"] for m in swarm.members() if m["kind"] == "llm"]


def user_notes(swarm: Swarm, kind: str) -> list[Any]:
    return swarm.db.all("SELECT id FROM notifications WHERE recipient = ? AND kind = ?", (USER, kind))


# ---------------------------------------------------------------------------------------------- the pieces
def test_the_modes_and_rules_of_the_options_are_the_tools_and_the_forums():
    assert tuple(tools.MODES) == ("board", "thread") and tuple(sessionopts.GOAL_RULES) == tuple(forum.RULES)


@pytest.mark.parametrize("text, title, body", [
    ("Add a README", "Add a README", "Add a README"),                   # one line: the text is the whole instruction
    ("Add a README\n\n  \n", "Add a README", "Add a README"),           # the rest blank
    ("# Add a README\nWith the install steps.", "# Add a README", "With the install steps."),
    ("Add a README\r\nWith the install steps.\r\n\r\nAnd the licence.", "Add a README",
     "With the install steps.\n\nAnd the licence."),
])
def test_post_parts_split_an_instruction_into_a_title_and_a_text(text, title, body):
    assert instruct.post_parts(text) == (title, body)


def test_a_long_first_line_keeps_the_whole_instruction_in_the_text():
    first = " ".join(["word"] * 60)   # 299 characters, no sentence end
    text = f"{first}\nThe second line."
    title, body = instruct.post_parts(text)
    assert len(title) <= GOAL_TITLE_MAX and title.endswith("…") and body == text


def test_continue_step_posts_in_a_thread_session_and_never_asks_the_coordinator():
    goal = {"title": "Write", "status": "active"}
    agents = [{"name": "Anna Smith", "kind": "llm", "status": "active"}]
    thread = {"swarm": {"goal": goal, "mode": "thread"}, "agents": agents, "tasks": []}
    assert instruct.continue_step(thread) == instruct.ContinueStep("post")
    paused = {**thread, "swarm": {**thread["swarm"], "paused": True, "pause_reason": "user"}}
    assert instruct.continue_step(paused) == instruct.ContinueStep("post", resume=True)
    assert instruct.continue_step({**thread, "swarm": {"mode": "thread"}}).action == "set_goal"
    done = {**thread, "swarm": {"goal": {**goal, "status": "done"}, "mode": "thread"}}
    assert instruct.continue_step(done).action == "reopen_goal"
    asked = {**thread, "swarm": {**thread["swarm"], "question": {"asker": "Anna Smith", "message_id": 7}}}
    assert instruct.continue_step(asked) == instruct.ContinueStep("message", to="Anna Smith", answers_question=7)
    # a board session (no mode: board) still writes to the coordinator
    board = {"swarm": {"goal": goal}, "agents": agents, "tasks": []}
    assert instruct.continue_step(board) == instruct.ContinueStep("message", to="Anna Smith")


def test_a_board_result_has_no_posts_and_no_decision_whatever_it_is_given():
    posts = [{"id": 1, "kind": "goal", "title": "Goal reached?", "text": "secret", "author": None, "likes": 1,
              "dislikes": 0, "comments": 2, "created_at": 1.0, "edited_at": None, "active_at": 2.0, "my_vote": None}]
    common = dict(status="done", session="s", goal=None, summary="", tasks=[], agents=[], jobs=[], cost_usd=0,
                  duration_s=None)
    board = build_result(**common, posts=posts, decision={"rule": "user"})
    assert board["mode"] == "board" and board["posts"] == [] and board["decision"] is None
    thread = build_result(**common, mode="thread", posts=posts * 300, decision={"rule": "user"})
    assert thread["mode"] == "thread" and thread["decision"] == {"rule": "user"} and len(thread["posts"]) == 200
    assert thread["posts"][0] == {"id": 1, "kind": "goal", "title": "Goal reached?", "author": None, "likes": 1,
                                  "dislikes": 0, "comments": 2, "created_at": 1.0, "active_at": 2.0}


def test_read_posts_of_a_board_file_reads_no_forum_table(tmp_path):
    path = tmp_path / "swarm.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    assert read_posts(path) == [] and read_posts(tmp_path / "none.db") == []


# ---------------------------------------------------------------------------------------------- create and options
async def test_a_thread_create_stores_its_mode_and_rule_and_has_its_forum_before_the_first_start(
        tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        no_start(hub.manager)
        status, answer = await hub.create({"goal": "Write a haiku", "mode": "thread", "goal_rule": "veto",
                                           "team": ["Critic", "writer"], **MANUAL})
        assert status == 202, answer
        await hub.settled()
        session = await summary_of(hub, answer["session"]["id"])
        assert session["mode"] == "thread" and session["state"] == "created" and session["tasks"] == {}
        assert session["goal"]["decision"] is None and session["goal_vote"] is None and session["posts"] == 0
        sid = session["name"]
        stored = hub.manager.meta(sid)["overrides"]["swarm"]
        assert stored["mode"] == "thread" and stored["goal_rule"] == "veto"
        assert stored["starting_roles"] == ["Critic", "writer"]   # (the reviewer's thread name resolves)
        conn = sqlite3.connect(hub.manager._data_dir(sid) / "swarm.db")
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            mode = conn.execute("SELECT value FROM kv WHERE key = 'swarm_mode'").fetchone()
        finally:
            conn.close()
        assert {"posts", "post_comments", "votes", "subscriptions"} <= tables and json.loads(mode[0]) == "thread"
        # a board create stores neither (and a home default of board)
        status, answer = await hub.create({"goal": "Write a limerick", **MANUAL})
        assert status == 202 and answer["session"]["mode"] == "board"
        assert "mode" not in (hub.manager.meta(answer["session"]["name"])["overrides"].get("swarm") or {})
        # the same client_ref with another mode is another request
        assert (await hub.create({"goal": "Job", "client_ref": "job-1", **MANUAL}))[0] == 202
        status, answer = await hub.create({"goal": "Job", "client_ref": "job-1", "mode": "thread", **MANUAL})
        assert status == 409 and answer["code"] == "conflict", answer
    finally:
        await hub.close()


@pytest.mark.parametrize("body, code, text", [
    ({"mode": "forum"}, "bad_request", "'mode' must be one of board, thread."),
    ({"mode": 1}, "bad_request", "'mode' must be one of board, thread."),
    ({"mode": "thread", "goal_rule": "majority"}, "bad_request", "'goal_rule' must be one of democracy, veto."),
    ({"goal_rule": "veto"}, "bad_request", sessionopts.GOAL_RULE_BOARD),          # the home's mode is board
    ({"mode": "board", "goal_rule": "democracy"}, "bad_request", sessionopts.GOAL_RULE_BOARD),
    ({"team": ["Critic"]}, "unknown_role", None),                                # no Critic in a board session
])
async def test_a_create_with_another_mode_or_rule_makes_nothing(tmp_path, api_token, body, code, text):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        status, answer = await hub.create({"goal": "Write a haiku", **MANUAL, **body})
        assert status == 400 and answer["code"] == code, answer
        assert text is None or answer["error"] == text
        assert hub.manager.ids() == []
    finally:
        await hub.close()


async def test_a_home_whose_default_is_thread_makes_thread_sessions_and_takes_a_rule(tmp_path, api_token, no_start):
    hub = Hub(_manager(_home(tmp_path, swarm={"mode": "thread", "goal_rule": "veto"})))
    try:
        no_start(hub.manager)
        status, answer = await hub.create({"goal": "Write a haiku", "goal_rule": "democracy", **MANUAL})
        assert status == 202 and answer["session"]["mode"] == "thread", answer
        assert hub.manager.meta(answer["session"]["name"])["overrides"]["swarm"]["goal_rule"] == "democracy"
        status, answer = await hub.create({"goal": "Write a limerick", "mode": "board", **MANUAL})
        assert status == 202 and answer["session"]["mode"] == "board", answer
        status, options = await hub.call("GET", "/api/v1/options")
        assert status == 200 and options["mode"] == "thread" and options["goal_rule"] == "veto"
        reviewer = next(role for role in options["roles"] if role["key"] == "reviewer")
        assert reviewer["name"] == "Critic"   # the default mode's texts
    finally:
        await hub.close()


async def test_the_options_name_the_modes_and_the_rules(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        status, options = await hub.call("GET", "/api/v1/options")
        assert status == 200
        assert options["modes"] == ["board", "thread"] and options["mode"] == "board"
        assert options["goal_rules"] == ["democracy", "veto"] and options["goal_rule"] == "democracy"
        reviewer = next(role for role in options["roles"] if role["key"] == "reviewer")
        assert reviewer["name"] != "Critic"
    finally:
        await hub.close()


# ---------------------------------------------------------------------------------------------- summary and result
async def test_a_thread_summary_and_result_of_a_running_and_of_a_stopped_session(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        swarm.forum.create(USER, "Use the word cherry", "")
        member = llm_names(swarm)[0]
        session = await summary_of(hub, uid)
        assert list(session)[3:5] == ["state", "mode"] and session["mode"] == "thread"
        assert session["tasks"] == {} and session["posts"] == 1 and session["goal"]["decision"] is None
        assert session["goal_vote"] == {"rule": "democracy", "counted": 1, "for": [], "against": [], "needed": 1,
                                        "met": False}
        result = await result_of(hub, uid)
        assert result["mode"] == "thread" and result["tasks"] == [] and result["decision"] is None
        assert [p["id"] for p in result["posts"]][:2] == [GOAL_POST, PLAN_POST]
        assert result["posts"][2]["title"] == "Use the word cherry" and result["posts"][2]["author"] == USER
        assert all(set(p) == {"id", "kind", "title", "author", "likes", "dislikes", "comments", "created_at",
                              "active_at"} for p in result["posts"])
        swarm.forum.vote(member, "#1", "like")   # (democracy, one agent: the vote completes the goal)
        session = await summary_of(hub, uid)
        assert session["state"] == "done" and session["goal"]["decision"]["rule"] == "democracy"
        assert session["goal_vote"]["for"] == [member] and session["goal_vote"]["met"] is True
        result = await result_of(hub, uid)
        assert result["decision"] == session["goal"]["decision"] and result["summary"] == session["goal"]["summary"]
        assert result["posts"][0]["likes"] == 1
        status, answer = await hub.call("POST", f"/api/v1/sessions/{uid}/stop")
        assert status == 200 and answer["session"]["final"] is True
        assert sid not in hub.manager.swarms   # (unloaded: the figures below come from its file)
        stopped = await summary_of(hub, uid)
        assert stopped["mode"] == "thread" and stopped["posts"] == 1 and stopped["goal_vote"]["for"] == [member]
        assert stopped["goal"]["decision"] == session["goal"]["decision"]
        kept = await result_of(hub, uid)
        assert kept["final"] is True and [p["id"] for p in kept["posts"]] == [p["id"] for p in result["posts"]]
        assert kept["decision"] == session["goal"]["decision"] and kept["tasks"] == [] and kept["mode"] == "thread"
        assert sid not in hub.manager.swarms
    finally:
        await hub.close()


# ---------------------------------------------------------------------------------------------- events
async def test_the_timeline_shows_the_feed_and_not_the_votes(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub)
        member = llm_names(swarm)[0]
        post = swarm.forum.create(USER, "Use the word cherry", "")
        swarm.forum.update(USER, post["id"], title="Use the word plum")
        swarm.forum.comment(USER, post["id"], "Or cherry after all.")
        swarm.forum.vote(member, f"#{post['id']}", "like")
        swarm.bus.emit("files.committed", member, kind="autocommit", commit="0" * 40,
                       files=[{"path": "haiku.txt", "change": "added"}], suspects=[member])
        status, page = await hub.call("GET", f"/api/v1/sessions/{uid}/events", query="limit=500")
        assert status == 200, page
        types = [event["type"] for event in page["events"]]
        for kind in ("post.created", "post.updated", "post.comment", "files.committed"):
            assert kind in types, (kind, types)
        assert "post.vote" not in types and "subscription.changed" not in types
        assert all(event["text"] for event in page["events"] if event["type"].startswith(("post.", "files.")))
        status, page = await hub.call("GET", f"/api/v1/sessions/{uid}/events", query="detail=all&limit=500")
        assert status == 200 and "post.vote" in [event["type"] for event in page["events"]]
    finally:
        await hub.close()


# ---------------------------------------------------------------------------------------------- PATCH
async def test_patch_goal_rule_of_a_running_session_checks_the_vote_and_stop_when_done_stops_it(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        status, answer = await hub.create({"goal": "Write a haiku", "mode": "thread", "goal_rule": "veto",
                                           "start_agents": 3, "answers": "manual"})   # (stop_when_done: true)
        assert status == 202, answer
        await hub.settled()
        uid, sid = answer["session"]["id"], answer["session"]["name"]
        swarm = hub.manager.swarms[sid]
        first, second, _third = llm_names(swarm)
        swarm.forum.vote(first, "#1", "like")
        swarm.forum.vote(second, "#1", "like")
        session = await summary_of(hub, uid)
        assert session["state"] == "running" and session["goal_vote"]["needed"] == 3   # (veto: all three)
        status, answer = await hub.call("PATCH", f"/api/v1/sessions/{uid}", body={"goal_rule": "democracy"})
        assert status == 200, answer
        assert hub.manager.meta(sid)["overrides"]["swarm"]["goal_rule"] == "democracy"
        await _until(lambda: hub.manager.is_final(sid), "the stop of the done job")
        session = await summary_of(hub, uid)
        assert session["state"] == "done" and session["goal"]["decision"]["rule"] == "democracy"
        assert set(session["goal"]["decision"]["for"]) == {first, second}
        conn = sqlite3.connect(hub.manager._data_dir(sid) / "swarm.db")
        try:
            (actor,) = conn.execute("SELECT actor FROM events WHERE type = 'goal.completed'").fetchone()
        finally:
            conn.close()
        assert actor is None   # no voter met the rule: the rule did
    finally:
        await hub.close()


async def test_patch_goal_rule_of_a_stopped_session_writes_its_overrides_only(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        sid = (await hub.manager.create("Haiku", "Write a haiku", start=False, api=MANUAL,
                                        overrides={"swarm": {"mode": "thread", "goal_rule": "veto"}}))["id"]
        path = f"/api/v1/sessions/{session_uid(hub.manager.meta(sid))}"
        status, answer = await hub.call("PATCH", path, body={"goal_rule": "democracy"})
        assert status == 200 and answer["session"]["state"] == "created", answer
        assert hub.manager.meta(sid)["overrides"]["swarm"]["goal_rule"] == "democracy"
        assert sid not in hub.manager.swarms
        for body in ({"goal_rule": ""}, {"goal_rule": "majority"}, {"goal_rule": 1}):   # ("": cannot be cleared)
            status, answer = await hub.call("PATCH", path, body=body)
            assert status == 400 and answer["code"] == "bad_request", (body, answer)
        assert (await hub.call("PATCH", path, body={"goal_rule": None}))[0] == 200   # (null: not given)
        assert hub.manager.meta(sid)["overrides"]["swarm"]["goal_rule"] == "democracy"
        status, answer = await hub.call("PATCH", path, body={"complete_goal": True})
        assert status == 409 and answer["code"] == "not_running", answer
    finally:
        await hub.close()


async def test_patch_complete_goal_completes_it_as_the_user(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub, start_agents=2)
        path = f"/api/v1/sessions/{uid}"
        for body in ({"goal_summary": "Done."}, {"complete_goal": False, "goal_summary": "Done."},
                     {"complete_goal": "yes"}, {"complete_goal": True, "goal_summary": 5},
                     {"complete_goal": True, "goal_summary": "x" * (MAX_POST + 1)},
                     {"complete_goal": True, "goal_rule": "majority"}):
            status, answer = await hub.call("PATCH", path, body=body)
            assert status == 400 and answer["code"] == "bad_request", (body, answer)
        assert swarm.goal()["status"] == "active"
        assert (await hub.call("PATCH", path, body={"complete_goal": False}))[0] == 200   # (changes nothing)
        status, answer = await hub.call("PATCH", path, body={"complete_goal": True, "wrap_up": True,
                                                             "goal_summary": "  The haiku is in haiku.txt.  "})
        assert status == 200, answer
        session = answer["session"]
        assert session["state"] == "done" and session["goal"]["summary"] == "The haiku is in haiku.txt."
        decision = session["goal"]["decision"]
        assert decision["rule"] == "user" and decision["wrap_up"] is False   # (wrap-up mode came after)
        assert swarm.wrapping_up and swarm.goal()["completed_by"] == USER
        assert user_notes(swarm, "u_goal_done") == [] and user_notes(swarm, "u_goal_decided") == []
        limits = swarm.limits()
        status, answer = await hub.call("PATCH", path, body={"complete_goal": True, "max_agents": 2})
        assert status == 409 and answer["code"] == "conflict" and "already" in answer["error"], answer
        assert swarm.limits() == limits   # (the goal is checked first: nothing written)
    finally:
        await hub.close()


async def test_patch_complete_goal_without_a_goal_and_in_a_board_session(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        sid = (await hub.manager.create("Idle", "", start=True, api=MANUAL,
                                        overrides={"swarm": {"mode": "thread"}}))["id"]
        path = f"/api/v1/sessions/{session_uid(hub.manager.meta(sid))}"
        limits = hub.manager.swarms[sid].limits()
        status, answer = await hub.call("PATCH", path, body={"complete_goal": True, "max_agents": 2})
        assert status == 409 and answer["code"] == "conflict", answer
        assert hub.manager.swarms[sid].limits() == limits   # (checked first: nothing written)
        board = (await hub.create({"goal": "Write a limerick", **MANUAL}))[1]["session"]
        await hub.settled()
        path = f"/api/v1/sessions/{board['id']}"
        for body, text in (({"complete_goal": True}, api_v1.COMPLETE_GOAL_THREAD_TEXT),
                           ({"goal_summary": "Done."}, api_v1.COMPLETE_GOAL_THREAD_TEXT),
                           ({"goal_rule": "veto"}, api_v1.GOAL_RULE_THREAD_TEXT)):
            status, answer = await hub.call("PATCH", path, body=body)
            assert status == 400 and answer["code"] == "bad_request" and answer["error"] == text, answer
        assert hub.manager.swarms[board["name"]].goal()["status"] == "active"
    finally:
        await hub.close()


# ---------------------------------------------------------------------------------------------- continue
async def test_continue_in_a_thread_session_is_a_post_of_the_user_that_wakes_everyone(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub, start_agents=2)
        path = f"/api/v1/sessions/{uid}/continue"
        sent = len(_from_user(swarm))
        status, answer = await hub.call("POST", path, body={"text": "Add a README"})
        assert status == 200 and answer["session"]["posts"] == 1, answer
        (post,) = [p for p in swarm.forum.listing() if p["kind"] == "post"]
        assert post["author"] == USER and post["title"] == "Add a README"
        assert swarm.forum.get(post["id"])["text"] == "Add a README"
        for name in llm_names(swarm):
            assert any(row["kind"] == "f_post" and waking(row) and row["data"].get("post_id", post["id"]) == post["id"]
                       for row in f_rows(swarm, name)), name
        assert len(_from_user(swarm)) == sent   # (no message to a coordinator)
        first = " ".join(["word"] * 60)   # (a first line of 299 characters)
        text = f"{first}\nThen a second line."
        status, answer = await hub.call("POST", path, body={"text": text})
        assert status == 200, answer
        newest = max((p for p in swarm.forum.listing() if p["kind"] == "post"), key=lambda p: p["id"])
        assert len(newest["title"]) <= GOAL_TITLE_MAX and swarm.forum.get(newest["id"])["text"] == text
        # a text the forum refuses (a post holds at most MAX_POST characters): 409 with its text, nothing posted
        status, answer = await hub.call("POST", path, body={"text": "x" * (MAX_POST + 1)})
        assert status == 409 and answer["code"] == "conflict" and "too long" in answer["error"], answer
        assert answer["error"] and len([p for p in swarm.forum.listing() if p["kind"] == "post"]) == 2
        # a pending question: the answer is a message to the asker, no post
        member = llm_names(swarm)[0]
        asked = swarm.mail.send(member, [USER], "Which season?")
        swarm.pause(member, "question", asker=member, message_id=asked["id"], since=time.time())
        status, answer = await hub.call("POST", path, body={"text": "Spring."})
        assert status == 200 and _from_user(swarm)[-1] == ([member], "Spring.", asked["id"]), answer
        assert not swarm.paused and len([p for p in swarm.forum.listing() if p["kind"] == "post"]) == 2
    finally:
        await hub.close()


async def test_continue_sets_and_reopens_a_thread_goal(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        sid = (await hub.manager.create("Idle", "", start=False, api=MANUAL,
                                        overrides={"swarm": {"mode": "thread"}}))["id"]
        path = f"/api/v1/sessions/{session_uid(hub.manager.meta(sid))}"
        status, answer = await hub.call("POST", f"{path}/continue", body={"text": "Write a poem\nAbout the sea."})
        assert status == 200 and answer["session"]["state"] == "starting", answer
        await hub.settled()
        swarm = hub.manager.swarms[sid]
        assert swarm.goal()["title"] == "Write a poem" and swarm.forum.has_post(GOAL_POST)
        assert swarm.forum.has_post(PLAN_POST)
        member = llm_names(swarm)[0]
        swarm.forum.vote(member, "#1", "like")
        assert swarm.goal()["status"] == "done"
        comment = "Not yet: " + "x" * 30_000
        status, answer = await hub.call("POST", f"{path}/continue", body={"text": comment})
        assert status == 200 and answer["session"]["goal"]["status"] == "active", answer
        assert answer["session"]["goal"]["decision"] is None
        assert swarm.forum.votes("#1") == [] and answer["session"]["goal_vote"]["for"] == []
        (row,) = swarm.db.all("SELECT author, text FROM post_comments WHERE post_id = ? ORDER BY id DESC LIMIT 1",
                              (GOAL_POST,))
        assert row["author"] == USER and len(row["text"]) <= MAX_POST and row["text"].startswith("Not yet: x")
    finally:
        await hub.close()


async def test_print_mode_continues_a_thread_session_with_a_post_and_reports_the_feed(launch):
    """`dreamteam -p -r <id> "<text>"` in a thread session: the prompt becomes a post of the user (post_create), which
    wakes the member; its like on #1 decides the goal, and the JSON result has the mode, the posts and the decision."""
    voted: list[bool] = []

    def voter(system, messages, tools):
        if not voted and any(MARK in t for t in texts(messages)):
            voted.append(True)
            return call("vote", target="#1", value="like")
        return call("wait")

    conn = await launch(voter)
    sid = (await conn.manager.create("Poem", "Write a poem", start=True,
                                     overrides={"swarm": {"mode": "thread"}}))["id"]
    code, out, err = await run(conn, f"Use the word {MARK}\nIn the second line.", session=sid,
                               output_format="json")
    assert code == 0, err
    result = json.loads(out)
    assert result["status"] == "done" and result["mode"] == "thread" and result["tasks"] == []
    assert list(result)[-3:] == ["mode", "posts", "decision"]
    assert result["decision"]["rule"] == "democracy" and result["decision"]["for"] == result["agents"]
    (post,) = [p for p in result["posts"] if p["kind"] == "post"]
    assert post["author"] == USER and post["title"] == f"Use the word {MARK}" and "text" not in post
    assert [p["id"] for p in result["posts"]][:2] == [GOAL_POST, PLAN_POST]
    swarm = conn.manager.swarms[sid]
    assert swarm.forum.get(post["id"])["text"] == "In the second line."


# ---------------------------------------------------------------------------------------------- requests and answers
async def test_automatic_answers_write_plan_without_the_clarify_line(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path, swarm={"clarify": True})))
    try:
        status, answer = await hub.create({"goal": "Write a haiku", "mode": "thread", "stop_when_done": False})
        assert status == 202 and answer["session"]["answers"] == "auto", answer
        await hub.settled()
        swarm = hub.manager.swarms[answer["session"]["name"]]
        assert swarm.forum.plan_data() == {"clarify": False, "asker": None}
        clarify_line = forum.t("en", "post_plan_clarify")
        assert clarify_line and clarify_line not in swarm.forum.get(PLAN_POST)["text"]
    finally:
        await hub.close()


async def test_the_clarify_asker_of_a_thread_session_asks_a_question_request(tmp_path, api_token):
    hub = Hub(_manager(_home(tmp_path)))
    try:
        uid, sid, swarm = await running(hub, clarify=True)
        asker = swarm.forum.plan_data()["asker"]
        assert asker == llm_names(swarm)[0]
        base = f"/api/v1/sessions/{uid}"
        assert (await hub.call("GET", f"{base}/requests"))[1] == {"requests": []}
        asked = swarm.mail.send(asker, [USER], "Who reads the haiku?")
        status, answer = await hub.call("GET", f"{base}/requests")
        assert status == 200, answer
        (request,) = answer["requests"]
        assert request["id"] == f"q{asked['id']}" and request["kind"] == "question" and request["from"] == asker
        assert request["pauses_swarm"] is False and request["text"] == "Who reads the haiku?"
        session = await summary_of(hub, uid)
        assert session["state"] == "waiting" and session["requests"] == 1
        assert (await hub.call("POST", f"{base}/stop"))[0] == 200
        assert sid not in hub.manager.swarms   # (read from its file: the thread database's #2)
        status, answer = await hub.call("GET", f"{base}/requests")
        assert status == 200 and [r["id"] for r in answer["requests"]] == [request["id"]], answer
        assert (await summary_of(hub, uid))["requests"] == 1
    finally:
        await hub.close()
