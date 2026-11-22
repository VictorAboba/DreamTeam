"""The one-time money notice and the money line (docs/SPEC.md §3 `record_usage`, §6.3, §17.1 `[swarm]
budget_notice_at`, §18.4): `n_budget_low` to every AI agent (waking by resume's rule, else quiet) and the quiet
`u_budget_low` once per limit and share (kv `budget_notice`), re-armed by a new limit or share; the setting's checks,
template line, settings view and home.js field; the `money` line of a full wake-up; the system prompt's sentences;
the terminal's warning kinds and print mode's stderr line."""

from __future__ import annotations

import asyncio
import io
import re
import tomllib
from types import SimpleNamespace

import pytest

from dreamteam import prompts
from dreamteam import settings as st
from dreamteam.config import Config, SwarmSettings, render_template
from dreamteam.db import loads
from dreamteam.llm.base import Usage
from dreamteam.models import USER, AgentKind, AgentStatus
from dreamteam.swarm import QUIET_KEY
from dreamteam.tui import printmode, render
from test_runtime import call, make_config, make_swarm, shutdown
from test_ui import needs_node, run_js

EXT = "Claude Code"


def spend(swarm, name: str, usd: float) -> None:
    swarm.record_usage(name, Usage(cost_usd=usd), swarm.config.models["default"])


def rows(swarm, kind: str) -> list[dict]:
    return [{"recipient": r["recipient"], "text": r["text"], **loads(r["data"], {})}
            for r in swarm.db.all("SELECT recipient, text, data FROM notifications WHERE kind = ? ORDER BY id",
                                  (kind,))]


def quiet_of(swarm) -> dict[str, bool]:
    return {r["recipient"]: bool(r.get(QUIET_KEY)) for r in rows(swarm, "n_budget_low")}


async def started(tmp_path, agents: int = 1, **swarm_settings):
    swarm, _ = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": agents, **swarm_settings})
    await swarm.start(run_agents=False)
    names = [m["name"] for m in swarm.members()]
    for name in names:
        swarm.render_wake(name)  # the hiring news drained: nothing pending
    return swarm, names


# --- the notice ---------------------------------------------------------------------------------------------------

async def test_crossing_the_share_tells_every_agent_once_and_the_user_quietly(tmp_path):
    swarm, (anna, boris, carl, dora) = await started(tmp_path, agents=4, max_cost_usd=1.0)
    await swarm.hire(USER, "Assistant", name=EXT, kind=AgentKind.EXTERNAL)
    await swarm.retire(USER, dora)  # a retired member: no notice
    signalled: list[str] = []
    signal = swarm.signal
    swarm.signal = lambda name: (signalled.append(name), signal(name))[1]
    try:
        for name in (anna, boris, carl):
            swarm.render_wake(name)  # the news of the new member and of the retired one drained
        swarm.board.create(USER, "Ship it", assignee=anna)       # open tasks: waking
        swarm.notify(carl, "n_restart")                          # news pending: waking
        for _ in range(7):
            spend(swarm, anna, 0.1)
        assert rows(swarm, "n_budget_low") == [] and rows(swarm, "u_budget_low") == []
        signalled.clear()
        spend(swarm, anna, 0.1)  # 8 x 0.1 = 0.7999999999999999: the float tolerance counts it as 0.8
        assert quiet_of(swarm) == {anna: False, boris: True, carl: False}  # no external or retired member
        assert not swarm.has_pending(boris) and swarm.has_pending(anna)
        assert USER not in signalled and boris not in signalled  # the quiet ones are not woken
        (user,) = rows(swarm, "u_budget_low")
        assert user["recipient"] == USER and user["pct"] == 80
        assert user["text"] == ("The team has spent $0.80 of the $1.00 budget (80%); the agents were told to finish "
                                "what is nearly done and start nothing large. When the budget is used up, the swarm "
                                "pauses; to let it work longer, raise the limit.")
        text = rows(swarm, "n_budget_low")[0]["text"]
        assert text.startswith("Money: spent $0.80 of the $1.00 budget (80%). When it is used up, the swarm pauses")
        assert "(`task_complete`)" in text and text.endswith("start nothing large.")
        record = swarm.db.get("budget_notice")
        assert record["limit"] == 1.0 and record["at"] == 0.8 and record["spent"] == pytest.approx(0.8)
        spend(swarm, anna, 0.1)  # 0.9: nothing more
        assert len(rows(swarm, "n_budget_low")) == 3 and len(rows(swarm, "u_budget_low")) == 1
        spend(swarm, anna, 0.15)  # the limit: u_budget's pause covers it, no low notice
        assert swarm.budget_exceeded() and len(rows(swarm, "u_budget_low")) == 1
        swarm.pause(USER, "budget")
        assert [(r["recipient"], r["text"]) for r in rows(swarm, "u_budget")] == [
            (USER, prompts.t("en", "u_budget", spent=swarm.spent_usd(), limit=1.0))]
    finally:
        await shutdown(swarm)


