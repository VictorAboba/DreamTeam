"""TUI slash commands, input helpers and dialogs (dreamteam.tui.commands / input / dialogs) without the app."""

from __future__ import annotations

import asyncio
import os
import sys
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
    for required in ("help", "new", "team", "sessions", "resume", "goal", "board", "task", "agents", "roles", "jobs",
                     "to", "hire", "trace", "span", "files", "cat", "cd", "cost", "status", "model", "pause", "budget",
                     "max", "approve", "reject", "stop", "web", "settings", "verbose", "lang", "clear", "exit"):
        assert required in names
    for lang in ("ru", "en"):
        for cmd in COMMANDS:
            assert f"cmd_{cmd.name}" in TEXTS[lang]
            if cmd.args:
                assert f"cmdargs_{cmd.name}" in TEXTS[lang]
        rows = commands.help_rows(lang)
        assert rows[0]["name"] == "help" and rows[0]["description"] == TEXTS[lang]["cmd_help"]
    assert find("task").usage("en") == "/task N"
    assert find("hire").usage("ru") == "/hire Роль [| инструкции]"
    assert find("approve").usage("en") == "/approve name | N [comment]"
    assert names.index("roles") == names.index("agents") + 1 and names.index("team") == names.index("new") + 1
    assert names.index("jobs") == names.index("roles") + 1
    assert find("jobs").usage("en") == "/jobs [jN | stop jN]" and find("jobs").session and not find("jobs").required
    assert "pausing does not stop them" in find("jobs").description("en")
    assert "пауза их не останавливает" in find("jobs").description("ru")
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
    # --team: the roles in order; it sets start_agents unless --agents gives it (in either order)
    assert session_overrides({"team": "architect, developer*2"})[0] == \
        {"swarm": {"starting_roles": ["architect", "developer", "developer"], "start_agents": 3}}
    assert session_overrides({"team": "developer*2", "agents": 5})[0] == \
        {"swarm": {"starting_roles": ["developer", "developer"], "start_agents": 5}}
    assert session_overrides({"agents": 1, "team": "tester"})[0] == \
        {"swarm": {"start_agents": 1, "starting_roles": ["tester"]}}
    assert session_overrides({"team": None, "agents": 2})[0] == {"swarm": {"start_agents": 2}}


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
                "confirm_new_text", "session_starting", "session_stopped", "unknown_session",
                "task_review_you", "td_review_hint", "review_reason_required", "roles_title", "roles_hint",
                "unknown_role", "no_roles", "builtin_role", "role_model", "role_instructions", "team_line", "team_set",
                "team_reset", "team_bad", "col_description"):
        assert TEXTS[lang].get(key), key
    # the goal texts say how a typed goal is split (first line = title)
    assert all("Alt+Enter" in TEXTS[lang][key] for key in ("ph_goal", "ph_no_session", "tip_goal"))


# ---- review, roles and the starting team with a stand-in hub client -------------------------------------
CATALOG = [{"key": "generalist", "name": "Generalist", "builtin": True,
            "description": "You take whichever task is most useful now. More.", "instructions": "", "model": None},
           {"key": "tester", "name": "Tester", "description": "You check that what others built really works. More.",
            "instructions": "Use pytest.", "model": "fast", "builtin": True},
           {"key": "data engineer", "name": "Data engineer", "description": "You build data pipelines.",
            "instructions": "", "model": None, "builtin": False}]


def settings_view() -> dict:
    """GET /api/settings as the launcher answers it (the parts /roles and /team read)."""
    def both(en: str, ru: str) -> dict:
        return {"en": en, "ru": ru}
    return {"settings": {"swarm": {"starting_roles": ["architect"], "start_agents": 3}},
            "role_catalog": [
                {"key": "generalist", "builtin": True, "customized": False, "hidden": False,
                 "name": both("Generalist", "Универсал"), "description": both("You take tasks.", "Ты берёшь задачи."),
                 "instructions": "", "model": None},
                {"key": "architect", "builtin": True, "customized": False, "hidden": False,
                 "name": both("Architect", "Архитектор"), "description": both("You design.", "Ты проектируешь."),
                 "instructions": "", "model": None},
                {"key": "developer", "builtin": True, "customized": False, "hidden": False,
                 "name": both("Developer", "Разработчик"), "description": both("You code.", "Ты пишешь код."),
                 "instructions": "", "model": None},
                {"key": "researcher", "builtin": True, "customized": True, "hidden": True,
                 "name": both("Researcher", "Исследователь"), "description": both("You dig.", "Ты исследуешь."),
                 "instructions": "", "model": None}]}


