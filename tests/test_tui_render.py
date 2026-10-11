"""TUI rendering (docs/TUI.md §3): i18n, theme and the pure renderers of render.py."""

from __future__ import annotations

import io
import json
import re
import string
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.cells import cell_len

from dreamteam.tui import i18n, render, theme
from dreamteam.tui.render import RenderContext, to_ansi, to_plain

ANNA, BORIS = "Анна Смирнова", "Борис Петров"
NOW = 1_760_000_000.0
ESC = "\x1b"
# symbols of the table that are not plain ASCII (ASCII mode must never print them)
UNICODE_SYMBOLS = {ch for value in theme.SYMBOLS.values() for ch in value if not ch.isascii()} | \
    {ch for ch in "".join(theme.SPINNER_FRAMES_UNICODE) if not ch.isascii()}


def ctx(verbosity: str = "normal", lang: str = "ru", width: int = 100, unicode: bool = True) -> RenderContext:
    return RenderContext(lang=lang, verbosity=verbosity, width=width, unicode=unicode)


def plain(renderable, width: int = 100) -> str:
    return to_plain(renderable, width)


def ev(type_: str, actor: str | None = None, **data) -> dict:
    return {"id": 1, "ts": NOW, "type": type_, "actor": actor, "data": data}


def activity(kind: str, text: str = "", agent: str = ANNA, **extra) -> dict:
    return ev("agent.activity", agent, agent=agent, kind=kind, text=text, **extra)


def task(id_: int = 3, title: str = "Парсер", status: str = "todo", **kw) -> dict:
    base = {"id": id_, "title": title, "goal": "Разобрать ввод", "description": "", "status": status,
            "assignee": None, "reviewer": None, "created_by": ANNA, "parent_id": None, "depends_on": [],
            "priority": 1, "tags": [], "result": None, "created_at": NOW, "updated_at": NOW, "blocked_by": [],
            "comments": 0}
    base.update(kw)
    return base


def message(sender: str, recipients: list[str], text: str, id_: int = 5) -> dict:
    return {"id": id_, "ts": NOW, "sender": sender, "recipients": recipients, "text": text, "reply_to": None}


def job(id_: int = 3, **kw) -> dict:
    """A background job as job.started / job.ended carry it (DESIGN: the public job dict)."""
    base = {"id": id_, "label": f"j{id_}", "owner": ANNA, "command": "npm run dev", "origin": "background",
            "status": "running", "reason": None, "stopped_by": None, "exit_code": None, "started_at": NOW,
            "background_at": NOW + 2, "ended_at": None, "log": f".scratch/anna/jobs/j{id_}.log",
            "processes": ["node.exe"]}
    base.update(kw)
    return base


TRACE = {
    "id": 12, "agent": ANNA, "kind": "llm", "status": "done", "steps": 3, "llm_calls": 2, "tool_calls": 2,
    "errors": 1, "input_tokens": 4200, "output_tokens": 310, "cache_read_tokens": 0, "cache_write_tokens": 0,
    "cost_usd": 0.0123, "duration_ms": 12300, "tasks": [3], "started_at": NOW - 12, "ended_at": NOW,
    "trigger": "You have new messages:\nfrom user: hi", "outcome": "Turn ended (wait)", "model": "claude-opus-5-5",
    "from_user": True,
    "spans": [
        {"id": 40, "kind": "input", "name": "wake", "step": 0, "status": "ok", "output": "You have new messages"},
        {"id": 41, "kind": "llm", "name": "claude-opus-5-5", "step": 1, "status": "ok", "duration_ms": 1200,
         "usage": {"input_tokens": 4000, "output_tokens": 200, "cache_read_tokens": 100}, "cost_usd": 0.01,
         "meta": {"purpose": "step", "stop_reason": "tool_use"},
         "output": {"stop_reason": "tool_use", "thinking": "Надо взять задачу", "text": "",
                    "tool_calls": [{"id": "c1", "name": "task_claim", "arguments": {"task_id": 3}}]}},
        {"id": 42, "kind": "tool", "name": "task_claim", "step": 1, "status": "ok", "duration_ms": 12,
         "input": {"task_id": 3}, "output": "Claimed task #3"},
        {"id": 43, "kind": "tool", "name": "write_file", "step": 2, "status": "error", "duration_ms": 5,
         "input": {"path": "a.py"}, "output": "Error: a.py is locked"},
        {"id": 44, "kind": "llm", "name": "claude-opus-5-5", "step": 3, "status": "error", "duration_ms": 30000,
         "error": "overloaded", "meta": {"error_kind": "overloaded", "retry_in": 5}},
        {"id": 45, "kind": "event", "name": "reply_to_user", "step": 3, "status": "ok", "output": "Готово"},
    ],
}
SPAN = {**TRACE["spans"][1], "trace_id": 12, "agent": ANNA, "started_at": NOW,
        "input": {"epoch": 0, "upto": 5, "messages": 5, "extra": []}}
CONTEXT = {"span_id": 41, "agent": ANNA, "model": "claude-opus-5-5", "system": "You are Anna.\nBe kind.",
           "tools": [{"name": "task_claim"}, {"name": "send_message"}], "complete": True, "note": "", "epoch": 0,
           "messages": [{"role": "user", "content": "wake up"},
                        {"role": "assistant", "content": "",
                         "tool_calls": [{"id": "c1", "name": "task_claim", "arguments": {"task_id": 3}}]},
                        {"role": "tool", "name": "task_claim", "tool_call_id": "c1", "content": "Claimed task #3",
                         "is_error": False}]}
AGENTS = [
    {"name": ANNA, "kind": "llm", "role": "Координатор", "status": "working", "model": "default",
     "cost_usd": 0.42, "tokens_in": 42000, "tokens_out": 3100, "open_tasks": [3], "unread": 0},
    {"name": BORIS, "kind": "llm", "role": "Разработчик", "status": "idle", "model": "default",
     "cost_usd": 0.004, "tokens_in": 1200, "tokens_out": 300, "open_tasks": [], "unread": 1},
    {"name": "Claude Code", "kind": "external", "role": "Помощник", "status": "retired", "model": None,
     "cost_usd": 0, "tokens_in": 0, "tokens_out": 0, "open_tasks": [], "unread": 0},
]
STATE = {"swarm": {"name": "calc", "spent_usd": 0.424, "limits": {"max_cost_usd": 5.0, "max_agents": 6},
                   "goal": {"title": "Калькулятор", "status": "active"}, "paused": False},
         "agents": AGENTS, "tasks": []}
SESSIONS = [
    {"id": "001-calc", "title": "Калькулятор", "status": "running", "paused": False,
     "goal": {"title": "CLI калькулятор", "status": "active"}, "agents": {"total": 3, "working": 1},
     "tasks": {"total": 5, "done": 1}, "spent_usd": 0.42, "max_cost_usd": 5.0, "last_activity": NOW},
    {"id": "002-snake", "title": "Змейка", "status": "stopped", "goal": {"title": "Змейка", "status": "done"},
     "agents": {"total": 2, "working": 0}, "tasks": {"total": 4, "done": 4}, "spent_usd": 1.5, "max_cost_usd": 0},
]
SETTINGS = {"agents": {"default_model": "default"},
            "models": {"default": {"provider": "anthropic", "model": "claude-opus-5-5", "api_key_set": True,
                                   "api_key_hint": "…abcd", "api_key": None},
                       "local": {"provider": "openai", "model": "qwen", "base_url": "http://localhost:11434/v1",
                                 "api_key_env": "OLLAMA_KEY", "api_key_env_set": False, "api_key_set": False}}}
ROLES = [  # GET /api/roles "roles"
    {"key": "generalist", "name": "Универсал", "builtin": True, "instructions": "", "model": None,
     "description": "Ты берёшь ту задачу, которая сейчас полезнее всего: код, тексты, анализ. Перед завершением ты сам "
                    "проверяешь результат."},
    {"key": "tester", "name": "Тестировщик", "builtin": True, "instructions": "Пиши тесты на pytest.", "model": "fast",
     "description": "Ты проверяешь, что сделанное другими действительно работает. Код, который ты написал сам, ты не "
                    "тестируешь."},
    {"key": "дата-инженер", "name": "Дата-инженер", "builtin": False, "instructions": "", "model": None,
     "description": "Ты строишь конвейеры данных " + "и проверяешь их " * 30},
]
# the state's "jobs": the running ones, then those that ended in the last hour (newest first)
JOBS = [job(5, command="pytest -q tests/test_parser.py", origin="timeout", owner=BORIS, processes=["python.exe"],
            started_at=NOW - 245),
        job(3, processes=["node.exe", "node.exe", "conhost.exe"], started_at=NOW - 1500),
        job(4, command="npm test", status="ended", exit_code=1, started_at=NOW - 600, ended_at=NOW - 360),
        job(2, command="gradle build", origin="detached", status="ended", started_at=NOW - 900, ended_at=NOW - 700),
        job(1, command="python -m http.server", status="stopped", reason="user", stopped_by="user",
            started_at=NOW - 3000, ended_at=NOW - 2900),
        job(6, command="sleep 99", status="stopped", reason="lifetime", started_at=NOW - 3500, ended_at=NOW - 1700),
        job(7, command="make", status="lost", reason="crash", started_at=NOW - 3500, ended_at=NOW - 3400)]
JOB_DETAIL = ("Job j3 of Анна Смирнова: npm run dev\nState: running for 25 min (started in the background) [bash]\n"
              "Processes: node.exe ×2\nLog: .scratch/anna/jobs/j3.log (2.1 MB; below is its end)\n"
              "--- last 3 lines ---\n"
              "> dev\nready in 300 ms\nListening on :5173")
COMMANDS = [{"name": "/help", "args": "", "description": {"ru": "справка", "en": "help"}},
            ("/new", "[цель]", "новая сессия"),
            {"name": "board", "args": "", "description": "доска задач"},
            {"name": "/task", "args": "N", "description": "задача с обсуждением"}]


