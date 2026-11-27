"""The thread tool set (docs/SPEC.md §22.5): the registry's modes and variants, the refusal of a tool of the other
mode on every caller's path, the forum tools' id helpers and results, and the thread texts of shared tools. The forum
tools run against `TableForum`, a small stand-in over the real forum tables with the method names and dict shapes of
§22.2, so these tests check the tool layer only (the real Forum has its own tests)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from dreamteam.config import Config
from dreamteam.forum import Forum
from dreamteam.hub_mcp import hub_specs, list_hub_tools
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentKind, ToolError
from dreamteam.swarm import Swarm
from dreamteam.tools import REGISTRY, ToolContext, call_tool, prepare_args, schemas_for, tool, tools_for
from dreamteam.tools import forum_tools, frozen, swarm_tools
from dreamteam.tools.forum_tools import _comment_id, _post_target, _sub_id, _vote_target

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

BOARD_ONLY = ["board", "task_get", "task_create", "task_claim", "task_update", "task_complete", "task_review",
              "task_comment", "task_discussion", "goal_complete"]
FORUM = ["feed", "post_read", "post_create", "post_update", "comment", "vote", "subscriptions", "subscribe",
         "unsubscribe"]
BOARD_WORDS = ("task", "board", "todo", "assign", "задач", "доск", "назнач")


def make_config(tmp_path: Path, mode: str = "thread", language: str = "en") -> Config:
    raw: dict[str, Any] = {
        "swarm": {"name": "Thread tools", "language": language, "start_agents": 2, "max_agents": 6,
                  "max_cost_usd": 0, "mode": mode},
        "agents": {"stagger_seconds": 0, "max_steps_per_turn": 10},
        "workspace": {"git": False},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    return Config.from_dict(tmp_path / "project", raw)


class TableForum:
    """The §22.2 API over the forum tables, as far as the tools use it (no events, no delivery, no goal rules)."""

    def __init__(self, swarm: Swarm) -> None:
        self.swarm, self.db = swarm, swarm.db
        self.similar: list[dict] = []
        self.tally: dict | None = None
        self.decided = False

    def __getattr__(self, name: str) -> Any:
        """What the swarm itself calls (auto_subscribe, ensure_system_posts, reopen, ...) is the real Forum's."""
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(Forum(self.swarm), name)

    def _post(self, row) -> dict:
        post = {k: row[k] for k in ("id", "kind", "title", "text", "author", "created_at", "edited_at", "active_at")}
        post["likes"], post["dislikes"] = self._counts(f"p{row['id']}")
        post["comments"] = int(self.db.scalar("SELECT COUNT(*) FROM post_comments WHERE post_id = ?", (row["id"],)))
        return post

    def _counts(self, target: str) -> tuple[int, int]:
        rows = self.db.all("SELECT value FROM votes WHERE target = ?", (target,))
        return sum(1 for r in rows if r["value"] > 0), sum(1 for r in rows if r["value"] < 0)

    def get(self, post_id: int) -> dict:
        row = self.db.one("SELECT * FROM posts WHERE id = ?", (post_id,))
        if row is None:
            raise ToolError(f"No post #{post_id}")
        return self._post(row)

    def feed(self, *, by=None, since=None, query=None, limit=30) -> list[dict]:
        rows = sorted(self.db.all("SELECT * FROM posts"), key=lambda r: (
            r["id"] not in (1, 2), r["id"] if r["id"] in (1, 2) else -r["active_at"], -r["id"]))
        posts = [self._post(r) for r in rows if (by is None or r["author"] == by)
                 and (since is None or r["active_at"] >= since)
                 and (query is None or query.casefold() in (r["title"] + r["text"]).casefold())]
        return posts[:limit]

    def create(self, actor: str, title: str, text: str, *, post_id: int | None = None, kind: str = "post") -> dict:
        now = time.time()
        cur = self.db.execute("INSERT INTO posts(id, author, kind, title, text, created_at, active_at) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?)", (post_id, actor, kind, title.strip(), text.strip(), now,
                                                               now))
        return self.get(cur.lastrowid if post_id is None else post_id)

    def update(self, actor: str, post_id: int, *, title=None, text=None) -> dict:
        if title is None and text is None:
            raise ToolError("Pass title or text.")
        old = self.get(post_id)
        new = {"title": title.strip() if title is not None else old["title"],
               "text": text.strip() if text is not None else old["text"]}
        if new["title"] != old["title"] or new["text"] != old["text"]:
            self.db.execute("UPDATE posts SET title = ?, text = ?, edited_at = ?, active_at = ? WHERE id = ?",
                            (new["title"], new["text"], time.time(), time.time(), post_id))
        return self.get(post_id)

    def feed_total(self, *, by=None, since=None, query=None) -> int:
        return len(self.feed(by=by, since=since, query=query, limit=10 ** 6))

    def counts(self) -> dict[str, int]:
        return {"posts": int(self.db.scalar("SELECT COUNT(*) FROM posts WHERE kind = 'post'")),
                "comments": int(self.db.scalar("SELECT COUNT(*) FROM post_comments"))}

    def comments(self, post_id: int, since_id: int = 0, limit: int = 200) -> list[dict]:
        rows = self.db.all("SELECT * FROM post_comments WHERE post_id = ? AND id > ? ORDER BY id LIMIT ?",
                           (post_id, since_id, limit))
        return [{**dict(r), "likes": self._counts(f"c{r['id']}")[0], "dislikes": self._counts(f"c{r['id']}")[1]}
                for r in rows]

    def comment(self, actor: str, post_id: int, text: str, reply_to: int | None = None) -> dict:
        self.get(post_id)
        cur = self.db.execute("INSERT INTO post_comments(post_id, author, text, reply_to, ts) VALUES (?, ?, ?, ?, ?)",
                              (post_id, actor, text.strip(), reply_to, time.time()))
        self.db.execute("UPDATE posts SET active_at = ? WHERE id = ?", (time.time() + cur.lastrowid, post_id))
        return self.comments(post_id, cur.lastrowid - 1, 1)[0]

    def vote(self, actor: str, target: str, value: str) -> dict:
        assert target[0] in "pc" and target[1:].isdigit(), target   # the tool passes the stored form
        if value == "none":
            self.db.execute("DELETE FROM votes WHERE target = ? AND voter = ?", (target, actor))
        else:
            self.db.execute("INSERT INTO votes(target, voter, value, ts) VALUES (?, ?, ?, ?) ON CONFLICT (target, "
                            "voter) DO UPDATE SET value = excluded.value",
                            (target, actor, 1 if value == "like" else -1, time.time()))
        likes, dislikes = self._counts(target)
        result = {"target": target, "value": value, "likes": likes, "dislikes": dislikes}
        if target == "p1":
            result.update(goal_vote=self.tally, decided=self.decided)
        return result

    def goal_vote(self) -> dict | None:
        return self.tally

    def similar_open(self, post: dict) -> list[dict]:
        return self.similar

    def subscriptions(self, member: str) -> list[dict]:
        rows = self.db.all("SELECT * FROM subscriptions WHERE member = ? ORDER BY id", (member,))
        return [{**{k: r[k] for k in ("id", "member", "kind", "target", "mode", "auto", "created_at")},
                 "by": r["by_member"]} for r in rows]

    def subscribe(self, actor, kind, target=None, by=None, mode="wake") -> dict:
        key = (actor, kind, target or "", by or "")
        row = self.db.one("SELECT * FROM subscriptions WHERE member = ? AND kind = ? AND target = ? AND by_member = ?",
                          key)
        if row is None:
            self.db.execute("INSERT INTO subscriptions(member, kind, target, by_member, mode, auto, created_at) "
                            "VALUES (?, ?, ?, ?, ?, 0, ?)", (*key, mode, time.time()))
            op = "created"
        elif row["mode"] != mode:
            self.db.execute("UPDATE subscriptions SET mode = ? WHERE id = ?", (mode, row["id"]))
            op = "changed"
        else:
            op = "unchanged"
        sub = next(s for s in self.subscriptions(actor) if (s["kind"], s["target"], s["by"]) == key[1:])
        return {**sub, "op": op}

    def unsubscribe(self, actor: str, subscription_id: int) -> None:
        sub = next((s for s in self.subscriptions(actor) if s["id"] == subscription_id), None)
        if sub is None:
            raise ToolError(f"No subscription s{subscription_id} of yours")
        self.db.execute("DELETE FROM subscriptions WHERE id = ?", (subscription_id,))

    def auto_rows(self, member: str) -> None:
        for kind, target in (("feed", ""), ("reactions", ""), ("post", "1"), ("post", "2")):
            self.db.execute("INSERT INTO subscriptions(member, kind, target, by_member, mode, auto, created_at) "
                            "VALUES (?, ?, ?, '', 'digest', 1, ?) ON CONFLICT DO NOTHING",
                            (member, kind, target, time.time()))