class HubApp(FakeApp):
    """A stand-in app whose hub client records the calls (act, roles, settings) and the MCP decisions."""

    def __init__(self, mode: str = "launcher", attached: bool = False):
        super().__init__(mode, attached)
        self.acts: list[tuple[str, dict]] = []
        self.overrides: dict = {}

        async def act(sid, tool, args):
            self.acts.append((tool, args))
            return True, f"{tool} ok"

        async def roles(sid):
            self.calls.append(("roles", sid))
            return {"language": "en", "roles": CATALOG}

        async def settings():
            self.calls.append(("settings",))
            return settings_view()

        self.client = SimpleNamespace(mode=mode, act=act, roles=roles, settings=settings)

    async def decide_mcp(self, name, approve, comment=""):
        self.calls.append(("mcp", name, approve, comment))


async def test_approve_and_reject_settle_tasks_or_mcp_servers():
    app = HubApp(attached=True)
    await execute(app, "/approve 12 looks good")
    await execute(app, "/approve #12")
    await execute(app, "/reject 12")
    await execute(app, "/reject #12 fix the parser: it drops the last line")
    await execute(app, "/reject 12 'don't use global state'")
    await execute(app, "/approve calc")
    await execute(app, "/reject calc 'not safe'")
    assert app.acts == [
        ("task_review", {"task_id": 12, "approve": True, "comment": "looks good"}),
        ("task_review", {"task_id": 12, "approve": True, "comment": ""}),
        ("task_review", {"task_id": 12, "approve": False, "comment": "fix the parser: it drops the last line"}),
        ("task_review", {"task_id": 12, "approve": False, "comment": "don't use global state"}),  # quotes around
    ]
    assert "Say what to fix: /reject 12 what to fix" in app.text   # a return needs a reason: no call
    assert "task_review ok" in app.text
    assert [c for c in app.calls if c[0] == "mcp"] == [("mcp", "calc", True, ""), ("mcp", "calc", False, "not safe")]


JOB_STATE = {"jobs": [
    {"id": 3, "label": "j3", "owner": ANNA, "command": "npm run dev", "origin": "background", "status": "running",
     "reason": None, "exit_code": None, "started_at": 1_760_000_000.0, "ended_at": None, "processes": ["node.exe"],
     "log": ".scratch/anna/jobs/j3.log"},
    {"id": 2, "label": "j2", "owner": BORIS, "command": "pytest -q", "origin": "timeout", "status": "ended",
     "reason": None, "exit_code": 0, "started_at": 1_760_000_000.0, "ended_at": 1_760_000_240.0, "processes": [],
     "log": ".scratch/boris/jobs/j2.log"}]}


class JobsApp(HubApp):
    """HubApp whose state has background jobs and whose `jobs` / `job_stop` answer like the hub."""

    def __init__(self, state: dict | None = None):
        super().__init__(attached=True)
        self.state = state if state is not None else JOB_STATE

        async def act(sid, tool, args):
            self.acts.append((tool, args))
            if args.get("job_id") == "j9":
                return False, "There is no job j9. Running jobs: j3."
            if tool == "jobs":
                return True, (f"Job {args['job_id']} of {ANNA}: npm run dev\nState: running for 25 min (started in the "
                              "background) [bash]\n--- last 2 lines ---\nready\nListening on :5173")
            return True, f"Stopped job {args['job_id']} (npm run dev) of {ANNA} after 25 min, together with the " \
                         "processes it started. Last output:\nListening on :5173"

        self.client.act = act

    async def refresh_state(self) -> dict:
        self.calls.append(("state",))
        return self.state