async def test_a_working_agent_or_one_with_a_review_is_woken(tmp_path):
    swarm, (anna, boris, carl) = await started(tmp_path, agents=3, max_cost_usd=1.0)
    try:
        task = swarm.board.create(USER, "Ship it", assignee=anna)
        swarm.db.execute("UPDATE tasks SET status = 'review', reviewer = ? WHERE id = ?", (boris, task.id))
        swarm.db.execute("UPDATE agents SET status = 'working' WHERE name = ?", (carl,))
        assert swarm.board.reviews_of(boris)
        spend(swarm, carl, 0.85)
        assert quiet_of(swarm) == {anna: False, boris: False, carl: False}
    finally:
        await shutdown(swarm)


async def test_an_agent_whose_runtime_works_is_woken(tmp_path):
    swarm, (anna, boris) = await started(tmp_path, agents=2, max_cost_usd=1.0)
    try:
        swarm.runtimes[anna] = SimpleNamespace(state=AgentStatus.WORKING, stalled=frozenset(), interrupted=False,
                                               _task=None, wake=lambda: None, reset_llm=lambda: None, stop=_no_stop)
        assert swarm.member(anna)["status"] != AgentStatus.WORKING  # only the runtime says it works
        spend(swarm, boris, 0.8)
        assert quiet_of(swarm) == {anna: False, boris: True}
    finally:
        await shutdown(swarm)


async def _no_stop() -> None:
    pass


async def test_a_new_limit_or_share_re_arms_the_notice(tmp_path):
    swarm, (anna,) = await started(tmp_path, max_cost_usd=1.0)
    try:
        spend(swarm, anna, 0.8)
        assert len(rows(swarm, "u_budget_low")) == 1
        swarm.set_limits(USER, max_cost_usd=2.0)
        spend(swarm, anna, 0.1)  # 0.9 of 2.0: below the new threshold
        assert len(rows(swarm, "u_budget_low")) == 1
        spend(swarm, anna, 0.7)  # 1.6 of 2.0
        assert [r["limit"] for r in rows(swarm, "u_budget_low")] == [1.0, 2.0]
        assert "$1.60 of the $2.00 budget (80%)" in rows(swarm, "n_budget_low")[-1]["text"]
        swarm.set_limits(USER, max_cost_usd=2.0)  # the same limit again: no new notice
        spend(swarm, anna, 0.01)
        assert len(rows(swarm, "u_budget_low")) == 2
        # a new share through reconfigure (the stored limit 2.0 still wins over the config's)
        swarm.reconfigure(make_config(tmp_path, swarm={"max_cost_usd": 1.0, "budget_notice_at": 0.5}))
        assert len(rows(swarm, "u_budget_low")) == 2  # no hook: the next record_usage
        spend(swarm, anna, 0.01)
        assert len(rows(swarm, "u_budget_low")) == 3 and rows(swarm, "u_budget_low")[-1]["limit"] == 2.0
        assert swarm.db.get("budget_notice")["at"] == 0.5
    finally:
        await shutdown(swarm)


async def test_reconfigure_with_a_new_config_limit_re_arms_it(tmp_path):
    swarm, (anna,) = await started(tmp_path, max_cost_usd=1.0)
    try:
        spend(swarm, anna, 0.9)
        swarm.reconfigure(make_config(tmp_path, swarm={"max_cost_usd": 2.0}))
        spend(swarm, anna, 0.7)  # 1.6 of 2.0
        assert [r["limit"] for r in rows(swarm, "u_budget_low")] == [1.0, 2.0]
    finally:
        await shutdown(swarm)