def _swarm(tmp_path: Path, mode: str, language: str = "en") -> Swarm:
    return Swarm(config=make_config(tmp_path / f"{mode}-{language}", mode, language),
                 llm_factory=lambda profile, agent: ScriptedLLM(lambda *a: "ok", profile))


@pytest.fixture
async def thread(tmp_path):
    sw = _swarm(tmp_path, "thread")
    await sw.start(run_agents=False)
    sw.forum = TableForum(sw)
    try:
        yield sw
    finally:
        await sw.stop()
        sw.close()


@pytest.fixture
async def board(tmp_path):
    sw = _swarm(tmp_path, "board")
    await sw.start(run_agents=False)
    try:
        yield sw
    finally:
        await sw.stop()
        sw.close()


async def run(sw: Swarm, actor: str, name: str, args: Any = None) -> tuple[str, bool]:
    return await call_tool(ToolContext(sw, actor), name, args)


async def ok(sw: Swarm, actor: str, name: str, args: Any = None) -> str:
    text, is_error = await run(sw, actor, name, args)
    assert not is_error, text
    return text


async def err(sw: Swarm, actor: str, name: str, args: Any = None) -> str:
    text, is_error = await run(sw, actor, name, args)
    assert is_error, text
    return text


def agents_of(sw: Swarm) -> list[str]:
    return [m["name"] for m in sw.members() if m["kind"] == AgentKind.LLM]


async def joined(sw: Swarm, name: str = "Claude Code") -> str:
    await ok(sw, USER, "join_swarm", {"role": "Reviewer", "name": name})
    return name


# ================================================================== the registry's modes
def test_modes_and_the_thread_tool_set():
    tools_for("llm")
    for name in BOARD_ONLY:
        assert REGISTRY[name].modes == {"board"}, name
    for name in FORUM:
        assert REGISTRY[name].modes == {"thread"}, name
    thread = [s.name for s in tools_for("llm", "thread")]
    assert thread == frozen.names(frozen.snapshot(6, "en"))
    assert [s.name for s in tools_for("llm", "board")] == frozen.names(frozen.snapshot(5, "en"))
    assert {s.name for s in tools_for("llm")} == set(thread) | {s.name for s in tools_for("llm", "board")}
    # the registry order of every board tool is what it was: the forum tools come last
    order = list(REGISTRY)
    assert order[-len(FORUM):] == FORUM
    assert {s.name for s in tools_for("user", "thread")} >= {"feed", "post_create", "swarm_control", "set_goal"}
    assert not {"subscriptions", "subscribe", "unsubscribe"} & {s.name for s in tools_for("user", "thread")}