async def test_jobs_lists_shows_and_stops_background_jobs():
    app = JobsApp()
    await execute(app, "/jobs")
    table = app.out[-1]
    assert ("state",) in app.calls and not app.acts                     # the table comes from the state
    assert "j3" in table and "Анна Смирнова" in table and "npm run dev" in table and "running" in table
    assert "j2" in table and "ended with exit code 0" in table and "/jobs stop jN" in table
    await execute(app, "/jobs j3")
    assert app.acts[-1] == ("jobs", {"job_id": "j3"})
    assert app.out[-1].startswith("⚙ Job j3 of Анна Смирнова: npm run dev") and "Listening on :5173" in app.out[-1]
    for ref in ("#3", "%3", "3", "J3"):                                   # the forms of a job id people type
        await execute(app, f"/jobs {ref}")
        assert app.acts[-1] == ("jobs", {"job_id": "j3"})
    await execute(app, "/jobs stop j3 5")
    assert app.acts[-2:] == [("job_stop", {"job_id": "j3"}), ("job_stop", {"job_id": "j5"})]
    assert "Stopped job j5 (npm run dev)" in app.out[-1] and "Listening on :5173" in app.out[-1]
    await execute(app, "/jobs стоп j3")
    assert app.acts[-1] == ("job_stop", {"job_id": "j3"})
    acts = len(app.acts)
    for bad in ("/jobs stop", "/jobs npm", "/jobs j3 j4", "/jobs stop all"):
        await execute(app, bad)
        assert app.out[-1] == "⚠ Usage: /jobs [jN | stop jN]", bad
    assert len(app.acts) == acts
    await execute(app, "/jobs j9")                                         # a refusal is an error line
    assert app.out[-1] == "⚠ There is no job j9. Running jobs: j3."
    await execute(app, "/jobs stop j9")
    assert app.out[-1] == "⚠ There is no job j9. Running jobs: j3."

    empty = JobsApp({"jobs": []})
    empty.ctx.lang = "ru"
    await execute(empty, "/jobs")
    assert empty.out[-1] == "  ⎿  Фоновых процессов нет."
    detached = HubApp()                                                    # no session: nothing to list
    await execute(detached, "/jobs")
    assert "No current session" in detached.text and not detached.acts


async def test_hire_always_sends_instructions():
    # `instructions` stays a required parameter of hire_agent (R0): "" for a catalog role
    app = HubApp(attached=True)
    await execute(app, "/hire Tester")
    await execute(app, "/hire QA engineer | test the parser")
    await execute(app, "/hire | nothing")
    assert app.acts == [("hire_agent", {"role": "Tester", "instructions": ""}),
                        ("hire_agent", {"role": "QA engineer", "instructions": "test the parser"})]
    assert "Usage: /hire Role [| instructions]" in app.text


async def test_roles_lists_the_catalog_and_one_role():
    app = HubApp(attached=True)
    app.ctx.width = 100
    await execute(app, "/roles")
    table = app.out[-1]
    assert "Role catalog" in table and "Tester" in table and "fast" in table and "Data engineer" in table
    assert "You check that what others built really works." in table and "More." not in table  # the first sentence
    assert "/roles Role for the full description" in table
    await execute(app, "/roles tester")
    detail = app.out[-1]
    assert "Tester" in detail and "built-in" in detail and "More." in detail
    assert "Extra instructions: Use pytest." in detail and "model profile: fast" in detail
    await execute(app, "/roles  data   ENGINEER ")
    assert "You build data pipelines." in app.out[-1] and "built-in" not in app.out[-1]
    await execute(app, "/roles Тестировщик")   # a preset by its name in the other language
    assert "Use pytest." in app.out[-1]
    await execute(app, "/roles nope")
    assert "No role “nope” in the catalog. /roles lists them." in app.out[-1]
    assert [c for c in app.calls if c[0] == "roles"] == [("roles", "001-x")] * 5

    launcher = HubApp()   # no session: the launcher's catalog in the UI language, hidden roles left out
    launcher.ctx.lang = "ru"
    await execute(launcher, "/roles")
    assert "Архитектор" in launcher.out[-1] and "Ты проектируешь." in launcher.out[-1]
    assert "Исследователь" not in launcher.out[-1]
    swarm_hub = HubApp(mode="swarm")
    await execute(swarm_hub, "/roles")
    assert "No current session" in swarm_hub.text


async def test_team_sets_the_starting_team_of_new_sessions():
    app = HubApp()
    await execute(app, "/team")
    assert "Starting team of new sessions: Architect ×1 · Generalist ×2 (3 in all)" in app.out[-1]
    await execute(app, "/team developer*2, tester")
    assert app.overrides == {"swarm": {"starting_roles": ["developer", "developer", "tester"], "start_agents": 3}}
    assert "New sessions from this window start with: Developer ×2 · Tester ×1 (3 in all)" in app.out[-1]
    await execute(app, "/team")
    assert "New sessions from this window start with: Developer ×2" in app.out[-1]
    await execute(app, "/team x*0")
    assert "'x*0': the count must be a whole number from 1 to 50. Example: /team architect" in app.out[-1]
    assert app.overrides["swarm"]["start_agents"] == 3                       # unchanged
    app.overrides["swarm"]["max_agents"] = 9                                 # (another override stays)
    await execute(app, "/team -")
    assert app.overrides == {"swarm": {"max_agents": 9}} and "comes from the settings" in app.out[-1]
    await execute(app, "/team reset")
    assert app.overrides == {"swarm": {"max_agents": 9}}
    del app.overrides["swarm"]["max_agents"]
    await execute(app, "/team сброс")
    assert app.overrides == {}
    swarm_hub = HubApp(mode="swarm", attached=True)
    await execute(swarm_hub, "/team tester")
    assert "needs a session launcher" in swarm_hub.text and swarm_hub.overrides == {}