@pytest.mark.parametrize("settings", [{"max_cost_usd": 1.0, "budget_notice_at": 0},
                                      {"max_cost_usd": 1.0, "budget_notice_at": 1},
                                      {"max_cost_usd": 0, "budget_notice_at": 0.8}])
async def test_no_notice_when_off_or_without_a_limit(tmp_path, settings):
    swarm, (anna,) = await started(tmp_path, **settings)
    try:
        for _ in range(12):
            spend(swarm, anna, 0.09)
        assert rows(swarm, "n_budget_low") == [] and rows(swarm, "u_budget_low") == []
        assert swarm.db.get("budget_notice") is None
    finally:
        await shutdown(swarm)


async def test_share_1_gives_no_notice_just_below_the_limit(tmp_path):
    # 10 x 0.1 = 0.9999999999999999: within the share's float tolerance, yet not the limit (budget_exceeded)
    swarm, (anna,) = await started(tmp_path, max_cost_usd=1.0, budget_notice_at=1)
    try:
        for _ in range(10):
            spend(swarm, anna, 0.1)
        assert swarm.spent_usd() >= 1.0 - 1e-9 and not swarm.budget_exceeded()
        assert rows(swarm, "n_budget_low") == [] and rows(swarm, "u_budget_low") == []
    finally:
        await shutdown(swarm)


async def test_a_call_past_the_limit_gives_no_low_notice_and_pct_stays_below_100(tmp_path):
    swarm, (anna,) = await started(tmp_path, max_cost_usd=1.0)
    try:
        spend(swarm, anna, 1.5)
        assert rows(swarm, "u_budget_low") == [] and swarm.budget_exceeded()
        # raised: the next call past the new threshold tells it; 99.6 % shows as 99
        swarm.set_limits(USER, max_cost_usd=1.51)
        spend(swarm, anna, 0.004)
        (user,) = rows(swarm, "u_budget_low")
        assert user["pct"] == 99 and "(99%)" in user["text"]
        assert "Money: spent $1.50 of $1.51 (99%)." in swarm.render_wake(anna)
    finally:
        await shutdown(swarm)


async def test_russian_agents_get_the_russian_text(tmp_path):
    swarm, (anna,) = await started(tmp_path, max_cost_usd=1.0, language="ru")
    try:
        spend(swarm, anna, 0.8)
        assert rows(swarm, "n_budget_low")[0]["text"].startswith(
            "Деньги: потрачено $0.80 из $1.00 денежного бюджета (80%). Когда он закончится, рой встанет на паузу")
        assert rows(swarm, "u_budget_low")[0]["text"].startswith("Команда потратила $0.80 из $1.00 бюджета (80%);")
    finally:
        await shutdown(swarm)


# --- the setting --------------------------------------------------------------------------------------------------

def test_the_budget_notice_setting(tmp_path):
    assert SwarmSettings().budget_notice_at == 0.8
    for value in (0, 1, 0.5):
        assert Config.from_dict(tmp_path, {"swarm": {"budget_notice_at": value}}).swarm.budget_notice_at == value
    for value in (1.5, -0.1):
        with pytest.raises(ValueError, match=r"\[swarm\] budget_notice_at must be between 0 and 1, got"):
            Config.from_dict(tmp_path / "bad", {"swarm": {"budget_notice_at": value}})
    with pytest.raises(ValueError, match=r"\[swarm\] budget_notice_at must be a number"):
        Config.from_dict(tmp_path / "bad", {"swarm": {"budget_notice_at": "80%"}})
    template = render_template("X", "ru", "anthropic")
    lines = template.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("budget_notice_at = "))
    assert lines[at - 1].startswith("max_cost_usd = ")
    assert lines[at].startswith("budget_notice_at = 0.8        # при этой доле бюджета агенты и вы получаете")
    assert lines[at + 1].startswith('goal = ""          # ')  # the next line as it was
    assert "ё" not in lines[at]
    assert tomllib.loads(template)["swarm"]["budget_notice_at"] == 0.8
    assert st.public_view({})["swarm"]["budget_notice_at"] == 0.8
    assert st.public_view({"swarm": {"budget_notice_at": 0.5}})["swarm"]["budget_notice_at"] == 0.5