def test_variants_fall_back_to_the_base_texts_and_schema():
    spec = REGISTRY["send_message"]
    assert spec.describe("en") == spec.describe("en", "board") == spec.description["en"]
    assert "About a specific post, prefer a comment on it" in spec.describe("en", "thread")
    assert "для вопроса только о своей работе" in spec.describe("ru", "thread")
    assert spec.parameters_for("thread") is spec.parameters  # texts only: the base schema
    assert spec.describe("de", "thread") == spec.describe("en", "thread")
    read = REGISTRY["read_file"]
    assert read.describe("ru", "thread") == read.describe("ru") and read.parameters_for("thread") is read.parameters
    control = REGISTRY["swarm_control"]
    assert control.parameters_for("board") is control.parameters
    assert "complete_goal" not in control.parameters["properties"]
    props = list(control.parameters_for("thread")["properties"])
    assert props == [*control.parameters["properties"], "complete_goal", "goal_summary"]
    traces = REGISTRY["agent_traces"]
    assert "task" in traces.parameters["properties"] and "task" not in traces.parameters_for("thread")["properties"]
    assert '(filter by agent or status, e.g. "failed")' in traces.describe("en", "thread")
    assert '(фильтры: agent или status, например "failed")' in traces.describe("ru", "thread")


def test_thread_variant_texts_name_no_board_tool():
    """The thread variants, frozen (revision 6) and live (wait_for_events, swarm_status, swarm_control,
    agent_traces), and the forum tools name no task, board or assignment; their Russian texts use е."""
    varied = {name for name, spec in REGISTRY.items() if spec.variants.get("thread") is not None}
    assert varied == {"send_message", "wait", "agents", "retire_agent", "mcp_create", "trigger_create",
                      "wait_for_events", "swarm_status", "swarm_control", "agent_traces"}
    for name in varied | set(FORUM):
        spec = REGISTRY[name]
        for lang in ("en", "ru"):
            text = spec.describe(lang, "thread")
            low = text.lower()
            assert not any(word in low for word in BOARD_WORDS), (name, lang, text)
            assert lang == "en" or "\u0451" not in text, name


def test_a_thread_variant_needs_the_thread_mode():
    with pytest.raises(AssertionError):
        tool("probe_variant", en="x", ru="y", modes=("board",), thread={"en": "a", "ru": "b"})
    with pytest.raises(AssertionError):  # a variant's required must be among its properties
        tool("probe_variant", en="x", ru="y", properties={"a": {"type": "string"}}, required=["a"],
             thread={"properties": {"b": {"type": "string"}}})
    assert "probe_variant" not in REGISTRY


def test_schemas_for_takes_the_mode():
    board = schemas_for("llm", "en")
    assert board == schemas_for("llm", "en", "board")
    thread = {s["name"]: s for s in schemas_for("llm", "ru", "thread")}
    assert set(thread) == set(frozen.names(frozen.snapshot(6, "ru")))
    assert thread["mcp_create"]["parameters"]["properties"]["scope"]["enum"] == ["swarm", "private"]
    thread["mcp_create"]["parameters"]["properties"]["injected"] = {}
    assert "injected" not in REGISTRY["mcp_create"].parameters_for("thread")["properties"]  # copies


def test_prepare_args_validates_the_mode_schema():
    spec = REGISTRY["mcp_create"]
    args = {"name": "x", "description": "d", "code": "c", "scope": "task", "task_id": 3}
    assert prepare_args(spec, args)["scope"] == "task"
    with pytest.raises(ToolError, match="Argument 'scope' must be one of: swarm, private"):
        prepare_args(spec, args, mode="thread")
    assert "task_id" not in prepare_args(spec, {**args, "scope": "private"}, mode="thread")


# ================================================================== the other mode's tools
async def test_board_tools_are_refused_in_a_thread_session(thread):
    member = await joined(thread)
    for actor in (USER, member):
        for name in BOARD_ONLY:
            text = await err(thread, actor, name, {"task_id": 1})
            assert text.startswith(f"Tool '{name}' is for board-mode sessions; this session works in the thread "
                                   "mode (no tasks): "), text
            if name == "goal_complete":
                assert text.endswith("the goal is decided by the votes on #1 (vote target=#1 value=like), and the "
                                     "user may complete it with swarm_control complete_goal=true.")
            else:
                assert text.endswith("post to the feed with post_create.")
    assert thread.db.scalar("SELECT COUNT(*) FROM tasks") == 0


async def test_forum_tools_are_refused_in_a_board_session(board):
    member = await joined(board)
    nearest = {"post_create": "task_create", "post_update": "task_create", "comment": "task_comment"}
    for actor in (USER, member):
        for name in FORUM:
            text = await err(board, actor, name, {"post_id": 1})
            assert text == (f"Tool '{name}' is for thread-mode sessions; this session uses the task board: "
                            f"{nearest.get(name, 'board')}."), text


async def test_the_refusal_is_in_the_callers_language(tmp_path):
    sw = _swarm(tmp_path, "thread", "ru")
    await sw.start(run_agents=False)
    try:
        text = await err(sw, USER, "task_create", {"title": "x"})
        assert text == ("Инструмент 'task_create' — для сессий с доской задач; эта сессия работает в режиме треда "
                        "(задач нет): публикуй в ленту через post_create.")
        text = await err(sw, USER, "goal_complete", {"summary": "x"})
        assert text.endswith("о цели решают голоса на #1 (vote target=#1 value=like), а пользователь может завершить "
                             "ее через swarm_control complete_goal=true.")
    finally:
        await sw.stop()
        sw.close()