# ---- /cd helpers (dreamteam.console, shared by the TUI and the console) ------------------------------------
def test_cd_folder_resolution_and_states(tmp_path):
    from dreamteam.console import folder_state, is_loopback, resolve_folder, same_folder, unquote

    assert unquote('"C:\\My Projects\\app"') == "C:\\My Projects\\app" and unquote("'/srv/a b'") == "/srv/a b"
    assert unquote("plain") == "plain" and unquote('"') == '"'
    assert resolve_folder("proj", local=True, cwd=str(tmp_path)) == str(tmp_path / "proj")
    assert resolve_folder(f'"{tmp_path / "a" / ".." / "b"}"', local=True) == str(tmp_path / "b")
    assert resolve_folder("~/proj", local=True) == os.path.normpath(os.path.expanduser("~/proj"))
    assert resolve_folder("proj", local=False) is None and resolve_folder("~/proj", local=False) is None
    assert resolve_folder("/srv/app", local=False) == "/srv/app"  # the hub's system decides
    assert resolve_folder("D:\\app", local=False) == "D:\\app"
    assert resolve_folder("  ", local=True) is None
    if sys.platform == "win32":  # a rooted path without a drive gets the current drive (as `--workspace` does)
        drive = os.path.splitdrive(str(tmp_path))[0]
        assert resolve_folder("\\projects\\app", local=True, cwd=str(tmp_path)) == drive + "\\projects\\app"
        assert os.path.isabs(resolve_folder("\\projects\\app", local=True))
    assert is_loopback("http://127.0.0.1:8765/s/001") and is_loopback("http://[::1]:1")
    assert is_loopback("http://localhost")
    assert not is_loopback("http://10.0.0.5:8765")
    # the console of `serve --host <LAN IP>` runs next to its hub, and `dreamteam console` found the hub through the
    # home's hub.json: local, whatever the address
    from dreamteam.console import Console

    assert not Console("http://192.168.1.5:8765/s/001-calc", "en").local
    assert Console("http://192.168.1.5:8765/s/001-calc", "en", local=True).local

    (tmp_path / "empty").mkdir()
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x.txt").write_text("x", encoding="utf-8")
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    assert [folder_state(str(tmp_path / n)) for n in ("empty", "full", "file.txt", "nope")] == \
        ["empty", "files", "file", "missing"]
    assert same_folder(tmp_path / "full", str(tmp_path / "x" / ".." / "full"))
    assert not same_folder(tmp_path / "full", tmp_path / "empty") and not same_folder(None, tmp_path)


def test_cd_preview_words_the_question(tmp_path):
    from dreamteam.console import cd_preview

    own = {"path": str(tmp_path / "s" / "workspace"), "default": True, "adopted": False, "exists": True}
    moved = {"path": str(tmp_path / "moved"), "default": False, "adopted": False, "exists": True}
    adopted = {"path": str(tmp_path / "project"), "default": False, "adopted": True, "exists": True}
    lost = {**moved, "exists": False}
    default = own["path"]
    (tmp_path / "empty").mkdir()
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x.txt").write_text("x", encoding="utf-8")
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")

    def preview(current, target, local=True):
        return cd_preview(current, None if target is None else str(tmp_path / target), default, local=local)

    assert preview(own, None) == ("same", None)
    assert cd_preview(moved, moved["path"], default, local=True) == ("same", None)
    assert preview(own, "new") == ("missing", "text_missing")
    assert preview(own, "empty") == ("empty", "text_empty")
    assert preview(own, "full") == ("files", "text_files")
    assert preview(own, "file.txt") == ("file", None)
    assert preview(adopted, "empty") == ("empty", "text_fresh")  # the user's files stay: nothing of theirs moves
    assert preview(lost, "new") == ("missing", "text_lost")
    assert preview(moved, None) == ("missing", "text_missing")   # its own folder was removed by the first move
    (tmp_path / "s" / "workspace").mkdir(parents=True)
    (tmp_path / "s" / "workspace" / "old.txt").write_text("x", encoding="utf-8")
    assert preview(adopted, None) == ("files", "text_back")      # its old files are still there
    assert preview(own, "full", local=False) == ("unknown", "text_unknown")


