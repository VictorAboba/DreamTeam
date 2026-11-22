"""The user's pause and the background jobs it leaves running (docs/SPEC.md §3 `_set_pause`, §7.1 lifecycle, §12
`swarm_control`, §18.4 the pause button): a fresh pause by the user (swarm.USER_PAUSE) while jobs run gives USER one
quiet `u_paused_jobs` naming them and adds a sentence with their ids to the result; without jobs, while already
paused, and for a budget or question pause nothing is added; the Russian pause reason «по вашему решению» (not a job's
«остановлен вами»); the web pause button pauses quietly and toasts a resume and a refusal.

One test runs a real job (a Python child outside the workspace, checked gone by the `kids` fixture); the others stand
jobs in for `jobs.running_of`, the one source both texts read."""

from __future__ import annotations

import re
import time
from types import SimpleNamespace

import pytest

from dreamteam import prompts, uitext
from dreamteam.eventtext import TextContext, _reason
from dreamteam.models import USER
from dreamteam.swarm import QUESTION, USER_PAUSE
from dreamteam.tools import ToolContext, call_tool
from test_job_tools import ANNA, apid, fast, kids, py_cmd, team  # noqa: F401 - `fast`, `kids`: fixtures
from test_runtime import call, make_swarm, shutdown
from test_ui import APP_JS, QUESTION_HARNESS, needs_node, run_js

KEPT = "Background jobs keep running ({ids}): a pause stops only the agents' model calls; job_stop stops a job."


def fake_job(job_id: int, owner: str, command: str, minutes: float) -> SimpleNamespace:
    return SimpleNamespace(id=job_id, label=f"j{job_id}", owner=owner, command=command,
                           started_at=time.time() - minutes * 60)


async def control(swarm, args: dict) -> str:
    text, is_error = await call_tool(ToolContext(swarm, USER), "swarm_control", args)
    assert not is_error, text
    return text


def told(swarm) -> list[str]:
    return [r["text"] for r in swarm.db.all("SELECT text FROM notifications WHERE recipient = ? AND kind = "
                                            "'u_paused_jobs' ORDER BY id", (USER,))]


async def started(tmp_path, monkeypatch, *jobs: SimpleNamespace, language: str = "en"):
    """A started swarm (no agents run) whose running jobs are `jobs`, and the recipients it signals."""
    swarm, _ = make_swarm(tmp_path, lambda *a: call("wait"), swarm={"start_agents": 1, "language": language})
    await swarm.start(run_agents=False)
    monkeypatch.setattr(swarm.jobs, "running_of", lambda owner=None: [j for j in jobs if owner in (None, j.owner)])
    signalled: list[str] = []
    real = swarm.signal
    monkeypatch.setattr(swarm, "signal", lambda name: (signalled.append(name), real(name)))
    return swarm, signalled


async def test_a_user_pause_names_a_real_running_job(tmp_path, fast, kids):
    swarm, _ = await team(tmp_path)
    try:
        anna = ToolContext(swarm, ANNA)
        text, is_error = await call_tool(anna, "shell", {"command": py_cmd(kids.script, "30", kids.pidfile()),
                                                         "background": True})
        assert not is_error, text
        await apid(kids)
        assert [j.label for j in swarm.jobs.running_of()] == ["j1"]
        assert await control(swarm, {"action": "pause"}) == "Done: paused. " + KEPT.format(ids="j1")
        assert swarm.paused and swarm.pause_reason == USER_PAUSE
        (notice,) = told(swarm)
        assert re.fullmatch(r"Background jobs of the agents are not paused with the swarm: j1 \(Anna Smith: .+, "
                            r"\d+ s\)\. A pause stops only the agents' model calls; the jobs run until you stop them "
                            r"or the session stops: use the Jobs tab on the board or /jobs stop <id> in the "
                            r"terminal\.", notice), notice
        # the job goes on: a pause stops only the model calls
        assert swarm.jobs.running_of() and swarm.db.one("SELECT status FROM jobs WHERE id = 1")["status"] == "running"
        await swarm.jobs.stop(1, USER, force=True)
    finally:
        await shutdown(swarm)


async def test_the_result_and_the_notice_name_every_running_job(tmp_path, monkeypatch):
    jobs = (fake_job(3, "Anna Smith", "npm run dev", 25), fake_job(5, "Boris King", "pytest -x " + "a" * 80, 2))
    swarm, signalled = await started(tmp_path, monkeypatch, *jobs)
    try:
        # the done parts first, then the ids in a sentence of their own
        text = await control(swarm, {"action": "pause", "wrap_up": False})
        assert text == "Done: paused; wrap-up mode was not on. " + KEPT.format(ids="j3, j5")
        (notice,) = told(swarm)
        assert notice.startswith("Background jobs of the agents are not paused with the swarm: j3 (Anna Smith: npm "
                                 "run dev, 25 min); j5 (Boris King: pytest -x ")
        assert "…" in notice and ", 2 min). A pause stops only the agents' model calls" in notice  # short_command
        assert USER not in signalled  # quiet: a user MCP client in wait_for_events is not woken for it
        # a pause while paused changes nothing: no sentence, no second notice
        assert await control(swarm, {"action": "pause"}) == "Done: already paused (paused by the user)."
        assert len(told(swarm)) == 1
        # a resume says nothing of the jobs; a fresh pause later tells again
        assert await control(swarm, {"action": "resume"}) == "Done: resumed."
        assert await control(swarm, {"action": "pause"}) == "Done: paused. " + KEPT.format(ids="j3, j5")
        assert len(told(swarm)) == 2
    finally:
        await shutdown(swarm)