async def test_has_tool_and_unknown_tool_follow_the_mode(thread, board):
    member = await joined(thread)
    assert ToolContext(thread, USER).mode == "thread" and ToolContext(board, USER).mode == "board"
    assert ToolContext(thread, member).has_tool("post_create") and not ToolContext(thread, member).has_tool("board")
    assert ToolContext(board, USER).has_tool("board") and not ToolContext(board, USER).has_tool("feed")
    text = await err(thread, member, "post_creat")
    assert "Did you mean: post_create" in text and "task_create" not in text
    text = await err(board, USER, "task_creat")
    assert "feed" not in text.split("Available tools: ", 1)[1].split(", ")
    assert frozen.member_tool_names(thread, thread.member(member)) == {s.name for s in tools_for("external", "thread")}


async def test_an_agent_of_a_thread_session_freezes_revision_6(thread):
    from dreamteam.agent import AgentRuntime

    agent = agents_of(thread)[0]
    runtime = AgentRuntime(thread, agent)
    record = frozen.read(thread.db, agent)
    assert runtime.tools_rev == 6 and record["mode"] == "thread"
    assert dumps_equal(record["tools"], frozen.snapshot(6, "en"))
    assert "post_create" in runtime.tool_names and "task_create" not in runtime.tool_names
    # a board tool is unknown to it: its frozen set has only its mode's tools
    text, is_error = await call_tool(ToolContext(thread, agent, runtime), "task_create", {"title": "x"})
    assert is_error and text.startswith("Unknown tool 'task_create'.")
    assert frozen.member_tool_names(thread, thread.member(agents_of(thread)[1])) == set(runtime.tool_names)


def dumps_equal(a: Any, b: Any) -> bool:
    from dreamteam.db import dumps

    return dumps(a) == dumps(b)


async def test_mcp_create_has_no_task_scope_in_a_thread_session(thread):
    member = await joined(thread)
    args = {"name": "x_tools", "description": "d", "code": "c", "scope": "task", "task_id": 3}
    text = await err(thread, member, "mcp_create", args)
    assert "Argument 'scope' must be one of: swarm, private" in text
    assert "[Ignored unknown argument(s): task_id." in text
    ctx = ToolContext(thread, member)
    with pytest.raises(ToolError, match="scope=task is for board sessions"):  # the handler's own guard
        await REGISTRY["mcp_create"].handler(ctx, args)


async def test_swarm_control_complete_goal_is_thread_only(board, thread):
    text = await err(board, USER, "swarm_control", {"complete_goal": True, "goal_summary": "x"})
    assert text.startswith("Error: Nothing to do: pass action (pause/resume), wrap_up, a limit or reopen_goal_comment.")
    assert "[Ignored unknown argument(s): complete_goal, goal_summary." in text
    text = await err(thread, USER, "swarm_control", {"complete_goal": True, "reopen_goal_comment": "again"})
    assert text == "Error: Pass either complete_goal or reopen_goal_comment, not both."
    text = await err(thread, USER, "swarm_control", {"goal_summary": "x", "max_agents": 3})
    assert text == "Error: goal_summary goes with complete_goal."
    assert thread.limits()["max_agents"] == 6  # checked first: nothing applied
    text = await err(thread, USER, "swarm_control", {})
    assert text == ("Error: Nothing to do: pass action (pause/resume), wrap_up, a limit, reopen_goal_comment or "
                    "complete_goal.")

    calls = []

    def complete_goal_by_user(summary: str = "") -> dict:
        calls.append(summary)
        if len(calls) > 1:
            raise ToolError("The global goal is already marked as achieved.")
        return {}

    thread.complete_goal_by_user = complete_goal_by_user
    text = await ok(thread, USER, "swarm_control", {"complete_goal": True, "goal_summary": " Done well "})
    assert text == "Done: goal marked reached." and calls == [" Done well "]
    text = await err(thread, USER, "swarm_control", {"complete_goal": True, "max_agents": 4})
    assert text == ("Error: The global goal is already marked as achieved (limits: no budget limit, at most 4 AI "
                    "agents applied).")


async def test_hub_listing_follows_the_mode(board, thread):
    names = {t.name: t for t in list_hub_tools(board)}
    spec = REGISTRY["swarm_control"]
    schema = dict(names["swarm_control"].input_schema)
    schema["properties"] = {k: v for k, v in schema["properties"].items() if k != "as_agent"}
    assert schema == spec.parameters_for("board") and not set(FORUM) & set(names)
    listed = {t.name: t for t in list_hub_tools(thread)}
    assert set(FORUM) <= set(listed) and not set(BOARD_ONLY) & set(listed)
    assert "complete_goal" in listed["swarm_control"].input_schema["properties"]
    assert listed["wait_for_events"].description.startswith(REGISTRY["wait_for_events"].describe("en", "thread"))
    assert {s.name for s in hub_specs()} == {s.name for s in hub_specs("board")} | {s.name for s in hub_specs("thread")}


# ================================================================== ids
def test_id_helpers():
    for value in (31, "31", "c31", "C31", " c31 "):
        assert _comment_id(value) == 31, value
    for bad in ("#31", "x", "", "c", 0, "c0", "p31"):
        with pytest.raises(ToolError, match="reply_to must be a comment id such as c31"):
            _comment_id(bad)
    with pytest.raises(ToolError, match="since_id must be a comment id such as c31"):
        _comment_id("y", "since_id")
    assert [_vote_target(v) for v in ("#5", "p5", "P5", " # 5", "c31", "C31")] == ["p5"] * 4 + ["c31"] * 2
    for bad in ("5", 5, "x5", "#", "#0", ""):
        with pytest.raises(ToolError, match="target must be '#5' for a post or 'c31' for a comment"):
            _vote_target(bad)
    assert [_sub_id(v) for v in (12, "12", "s12", "S12")] == [12] * 4
    for bad in ("x", "c12", 0, ""):
        with pytest.raises(ToolError, match="subscription_id must be an id such as s12"):
            _sub_id(bad)
    assert [_post_target(v) for v in ("#9", "9", "p9", " 9 ")] == ["9"] * 4 and _post_target("src/a") == "src/a"