def test_cd_result_lines():
    from dreamteam.console import cd_result_lines

    unchanged = cd_result_lines({"mode": "unchanged", "to": "/p"}, "Calc")
    assert unchanged == [("unchanged", {"title": "Calc", "path": "/p"})]
    lines = cd_result_lines({"mode": "moved", "from": "/a", "to": "/b", "git": "moved", "mcp_moved": ["calc", "csv"],
                             "old_kept": None, "old_missing": False, "restarted": True, "start_error": None}, "Calc")
    assert lines == [("moved", {"title": "Calc", "path": "/b"}), ("git_moved", {}),
                     ("mcp_moved", {"names": "calc, csv"})]
    lines = cd_result_lines({"mode": "adopted", "from": "/a", "to": "/repo", "git": "existing", "mcp_moved": [],
                             "old_kept": "/a", "old_missing": False, "start_error": "boom"}, "Calc")
    assert [key for key, _ in lines] == ["adopted", "git_existing", "old_kept", "start_error"]
    lines = cd_result_lines({"mode": "moved", "from": "/gone", "to": "/b", "git": "disabled", "old_kept": None,
                             "old_missing": True}, "Calc")
    assert [key for key, _ in lines] == ["now", "git_disabled", "old_missing"]
    assert lines[-1] == ("old_missing", {"path": "/gone"})
    assert [k for k, _ in cd_result_lines({"mode": "created", "to": "/n", "git": "initialized"}, "N")] == \
        ["created", "git_initialized"]
    # leaving the user's folder whose repository tracked the session's MCP servers: their removal is said
    left = {"mode": "moved", "from": "/repo", "to": "/b", "git": "initialized", "mcp_moved": ["calc"],
            "old_kept": "/repo", "old_missing": False,
            "old_changed": {"paths": ["mcp/calc"], "commit": "0123456789abcdef", "error": None}}
    lines = cd_result_lines(left, "Calc")
    assert [k for k, _ in lines] == ["now", "git_initialized", "mcp_moved", "old_mcp_committed"]
    assert lines[-1][1] == {"path": "/repo", "paths": "mcp/calc", "commit": "01234567"}
    lines = cd_result_lines({**left, "old_changed": {"paths": ["mcp/calc"], "commit": None, "error": "detached"}}, "C")
    assert lines[-1] == ("old_mcp_uncommitted", {"path": "/repo", "paths": "mcp/calc", "error": "detached"})
    for lang in ("ru", "en"):  # every key has a text in the TUI and in the console
        from dreamteam.console import CD_TEXTS

        for key in ("unchanged", "moved", "now", "adopted", "created", "git_moved", "git_existing", "git_initialized",
                    "git_disabled", "git_none", "mcp_moved", "old_kept", "old_missing", "start_error", "text_missing",
                    "text_empty", "text_fresh", "text_lost", "text_files", "text_back", "text_unknown",
                    "kind_default", "kind_moved", "kind_adopted", "old_mcp_committed", "old_mcp_uncommitted"):
            assert TEXTS[lang].get("cd_" + key) and CD_TEXTS[lang].get(key), (lang, key)
            fields = {"path": "/p", "paths": "mcp/x", "commit": "abc", "error": "e", "title": "T", "names": "n",
                      "old": "/o"}
            TEXTS[lang]["cd_" + key].format(**fields)  # (no unknown fields)
            CD_TEXTS[lang][key].format(**fields)
        assert TEXTS[lang].get("cd_text_bash")