@needs_node
def test_the_settings_page_has_the_field_after_the_budget():
    out = run_js(r"""
      const home = loadHomeJs();
      el("settings-form"); el("settings-nav"); el("profiles"); el("save-bar"); el("save-msg"); el("add-profile");
      const v = { swarm: { name: "S", language: "en", max_cost_usd: 5, budget_notice_at: 0.8 }, agents: {},
                  shell: {}, workspace: {}, server: {}, tracing: {}, mcp: { external: {} }, models: {} };
      home.H.view = JSON.parse(JSON.stringify(v)); home.H.original = JSON.parse(JSON.stringify(v));
      home.H.lang = "en";
      home.renderSettings(true);
      out.en = elements["settings-form"].innerHTML;
      home.H.lang = "ru";
      home.renderSettings(true);
      out.ru = elements["settings-form"].innerHTML;
    """)
    en, ru = out["en"], out["ru"]
    field = ('<input data-path="swarm.budget_notice_at" data-type="float" type="number" step="0.05" min="0" max="1" '
             'value="0.8"')
    assert field in en
    assert en.index('data-path="swarm.max_cost_usd"') < en.index('data-path="swarm.budget_notice_at"')
    assert en.index('data-path="swarm.budget_notice_at"') < en.index('data-path="swarm.independent_review"')
    assert ("<span>Budget warning at share <span class=\"hint\">At this share of the budget spent, the agents and you "
            "get one notice (0.8 = 80 %); 0 or 1: none.</span>") in en
    assert ("<span>Предупредить о бюджете при доле <span class=\"hint\">При этой доле потраченного бюджета агенты и вы "
            "получаете одно предупреждение (0.8 — 80 %); 0 или 1 — без него.</span>") in ru


# --- the money line and the system prompt -------------------------------------------------------------------------

async def test_the_money_line_follows_the_goal_line(tmp_path):
    swarm, (anna, ru) = await started(tmp_path, agents=2, max_cost_usd=5.0)
    await swarm.hire(USER, "Assistant", name=EXT, kind=AgentKind.EXTERNAL)
    try:
        swarm.db.execute("UPDATE agents SET language = 'ru' WHERE name = ?", (ru,))
        spend(swarm, anna, 1.2)
        lines = swarm.render_wake(anna).split("\n")
        at = next(i for i, line in enumerate(lines) if line.startswith("Global goal: "))
        assert lines[at + 1] == "Money: spent $1.20 of $5.00 (24%)."
        lines = swarm.render_wake(ru).split("\n")
        at = next(i for i, line in enumerate(lines) if line.startswith("Глобальная цель: "))
        assert lines[at + 1] == "Деньги: потрачено $1.20 из $5.00 (24%)."
        # with a goal and wrap-up mode: still right after the goal line, never before the news
        swarm.set_goal(USER, "Ship it")
        swarm.wrap_up(USER)
        lines = swarm.render_wake(anna).split("\n")
        assert lines[1] == prompts.t("en", "wrap_up_line") and lines[2] == "What happened:"
        at = lines.index("Global goal: Ship it [active]")
        assert lines[at + 1] == "Money: spent $1.20 of $5.00 (24%)."
        # never in the news of a running activation, never for an external member or the user
        swarm.notify(anna, "n_restart")
        assert "Money:" not in swarm.render_wake(anna, incoming=True)
        assert "Money:" not in swarm.render_wake(EXT)
        swarm.notify(USER, "u_budget_low", spent=1.2, limit=5.0, pct=24)
        assert "Money:" not in (swarm.render_wake(USER) or "")
    finally:
        await shutdown(swarm)


async def test_no_money_line_without_a_limit(tmp_path):
    swarm, (anna,) = await started(tmp_path, max_cost_usd=0)
    try:
        spend(swarm, anna, 1.2)
        assert "Money:" not in swarm.render_wake(anna)
    finally:
        await shutdown(swarm)