# ================================================================== the forum tools' results
async def test_post_create_update_and_similar_posts(thread):
    anna = agents_of(thread)[0]
    text = await ok(thread, anna, "post_create", {"title": "Parser benchmark", "text": ""})
    assert text == 'Posted #3 "Parser benchmark".'
    thread.forum.similar = [thread.forum.get(3)]
    bob = agents_of(thread)[1]
    text = await ok(thread, bob, "post_create", {"title": "Parser benchmarks", "text": "numbers"})
    assert text == (f'Posted #4 "Parser benchmarks". Similar posts: #3 "Parser benchmark" ({anna}). If yours repeats '
                    "one, comment there instead.")
    assert await ok(thread, USER, "post_create", {"title": "From the user", "text": "x"}) == (
        'Posted #5 "From the user".')
    assert await ok(thread, anna, "post_update", {"post_id": "#3", "title": "Parser benchmark"}) == "#3 is unchanged."
    text = await ok(thread, anna, "post_update", {"post_id": 3, "text": "Results", "title": ""})
    assert text == 'Updated #3 "Parser benchmark" (text).'
    assert await err(thread, anna, "post_update", {"post_id": 3}) == "Error: Pass title or text."
    assert await err(thread, anna, "post_read", {"post_id": 99}) == "Error: No post #99"


async def test_post_create_similar_posts_in_russian(tmp_path):
    sw = _swarm(tmp_path, "thread", "ru")
    await sw.start(run_agents=False)
    sw.forum = TableForum(sw)
    try:
        anna = agents_of(sw)[0]
        await ok(sw, anna, "post_create", {"title": "Бенчмарк", "text": ""})
        sw.forum.similar = [sw.forum.get(3)]
        text = await ok(sw, agents_of(sw)[1], "post_create", {"title": "Бенчмарки", "text": ""})
        assert text.endswith(f"Похожие объявления: #3 «Бенчмарк» ({anna}). Если твое повторяет одно из них, напиши "
                             "комментарий там.")
    finally:
        await sw.stop()
        sw.close()


async def test_feed_lines_filters_and_more(thread):
    anna, bob = agents_of(thread)
    forum = thread.forum
    forum.create(None, "Goal reached?", "**Goal:** g", post_id=1, kind="goal")
    forum.create(None, "Let's plan the work", "plan", post_id=2, kind="plan")
    forum.tally = {"rule": "democracy", "counted": 2, "for": [bob], "against": [], "needed": 2, "met": False}
    for i in range(4):
        forum.create(anna if i % 2 else bob, f"Post {i}", "the parser" if i == 1 else "x")
    await ok(thread, anna, "vote", {"target": "#4", "value": "like"})
    await ok(thread, anna, "comment", {"post_id": 4, "text": "nice"})
    text = await ok(thread, anna, "feed", {"limit": 4})
    lines = text.splitlines()
    assert lines[0] == "Feed: 6 posts, newest activity first."
    assert lines[1].startswith('#1 "Goal reached?" — [system], likes 0, dislikes 0, comments 0, active ')
    assert lines[2] == "  Goal vote (#1, democracy): 1 of 2 for, 0 against; needs 2."
    assert lines[3].startswith('#2 "Let\'s plan the work" — [system]')
    assert lines[4].startswith(f'#4 "Post 1" — {anna}, likes 1, dislikes 0, comments 1, active ')  # commented last
    assert lines[4].endswith("; you: like") and lines[5].startswith(f'#6 "Post 3" — {anna}, likes 0')
    assert lines[-1] == "… and 2 more: `feed` with limit, by, since or query."
    text = await ok(thread, anna, "feed", {"by": bob, "since": 30, "query": "PARSER"})
    assert text == "Feed: no posts; by " + bob + ', active in the last 30 min, matching "PARSER".'
    text = await ok(thread, bob, "feed", {"by": anna, "query": "PARSER"})
    assert text.splitlines()[0] == f'Feed: 1 post, newest activity first; by {anna}, matching "PARSER".'
    assert "comments 1" in text and "; you:" not in text
    text = await ok(thread, anna, "feed", {"query": "Post 2"})
    assert "; you:" not in text
    assert "#4 \"Post 1\"" in await ok(thread, bob, "feed", {"query": "parser"})
    assert (await ok(thread, anna, "feed", {})).count("; you: like") == 1
    assert await err(thread, anna, "feed", {"since": 0}) == "Error: since must be a number of minutes > 0"