def test_cd_expect_and_the_mcp_words():
    from dreamteam.console import cd_expect, cd_moves_files
    from dreamteam.launcher_mcp import workspace_text

    assert [cd_expect(s) for s in ("missing", "empty", "files", "unknown", "same")] == \
        ["moved", "moved", "adopted", None, None]
    # the session's own files leave its folder (a running `!` command there is stopped first) only on a real move
    own = {"path": "D:/w", "default": True, "adopted": False, "exists": True}
    assert [cd_moves_files(s, own) for s in ("missing", "empty", "unknown", "files", "same", "file")] == \
        [True, True, True, False, False, False]
    assert not cd_moves_files("empty", {**own, "default": False, "adopted": True})  # the user's files stay
    assert not cd_moves_files("missing", {**own, "default": False, "exists": False})  # nothing there to move
    # the MCP words say what the TUI and the console say: after the user's folder nothing of the session's moved
    text = workspace_text({"mode": "moved", "to": "D:/fresh", "git": "initialized", "mcp_moved": ["calc"],
                           "old_kept": "D:/proj"})
    assert text.startswith("The session now works in D:/fresh.") and "Moved the session's files" not in text
    text = workspace_text({"mode": "moved", "from": "D:/gone", "to": "D:/b", "git": "disabled", "old_missing": True})
    assert text.startswith("The session now works in D:/b.") and "did not exist" in text
    assert workspace_text({"mode": "moved", "to": "D:/b", "git": "moved"}).startswith("Moved the session's files to")
    text = workspace_text({"mode": "moved", "from": "D:/proj", "to": "D:/b", "git": "initialized",
                           "old_kept": "D:/proj",
                           "old_changed": {"paths": ["mcp/calc"], "commit": None, "error": "detached HEAD"}})
    assert "not committed there (detached HEAD)" in text and "stays as it was" not in text


# ---- /skills ----------------------------------------------------------------------------------------
SKILL_STATE = {"skills": [
    {"name": "pdf", "description": "Reads PDF files.", "from": "library", "owner": None, "library": "none",
     "roles": ["tester"], "manual": False, "files": 3,
     "deps": [{"ecosystem": "python", "status": "ready", "files": ["requirements.txt"], "packages": ["pypdf"]},
              {"ecosystem": "node", "status": "failed", "files": ["package.json"], "packages": ["sharp"],
               "error": "npm ci exited with code 1"}]},
    {"name": "csv-cleanup", "description": "Cleans CSV files.", "from": "agent", "owner": ANNA, "library": "pending",
     "roles": [], "manual": True, "files": 2,
     "deps": [{"ecosystem": "python", "status": "unsupported", "files": ["setup.py"], "packages": [],
               "reason": {"code": "unsupported", "detail": "setup.py runs code"}}]},
    {"name": "old", "description": "", "from": "claude-user", "owner": None, "library": "rejected",
     "comment": "too broad", "roles": [], "deps": []}]}
SKILLS_GET = {"library_dir": "/home/u/.dreamteam/skills",
              "library": [{"name": "pdf", "description": "Reads PDF files."},
                          {"name": "web", "description": "Browses.", "changed_outside": True}],
              "claude": [{"name": "notes", "origin": "claude-project", "description": "Notes.", "agent_writable": True}],
              "host": [], "attached": [{"name": "pdf"}]}


class SkillsApp(HubApp):
    """HubApp with the skills parts of the client and the app (`skills_override`, `local`, decide_skill)."""

    def __init__(self, attached: bool = True, mode: str = "launcher"):
        super().__init__(mode, attached)
        self.state = SKILL_STATE
        self.local = True
        self.skills_override: list[str] = []
        self.cleanup = {"removed": ["a", "b"], "kept": [{"dirname": "c", "why": "in use"}]}
        client = self.client

        async def skills(sid):
            self.calls.append(("skills", sid))
            return SKILLS_GET

        async def skill(sid, name, frm="library"):
            self.calls.append(("skill", sid, name, frm))
            return {"name": name, "description": "Reads PDF files.", "files_list": [{"path": "SKILL.md"}],
                    "skill_md": "\n".join(f"line {i}" for i in range(60)),
                    "deps": {"python": {"files": ["requirements.txt"], "packages": ["pypdf"]}},
                    "install": {"enabled": True, "proposals": True}}

        async def skill_log(sid, name, ecosystem=None):
            self.calls.append(("log", sid, name, ecosystem))
            return {"name": name, "ecosystem": "node", "status": "failed", "path": "/envs/x/install.log",
                    "log": "npm ERR! gyp"}

        async def session_skills(sid, show=None):
            self.calls.append(("session_skills", sid))
            return {"attached": SKILL_STATE["skills"], "running": False}

        async def clean_skill_envs():
            self.calls.append(("clean",))
            return self.cleanup

        for fn in (skills, skill, skill_log, session_skills, clean_skill_envs):
            setattr(client, fn.__name__, fn)

    async def refresh_state(self) -> dict:
        return self.state

    async def decide_skill(self, name, approve, comment="", replace=False, sid=None):
        self.calls.append(("decide", name, approve, comment, replace) + ((sid,) if sid else ()))


