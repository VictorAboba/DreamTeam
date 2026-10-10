"""TUI slash commands, input helpers and dialogs (dreamteam.tui.commands / input / dialogs) without the app."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from dreamteam.tui import commands, render
from dreamteam.tui.commands import COMMANDS, execute, find, session_overrides
from dreamteam.tui.dialogs import (
    APPROVE, REJECT, ConfirmDialog, MCPApprovalDialog, SelectList, SessionPicker, Option, box, fit, frag_len, wrap,
)
from dreamteam.tui.i18n import TEXTS
from dreamteam.tui.input import (
    ALL, CommandInfo, bash_command, default_addressee, did_you_mean, is_bash, load_prefs, members_of, number,
    parse_command, planning_assignee, resolve_member, save_prefs, split_mention, suggest, window,
)

ANNA, ANTON, BORIS = "Анна Смирнова", "Антон Волков", "Борис Петров"
STATE = {
    "agents": [{"name": ANNA, "status": "idle"}, {"name": ANTON, "status": "working"},
               {"name": BORIS, "status": "idle"}, {"name": "Old Timer", "status": "retired"}],
    "tasks": [{"id": 1, "title": "Plan the work", "status": "in_progress", "assignee": BORIS, "tags": ["planning"]},
              {"id": 2, "title": "Write the parser", "status": "todo"},
              {"id": 12, "title": "Ship it", "status": "done"}],
}


# ---- parsing --------------------------------------------------------------------------------------
def test_parse_command_and_bash():
    assert parse_command("/board") == ("board", "")
    assert parse_command("  /Task  3 ") == ("task", "3")
    assert parse_command("/hire Analyst | dig\ninto data") == ("hire", "Analyst | dig\ninto data")
    assert parse_command("hello /board") is None
    assert parse_command("") is None
    assert bash_command("!ls -la") == "ls -la"
    assert bash_command("  ! git status ") == "git status"
    assert bash_command("ls") is None
    assert is_bash(" !x") and not is_bash("x!")


def test_registry_lookup_and_texts():
    assert find("board").name == "board"
    assert find("/quit").name == "exit" and find("q").name == "exit"
    assert find("nope") is None
    names = [cmd.name for cmd in COMMANDS]
    assert len(names) == len(set(names))
    for required in ("help", "new", "sessions", "resume", "goal", "board", "task", "agents", "to", "hire", "trace",
                     "span", "files", "cat", "cost", "status", "model", "pause", "budget", "max", "approve",
                     "reject", "stop", "web", "settings", "verbose", "lang", "clear", "exit"):
        assert required in names
    for lang in ("ru", "en"):
        for cmd in COMMANDS:
            assert f"cmd_{cmd.name}" in TEXTS[lang]
            if cmd.args:
                assert f"cmdargs_{cmd.name}" in TEXTS[lang]
        rows = commands.help_rows(lang)
        assert rows[0]["name"] == "help" and rows[0]["description"] == TEXTS[lang]["cmd_help"]
    assert find("task").usage("en") == "/task N"
    assert find("hire").usage("ru") == "/hire Роль | инструкции"
    help_text = render.to_plain(render.help_view(render.RenderContext(lang="en"), commands.help_rows("en")), 100)
    assert "/board" in help_text and "/exit, /quit, /q" in help_text and "task board" in help_text


def test_did_you_mean_and_numbers():
    names = [cmd.name for cmd in COMMANDS]
    assert did_you_mean("bord", names)[0] == "board"
    assert "sessions" in did_you_mean("sesion", names)
    assert did_you_mean("zzzzzz", names) == []
    assert number("5") == 5.0 and number("$2.50") == 2.5 and number("2,5") == 2.5
    assert number("five") is None and number("nan") is None


def test_session_overrides():
    sections, title = session_overrides({"model": "fast", "agents": 2, "max_agents": 4, "budget": 1.5,
                                         "title": "Calc", "language": None})
    assert title == "Calc"
    assert sections == {"agents": {"default_model": "fast"},
                        "swarm": {"start_agents": 2, "max_agents": 4, "max_cost_usd": 1.5}}
    sections, title = session_overrides({"swarm": {"max_agents": 2}, "agents": {"default_model": "x"}})
    assert sections == {"swarm": {"max_agents": 2}, "agents": {"default_model": "x"}} and title is None
    assert session_overrides(None) == ({}, None)


# ---- suggestions ----------------------------------------------------------------------------------
INFOS = [CommandInfo("board", "task board"), CommandInfo("budget", "budget", args="N", required=True),
         CommandInfo("exit", "quit", aliases=("quit",)), CommandInfo("help", "help")]


def test_command_suggestions():
    found = suggest("/b", commands=INFOS)
    assert [s.label for s in found] == ["/board", "/budget N"]
    assert found[0].runnable and not found[1].runnable
    assert found[1].apply("/b") == ("/budget ", 8)
    assert [s.label for s in suggest("/", commands=INFOS)] == ["/board", "/budget N", "/exit", "/help"]
    assert [s.insert for s in suggest("/qu", commands=INFOS)] == ["/quit"]
    assert [s.label for s in suggest("/oar", commands=INFOS)] == ["/board"]  # substring after prefixes
    assert suggest("/board 3", commands=INFOS) == []                       # arguments being typed
    assert suggest("hello /b", commands=INFOS) == []


def test_member_and_task_suggestions():
    members = members_of(STATE)
    assert members == [ANNA, ANTON, BORIS]
    found = suggest("@Ан", members=members, everyone="everyone")
    assert [s.label for s in found] == [f"@{ANNA}", f"@{ANTON}"]
    assert found[0].apply("@Ан") == (f"@{ANNA} ", len(ANNA) + 2)
    assert [s.label for s in suggest("hi @петр", members=members)] == [f"@{BORIS}"]   # by last name
    all_rows = suggest("@", members=members, everyone="everyone")
    assert all_rows[-1].label == "@all" and all_rows[-1].description == "everyone" and len(all_rows) == 4
    assert suggest(f"@{ANNA} ", members=members) == []            # complete: the message follows
    assert suggest(f"@{ANNA} привет", members=members) == []
    assert suggest("mail@Ан", members=members) == []              # not a mention

    tasks = STATE["tasks"]
    assert [s.label for s in suggest("#", tasks=tasks)] == ["#1 Plan the work", "#2 Write the parser", "#12 Ship it"]
    found = suggest("see #1", tasks=tasks)
    assert [s.insert for s in found] == ["#1 ", "#12 "]
    assert found[0].apply("see #1") == ("see #1 ", 7)
    assert [s.label for s in suggest("#pars", tasks=tasks)] == ["#2 Write the parser"]
    labels = {"todo": "к работе", "in_progress": "в работе"}
    found = suggest("#", tasks=tasks, status=lambda s: labels.get(s, s))  # statuses in the UI language
    assert [s.description for s in found] == ["в работе", "к работе", "done"]
    assert list(window(20, 0)) == list(range(8)) and list(window(20, 15)) == list(range(8, 16))
    assert list(window(3, 2)) == [0, 1, 2]


# ---- addressee ------------------------------------------------------------------------------------
def test_mentions_and_addressee():
    members = members_of(STATE)
    assert split_mention(f"@{ANNA}, hi there", members) == (ANNA, "hi there")
    assert split_mention("@борис ping", members) == (BORIS, "ping")       # a unique first name
    assert split_mention("@all stop", members) == (ALL, "stop")
    assert split_mention("@Все стоп", members) == (ALL, "стоп")
    assert split_mention("@Нико hi", members) == (None, "Нико hi")
    assert resolve_member("Борис", members) == BORIS
    assert resolve_member("@all", members) == ALL and resolve_member("все", members) == ALL
    assert resolve_member("Ан", members) is None and resolve_member("", members) is None
    assert planning_assignee(STATE) == BORIS
    assert default_addressee(STATE, ANNA) == ANNA
    assert default_addressee(STATE, "Old Timer") == BORIS                 # retired: the planner instead
    assert default_addressee({"agents": STATE["agents"], "tasks": []}, None) == ALL


def test_prefs_roundtrip(tmp_path):
    assert load_prefs(tmp_path) == {}
    save_prefs(tmp_path, lang="en")
    save_prefs(tmp_path, other=1)
    assert load_prefs(tmp_path) == {"lang": "en", "other": 1}
    (tmp_path / ".dreamteam" / "tui.json").write_text("not json", encoding="utf-8")
    assert load_prefs(tmp_path) == {}


# ---- dialogs --------------------------------------------------------------------------------------
def plain(frags) -> str:
    return "".join(text for _, text in frags)


def test_fit_wrap_and_box():
    assert plain(fit([("", "abc")], 5)) == "abc  "
    assert plain(fit([("a", "hello "), ("b", "world")], 8)) == "hello w…"
    assert frag_len(fit([("", "Привет, мир")], 6)) == 6
    assert wrap("one two three four", 9) == ["one two", "three", "four"]
    assert wrap("x " * 100, 10, max_lines=2)[-1].endswith("…")
    lines = plain(box([[("", "body")]], 20, title="Title")).split("\n")
    assert lines[0].startswith("╭─ Title ") and lines[0].endswith("╮") and len(lines[0]) == 20
    assert lines[1] == "│ body             │" and lines[2] == "╰" + "─" * 18 + "╯"
    ascii_lines = plain(box([[("", "x")]], 12, unicode=False)).split("\n")
    assert ascii_lines[0] == "+" + "-" * 10 + "+" and ascii_lines[1] == "| x        |"


async def test_select_list_and_dialog_results():
    select = SelectList([Option("a", 1), Option("b", 2), Option("c", 3)], rows=2)
    select.move(-1)
    assert select.selected.value == 3 and list(select.visible()) == [1, 2]
    assert select.pick_number(1) and select.selected.value == 1 and not select.pick_number(4)

    confirm = ConfirmDialog("en", "Sure?", "Really")
    waiter = asyncio.ensure_future(confirm.wait())
    await asyncio.sleep(0)
    confirm.move(1)
    confirm.choose()
    assert await waiter is False
    assert await confirm.wait() is False  # answered once

    mcp = MCPApprovalDialog("ru", {"name": "calc", "owner": ANNA, "scope": "task", "task_id": 3,
                                   "description": "Arithmetic helpers", "tools": ["add", "sub"],
                                   "command": ["python", "server.py"]})
    text = plain(mcp.fragments(70))
    for needle in ("MCP-сервер ждёт одобрения", "calc", "Владелец: Анна Смирнова", "Область: участники задачи #3",
                   "Описание: Arithmetic helpers", "Команда: python server.py", "Инструменты: add, sub",
                   "❯ 1. Да, разрешить", "2. Нет, отклонить (с комментарием)", "Esc — решить позже"):
        assert needle in text
    assert all(len(line) == 70 for line in text.split("\n"))
    assert mcp.pick_number(2) and await mcp.wait() == REJECT
    approve = MCPApprovalDialog("en", {"name": "calc"})
    assert approve.pick_number(1) and await approve.wait() == APPROVE
    later = MCPApprovalDialog("en", {"name": "calc"})
    later.cancel()
    assert await later.wait() is None
    ascii_box = plain(MCPApprovalDialog("en", {"name": "calc", "owner": "Bob", "description": "x — y…"})
                      .fragments(60, unicode=False))
    assert ascii_box.isascii() and all(len(line) == 60 for line in ascii_box.split("\n"))
    assert "> 1. Yes, allow" in ascii_box and "Esc to decide later" in ascii_box

    sessions = [{"id": "001-a", "title": "A", "status": "running", "goal": {"title": "Goal A", "status": "done"}},
                {"id": "002-b", "title": "B", "status": "stopped"}]
    picker = SessionPicker("en", sessions, current="002-b")
    assert picker.select.selected.value == "002-b" and not picker.pick_number(1)
    text = plain(picker.fragments(80))
    assert "001-a · goal achieved · Goal A" in text and "▶ B" in text
    picker.move(1)
    picker.choose()
    assert await picker.wait() == "001-a"


# ---- execute() with a stand-in app ----------------------------------------------------------------
class FakeApp:
    def __init__(self, mode: str = "launcher", attached: bool = False):
        self.ctx = render.RenderContext(lang="en")
        self.client = SimpleNamespace(mode=mode)
        self.attached = attached
        self.sid = "001-x" if attached else None
        self.members = [ANNA, BORIS]
        self.out: list[str] = []
        self.calls: list[tuple] = []

    def print(self, renderable) -> None:
        self.out.append(render.to_plain(renderable, 100))

    def set_addressee(self, name: str) -> None:
        self.calls.append(("to", name))

    def cycle_verbosity(self) -> None:
        self.calls.append(("verbose",))

    def set_lang(self, lang: str) -> None:
        self.calls.append(("lang", lang))

    def ask_goal(self) -> None:
        self.calls.append(("ask_goal",))

    def exit(self) -> None:
        self.calls.append(("exit",))

    @property
    def text(self) -> str:
        return "\n".join(self.out)


async def test_execute_checks_and_dispatch():
    app = FakeApp()
    assert await execute(app, "hello") is False
    assert await execute(app, "/bord") is True
    assert "Unknown command /bord. /help for the list." in app.text and "Did you mean: /board" in app.text
    await execute(app, "/board")
    assert "No current session" in app.text
    await execute(app, "/lang")
    assert "Usage: /lang ru|en" in app.text
    await execute(app, "/lang de")
    assert app.text.count("Usage: /lang ru|en") == 2
    await execute(app, "/lang ru")
    await execute(app, "/verbose")
    await execute(app, "/new")
    await execute(app, "/quit")
    assert app.calls == [("lang", "ru"), ("verbose",), ("ask_goal",), ("exit",)]

    swarm_hub = FakeApp(mode="swarm", attached=True)
    await execute(swarm_hub, "/sessions")
    assert "needs a session launcher" in swarm_hub.text
    await execute(swarm_hub, "/to борис")
    await execute(swarm_hub, "/to Nobody")
    assert swarm_hub.calls == [("to", BORIS)]
    assert "Now writing to: Борис Петров" in swarm_hub.text and "No such member: Nobody" in swarm_hub.text
    await execute(swarm_hub, "/task abc")
    assert "Usage: /task N" in swarm_hub.text
    await execute(swarm_hub, "/budget lots")
    assert "Not a number: lots" in swarm_hub.text


def test_unknown_event_types_do_not_break_rendering():
    # the app prints render.event() for every bus event: odd payloads must render or be skipped
    ctx = render.RenderContext(lang="en")
    for ev in ({"type": "message.sent", "data": {"message": {"sender": "user", "recipients": [ANNA],
                                                            "text": "hi"}}},
               {"type": "brand.new", "data": None}, {"type": "_connection", "data": {"state": "lost"}}):
        out = render.event(ctx, ev)
        assert out is None or isinstance(render.to_plain(out, 80), str)


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_app_texts_exist(lang):
    for key in ("box_new_session", "ph_no_session", "ph_session", "ph_goal", "ctrl_c_again", "sessions_stopped",
                "unknown_command", "did_you_mean", "attached", "session_created", "no_session", "addressee_set",
                "unknown_member", "pick_session", "mcp_title", "mcp_yes", "mcp_no", "mcp_later",
                "mcp_reject_reason", "mcp_deferred", "usage", "launcher_only", "confirm_new_title",
                "confirm_new_text", "session_starting", "session_stopped", "unknown_session"):
        assert TEXTS[lang].get(key), key