async def test_post_read_tree_votes_and_since_id(thread):
    anna, bob = agents_of(thread)
    member = await joined(thread)
    forum = thread.forum
    forum.create(anna, "Parser benchmark", "The numbers.")
    for i in range(3):
        await ok(thread, bob if i != 1 else member, "comment", {"post_id": 3, "text": f"comment {i}"})
    text = await ok(thread, anna, "comment", {"post_id": 3, "text": "an answer\nsecond line", "reply_to": "c1"})
    assert text == f'Replied c4 to c1 ({bob}) on #3 "Parser benchmark".'
    assert await ok(thread, anna, "comment", {"post_id": 3, "text": "more", "reply_to": ""}) == \
        'Commented c5 on #3 "Parser benchmark".'
    await ok(thread, bob, "vote", {"target": "p3", "value": "like"})
    await ok(thread, member, "vote", {"target": "#3", "value": "dislike"})
    await ok(thread, bob, "vote", {"target": "c4", "value": "like"})
    text = await ok(thread, bob, "post_read", {"post_id": 3})
    lines = text.splitlines()
    assert lines[0].startswith(f'#3 "Parser benchmark" by {anna}, ')
    assert lines[0].endswith(f"; likes 1 ({bob}), dislikes 1 ({member}); you: like")
    assert lines[1] == "The numbers." and lines[2] == "Comments (5):"
    assert lines[3].startswith(f"c1 {bob}, ") and lines[3].endswith(", likes 0, dislikes 0: comment 0")
    assert lines[4].startswith(f"  c4 {anna}, ") and lines[4].endswith(", likes 1, dislikes 0, you: like: an answer")
    assert lines[5] == "    second line"
    assert lines[6].startswith(f"c2 {member}, ")
    text = await ok(thread, bob, "post_read", {"post_id": 3, "since_id": "c3"})
    lines = text.splitlines()
    # Bob has read the text as it is: a page after since_id shows only its head (docs/SPEC.md §22.11)
    assert lines[1] == "The numbers." and lines[2] == "[text unchanged since you read it: 12 characters]"
    assert lines[3] == "Comments after c3 (2 of 5):"
    assert lines[4].startswith(f"c4 {anna}, ") and ", reply to c1: an answer" in lines[4]
    assert await err(thread, bob, "post_read", {"post_id": 3, "since_id": "c99"}) == "Error: No comment c99 on #3"
    assert await err(thread, bob, "post_read", {"post_id": 3, "since_id": "#2"}) == \
        "Error: since_id must be a comment id such as c31"
    forum.create(anna, "Empty", "")
    assert (await ok(thread, bob, "post_read", {"post_id": 4})).splitlines()[1] == "No comments yet."


async def test_post_read_caps_the_comments(thread, monkeypatch):
    monkeypatch.setattr(forum_tools, "COMMENTS_MAX", 2)
    anna, bob = agents_of(thread)
    thread.forum.create(anna, "Many", "")
    for i in range(5):
        thread.forum.comment(bob, 3, f"c {i}")
    text = await ok(thread, anna, "post_read", {"post_id": 3})
    assert text.splitlines()[-1] == "… and 3 more: `post_read` post_id=3 since_id=c2."


async def test_goal_post_shows_the_vote(thread):
    anna, bob = agents_of(thread)
    member = await joined(thread)
    forum = thread.forum
    forum.create(None, "Goal reached?", "**Goal:** ship it", post_id=1, kind="goal")
    forum.tally = {"rule": "veto", "counted": 2, "for": [anna], "against": [bob], "needed": 2, "met": False}
    for voter, value in ((anna, "like"), (bob, "dislike"), (member, "like"), (USER, "dislike")):
        await ok(thread, voter, "vote", {"target": "#1", "value": value})
    lines = (await ok(thread, anna, "post_read", {"post_id": 1})).splitlines()
    assert lines[0].startswith('#1 "Goal reached?" by [system], ')
    assert lines[0].endswith("likes 2, dislikes 2; you: like")
    assert lines[1] == "**Goal:** ship it"
    assert lines[2] == (f"Goal vote (#1, veto): 1 of 2 for, 1 against; needs all 2 and none against. For: {anna}; "
                        f"against: {bob}.")
    assert lines[3] == f"Not counted: {member} (like), user (dislike)."


async def test_vote_results(thread):
    anna, bob = agents_of(thread)
    member = await joined(thread)
    forum = thread.forum
    forum.create(anna, "Mine", "")
    forum.comment(anna, 3, "a comment")
    assert await ok(thread, bob, "vote", {"target": "#3", "value": "like"}) == "Liked #3 (likes 1, dislikes 0)."
    assert await ok(thread, bob, "vote", {"target": "p3", "value": "like"}) == (
        "You already like #3 (likes 1, dislikes 0).")
    assert await ok(thread, bob, "vote", {"target": "c1", "value": "dislike"}) == \
        "Disliked c1 on #3 (likes 0, dislikes 1)."
    assert await ok(thread, bob, "vote", {"target": "c1", "value": "dislike"}) == \
        "You already dislike c1 on #3 (likes 0, dislikes 1)."
    assert await ok(thread, bob, "vote", {"target": "#3", "value": "none"}) == \
        "Withdrew your vote on #3 (likes 0, dislikes 0)."
    assert await ok(thread, bob, "vote", {"target": "#3", "value": "none"}) == "You have no vote on #3."
    assert "must be '#5' for a post" in await err(thread, bob, "vote", {"target": 3, "value": "like"})
    forum.create(None, "Goal reached?", "g", post_id=1, kind="goal")
    forum.tally = {"rule": "democracy", "counted": 2, "for": [anna, bob], "against": [], "needed": 2, "met": True}
    forum.decided = True
    text = await ok(thread, bob, "vote", {"target": "#1", "value": "like"})
    assert text == ("Liked #1 (likes 1, dislikes 0).\nGoal vote (#1, democracy): 2 of 2 for, 0 against; needs 2. The "
                    "goal is reached by the vote.")
    forum.decided = False
    text = await ok(thread, member, "vote", {"target": "#1", "value": "like"})
    assert text.endswith("needs 2. Your vote is shown, not counted: only the swarm's AI agents' votes count.")
    thread.db.set("wrap_up", {"on": True})
    text = await ok(thread, USER, "vote", {"target": "#1", "value": "dislike"})
    assert text.endswith('needs 2. (Wrap-up mode: a like on #1 means "finish with what we have".) Your vote is shown, '
                         "not counted: only the swarm's AI agents' votes count.")