async def test_no_jobs_no_sentence_and_no_notice(tmp_path, monkeypatch):
    swarm, _ = await started(tmp_path, monkeypatch)
    try:
        assert await control(swarm, {"action": "pause"}) == "Done: paused."
        assert await control(swarm, {"action": "pause", "wrap_up": False}) == \
            "Done: already paused (paused by the user); wrap-up mode was not on."
        assert told(swarm) == []
    finally:
        await shutdown(swarm)


async def test_budget_and_question_pauses_do_not_send_it(tmp_path, monkeypatch):
    swarm, _ = await started(tmp_path, monkeypatch, fake_job(3, "Anna Smith", "npm run dev", 25))
    try:
        member = swarm.members()[0]["name"]
        swarm.pause(USER, "budget")
        assert swarm.paused and told(swarm) == []
        swarm.resume(USER)
        swarm.pause(member, QUESTION, asker=member, message_id=7, since=time.time())
        # a question pause turned into a budget pause (written while paused) is no fresh pause either
        swarm._set_pause(USER, "budget", outcome={"asker": member, "message_id": 7, "outcome": "dropped"})
        assert told(swarm) == []
        kinds = [r["kind"] for r in swarm.db.all("SELECT kind FROM notifications WHERE recipient = ?", (USER,))]
        assert kinds.count("u_budget") == 2 and kinds.count("u_question") == 1
        swarm.resume(USER)
        # another reason is not the user's pause (tests and old rows use "user", "manual")
        assert swarm.pause(USER, "manual") and told(swarm) == []
    finally:
        await shutdown(swarm)


async def test_the_russian_notice(tmp_path, monkeypatch):
    swarm, _ = await started(tmp_path, monkeypatch, fake_job(3, "Anna Smith", "npm run dev", 25), language="ru")
    try:
        assert swarm.pause(USER, USER_PAUSE)
        assert told(swarm) == ["Фоновые процессы агентов не встают на паузу вместе с роем: j3 (Anna Smith: npm run "
                               "dev, 25 мин). Пауза останавливает только обращения агентов к модели; процессы "
                               "работают, пока вы их не остановите или пока не остановится сессия: откройте вкладку "
                               "«Процессы» на доске или выполните /jobs stop <id> в терминале."]
    finally:
        await shutdown(swarm)


def test_the_texts_and_the_user_pause_reason():
    for lang in ("en", "ru"):
        text = prompts.TEXTS[lang]["u_paused_jobs"]
        assert "{jobs}" in text and "/jobs stop <id>" in text and chr(0x451) not in text
    # the user's own pause reads «по вашему решению»; a job stopped by the user keeps «остановлен вами»
    assert _reason(TextContext(lang="ru"), USER_PAUSE) == "по вашему решению"
    assert _reason(TextContext(lang="en"), USER_PAUSE) == "paused by you"
    assert uitext.TEXTS["ru"]["job_stopped_user"] == "остановлен вами"
    assert '"reason_paused by the user": "по вашему решению"' in APP_JS
    assert 'job_stopped_user: "остановлен вами"' in APP_JS


@needs_node
def test_the_pause_button_pauses_quietly():
    """A pause is not toasted (the banner says it, a u_paused_jobs notice comes as its own toast); a resume and a
    refusal are."""
    out = run_js(QUESTION_HARNESS + r"""
      app.S.lang = "en";
      app.S.state = { swarm: swarm({}), agents, mcp: [], triggers: [], jobs: [] };
      const press = async () => {
        calls.length = 0; toasts.length = 0;
        await app.togglePause();
        return [calls.filter((c) => c.url.includes("/act/")).map((c) => [c.url, c.body]), toasts.map((x) => x[1])];
      };
      out.pause = await press();
      app.S.state.swarm = swarm({ paused: true, pause_reason: "paused by the user" });
      out.resume = await press();
      app.S.state.swarm = swarm({});
      globalThis.fetch = async (url, opts) => {
        calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
        return { ok: true, json: async () => ({ ok: false, result: "refused" }), text: async () => "" };
      };
      out.refused = await press();
      // the notice itself is a toast like every user notice
      toasts.length = 0;
      app.handleEvent({ id: 601, ts: 0, type: "notification", actor: null,
                        data: { recipient: "user", kind: "u_paused_jobs", text: "Background jobs ... j3" } });
      out.notice = toasts.map((x) => x[1]);
    """)
    act = "/api/act/swarm_control"
    assert out["pause"] == [[[act, {"action": "pause"}]], []]
    assert out["resume"] == [[[act, {"action": "resume"}]], ["done"]]
    assert out["refused"] == [[[act, {"action": "pause"}]], ["refused"]]
    assert out["notice"] == ["Background jobs ... j3"]
    assert '$("#pause-btn").addEventListener("click", togglePause);' in APP_JS