async def test_skills_lists_attaches_and_decides(tmp_path):
    app = SkillsApp()
    await execute(app, "/skills")
    table = app.out[-1]
    assert "pdf" in table and "Reads PDF files." in table and "tester" in table
    assert "python: ready" in table and "node: failed" in table and "npm ci exited with code 1" in table
    assert "csv-cleanup" in table and "waits for you" in table and "only when asked" in table
    assert "setup.py runs code" in table and "manual" in table and "not saved" in table and "too broad" in table
    await execute(app, "/skills add pdf")
    await execute(app, "/skills add claude-user:notes --replace")
    await execute(app, f"/skills add {tmp_path / 'my skill'} --pick a,b")
    await execute(app, "/skills add https://github.com/org/repo")
    await execute(app, "/skills update pdf")
    await execute(app, "/skills rm pdf")
    await execute(app, "/skills reinstall pdf node")
    assert app.acts == [
        ("skill_attach", {"name": "pdf"}),
        ("skill_attach", {"name": "claude-user:notes", "replace": True}),
        ("skill_attach", {"source": os.path.normpath(str(tmp_path / "my skill")), "pick": ["a", "b"]}),
        ("skill_attach", {"source": "https://github.com/org/repo"}),
        ("skill_attach", {"name": "library:pdf", "replace": True}),   # again from its own source
        ("skill_detach", {"name": "pdf"}),
        ("skill_reinstall", {"name": "pdf", "ecosystem": "node"}),
    ]
    acts = len(app.acts)
    await execute(app, "/skills update csv-cleanup")   # a member's proposal has no source of its own
    assert "made by a member" in app.out[-1]
    await execute(app, "/skills update nope")
    assert 'Skill "nope" is not attached' in app.out[-1] and len(app.acts) == acts
    for bad in ("/skills frobnicate", "/skills add", "/skills reject", "/skills log pdf ruby"):
        await execute(app, bad)
        assert app.out[-1].startswith("⚠ Usage: /skills [lib |"), bad
    await execute(app, "/skills approve csv-cleanup replace")
    await execute(app, '/skills reject csv-cleanup "not general enough"')
    await execute(app, "/skills reject csv-cleanup")   # the reason is optional, as on the web page and in the dialog
    assert [c for c in app.calls if c[0] == "decide"] == [("decide", "csv-cleanup", True, "", True),
                                                          ("decide", "csv-cleanup", False, "not general enough", False),
                                                          ("decide", "csv-cleanup", False, "", False)]
    await execute(app, "/skills show pdf")   # attached: the session's copy
    assert app.calls[-1] == ("skill", "001-x", "pdf", "session")
    shown = app.out[-1]
    assert "pdf · session" in shown and "Files (1): SKILL.md" in shown and "Installs automatically: pypdf" in shown
    assert "line 39" in shown and "line 40" not in shown and "+20 lines" in shown
    await execute(app, "/skills show claude-user:notes")
    assert app.calls[-1] == ("skill", "001-x", "notes", "claude-user")
    await execute(app, "/skills log pdf")
    assert app.calls[-1] == ("log", "001-x", "pdf", None) and "npm ERR! gyp" in app.out[-1]
    await execute(app, "/skills lib")
    lib = app.out[-1]
    assert "Skills library (/home/u/.dreamteam/skills)" in lib and "claude-project:notes" in lib
    assert "attached" in lib and "changed outside DreamTeam" in lib and "agents can write here" in lib
    await execute(app, "/skills pending")
    assert "csv-cleanup" in app.out[-1] and "pdf" not in app.out[-1] and "/skills approve name" in app.out[-1]
    assert "--session" not in app.out[-1]
    # another session's proposals: the header names it, and the decision goes to it (not the attached one)
    await execute(app, "/skills pending 002-y")
    assert app.calls[-1] == ("session_skills", "002-y") and "/skills approve name --session 002-y" in app.out[-1]
    await execute(app, "/skills approve csv-cleanup --session 002-y")
    await execute(app, "/skills reject csv-cleanup --session 002-y too narrow")
    await execute(app, "/skills approve csv-cleanup replace --session 001-x")   # the attached one: as without it
    assert [c for c in app.calls if c[0] == "decide"][-3:] == [
        ("decide", "csv-cleanup", True, "", False, "002-y"), ("decide", "csv-cleanup", False, "too narrow", False, "002-y"),
        ("decide", "csv-cleanup", True, "", True)]
    await execute(app, "/skills approve csv-cleanup --session")
    assert app.out[-1].startswith("⚠ Usage: /skills [lib |")
    await execute(app, "/skills clean-envs")
    assert "Removed 2 skill environment(s); kept 1 (in use)" in app.out[-1]
    # what was kept and why comes from the report: nothing kept says nothing about it
    app.cleanup = {"removed": [], "kept": [{"dirname": "c", "why": "being built"}, {"dirname": "d", "why": "in use"}]}
    await execute(app, "/skills clean-envs")
    assert "Removed 0 skill environment(s); kept 2 (being built, in use)" in app.out[-1]
    app.cleanup = {"removed": ["a"], "kept": []}
    await execute(app, "/skills clean-envs")
    assert "Removed 1 skill environment(s)" in app.out[-1] and "kept" not in app.out[-1]