async def test_subscription_results(thread):
    anna, bob = agents_of(thread)
    forum = thread.forum
    # the starting team's automatic rows (Swarm._create_member) go: anna's own come first here
    thread.db.execute("DELETE FROM subscriptions")
    thread.db.execute("DELETE FROM sqlite_sequence WHERE name = 'subscriptions'")
    forum.auto_rows(anna)
    forum.create(bob, "Parser", "")
    (Path(thread.workspace.root) / "src" / "parser").mkdir(parents=True)
    assert await ok(thread, anna, "subscribe", {"what": "post", "target": "#3"}) == \
        'Subscribed s5: comments and edits of #3 "Parser" by anyone, wake.'
    assert await ok(thread, anna, "subscribe", {"what": "post", "target": "p3", "mode": "digest"}) == \
        "s5 (comments and edits of #3): mode changed to digest."
    assert await ok(thread, anna, "subscribe", {"what": "post", "target": "3", "mode": "digest"}) == \
        "You already have s5 (comments and edits of #3), digest."
    assert await ok(thread, anna, "subscribe", {"what": "file", "target": "src/parser", "by": bob}) == \
        f"Subscribed s6: changes under src/parser/ by {bob}, wake."
    assert await ok(thread, anna, "subscribe", {"what": "reactions", "mode": "wake"}) == \
        "s2 (reactions to your posts and comments): mode changed to wake (votes always come in your digest)."
    assert await ok(thread, anna, "subscribe", {"what": "feed", "by": "user"}) == \
        "Subscribed s7: new posts by the user, wake."
    text = await ok(thread, anna, "subscriptions")
    lines = text.splitlines()
    assert lines[0] == "Your subscriptions (7):"
    assert lines[1] == "s1 feed: new posts by anyone, digest (automatic: it cannot be removed, only its mode changed)"
    assert lines[2] == ("s2 reactions: comments, replies, edits and votes on your posts and comments, by anyone, wake "
                        "(automatic)")
    assert lines[3] == "s3 post #1: comments and edits by anyone, digest (automatic)"  # no #1 before the first goal
    assert lines[5] == 's5 post #3 "Parser": comments and edits by anyone, digest'
    assert lines[6] == f"s6 file src/parser/: changes by {bob}, wake"
    assert lines[7] == "s7 feed: new posts by the user, wake"
    assert lines[8] == ("Digest items come with your next wake-up, and while you are idle at the latest about 1 min "
                        "after the oldest one; votes never wake.")
    await ok(thread, USER, "retire_agent", {"name": bob})
    assert f"s6 file src/parser/: changes by {bob} (a former member), wake" in await ok(thread, anna, "subscriptions")
    assert await ok(thread, anna, "unsubscribe", {"subscription_id": "s5"}) == \
        "Unsubscribed s5 (comments and edits of #3)."
    assert await err(thread, anna, "unsubscribe", {"subscription_id": 5}) == "Error: No subscription s5 of yours"
    assert await err(thread, anna, "unsubscribe", {"subscription_id": "c5"}) == \
        "Error: subscription_id must be an id such as s12"


# ================================================================== shared tools in a thread session
def test_progress_lines_of_a_thread_session():
    texts = swarm_tools._PROGRESS["en"]

    def line(kind: str, data: dict, actor: str | None = "Ann Lee", titles: dict | None = None) -> str | None:
        event = {"type": kind, "actor": actor, "data": data}
        return swarm_tools._progress_line(texts, event, "Me", "thread", "en", titles or {})

    post = {"id": 9, "title": "Old title", "author": "Ann Lee"}
    assert line("post.created", {"post": post}, titles={9: "Parser benchmark"}) == \
        '#9 "Parser benchmark": posted by Ann Lee'
    assert line("post.created", {"post": {**post, "author": None}}, actor=None) is None  # a system post
    comment = {"id": 31, "post_id": 9, "text": "x" * 100}
    text = line("post.comment", {"post_id": 9, "comment": comment}, actor="Bob Stone", titles={9: "Parser benchmark"})
    assert text == 'c31 on #9 "Parser benchmark" by Bob Stone: ' + "x" * 79 + "…"
    decided = {"goal": {"decision": {"rule": "democracy"}}}
    assert line("goal.completed", decided) == "goal reached by the vote (democracy)"
    assert line("goal.completed", {"goal": {"decision": {"rule": "user"}}}, actor=USER) == "goal completed by the user"
    assert line("goal.reopened", {}, actor=USER) == "goal reopened by user: the votes on #1 were cleared"
    assert line("swarm.wrap_up", {"on": True}, actor=USER) == ("wrap-up mode on (user): post your results and what is "
                                                               "left, stop jobs nobody needs, start nothing new, then "
                                                               "like #1")
    assert line("swarm.wrap_up", {"on": False}, actor=USER) == "wrap-up mode off (user)"
    assert line("post.created", {"post": post}, actor="Me") is None  # its own
    ru = swarm_tools._PROGRESS["ru"]
    event = {"type": "goal.completed", "actor": None, "data": decided}
    assert swarm_tools._progress_line(ru, event, "Me", "thread", "ru", {}) == (
        "цель достигнута голосованием (демократия)")
    for key in ("posted", "commented", "goal_done_vote", "goal_done_user", "goal_reopened_thread", "wrap_up_on_thread",
                "header_thread", "more_thread"):
        assert "\u0451" not in ru[key] and "board" not in swarm_tools._PROGRESS["en"][key], key
    assert set(swarm_tools.PROGRESS_TYPES) == {"board", "thread"} and "post.vote" not in swarm_tools.PROGRESS_TYPES[
        "thread"]