def test_the_system_prompt_names_the_money_and_untracked_processes():
    en, ru = prompts.SYSTEM_EN, prompts.SYSTEM_RU
    assert "shows the global goal, the money spent (while the budget has a limit), your open tasks" in en
    assert "показывает глобальную цель, потраченные деньги (пока у бюджета есть лимит), твои открытые задачи" in ru
    nine = next(line for line in en.splitlines() if line.startswith("9. "))
    assert nine.endswith("When a notice says that most of the money budget is spent, don't wait for the user to ask "
                         "for a wrap-up: finish what is nearly done and start nothing large.")
    nine = next(line for line in ru.splitlines() if line.startswith("9. "))
    assert nine.endswith("Если уведомление говорит, что большая часть денежного бюджета потрачена, не жди, пока "
                         "пользователь попросит закругляться: доводи до конца то, что почти готово, и не начинай "
                         "ничего большого.")
    ten = next(line for line in en.splitlines() if line.startswith("10. "))
    assert ten.endswith("A process you start with & or nohup that `jobs` does not list is not tracked: its end wakes "
                        "nobody.")
    ten = next(line for line in ru.splitlines() if line.startswith("10. "))
    assert ten.endswith("Процесс, запущенный через & или nohup, которого нет в списке `jobs`, не отслеживается: его "
                        "завершение никого не разбудит.")


# --- surfaces -----------------------------------------------------------------------------------------------------

def test_the_terminal_shows_the_notice_as_a_warning():
    assert {"u_budget_low", "u_team_asleep", "u_team_waits"} <= render._WARN_NOTES
    ctx = render.RenderContext(lang="en", verbosity="normal", width=200, unicode=True)
    warn = re.search(r"38;2;\d+;\d+;\d+", render.to_ansi(render.warning(ctx, "x"), 80)).group(0)
    note = {"id": 1, "ts": 0.0, "type": "notification", "actor": None,
            "data": {"recipient": USER, "kind": "u_budget_low", "text": "The team has spent $0.80"}}
    assert render.to_plain(render.event(ctx, note), 200) == "🔔 The team has spent $0.80"
    assert warn in render.to_ansi(render.event(ctx, note), 200)
    quiet = render.RenderContext(lang="en", verbosity="quiet", width=200, unicode=True)
    assert render.event(quiet, note) is not None


def _print_run(output_format: str = "text", verbose: bool = False):
    out, err = io.StringIO(), io.StringIO()
    conn = SimpleNamespace(client=SimpleNamespace(auto_reply=False))
    run = printmode._PrintRun(conn, "x", session=None, continue_last=False, title=None, overrides=None,
                              output_format=output_format, verbose=verbose, timeout=None, auto_reply=False,
                              lang="en", out=out, err=err)
    return run, out, err


def test_print_mode_writes_the_notice_to_stderr_once():
    assert {"u_cost_unknown", "u_budget_low"} <= printmode.STDERR_NOTES
    text = prompts.t("en", "u_budget_low", spent=0.8, limit=1.0, pct=80)

    def notice(kind: str, recipient: str = USER, body: str = text) -> dict:
        return {"type": "notification", "data": {"recipient": recipient, "kind": kind, "text": body}}

    run, out, err = _print_run()
    asyncio.run(run.on_event(notice("u_budget_low")))
    asyncio.run(run.on_event(notice("u_budget_low")))          # the same text: once
    asyncio.run(run.on_event(notice("n_budget_low", "Anna")))   # a member's notice: not here
    asyncio.run(run.on_event(notice("u_budget_low", "Anna", "other text")))  # a stderr kind, but not to the user
    asyncio.run(run.on_event(notice("u_budget", body="Budget exhausted")))  # not a stderr kind
    assert err.getvalue() == text + "\n" and out.getvalue() == ""
    run, out, err = _print_run("json")
    asyncio.run(run.on_event(notice("u_budget_low")))
    asyncio.run(run.on_event(notice("u_budget_low")))
    assert err.getvalue() == text + "\n" and out.getvalue() == ""
    run, out, err = _print_run(verbose=True)  # the feed shows it as a notice line instead
    asyncio.run(run.on_event(notice("u_budget_low")))
    assert run.notice_told == set() and err.getvalue().count(text) <= 1
    run, out, err = _print_run("stream-json")
    asyncio.run(run.on_event(notice("u_budget_low")))
    assert err.getvalue() == "" and '"u_budget_low"' in out.getvalue()