# realistic payloads of every event type, as the server emits them
def realistic_events() -> list[dict]:
    t3 = task(3, "Парсер", "in_progress", assignee=ANNA)
    return [
        ev("task.created", ANNA, task=task(3)),
        ev("task.updated", ANNA, task=t3, changes={"status": ["todo", "in_progress"], "assignee": [None, ANNA]}),
        ev("task.updated", ANNA, task=task(3, status="review", assignee=ANNA, reviewer=BORIS),
           changes={"status": ["in_progress", "review"]}),
        ev("task.updated", ANNA, task=task(3, status="review", assignee=ANNA, reviewer="user"),
           changes={"status": ["in_progress", "review"], "reviewer": [None, "user"]}),
        ev("task.updated", BORIS, task=task(3, status="done", assignee=ANNA, result="Готово: parser.py\nподробности"),
           changes={"status": ["review", "done"], "result": [None, "Готово"]}),
        ev("task.updated", ANNA, task=task(3, status="failed", assignee=ANNA, result="Не вышло"),
           changes={"status": ["in_progress", "failed"]}),
        ev("task.updated", "user", task=task(3, status="cancelled"), changes={"status": ["todo", "cancelled"]}),
        ev("task.updated", ANNA, task=task(3, status="waiting", assignee=ANNA),
           changes={"status": ["todo", "waiting"]}),
        ev("task.updated", ANNA, task=task(3, priority=3), changes={"priority": [1, 3]}),
        ev("task.comment", ANNA, task_id=3, post={"id": 7, "task_id": 3, "author": ANNA, "text": "Кто возьмёт?",
                                                   "reply_to": None, "ts": NOW}),
        ev("message.sent", ANNA, message=message(ANNA, ["user"], "## План\n- шаг 1\n- шаг 2\n\n`код`")),
        ev("message.sent", ANNA, message=message(ANNA, [BORIS], "Посмотри задачу #3")),
        ev("message.sent", "user", message=message("user", [ANNA], "Сделай быстрее")),
        ev("notification", None, recipient="user", kind="u_mcp_pending", text="Анна создала MCP «git»"),
        ev("notification", None, recipient="user", kind="u_budget", text="Бюджет исчерпан"),
        ev("notification", None, recipient="user", kind="u_agent_error", text="У агента Анна ошибка: boom"),
        ev("notification", None, recipient="user", kind="u_goal_done", text="Цель достигнута"),
        ev("notification", None, recipient=ANNA, kind="n_ready", text="Есть задачи к работе"),
        ev("notification", None, recipient="user", kind="u_review_fallback",
           text="Анна завершила задачу #3 «Парсер», а проверить её сейчас больше некому: /approve 3"),
        ev("goal.set", "user", goal={"title": "Калькулятор", "description": "CLI **калькулятор**", "status": "active",
                                     "summary": "", "set_by": "user", "updated_at": NOW}),
        ev("goal.completed", ANNA, goal={"title": "Калькулятор", "description": "", "status": "done",
                                         "summary": "Всё **готово**", "completed_by": ANNA}),
        ev("goal.reopened", "user", goal={"title": "Калькулятор", "status": "active"}, comment="Добавь тесты"),
        ev("agent.hired", "user", name=BORIS, role="Разработчик", kind="llm", model="default", hired_by="user"),
        ev("agent.retired", ANNA, name=BORIS, reason="Задачи закончились", released=[]),
        ev("agent.status", ANNA, name=ANNA, status="working"),
        ev("agent.status", ANNA, name=ANNA, status="error"),
        activity("wake", "You have new messages:\nfrom user: hi"),
        activity("thinking", "Сначала посмотрю доску.\n" * 20),
        activity("text", "Промежуточный ответ"),
        activity("tool_call", json.dumps({"task_id": 3}), tool="task_claim"),
        activity("tool_result", "Claimed task #3 \"Парсер\"\n" + "строка\n" * 30, tool="task_claim", is_error=False),
        activity("tool_result", "Error: no such task", tool="task_claim", is_error=True),
        activity("error", "overloaded: try later — retrying in 5s"),
        activity("nudge", "You still hold #3 in progress"),
        activity("compacted", "Summary of the conversation so far"),
        ev("agent.compacted", ANNA, agent=ANNA, chars=1234),
        ev("agent.usage", ANNA, agent=ANNA, cost=0.01, input_tokens=4000, output_tokens=200, cache_read_tokens=0,
           spent=0.42),
        ev("file.changed", ANNA, path="src/main.py", op="edit"),
        ev("file.locked", ANNA, path="src/main.py", owner=ANNA, expires_at=NOW + 600),
        ev("file.unlocked", None, path="src/main.py", owner=ANNA, expired=True),
        ev("trigger.created", ANNA, trigger={"id": 5, "owner": ANNA, "condition": "task(3).status == 'done'"}),
        ev("trigger.fired", ANNA, trigger={"id": 5, "condition": "every(60)"}, errors=[]),
        ev("trigger.fired", ANNA, trigger={"id": 6, "condition": "every(60)"}, errors=["wake: boom"]),
        ev("trigger.deleted", "user", trigger_id=5, trigger={"id": 5, "condition": "every(60)"}),
        ev("mcp.created", ANNA, server={"name": "git", "owner": ANNA, "scope": "swarm", "status": "pending",
                                        "description": "Git helper", "tools": ["log"]}),
        ev("mcp.status", "user", name="git", status="active"),
        ev("swarm.paused", None, reason="budget"),
        ev("swarm.paused", "user", reason="paused by the user"),
        ev("swarm.resumed", "user"),
        ev("swarm.limits", "user", max_cost_usd=5.0, max_agents=6),
        ev("job.started", ANNA, job=job()),
        ev("job.ended", ANNA, job=job(status="ended", exit_code=0, ended_at=NOW + 245, duration=245.0)),
        ev("job.ended", ANNA, job=job(status="stopped", reason="user", stopped_by="user", ended_at=NOW + 60)),
        {"id": None, "ts": NOW, "type": "trace.started", "actor": ANNA, "data": {"trace": TRACE}},
        {"id": None, "ts": NOW, "type": "trace.span", "actor": ANNA, "data": {"trace": TRACE, "span": SPAN}},
        {"id": None, "ts": NOW, "type": "trace.finished", "actor": ANNA, "data": {"trace": TRACE}},
        {"type": "sessions.changed", "session": "001-calc", "change": "started", "ts": NOW,
         "data": {"session": "001-calc", "change": "started"}},
        {"type": "_connection", "data": {"state": "lost"}},
        {"type": "_connection", "data": {"state": "connected"}},
        ev("something.new", ANNA, x=1),
    ]


def all_renderables(c: RenderContext) -> list:
    out = [render.event(c, e) for e in realistic_events()]
    out += [
        render.welcome(c, version="0.1.0", home="C:/Users/me/DreamTeam", web_url="http://127.0.0.1:8765",
                       session=SESSIONS[0]),
        render.welcome(c, version="0.1.0", home="/home/me", web_url=None, session=None),
        render.tips(c, True), render.tips(c, False),
        render.user_echo(c, "Привет\nвторая строка", ANNA), render.user_echo(c, "hi", "all"),
        render.agent_message(c, message(ANNA, ["user", BORIS], "**Готово**")),
        render.goal_panel(c, None), render.goal_panel(c, {"title": "Цель", "status": "pending"}),
        render.board(c, [task(1, status="done", assignee=ANNA, comments=2, priority=2),
                         task(2, status="in_progress", assignee=BORIS, depends_on=[1]),
                         task(3, status="todo", depends_on=[2], blocked_by=[2], priority=3),
                         task(4, status="review", assignee=BORIS, reviewer=ANNA),
                         task(5, status="cancelled"), task(6, status="failed")]),
        render.board(c, []), render.board_summary(c, [task(1), task(2, status="done")]),
        render.board_summary(c, {"todo": 1, "in_progress": 2}),
        render.task_detail(c, task_data()), render.task_detail(c, {"error": "not found"}),
        render.agents_table(c, AGENTS), render.agents_table(c, []),
        render.agents_table(c, [{**AGENTS[0], "jobs": 2}, {**AGENTS[1], "open_tasks": [], "jobs": 1}]),
        render.jobs_table(c, JOBS), render.jobs_table(c, []), render.jobs_table(c, [{"id": None}, {}]),
        render.job_output(c, JOB_DETAIL), render.job_output(c, "Job j9 of x: y"),
        render.sessions_table(c, SESSIONS, current="001-calc"), render.sessions_table(c, []),
        render.cost_table(c, STATE), render.cost_table(c, {}),
        render.models_table(c, SETTINGS),
        render.roles_table(c, ROLES), render.roles_table(c, []), render.role_detail(c, ROLES[1]),
        render.role_detail(c, {}),
        render.status_report(c, status_info()),
        render.traces_list(c, [TRACE, {**TRACE, "id": 13, "kind": "external", "status": "running"}]),
        render.traces_list(c, []),
        render.trace_detail(c, TRACE), render.span_detail(c, SPAN), render.span_detail(c, TRACE["spans"][2]),
        render.context_view(c, CONTEXT), render.context_view(c, CONTEXT, offset=1, limit=1),
        render.files_view(c, dir_data()), render.files_view(c, file_data()),
        render.files_view(c, {"type": "file", "path": "x.bin", "content": None}),
        render.help_view(c, COMMANDS), render.shortcuts_view(c),
        render.info(c, "Сессия запущена"), render.error(c, "Что-то пошло не так"),
        render.bash_result(c, "ls", 0, "a.txt\nb.txt"), render.bash_result(c, "false", 1, ""),
        render.bash_result(c, "sleep 999", None, "partial"),
    ]
    return [r for r in out if r is not None]


def task_data() -> dict:
    return {"task": task(2, "Парсер выражений", "done", assignee=BORIS, reviewer=ANNA, description="Нужно **быстро**",
                         result="Сделано: parser.py", depends_on=[1], tags=["core"], priority=2),
            "discussion": [{"id": 1, "task_id": 2, "author": ANNA, "text": "Как дела?", "reply_to": None, "ts": NOW},
                           {"id": 2, "task_id": 2, "author": BORIS, "text": "Почти готово", "reply_to": 1, "ts": NOW}],
            "events": [ev("task.created", ANNA, task=task(2)),
                       ev("task.updated", BORIS, task=task(2), changes={"status": ["todo", "in_progress"]}),
                       ev("task.comment", ANNA, task_id=2, post={"text": "Как дела?"})]}


def status_info() -> dict:
    return {"version": "0.1.0", "home": "C:/Users/me/DreamTeam", "web": "http://127.0.0.1:8765",
            "session": SESSIONS[0], "goal": {"title": "Калькулятор", "status": "active"},
            "models": [{"name": "default", "model": "claude-opus-5-5", "default": True}, "local"],
            "language": "ru", "agents": {"total": 3, "working": 1}, "jobs": {"running": 2, "ids": ["j3", "j5"]},
            "tracing": True, "verbosity": "verbose", "connected": False, "extra_key": "value"}


def dir_data() -> dict:
    return {"type": "dir", "path": "src", "entries": [
        {"path": "src/main.py", "type": "file", "size": 1234, "mtime": NOW},
        {"path": "src/lib", "type": "dir", "size": 0, "mtime": NOW},
        {"path": "src/README.md", "type": "file", "size": 5_000_000, "mtime": NOW}]}


def file_data() -> dict:
    code = "def main():\n    print('hello, world')  # " + "a very long comment " * 8 + "\n\nmain()\n"
    return {"type": "file", "path": "src/main.py", "content": code, "truncated": True}


def assert_fits(text: str, width: int) -> None:
    for line in text.split("\n"):
        assert cell_len(line) <= width, f"{cell_len(line)} > {width}: {line!r}"


# ---- i18n -----------------------------------------------------------------------------------------
def test_i18n_languages_have_the_same_keys():
    assert set(i18n.TEXTS) == {"ru", "en"}
    assert set(i18n.TEXTS["ru"]) == set(i18n.TEXTS["en"])


def test_i18n_placeholders_and_plural_forms_match():
    fields = lambda s: {f for _, f, _, _ in string.Formatter().parse(s) if f}  # noqa: E731
    for key, ru in i18n.TEXTS["ru"].items():
        en = i18n.TEXTS["en"][key]
        assert fields(ru) == fields(en), key
        if key.startswith("n_"):
            assert len(ru.split("|")) == 3 and len(en.split("|")) == 2, key


