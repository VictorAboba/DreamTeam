"""eventtext.py: one plain line per journaled event, the `text` of the HTTP API's events (docs/SPEC.md §21.6), and the
texts and symbols it shares with the terminal UI (uitext.py, symbols.py)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from dreamteam import eventtext, prompts, symbols, uitext
from dreamteam.board import Board
from dreamteam.eventtext import QUOTE_CHARS, event_text
from dreamteam.models import USER
from dreamteam.tui import i18n, render, theme
from test_tui_render import ANNA, BORIS, NOW, activity, ev, fuzz_events, job, message, realistic_events, task

LANGS = ("ru", "en")
# what a line never holds: C0/C1 control characters (newlines and escapes among them), line and paragraph separators,
# the bidi embeddings, overrides, isolates and marks and the invisible spaces
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u061c\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064"
                      r"\u2066-\u2069\ufeff]")
_LIVE = ("trace.started", "trace.span", "trace.finished", "job.silent")   # never journaled, never sent
_CLIENT = ("sessions.changed", "_connection")                             # the terminal client's own pseudo events
JOURNALED = [t for t in render.EVENT_TYPES if t not in _LIVE + _CLIENT]
NO_LINE = ("agent.usage", "agent.compacted")                              # the journaled events no feed shows
# what a line naming a place on the hub's computer must never hold
PATH_PARTS = ("/home", "/opt", ":\\", ":/", "\\\\", ".dreamteam", "skill-envs", "alice", "Users")


def assert_one_clean_line(text: str) -> None:
    assert isinstance(text, str) and text, text
    assert text == text.strip() and "  " not in text, repr(text)
    assert not _CONTROL.search(text), repr(text)
    json.dumps({"text": text}, ensure_ascii=False).encode("utf-8")   # no lone surrogate: the event can be sent


# ---- the move out of the tui package ----------------------------------------------------------------
def test_the_feed_texts_and_symbols_live_outside_the_tui_package():
    """tui/i18n.py is uitext.py under its old name, tui/theme.py re-exports symbols.py, and render.py takes the pure
    helpers back from eventtext.py: one copy of each, so a change (or a test's monkeypatch) reaches both."""
    from dreamteam.tui.i18n import TEXTS, t

    assert i18n is uitext and TEXTS is uitext.TEXTS and t is uitext.t
    assert theme.SYMBOLS is symbols.SYMBOLS and theme.SYMBOLS_ASCII is symbols.SYMBOLS_ASCII
    assert theme.symbols is symbols.symbols and symbols.symbols(False) is symbols.SYMBOLS_ASCII
    for name in ("clean", "first_name", "fmt_cost", "fmt_elapsed", "tool_call_text", "_who", "_status_label",
                 "_job_result", "_via_text", "_deps_word", "_deps_reason", "_crew_size", "_clock", "_ANSI"):
        assert getattr(render, name) is getattr(eventtext, name), name
    assert render.TOOL_CALL_CHARS == eventtext.TOOL_CALL_CHARS == 80
    assert issubclass(render.RenderContext, eventtext.TextContext)
    c = render.RenderContext()
    assert (c.lang, c.verbosity, c.width, c.unicode, c.me, c.quiet, c.verbose) == \
        ("ru", "normal", 100, True, USER, False, False)
    assert c.sym is symbols.SYMBOLS and render.RenderContext(unicode=False).sym is symbols.SYMBOLS_ASCII
    assert eventtext.TextContext().lang == "en"
    # the subclass orders its fields differently: a positional call would bind the wrong one, so none is taken
    for cls in (render.RenderContext, eventtext.TextContext):
        with pytest.raises(TypeError):
            cls("en", "verbose")


def test_every_text_key_eventtext_uses_exists_in_both_languages():
    source = Path(eventtext.__file__).read_text(encoding="utf-8")
    keys = {k for k in re.findall(r"\.tn?\(\s*[\"']([a-z0-9_]+)[\"']", source) if not k.endswith("_")}  # not prefixes
    keys |= set(re.findall(r"\.tn?\(\s*\"[a-z0-9_]+\" if .+? else \"([a-z0-9_]+)\"", source))   # t("a" if x else "b")
    assert len(keys) > 50 and {"ev_crew_task", "ev_crew_on", "ev_crew_off", "ev_part_of", "ev_trigger_held",
                               "ev_trigger_held_for"} <= keys
    for lang in LANGS:
        assert not sorted(k for k in keys if k not in uitext.TEXTS[lang]), lang
    # the characters a line never holds are spelt as escapes here, never written as themselves
    for path in (Path(eventtext.__file__), Path(__file__)):
        assert not _CONTROL.search(re.sub(r"[\r\n]", "", path.read_text(encoding="utf-8"))), path


# ---- every event ----------------------------------------------------------------------------------
def test_every_server_type_has_its_own_handler():
    assert set(render.EVENT_TYPES) - {"trace.started", "trace.span", "trace.finished"} <= set(eventtext._HANDLERS)


@pytest.mark.parametrize("lang", LANGS)
def test_every_type_has_a_line_and_null_only_for_usage_and_compacted(lang):
    assert set(JOURNALED) | set(_LIVE) | set(_CLIENT) == set(render.EVENT_TYPES) and set(NO_LINE) < set(JOURNALED)

    def text_of(type_: str, data: dict | None) -> str | None:
        return event_text({"id": 1, "ts": NOW, "type": type_, "actor": ANNA, "data": data}, lang)

    for data in ({}, {"x": 1}, None):
        for type_ in JOURNALED:
            if type_ in NO_LINE:
                assert text_of(type_, data) is None, type_
            else:
                assert_one_clean_line(text_of(type_, data))
        for type_ in _LIVE:
            assert text_of(type_, data) is None, type_
        assert_one_clean_line(text_of("sessions.changed", data))
        assert text_of("_connection", data) is None   # the client's own notice: a line only for a lost connection
    for e in realistic_events():
        text = event_text(e, lang)
        if e["type"] in NO_LINE or e["type"] in _LIVE:
            assert text is None, e
        elif e["type"] in JOURNALED:
            assert_one_clean_line(text)


@pytest.mark.parametrize("me", [USER, ANNA])
@pytest.mark.parametrize("lang", LANGS + ("de", None))
def test_fuzz_never_raises_and_every_line_is_one_clean_line(lang, me):
    evil = ("hi\x1b]0;pwned\x07 \x1b[2J\x1b[31mred\x1b[0m \x9b31m \x07bell\rcr \ud800\u2028next\u2029para\x85\x00\tz"
            " \u202eevil\u200b\u2066x\u2069\u200f\ufeff\u061c\u2060")
    events = fuzz_events() + [
        ev("message.sent", ANNA, message=message(ANNA, ["user"], evil)),
        ev("message.sent", "user", message=message("user", [ANNA, BORIS, "x", "y"], evil)),
        ev("task.created", evil, task=task(3, evil, goal=evil, assignee=evil, crew=3, parent_id=evil)),
        ev("task.updated", ANNA, task=task(3, evil, status="done", result=evil), changes={"status": [None, "done"]}),
        ev("task.updated", ANNA, task={"status": ["x"], "id": {"a": 1}}, changes={"status": [None, ["x"]],
                                                                                "crew": [{}, []], "parent_id": 5}),
        ev("task.updated", ANNA, task=task(3), changes={"crew": "x", "parent_id": [None, True], 7: [1, 2]}),
        ev("notification", None, recipient=ANNA, kind="n_workspace_moved", text=5),
        ev("notification", None, recipient="user", kind=["u_budget"], text=evil),
        activity("tool_result", evil, tool=evil, is_error=True), activity("tool_call", evil, tool=evil),
        activity("wake", evil), activity("thinking", evil), activity("error", evil, agent=evil),
        {"type": {"a": 1}, "actor": {"b": 2}, "data": []}, {"type": "x" * 5, "actor": evil, "data": {}},
        {"type": "goal.set", "data": {"goal": {"title": evil, "status": ["pending"]}}},
    ]
    for e in events:
        text = event_text(e, lang, me=me)
        if text is not None:
            assert_one_clean_line(text)
            assert "pwned" not in text and "[2J" not in text
    for not_an_event in (None, "text", 5, [], ("type", "x")):
        assert event_text(not_an_event, lang, me=me) is None


def test_a_handler_that_fails_gives_the_generic_line(monkeypatch):
    def broken(ctx, ev_, d):
        raise RuntimeError("boom")

    monkeypatch.setitem(eventtext._HANDLERS, "task.created", broken)
    assert event_text(ev("task.created", ANNA, task=task(3))) == f"task.created · {ANNA}"
    assert event_text({"type": "task.created", "data": {}}) == "task.created"
    # the user is named as in every other line, however the event fails
    assert event_text(ev("task.created", "user", task=task(3)), "ru") == "task.created · вы"
    assert event_text(ev("task.created", BORIS, task=task(3)), me=BORIS) == "task.created · you"
    assert event_text(ev("task.created", "user\u202e", task=task(3))) == "task.created · user"
    assert event_text(ev("brand.new", ANNA, x=1)) == f"brand.new · {ANNA}"
    assert event_text(ev("brand.new", "user", x=1), "ru") == "brand.new · вы"
    assert event_text({"data": {}}) == "?"
    assert event_text(ev("goal.set", None, goal=None), lang=5) == "No goal yet"     # a lang that is no text: English


# ---- the lines ------------------------------------------------------------------------------------
def test_task_lines():
    def line(e, lang="en"):
        return event_text(e, lang)

    created = ev("task.created", ANNA, task=task(3, "Parser", goal="Parse the input", assignee=BORIS))
    assert line(created) == f"☐ #3 Parser — Борис · Parse the input · by {ANNA}"
    assert line(created, "ru") == f"☐ #3 Parser — Борис · Parse the input · автор: {ANNA}"
    assert line(ev("task.created", "user", task=task(3, "Parser", goal=""))) == "☐ #3 Parser · by you"
    claimed = ev("task.updated", ANNA, task=task(3, "Parser", "in_progress", assignee=ANNA),
                 changes={"status": ["todo", "in_progress"], "assignee": [None, ANNA]})
    assert line(claimed) == "◐ #3 Parser — Анна"   # the spec's example: the assignee by the first name, as the feed
    review = ev("task.updated", ANNA, task=task(3, "Parser", "review", assignee=ANNA, reviewer=BORIS),
                changes={"status": ["in_progress", "review"]})
    assert line(review) == "◑ #3 Parser — in review (Борис)"
    yours = ev("task.updated", ANNA, task=task(3, "Parser", "review", assignee=ANNA, reviewer="user"),
               changes={"status": ["in_progress", "review"]})
    assert line(yours) == "◑ #3 Parser — waits for your review"
    assert event_text(yours, "en", me=BORIS) == "◑ #3 Parser — in review (user)"
    done = ev("task.updated", BORIS, task=task(3, "Parser", "done", assignee=ANNA, result="Done: parser.py\nmore"),
              changes={"status": ["review", "done"]})
    assert line(done) == "☒ #3 Parser — Анна ⎿ Done: parser.py more"
    failed = ev("task.updated", ANNA, task=task(3, "Parser", "failed", assignee=ANNA, result="boom"),
                changes={"status": ["in_progress", "failed"]})
    assert line(failed) == "✗ #3 Parser — Анна · failed ⎿ boom"
    cancelled = ev("task.updated", "user", task=task(3, "Parser", "cancelled"),
                   changes={"status": ["todo", "cancelled"]})
    assert line(cancelled) == "✗ #3 Parser · cancelled"
    released = ev("task.updated", ANNA, task=task(3, "Parser"), changes={"status": ["in_progress", "todo"],
                                                                         "assignee": [ANNA, None]})
    assert line(released) == "☐ #3 Parser · back to do"
    assigned = ev("task.updated", "user", task=task(3, "Parser", assignee=ANNA), changes={"assignee": [None, ANNA]})
    assert line(assigned) == "☐ #3 Parser — Анна"
    minor = ev("task.updated", ANNA, task=task(3, "Parser", priority=3),
               changes={"priority": [1, 3], "tags": [[], ["x"]]})
    assert line(minor) == "✎ #3 Parser · updated: priority, tags"
    assert line(minor, "ru") == "✎ #3 Parser · изменена: приоритет, теги"
    comment = ev("task.comment", ANNA, task_id=3, post={"author": ANNA, "text": "Who takes it?\nAnyone?"})
    assert line(comment) == "💬 #3 Анна: Who takes it? Anyone?"
    long = ev("task.updated", ANNA, task=task(3, "Parser", "done", assignee=ANNA, result="word " * 200),
              changes={"status": ["review", "done"]})
    quoted = line(long).split(" ⎿ ", 1)[1]
    assert len(quoted) <= QUOTE_CHARS and quoted.endswith("…")


def test_crew_lines():
    """docs/SPEC.md §4.2 (0.2.0): a crew task says so when it is created, never with its working set; a subtask's lines
    say whose part they are; a change of the crew number is said in words."""
    parent = task(1, "Parser", goal="", assignee=ANNA, crew=3, crew_working=[ANNA, BORIS])
    created = event_text(ev("task.created", ANNA, task=parent))
    assert created == f"☐ #1 Parser — Анна · crew task, up to 3 members · by {ANNA}"
    assert event_text(ev("task.created", ANNA, task=parent), "ru") == \
        f"☐ #1 Parser — Анна · групповая задача, до 3 участников · автор: {ANNA}"
    claimed = ev("task.updated", ANNA, task={**parent, "status": "in_progress"},
                 changes={"status": ["todo", "in_progress"]})
    assert event_text(claimed) == "◐ #1 Parser — Анна"   # the crew_working of the event's copy is never shown
    for e in (created, event_text(claimed)):
        assert "2/3" not in e and "working" not in e
    direction = task(2, "Lexer", goal="", parent_id=1)
    assert event_text(ev("task.created", ANNA, task=direction)) == f"☐ #2 Lexer · part of #1 · by {ANNA}"
    assert event_text(ev("task.created", ANNA, task=direction), "ru") == f"☐ #2 Lexer · часть задачи #1 · автор: {ANNA}"
    finished = ev("task.updated", BORIS, task={**direction, "status": "done", "assignee": BORIS, "result": "ok"},
                  changes={"status": ["review", "done"]})
    assert event_text(finished) == "☒ #2 Lexer — Борис · part of #1 ⎿ ok"

    def changed(lang: str = "en", **changes) -> str:
        return event_text(ev("task.updated", ANNA, task=task(4, "Docs"), changes=changes), lang)

    assert changed(crew=[0, 3]) == "✎ #4 Docs · now a crew task, up to 3 members"
    assert changed(crew=[None, 3]) == "✎ #4 Docs · now a crew task, up to 3 members"
    assert changed(crew=[3, 0]) == "✎ #4 Docs · now an ordinary task"
    assert changed("ru", crew=[3, 0]) == "✎ #4 Docs · теперь обычная задача"
    assert changed("ru", crew=[0, 2]) == "✎ #4 Docs · теперь групповая задача, до 2 участников"
    assert changed(crew=[3, 4]) == "✎ #4 Docs · crew task, up to 4 members"
    assert changed(crew=[False, True]) == "✎ #4 Docs · updated: crew"          # a bool is no crew number
    assert changed(crew=[0, 3], priority=[1, 2]) == "✎ #4 Docs · now a crew task, up to 3 members · updated: priority"
    assert changed(parent_id=[None, 1]) == "✎ #4 Docs · part of #1"
    assert changed(parent_id=[1, None]) == "✎ #4 Docs · updated: parent"
    # a subtask's line says whose part it is whatever changed
    sub = task(2, "Lexer", parent_id=1)
    assert event_text(ev("task.updated", ANNA, task=sub, changes={"goal": ["a", "b"]})) == \
        "✎ #2 Lexer · part of #1 · updated: goal"
    assert event_text(ev("task.updated", ANNA, task=sub, changes={})) == "✎ #2 Lexer · part of #1 · updated"
    assert event_text(ev("task.updated", ANNA, task=sub, changes={"crew": [0, 2]})) == \
        "✎ #2 Lexer · now a crew task, up to 2 members · part of #1"
    closed = ev("task.updated", ANNA, task=task(4, "Docs", "done", assignee=ANNA),
                changes={"status": ["in_progress", "done"], "crew": [3, 0]})
    assert event_text(closed) == "☒ #4 Docs — Анна · now an ordinary task"


def test_crew_lines_from_a_real_board(fake_swarm):
    """The events a board journals for a crew task with directions and a crew number switched on and off."""
    anna, bob = "Anna Smith", "Bob Brown"
    fake_swarm.board = board = Board(fake_swarm)
    for name in (anna, bob):
        fake_swarm.add_member(name, language="en")
    board.create(anna, "Parser", crew=3, directions=["lexer", "cli"])
    docs = board.create(anna, "Docs", goal="Write the docs")
    board.update(anna, docs.id, crew=2)
    board.update(anna, docs.id, crew=0)
    board.update(anna, 2, goal="Tokens")
    board.claim(bob, 2)
    lines = [event_text(e) for e in fake_swarm.bus.recent(limit=100, types=("task.*",))]
    assert lines == [
        "☐ #1 Parser · crew task, up to 3 members · by Anna Smith",
        "☐ #2 lexer · part of #1 · by Anna Smith",
        "☐ #3 cli · part of #1 · by Anna Smith",
        "☐ #4 Docs · Write the docs · by Anna Smith",
        "✎ #4 Docs · now a crew task, up to 2 members",
        "✎ #4 Docs · now an ordinary task",
        "✎ #2 lexer · part of #1 · updated: goal",   # every line of a subtask says whose part it is
        "◐ #2 lexer — Bob · part of #1",
    ]


def test_messages_and_the_viewer():
    to_user = ev("message.sent", ANNA, message=message(ANNA, ["user"], "# Plan\n- a\n- b"))
    assert event_text(to_user) == f"{ANNA} → you: # Plan - a - b"
    assert event_text(to_user, "ru") == f"{ANNA} → вам: # Plan - a - b"
    to_both = ev("message.sent", ANNA, message=message(ANNA, ["user", BORIS], "ok"))
    assert event_text(to_both) == f"{ANNA} → you, {BORIS}: ok"
    between = ev("message.sent", ANNA, message=message(ANNA, [BORIS], "Look at #3"))
    assert event_text(between) == "✉ Анна → Борис: Look at #3"
    assert event_text(between, me=BORIS) == f"{ANNA} → you: Look at #3"       # as Boris sees it
    mine = ev("message.sent", "user", message=message("user", [ANNA], "Faster, please"))
    assert event_text(mine) == f"> Faster, please → {ANNA}"
    everyone = ev("message.sent", "user", message=message("user", ["all"], "Stop"))
    assert event_text(everyone) == "> Stop → everyone" and event_text(everyone, "ru") == "> Stop → все"
    long = event_text(ev("message.sent", ANNA, message=message(ANNA, [BORIS], "x " * 1000)))
    assert len(long) <= QUOTE_CHARS + 20 and long.endswith("…")
    assert event_text(ev("message.sent", ANNA, message=message(ANNA, ["user"], "/home/me/a.py"))).endswith(
        "/home/me/a.py")   # what an agent wrote is quoted as written


def test_tool_call_and_result_lines():
    """A tool call is journaled before the tool runs: the call has its line, the result a line of its own."""
    call = activity("tool_call", json.dumps({"task_id": 3}), tool="task_claim")
    assert event_text(call) == f"{ANNA} · task_claim(task_id=3)"   # the line of the spec's example (no ⏺)
    result = activity("tool_result", "Claimed task #3\nmore lines\n" * 5, tool="task_claim", is_error=False)
    assert event_text(result) == f"{ANNA} · task_claim ⎿ Claimed task #3"
    failed = activity("tool_result", "Error: no such task", tool="task_claim", is_error=True)
    assert event_text(failed) == f"{ANNA} · task_claim ⎿ ✗ Error: no such task"
    assert event_text(activity("tool_result", "", tool="wait")) == f"{ANNA} · wait ⎿ (empty)"
    assert event_text(activity("tool_result", "", tool="wait"), "ru") == f"{ANNA} · wait ⎿ (пусто)"
    long = event_text(activity("tool_call", json.dumps({"path": "a.py", "content": "x" * 500}), tool="write_file"))
    assert long == f'{ANNA} · write_file(path="a.py", content="{"x" * (eventtext.ARG_CHARS - 1)}…")'
    many = event_text(activity("tool_call", json.dumps({f"a{i}": i for i in range(100)}), tool="t"))
    assert many.endswith("…") and len(many.split(" · ", 1)[1]) <= eventtext.TOOL_LINE_CHARS
    # the housekeeping and echoed tools are left out of the normal feed only; here every call has its line
    assert event_text(activity("tool_call", "{}", tool="wait")) == f"{ANNA} · wait()"


def test_activity_lines():
    assert event_text(activity("thinking", "First the board.\nThen the code.")) == \
        f"✻ {ANNA} is thinking… First the board. Then the code."
    assert event_text(activity("text", "Halfway")) == f"{ANNA} · reply: Halfway"
    wake = activity("wake", "=== Wake-up 2026-10-08 19:37 ===\nWhat happened:\n• Task #3 is ready\n• other")
    assert event_text(wake) == f"⏰ {ANNA} wakes up: Task #3 is ready"
    assert event_text(activity("nudge", "You hold #3\nmore")) == f"↻ {ANNA}: nudge — You hold #3"
    assert event_text(activity("compacted", "Summary")) == f"✻ {ANNA} · context compacted ⎿ Summary"
    assert event_text(activity("error", "overloaded\nretrying")) == f"⚠ {ANNA}: overloaded retrying"
    assert event_text(activity("telepathy", "x")) == f"agent.activity · {ANNA}"
    assert event_text(ev("agent.status", ANNA, name=ANNA, status="working")) == f"· {ANNA}: working"
    for type_ in ("agent.usage", "agent.compacted"):
        assert event_text(ev(type_, ANNA, agent=ANNA, cost=0.01, chars=12)) is None
    for type_ in _LIVE:
        assert event_text({"id": None, "ts": NOW, "type": type_, "actor": ANNA, "data": {}}) is None


def test_goal_team_swarm_and_job_lines():
    goal = {"title": "Calculator", "description": "A CLI\ncalculator", "status": "active"}
    assert event_text(ev("goal.set", "user", goal=goal)) == "Goal: Calculator"
    assert event_text(ev("goal.set", "user", goal={**goal, "status": "pending"})) == \
        "Goal (waiting to start): Calculator"
    assert event_text(ev("goal.set", "user", goal=goal), "ru") == "Цель: Calculator"
    done = ev("goal.completed", ANNA, goal={**goal, "status": "done", "summary": "All done.\nDetails below."})
    assert event_text(done) == "✓ Goal achieved: Calculator — All done."
    assert event_text(ev("goal.completed", ANNA, goal={**goal, "summary": ""})) == "✓ Goal achieved: Calculator"
    assert event_text(ev("goal.set", "user", goal=None)) == "No goal yet"
    assert event_text(ev("goal.reopened", "user", goal=goal, comment="Add tests")) == \
        "you · the goal is back in progress: Add tests"
    hired = ev("agent.hired", "user", name=BORIS, role="Developer", kind="llm", model="default", hired_by="user")
    assert event_text(hired) == f"+ {BORIS} (Developer) joined the team · model: default · hired by you"
    external = ev("agent.hired", "user", name="Claude Code", role="", kind="external")
    assert event_text(external) == "+ Claude Code (external agent) joined the team · hired by you"
    assert event_text(ev("agent.retired", ANNA, name=BORIS, reason="Out of tasks")) == \
        f"− {BORIS} left the team: Out of tasks"
    assert event_text(ev("swarm.paused", None, reason="budget")) == "⏸ The swarm is paused (the budget is spent)"
    question = ev("swarm.paused", ANNA, reason="question", asker=ANNA, message_id=12, since=NOW)
    assert event_text(question) == "⏸ The swarm waits for your answer to Анна"
    answered = ev("swarm.resumed", "user", question={"asker": ANNA, "message_id": 12, "outcome": "answered"})
    assert event_text(answered) == "▶ The swarm is back to work: every member got your answer to question #12"
    assert event_text(ev("swarm.limits", "user", max_cost_usd=5, max_agents=6)) == \
        "· Limits: budget $5.00, at most 6 agents"
    assert event_text(ev("swarm.limits", "user", max_cost_usd=0, max_agents=6)) == \
        "· Limits: budget unlimited, at most 6 agents"
    assert event_text(ev("swarm.wrap_up", "user", on=False)) == "· you: wrap-up mode off"
    assert event_text(ev("swarm.wrap_up", "user", on=True)).startswith("⚠ you: wrap-up mode")
    ended = ev("job.ended", ANNA, job=job(status="ended", exit_code=0, ended_at=NOW + 245, duration=245.0,
                                          command="npm test"))
    assert event_text(ended) == f"⚙ {ANNA} · job j3 ended with exit code 0 (4m 05s): npm test"
    assert ".scratch" not in event_text(ended) and ".scratch" not in event_text(ev("job.started", ANNA, job=job()))
    fired = ev("trigger.fired", ANNA, trigger={"id": 4}, errors=["Unknown task #9"])
    assert event_text(fired) == "⚠ Trigger #4 failed: Unknown task #9"


def test_notification_lines():
    to_user = ev("notification", None, recipient="user", kind="u_budget", text="The budget is spent.\nRaise it.")
    assert event_text(to_user) == "🔔 The budget is spent. Raise it."
    # the notices the terminal leaves out (the goal panel, the hire line, the pause line say them) have a line here
    for kind in ("u_goal_done", "u_member_joined", "u_question"):
        assert event_text(ev("notification", None, recipient="user", kind=kind, text="Said")) == "🔔 Said"
    error = ev("notification", None, recipient="user", kind="u_agent_error", text="Anna: boom")
    assert event_text(error) == "⚠ Anna: boom"
    member = ev("notification", None, recipient=ANNA, kind="n_ready", text="Tasks are ready\n" + "x" * 400)
    assert event_text(member) == "🔔 → Анна: Tasks are ready"
    assert event_text(ev("notification", None, recipient="user", kind="u_x", text="")) == "🔔 u_x"


@pytest.mark.parametrize("member_lang", LANGS)
@pytest.mark.parametrize("kind", ["n_workspace_moved", "n_workspace_changed"])
def test_a_workspace_move_names_the_folder_by_its_last_component(kind, member_lang):
    """The notices of a workspace move (Swarm.notify, in the member's language) name the whole path on the hub's
    computer: the line is the notice with each folder named by its last component."""
    for (path, name), (old, old_name) in (
            (("/home/me/projects/snake game", "snake game"), ("/home/me/DreamTeam/sessions/001-snake/workspace",
                                                              "workspace")),
            (("C:\\Users\\me\\work\\snake game\\", "snake game"), ("D:\\old place\\ws", "ws"))):
        text = prompts.t(member_lang, kind, path=path, old=old)
        assert path in text
        notice = ev("notification", None, recipient=ANNA, kind=kind, text=text)
        for lang in LANGS:
            line = event_text(notice, lang)
            assert_one_clean_line(line)
            assert line == "🔔 → Анна: " + eventtext.TextContext().short(
                prompts.t(member_lang, kind, path=name, old=old_name), 200)
            for part in ("/home", "me/", "\\Users", "Users", "C:", "D:", "old place", "DreamTeam", "sessions"):
                assert part not in line, (part, line)
        # a text no template matches (another version's wording): the notice's kind rather than a path
        odd = ev("notification", None, recipient=ANNA, kind=kind, text=f"Moved to {path} somehow")
        assert event_text(odd) == f"🔔 → Анна: {kind}"


def test_a_skill_environment_is_left_out_of_the_notices():
    """docs/SPEC.md §21.6: a skill's environment (its Python) and its build log's folder never show in a line."""
    env = "/home/me/.dreamteam/skill-envs/ab12"
    deps = prompts.t("en", "skills_deps_python", python=f"{env}/bin/python", files="requirements.txt", extra="")
    ready = prompts.t("en", "n_skill_deps_ready", name="pdf", deps=deps)
    assert env in ready
    assert event_text(ev("notification", None, recipient=ANNA, kind="n_skill_deps_ready", text=ready)) == \
        '🔔 → Анна: The packages of the skill "pdf" are installed.'
    failed = prompts.t("ru", "n_skill_deps_failed", name="pdf", deps=deps)
    assert event_text(ev("notification", None, recipient=ANNA, kind="n_skill_deps_failed", text=failed)) == \
        "🔔 → Анна: Установить пакеты скилла «pdf» не удалось."
    for lang in LANGS:
        text = prompts.t(lang, "u_skill_deps_failed", name="pdf", files="requirements.txt", error="pip failed",
                         log=f"{env}/build.log")
        line = event_text(ev("notification", None, recipient="user", kind="u_skill_deps_failed", text=text), lang)
        assert line == "🔔 " + prompts.t(lang, "u_skill_deps_failed", name="pdf", files="requirements.txt",
                                         error="pip failed", log="build.log")
        assert env not in line and ".dreamteam" not in line
        # an installer's error that names a folder (str of an OSError): the folder by its last component too
        text = prompts.t(lang, "u_skill_deps_failed", name="pdf", files="requirements.txt",
                         error="[Errno 13] Permission denied: '/home/alice/my envs/ab12/lib'", log=f"{env}/build.log")
        line = event_text(ev("notification", None, recipient="user", kind="u_skill_deps_failed", text=text), lang)
        assert line == "🔔 " + prompts.t(lang, "u_skill_deps_failed", name="pdf", files="requirements.txt",
                                         error="[Errno 13] Permission denied: 'lib'", log="build.log")
        assert "/skills reinstall pdf" in line   # a command is no path
        for part in PATH_PARTS + ("my envs",):
            assert part not in line, (part, line)


_B = "\\"   # a Windows path's separator


@pytest.mark.parametrize(("text", "line"), [
    ("Moved to /home/alice/acme-secret.", "Moved to acme-secret."),
    ("Python: C:" + _B + "Users" + _B + "alice" + _B + "Scripts" + _B + "python.exe (ok)", "Python: python.exe (ok)"),
    ("Permission denied: '/home/alice/my dir/lib'", "Permission denied: 'lib'"),
    ('see "C:/Program Files/x y/z.txt" now', 'see "z.txt" now'),
    ("on " + _B * 2 + "server" + _B + "share" + _B + "f.log, then", "on f.log, then"),
    (_B * 2 + "?" + _B + "C:" + _B + "long" + _B + "x.txt", "x.txt"),
    ("in ~/projects/x) and (/srv/data/file)", "in x) and (file)"),
    ("file=/var/log/x.log; Log:/home/a/b.log.", "file=x.log; Log:b.log."),
    ("`/home/alice/x y/z`", "`z`"),
    ("'/home/alice/unclosed", "'unclosed"),
    # no path of anyone's folder: a command, a URL, a fraction, a relative path, a single folder
    ("Reinstall with /skills reinstall pdf.", None),
    ("429 from https://api.example.com/v1/messages", None),
    ("and/or 24/7 a/b/c /tmp", None),
])
def test_dreamteams_own_texts_name_a_path_by_its_last_component(text, line):
    assert eventtext.without_abs_paths(text) == (text if line is None else line)


_SITE = "C:" + _B + "Users" + _B + "Jane Doe" + _B + "jobs" + _B + "acme site"
_JANE = "C:" + _B + "Users" + _B + "Jane Doe"
_WORK = "D:" + _B + "Work Stuff" + _B + "big repo"


@pytest.mark.parametrize(("text", "line"), [
    # unquoted, spaces and all: the named folder whole, never cut at its first space
    (f"does not exist: {_SITE}. Bring it back", "does not exist: acme site. Bring it back"),
    # the other separator, another case; what lies below it stays after its name, relative
    ("in c:/users/jane doe/jobs/acme site/sub dir/x.txt: busy", "in acme site/sub dir/x.txt: busy"),
    # a folder below the user's home ("~"): then named by its last component as any other path
    (f"inside the git repository {_JANE}{_B}jobs: move it", "inside the git repository jobs: move it"),
    # whole components only ("acme site2" is another folder), and never inside a longer path (cut at the space then,
    # as a path nobody named)
    (f"{_SITE}2", "acme site2"),
    ("/srv/home/jane doe/x and /home/jane doe/y", "jane doe/x and y"),
    # an exact folder only as itself; the path of an OSError's repr (doubled separators) below one is anyone's
    (f"inside the git repository {_WORK}: move it", "inside the git repository big repo: move it"),
    ("No such file: 'C:" + _B * 2 + "Users" + _B * 2 + "alice" + _B * 2 + "f'", "No such file: 'f'"),
    (f"{_WORK}{_B}x.txt and {_WORK}.old", "Work Stuff\\big repo\\x.txt and Work Stuff\\big repo.old"),
])
def test_named_folders_are_replaced_whole_before_any_other_path(text, line):
    named = [(_SITE, "acme site"), (_JANE, "~"), ("/home/jane doe", "~"), ("/", "root"), ("C:" + _B, "drive")]
    exact = [(_WORK, "big repo"), ("C:" + _B + "Users", "Users")]
    assert eventtext.without_abs_paths(text, named, exact) == line   # (a root names nothing)


_SESSIONS = "C:" + _B + "home" + _B + "sessions"


@pytest.mark.parametrize(("text", "line"), [
    # a path below a hidden folder goes whole, what ends the sentence stays; the folder itself by its exact name
    (f"Cannot use {_SESSIONS}{_B}003-acme-merger{_B}workspace{_B}plan.md.", "Cannot use (other)."),
    (f"Cannot list {_SESSIONS}: busy", "Cannot list sessions: busy"),
    (f"{_SESSIONS.lower()}/003-acme-merger, {_SESSIONS}{_B * 2}004-x{_B * 2}y)", "(other), (other))"),
    # a named folder below it is longer, so named first: what lies below it stays; "005-xy" is another folder
    (f"in {_SESSIONS}{_B}005-x{_B}workspace{_B}a.txt, not {_SESSIONS}{_B}005-xy{_B}b", "in 005-x\\workspace\\a.txt, "
                                                                                       "not (other)"),
    # a folder whose name only starts the same is another one (below the named home here)
    (f"see {_SESSIONS}-old{_B}003-x", "see home\\sessions-old\\003-x"),
])
def test_a_hidden_folder_hides_every_path_below_it(text, line):
    named = [(_SESSIONS + _B + "005-x", "005-x"), ("C:" + _B + "home", "home")]
    hidden = eventtext.without_abs_paths(text, named, exact=[(_SESSIONS, "sessions")], hidden=[(_SESSIONS, "(other)")])
    assert hidden == line


@pytest.mark.parametrize("lang", LANGS)
def test_no_absolute_path_of_the_hub_in_a_wake_up_an_error_or_a_skill_environment(lang):
    """docs/SPEC.md §21.6: the text adds no absolute path of the hub's computer, wherever DreamTeam's own text puts one:
    the first notice of a wake-up (a workspace move, a skill's Python), an installer's error, a configured Python, an
    agent's error. What an agent wrote keeps its paths."""
    def wake(notice: str) -> dict:
        return activity("wake", "\n".join([prompts.t(lang, "wake_header", time="2026-11-16 10:00"),
                                           prompts.t(lang, "reasons"), f"• {notice}", "• Task #3 is ready"]))

    def clean_of_paths(line: str) -> str:
        assert_one_clean_line(line)
        for part in PATH_PARTS:
            assert part not in line, (part, line)
        return line

    wakes = f"⏰ {ANNA} {uitext.t(lang, 'wakes')}: "
    for path in ("/home/alice/clients/acme-secret", "C:" + _B + "Users" + _B + "alice" + _B + "acme-secret"):
        moved = event_text(wake(prompts.t(lang, "n_workspace_moved", path=path)), lang)
        assert clean_of_paths(moved) == wakes + prompts.t(lang, "n_workspace_moved", path="acme-secret")
    for python in ("/home/alice/.dreamteam/skill-envs/ab12/bin/python",
                   "C:" + _B + "Users" + _B + "alice" + _B + ".dreamteam" + _B + "skill-envs" + _B + "python.exe"):
        deps = prompts.t(lang, "skills_deps_python", python=python, files="requirements.txt", extra="")
        ready = event_text(wake(prompts.t(lang, "n_skill_deps_ready", name="pdf", deps=deps)), lang)
        name = python.replace(_B, "/").rsplit("/", 1)[1]
        assert clean_of_paths(ready) == wakes + " ".join(prompts.t(
            lang, "n_skill_deps_ready", name="pdf", deps=deps.replace(python, name)).split())

    base = {"name": "pdf", "ecosystem": "python", "files": ["requirements.txt"], "key": "ab12",
            "env": "/home/alice/.dreamteam/skill-envs/ab12"}
    failed = event_text(ev("skill.deps", None, status="failed", **base,
                           error="[Errno 13] Permission denied: '/home/alice/.dreamteam/skill-envs/ab12/lib'"), lang)
    assert clean_of_paths(failed).endswith("[Errno 13] Permission denied: 'lib'")
    missing = event_text(ev("skill.deps", None, status="unavailable", **base, reason={
        "code": "unavailable", "detail": "Python '/opt/py311/bin/python3' of [skills] python"}), lang)
    assert clean_of_paths(missing).endswith("(Python 'python3' of [skills] python)")
    need = event_text(ev("skill.deps", None, status="unsupported", **base,
                         reason={"code": "python", "need": ">=3.12", "have": "3.11.4"}), lang)
    assert clean_of_paths(need).endswith("(>=3.12 (3.11.4))")

    error = activity("error", "Could not forward the reply to the user: [Errno 28] No space left on device: "
                              "'C:" + _B + "Users" + _B + "alice" + _B + "DreamTeam" + _B + "x.db'")
    assert clean_of_paths(event_text(error, lang)).endswith("No space left on device: 'x.db'")
    agent_error = ev("notification", None, recipient="user", kind="u_agent_error",
                     text="Анна: [Errno 2] No such file or directory: '/home/alice/x/y.txt'")
    assert clean_of_paths(event_text(agent_error, lang)) == "⚠ Анна: [Errno 2] No such file or directory: 'y.txt'"
    trigger = ev("trigger.fired", ANNA, trigger={"id": 4}, errors=["Cannot read /home/alice/x/state.json"])
    assert clean_of_paths(event_text(trigger, lang)).endswith(": Cannot read state.json")
    # what an agent wrote is quoted as written
    for kind, extra in (("tool_result", {"tool": "read_file"}), ("thinking", {}), ("text", {})):
        assert event_text(activity(kind, "/home/alice/x/y.txt", **extra), lang).endswith("/home/alice/x/y.txt")


def test_skill_mcp_file_and_trigger_lines():
    attached = ev("skill.attached", "user", name="pdf", files=3, replaced=False, via="mcp", **{"from": "library"})
    assert event_text(attached) == "you · attached skill pdf · library · via MCP"
    proposed = ev("skill.proposed", ANNA, name="csv", owner=ANNA, files=2, replaced=False)
    assert event_text(proposed) == f"{ANNA} · proposes skill csv"
    saved = ev("skill.library", "user", name="csv", owner=ANNA, status="saved", comment="", via="home")
    assert event_text(saved) == f"you · saved {ANNA}'s skill csv to the library"
    deps = {"name": "pdf", "ecosystem": "python", "files": ["requirements.txt"], "env": "/home/me/.dreamteam/envs/ab12",
            "key": "ab12"}
    assert event_text(ev("skill.deps", None, status="ready", seconds=65, **deps)) == \
        "✓ skill pdf: packages installed (requirements.txt, 1m 05s)"
    assert event_text(ev("skill.deps", None, status="installing", **deps)) == \
        "· skill pdf: installing the packages (requirements.txt)"
    manual = event_text(ev("skill.deps", None, status="off", reason={"code": "off"}, **deps))
    assert manual == "· pdf: python manual (off)"
    for status in ("ready", "installing", "failed", "off"):
        assert "envs" not in event_text(ev("skill.deps", None, status=status, error="pip failed", **deps))
    mcp = ev("mcp.created", ANNA, server={"name": "git", "owner": ANNA, "status": "pending", "description": "Git"})
    assert event_text(mcp) == f'{ANNA} · MCP "git" — pending approval · Git'
    assert event_text(ev("file.changed", ANNA, path="src/main.py", op="edit")) == "✎ src/main.py — edited · Анна"
    created = ev("trigger.created", ANNA, trigger={"id": 5, "owner": ANNA, "condition": "every(60)"})
    assert event_text(created) == "⚡ trigger #5: every(60) · Анна"
    # a fire whose wake-up the wake limit holds: for how long, never a clock (the hub's time zone is not the reader's)
    held = ev("trigger.fired", ANNA, trigger={"id": 5, "condition": "every(60)", "held": 2, "wake_at": NOW + 245})
    assert event_text(held) == "⚡ trigger #5 fired: every(60) (wake-up held for 4m 05s) · Анна"
    assert event_text(held, "ru") == "⚡ сработал триггер #5: every(60) (пробуждение отложено на 4m 05s) · Анна"
    assert event_text({**held, "ts": None}) == "⚡ trigger #5 fired: every(60) (wake-up held) · Анна"
    assert event_text({**held, "ts": NOW + 300}) == "⚡ trigger #5 fired: every(60) (wake-up held) · Анна"
    assert event_text(ev("trigger.fired", ANNA, trigger={"id": 5, "condition": "every(60)", "held": 0,
                                                         "wake_at": None})) == "⚡ trigger #5 fired: every(60) · Анна"