async def test_wait_for_events_progress_in_a_thread_session(thread):
    anna, bob = agents_of(thread)
    member = await joined(thread)
    forum = thread.forum
    forum.create(None, "Goal reached?", "g", post_id=1, kind="goal")
    forum.tally = {"rule": "democracy", "counted": 2, "for": [anna], "against": [], "needed": 2, "met": False}
    assert "No new events" in await ok(thread, member, "wait_for_events", {"timeout": 0, "progress": True})
    post = forum.create(anna, "Parser benchmark", "")
    thread.bus.emit("post.created", actor=anna, post=post)
    thread.bus.emit("post.vote", actor=anna, target="p1", voter=anna, value="like", likes=1, dislikes=0)
    text = await ok(thread, member, "wait_for_events", {"timeout": 0, "progress": True})
    lines = text.splitlines()
    assert "Feed:" in lines
    shown = lines[lines.index("Feed:") + 1:]
    assert shown[0].endswith(f'#3 "Parser benchmark": posted by {anna}') and shown[0].startswith("• ")
    assert shown[-1] == "• Goal vote (#1, democracy): 1 of 2 for, 0 against; needs 2."
    # votes alone: one vote line, on the timeout too
    thread.bus.emit("post.vote", actor=bob, target="p1", voter=bob, value="like", likes=2, dislikes=0)
    text = await ok(thread, member, "wait_for_events", {"timeout": 0, "progress": True})
    assert text.splitlines()[-2:] == ["Feed:", "• Goal vote (#1, democracy): 1 of 2 for, 0 against; needs 2."]
    assert "No new events" in await ok(thread, member, "wait_for_events", {"timeout": 0, "progress": True})


async def test_shared_tool_results_in_a_thread_session(thread):
    anna = agents_of(thread)[0]
    text = await ok(thread, anna, "agents")
    assert "tasks:" not in text and anna in text
    text = await ok(thread, anna, "hire_agent", {"role": "Researcher", "instructions": "Look things up"})
    assert text.endswith("They are woken now: they will read the feed and #2; no welcome message is needed.")
    assert "task" not in text.lower()
    text = await ok(thread, USER, "join_swarm", {"role": "Reviewer", "name": "Claude Code"})
    assert "Read the feed (`feed`, `post_read`), post with `post_create`" in text and "about 1 min after" in text
    assert "board" not in text and "task" not in text
    assert "Your posts: none." in await ok(thread, "Claude Code", "swarm_status")
    thread.forum.create("Claude Code", "Mine", "")
    thread.forum.comment(anna, 3, "ok")
    text = await ok(thread, "Claude Code", "swarm_status")
    assert "Feed: 1 post, 1 comment." in text and "Tasks:" not in text
    assert "You are Claude Code. Your posts: #3 (likes 0, dislikes 0, comments 1)." in text
    thread.db.set("wrap_up", {"on": True})
    assert 'a like on #1 now means "finish with what we have"' in await ok(thread, USER, "swarm_status")


async def test_board_session_results_are_unchanged(board):
    agent = agents_of(board)[0]
    assert "tasks: none" in await ok(board, agent, "agents")
    text = await ok(board, agent, "hire_agent", {"role": "Researcher", "instructions": "Look things up"})
    assert text.endswith("assign them a task (task_update assignee) or leave ready tasks for them to claim; no welcome "
                         "message is needed.")
    member = await joined(board)
    status = await ok(board, member, "swarm_status")
    assert "Tasks: none" in status and "Your open tasks: none" in status and "Feed:" not in status


async def test_the_forum_tools_on_the_real_forum(tmp_path):
    """The tools against swarm.forum itself (forum.py), for the calls whose shapes the tools rely on."""
    sw = _swarm(tmp_path, "thread")
    await sw.start(run_agents=False)
    try:
        anna, bob = agents_of(sw)
        assert await ok(sw, anna, "post_create", {"title": "Parser benchmark", "text": "First paragraph."}) == \
            'Posted #3 "Parser benchmark".'
        await ok(sw, bob, "post_read", {"post_id": 3})   # read before write (docs/SPEC.md §22.11)
        assert await ok(sw, bob, "comment", {"post_id": "#3", "text": "Looks good"}) == \
            'Commented c1 on #3 "Parser benchmark".'
        await ok(sw, anna, "post_read", {"post_id": 3})
        assert await ok(sw, anna, "comment", {"post_id": 3, "text": "Thanks", "reply_to": "c1"}) == \
            f'Replied c2 to c1 ({bob}) on #3 "Parser benchmark".'
        assert await ok(sw, bob, "vote", {"target": "#3", "value": "like"}) == "Liked #3 (likes 1, dislikes 0)."
        assert await ok(sw, bob, "vote", {"target": "#3", "value": "like"}) == \
            "You already like #3 (likes 1, dislikes 0)."
        assert await ok(sw, anna, "vote", {"target": "c1", "value": "dislike"}) == \
            "Disliked c1 on #3 (likes 0, dislikes 1)."
        assert "You cannot vote on your own post." in await err(sw, anna, "vote", {"target": "p3", "value": "like"})
        assert await ok(sw, anna, "post_update", {"post_id": 3, "title": "Parser benchmark"}) == "#3 is unchanged."
        assert await ok(sw, anna, "post_update", {"post_id": 3, "text": "New text."}) == \
            'Updated #3 "Parser benchmark" (text).'
        feed = await ok(sw, bob, "feed", {})
        assert feed.splitlines()[0] == "Feed: 1 post, newest activity first."
        assert "comments 2" in feed and feed.endswith("; you: like")
        read = (await ok(sw, bob, "post_read", {"post_id": 3, "since_id": "c1"})).splitlines()
        assert read[2] == "Comments after c1 (1 of 2):" and ", reply to c1: Thanks" in read[3]
        created = await ok(sw, anna, "subscribe", {"what": "post", "target": "#3", "mode": "wake"})
        assert created.startswith("Subscribed s") and created.endswith(f'comments and edits of #3 "Parser benchmark" '
                                                                       "by anyone, wake.")
        listed = await ok(sw, anna, "subscriptions")
        assert listed.splitlines()[-1].startswith("Digest items come with your next wake-up")
        sub_id = created.split(":")[0].removeprefix("Subscribed ")
        assert await ok(sw, anna, "unsubscribe", {"subscription_id": sub_id}) == \
            f"Unsubscribed {sub_id} (comments and edits of #3)."
    finally:
        await sw.stop()
        sw.close()