def test_t_falls_back_to_english_then_to_the_key():
    assert i18n.t("ru", "welcome") == "Добро пожаловать в DreamTeam!"
    assert i18n.t("en", "welcome") == "Welcome to DreamTeam!"
    assert i18n.t("de", "welcome") == "Welcome to DreamTeam!"
    assert i18n.t(None, "goal") == "Goal"
    assert i18n.t("ru", "no.such.key") == "no.such.key"
    assert i18n.t("ru", "unknown_command", cmd="/x") == "Неизвестная команда /x. /help — список."
    assert i18n.t("ru", "unknown_command") == i18n.TEXTS["ru"]["unknown_command"]  # missing kw: unformatted
    i18n.TEXTS["en"]["_only_en"] = "only {x}"
    try:
        assert i18n.t("ru", "_only_en", x=1) == "only 1"
    finally:
        del i18n.TEXTS["en"]["_only_en"]


@pytest.mark.parametrize("n,ru,en", [(1, "1 агент", "1 agent"), (2, "2 агента", "2 agents"),
                                     (5, "5 агентов", "5 agents"), (11, "11 агентов", "11 agents"),
                                     (21, "21 агент", "21 agents"),
                                     (22, "22 агента", "22 agents"), (112, "112 агентов", "112 agents"),
                                     (0, "0 агентов", "0 agents")])
def test_tn_plurals(n, ru, en):
    assert i18n.tn("ru", "n_agents", n) == ru
    assert i18n.tn("en", "n_agents", n) == en
    assert i18n.tn("xx", "n_agents", n) == en


def test_every_text_key_used_by_render_exists():
    source = Path(render.__file__).read_text(encoding="utf-8")
    keys = {k for k in re.findall(r"\.tn?\(\s*[\"']([a-z0-9_]+)[\"']", source) if not k.endswith("_")}  # not prefixes
    assert len(keys) > 60
    missing = sorted(k for k in keys if k not in i18n.TEXTS["en"])
    assert not missing, missing
    labels = [("s_", ("todo", "in_progress", "waiting", "review", "done", "failed", "cancelled", "closed")),
              ("a_", ("idle", "working", "paused", "error", "retired")),
              ("tr_", ("running", "done", "error", "step_limit", "refusal", "paused", "stopped", "interrupted")),
              ("mcp_", ("active", "pending", "failed", "rejected", "available", "removed")),
              ("op_", ("create", "write", "edit")), ("p", ("0", "1", "2", "3")),
              ("verbosity_", render.VERBOSITY), ("k_", render.ACTIVITY_KINDS)]
    for prefix, values in labels:
        for value in values:
            assert prefix + value in i18n.TEXTS["en"], prefix + value
    for _, key in render.SHORTCUTS:
        assert key in i18n.TEXTS["en"]