async def test_skills_without_a_session_pick_the_next_ones():
    app = SkillsApp(attached=False)
    await execute(app, "/skills")
    assert "No current session" in app.out[-1]
    await execute(app, "/skills add pdf")
    await execute(app, "/skills add claude-user:notes")
    assert app.skills_override == ["pdf", "claude-user:notes"] and not app.acts
    await execute(app, "/skills")
    assert "Skills of the next new session: pdf, claude-user:notes" in app.out[-1]
    await execute(app, "/skills show pdf")   # without a session: the library's
    assert app.calls[-1] == ("skill", None, "pdf", "library")
    await execute(app, "/skills rm pdf")
    assert "No current session" in app.out[-1]
    await execute(app, "/skills approve csv-cleanup")
    assert "No current session" in app.out[-1]
    # a session listed by `/skills pending ID` is decided without attaching (and so starting) it
    await execute(app, "/skills pending 002-y")
    assert "[reason] --session 002-y" in app.out[-1]
    await execute(app, "/skills approve csv-cleanup --session 002-y")
    assert app.calls[-1] == ("decide", "csv-cleanup", True, "", False, "002-y")
    swarm = SkillsApp(mode="swarm")
    await execute(swarm, "/skills clean-envs")
    assert "needs a session launcher" in swarm.out[-1]


def test_skill_approval_dialog_shows_the_files_and_the_start_of_skill_md():
    from dreamteam.tui.dialogs import SkillApprovalDialog

    body = "\n".join(f"step {i}" for i in range(1, 21))
    skill = {"name": "csv-cleanup", "owner": ANNA, "files": 2, "description": "Cleans CSV files.",
             "files_list": [{"path": "SKILL.md", "size": 2048}, {"path": "scripts/run.py", "size": 12}],
             "skill_md": f"---\nname: csv-cleanup\ndescription: Cleans CSV files.\n---\n\n{body}\n"}
    text = "".join(part for _, part in SkillApprovalDialog("en", skill).fragments(100))
    assert "Files: SKILL.md (2.0 KB), scripts/run.py (12 B)" in text
    assert "step 1" in text and "step 8" in text and "step 9" not in text and "name: csv-cleanup" not in text
    assert "all of SKILL.md: /skills show csv-cleanup" in text
    # the snapshot alone (no detail fetched): the count, no SKILL.md
    text = "".join(part for _, part in SkillApprovalDialog("ru", {"name": "x", "files": 3}).fragments(100))
    assert "Файлов: 3" in text and "/skills show" not in text
    assert render.skill_md_body("---\nname: x\n") == "---\nname: x"   # unclosed: not front matter


def test_skills_registry_and_texts():
    names = [cmd.name for cmd in COMMANDS]
    assert names.index("skills") == names.index("jobs") + 1
    skills = find("skills")
    assert skills.args and not skills.session and not skills.required and not skills.launcher
    for lang in ("ru", "en"):
        assert "clean-envs" in skills.usage(lang) and "reinstall X" in skills.usage(lang)
        for key in ("skill_title", "skill_yes", "skill_no", "skill_later", "skill_reject_reason", "skill_deferred",
                    "skill_waiting", "delete_pending_title", "delete_pending_save", "delete_pending_discard",
                    "skills_none", "skills_hint", "col_packages", "role_skills"):
            assert TEXTS[lang].get(key), (lang, key)
    # the role catalog of the launcher keeps each role's skills (/roles X shows them)
    view = settings_view()
    view["role_catalog"][1]["skills"] = ["pdf"]
    assert commands._localized("en", view["role_catalog"])[1]["skills"] == ["pdf"]
    text = render.to_plain(render.role_detail(render.RenderContext(lang="en"), {"name": "Tester", "skills": ["pdf"]}),
                           100)
    assert "Role skills (from the library): pdf" in text