# ---- theme ----------------------------------------------------------------------------------------
def _luminance(color: str) -> float:
    r, g, b = (int(color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def test_agent_colors_are_stable_and_readable_on_dark_and_light():
    names = [ANNA, BORIS, "Вера Котова", "Глеб", "Claude Code", "Dana", "Eve", "Frank", "Gus", "Hana", "Ian"]
    for name in names:
        color = theme.agent_style(name)
        assert color in theme.AGENT_PALETTE
        assert color == theme.agent_style(name) == theme.agent_style(f"  {name.upper()} ")
        assert color == theme.AGENT_PALETTE[zlib.crc32(name.casefold().encode()) % len(theme.AGENT_PALETTE)]
    assert len({theme.agent_style(n) for n in names}) >= 5
    assert theme.agent_style("user") == theme.USER and theme.agent_style(None) == "default"
    for color in theme.AGENT_PALETTE + (theme.ACCENT, theme.OK, theme.ERROR, theme.WARN, theme.BASH):
        lum = _luminance(color)
        assert (lum + 0.05) / 0.05 >= 4.0, color          # on black
        assert 1.05 / (lum + 0.05) >= 2.7, color          # on white
    assert len(set(theme.AGENT_PALETTE)) == len(theme.AGENT_PALETTE)


@pytest.mark.parametrize("encoding,ok", [("utf-8", True), ("UTF-8", True), ("cp65001", True), ("utf-16", True),
                                         ("cp1251", False), ("cp866", False), ("ascii", False), (None, False),
                                         ("", False), ("no-such-codec", False)])
def test_unicode_detection_by_encoding(encoding, ok):
    assert theme.can_encode(encoding) is ok
    stream = io.TextIOWrapper(io.BytesIO(), encoding=encoding) if encoding and ok else type(
        "S", (), {"encoding": encoding})()
    assert theme.unicode_supported(stream) is ok
    assert theme.pick_symbols(stream) is (theme.SYMBOLS if ok else theme.SYMBOLS_ASCII)


def test_symbol_tables():
    assert set(theme.SYMBOLS) == set(theme.SYMBOLS_ASCII)
    assert all(v.isascii() and v for v in theme.SYMBOLS_ASCII.values())
    assert theme.SYMBOLS["bullet"] == "⏺" and theme.SYMBOLS["cont"] == "⎿" and theme.SYMBOLS["todo"] == "☐"
    assert (theme.SYMBOLS_ASCII["bullet"], theme.SYMBOLS_ASCII["cont"], theme.SYMBOLS_ASCII["todo"],
            theme.SYMBOLS_ASCII["doing"], theme.SYMBOLS_ASCII["done"], theme.SYMBOLS_ASCII["failed"]) == \
        ("*", "|_", "[ ]", "[~]", "[x]", "x")
    assert render.SPINNER_FRAMES == ("·", "✢", "✳", "✶", "✻", "✽")


def test_pt_style_builds_and_covers_the_fragment_classes():
    from prompt_toolkit.styles import Style

    Style.from_dict(theme.PT_STYLE)
    c = ctx()
    frags = render.spinner_line(c, frame=0, verb="Мозгуют", working=[ANNA], elapsed=3, tokens_in=10, tokens_out=5,
                                cost=0.5, paused=False)
    frags += render.spinner_line(c, frame=0, verb="x", working=[], elapsed=3, tokens_in=0, tokens_out=0, cost=0,
                                 paused=True)
    for connected, hint, verbosity in ((True, None, "normal"), (False, None, "normal"), (True, "hint", "quiet"),
                                       (True, None, "verbose")):
        left, right = render.status_line(c, session=SESSIONS[0], addressee=ANNA, agents_total=3, working=1,
                                         spent=4.5, budget=5.0, model="m", verbosity=verbosity,
                                         connected=connected, hint=hint)
        frags += left + right
    for style, _ in frags:
        assert style.startswith("class:")
        assert style[len("class:"):] in theme.PT_STYLE, style


# ---- formatting -----------------------------------------------------------------------------------
@pytest.mark.parametrize("n,text", [(0, "0"), (310, "310"), (999, "999"), (1000, "1.0k"), (4200, "4.2k"),
                                    (12_345, "12.3k"), (99_949, "99.9k"), (99_950, "100k"), (123_456, "123k"),
                                    (999_499, "999k"), (999_500, "1.0M"), (999_999, "1.0M"),
                                    (1_300_000, "1.3M"), (None, "0"), ("x", "0"), (-5, "0")])
def test_fmt_tokens(n, text):
    assert render.fmt_tokens(n) == text


def test_fmt_elapsed_cost_ms():
    assert [render.fmt_elapsed(s) for s in (0, 12, 12.9, 59, 60, 185, 3599, 3720)] == \
        ["0s", "12s", "12s", "59s", "1m 00s", "3m 05s", "59m 59s", "1h 02m"]
    assert [render.fmt_cost(v) for v in (0, 0.12, 0.123, 0.004, 5, None, "bad")] == \
        ["$0.00", "$0.12", "$0.12", "$0.004", "$5.00", "$0.00", "$0.00"]
    assert render.fmt_cost(0.0123, 4) == "$0.0123"
    assert [render.fmt_ms(v) for v in (5, 1200, 61_000)] == ["5ms", "1.2s", "1m 01s"]


def test_tool_call_text():
    c = ctx()
    assert render.tool_call_text(c, "task_claim", '{"task_id": 3}') == "task_claim(task_id=3)"
    text = render.tool_call_text(c, "send_message", {"to": "Борис", "text": "x" * 200, "urgent": True,
                                                     "ids": [1, 2]})
    assert text.startswith('send_message(to="Борис", text="xxx') and "…" in text
    assert len(text) <= render.TOOL_CALL_CHARS
    assert render.tool_call_text(c, "x", "not json") == "x(not json)"
    assert render.tool_call_text(c, "end_turn", "") == "end_turn()"
    assert render.tool_call_text(c, None, None) == "?()"


# ---- events: snapshots ----------------------------------------------------------------------------
def test_tool_call_and_result_lines():
    c = ctx()
    out = plain(render.event(c, activity("tool_call", '{"path": "calc.py"}', tool="read_file")))
    assert out == '⏺ Анна Смирнова · read_file(path="calc.py")'
    # a long call stays on one line at normal verbosity, whatever the name's length (the TUI prints at width-1)
    args = json.dumps({"path": "src/calc/main.py", "content": "def add(a, b): return a + b\n" * 5})
    for name in (ANNA, "Григорий Константинопольский"):
        out = plain(render.event(ctx(width=80), activity("tool_call", args, agent=name, tool="write_file")), 79)
        assert "\n" not in out and out.startswith(f"⏺ {name} · write_file(path=") and out.endswith("…")
    out = plain(render.event(c, activity("tool_result", "def add(a, b):\nsecond line", tool="read_file")))
    assert out == "  ⎿  def add(a, b):"
    long_result = "\n".join(f"line {i}" for i in range(1, 31))
    out = plain(render.event(ctx("verbose"), activity("tool_result", long_result, tool="board_list")))
    lines = out.split("\n")
    assert lines[0] == "  ⎿  line 1" and lines[19] == "     line 20" and lines[20] == "     … +10 строк"
    assert len(lines) == 21
    out = plain(render.event(ctx("verbose", "en"), activity("tool_result", long_result, tool="board_list")))
    assert out.split("\n")[-1] == "     … +10 lines"


def test_messages():
    c = ctx()
    to_user = message(ANNA, ["user"], "# План\nВот **план**:\n- a")
    out = plain(render.event(c, ev("message.sent", ANNA, message=to_user)))
    lines = out.split("\n")
    assert lines[0] == "⏺ Анна Смирнова → вам"
    assert lines[1] == "  План" and "  Вот план:" in lines and any(line.strip() == "• a" for line in lines)
    out = plain(render.event(c, ev("message.sent", ANNA, message=message(ANNA, [BORIS], "Посмотри #3"))))
    assert out == "✉ Анна → Борис: Посмотри #3"
    out = plain(render.event(c, ev("message.sent", "user", message=message("user", [ANNA], "Сделай быстрее"))))
    assert out == "> Сделай быстрее   → Анна Смирнова"
    out = plain(render.user_echo(ctx(lang="en"), "hi", "all"))
    assert out == "> hi   → everyone"
    out = plain(render.agent_message(c, message(ANNA, ["user", BORIS], "ok")))
    assert out.split("\n")[0] == "⏺ Анна Смирнова → вам, Борис Петров"
    # messages loaded from /api/messages render the same way
    assert plain(render.agent_message(c, message(ANNA, ["user"], "x"))).startswith("⏺ Анна Смирнова → вам")
    # the user's echo has a background colour in ANSI
    assert "48;2;" in to_ansi(render.user_echo(c, "hi", ANNA), 80)


def test_agent_to_agent_message_shortened_in_normal_full_in_verbose_hidden_in_quiet():
    text = "очень длинное сообщение " * 20
    e = ev("message.sent", ANNA, message=message(ANNA, [BORIS], text))
    normal = plain(render.event(ctx(width=60), e), 60)
    assert "\n" not in normal and normal.endswith("…")
    verbose = plain(render.event(ctx("verbose", width=60), e), 60)
    assert verbose.count("\n") >= 5 and "…" not in verbose
    assert render.event(ctx("quiet"), e) is None


def test_task_lines():
    c = ctx()
    created = plain(render.event(c, ev("task.created", ANNA, task=task(3, "Парсер", goal="Разобрать ввод"))))
    assert created == "☐ #3 Парсер · Разобрать ввод"
    claimed = ev("task.updated", ANNA, task=task(3, status="in_progress", assignee=ANNA),
                 changes={"status": ["todo", "in_progress"], "assignee": [None, ANNA]})
    assert plain(render.event(c, claimed)) == "◐ #3 Парсер — Анна"
    review = ev("task.updated", ANNA, task=task(3, status="review", assignee=ANNA, reviewer=BORIS),
                changes={"status": ["in_progress", "review"]})
    assert plain(render.event(c, review)) == "◑ #3 Парсер — на ревью (Борис)"
    yours = ev("task.updated", ANNA, task=task(3, status="review", assignee=ANNA, reviewer="user"),
               changes={"status": ["in_progress", "review"], "reviewer": [None, "user"]})
    assert plain(render.event(c, yours)) == "◑ #3 Парсер — ждёт вашего ревью"
    assert plain(render.event(ctx(lang="en"), yours)) == "◑ #3 Парсер — waits for your review"
    assert f"bold {theme.WARN}" in str(render._review_label(c, "user")[1])
    done = ev("task.updated", BORIS, task=task(3, status="done", assignee=ANNA, result="Готово: parser.py\nещё"),
              changes={"status": ["review", "done"]})
    assert plain(render.event(c, done)) == "☒ #3 Парсер — Анна\n  ⎿  Готово: parser.py"
    assert plain(render.event(ctx("verbose"), done)).endswith("  ⎿  Готово: parser.py\n     ещё")
    failed = ev("task.updated", ANNA, task=task(3, status="failed", assignee=ANNA, result="boom"),
                changes={"status": ["in_progress", "failed"]})
    assert plain(render.event(c, failed)).startswith("✗ #3 Парсер — Анна · провалена\n  ⎿  boom")
    cancelled = ev("task.updated", "user", task=task(3, status="cancelled"), changes={"status": ["todo", "cancelled"]})
    assert plain(render.event(c, cancelled)) == "✗ #3 Парсер · отменена"
    comment = ev("task.comment", ANNA, task_id=3, post={"author": ANNA, "text": "Кто возьмёт?"})
    assert plain(render.event(c, comment)) == "💬 #3 Анна: Кто возьмёт?"
    assigned = ev("task.updated", "user", task=task(3, assignee=ANNA), changes={"assignee": [None, ANNA]})
    assert plain(render.event(c, assigned)) == "☐ #3 Парсер — Анна"
    released = ev("task.updated", ANNA, task=task(3), changes={"status": ["in_progress", "todo"],
                                                               "assignee": [ANNA, None]})
    assert plain(render.event(c, released)) == "☐ #3 Парсер · снова к работе"
    minor = ev("task.updated", ANNA, task=task(3, priority=3), changes={"priority": [1, 3]})
    assert render.event(c, minor) is None
    assert "приоритет" in plain(render.event(ctx("verbose"), minor))
    for e in (claimed, done, comment, ev("task.created", ANNA, task=task(3))):
        assert render.event(ctx("quiet"), e) is None
    # task lines in ANSI: the done symbol is green
    assert "38;2;58;158;74" in to_ansi(render.event(c, done), 80)


def test_goal_panels():
    c = ctx()
    out = plain(render.event(c, ev("goal.set", "user", goal={"title": "Калькулятор", "description": "CLI",
                                                             "status": "active"})))
    lines = out.split("\n")
    assert lines[0].startswith("╭─ Цель ─") and lines[0].endswith("╮") and lines[-1].startswith("╰")
    assert "│ Калькулятор" in out and "│ CLI" in out
    out = plain(render.event(c, ev("goal.completed", ANNA, goal={"title": "Калькулятор", "status": "done",
                                                                 "summary": "Всё **готово**"})))
    assert out.startswith("╭─ ✓ Цель достигнута ─") and "│ Всё готово" in out
    assert plain(render.goal_panel(ctx(lang="en"), None)).startswith("╭─ Goal ─")
    for e in realistic_events():
        if e["type"].startswith("goal."):
            assert render.event(ctx("quiet"), e) is not None


def test_team_pause_and_notification_lines():
    c = ctx()
    hired = ev("agent.hired", "user", name=ANNA, role="Аналитик", kind="llm", model="default", hired_by="user")
    assert plain(render.event(c, hired)) == "+ Анна Смирнова (Аналитик) в команде"
    assert plain(render.event(ctx("verbose"), hired)).endswith("· модель: default · инициатор: вы")
    assert plain(render.event(ctx(lang="en"), hired)) == "+ Анна Смирнова (Аналитик) joined the team"
    retired = ev("agent.retired", "user", name=ANNA, reason="")
    assert plain(render.event(c, retired)) == "− Анна Смирнова больше не в команде"
    assert plain(render.event(c, ev("swarm.paused", None, reason="budget"))) == "⏸ Рой на паузе (исчерпан бюджет)"
    assert plain(render.event(c, ev("swarm.paused", "user", reason="paused by the user"))) == \
        "⏸ Рой на паузе (остановлен вами)"
    assert plain(render.event(c, ev("swarm.resumed", "user"))) == "▶ Рой продолжает работу"
    assert plain(render.event(c, ev("swarm.limits", "user", max_cost_usd=5, max_agents=6))) == \
        "· Лимиты: бюджет $5.00, агентов не больше 6"
    note = ev("notification", None, recipient="user", kind="u_mcp_pending", text="Одобрите MCP «git»")
    assert plain(render.event(c, note)) == "🔔 Одобрите MCP «git»"
    assert plain(render.event(c, activity("error", "overloaded"))) == "⚠ Анна Смирнова: overloaded"
    assert plain(render.event(c, {"type": "_connection", "data": {"state": "lost"}})).startswith("⚠ Нет связи")
    assert render.event(c, {"type": "_connection", "data": {"state": "connected"}}) is None


def test_background_job_lines():
    c = ctx()
    assert plain(render.event(c, ev("job.started", ANNA, job=job()))) == \
        "⚙ Анна Смирнова · фоновый процесс j3: npm run dev"
    ended = ev("job.ended", ANNA, job=job(status="ended", exit_code=0, ended_at=NOW + 245, duration=245.0,
                                          command="npm test"))
    assert plain(render.event(c, ended)) == "⚙ Анна Смирнова · процесс j3 завершился с кодом 0 (4m 05s): npm test"
    assert plain(render.event(ctx(lang="en"), ended)) == \
        "⚙ Анна Смирнова · job j3 ended with exit code 0 (4m 05s): npm test"
    cases = {("ended", None): "завершился (1m 00s)", ("stopped", "user"): "остановлен вами (1m 00s)",
             ("stopped", "lifetime"): "остановлен по пределу времени", ("stopped", "session"): "вместе с сессией",
             ("stopped", "retired"): "агент ушёл из роя", ("stopped", "owner"): "остановлен владельцем",
             ("stopped", "new"): "процесс j3 остановлен (1m 00s)", ("lost", "crash"): "потерян при сбое"}
    for (status, reason), text in cases.items():  # without a duration it is counted from the times
        line = plain(render.event(c, ev("job.ended", ANNA, job=job(status=status, reason=reason, ended_at=NOW + 60))))
        assert text in line, (status, reason, line)
    assert render.event(ctx("quiet"), ended) is None  # the user hears of jobs through notifications there
    assert plain(render.event(ctx("verbose", unicode=False), ended)).startswith("& Анна Смирнова - процесс j3")


def test_verbose_only_lines():
    v, n = ctx("verbose"), ctx()
    thinking = activity("thinking", "\n".join(f"мысль {i}" for i in range(20)))
    out = plain(render.event(v, thinking))
    assert out.startswith("✻ Анна Смирнова думает…") and "мысль 11" in out and "мысль 12" not in out
    assert "+8 строк" in out
    wake = activity("wake", "You have new messages:\nfrom user: hi")
    assert plain(render.event(v, wake)) == "⏰ Анна Смирнова просыпается: You have new messages: from user: hi"
    # a real wake-up text: its header and the "what happened" label say nothing, the first reason does
    real = activity("wake", "=== Пробуждение 2026-10-08 19:37 ===\nЧто произошло:\n"
                            "• Сообщение #5 от user для тебя:\nкак дела?\nвторая строка\nЦель: x [active]")
    assert plain(render.event(v, real)) == "⏰ Анна Смирнова просыпается: Сообщение #5 от user для тебя: как дела?"
    note = activity("wake", "=== Wake-up 2026-10-08 19:37 ===\nWhat happened:\n• Task #3 is ready\n• other")
    assert plain(render.event(v, note)) == "⏰ Анна Смирнова просыпается: Task #3 is ready"
    assert "напоминание" in plain(render.event(v, activity("nudge", "You hold #3")))
    assert "контекст сжат" in plain(render.event(v, activity("compacted", "summary")))
    assert "src/main.py — изменён · Анна" in plain(render.event(v, ev("file.changed", ANNA, path="src/main.py",
                                                                      op="edit")))
    for e in (thinking, wake, activity("nudge", "x"), activity("compacted", "x"), activity("text", "x"),
              ev("file.changed", ANNA, path="a", op="edit"), ev("trigger.created", ANNA, trigger={"id": 1}),
              ev("agent.status", ANNA, name=ANNA, status="error"),
              ev("notification", None, recipient=ANNA, kind="n_ready", text="x")):
        assert render.event(n, e) is None, e["type"]
        assert render.event(v, e) is not None, e["type"]


def test_quiet_shows_only_user_messages_goal_errors_approvals_and_pause():
    q = ctx("quiet")
    shown = {(e["type"], (e.get("data") or {}).get("kind")) for e in realistic_events()
             if render.event(q, e) is not None}
    assert shown == {("message.sent", None), ("notification", "u_mcp_pending"), ("notification", "u_budget"),
                     ("notification", "u_agent_error"), ("notification", "u_review_fallback"),
                     ("goal.set", None), ("goal.completed", None),
                     ("goal.reopened", None), ("swarm.paused", None), ("swarm.resumed", None),
                     ("swarm.limits", None), ("_connection", None), ("trigger.fired", None)}
    # ...the trigger.fired among them is the one with errors: errors show at every verbosity
    fired = ev("trigger.fired", ANNA, trigger={"id": 4}, errors=["Unknown task #9"])
    assert plain(render.event(q, fired)) == plain(render.event(ctx(), fired)) == "⚠ Ошибки триггера #4: Unknown task #9"
    assert render.event(q, ev("trigger.fired", ANNA, trigger={"id": 4}, errors=[])) is None
    # the agent-to-agent message is not among them; the error comes once, as the notification
    assert render.event(q, ev("message.sent", ANNA, message=message(ANNA, [BORIS], "x"))) is None
    assert render.event(q, activity("error", "boom")) is None
    assert plain(render.event(q, ev("notification", None, recipient="user", kind="u_agent_error",
                                    text="У агента Анна ошибка: boom"))) == "⚠ У агента Анна ошибка: boom"
    # in normal mode the activity error is shown and the duplicate notification is not
    assert render.event(ctx(), ev("notification", None, recipient="user", kind="u_agent_error", text="x")) is None


def test_trace_and_unknown_events():
    for verbosity in render.VERBOSITY:
        c = ctx(verbosity)
        for type_ in ("trace.started", "trace.span", "trace.finished", "trace.anything"):
            assert render.event(c, {"type": type_, "data": {"trace": TRACE}}) is None
        for type_ in ("agent.usage", "agent.compacted"):
            assert render.event(c, ev(type_, ANNA)) is None
    unknown = ev("brand.new", ANNA, x=1)
    assert render.event(ctx("quiet"), unknown) is None and render.event(ctx(), unknown) is None
    assert plain(render.event(ctx("verbose"), unknown)) == '· brand.new Анна Смирнова: {"x": 1}'
    unknown_kind = activity("telepathy", "x")
    assert render.event(ctx(), unknown_kind) is None
    assert "telepathy" in plain(render.event(ctx("verbose"), unknown_kind))


def test_event_types_cover_the_server():
    """EVENT_TYPES lists every type emitted by the server code (bus.emit / tracer publish)."""
    src = Path(render.__file__).resolve().parents[1]
    emitted = set()
    for path in list(src.glob("*.py")) + list((src / "tools").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        emitted |= set(re.findall(r"(?:emit|_publish|publish)\(\s*\"([a-z_]+\.[a-z_]+|notification)\"", text))
    assert emitted, "no emit() calls found"
    assert emitted <= set(render.EVENT_TYPES), emitted - set(render.EVENT_TYPES)
    spec = (src.parents[1] / "docs" / "SPEC.md").read_text(encoding="utf-8")
    section = spec.split("## 9.", 1)[1].split("## 10.", 1)[0]
    assert set(re.findall(r"[a-z_]+\.[a-z_]+|notification", section)) <= set(render.EVENT_TYPES)
    agent_src = (src / "agent.py").read_text(encoding="utf-8")
    kinds = set(re.findall(r"self\._activity\(\"([a-z_]+)\"", agent_src))
    assert kinds and kinds <= set(render.ACTIVITY_KINDS), kinds - set(render.ACTIVITY_KINDS)


# ---- events: robustness ---------------------------------------------------------------------------
def fuzz_events() -> list:
    events = list(realistic_events())
    for type_ in render.EVENT_TYPES + ("unknown.type",):
        events += [{"type": type_, "data": {}}, {"type": type_}, {"type": type_, "data": None, "actor": None},
                   {"type": type_, "data": {"task": None, "message": None, "goal": None, "post": None,
                                            "server": None, "trigger": None, "changes": None, "recipients": None}},
                   {"type": type_, "actor": 5, "data": {"task": "x", "message": [], "goal": 3, "post": "y",
                                                         "changes": {"status": "done"}, "text": None, "kind": 7,
                                                         "name": None, "reason": None, "errors": "e"}}]
    for kind in render.ACTIVITY_KINDS + ("other",):
        events += [{"type": "agent.activity", "data": {"kind": kind}},
                   {"type": "agent.activity", "data": {"kind": kind, "text": None, "agent": None, "tool": None}},
                   {"type": "agent.activity", "data": {"kind": kind, "text": {"a": 1}, "agent": 3, "tool": [1]}}]
    events += [None, "text", 5, [], {}, {"type": None}, {"type": 5, "data": "x"},
               ev("message.sent", ANNA, message={"sender": None, "recipients": "user", "text": None}),
               ev("task.updated", ANNA, task={"id": None, "status": "weird"}, changes={"status": [None]})]
    return events


@pytest.mark.parametrize("unicode", [True, False])
@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("verbosity", render.VERBOSITY)
def test_every_event_renders_without_errors_and_fits(verbosity, lang, unicode):
    for width in (40, 120):
        c = ctx(verbosity, lang, width, unicode)
        for e in fuzz_events():
            renderable = render._event(c, e)  # the unguarded path: no exception may be swallowed
            assert render.event(c, e) is not None or renderable is None
            if renderable is None:
                continue
            text = plain(render._out(c, renderable), width)
            assert ESC not in text
            assert_fits(text, width)
            if not unicode:
                assert not UNICODE_SYMBOLS & set(text), (e, text)


@pytest.mark.parametrize("width", [40, 120])
@pytest.mark.parametrize("unicode", [True, False])
def test_every_view_fits_the_width(width, unicode):
    for lang in ("ru", "en"):
        for verbosity in render.VERBOSITY:
            c = ctx(verbosity, lang, width, unicode)
            for renderable in all_renderables(c):
                text = plain(renderable, width)
                assert ESC not in text
                assert_fits(text, width)
                ansi = to_ansi(renderable, width)
                assert_fits(render._ANSI.sub("", ansi), width)
                if not unicode:
                    assert not UNICODE_SYMBOLS & set(text), text


def test_ascii_mode_with_ascii_data_is_pure_ascii():
    c = ctx("verbose", "en", 100, unicode=False)
    def ascii_only(value):
        if isinstance(value, str):
            return re.sub(r"[^\x00-\x7f]", "a", value)
        if isinstance(value, dict):
            return {ascii_only(k): ascii_only(v) for k, v in value.items()}
        if isinstance(value, list):
            return [ascii_only(v) for v in value]
        return value

    events = ascii_only(realistic_events())
    trace, span, context, sessions, agents, detail = ascii_only([TRACE, SPAN, CONTEXT, SESSIONS, AGENTS,
                                                                 task_data()])
    renderables = [render.event(c, e) for e in events] + [
        render.welcome(c, version="1", home="/h", web_url="http://x", session=sessions[0]), render.tips(c, True),
        render.trace_detail(c, trace), render.span_detail(c, span), render.context_view(c, context),
        render.sessions_table(c, sessions, "001-calc"), render.agents_table(c, agents),
        render.task_detail(c, detail), render.cost_table(c, {"agents": agents, "swarm": {}}),
        render.help_view(c, [("/help", "", "help")]), render.shortcuts_view(c), render.goal_panel(c, None),
        render.event(c, ev("message.sent", "Ann", message=message("Ann", ["user"], "# T\n- a\n\n> q\n\n---\n"))),
        render.files_view(c, dir_data() | {"entries": [{"path": "a/b.py", "type": "file", "size": 1}]}),
        render.bash_result(c, "ls", 2, "out")]
    for renderable in renderables:
        if renderable is not None:
            text = plain(renderable)
            assert text.isascii(), [ch for ch in text if not ch.isascii()]
    line = render.spinner_line(c, frame=3, verb="Pondering", working=["Ann B"], elapsed=5, tokens_in=1,
                               tokens_out=2, cost=0.1, paused=False)
    assert "".join(text for _, text in line).isascii()
    left, right = render.status_line(c, session={"title": "t"}, addressee="Ann", agents_total=2, working=1,
                                     spent=1, budget=2, model="m", verbosity="normal", connected=False, hint=None)
    assert "".join(text for _, text in left + right).isascii()


def test_agent_text_cannot_drive_the_terminal():
    evil = "hi\x1b]0;pwned\x07 \x1b[2J\x1b[31mred\x1b[0m \x9b31m \x07bell\rcr \ud800"
    c = ctx()
    for renderable in (render.event(c, ev("message.sent", ANNA, message=message(ANNA, ["user"], evil))),
                       render.event(c, activity("tool_result", evil)), render.bash_result(c, "x", 0, evil),
                       render.event(ctx("verbose"), activity("thinking", evil))):
        out = to_ansi(renderable, 80)
        assert "pwned" not in out and "[2J" not in out and "\x07" not in out and "\x9b" not in out
        assert "\r" not in out and "\ud800" not in out
        assert "red" in to_plain(renderable, 80)


# ---- views ----------------------------------------------------------------------------------------
def test_board_and_summary():
    tasks = [task(1, "План", "done", assignee=ANNA, comments=2, priority=2),
             task(2, "Код", "in_progress", assignee=BORIS, depends_on=[1]),
             task(3, "Тесты", "todo", depends_on=[2], blocked_by=[2], priority=3),
             task(4, "Ревью", "review", assignee=BORIS, reviewer=ANNA),
             task(5, "Старое", "cancelled"), task(6, "Сломалось", "failed", assignee=ANNA)]
    out = plain(render.board(ctx(), tasks))
    assert out.split("\n")[0] == "К работе (1)"
    assert "  ☐ #3 Тесты · критичный · ждёт #2" in out
    assert "  ◐ #2 Код — Борис" in out
    assert "  ◑ #4 Ревью — Борис · на ревью (Анна)" in out
    yours = plain(render.board(ctx(lang="en"), [task(7, "Docs", "review", assignee=BORIS, reviewer="user")]))
    assert "  ◑ #7 Docs — Борис · waits for your review" in yours
    assert "  ☒ #1 План — Анна · высокий · 💬2" in out
    assert "Закрыто (2)" in out and "  ✗ #5 Старое" in out and "  ✗ #6 Сломалось — Анна" in out
    assert out.index("К работе") < out.index("В работе") < out.index("Ревью (1)") < out.index("Готово") < \
        out.index("Закрыто")
    assert plain(render.board_summary(ctx(), tasks)) == "Задачи: ☐ 1 · ◐ 1 · ◑ 1 · ☒ 1 · ✗ 2"
    assert plain(render.board_summary(ctx(), {"todo": 3, "in_progress": 2, "done": 5})) == \
        "Задачи: ☐ 3 · ◐ 2 · ☒ 5"
    assert plain(render.board_summary(ctx(lang="en", unicode=False), {"todo": 3, "done": 5})) == \
        "Tasks: [ ] 3 - [x] 5"
    assert plain(render.board(ctx(lang="en"), [])) == "  ⎿  No tasks yet"
    many = [task(i, f"T{i}", "done") for i in range(1, 31)]
    assert "#1 " not in plain(render.board(ctx(), many)) and "#30 " in plain(render.board(ctx(), many))
    assert "#1 " in plain(render.board(ctx("verbose"), many))


def test_task_detail():
    out = plain(render.task_detail(ctx(), task_data()))
    lines = out.split("\n")
    assert lines[0] == "☒ #2 Парсер выражений"
    for expected in ("Статус       Готово", "Исполнитель  Борис Петров", "Ревьюер      Анна Смирнова",
                     "Приоритет    высокий", "Теги         core", "Зависит от   #1 ✓", "Цель задачи",
                     "  Разобрать ввод", "Описание", "  Нужно быстро", "Итог", "  Сделано: parser.py",
                     "Обсуждение (2)", "   Как дела?", "   Почти готово"):
        assert expected in out, expected
    assert re.search(r"💬 Анна Смирнова · \d\d:\d\d · #1", out)
    assert "↳ ответ на #1" in out
    assert "История" not in out
    verbose = plain(render.task_detail(ctx("verbose"), task_data()))
    assert "История" in verbose and "статус: К работе → В работе" in verbose
    assert plain(render.task_detail(ctx(), {"error": "Task #9 not found"})) == "⚠ Task #9 not found"
    assert "/approve" not in out  # a done task: nothing to settle
    review = task_data()
    review["task"] = {**review["task"], "status": "review", "reviewer": "user"}
    out = plain(render.task_detail(ctx(lang="en"), review))
    assert "Approve: /approve 2 [comment] · return: /reject 2 what to fix" in out
    assert out.index("Status") < out.index("/approve 2") < out.index("Task goal")


def test_review_notices_for_the_user_are_one_warning_line():
    c = ctx(lang="en")
    budget = ev("notification", None, recipient="user", kind="u_budget", text="The budget is spent")
    warn = re.search(r"38;2;\d+;\d+;\d+", to_ansi(render.event(c, budget), 200)).group(0)  # the warning colour
    for kind in ("u_review_requested", "u_review_fallback", "u_review_handed_over", "u_reviews_waiting"):
        note = ev("notification", None, recipient="user", kind=kind,
                  text="Anna finished task #3 \"Parser\": /approve 3 [comment] or /reject 3 <what to fix>.")
        out = plain(render.event(c, note), 200)
        assert out == "🔔 Anna finished task #3 \"Parser\": /approve 3 [comment] or /reject 3 <what to fix>.", kind
        assert warn in to_ansi(render.event(c, note), 200), kind
        assert render.event(ctx("quiet", "en"), note) is not None   # the user has to act: shown even when quiet
    other = ev("notification", None, recipient=BORIS, kind="n_review_requested", text="Review task #3")
    assert render.event(c, other) is None and "Review task #3" in plain(render.event(ctx("verbose", "en"), other))


def test_role_catalog_views():
    c = ctx(width=100)
    out = plain(render.roles_table(c, ROLES))
    assert out.startswith("Каталог ролей")
    assert "Тестировщик" in out and "fast" in out and "Дата-инженер" in out
    assert "Ты проверяешь, что сделанное другими действительно работает." in out
    assert "Код, который ты написал сам" not in out                       # only the first sentence
    assert out.endswith("/roles Роль — полное описание · /hire Роль — нанять · изменить каталог: /settings")
    assert "fast" not in plain(render.roles_table(ctx(width=60), ROLES), 60)  # narrow: no model column
    assert plain(render.roles_table(ctx(lang="en"), [])) == "  ⎿  The role catalog is empty."
    detail = plain(render.role_detail(c, ROLES[1]))
    assert detail.startswith("╭─ Тестировщик · встроенная ─")
    for needle in ("Код, который ты написал сам", "Доп. инструкции: Пиши тесты на pytest.", "профиль модели: fast"):
        assert needle in detail, needle
    own = plain(render.role_detail(ctx(lang="en"), ROLES[2]))
    assert own.startswith("╭─ Дата-инженер ─") and "built-in" not in own and "model profile" not in own


def test_jobs_table_and_one_job(monkeypatch):
    monkeypatch.setattr(render.time, "time", lambda: NOW)   # the running times
    out = plain(render.jobs_table(ctx(width=140), JOBS), 140)
    head = out.split("\n")[1]
    for column in ("Процесс", "Агент", "Состояние", "Время", "Команда"):
        assert column in head, column
    rows = {m.group(1): line for line in out.split("\n") if (m := re.search(r"│ (j\d) ", line))}
    assert list(rows) == ["j5", "j3", "j4", "j2", "j1", "j6", "j7"]            # in the order of the state
    assert "Борис Петров" in rows["j5"] and "работает" in rows["j5"] and "pytest -q tests/test_parser.py" in rows["j5"]
    assert "4m 05s" in rows["j5"] and "25m 00s" in rows["j3"]
    assert "ушёл в фон по тайм-ауту" in out and "запущен в фоне" in out           # how a running one got there
    assert "node.exe ×2, conhost.exe" in out                                      # its processes
    assert "завершился с кодом 1" in rows["j4"] and "4m 00s" in rows["j4"]
    assert "завершился" in rows["j2"] and "кодом" not in rows["j2"]               # a detached job: no shell code
    assert "остановлен вами" in rows["j1"] and "остановлен по пределу времени" in rows["j6"]
    assert "потерян при сбое" in rows["j7"]
    assert out.endswith("/jobs jN — вывод процесса · /jobs stop jN — остановить · пауза роя их не останавливает")
    assert ".scratch/anna/jobs/j3.log" not in out and ".scratch/anna/jobs/j3.log" in plain(
        render.jobs_table(ctx("verbose", width=140), JOBS), 140)                  # the log in verbose
    narrow = plain(render.jobs_table(ctx(lang="en", width=50), JOBS), 50)
    assert "Agent" not in narrow and "Time" not in narrow and "Борис" in narrow and "running" in narrow
    assert plain(render.jobs_table(ctx(lang="en"), [])) == "  ⎿  No background jobs."
    ansi = to_ansi(render.jobs_table(ctx(width=140), JOBS[:3]), 140)
    assert re.search(r"38;2;\d+;\d+;\d+m.*завершился с кодом 1", ansi)              # a failed job in colour
    ascii_out = plain(render.jobs_table(ctx(lang="en", width=140, unicode=False), JOBS), 140)
    assert "node.exe x2" in ascii_out and not UNICODE_SYMBOLS & set(ascii_out)

    detail = plain(render.job_output(ctx(), JOB_DETAIL))
    lines = detail.split("\n")
    assert lines[0] == "⚙ Job j3 of Анна Смирнова: npm run dev"
    assert lines[1] == "  ⎿  State: running for 25 min (started in the background) [bash]"
    assert lines[-1] == "     Listening on :5173"
    assert plain(render.job_output(ctx(unicode=False), "Job j9 of x: y")) == "& Job j9 of x: y"
    long = "Job j1 of x: y\n" + "\n".join(f"line {i}" for i in range(300))
    assert "line 299" not in plain(render.job_output(ctx(), long)) and "+100 строк" in plain(
        render.job_output(ctx(), long))
    assert "line 299" in plain(render.job_output(ctx("verbose"), long))


def test_agents_table_marks_members_with_running_jobs():
    agents = [{**AGENTS[0], "jobs": 2}, {**AGENTS[1], "open_tasks": [], "jobs": 1}, AGENTS[2]]
    out = plain(render.agents_table(ctx(width=120), agents), 120)
    assert "#3 ⚙2" in out and re.search(r"│ ⚙1 +│", out)
    assert out.count("⚙") == 2
    narrow = plain(render.agents_table(ctx(width=60), agents), 60)   # no tasks column: after the status
    assert "работает ⚙2" in narrow and "свободен ⚙1" in narrow
    assert "#3 &2" in plain(render.agents_table(ctx(width=120, unicode=False), agents), 120)


def test_tables():
    out = plain(render.agents_table(ctx(width=120), AGENTS), 120)
    assert out.startswith("╭") and "│ ⏺ Анна Смирнова" in out and "Координатор" in out and "работает" in out
    assert "↑42.0k ↓3.1k" in out and "$0.42" in out and "Помощник (внешний агент)" in out
    narrow = plain(render.agents_table(ctx(width=40), AGENTS), 40)
    assert "Координатор" not in narrow and "работает" in narrow
    out = plain(render.cost_table(ctx(), STATE))
    assert "Итого" in out and "43.2k" in out and out.endswith("Потрачено $0.42 из $5.00 (8%)")
    assert plain(render.cost_table(ctx(lang="en"), {"agents": [], "swarm": {"spent_usd": 1}})).endswith(
        "Spent $1.00 (no limit)")
    out = plain(render.sessions_table(ctx(), SESSIONS, current="001-calc"))
    assert "▶" in out and "Калькулятор  001-calc" in out and "работает" in out and "цель достигнута" in out
    assert "$0.42/$5.00" in out and "1/3" in out and "4/4" in out
    assert plain(render.sessions_table(ctx(lang="en"), [])).startswith("  ⎿  No sessions yet")
    out = plain(render.models_table(ctx(width=140), SETTINGS), 140)
    assert "Профили моделей" in out and "✓" in out and "ключ задан …abcd" in out
    assert "переменная $OLLAMA_KEY не задана" in out and "abcd" in out and "api_key" not in out


def test_status_report():
    out = plain(render.status_report(ctx(), status_info()))
    for expected in ("Статус", "Версия         0.1.0", "Сессия         Калькулятор (001-calc) · работает",
                     "Цель           Калькулятор · в работе",
                     "Модели         default (claude-opus-5-5) ✓, local", "Агенты         3 агента (1 в работе)",
                     "Трейсы         вкл", "Подробность    подробно", "Связь с хабом  нет связи",
                     "extra_key      value"):
        assert expected in out, expected
    done = status_info() | {"goal": {"title": "Калькулятор", "status": "done"}}
    assert "Цель           Калькулятор · цель достигнута" in plain(render.status_report(ctx(), done))
    # the session folder (/cd): a text row right after the session
    folder = status_info() | {"workspace": "C:/proj/app (папка проекта: агенты коммитят в её текущую ветку)"}
    out = plain(render.status_report(ctx(), folder))
    assert "Папка сессии   C:/proj/app (папка проекта: агенты коммитят в её текущую ветку)" in out
    assert out.index("Сессия ") < out.index("Папка сессии") < out.index("Цель ")


def test_status_report_counts_the_running_jobs():
    out = plain(render.status_report(ctx(), status_info()))
    assert "Процессы       2 работают (j3, j5)" in out
    assert out.index("Агенты ") < out.index("Процессы ") < out.index("Трейсы ")
    one = status_info() | {"jobs": {"running": 1, "ids": ["j7"]}}
    assert "Процессы       1 работает (j7)" in plain(render.status_report(ctx(), one))
    out = plain(render.status_report(ctx(lang="en"), status_info() | {"jobs": {"running": 0, "ids": []}}))
    assert "Jobs            0 running" in out and "(" not in out.split("Jobs", 1)[1].split("\n")[0]


def test_traces():
    out = plain(render.traces_list(ctx(width=120), [TRACE]), 120)
    first = out.split("\n")[0]
    assert first.startswith("⏺ #12 Анна Смирнова · готово · 3 шага · 2 LLM · 2 вызова · 1 ошибка · 12.3s · "
                            "↑ 4.2k ↓ 310 · $0.0123 · задачи #3")
    assert "  ⎿  Turn ended (wait)" in out and out.endswith("/trace N — трейс по шагам, /span N — шаг полностью")
    assert "Причина пробуждения" in plain(render.traces_list(ctx("verbose", width=120), [TRACE]), 120)
    assert plain(render.traces_list(ctx(), [])) == "  ⎿  Трейсов пока нет"
    out = plain(render.trace_detail(ctx(width=120), TRACE), 120)
    assert out.startswith("⏺ Трейс #12 Анна Смирнова · готово")
    for expected in ("Причина пробуждения:", "⏰ [0] input/wake: You have new messages  #40",
                     "⏺ [1] claude-opus-5-5 · 1.2s · ↑ 4.1k ↓ 200 · $0.0100 · stop=tool_use  #41",
                     "  ⎿  Надо взять задачу", "  ⎿  → task_claim(task_id=3)",
                     "⏺ [1] task_claim(task_id=3) · 12ms  #42", "  ⎿  Claimed task #3",
                     "⏺ [2] write_file(path=\"a.py\") · 5ms  #43", "  ⎿  Error: a.py is locked",
                     "  ⎿  overloaded (повтор через 5s)", "· [3] event/reply_to_user: Готово  #45",
                     "⎿ Итог: Turn ended (wait)"):
        assert expected in out, expected
    out = plain(render.span_detail(ctx(), SPAN))
    for expected in ("Шаг #41 · трейс #12 · Анна Смирнова · шаг 1", "llm claude-opus-5-5 · ok · 1.2s",
                     "Токены: ↑ 4.1k ↓ 200 (cache 100) · $0.0100",
                     "Запрос: 5 сообщ. в контексте (эпоха 0) — /span 41 ctx",
                     "Размышления", "  Надо взять задачу", "Вызовы инструментов", "  → task_claim", '"task_id": 3'):
        assert expected in out, expected
    out = plain(render.span_detail(ctx(lang="en"), {**TRACE["spans"][3], "trace_id": 12, "agent": ANNA}))
    assert "Input" in out and '"path": "a.py"' in out and "Output" in out and "Error: a.py is locked" in out
    out = plain(render.context_view(ctx(), CONTEXT, offset=0, limit=2))
    for expected in ("Контекст LLM-шага #41 (Анна Смирнова, claude-opus-5-5)",
                     "3 сообщ., показаны 1–2 · инструментов: 2 · системный промпт: 22 симв.", "SYSTEM",
                     "  You are Anna.", "[0] USER", "  wake up", "[1] ASSISTANT", "  → task_claim(task_id=3)",
                     "ещё 1 сообщ.: /span 41 ctx 2"):
        assert expected in out, expected
    out = plain(render.context_view(ctx(lang="en"), CONTEXT, offset=2, limit=5))
    assert "SYSTEM" not in out and "[2] TOOL task_claim" in out and "more message" not in out
    assert "no messages from offset 9" in plain(render.context_view(ctx(lang="en"), CONTEXT, offset=9))


def test_files_view():
    out = plain(render.files_view(ctx(), dir_data()))
    assert out.split("\n")[0] == "▸ src/"
    lines = out.split("\n")[1:]
    assert lines[0].lstrip().startswith("lib/") and "main.py" in lines[1] and "1.2 KB" in lines[1]
    assert "README.md" in lines[2] and "4.8 MB" in lines[2]
    assert plain(render.files_view(ctx(lang="en"), {"type": "dir", "path": ".", "entries": []})) == \
        "▸ ./\n  (empty folder)"
    c = ctx(width=60)
    out = plain(render.files_view(c, file_data()), 60)
    lines = out.split("\n")
    assert lines[0] == "src/main.py  (показано начало файла)"
    assert re.match(r"\s*1 def main\(\):", lines[1]) and re.match(r"\s*2 +print\('hello, world'\)", lines[2])
    assert len(lines) > 5  # the long comment wraps
    assert_fits(out, 60)
    ansi = to_ansi(render.files_view(c, file_data()), 60)
    assert ESC in ansi and "def" in ansi  # highlighted
    assert plain(render.files_view(ctx(), {"error": "Path not found: 'x'."})) == "⚠ Path not found: 'x'."
    md = plain(render.files_view(ctx(), {"type": "file", "path": "README.md", "content": "# T\n"}))
    assert "# T" in md  # files are shown as source, not rendered
    out = plain(render.files_view(ctx(), {"type": "file", "path": "a.py", "content": "x = 1\n\ny = 2\n"}))
    assert re.match(r"\s*3 y = 2$", out.split("\n")[-1])  # the final newline is no numbered line of its own


def test_welcome_tips_help_shortcuts():
    out = plain(render.welcome(ctx(), version="0.1.0", home="C:/me/DreamTeam", web_url="http://127.0.0.1:8765",
                               session=SESSIONS[0]))
    lines = out.split("\n")
    assert lines[0].startswith("╭") and lines[-1].startswith("╰")
    assert "│ ✻ Добро пожаловать в DreamTeam!  v0.1.0" in lines[1]
    assert lines[2].strip("│ ") == ""
    assert "/help — справка · /sessions — сессии · ? — горячие клавиши" in lines[3]
    assert "home: C:/me/DreamTeam" in out and "web:  http://127.0.0.1:8765" in out
    assert "сессия: Калькулятор (001-calc) · работает" in out
    out = plain(render.welcome(ctx(lang="en"), version="1", home="/h", web_url=None, session=None))
    assert "Welcome to DreamTeam!" in out and "web:  off" in out and "session:" not in out
    out = plain(render.tips(ctx(), False))
    assert out.split("\n")[0].strip() == "Советы для начала:"
    # (a long tip wraps: its next lines are indented under its text)
    assert [line.strip()[:2] for line in out.split("\n")[1:] if line.strip()[:1].isdigit()] == ["1.", "2.", "3.", "4."]
    assert "Первая строка станет заголовком цели" in out
    assert "/sessions" in plain(render.tips(ctx(), True)) and "/sessions" not in out
    out = plain(render.help_view(ctx(), COMMANDS))
    assert out.split("\n")[0] == "Команды"
    assert re.search(r"/help +справка", out) and re.search(r"/new +\[цель\] +новая сессия", out)
    assert re.search(r"/board +доска задач", out) and re.search(r"/task +N +задача с обсуждением", out)
    assert re.search(r"/help +help", plain(render.help_view(ctx(lang="en"), COMMANDS)))
    command = SimpleNamespace(name="exit", args="", aliases=["quit"], description={"ru": "выход", "en": "quit"})
    assert re.search(r"/exit, /quit +quit", plain(render.help_view(ctx(lang="en"), [command])))
    assert re.search(r"/trace \[Имя\|N\] +трейсы", plain(render.help_view(ctx(width=40), [
        {"name": "/trace", "args": "[Имя|N]", "description": "трейсы"}]), 40))
    out = plain(render.shortcuts_view(ctx(width=100)))
    assert out.split("\n")[0] == "Горячие клавиши"
    assert re.search(r"Enter +отправить .* ↑/↓ +подсказки / история", out)  # two columns
    assert re.search(r"Esc +закрыть / пауза роя", out)
    assert "Shift+Tab" in out and "Ctrl+C" in out and "@" in out and "↑/↓" in out
    assert "Ctrl+O" in plain(render.shortcuts_view(ctx(width=40)), 40)


def test_info_error_bash():
    assert plain(render.info(ctx(), "Готово")) == "  ⎿  Готово"
    assert plain(render.info(ctx(unicode=False), "ok")) == "  |_ ok"
    assert plain(render.error(ctx(), "Плохо")) == "⚠ Плохо"
    assert plain(render.error(ctx(unicode=False), "bad")) == "! bad"
    assert "38;2;224;71;91" in to_ansi(render.error(ctx(), "x"), 40)  # red
    assert plain(render.bash_result(ctx(), "ls", 0, "a\nb\n")) == "! ls\n  ⎿  a\n     b"
    assert plain(render.bash_result(ctx(), "false", 1, "")) == "! false\n  ⎿  (нет вывода)\n     код выхода 1"
    assert plain(render.bash_result(ctx(lang="en"), "sleep", None, "x")).endswith(
        "interrupted (timeout or signal)")
    big = "\n".join(str(i) for i in range(500))
    out = plain(render.bash_result(ctx(), "seq", 0, big))
    assert out.endswith("… +300 строк") and "199" in out and "\n     200\n" not in out


# ---- bottom area ----------------------------------------------------------------------------------
def _text(frags) -> str:
    return "".join(text for _, text in frags)


def test_spinner_line():
    c = ctx()
    line = render.spinner_line(c, frame=4, verb="Совещаются", working=[ANNA, BORIS], elapsed=12.3,
                               tokens_in=4200, tokens_out=310, cost=0.12, paused=False)
    assert _text(line) == "✻ Совещаются… (12s · ↑ 4.2k ↓ 310 · $0.12 · esc — пауза)  Анна, Борис"
    assert line[0] == ("class:spinner", "✻ ") and line[1][0] == "class:spinner.verb"
    line = render.spinner_line(ctx(lang="en"), frame=0, verb="Huddling", working=[], elapsed=185, tokens_in=0,
                               tokens_out=0, cost=0, paused=False)
    assert _text(line) == "· Huddling… (3m 05s · esc to pause)"
    line = render.spinner_line(c, frame=0, verb="x", working=[ANNA], elapsed=5, tokens_in=1, tokens_out=1,
                               cost=0.004, paused=True)
    assert _text(line) == "⏸ Рой на паузе (5s · ↑ 1 ↓ 1 · $0.004 · /pause — продолжить)  Анна"
    # paused with nobody finishing a step: no meaningless "0s"
    line = render.spinner_line(c, frame=0, verb="x", working=[], elapsed=0, tokens_in=0, tokens_out=0, cost=0,
                               paused=True)
    assert _text(line) == "⏸ Рой на паузе (/pause — продолжить)"
    frames = [render.spinner_frame(c, i) for i in range(12)]
    assert frames[:10] == ["·", "✢", "✳", "✶", "✻", "✽", "✻", "✶", "✳", "✢"] and frames[10] == "·"
    assert {render.spinner_frame(ctx(unicode=False), i) for i in range(8)} <= set("-\\|/")
    many = [f"Агент{i} Фамилия" for i in range(12)]
    for width in (40, 60, 100):
        line = render.spinner_line(ctx(width=width), frame=1, verb="Совещаются", working=many, elapsed=1,
                                   tokens_in=10**6, tokens_out=10**5, cost=12.5, paused=False)
        assert cell_len(_text(line)) <= width - 1
    line = render.spinner_line(ctx(width=100), frame=1, verb="Совещаются", working=many, elapsed=1,
                               tokens_in=1, tokens_out=1, cost=1, paused=False)
    assert re.search(r"Агент0, .* \+\d+$", _text(line))
    assert render.spinner_verbs("ru")[0] == "Совещаются" and "Huddling" in render.spinner_verbs("en")
    assert render.spinner_verbs("xx") == render.spinner_verbs("en")


def test_status_line():
    c = ctx(width=120)
    left, right = render.status_line(c, session={"id": "001-calc", "title": "Калькулятор"}, addressee=ANNA,
                                     agents_total=3, working=0, spent=0.42, budget=5.0, model="claude-opus-5-5",
                                     verbosity="normal", connected=True, hint=None)
    assert _text(left) == "? — горячие клавиши"
    assert _text(right) == "Калькулятор · → Анна Смирнова · 3 агента · $0.42/$5.00 · claude-opus-5-5"
    left, right = render.status_line(ctx(lang="en", width=120), session=None, addressee=None, agents_total=0,
                                     working=0, spent=0, budget=0, model=None, verbosity="verbose", connected=True,
                                     hint=None)
    assert _text(left) == "output: verbose (shift+tab)" and _text(right) == "no session"
    left, _ = render.status_line(c, session=None, addressee="all", agents_total=1, working=1, spent=0, budget=0,
                                 model=None, verbosity="normal", connected=False, hint="ignored")
    assert _text(left) == "⚠ нет связи…" and left[0][0] == "class:status.warn"
    left, right = render.status_line(c, session={"id": "s"}, addressee="all", agents_total=5, working=2, spent=4.5,
                                     budget=5.0, model=None, verbosity="quiet", connected=True, hint="Нажмите Ctrl+C")
    assert _text(left) == "Нажмите Ctrl+C"
    assert _text(right) == "s · → все · 5 агентов (2 в работе) · $4.50/$5.00"
    assert ("class:status.warn", "$4.50/$5.00") in right
    # narrow: the model goes first, then the agents count; the session title stays
    for width in (30, 50, 70):
        left, right = render.status_line(ctx(width=width), session={"title": "Калькулятор"}, addressee=ANNA,
                                         agents_total=3, working=1, spent=0.42, budget=5.0,
                                         model="claude-opus-5-5", verbosity="normal", connected=True, hint=None)
        assert cell_len(_text(left)) + 2 + cell_len(_text(right)) <= width - 4
        assert "claude-opus-5-5" not in _text(right)
        if width >= 50:
            assert "Калькулятор" in _text(right)


def test_status_line_counts_the_running_jobs():
    # agents asleep while their jobs work: the session is not stalled, and the status line says so
    def right_of(width: int, jobs: int, lang: str = "ru") -> str:
        _, right = render.status_line(ctx(lang=lang, width=width), session={"title": "Калькулятор"}, addressee=None,
                                      agents_total=3, working=0, spent=0.42, budget=5.0, model="m1", verbosity="normal",
                                      connected=True, hint=None, jobs=jobs)
        return _text(right)

    assert right_of(120, 2) == "Калькулятор · 3 агента · процессы 2 · $0.42/$5.00 · m1"
    assert right_of(120, 1, "en") == "Калькулятор · 3 agents · jobs 1 · $0.42/$5.00 · m1"
    assert "процессы" not in right_of(120, 0)
    # narrow: the model goes first, then the jobs, then the agents count
    assert right_of(76, 2) == "Калькулятор · 3 агента · процессы 2 · $0.42/$5.00"
    assert right_of(70, 2) == "Калькулятор · 3 агента · $0.42/$5.00"


# ---- output ---------------------------------------------------------------------------------------
def test_to_ansi_and_to_plain():
    renderable = render.event(ctx(), activity("tool_call", '{"command": "pytest"}', tool="shell"))
    ansi = to_ansi(renderable, 80)
    assert ESC + "[" in ansi and "38;2;" in ansi and not ansi.endswith("\n")
    assert render._ANSI.sub("", ansi).rstrip() == '⏺ Анна Смирнова · shell(command="pytest")'
    text = to_plain(renderable, 80)
    assert ESC not in text and text == '⏺ Анна Смирнова · shell(command="pytest")'
    assert to_ansi(None, 80) == "" and to_plain(None, 80) == ""
    assert to_plain("plain [bold]str[/bold] :smile:", 80) == "plain [bold]str[/bold] :smile:"
    long = render.event(ctx(width=40), ev("message.sent", ANNA, message=message(ANNA, ["user"], "слово " * 60)))
    text = to_plain(long, 40)
    assert text.count("\n") >= 8
    assert_fits(text, 40)


def _styles(renderable, text: str) -> list[str]:
    """The SGR parameters in effect where `text` starts in the ANSI rendering."""
    ansi = to_ansi(renderable, 100)
    at = ansi.index(text)
    codes = re.findall(r"\x1b\[([0-9;]*)m", ansi[:at])
    return codes[-1].split(";") if codes and codes[-1] != "0" else []


def test_only_the_name_is_bold_and_coloured():
    call = render.event(ctx(), activity("tool_call", '{"command": "pytest"}', tool="shell"))
    assert "1" in _styles(call, ANNA)
    assert "1" not in _styles(call, "shell(") and "38" not in _styles(call, "shell(")
    hired = render.event(ctx(), ev("agent.hired", ANNA, name=ANNA, role="Аналитик"))
    assert "1" not in _styles(hired, " в команде")
    retired = render.event(ctx(), ev("agent.retired", ANNA, name=ANNA))
    assert "1" not in _styles(retired, " " + i18n.t("ru", "retired"))
    to_me = render.event(ctx(), ev("message.sent", ANNA, message=message(ANNA, ["user"], "Вопрос")))
    assert "1" not in _styles(to_me, " → вам") and "2" in _styles(to_me, " → вам")


def act(agent: str, kind: str, tool: str, text: str = "", **extra) -> dict:
    return activity(kind, text, agent=agent, tool=tool, **extra)


def test_tool_pairs_keep_each_result_under_its_call():
    """An agent's step emits all its calls, then the results; other agents interleave (agent.py)."""
    c = ctx(lang="en")
    pairs = render.ToolPairs(hold=1.0)
    feed = [
        act(ANNA, "tool_call", "shell", '{"command": "pytest"}'),
        act(ANNA, "tool_call", "write_file", '{"path": "snake.py"}'),
        act(BORIS, "tool_call", "write_file", '{"path": "snake.py"}'),
        ev("task.created", ANNA, task=task(3, "Loop")),
        act(ANNA, "tool_result", "shell", "3 passed"),
        act(BORIS, "tool_result", "write_file", "Created snake.py"),
        act(ANNA, "tool_result", "write_file", "snake.py unchanged"),
        act(BORIS, "tool_call", "wait", "{}"),
        act(BORIS, "tool_result", "wait", "OK, waiting."),
    ]
    printed: list[str] = []
    for i, e in enumerate(feed):
        lines = pairs.feed(c, e, now=i * 0.1)
        for line in ([render.event(c, e)] if lines is None else lines):
            if line is not None:
                pairs.printed(line)
                printed.append(plain(line))
    assert printed == [
        "☐ #3 Loop · Разобрать ввод",
        f"⏺ {ANNA} · shell(command=\"pytest\")\n  ⎿  3 passed",
        f"⏺ {BORIS} · write_file(path=\"snake.py\")\n  ⎿  Created snake.py",
        f"⏺ {ANNA} · write_file(path=\"snake.py\")\n  ⎿  snake.py unchanged",
    ]  # wait() is housekeeping: verbose only
    assert pairs.next_due() is None

    # a long call is printed alone after `hold`; its result follows directly while nothing came between...
    assert pairs.feed(c, act(ANNA, "tool_call", "run_command", '{"command": "pytest"}'), now=10) == []
    assert pairs.next_due() == 11 and pairs.due(c, now=10.5) == []
    alone = pairs.due(c, now=11)
    assert [plain(x) for x in alone] == [f"⏺ {ANNA} · run_command(command=\"pytest\")"]
    pairs.printed(alone[0])
    assert [plain(x) for x in pairs.feed(c, act(ANNA, "tool_result", "run_command", "3 passed"), now=12)] == \
        ["  ⎿  3 passed"]
    # ...and with a header when something else was printed in between
    pairs.feed(c, act(ANNA, "tool_call", "run_command", '{"command": "pytest"}'), now=20)
    pairs.printed(pairs.due(c, now=21)[0])
    pairs.printed(render.info(c, "something else"))
    assert [plain(x) for x in pairs.feed(c, act(ANNA, "tool_result", "run_command", "3 passed"), now=22)] == \
        [f"⏺ {ANNA} · run_command\n  ⎿  3 passed"]
    # a result whose call was never seen (attached mid-step) gets the header too
    assert [plain(x) for x in pairs.feed(c, act(BORIS, "tool_result", "board", "Goal [active]"), now=30)] == \
        [f"⏺ {BORIS} · board\n  ⎿  Goal [active]"]
    # a new step of the agent releases calls that never got a result
    pairs.feed(c, act(ANNA, "tool_call", "board", "{}"), now=40)
    out = pairs.feed(c, activity("error", "LLM failed", agent=ANNA), now=40.1)
    assert [plain(x) for x in out] == [f"⏺ {ANNA} · board()", f"⚠ {ANNA}: LLM failed"]
    assert pairs.feed(c, ev("task.created", ANNA, task=task())) is None
    # verbose shows the housekeeping calls, paired as well
    v = ctx("verbose", lang="en")
    pairs.reset()
    pairs.feed(v, act(BORIS, "tool_call", "wait", "{}"), now=50)
    assert plain(pairs.feed(v, act(BORIS, "tool_result", "wait", "OK"), now=50.1)[0]) == \
        f"⏺ {BORIS} · wait()\n  ⎿  OK"


def test_housekeeping_tools_are_verbose_only():
    for kind in ("tool_call", "tool_result"):
        for tool in ("wait", "wait_for_events"):
            e = activity(kind, "{}" if kind == "tool_call" else "OK, waiting.", tool=tool)
            assert render.event(ctx(), e) is None and render.event(ctx("verbose"), e) is not None
    failed = activity("tool_result", "Error: bad timeout", tool="wait", is_error=True)
    assert "bad timeout" in plain(render.event(ctx(), failed))


def test_bottom_area_widths_follow_prompt_toolkit():
    from prompt_toolkit.formatted_text import fragment_list_width

    from dreamteam.tui.dialogs import MCPApprovalDialog, SessionPicker

    for text in ("⚠️", "👍🏽", "👨‍👩‍👧", "Анна", "日本"):
        assert render.cells(text) == fragment_list_width([("", text)])
    assert render.cut_cells("ab日本", 3) == "ab" and render.cut_cells("ab日本", 4) == "ab日"
    dialog = MCPApprovalDialog("ru", {"name": "net", "owner": ANNA, "description": "⚠️ Нужен доступ в сеть 👍🏽"})
    picker = SessionPicker("ru", [{"id": "001-a", "title": "Игра 👨‍👩‍👧 ⚠️", "status": "running"}])
    for frags in (dialog.fragments(60), picker.fragments(60)):
        lines = [[]]
        for style, text in frags:
            for i, part in enumerate(text.split("\n")):
                if i:
                    lines.append([])
                lines[-1].append((style, part))
        assert {fragment_list_width(line) for line in lines} == {60}
    left, right = render.status_line(ctx(width=60), session={"title": "Игра 👨‍👩‍👧 ⚠️"}, addressee=ANNA,
                                     agents_total=3, working=1, spent=0.4, budget=5.0, model="m",
                                     verbosity="normal", connected=True, hint=None)
    assert fragment_list_width(left) + 2 + fragment_list_width(right) <= 56


def test_render_is_fast():
    started = time.perf_counter()
    c = ctx("verbose")
    for _ in range(5):
        for e in realistic_events():
            to_ansi(render.event(c, e), 100)
    assert time.perf_counter() - started < 10


def test_tools_echoed_by_their_own_line_show_only_in_verbose():
    """send_message / task_* / goal_complete / hire_agent already have their own line (the message, the task,
    the goal panel, the team change): in normal verbosity their calls would only repeat it. Errors still show."""
    for tool in sorted(render.ECHOED_TOOLS):
        call = activity("tool_call", '{"task_id": 3}', tool=tool)
        result = activity("tool_result", "Done.", tool=tool)
        assert render.event(ctx(), call) is None and render.event(ctx(), result) is None
        assert plain(render.event(ctx("verbose"), call)).startswith(f"⏺ {ANNA} · {tool}(")
        assert plain(render.event(ctx("verbose"), result)) == "  ⎿  Done."
    pairs = render.ToolPairs(hold=1.0)
    c = ctx(lang="en")
    assert pairs.feed(c, act(ANNA, "tool_call", "task_claim", '{"task_id": 3}'), now=0) == []
    assert pairs.feed(c, act(ANNA, "tool_call", "send_message", '{"to": ["user"]}'), now=0.1) == []
    assert pairs.feed(c, act(ANNA, "tool_result", "task_claim", "You now work on #3"), now=0.2) == []
    failed = pairs.feed(c, act(ANNA, "tool_result", "send_message", "Unknown recipient 'Bob'.", is_error=True),
                        now=0.3)
    assert [plain(line) for line in failed] == [f"⏺ {ANNA} · send_message\n  ⎿  Unknown recipient 'Bob'."]
    assert pairs.next_due() is None
