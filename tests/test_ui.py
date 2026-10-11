"""Tests of the online board UI (src/dreamteam/static).

* static checks (always run): RU/EN i18n completeness against the keys the UI uses and the enums the
  backend emits, element ids referenced by app.js exist;
* behaviour of app.js helpers, executed in Node.js with a tiny DOM stub (skipped without `node`);
* the API contract: every field the UI reads exists in the responses of a real hub (web.py + Swarm).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

import dreamteam
from dreamteam import jobs as jobs_module
from dreamteam import mcp_forge
from dreamteam import swarm as swarm_module
from dreamteam.config import Config, ShellSettings
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, AgentStatus, Status
from dreamteam.swarm import Swarm

STATIC = Path(dreamteam.__file__).parent / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
SRC = Path(dreamteam.__file__).parent


# ------------------------------------------------------------------------------------------------ static checks
def i18n_blocks() -> dict[str, set[str]]:
    block = re.search(r"const I18N = \{(.*?)\n\};", APP_JS, re.S)
    assert block, "I18N dictionary not found in app.js"
    out: dict[str, set[str]] = {}
    for lang, body in re.findall(r"\n  (ru|en): \{(.*?)\n  \},", block.group(1), re.S):
        found = re.findall(r"(?:^|[\s,{])(?:([a-z][a-z0-9_]*)|\"([^\"]+)\"): \"", body)
        out[lang] = {a or b for a, b in found}
    return out


# the kinds of session folder (summary / state "workspace": default, adopted, else moved) and their tooltips
WORKSPACE_KINDS_KEYS = {f"ws_{k}{h}" for k in ("default", "adopted", "moved") for h in ("", "_hint")}


def test_i18n_languages_have_the_same_keys():
    blocks = i18n_blocks()
    assert set(blocks) == {"ru", "en"}
    assert blocks["ru"] == blocks["en"], {"only ru": blocks["ru"] - blocks["en"], "only en": blocks["en"] - blocks["ru"]}
    assert len(blocks["ru"]) > 150


def test_every_literal_key_used_by_the_ui_is_translated():
    keys = i18n_blocks()["en"]
    used = set(re.findall(r"\bt\(\"([a-z0-9_]+)\"\s*[,)]", APP_JS))
    used |= set(re.findall(r'data-i18n(?:-placeholder|-title)?="([a-z0-9_]+)"', INDEX))
    used |= set(re.findall(r"t\(rail \? \"(\w+)\" : \"(\w+)\"\)", APP_JS)[0])
    used |= WORKSPACE_KINDS_KEYS  # workspaceHtml(): t("ws_" + kind), t("ws_" + kind + "_hint")
    missing = sorted(k for k in used if k not in keys)
    assert not missing, missing
    assert {"goal_field", "discussion", "reply", "write_post", "disconnected"} <= used
    assert "review_drop" in keys  # used through dropAction()


def test_i18n_covers_every_value_the_backend_emits():
    keys = i18n_blocks()["en"]
    statuses = set(Status.ALL) | {"closed"}
    agent_statuses = {v for k, v in vars(AgentStatus).items() if k.isupper()}
    activity_kinds = set(re.findall(r'_activity\("(\w+)"', (SRC / "agent.py").read_text(encoding="utf-8")))
    assert {"wake", "thinking", "text", "tool_call", "tool_result"} <= activity_kinds
    mcp_statuses = {mcp_forge.ACTIVE, mcp_forge.PENDING, mcp_forge.FAILED, mcp_forge.REJECTED, mcp_forge.AVAILABLE,
                    mcp_forge.REMOVED}
    origins = {mcp_forge.ORIGIN_WORKSPACE, mcp_forge.ORIGIN_CONFIG, mcp_forge.ORIGIN_LIBRARY}
    workspace_src = (SRC / "workspace.py").read_text(encoding="utf-8")
    ops = set(re.findall(r'"(\w+)"', " ".join(re.findall(r"_store_and_commit\(actor.*", workspace_src))))
    assert {"create", "write", "edit"} <= ops
    task_fields = {"title", "goal", "description", "status", "assignee", "reviewer", "priority", "tags",
                   "depends_on", "result", "parent_id"}
    pause_reasons = {r for f in SRC.rglob("*.py")
                     for r in re.findall(r"\.pause\(\w+, \"([^\"]+)\"\)", f.read_text(encoding="utf-8"))}
    assert "budget" in pause_reasons
    # background jobs (the Jobs tab): how one went to the background, why one was stopped
    job_origins = set(jobs_module.ORIGIN_PHRASES)
    stop_reasons = set(re.findall(r'"(\w+)": f?"stopped', (SRC / "jobs.py").read_text(encoding="utf-8")))
    assert job_origins == {"timeout", "background", "detached"}
    assert stop_reasons == {"owner", "user", "lifetime", "session", "retired"}
    expected = ({f"reason_{r}" for r in pause_reasons} | {f"s_{s}" for s in statuses} | {f"a_{s}" for s in agent_statuses} | {f"k_{k}" for k in activity_kinds}
                | {f"mcp_{s}" for s in mcp_statuses} | {f"origin_{o}" for o in origins}
                | {f"scope_{s}" for s in mcp_forge.SCOPES} | {f"op_{o}" for o in ops} | {f"f_{f}" for f in task_fields}
                | {f"p{p}" for p in range(4)} | {f"job_origin_{o}" for o in job_origins}
                | {f"job_stopped_{r}" for r in stop_reasons})
    missing = sorted(expected - keys)
    assert not missing, missing


def test_no_hardcoded_user_visible_english_in_index():
    # every visible label/tooltip of the static page goes through i18n
    assert not re.search(r'(?<![-\w])(title|placeholder|aria-label)="[A-Za-zА-Яа-я]', INDEX)
    texts = re.findall(r">([^<>{}]+)<", INDEX)
    words = [x.strip() for x in texts if re.search(r"[A-Za-zА-Яа-я]{3,}", x)]
    assert words == ["DreamTeam", "DreamTeam"], words  # <title> and the brand name


def test_ids_used_by_app_js_exist():
    static_ids = set(re.findall(r'\bid="([\w-]+)"', INDEX))
    template_ids = set(re.findall(r'\bid="([\w-]+)"', APP_JS))
    used = set(re.findall(r'\$\("#([\w-]+)[" .]', APP_JS))
    used |= {m for m in re.findall(r'"#([\w-]+)"', APP_JS)}
    missing = sorted(u for u in used - static_ids - template_ids if not u.endswith("-"))
    assert not missing, missing
    # the task modal live-update needs these sections
    assert {"task-head", "task-meta", "task-result", "task-review", "thread", "task-history", "post-text",
            "mention-menu", "f-goal"} <= template_ids


def test_jobs_tab_feed_and_settings_fields():
    assert INDEX.index('data-tab="triggers"') < INDEX.index('data-tab="jobs"') < INDEX.index('data-tab="traces"')
    assert '<span data-i18n="tab_jobs"></span><span class="tab-badge hidden" id="jobs-badge"></span>' in INDEX
    pane = '<section class="tab-pane hidden" id="pane-jobs"><div id="job-list" class="cards-list"></div></section>'
    assert pane in INDEX
    assert 'id="drawer-jobs"' in INDEX
    panes = json.loads(re.search(r'for \(const name of (\[.*?\])\) \$\("#pane-"', APP_JS).group(1))
    assert panes == ["board", "feed", "files", "mcp", "triggers", "jobs", "traces"]
    for name in panes:
        assert f'id="pane-{name}"' in INDEX, name
    # one line: the API contract test below reads it
    assert "job.*" in json.loads(re.search(r"const FEED_TYPES = (\[.*?\]);", APP_JS).group(1))
    # the settings page has a field for every [shell] setting, the background-job limits included
    shell = dict(re.findall(r'\bfield\("(sh_\w+)", "shell\.(\w+)"', HOME_JS))
    assert set(shell.values()) == {f.name for f in dataclasses.fields(ShellSettings)}
    assert {"sh_bg", "sh_jobs", "sh_jobs_total", "sh_job_minutes", "sh_job_log", "sh_fg_max", "sh_cpu"} <= set(shell)
    assert re.search(r'field\("sh_cpu", "shell\.cpu_threads", "auto"', HOME_JS)
    assert re.search(r'field\("sh_bg", "shell\.background_on_timeout", "bool", \{ full: true, hint: "sh_bg_hint" \}\)',
                     HOME_JS)


# ------------------------------------------------------------------------------------------------ launcher / traces
HOME_JS = (STATIC / "home.js").read_text(encoding="utf-8")
HOME_HTML = (STATIC / "home.html").read_text(encoding="utf-8")
TRACES_JS = (STATIC / "traces.js").read_text(encoding="utf-8")


def _keys(body: str) -> set[str]:
    found = re.findall(r"(?:^|[\s,{])(?:([a-z][a-z0-9_]*)|\"([^\"]+)\"): \"", body)
    return {a or b for a, b in found}


def home_i18n() -> dict[str, set[str]]:
    block = re.search(r"const I18N = \{(.*?)\n\};", HOME_JS, re.S)
    assert block, "I18N dictionary not found in home.js"
    return {lang: _keys(body) for lang, body in re.findall(r"\n  (ru|en): \{(.*?)\n  \},", block.group(1), re.S)}


def traces_i18n() -> dict[str, set[str]]:
    blocks = dict(re.findall(r"Object\.assign\(I18N\.(ru|en), \{(.*?)\n\}\);", TRACES_JS, re.S))
    assert set(blocks) == {"ru", "en"}
    return {lang: _keys(body) for lang, body in blocks.items()}


def used_keys(src: str) -> set[str]:
    """i18n keys a script passes to t(): literals, both branches of `t(cond ? "a" : "b")`."""
    used = set(re.findall(r"\bt\(\"([a-z0-9_]+)\"\s*[,)]", src))
    for a, b in re.findall(r"\bt\([^()]*?\? \"(\w+)\" : \"(\w+)\"", src):
        used |= {a, b}
    return used


def test_home_i18n_is_complete():
    blocks = home_i18n()
    assert set(blocks) == {"ru", "en"}
    assert blocks["ru"] == blocks["en"], {"only ru": blocks["ru"] - blocks["en"], "only en": blocks["en"] - blocks["ru"]}
    keys = blocks["en"]
    used = used_keys(HOME_JS)
    used |= set(re.findall(r'data-i18n(?:-placeholder|-title)?="([a-z0-9_]+)"', HOME_HTML))
    used |= set(re.findall(r'\bfield\("([a-z0-9_]+)"', HOME_JS))                     # settings field labels
    used |= set(re.findall(r'\bhint: "([a-z0-9_]+)"', HOME_JS))
    sections = set(re.findall(r'\["\w+", "(s_\w+)"\]', HOME_JS))
    assert len(sections) == 9 and "s_roles" in sections
    used |= sections | {s + "_hint" for s in sections}
    used |= {f"st_{s}" for s in ("running", "paused", "stopped", "done", "pending")}  # sessionState() + pending goal
    used |= WORKSPACE_KINDS_KEYS  # workspaceLine(): t("ws_" + kind), t("ws_" + kind + "_hint")
    assert {"new_session", "delete_text", "p_key_saved", "p_key_env_drop", "p_model_required", "discard_new",
            "started", "stopped", "p_model_ok", "create_open", "f_team", "team_over_max", "r_err_model", "sw_review",
            "sw_review_hint", "f_goal_hint", "f_model_hint"} <= used
    # the starting team is a composer now: the old number field and the comma list are gone
    assert not {"f_start_agents", "f_roles", "f_roles_ph", "f_roles_hint", "sw_start", "sw_roles"} & keys
    missing = sorted(k for k in used if k not in keys)
    assert not missing, missing


def test_traces_i18n_is_complete():
    blocks = traces_i18n()
    assert blocks["ru"] == blocks["en"], {"only ru": blocks["ru"] - blocks["en"], "only en": blocks["en"] - blocks["ru"]}
    keys = blocks["en"] | i18n_blocks()["en"]  # traces.js extends app.js's dictionary
    used = used_keys(TRACES_JS)
    missing = sorted(k for k in used if k not in keys)
    assert not missing, missing
    # every status / span kind / span name the backend records has a label
    from dreamteam.tracing import SPAN_KINDS, TRACE_STATUSES
    names = set(re.findall(r'record\("(?:input|event)", "(\w+)"', (SRC / "agent.py").read_text(encoding="utf-8")))
    assert {"wake", "incoming", "nudge", "continue", "reply_to_user", "refusal", "budget_pause", "step_limit",
            "stopped", "crash"} <= names
    expected = ({f"trs_{s}" for s in TRACE_STATUSES} | {f"trk_{k}" for k in SPAN_KINDS}
                | {f"trn_{n}" for n in names | {"step", "compaction"}})
    assert not sorted(expected - blocks["en"]), sorted(expected - blocks["en"])
    ui_statuses = json.loads(re.search(r"const TRACE_STATUSES_UI = (\[.*?\]);", TRACES_JS).group(1))
    failed = json.loads(re.search(r"const FAILED_STATUSES = (\[.*?\]);", TRACES_JS).group(1))
    assert ui_statuses == list(TRACE_STATUSES) and set(failed) < set(TRACE_STATUSES)


def test_no_hardcoded_user_visible_text_in_home_html():
    assert not re.search(r'(?<![-\w])(title|placeholder|aria-label)="[A-Za-zА-Яа-я]', HOME_HTML)
    texts = [x.strip() for x in re.findall(r">([^<>{}]+)<", HOME_HTML) if re.search(r"[A-Za-zА-Яа-я]{3,}", x)]
    assert texts == ["DreamTeam — сессии", "DreamTeam"], texts  # <title> (replaced by applyI18n) and the brand


def test_ids_used_by_home_and_traces_exist():
    home_ids = set(re.findall(r'\bid="([\w-]+)"', HOME_HTML)) | set(re.findall(r'\bid="([\w-]+)"', HOME_JS))
    used = {u for u in re.findall(r'\$\("#([\w-]+)[" .]', HOME_JS) if not u.endswith("-")}
    assert not sorted(used - home_ids), sorted(used - home_ids)
    board_ids = set(re.findall(r'\bid="([\w-]+)"', INDEX)) | set(re.findall(r'\bid="([\w-]+)"', TRACES_JS + APP_JS))
    used = set(re.findall(r'\$\("#([\w-]+)[" .]', TRACES_JS))
    assert not sorted(used - board_ids), sorted(used - board_ids)
    assert {"stopped-banner", "start-session-btn", "back-link", "pane-traces", "swarm-name"} <= board_ids
    assert '<script src="/static/traces.js">' in INDEX and INDEX.index("/static/app.js") < INDEX.index("/static/traces.js")


# ------------------------------------------------------------------------------------------------ app.js in Node
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")

HARNESS = r"""
const appPath = process.env.DREAMTEAM_APP_JS;
const staticDir = require("path").dirname(appPath);
const calls = [];
const answers = { prompt: null, confirm: false };
const any = new Proxy(function () {}, {
  get: (t, k) => (k === "then" ? undefined : k === "length" ? 0 : k === Symbol.toPrimitive ? () => "" : any),
  apply: () => any, set: () => true,
});
function classList(initial) {
  const set = new Set(initial || []);
  return { set, add: (...c) => c.forEach((x) => set.add(x)), remove: (...c) => c.forEach((x) => set.delete(x)),
           contains: (c) => set.has(c), toggle: (c, on) => { const v = on === undefined ? !set.has(c) : !!on; if (v) set.add(c); else set.delete(c); return v; } };
}
const elements = {};
function el(id, props) {
  elements[id] = Object.assign({ id, value: "", innerHTML: "", textContent: "", style: {}, dataset: {}, classList: classList(),
                                 addEventListener: () => {}, querySelector: () => null, querySelectorAll: () => [] }, props || {});
  return elements[id];
}
globalThis.document = {
  hidden: false,
  querySelector: (sel) => (sel.startsWith("#") && elements[sel.slice(1)]) || any,
  querySelectorAll: () => [],
  createElement: () => any,
  addEventListener: () => {},
  activeElement: null,
  body: any, documentElement: any,
};
globalThis.setTimeout = () => 0;
globalThis.clearTimeout = () => {};
globalThis.prompt = () => answers.prompt;
globalThis.confirm = () => answers.confirm;
globalThis.fetch = async (url, opts) => {
  calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
  return { ok: true, json: async () => ({ ok: true, result: "done", events: [], messages: [] }), text: async () => "" };
};
/*PRE*/
const app = require(appPath);
// traces.js is a classic script on top of app.js in the browser: its free names resolve to app.js globals
const loadTracesJs = () => { Object.assign(globalThis, app); return require(staticDir + "/traces.js"); };
const loadHomeJs = () => require(staticDir + "/home.js");
const out = {};
(async () => {
/*BODY*/
})().then(() => console.log(JSON.stringify(out)), (e) => { console.error(e && e.stack || e); process.exit(1); });
"""


def run_js(body: str, pre: str = "", env: dict[str, str] | None = None) -> dict:
    script = HARNESS.replace("/*PRE*/", pre).replace("/*BODY*/", body)
    full_env = dict(os.environ, DREAMTEAM_APP_JS=str(STATIC / "app.js"), **(env or {}))
    proc = subprocess.run([NODE, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30,
                          env=full_env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@needs_node
def test_linkify_escapes_everything_but_task_links_and_mentions():
    out = run_js(r"""
      app.S.state = { agents: [{ name: "Анна Смирнова", status: "idle" }, { name: '<b x="1">Evil</b> (Bob)', status: "idle" }] };
      out.html = app.linkify(`<img src=x onerror=alert(1)> #5 (#6) &#39; '"q" @анна смирнова @user @<b x="1">Evil</b> (Bob) a@Анна Смирнова`);
      out.attr = app.linkify('#1"><script>alert(1)</script>');
      out.card = app.cardHtml({ id: "7 onmouseover=x", title: "<i>t</i>", goal: "<g>", tags: ["<x>"], priority: '1" onclick="x',
                                assignee: "<a>", status: "todo", blocked_by: [], comments: 0 });
      out.act = app.activityHtml({ id: 1, ts: 0, data: { kind: 'text" onclick="x', tool: "<t>", text: "<pre>" } });
      out.msg = app.messageHtml({ sender: "<s>", recipients: ["user"], text: "<script>1</script> #3", ts: 0 });
    """)
    html = out["html"]
    assert "<img" not in html and "&lt;img src=x onerror=alert(1)&gt;" in html
    assert '<button type="button" class="link" data-open-task="5">#5</button>' in html
    assert 'data-open-task="6"' in html and "&#39;" in html and 'data-open-task="39"' not in html
    assert html.count('<span class="mention">') == 3  # Анна (case-insensitive), user, the HTML-named member
    assert '<span class="mention">@&lt;b x=&quot;1&quot;&gt;Evil&lt;/b&gt; (Bob)</span>' in html
    assert "a@Анна" in html and 'a<span class="mention">' not in html  # not a mention inside a word
    assert "<script>" not in out["attr"] and 'data-open-task="1"' in out["attr"]
    card = out["card"]
    assert "<i>" not in card and "<g>" not in card and "<x>" not in card and "<a>" not in card
    assert "onmouseover" not in card and "onclick" not in card and 'class="card p0"' in card
    assert "onclick" not in out["act"].split("&quot;")[0] and "<pre><pre>" not in out["act"] and "&lt;t&gt;" in out["act"]
    assert "<script>" not in out["msg"] and "&lt;s&gt;" in out["msg"] and 'data-open-task="3"' in out["msg"]


@needs_node
def test_scheduler_is_not_starved_by_a_stream_of_events():
    # regression: scheduleRefresh restarted its timer on every event, so a busy swarm (an event every
    # <250 ms) never refreshed the board at all
    out = run_js(r"""
      let now = 0; const timers = [];
      const clock = { now: () => now, clearTimeout: (h) => { if (h) h.live = false; },
                      setTimeout: (f, ms) => { const h = { f, at: now + ms, live: true }; timers.push(h); return h; } };
      const advance = (ms) => {
        const end = now + ms;
        for (;;) {
          const due = timers.filter((h) => h.live && h.at <= end).sort((a, b) => a.at - b.at)[0];
          if (!due) break;
          now = due.at; due.live = false; due.f();
        }
        now = end;
      };
      let runs = 0;
      const schedule = app.makeScheduler(() => runs++, clock);
      for (let i = 0; i < 20; i++) { schedule(250); advance(100); }
      out.burst = runs;
      runs = 0; schedule(1000); schedule(10); advance(20); out.earlier = runs;
      runs = 0; schedule(10); schedule(1000); advance(20); out.later = runs; advance(2000); out.after = runs;
    """)
    assert out["burst"] >= 6
    assert out["earlier"] == 1  # a sooner request moves the pending run earlier
    assert out["later"] == 1 and out["after"] == 1  # a later request does not postpone (nor duplicate) it


@needs_node
def test_task_form_helpers():
    out = run_js(r"""
      app.S.state = { agents: [{ name: "Ann Lee", role: "Dev", status: "idle" }, { name: "Old Timer", role: "QA", status: "retired" }] };
      // a done task assigned to a retired member must keep its assignee in the select (saving must not unassign)
      out.opts = app.memberOptions("Old Timer");
      out.noneSelected = app.memberOptions("");
      out.status = app.statusOptions("in_progress");
      out.statusReview = app.statusOptions("review");
      const task = { id: 4, title: "T", goal: "G", description: "a\r\nb", status: "todo", priority: 1, assignee: "Old Timer",
                     reviewer: null, tags: ["x", "y"], depends_on: [1, 2] };
      const form = { "f-title": "T", "f-goal": "G", "f-desc": "a\nb", "f-status": "todo", "f-priority": "1",
                     "f-assignee": "Old Timer", "f-reviewer": "", "f-tags": "x, y", "f-deps": "1, 2" };
      out.unchanged = app.taskChanges(task, form);
      out.dirtyNone = app.dirtyFields(task, form);
      out.changes = app.taskChanges(task, Object.assign({}, form, { "f-title": " New ", "f-assignee": "", "f-tags": "y",
                                                                   "f-deps": "#3", "f-priority": "3", "f-status": "waiting" }));
      out.dirty = app.dirtyFields(task, Object.assign({}, form, { "f-goal": "other" }));
    """)
    assert out["opts"].count("selected") == 1 and 'value="Old Timer" selected' in out["opts"]
    assert out["noneSelected"].startswith('<option value="" selected>')
    assert '<option value="review" disabled>' in out["status"] and 'value="in_progress" selected' in out["status"]
    assert 'value="review" selected>' in out["statusReview"] and "disabled" not in out["statusReview"]
    assert out["unchanged"] is None and out["dirtyNone"] == {}
    assert out["changes"] == {"title": "New", "assignee": "", "tags": ["y"], "depends_on": [3], "priority": 3,
                              "status": "waiting"}
    assert out["dirty"] == {"f-goal": "other"}


@needs_node
def test_live_update_keeps_unsaved_edits_in_the_task_form():
    out = run_js(r"""
      app.S.state = { agents: [{ name: "Ann Lee", role: "Dev", status: "idle" }] };
      const old = { id: 1, title: "Old title", goal: "Old goal", description: "", status: "todo", priority: 1,
                    assignee: null, reviewer: null, tags: [], depends_on: [] };
      const fresh = Object.assign({}, old, { title: "Agent title", goal: "Agent goal", status: "in_progress", assignee: "Ann Lee" });
      for (const [id, valueOf] of Object.entries(app.TASK_FORM)) el(id, { value: valueOf(old) });
      elements["f-title"].value = "My unsaved title";       // the user is typing here
      app.fillTaskForm(fresh, old, false);
      out.live = Object.fromEntries(Object.keys(app.TASK_FORM).map((id) => [id, elements[id].value]));
      out.statusOptions = elements["f-status"].innerHTML;
      app.fillTaskForm(fresh, fresh, true);                    // after the user's own save
      out.forced = elements["f-title"].value;
    """)
    live = out["live"]
    assert live["f-title"] == "My unsaved title"
    assert live["f-goal"] == "Agent goal" and live["f-status"] == "in_progress" and live["f-assignee"] == "Ann Lee"
    assert 'value="in_progress" selected' in out["statusOptions"]
    assert out["forced"] == "Agent title"


@needs_node
def test_drop_actions():
    out = run_js(r"""
      const t = (status) => ({ id: 1, status });
      out.r = [app.dropAction(t("todo"), "in_progress"), app.dropAction(t("todo"), "todo"), app.dropAction(t("failed"), "closed"),
               app.dropAction(t("in_progress"), "closed"), app.dropAction(t("todo"), "review"), app.dropAction(t("cancelled"), "todo"),
               app.dropAction(t("todo"), "nonsense"), app.dropAction(undefined, "done")];
    """)
    assert out["r"] == [{"status": "in_progress"}, None, None, {"status": "cancelled"}, {"error": "review_drop"},
                        {"status": "todo"}, None, None]
    out = run_js(r"""
      const t = (status) => ({ id: 1, status });
      out.r = [app.dropAction(t("review"), "done"), app.dropAction(t("review"), "in_progress"),
               app.dropAction(t("review"), "todo"), app.dropAction(t("review"), "closed"), app.dropAction(t("review"), "review")];
    """)
    # review -> done approves, a return needs a reason (the card opens), closing cancels as before
    assert out["r"] == [{"review": True}, {"open": True}, {"open": True}, {"status": "cancelled"}, None]


@needs_node
def test_review_on_the_board():
    out = run_js(r"""
      app.S.lang = "en";
      app.S.state = { swarm: { independent_review: true }, agents: [{ name: "Ann Lee", role: "Dev", status: "idle" }], tasks: [] };
      const t = (reviewer) => ({ id: 4, title: "T", status: "review", priority: 1, assignee: "Ann Lee", reviewer, tags: [],
                                 blocked_by: [], comments: 0 });
      out.cardUser = app.cardHtml(t("user"));
      out.cardAgent = app.cardHtml(t("Bob Stone"));
      out.opts = app.memberOptions("", true, "X");
      out.auto = app.reviewerNone();
      app.S.state.swarm.independent_review = false;
      out.none = app.reviewerNone();
      out.reviewUser = app.taskReviewHtml(t("user"));
      out.reviewAgent = app.taskReviewHtml(t("Bob Stone"));
      // a return without a reason makes no call
      app.S.openTaskId = 4;
      el("review-comment", { value: "  ", focus: () => { out.focused = true; } });
      const toasts = [];
      el("toasts", { children: [], appendChild: (box) => toasts.push(box.textContent) });
      document.createElement = () => ({ className: "", textContent: "", remove() {}, addEventListener() {} });
      await app.reviewTask(false);
      out.afterEmpty = calls.length;
      out.toasts = toasts.slice();
      elements["review-comment"].value = "Add tests for the parser";
      await app.reviewTask(false);
      // dragging a card from Review to Done approves it
      app.S.state.tasks = [t("user")];
      app.moveTask(4, "done");
      out.calls = calls.map((c) => [c.url, c.body]);
    """)
    assert '<span class="badge warn">👁 waits for your review</span>' in out["cardUser"]
    assert '<span class="chip plain">👁 Bob Stone</span>' in out["cardAgent"] and "badge warn" not in out["cardAgent"]
    assert out["opts"].startswith('<option value="" selected>X</option>')
    assert out["auto"] == "— (the board picks)" and out["none"] == "— none —"
    assert "The task waits for your review" in out["reviewUser"] and "instead of the reviewer" not in out["reviewUser"]
    assert "Task waits for review · Reviewer: Bob Stone" in out["reviewAgent"]
    assert "You can approve or return it instead of the reviewer." in out["reviewAgent"]
    assert out["afterEmpty"] == 0 and out["focused"] is True and out["toasts"] == ["Write in the comment what to fix."]
    assert out["calls"] == [
        ["/api/act/task_review", {"task_id": 4, "approve": False, "comment": "Add tests for the parser"}],
        ["/api/act/task_review", {"task_id": 4, "approve": True}],
    ]


@needs_node
def test_goal_and_hire_dialogs():
    out = run_js(r"""
      app.S.lang = "en";
      app.S.state = { swarm: { goal: { title: "Old", description: "", status: "active" }, session: null, models: ["default", "fast"] } };
      const handlers = {};
      const on = (id) => el(id, { addEventListener: (type, fn) => { handlers[id + ":" + type] = fn; } });
      el("modal"); el("modal-backdrop");
      // the goal dialog: the title is a textarea whose first line is the title; the text goes to the hub unchanged
      on("g-save"); el("g-title"); el("g-desc");
      app.goalModal();
      out.goalHtml = elements.modal.innerHTML;
      elements["g-title"].value = "  Snake game\nArrow keys, score  ";
      elements["g-desc"].value = "pytest";
      await handlers["g-save:click"]();
      // the hire dialog: the role catalog of the session
      globalThis.fetch = async (url, opts) => {
        calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
        const data = url.endsWith("/api/roles")
          ? { language: "en", roles: [{ key: "tester", name: "Tester", description: "You check <things>.", instructions: "",
                                        model: "fast", builtin: true, names: ["Tester", "tester", "Тестировщик"] }],
              hidden: [{ key: "researcher", name: "Researcher", description: "You find things out.", instructions: "",
                         model: null, builtin: true, names: ["Researcher", "researcher", "Исследователь"] }] }
          : { ok: true, result: "Hired" };
        return { ok: true, json: async () => data, text: async () => "" };
      };
      on("h-role"); on("h-save"); el("h-role-desc"); el("h-instr"); el("h-model"); el("h-name");
      await app.hireModal();
      out.hireHtml = elements.modal.innerHTML;
      const preview = (role) => { elements["h-role"].value = role; handlers["h-role:input"](); return elements["h-role-desc"].innerHTML; };
      out.previews = [preview("tester"), preview(" TESTER "), preview("QA engineer"), preview(""),
                      preview("тестировщик"), preview("Исследователь")];
      elements["h-role"].value = "Tester";
      await handlers["h-save:click"]();
      Object.assign(elements["h-model"], { value: "fast" });
      Object.assign(elements["h-instr"], { value: " write the tests " });
      Object.assign(elements["h-name"], { value: "Grace Hopper" });
      await handlers["h-save:click"]();
      out.calls = calls.filter((c) => c.url.includes("/api/")).map((c) => [c.url, c.body]);
    """)
    goal = out["goalHtml"]
    assert '<textarea id="g-title" rows="3"></textarea>' in goal and "<input id=\"g-title\"" not in goal
    assert "the first line is the title, the other lines the description" in goal
    hire = out["hireHtml"]
    assert '<input id="h-role" list="h-roles"' in hire and '<datalist id="h-roles"><option value="Tester"></option>' in hire
    assert '<select id="h-model"><option value="">— the role&#39;s or the session&#39;s profile</option>' in hire
    assert "optional for a catalog role" in hire
    known, upper, unknown, empty, other_language, hidden = out["previews"]
    assert known == upper == other_language == 'You check &lt;things&gt;. <span class="badge plain">role profile: fast</span>'
    assert hidden == "You find things out." and "Researcher" not in hire   # resolved by the hire, not offered
    assert unknown == "A new role, not in the catalog: describe its responsibilities in the instructions." and empty == ""
    assert out["calls"] == [
        ["/api/act/set_goal", {"title": "Snake game\nArrow keys, score", "description": "pytest"}],
        ["/api/roles", None],
        ["/api/act/hire_agent", {"role": "Tester", "instructions": ""}],   # instructions stay a required parameter
        ["/api/act/hire_agent", {"role": "Tester", "instructions": "write the tests", "model": "fast",
                                 "name": "Grace Hopper"}],
    ]


@needs_node
def test_discussion_thread_and_mentions():
    out = run_js(r"""
      app.S.state = { agents: [{ name: "Анна Смирнова", status: "idle" }, { name: "Анатолий Ким", status: "idle" },
                               { name: "Борис Лев", status: "idle" }] };
      const posts = [{ id: 1, author: "user", text: "root", reply_to: null, ts: 0 },
                     { id: 2, author: "Борис Лев", text: "second root", reply_to: null, ts: 0 },
                     { id: 3, author: "Анна Смирнова", text: "reply to 1", reply_to: 1, ts: 0 },
                     { id: 4, author: "user", text: "reply to 3", reply_to: 3, ts: 0 },
                     { id: 5, author: "user", text: "orphan", reply_to: 99, ts: 0 }];
      out.order = app.threadOrder(posts).map((x) => [x.post.id, x.depth]);
      out.html = app.threadHtml(posts);
      const names = app.S.state.agents.map((a) => a.name);
      out.q1 = app.mentionQuery("Hi @Ан", 6);
      out.q2 = app.mentionQuery("mail a@b", 8);
      out.q3 = app.mentionQuery("@Анна См", 8);
      out.c1 = app.mentionCandidates("ан", names);
      out.c2 = app.mentionCandidates("лев", names);
      out.c3 = app.mentionCandidates("анна с", names);
      out.c4 = app.mentionCandidates("анна смирнова", names);
    """)
    assert out["order"] == [[1, 0], [3, 1], [4, 2], [2, 0], [5, 0]]
    assert 'data-goto-post="1"' in out["html"] and "↪ Анна Смирнова" in out["html"] and "margin-left:44px" in out["html"]
    assert out["q1"] == {"start": 3, "query": "Ан"} and out["q2"] is None and out["q3"] == {"start": 0, "query": "Анна См"}
    assert out["c1"] == ["Анна Смирнова", "Анатолий Ким"]
    assert out["c2"] == ["Борис Лев"] and out["c3"] == ["Анна Смирнова"] and out["c4"] == []


@needs_node
def test_cancelled_dialogs_do_nothing_and_confirmed_ones_call_the_api():
    # regression: cancelling the "reject MCP" prompt still rejected the server
    out = run_js(r"""
      answers.prompt = null; out.cancelled = app.rejectMcp("csv_tools"); out.afterCancel = calls.length;
      answers.prompt = "  unsafe  "; await app.rejectMcp("csv_tools");
      answers.confirm = false; out.noDelete = app.deleteTrigger(3);
      answers.confirm = true; await app.deleteTrigger(3);
      out.calls = calls;
    """)
    assert out["cancelled"] is None and out["afterCancel"] == 0 and out["noDelete"] is None
    assert out["calls"] == [
        {"url": "/api/act/mcp_approve", "body": {"name": "csv_tools", "approve": False, "comment": "unsafe"}},
        {"url": "/api/act/trigger_delete", "body": {"trigger_id": 3}},
    ]


@needs_node
def test_open_chat_marks_new_messages_read():
    # regression: the unread check ran before the inbox was refreshed, so a reply arriving in the open
    # chat stayed "unread" in the sidebar
    out = run_js(r"""
      app.S.chatWith = "Ann Lee";
      app.S.userInbox = [{ id: 1, sender: "Ann Lee", read: false }, { id: 2, sender: "Bob", read: false }];
      app.autoMarkRead(); await new Promise((r) => setImmediate(r));
      out.calls = calls.slice();
      out.read = app.S.userInbox.map((m) => m.read);
      app.autoMarkRead(); await new Promise((r) => setImmediate(r));
      out.again = calls.length;
      app.S.chatWith = "all"; app.S.userInbox = [{ id: 3, sender: "Bob", read: false }]; app.autoMarkRead();
      out.all = calls.length;
    """)
    assert out["calls"] == [{"url": "/api/read", "body": {"with": "Ann Lee"}}]
    assert out["read"] == [True, False] and out["again"] == 1 and out["all"] == 1


@needs_node
def test_feed_requests_only_feed_events_and_describes_changes():
    out = run_js(r"""
      app.S.lang = "ru";
      await app.loadFeed();
      out.url = calls[0].url;
      out.updated = app.describeEvent({ type: "task.updated", actor: "Ann Lee", data: { task: { id: 3 },
        changes: { status: ["todo", "in_progress"], assignee: [null, "user"], priority: [1, 3], tags: [[], ["a", "b"]] } } });
      out.system = app.describeEvent({ type: "mcp.status", actor: null, data: { name: "x", status: "pending" } });
      app.S.lang = "en";
      out.en = app.describeEvent({ type: "file.changed", actor: null, data: { path: "a.txt", op: "edit" } });
      out.limits = app.limitsArgs("2,5", "4");
      out.bad = [app.limitsArgs("", "4"), app.limitsArgs("-1", "4"), app.limitsArgs("1", "0"), app.limitsArgs("1", "2.5")];
    """)
    url = urllib.parse.unquote(out["url"])
    assert url.startswith("/api/events?") and "types=task.*,message.*" in url and "agent.activity" not in url
    assert out["updated"] == ("Ann Lee изменил(а) #3: статус: К работе → В работе; исполнитель: — → пользователь (вы); "
                              "приоритет: обычный → критичный; теги: — → a, b")
    assert out["system"] == "MCP x: ждёт одобрения"
    assert out["en"] == "system edited file a.txt"
    assert out["limits"] == {"max_cost_usd": 2.5, "max_agents": 4} and out["bad"] == [None, None, None, None]


EVIL = "<img src=x onerror=alert(1)>\"'&"
EVIL_ESC = "&lt;img src=x onerror=alert(1)&gt;&quot;&#39;&amp;"


def assert_escaped(html: str, *, at_least: int = 1) -> None:
    assert "<img" not in html and EVIL not in html, html
    assert html.count(EVIL_ESC) >= at_least, html


@needs_node
def test_session_board_uses_the_session_base_for_api_ws_and_mcp():
    pre = r"""
      globalThis.location = { pathname: "/s/001-snake-game/", origin: "http://127.0.0.1:9", host: "127.0.0.1:9", protocol: "http:" };
      const sockets = [];
      globalThis.WebSocket = function (url) { sockets.push(url); return { close() {} }; };
      globalThis.wsUrls = sockets;
    """
    out = run_js(r"""
      out.base = app.BASE;
      await app.api("/api/state");
      app.connect();
      for (const id of ["back-link", "stopped-banner", "paused-banner", "goal-status", "mcp-badge", "triggers-badge", "team-unread"])
        el(id, { classList: classList(["hidden"]) });
      el("swarm-name"); el("mcp-hint"); el("pause-btn"); el("budget-bar"); el("goal-btn");
      app.S.state = { swarm: { name: "UI swarm", goal: null, limits: { max_cost_usd: 0, max_agents: 3 }, spent_usd: 0, mcp_url: null,
                               session: { id: "001-snake-game", title: "<b>Snake</b>", status: "stopped" } }, agents: [], mcp: [], triggers: [] };
      app.renderHeader();
      out.title = elements["swarm-name"].textContent;
      out.back = elements["back-link"].classList.contains("hidden");
      out.stopped = elements["stopped-banner"].classList.contains("hidden");
      out.hint = elements["mcp-hint"].innerHTML;
      app.S.state.swarm.session.status = "running"; app.renderHeader();
      out.running = elements["stopped-banner"].classList.contains("hidden");
      el("start-session-btn");
      calls.length = 0;
      await app.startSession();
      out.start = calls.map((c) => c.url);
      out.ws = globalThis.wsUrls;
    """, pre=pre)
    assert out["base"] == "/s/001-snake-game"
    assert out["ws"] == ["ws://127.0.0.1:9/s/001-snake-game/ws"]
    assert out["title"] == "<b>Snake</b>"  # textContent: shown as text
    assert out["back"] is False and out["stopped"] is False and out["running"] is True
    assert "http://127.0.0.1:9/s/001-snake-game/mcp" in out["hint"]
    assert out["start"] == ["/api/sessions/001-snake-game/start"]  # the launcher API, not the session's


@needs_node
def test_board_base_detection_and_readable_api_errors():
    for path, base in (("/", ""), ("/s/abc/", "/s/abc"), ("/s/abc", "/s/abc"), ("/s/ab_c/", ""), ("/static/x", ""),
                       ("/s/a-b-1/api/x", "/s/a-b-1")):
        out = run_js("out.base = app.BASE;", pre=f"globalThis.location = {{ pathname: {json.dumps(path)} }};")
        assert out["base"] == base, path
    out = run_js(r"""
      out.json = app.responseError(409, JSON.stringify({ ok: false, result: "The session is stopped." }));
      out.err = app.responseError(404, JSON.stringify({ error: "Trace not found." }));
      out.text = app.responseError(500, "Internal <b>Server</b> Error");
      globalThis.fetch = async () => ({ ok: false, status: 409, text: async () => JSON.stringify({ ok: false, result: "Stopped." }) });
      out.act = await app.act("task_create", { title: "x" });
      // the trace context modal widens #modal; the next modal must be normal again
      el("modal", { classList: classList(["wide"]) }); el("modal-backdrop");
      app.openModal("<p>x</p>");
      out.wide = elements.modal.classList.contains("wide");
    """)
    assert out["json"] == "The session is stopped." and out["err"] == "Trace not found."
    assert out["text"] == "500 Internal <b>Server</b> Error"
    assert out["act"] == {"ok": False, "result": "Stopped."}
    assert out["wide"] is False


@needs_node
def test_traces_html_escapes_every_agent_controlled_string():
    out = run_js(r"""
      const E = process.env.EVIL;
      const tr = loadTracesJs();
      app.S.lang = "en";
      out.item = tr.traceItemHtml({ id: 7, agent: E, status: E, trigger: E, kind: "llm", steps: 2, errors: 1, cost_usd: 0.5 });
      out.external = tr.traceItemHtml({ id: 8, agent: E, status: "done", kind: "external", tool_calls: 3 });
      tr.TR.expanded = new Set([1, 2, 3, 4, 5]);
      out.wf = tr.waterfallHtml({ started_at: 100, ended_at: 110, spans: [
        { id: 1, kind: "llm", name: E, status: "done", started_at: 100, ended_at: 104, usage: { input_tokens: 5 }, meta: { stop_reason: E },
          output: { thinking: E, text: E, tool_calls: [{ id: "c", name: E, arguments: { cmd: E }, parse_error: E }] } },
        { id: 2, kind: "tool", name: E, status: "error", started_at: 104, ended_at: 105, input: { path: E }, output: E,
          meta: { shown_chars: 5, chars: 10 } },
        { id: 3, kind: "event", name: E, status: "error", started_at: 105, output: E, error: E },
        { id: 4, kind: E, name: "x", status: "done", started_at: 106, output: { a: E } },
        { id: 5, kind: "llm", name: "step", status: "error", started_at: 106, error: E, meta: { retry_in: 2 } },
      ] });
      out.ctx = tr.contextHtml({ agent: E, model: E, note: E, system: E, tools: [{ name: E, description: E }],
        messages: [{ role: E, content: E, tool_calls: [{ name: E, arguments: { a: E } }] },
                   { role: "tool", name: E, content: { x: E }, is_error: true }, { role: "assistant", content: null }] }, 9);
      out.stats = tr.statsHtml({ traces: E, llm_calls: E, errors: 1, models: [{ name: E, calls: E }], tools: [{ name: E, calls: 2 }] });
      out.badge = tr.trStatusBadge("constructor");
    """, env={"EVIL": EVIL})
    assert_escaped(out["item"], at_least=2)  # the agent name and the wake-up text (the status falls back to text too)
    assert 'data-trace="7"' in out["item"] and "#7" in out["item"]
    assert_escaped(out["external"])
    wf = out["wf"]
    assert_escaped(wf, at_least=10)
    assert 'class="wf-bar k-&lt;img' in wf  # an unknown span kind is escaped inside the class attribute
    assert "retry in 2 s" in wf
    assert_escaped(out["ctx"], at_least=8)
    assert ">null<" not in out["ctx"]  # an assistant message with only tool calls has no "null" body
    assert_escaped(out["stats"], at_least=2)
    assert "<b>0</b>" in out["stats"]  # non-numeric totals are not injected
    assert out["badge"].startswith('<span class="badge plain">')


@needs_node
def test_traces_live_events_respect_the_filters():
    out = run_js(r"""
      const tr = loadTracesJs();
      const items = [{ id: 9, agent: "Ann", status: "running" }, { id: 7, agent: "Bob", status: "done" }];
      const m = (t, agent, status, more = false) => { const r = tr.mergeTraceEvent(items, t, agent, status, more); return r && r.map((x) => [x.id, x.status]); };
      out.update = m({ id: 9, agent: "Ann", status: "done" }, "", "");
      out.insert = m({ id: 10, agent: "Ann", status: "running" }, "", "");
      out.other_agent = m({ id: 10, agent: "Cid", status: "running" }, "Ann", "");
      out.leaves_filter = m({ id: 9, agent: "Ann", status: "done" }, "", "running");
      out.enters_failed = m({ id: 8, agent: "Ann", status: "step_limit" }, "", "failed");
      out.not_failed = m({ id: 10, agent: "Ann", status: "done" }, "", "failed");
      out.older_page = m({ id: 3, agent: "Ann", status: "done" }, "", "", true);
      out.older_all = m({ id: 3, agent: "Ann", status: "done" }, "", "", false);
      out.matches = [tr.traceMatches({ status: "interrupted" }, "", "failed"), tr.traceMatches({ status: "paused" }, "", "failed")];
    """)
    assert out["update"] == [[9, "done"], [7, "done"]]
    assert out["insert"] == [[10, "running"], [9, "running"], [7, "done"]]
    assert out["other_agent"] is None and out["not_failed"] is None and out["older_page"] is None
    assert out["leaves_filter"] == [[7, "done"]]
    assert out["enters_failed"] == [[9, "running"], [8, "step_limit"], [7, "done"]]
    assert out["older_all"] == [[9, "running"], [7, "done"], [3, "done"]]
    assert out["matches"] == [True, False]


@needs_node
def test_traces_pane_is_wired_once_and_ignores_stale_responses():
    # regression: every rebuild of the pane (the drawer's "Traces" button) added another click listener to
    # #pane-traces, so a span click toggled twice (= nothing) and "Show more" loaded twice
    out = run_js(r"""
      const tr = loadTracesJs();
      const counts = {};
      el("pane-traces", { addEventListener: (type) => { counts[type] = (counts[type] || 0) + 1; } });
      for (let i = 0; i < 3; i++) { tr.TR.loaded = false; await tr.loadTraces(); }
      out.counts = counts;
      out.urls = calls.map((c) => c.url);
      // a slow answer for an old filter must not replace the list of the current one
      const pending = [];
      globalThis.fetch = (url) => new Promise((resolve) => pending.push({ url, resolve }));
      const reply = (data) => ({ ok: true, json: async () => data, text: async () => JSON.stringify(data) });
      tr.TR.status = ""; const first = tr.loadTraces();
      tr.TR.status = "failed"; const second = tr.loadTraces();
      pending[1].resolve(reply({ traces: [{ id: 2, agent: "A", status: "error" }] })); await second;
      pending[0].resolve(reply({ traces: [{ id: 1, agent: "A", status: "done" }] })); await first;
      out.items = tr.TR.items.map((x) => x.id);
      out.second = pending[1].url;
      // the open trace's <details> state survives the live re-render
      tr.TR.openDetails = { "think-5": true };
      const span = { id: 5, kind: "llm", name: "step", status: "done", output: { thinking: "hm", text: "answer" } };
      out.kept = tr.spanDetailHtml(span);
      tr.TR.openDetails = {};
      out.fresh = tr.spanDetailHtml(span);
      // a payload clipped by ?payload= offers "Load in full"; the full span replaces it
      const tool = { id: 6, kind: "tool", name: "shell", status: "done", input: { command: "ls" }, output: "head\n…[123 chars cut]…\ntail", meta: {} };
      out.cut = tr.spanDetailHtml(tool);
      tr.TR.full[6] = Object.assign({}, tool, { output: "the whole output" });
      out.full = tr.spanDetailHtml(tool);
    """)
    assert out["counts"] == {"click": 1, "toggle": 1}
    assert out["urls"] == ["/api/traces?limit=50"] * 3
    assert out["items"] == [2] and out["second"] == "/api/traces?limit=50&status=failed"
    assert '<details class="sd-think" data-keep="think-5" open>' in out["kept"]
    assert '<details class="sd-think" data-keep="think-5">' in out["fresh"]  # with a reply, thinking starts closed
    assert 'data-span-full="6"' in out["cut"] and "chars cut" in out["cut"]
    assert 'data-span-full' not in out["full"] and "the whole output" in out["full"]


@needs_node
def test_home_cards_and_dialogs_escape_session_data():
    out = run_js(r"""
      const E = process.env.EVIL;
      const home = loadHomeJs();
      home.H.lang = "en";
      const s = { id: E, title: E, status: "running", paused: false, legacy: false, dir: E, created_at: 1, last_activity: 2,
                  model: E, model_profile: E, max_cost_usd: 2, spent_usd: 0.5, config_error: E,
                  goal: { title: E, description: E, status: "pending" }, tasks: { total: E, done: 1 }, agents: { total: E, working: E } };
      out.card = home.sessionCard(s);
      out.legacy = home.sessionCard({ id: "main", title: "Old", status: "stopped", legacy: true });
      out.broken = home.sessionCard({ id: "x", title: "x", status: "stopped", error: E });
      el("modal"); el("modal-backdrop");
      home.H.sessions = [s];
      home.deleteModal(E);
      out.del = elements.modal.innerHTML;
      home.H.original = { swarm: { language: "en", starting_roles: [E] }, agents: { default_model: E }, mcp: {},
                          models: { [E]: { model: E }, other: { model: "m" } } };
      await home.newSessionModal();
      out.ns = elements.modal.innerHTML;
      el("settings-form"); el("settings-nav"); el("profiles"); el("save-bar"); el("save-msg"); el("add-profile");
      const v = { swarm: { name: E, language: E, starting_roles: [E] }, agents: { default_model: E }, shell: { program: E, pass_env: [E] },
                  workspace: {}, server: { host: E }, tracing: {}, mcp: { library_dir: E, external: { [E]: { command: [E] } } },
                  models: { [E]: { provider: E, model: E, base_url: E, api_key_env: E, api_key_set: true, api_key_hint: E, effort: E,
                                   extra: { [E]: E } } } };
      home.H.view = v; home.H.original = JSON.parse(JSON.stringify(v)); home.H.path = E;
      home.H.providers = [{ id: E, label: E, provider: E, base_url: E, models: [E] }];
      home.renderSettings(true);
      out.settings = elements["settings-form"].innerHTML + elements["profiles"].innerHTML + elements["settings-nav"].innerHTML;
      out.t = home.t("started", { title: "{title} and {title}" });
      out.filter = [home.filterSessions([s, { id: "b", title: "Beta", status: "stopped" }], "stopped", ""),
                    home.filterSessions([s, { id: "b", title: "Beta", status: "stopped" }], "all", "BET")].map((l) => l.map((x) => x.id));
    """, env={"EVIL": EVIL})
    card = out["card"]
    assert_escaped(card, at_least=6)
    assert 'href="/s/%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E%22\'%26/"' in card  # the id is URL-encoded in links
    assert "<b>0</b> agents" in card and "1 of 0 tasks" in card  # non-numeric counters are not injected
    assert "data-delete" not in out["legacy"] and "project · main" in out["legacy"]
    assert_escaped(out["broken"])
    assert_escaped(out["del"])
    assert_escaped(out["ns"], at_least=3)
    settings = out["settings"]
    assert "<img" not in settings and EVIL not in settings and settings.count(EVIL_ESC) >= 10
    assert out["t"] == "Session “{title} and {title}” started"
    assert out["filter"] == [["b"], ["b"]]


@needs_node
def test_home_profile_key_semantics_and_no_key_is_ever_rendered():
    # regression: the payload turned every "" into null, so "remove key" and switching to an environment
    # variable silently KEPT the stored key (which wins over the variable)
    out = run_js(r"""
      const home = loadHomeJs();
      home.H.lang = "en";
      const view = { provider: "openai", model: "m", base_url: "", api_key: "sk-LEAK-0000", api_key_set: true,
                     api_key_hint: "…0000", api_key_env: null, api_key_env_set: null, extra: {} };
      const mk = (name, data, original = name) => home.makeProfile(name, Object.assign({}, view, data), original);
      const keep = mk("keep", {});
      const typed = mk("typed", {}); typed.newKey = "  sk-new-1234  ";
      const cleared = mk("cleared", {}); cleared.cleared = true; cleared.newKey = "";
      const env = mk("env", {}); env.keyMode = "env"; env.data.api_key_env = "MY_KEY";
      const renamed = mk("renamed", {}, "old-name");
      const fresh = mk("fresh", { api_key_set: false, api_key_hint: "" }, null);
      const freshTyped = mk("fresh2", { api_key_set: false }, null); freshTyped.newKey = "sk-x-9999";
      out.keys = Object.fromEntries([keep, typed, cleared, env, renamed, fresh, freshTyped].map((p) => [p.name, home.profilePayload(p).api_key]));
      out.renamed = home.profilePayload(renamed).renamed_from;
      out.payload = home.profilePayload(keep);
      out.modes = [keep.keyMode, mk("anth", { provider: "anthropic", api_key_set: false, api_key_env: "ANTHROPIC_API_KEY" }).keyMode];
      const bad = mk("bad", {}); bad.extraText = "[1, 2]";
      try { home.profilePayload(bad); out.badExtra = "accepted"; } catch (e) { out.badExtra = e.message; }
      home.H.original = { models: {} };
      out.card = home.profileCard(keep);
      out.envCard = home.profileCard(env);
      out.redacted = home.redactKey("401: invalid key sk-new-1234 given", typed);
    """)
    assert out["keys"] == {"keep": None, "typed": "sk-new-1234", "cleared": "", "env": "", "renamed": None,
                           "fresh": "", "fresh2": "sk-x-9999"}
    assert out["renamed"] == "old-name"
    payload = out["payload"]
    assert payload["base_url"] is None and payload["extra"] == {}
    assert not {"api_key_set", "api_key_hint", "api_key_env_set"} & set(payload)
    assert out["modes"] == ["key", "env"]
    assert "Invalid JSON" in out["badExtra"]
    for html in (out["card"], out["envCard"]):
        assert "sk-LEAK" not in html
    assert "…0000" in out["card"] and "will be removed on save" in out["envCard"]
    assert out["redacted"] == "401: invalid key …1234 given"


@needs_node
def test_home_settings_default_profile_validation_and_overrides():
    out = run_js(r"""
      const home = loadHomeJs();
      home.H.lang = "en";
      el("settings-form"); el("settings-nav"); el("profiles"); el("save-bar"); el("save-msg"); el("add-profile");
      const settings = {
        swarm: { name: "S", language: "ru", start_agents: 3, max_agents: 8, max_cost_usd: 5, starting_roles: ["Dev"] },
        agents: { default_model: "default", max_steps_per_turn: 40 }, shell: {}, workspace: {}, server: {}, tracing: {},
        mcp: { require_approval: false, external: {} },
        models: { default: { provider: "openai", model: "a", api_key_set: false }, def: { provider: "openai", model: "b", api_key_set: false } },
      };
      home.H.view = JSON.parse(JSON.stringify(settings)); home.H.original = JSON.parse(JSON.stringify(settings));
      home.renderSettings(true);
      const [a, b] = home.H.profiles;
      const input = (p, value) => home.onSettingsInput({ target: { dataset: { pf: "name" }, value, closest: () => ({ dataset: { uid: p.uid } }) } });
      // regression: typing a name through the default's name ("def" -> "default" -> "default2") moved the star
      for (const v of ["defa", "default", "default2"]) input(b, v);
      out.afterTyping = home.collectSettings().agents.default_model;
      input(a, "main");
      const v = home.collectSettings();
      out.default = v.agents.default_model;
      out.models = Object.keys(v.models);
      out.renamed = v.models.main.renamed_from;
      out.dirty = home.H.dirty;
      const errors = [];
      for (const [name, model] of [["bad name", "x"], ["main", "x"], ["ok", ""]]) {
        b.name = name; b.data.model = model;
        try { home.collectSettings(); errors.push(null); } catch (e) { errors.push(e.message); }
      }
      out.errors = errors;
      b.name = "ok"; b.data.model = "x";
      home.H.view.mcp.external = { __invalid: "External MCP servers (JSON)" };
      try { home.collectSettings(); } catch (e) { out.jsonError = e.message; }
      // text that does not parse yet survives a re-render (language switch) as typed
      home.H.view.mcp.external = home.readInput({ dataset: { type: "json", label: "X" }, value: '{"a": <b>' });
      out.jsonField = home.field("mcp_external", "mcp.external", "json");
      // new-session overrides against the SAVED defaults (the team: rows of the composer)
      const ov = (form) => home.sessionOverrides(settings, Object.assign({ lang: "ru", max: "8", budget: "5",
                                                                             team: home.teamRows(["Dev"], 3),
                                                                             model: "default", steps: "40", approval: false }, form));
      out.same = ov({});
      out.changed = ov({ lang: "en", budget: "0", team: [{ role: "generalist", count: 2 }], model: "def", steps: "12",
                         approval: true });
    """)
    assert out["afterTyping"] == "default"
    assert out["default"] == "main" and out["models"] == ["main", "default2"] and out["renamed"] == "default"
    assert out["dirty"] is True
    assert out["errors"] == ["Profile name “bad name”: use Latin letters, digits, “-” or “_”.",
                             "Two profiles are named “main”.", "Enter the model of profile “ok”."]
    assert out["jsonError"] == "Invalid JSON in “External MCP servers (JSON)”."
    assert ">{&quot;a&quot;: &lt;b&gt;</textarea>" in out["jsonField"]
    assert out["same"] == {"swarm": {"language": "ru", "start_agents": 3, "max_agents": 8, "max_cost_usd": 5}}
    assert out["changed"] == {"swarm": {"language": "en", "start_agents": 2, "starting_roles": [], "max_agents": 8,
                                        "max_cost_usd": 0},
                              "agents": {"default_model": "def", "max_steps_per_turn": 12}, "mcp": {"require_approval": True}}


TEAM_CASES = [([], 3), (["developer"], 3), (["dev", "tester"], 1), (["", "  "], 2), (["Разработчик"], 1),
              (["Tester", " tester ", "QA  lead", "generalist"], 5), (["generalist", "Developer"], 2), ([], 0)]


def role_presets() -> list[dict]:
    from dreamteam import roles
    return roles.presets_view()


@needs_node
def test_home_team_composer_matches_roles_py():
    from dreamteam import roles

    out = run_js(r"""
      const home = loadHomeJs();
      home.H.rolePresets = JSON.parse(process.env.DT_PRESETS);
      const cases = JSON.parse(process.env.DT_CASES);
      out.rows = cases.map(([r, n]) => home.teamRows(r, n).map((x) => [x.role, x.count]));
      out.values = cases.map(([r, n]) => { const v = home.teamValue(home.teamRows(r, n)); return [v.starting_roles, v.start_agents]; });
      out.mixed = home.teamValue([{ role: "generalist", count: 1 }, { role: "Developer", count: 2 }, { role: "Универсал", count: 2 }]);
      out.summaries = JSON.parse(process.env.DT_TEXTS).map((s) => home.roleSummary(s));
    """, env={"DT_PRESETS": json.dumps(role_presets()), "DT_CASES": json.dumps(TEAM_CASES),
              "DT_TEXTS": json.dumps(TEXTS_TO_SUMMARIZE)})
    assert out["rows"] == [[list(r) for r in roles.team_rows(*case)] for case in TEAM_CASES]
    assert out["values"] == [list(roles.team_value(roles.team_rows(*case))) for case in TEAM_CASES]
    assert out["mixed"] == {"starting_roles": ["generalist", "developer", "developer"], "start_agents": 5}
    assert out["summaries"] == [roles.summary(text) for text in TEXTS_TO_SUMMARIZE]


TEXTS_TO_SUMMARIZE = ["Hello world. Next.", "A" * 300, "word " * 100, "Short", "Ends with dots… then more", "x. y",
                      "  spaced   out text.  More "]


@needs_node
def test_home_new_session_composer():
    out = run_js(r"""
      const home = loadHomeJs();
      home.H.lang = "en";
      home.H.rolePresets = JSON.parse(process.env.DT_PRESETS);
      home.H.original = { swarm: { language: "en", start_agents: 3, max_agents: 4, max_cost_usd: 5, starting_roles: ["architect"] },
                          agents: { default_model: "default" }, mcp: {}, models: { default: { model: "m" } },
                          roles: [{ name: "tester", model: "fast" }, { name: "researcher", hidden: true },
                                  { name: "Data engineer", description: "You build data pipelines. Fast." }] };
      el("modal"); el("modal-backdrop");
      await home.newSessionModal();
      out.html = elements.modal.innerHTML;
      out.rows = JSON.parse(JSON.stringify(home.H.teams.ns));
      const fire = (type, attrs, value) => home.onTeamEvent({ type, target: { closest: () => ({ dataset: attrs, value }) } });
      const ns = (kind, row) => ({ team: kind, composer: "ns", row: String(row) });
      fire("click", ns("inc", 1));                                  // generalist 2 -> 3
      fire("click", { team: "add", composer: "ns" });               // the first visible role not used yet
      fire("change", ns("role", 2), "tester");
      fire("change", ns("role", 0), home.CUSTOM_ROLE);              // "Other role…"
      fire("input", ns("custom", 0), "Data  engineer");
      fire("input", ns("count", 2), "7");
      out.edited = JSON.parse(JSON.stringify(home.H.teams.ns));
      out.tooMany = home.teamError(home.H.teams.ns, "8");
      out.ok = home.teamError(home.H.teams.ns, "20");
      // "," and "*" separate the items of a team (roles.parse_team): not part of an own role's name
      out.badName = home.teamError([{ role: "developer*2", count: 1, custom: true }], "8");
      out.badComma = home.teamError([{ role: "architect, developer", count: 1, custom: true }], "8");
      out.overrides = home.sessionOverrides(home.H.original, { lang: "en", max: "20", budget: "5", team: home.H.teams.ns });
      fire("click", ns("del", 2)); fire("click", ns("del", 1)); fire("click", ns("del", 0));
      out.empty = home.teamError(home.H.teams.ns, "8");
      fire("click", { team: "add", composer: "ns" });
      out.afterAdd = JSON.parse(JSON.stringify(home.H.teams.ns));
      out.untouched = home.sessionOverrides(home.H.original, { lang: "en", max: "4", budget: "5",
                                                               team: home.teamRows(["architect"], 3) });
      // the saved list holds more roles than start_agents (a hand edit, or the old UI): a generalist added in
      // the modal must not let the session take the hidden third role
      const longer = { swarm: { start_agents: 2, starting_roles: ["Аналитик", "Разработчик", "Тестировщик"] } };
      const longerRows = home.teamRows(longer.swarm.starting_roles, 2);
      out.longerUntouched = home.sessionOverrides(longer, { lang: "en", team: longerRows });
      out.longerAdded = home.sessionOverrides(longer, { lang: "en", team: [...longerRows, { role: "generalist", count: 1 }] });
      out.longerFewer = home.sessionOverrides(longer, { lang: "en", team: [longerRows[0]] });
      out.composer = home.teamComposer("ns");
    """, env={"DT_PRESETS": json.dumps(role_presets())})
    html = out["html"]
    assert 'id="ns-start"' not in html and 'id="ns-roles"' not in html
    assert '<div class="team full" id="team-ns">' in html and "Agents in all: 3" in html
    assert 'the first line is the goal&#39;s title, the other lines its description' in html
    assert 'id="ns-goal" rows="4"' in html and "roles with a profile of their own keep it" in html
    assert "Snake game in Python\nArrow keys" in html  # the placeholder shows a two-line goal
    assert out["rows"] == [{"role": "architect", "count": 1}, {"role": "generalist", "count": 2}]
    assert out["edited"] == [{"role": "Data  engineer", "count": 1, "custom": True}, {"role": "generalist", "count": 3},
                             {"role": "tester", "count": 7, "custom": False}]
    assert out["tooMany"] == "The starting team has 11 agents, more than the maximum (8)." and out["ok"] is None
    assert out["badName"] == "Role name “developer*2”: at most 60 characters, no “,” or “*”."
    assert out["badComma"] == "Role name “architect, developer”: at most 60 characters, no “,” or “*”."
    assert out["overrides"] == {"swarm": {"language": "en", "start_agents": 11, "max_agents": 20, "max_cost_usd": 5,
                                          "starting_roles": ["Data engineer", "generalist", "generalist", "generalist",
                                                             "tester", "tester", "tester", "tester", "tester", "tester",
                                                             "tester"]}}
    assert out["empty"] == "Add at least one role to the starting team."
    assert out["afterAdd"] == [{"role": "generalist", "count": 1}]   # nothing used yet: the first visible role
    assert out["untouched"] == {"swarm": {"language": "en", "start_agents": 3, "max_agents": 4, "max_cost_usd": 5}}
    assert out["longerUntouched"] == {"swarm": {"language": "en", "start_agents": 2}}
    assert out["longerAdded"] == {"swarm": {"language": "en", "start_agents": 3,
                                            "starting_roles": ["analyst", "developer"]}}  # + an implicit generalist
    assert out["longerFewer"] == {"swarm": {"language": "en", "start_agents": 1}}  # the saved list's first role
    composer = out["composer"]
    # the saved catalog: names in the UI language, the role's model, a hidden role not offered, own roles after
    assert ">Generalist</option>" in composer and "model: fast" not in composer and ">Data engineer</option>" in composer
    assert ">Researcher" not in composer and 'value="__custom__"' in composer
    assert composer.index(">Generalist<") < composer.index(">Data engineer<")


@needs_node
def test_home_settings_roles_and_team():
    out = run_js(r"""
      const home = loadHomeJs();
      home.H.lang = "en";
      home.H.rolePresets = JSON.parse(process.env.DT_PRESETS);
      for (const id of ["settings-form", "settings-nav", "profiles", "save-bar", "save-msg", "add-profile", "roles", "team-defaults"]) el(id);
      const settings = {
        swarm: { name: "S", language: "en", start_agents: 3, max_agents: 8, max_cost_usd: 5, starting_roles: [],
                 independent_review: true },
        agents: { default_model: "default" }, shell: {}, workspace: {}, server: {}, tracing: {}, mcp: { external: {} },
        models: { default: { provider: "openai", model: "a", api_key_set: false }, fast: { provider: "openai", model: "b", api_key_set: false } },
        roles: [{ name: "tester", model: "fast", instructions: "Use pytest." }, { name: "researcher", hidden: true },
                { name: "Data engineer", description: "You build data pipelines." }],
      };
      home.H.view = JSON.parse(JSON.stringify(settings)); home.H.original = JSON.parse(JSON.stringify(settings));
      home.renderSettings(true);
      out.form = elements["settings-form"].innerHTML;
      out.cards = elements["roles"].innerHTML;
      out.untouched = home.collectSettings();
      // the composer of the defaults writes back only what changed: the canonical default keeps starting_roles []
      const fire = (type, attrs, value) => home.onSettingsInput({ type, target: { closest: () => ({ dataset: attrs, value }), dataset: {} } });
      const click = (attrs) => home.onSettingsClick({ type: "click", target: { closest: (sel) => (sel === "[data-team]" ? { dataset: attrs } : null) } });
      click({ team: "inc", composer: "defaults", row: "0" });
      out.changedTeam = [home.H.view.swarm.starting_roles, home.H.view.swarm.start_agents];
      click({ team: "dec", composer: "defaults", row: "0" });
      out.backTeam = [home.H.view.swarm.starting_roles, home.H.view.swarm.start_agents];
      // role cards: edit a built-in, hide one, add an own role
      const card = (id) => home.H.roleCards.find((c) => (c.builtin ? c.id : c.name) === id);
      const edit = (c, field, value) => home.onSettingsInput({ type: "input", target: { value, dataset: { rc: field },
        closest: (sel) => (sel === ".role-card" ? { dataset: { roleUid: c.uid }, querySelector: () => null } : null) } });
      const press = (c, attr) => home.onSettingsClick({ type: "click", target: {
        closest: (sel) => (sel === "button" ? { dataset: { [attr]: "" } } : sel === ".role-card" ? { dataset: { roleUid: c.uid } } : null) } });
      edit(card("developer"), "description", "You write the code. Always with tests.");
      edit(card("architect"), "description", home.H.rolePresets.find((p) => p.id === "architect").description.ru);  // a built-in text: no change
      press(card("writer"), "rcHide");
      press(card("tester"), "rcReset");
      home.onSettingsClick({ type: "click", target: { closest: (sel) => (sel === "button" ? { dataset: { roleAdd: "" } } : null) } });
      const fresh = home.H.roleCards[home.H.roleCards.length - 1];
      out.entries = () => { try { return home.collectSettings().roles; } catch (e) { return e.message; } };
      out.errors = [];
      out.errors.push(out.entries());                                   // the new card has no name
      edit(fresh, "name", "Data Engineer"); out.errors.push(out.entries());
      edit(fresh, "name", "Тестировщик"); out.errors.push(out.entries());
      edit(fresh, "name", "QA, lead"); out.errors.push(out.entries());
      edit(fresh, "name", "QA lead"); out.errors.push(out.entries());
      edit(fresh, "description", "You lead the testing."); edit(fresh, "model", "gone"); out.errors.push(out.entries());
      home.H.profiles.find((p) => p.name === "fast").name = "quick";  // a profile renamed in the same edit
      edit(fresh, "model", "fast");
      out.saved = home.collectSettings();
      out.composer = home.teamComposer("defaults");
      out.cardsAfter = home.H.roleCards.map(home.roleCard).join("");
    """, env={"DT_PRESETS": json.dumps(role_presets())})
    form = out["form"]
    assert form.index('id="s-models"') < form.index('id="s-roles"') < form.index('id="s-swarm"')
    assert 'data-path="swarm.independent_review"' in form and "Independent review of tasks" in form
    assert "a task without a reviewer is then done at once" in form
    assert 'data-path="swarm.start_agents"' not in form and 'data-path="swarm.starting_roles"' not in form
    assert '<div class="team full" id="team-defaults">' in form and "Agents in all: 3" in form
    cards = out["cards"]
    assert cards.count('class="role-card"') == 8 and cards.count('class="role-card hidden-role"') == 1  # 8 + 1 own role
    assert "ru: Тестировщик" in cards and ">changed</span>" in cards and "Use pytest." in cards
    assert out["untouched"]["roles"] == [{"name": "tester", "instructions": "Use pytest.", "model": "fast"},
                                         {"name": "researcher", "hidden": True},
                                         {"name": "Data engineer", "description": "You build data pipelines."}]
    assert out["untouched"]["swarm"]["starting_roles"] == [] and out["untouched"]["swarm"]["start_agents"] == 3
    assert out["changedTeam"] == [[], 4] and out["backTeam"] == [[], 3]
    assert out["errors"] == ["Enter the role name.", "Two roles are named “Data Engineer”.",
                             "“Тестировщик” is a built-in role: edit its card instead.",
                             "Role name “QA, lead”: at most 60 characters, no “,” or “*”.",
                             "Describe the role “QA lead”.",
                             "Role “QA lead” uses the profile “gone”, which does not exist."]
    assert out["saved"]["roles"] == [
        {"name": "developer", "description": "You write the code. Always with tests."},
        {"name": "writer", "hidden": True}, {"name": "researcher", "hidden": True},
        {"name": "Data engineer", "description": "You build data pipelines."},
        {"name": "QA lead", "description": "You lead the testing.", "model": "fast"},
    ]
    composer = out["composer"]
    assert ">QA lead</option>" in composer and ">Technical writer" not in composer  # the edited catalog
    assert ">Reset</button>" in out["cardsAfter"] and ">Show</button>" in out["cardsAfter"]


@needs_node
def test_home_polling_keeps_the_session_dom_and_busy_buttons():
    out = run_js(r"""
      const home = loadHomeJs();
      let writes = 0;
      const grid = el("sessions");
      let html = "";
      Object.defineProperty(grid, "innerHTML", { get: () => html, set: (v) => { writes++; html = v; } });
      el("sessions-empty");
      home.H.sessions = [{ id: "a", title: "A", status: "stopped" }];
      home.renderSessions(); home.renderSessions(); home.renderSessions();
      out.writes = writes;
      // a start in flight keeps its button disabled across the re-renders of the polled list
      let release;
      globalThis.fetch = (url) => new Promise((resolve) => { release = () => resolve({ ok: true, json: async () => ({ session: { title: "A" } }) }); });
      const started = home.startSession("a", null);
      home.renderSessions();
      out.during = /data-start="a" disabled/.test(html);
      home.startSession("a", null);  // a second click while busy does nothing
      release(); await started;
      out.after = /data-start="a" disabled/.test(html);
    """)
    assert out["writes"] == 1
    assert out["during"] is True and out["after"] is False


@needs_node
def test_home_card_shows_the_session_folder_and_delete_keeps_an_outside_one():
    out = run_js(r"""
      const E = process.env.EVIL;
      const home = loadHomeJs();
      home.H.lang = "en";
      const card = (id, workspace) => home.sessionCard({ id, title: "T", status: "stopped", workspace,
                                                         tasks: { total: 0, done: 0 }, agents: { total: 0, working: 0 } });
      // its own folder is created on demand: "exists: false" is not a missing folder
      out.own = card("own", { path: "/home/u/DreamTeam/sessions/own/workspace", default: true, adopted: false, exists: false });
      out.project = card("proj", { path: "/home/u/code/my-app", default: false, adopted: true, exists: true });
      out.moved = card("moved", { path: "D:\\work\\snake", default: false, adopted: false, exists: true });
      out.missing = card("gone", { path: "/media/usb/app", default: false, adopted: true, exists: false });
      out.evil = card("evil", { path: E, default: false, adopted: true, exists: false });
      out.none = card("old", undefined);
      out.long = card("long", { path: "C:\\Users\\someone\\DreamTeam\\sessions\\001-a-very-long-session-title\\workspace",
                               default: true, adopted: false, exists: true });
      out.short = [home.shortPath("C:\\Users\\kuzmi\\DreamTeam\\sessions\\001-snake-game\\workspace", 40),
                   home.shortPath("/a/b", 40), home.shortPath("x".repeat(60), 20)];
      out.search = home.filterSessions([{ id: "a", title: "A", workspace: { path: "/home/u/code/My-App" } }, { id: "b", title: "B" }],
                                       "all", "my-app").map((s) => s.id);
      home.H.lang = "ru";
      out.ru = card("proj", { path: "/p", default: false, adopted: true, exists: true }) + card("own", { path: "/o", default: true, exists: true });
      home.H.lang = "en";
      // delete: the dialog and the toast say that a folder outside the session stays
      const toasts = [];
      document.createElement = () => ({ className: "", textContent: "", remove() {} });
      el("toasts", { appendChild: (box) => toasts.push(box.textContent) });
      el("modal"); el("modal-backdrop");
      let confirmDelete = null;
      el("confirm-delete", { addEventListener: (type, fn) => { confirmDelete = fn; } });
      answers.confirm = true;
      home.H.sessions = [{ id: "proj", title: "Proj", status: "stopped", workspace: { path: "/home/u/code/my-app", default: false, adopted: true, exists: true } },
                         { id: "own", title: "Own", status: "stopped", workspace: { path: "/x/own/workspace", default: true, adopted: false, exists: true } },
                         { id: "evil", title: "Evil", status: "stopped", workspace: { path: E, default: false, adopted: false, exists: true } }];
      globalThis.fetch = async (url, opts) => {
        calls.push({ url, method: opts && opts.method });
        return { ok: true, json: async () => (url.startsWith("/api/sessions/proj") ? { ok: true, workspace_kept: "/home/u/code/my-app" } : { ok: true }) };
      };
      home.deleteModal("evil"); out.delEvil = elements.modal.innerHTML;
      home.deleteModal("own"); out.delOwn = elements.modal.innerHTML;
      home.deleteModal("proj"); out.delProj = elements.modal.innerHTML; await confirmDelete();
      home.deleteModal("own"); await confirmDelete();  // (the list reloaded after a delete is empty here)
      out.toasts = toasts;
      out.calls = calls.filter((c) => c.method === "DELETE").map((c) => c.url);
    """, env={"EVIL": EVIL})
    own = out["own"]
    # the path is an ltr run inside an rtl box: a path too long for the card loses its start, not its end
    assert '<div class="sc-folder"' in own and '<code><bdi dir="ltr">/home/u/DreamTeam/sessions/own/workspace</bdi></code>' in own
    assert '<span class="badge plain">own folder</span>' in own and "sc-warn" not in own
    assert "The session&#39;s own folder in the DreamTeam home" in own  # the tooltip explains the kind
    assert '<span class="badge ">project folder</span>' in out["project"] and "sc-warn" not in out["project"]
    assert "Deleting the session keeps the folder" in out["project"]
    assert '<bdi dir="ltr">D:\\work\\snake</bdi>' in out["moved"] and '<span class="badge ">moved</span>' in out["moved"]
    missing = out["missing"]
    assert '<span class="badge err">missing</span>' in missing and '<div class="sc-warn">' in missing
    assert "/cd FOLDER or /cd --default" in missing
    assert_escaped(out["evil"], at_least=2)  # the shown path and the tooltip
    assert "sc-folder" not in out["none"]  # an old hub without "workspace": no folder line
    # a long path is cut at the front (on a separator) and shown whole in the tooltip
    assert '<bdi dir="ltr">…\\sessions\\001-a-very-long-session-title\\workspace</bdi>' in out["long"]
    assert "Session folder: C:\\Users\\someone\\DreamTeam\\sessions\\001-a-very-long-session-title\\workspace" in out["long"]
    assert out["short"] == ["…\\sessions\\001-snake-game\\workspace", "/a/b", "…" + "x" * 19]
    assert out["search"] == ["a"]
    assert "папка проекта" in out["ru"] and "своя папка" in out["ru"]
    assert "Its folder /home/u/code/my-app stays where it is" in out["delProj"] and "workspace files" not in out["delProj"]
    assert "workspace files" in out["delOwn"] and "stays where it is" not in out["delOwn"]
    assert_escaped(out["delEvil"])
    assert out["toasts"] == ["Session deleted. The folder /home/u/code/my-app stays where it is.", "Session deleted"]
    assert out["calls"] == ["/api/sessions/proj?confirm=proj", "/api/sessions/own?confirm=own"]


@needs_node
def test_board_shows_the_session_folder_and_a_missing_one():
    out = run_js(r"""
      const E = process.env.EVIL;
      app.S.lang = "en";
      for (const id of ["back-link", "stopped-banner", "paused-banner", "workspace-banner", "goal-status", "mcp-badge",
                        "triggers-badge", "team-unread"])
        el(id, { classList: classList(["hidden"]) });
      for (const id of ["swarm-name", "mcp-hint", "pause-btn", "budget-bar", "goal-btn", "workspace-info", "file-list", "crumbs",
                        "file-path", "file-content", 'tabs > button[data-tab="files"]'])
        el(id);
      const state = (workspace) => ({ swarm: { name: "S", goal: null, limits: { max_cost_usd: 0, max_agents: 3 }, spent_usd: 0,
                                               mcp_url: "http://h/mcp", session: { id: "s", title: "S", status: "stopped" }, workspace },
                                      agents: [], mcp: [], triggers: [] });
      // a folder outside the session that is gone: a banner on every tab, the Files tab lists nothing (no API call)
      app.S.state = state({ path: E, default: false, adopted: true, exists: false });
      app.renderHeader();
      out.bannerShown = !elements["workspace-banner"].classList.contains("hidden");
      out.banner = elements["workspace-banner"].textContent;
      out.info = elements["workspace-info"].innerHTML;
      calls.length = 0;
      await app.openDir("src");
      out.missingCalls = calls.length;
      out.missingDir = app.S.fileDir;
      // the project folder (adopted): path, badge, tooltip on the Files tab; no banner
      app.S.state = state({ path: "/home/u/code/my-app", default: false, adopted: true, exists: true });
      app.renderHeader();
      out.bannerHidden = elements["workspace-banner"].classList.contains("hidden");
      out.project = elements["workspace-info"].innerHTML;
      out.tabTitle = elements['tabs > button[data-tab="files"]'].title;
      out.own = app.workspaceHtml({ path: "/x/sessions/s/workspace", default: true, adopted: false, exists: false });
      out.moved = app.workspaceHtml({ path: "/data/s", default: false, adopted: false, exists: true });
      out.none = app.workspaceHtml(undefined);
      out.kinds = [{ path: "/a", default: true }, { path: "/a", adopted: true }, { path: "/a" }, {}, null].map(app.workspaceKind);
      // after /cd the Files tab starts again from the root of the new folder
      app.S.tab = "files"; app.S.fileDir = "src"; app.S.openFile = "src/a.py";
      app.syncWorkspace();                                      // the first state: remembered, nothing reset
      out.first = [app.S.fileDir, app.S.openFile];
      app.S.state.swarm.workspace = { path: "/home/u/code/other", default: false, adopted: true, exists: true };
      calls.length = 0;
      app.syncWorkspace();
      await new Promise((r) => setImmediate(r));
      out.second = [app.S.fileDir, app.S.openFile, elements["file-path"].textContent, calls.map((c) => c.url)];
      calls.length = 0;
      app.syncWorkspace();                                      // unchanged: nothing reloaded
      out.third = calls.length;
    """, env={"EVIL": EVIL})
    assert out["bannerShown"] is True
    assert out["banner"].startswith("The session folder is missing: " + EVIL)  # textContent: shown as text
    assert "/cd FOLDER or /cd --default" in out["banner"]
    info = out["info"]
    assert_escaped(info)
    assert '<span class="badge err"' in info and ">missing</span>" in info and "/cd" not in info  # the banner says it
    assert out["missingCalls"] == 0 and out["missingDir"] == "."
    assert out["bannerHidden"] is True
    project = out["project"]
    assert '<code class="ws-path" id="ws-path" title="Click to copy">/home/u/code/my-app</code>' in project
    assert ">project folder</span>" in project and "commit to the current branch" in project and "badge err" not in project
    assert out["tabTitle"] == "Session folder: /home/u/code/my-app"
    assert '<span class="badge plain"' in out["own"] and ">own folder</span>" in out["own"] and "badge err" not in out["own"]
    assert ">moved</span>" in out["moved"] and "badge err" not in out["moved"]
    assert out["none"] == ""
    assert out["kinds"] == ["default", "adopted", "moved", None, None]
    assert out["first"] == ["src", "src/a.py"]
    assert out["second"] == [".", None, "Select a file on the left", ["/api/files?path=."]]
    assert out["third"] == 0


JOBS_PRE = r"""
  globalThis.jobsState = (E) => {
    const now = Date.now() / 1000;
    return [
      { id: 3, label: "j3", owner: E, command: E, origin: "background", status: "running", reason: null, exit_code: null,
        started_at: now - 1530, ended_at: null, log: ".scratch/" + E + "/jobs/j3.log", processes: ["node.exe", "node.exe", E] },
      { id: 2, label: "j2", owner: "Ann Lee", command: "pytest -q", origin: "timeout", status: "ended", reason: null,
        exit_code: 1, started_at: now - 600, ended_at: now - 360, log: "C:\\data\\jobs\\j2.log", processes: [] },
      { id: 1, label: "j1", owner: "Ann Lee", command: "serve", origin: "background", status: "stopped", reason: "user",
        exit_code: null, started_at: now - 900, ended_at: now - 840, log: ".scratch/ann/jobs/j1.log", processes: [] },
      { id: 4, owner: "Bob", command: "x", origin: "timeout", status: "stopped", reason: "brand-new", exit_code: null,
        started_at: now - 900, ended_at: now - 840, log: "", processes: [] },
      { id: 5, owner: "Bob", command: "make", origin: "detached", status: "lost", reason: "crash", exit_code: null,
        started_at: now - 900, ended_at: now - 840, log: null },
      { id: 6, owner: "Bob", command: "gradle build", origin: "detached", status: "ended", reason: null, exit_code: null,
        started_at: now - 900, ended_at: now - 840, log: ".scratch/bob/jobs/j6.log", processes: [] },
    ];
  };
"""


@needs_node
def test_jobs_tab_cards_output_and_stop():
    out = run_js(r"""
      const E = process.env.EVIL;
      app.S.lang = "en";
      el("job-list");
      app.S.state = { swarm: { name: "S", goal: null, limits: { max_cost_usd: 0, max_agents: 3 }, spent_usd: 0, mcp_url: "http://h/mcp" },
                      agents: [], mcp: [], triggers: [], jobs: jobsState(E) };
      app.S.tab = "jobs";
      app.renderJobs();
      out.html = elements["job-list"].innerHTML;
      out.states = app.S.state.jobs.map(app.jobState);
      // the output: the `jobs` tool as the user through the act API, shown escaped under its card
      calls.length = 0;
      globalThis.fetch = async (url, opts) => {
        calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
        return { ok: true, json: async () => ({ ok: true, result: "Job j3 of x\n" + E }), text: async () => "" };
      };
      app.toggleJobOutput(3);
      out.loading = elements["job-list"].innerHTML.includes('data-job-pre="3">…</pre>');
      await new Promise((r) => setImmediate(r));
      out.load = calls.map((c) => [c.url, c.body]);
      out.open = elements["job-list"].innerHTML;
      app.tickJobs();                                   // every few seconds while it runs
      await new Promise((r) => setImmediate(r));
      out.ticks = calls.length;
      app.handleEvent({ id: 41, ts: 0, type: "job.ended", actor: "x", data: { job: { id: 3, status: "ended" } } });
      await new Promise((r) => setImmediate(r));
      out.onEvent = calls.length;
      app.toggleJobOutput(3);
      out.closed = elements["job-list"].innerHTML.includes("data-job-pre");
      // stop: asks first; a "no" calls nothing
      calls.length = 0;
      answers.confirm = false;
      out.cancelled = await app.stopJob(3);
      out.cancelCalls = calls.length;
      answers.confirm = true;
      await app.stopJob(3);
      out.stop = calls.map((c) => [c.url, c.body]);
      // the clicks of a card
      const click = (dataset) => app.onDocumentClick({ target: { closest: () => ({ dataset }) } });
      calls.length = 0;
      click({ jobOut: "2" });
      await new Promise((r) => setImmediate(r));
      out.clickOut = [app.S.jobOpen.has(2), calls.map((c) => c.body)];
      click({ jobLog: ".scratch/ann/jobs/j1.log" });  // the log in the Files tab
      await new Promise((r) => setImmediate(r));
      out.log = [app.S.tab, app.S.fileDir, app.S.openFile, calls.slice(-2).map((c) => decodeURIComponent(c.url))];
      app.S.state.jobs = [];
      app.S.tab = "jobs";
      app.renderJobs();
      out.empty = elements["job-list"].innerHTML;
      app.S.lang = "ru";
      out.dur = [5, 59, 60, 754, 3600, 7500].map(app.fmtDur);
      out.ru = app.jobState(jobsState(E)[0])[1];
    """, pre=JOBS_PRE, env={"EVIL": EVIL})
    html = out["html"]
    assert_escaped(html, at_least=4)                    # owner, command, log, a process name
    assert html.count('data-job-stop="') == 1 and 'data-job-stop="3"' in html   # only a running job can be stopped
    assert html.count('data-job-out="') == 6 and 'aria-expanded="false"' in html
    assert "node.exe ×2, " + EVIL_ESC in html
    assert f'data-job-log=".scratch/{EVIL_ESC}/jobs/j3.log"' in html             # a workspace log: a link
    assert "<code>C:\\data\\jobs\\j2.log</code>" in html                         # outside the workspace: text
    assert '<div class="item job dim" data-job="2">' in html and '<div class="item job" data-job="3">' in html
    assert ">started in the background<" in html and ">went to the background at its timeout<" in html
    assert "ran 4 min · " in html
    assert out["states"] == [["", "running 25 min"], ["err", "ended: exit code 1"], ["plain", "stopped by you"],
                             ["plain", "stopped"], ["err", "lost in a crash"], ["done", "ended"]]
    assert out["loading"] is True
    assert out["load"] == [["/api/act/jobs", {"job_id": "j3", "tail": 200}]]
    assert '<pre class="job-output" data-job-pre="3">Job j3 of x\n' + EVIL_ESC + "</pre>" in out["open"]
    assert 'aria-expanded="true">Hide output</button>' in out["open"]
    assert out["ticks"] == 2 and out["onEvent"] == 3 and out["closed"] is False
    assert out["cancelled"] is None and out["cancelCalls"] == 0
    assert out["stop"] == [["/api/act/job_stop", {"job_id": "j3"}]]
    assert out["clickOut"] == [True, [{"job_id": "j2", "tail": 200}]]
    assert out["log"] == ["files", ".scratch/ann/jobs", ".scratch/ann/jobs/j1.log",
                          ["/api/files?path=.scratch/ann/jobs", "/api/files?path=.scratch/ann/jobs/j1.log"]]
    assert out["empty"].startswith('<div class="empty">No background jobs. A job appears when')
    assert out["dur"] == ["5 с", "59 с", "1 мин", "12 мин", "1 ч 00 мин", "2 ч 05 мин"]
    assert out["ru"] == "работает 25 мин"


@needs_node
def test_jobs_in_the_feed_team_panel_drawer_and_header():
    out = run_js(r"""
      const E = process.env.EVIL;
      app.S.lang = "en";
      const job = (extra) => ({ id: 3, owner: "Ann Lee", command: "npm run dev", started_at: 1000, ...extra });
      out.feed = [
        app.describeEvent({ type: "job.started", actor: "Ann Lee", data: { job: job({ status: "running" }) } }),
        app.describeEvent({ type: "job.ended", actor: "Ann Lee", data: { job: job({ status: "ended", exit_code: 0, duration: 245 }) } }),
        app.describeEvent({ type: "job.ended", actor: "Ann Lee", data: { job: job({ status: "stopped", reason: "user", ended_at: 1060 }) } }),
        app.describeEvent({ type: "job.ended", actor: "Ann Lee", data: {} }),
      ];
      out.feedLine = app.describeEvent({ type: "job.started", actor: E, data: { job: job({ owner: E, command: E }) } });
      app.S.lang = "ru";
      out.feedRu = app.describeEvent({ type: "job.ended", actor: "Ann Lee", data: { job: job({ status: "ended", exit_code: 2, duration: 3700 }) } });
      app.S.lang = "en";
      // the team panel: ⚙N (and a tooltip) for a member whose jobs run
      el("agent-list");
      app.S.userInbox = [];
      app.S.state = { swarm: { name: "S", goal: null, limits: { max_cost_usd: 0, max_agents: 3 }, spent_usd: 0, mcp_url: "http://h/mcp",
                               session: null, agent_count: 2 },
                      agents: [{ name: "Ann Lee", role: "Dev", status: "idle", kind: "llm", cost_usd: 0, open_tasks: [4], jobs: 2 },
                               { name: "Bob", role: "QA", status: "idle", kind: "llm", cost_usd: 0, open_tasks: [], jobs: 0 },
                               { name: "Claude Code", role: "Helper", status: "idle", kind: "external", cost_usd: 0, open_tasks: [] }],
                      jobs: [{ id: 3, owner: "Ann Lee", command: E, status: "running", started_at: Date.now() / 1000 - 125 },
                             { id: 5, owner: "Ann Lee", command: "pytest", status: "running", started_at: Date.now() / 1000 - 5 },
                             { id: 2, owner: "Ann Lee", command: "old", status: "ended", started_at: 0, ended_at: 1 }],
                      mcp: [], triggers: [] };
      app.renderAgents();
      out.agents = elements["agent-list"].innerHTML;
      // the drawer of a member: its running jobs, each with a stop button
      el("drawer-title"); el("drawer-jobs", { classList: classList(["hidden"]) }); el("composer-text");
      app.S.chatWith = "Ann Lee";
      app.renderDrawerHead();
      out.drawer = elements["drawer-jobs"].innerHTML;
      out.drawerShown = !elements["drawer-jobs"].classList.contains("hidden");
      app.S.chatWith = "Bob";
      app.renderDrawerHead();
      out.bobHidden = elements["drawer-jobs"].classList.contains("hidden");
      // the header: the running count on the tab; the agents counter counts what max_agents counts
      for (const id of ["back-link", "stopped-banner", "paused-banner", "workspace-banner", "goal-status", "mcp-badge",
                        "triggers-badge", "jobs-badge", "team-unread"])
        el(id, { classList: classList(["hidden"]) });
      for (const id of ["swarm-name", "mcp-hint", "pause-btn", "budget-bar", "goal-btn", "agents-count", "workspace-info"]) el(id);
      app.renderHeader();
      out.badge = [elements["jobs-badge"].textContent, elements["jobs-badge"].classList.contains("hidden")];
      out.count = elements["agents-count"].textContent;
      delete app.S.state.swarm.agent_count;              // an older hub: the AI agents of the state
      app.S.state.jobs = [];
      app.renderHeader();
      out.badge2 = [elements["jobs-badge"].textContent, elements["jobs-badge"].classList.contains("hidden")];
      out.count2 = elements["agents-count"].textContent;
    """, pre=JOBS_PRE, env={"EVIL": EVIL})
    assert out["feed"] == ["Ann Lee: background job j3 — npm run dev", "Ann Lee: job j3 ended: exit code 0 (4 min)",
                           "Ann Lee: job j3 stopped by you (1 min)", "Ann Lee: job ? ended (0 s)"]
    assert out["feedRu"] == "Ann Lee: процесс j3 завершён: код 2 (1 ч 01 мин)"
    assert EVIL in out["feedLine"]                       # plain text: feedLine() escapes it (linkify)
    agents = out["agents"]
    assert '<span class="jobs-mark">⚙2</span> #4 $0.00' in agents and agents.count("jobs-mark") == 1
    assert 'title="Ann Lee — Dev · idle · #4 · background jobs: 2"' in agents
    drawer = out["drawer"]
    assert_escaped(drawer, at_least=2)                    # the command and its tooltip
    assert drawer.count('data-job-stop="') == 2 and 'data-job-stop="3"' in drawer and 'data-job-stop="5"' in drawer
    assert "⚙ j3" in drawer and "2 min" in drawer and "5 s" in drawer and "old" not in drawer
    assert out["drawerShown"] is True and out["bobHidden"] is True
    assert out["badge"] == [2, False] and out["count"] == "2 / 3"
    assert out["badge2"] == ["", True] and out["count2"] == "2 / 3"   # the external member is not counted


# ------------------------------------------------------------------------------------------------ API contract
def make_swarm(tmp_path: Path) -> Swarm:
    raw: dict[str, Any] = {
        "swarm": {"name": "UI swarm", "language": "ru", "start_agents": 1, "max_agents": 3, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 0.2, "max_steps_per_turn": 10},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    cfg = Config.from_dict(tmp_path / "project", raw)

    def policy(system, messages, tools):
        last = next((m.get("content") or "" for m in reversed(messages) if m["role"] in ("user", "tool")), "")
        if "ping" in last:
            return "pong <b>#1</b>"
        return {"tool_calls": [{"name": "wait", "arguments": {}}]}

    return Swarm(config=cfg, llm_factory=lambda profile, agent: ScriptedLLM(policy, profile))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(method: str, url: str, body: Any = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(raw)
            except ValueError:
                return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


async def get(base: str, path: str) -> Any:
    status, data = await asyncio.to_thread(http, "GET", base + path)
    assert status == 200, (path, status, data)
    return data


async def act(base: str, tool: str, **args: Any) -> str:
    status, res = await asyncio.to_thread(http, "POST", base + "/api/act/" + tool, args)
    assert status == 200 and res["ok"], (tool, res)
    return res["result"]


async def until(fn, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await fn()
        if value:
            return value
        await asyncio.sleep(0.1)
    raise AssertionError("condition not reached")


@pytest.fixture(autouse=True)
def fast_scheduler(monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.1)


async def test_api_provides_every_field_the_ui_reads(tmp_path):
    import websockets

    from dreamteam.web import serve

    swarm = make_swarm(tmp_path)
    await swarm.start()
    port = free_port()
    server = asyncio.create_task(serve(swarm, "127.0.0.1", port))
    base = f"http://127.0.0.1:{port}"
    try:
        await until(lambda: _is_up(base))
        page = await get(base, "/")
        assert '/static/app.js' in page and '/static/style.css' in page
        for asset in ("app.js", "style.css"):
            await get(base, "/static/" + asset)
        agent = swarm.members()[0]["name"]
        enc = urllib.parse.quote(agent)

        async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
            await act(base, "set_goal", title="Ship the CLI", description="d")
            event = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert {"id", "ts", "type", "actor", "data"} <= set(event)

        await act(base, "task_create", title="Second", goal="It works", assignee=agent, tags=["x"], depends_on=[1])
        await act(base, "task_comment", task_id=2, text=f"@{agent} please")
        await act(base, "task_comment", task_id=2, text="and a reply", reply_to=1)
        await act(base, "trigger_create", condition="done(1)", note="go")
        await swarm.workspace.write(agent, "docs/readme.md", "# hi\n")
        await act(base, "send_message", to=[agent], text="ping")
        await until(lambda: _inbox_has_reply(base, agent))

        state = await get(base, "/api/state")
        sw = state["swarm"]
        assert {"name", "language", "paused", "pause_reason", "limits", "spent_usd", "goal", "mcp_url", "models"} <= set(sw)
        assert {"max_cost_usd", "max_agents"} <= set(sw["limits"]) and sw["models"] == ["default"]
        assert sw["workspace"] == {"path": str(swarm.workspace.root), "default": True, "adopted": False, "exists": True}
        assert {"title", "description", "status", "summary"} <= set(sw["goal"])
        task_keys = {"id", "title", "goal", "description", "status", "assignee", "reviewer", "created_by", "parent_id",
                     "depends_on", "priority", "tags", "result", "created_at", "updated_at", "blocked_by", "comments"}
        assert all(task_keys <= set(t) for t in state["tasks"])
        second = next(t for t in state["tasks"] if t["id"] == 2)
        assert second["blocked_by"] == [1] and second["comments"] == 2 and second["goal"] == "It works"
        agent_keys = {"name", "kind", "role", "status", "model", "cost_usd", "open_tasks", "jobs"}
        assert all(agent_keys <= set(a) for a in state["agents"])
        assert state["jobs"] == [] and sw["agent_count"] == 1
        assert {"id", "condition", "once", "owner", "fire_count", "note", "actions", "last_error"} <= set(state["triggers"][0])
        assert isinstance(state["mcp"], list) and isinstance(state["locks"], list)

        inbox = await get(base, "/api/inbox")
        reply = next(m for m in inbox["messages"] if m["sender"] == agent)
        assert {"id", "ts", "sender", "recipients", "text", "read"} <= set(reply) and reply["read"] is False
        convo = await get(base, f"/api/messages?with={enc}&limit=300")
        assert [m["sender"] for m in convo["messages"]] == [USER, agent]
        assert {"ts", "sender", "recipients", "text"} <= set(convo["messages"][0])
        assert (await get(base, "/api/messages?limit=300"))["messages"]
        status, res = await asyncio.to_thread(http, "POST", base + "/api/read", {"with": agent})
        assert status == 200 and res["ok"]
        assert all(m["read"] for m in (await get(base, "/api/inbox"))["messages"])

        detail = await get(base, "/api/tasks/2")
        assert {"task", "discussion", "events"} <= set(detail)
        assert all({"id", "author", "text", "reply_to", "ts"} <= set(p) for p in detail["discussion"])
        assert detail["discussion"][1]["reply_to"] == 1
        assert all({"ts", "type", "actor", "data"} <= set(e) for e in detail["events"])
        assert {e["type"] for e in detail["events"]} >= {"task.created", "task.comment"}

        activity = (await get(base, f"/api/agents/{enc}/activity?limit=300"))["activity"]
        assert activity and all({"id", "ts", "data"} <= set(a) and "kind" in a["data"] for a in activity)
        assert {"wake", "text"} <= {a["data"]["kind"] for a in activity}

        feed_types = json.loads(re.search(r"const FEED_TYPES = (\[.*?\]);", APP_JS).group(1))
        feed = (await get(base, "/api/events?limit=400&types=" + urllib.parse.quote(",".join(feed_types))))["events"]
        types = {e["type"] for e in feed}
        assert {"goal.set", "task.created", "task.comment", "message.sent", "trigger.created", "file.changed"} <= types
        assert not types & {"agent.activity", "agent.usage", "agent.status", "notification"}

        root = await get(base, "/api/files?path=.")
        assert root["type"] == "dir" and any(e["path"] == "docs" and e["type"] == "dir" for e in root["entries"])
        docs = await get(base, "/api/files?path=docs")
        assert docs["entries"] == [{"path": "docs/readme.md", "type": "file", "size": 5, "mtime": docs["entries"][0]["mtime"]}]
        file = await get(base, "/api/files?path=docs/readme.md")
        assert file["type"] == "file" and file["content"] == "# hi\n" and file["truncated"] is False

        await act(base, "trigger_delete", trigger_id=state["triggers"][0]["id"])
        await act(base, "task_update", task_id=2, status=Status.CANCELLED)
        state = await get(base, "/api/state")
        assert state["triggers"] == [] and next(t for t in state["tasks"] if t["id"] == 2)["status"] == "cancelled"
        await act(base, "swarm_control", max_cost_usd=3, max_agents=2)
        assert (await get(base, "/api/state"))["swarm"]["limits"] == {"max_cost_usd": 3.0, "max_agents": 2}
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
        await swarm.stop()
        swarm.close()


QUICK = "cmd" if sys.platform == "win32" else "sh"  # the fastest shell to start


def py_command(script: str, *args: str) -> str:
    """`$PYEXE script args` in the syntax of QUICK."""
    if sys.platform == "win32":
        return '"%PYEXE%" ' + " ".join(f'"{a}"' for a in (script, *args))
    return '"$PYEXE" ' + " ".join(f"'{a}'" for a in (script, *args))


async def test_api_serves_the_jobs_tab(tmp_path, monkeypatch):
    """A member's background job as the board sees it: the state's "jobs" and the agent's count, its output and its
    stop through the act API as the user, job.* in the feed. No process is left behind."""
    from dreamteam.web import serve
    from test_procs import Children

    monkeypatch.setenv("PYEXE", sys.executable)
    kids = Children(tmp_path)
    raw: dict[str, Any] = {
        "swarm": {"name": "UI swarm", "language": "en", "start_agents": 0, "max_agents": 3, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 30},
        "shell": {"program": QUICK},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    def waiting(*args):
        return {"tool_calls": [{"name": "wait", "arguments": {}}]}

    swarm = Swarm(config=Config.from_dict(tmp_path / "project", raw),
                  llm_factory=lambda profile, agent: ScriptedLLM(waiting, profile))
    await swarm.start()
    port = free_port()
    server = asyncio.create_task(serve(swarm, "127.0.0.1", port))
    base = f"http://127.0.0.1:{port}"
    try:
        await until(lambda: _is_up(base))
        member = await swarm.hire(USER, "Helper", name="Claude Code", kind="external")
        out = await swarm.jobs.run(member, py_command(str(tmp_path / "child.py"), "60", kids.pidfile()), timeout=60,
                                   env={**os.environ, "PYEXE": sys.executable}, max_output=4000, background=True)
        assert out.state == "started", out
        await asyncio.to_thread(kids.pid)
        state = await get(base, "/api/state")
        (job,) = state["jobs"]
        assert {"id", "label", "owner", "command", "origin", "status", "reason", "exit_code", "started_at",
                "ended_at", "log", "processes"} <= set(job)
        assert (job["label"], job["owner"], job["origin"], job["status"]) == ("j1", member, "background", "running")
        assert job["log"] == ".scratch/claude.code/jobs/j1.log"
        assert next(a for a in state["agents"] if a["name"] == member)["jobs"] == 1
        output = await act(base, "jobs", job_id="j1", tail=200)          # the card's "Output"
        assert output.startswith(f"Job j1 of {member}: ") and "child up" in output
        assert (await get(base, "/api/files?path=.scratch/claude.code/jobs/j1.log"))["type"] == "file"
        stopped = await act(base, "job_stop", job_id="j1")               # the card's "Stop"
        assert stopped.startswith(f"Stopped job j1 (") and f"of {member} after" in stopped
        assert not await asyncio.to_thread(lambda: kids.check())
        job = (await get(base, "/api/state"))["jobs"][0]
        assert (job["status"], job["reason"], job["stopped_by"]) == ("stopped", "user", "user")
        assert next(a for a in (await get(base, "/api/state"))["agents"] if a["name"] == member)["jobs"] == 0
        feed_types = json.loads(re.search(r"const FEED_TYPES = (\[.*?\]);", APP_JS).group(1))
        feed = (await get(base, "/api/events?limit=400&types=" + urllib.parse.quote(",".join(feed_types))))["events"]
        assert [e["type"] for e in feed if e["type"].startswith("job.")] == ["job.started", "job.ended"]
    finally:
        server.cancel()
        await asyncio.gather(server, return_exceptions=True)
        await swarm.stop()
        swarm.close()
        assert kids.check() == [], "child processes were left running"


async def _is_up(base: str) -> bool:
    try:
        return (await asyncio.to_thread(http, "GET", base + "/api/state"))[0] == 200
    except OSError:
        return False


async def _inbox_has_reply(base: str, agent: str) -> bool:
    inbox = await get(base, "/api/inbox")
    return any(m["sender"] == agent for m in inbox["messages"])


# ------------------------------------------------------------------------------------------------ launcher contract
SECRETS = ("sk-keep-1111", "sk-secret-2222", "sk-env-3333", "sk-reuse-4444")


def launcher_client(tmp_path: Path):
    from starlette.testclient import TestClient

    from dreamteam import settings as st
    from dreamteam.launcher import create_launcher_app
    from dreamteam.sessions import SessionManager

    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 2.0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 0.2, "max_steps_per_turn": 6},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {
            "default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0,
                        "api_key": SECRETS[0]},
            "secret": {"provider": "openai", "model": "gpt-x", "api_key": SECRETS[1]},
            "envkey": {"provider": "openai", "model": "gpt-y", "api_key": SECRETS[2]},
            "reuse": {"provider": "openai", "model": "gpt-z", "api_key": SECRETS[3]},
        },
    })

    def policy(system, messages, tools):
        return {"tool_calls": [{"name": "wait", "arguments": {}}], "thinking": "Nothing <b>yet</b>"}

    manager = SessionManager(home, llm_factory=lambda profile, agent: ScriptedLLM(policy, profile))
    client = TestClient(create_launcher_app(manager), base_url="http://127.0.0.1:8765")
    client.manager = manager
    return client


def _json_post(c, path: str, body: Any):
    return c.post(path, content=json.dumps(body), headers={"content-type": "application/json"})


def test_launcher_api_provides_every_field_the_ui_reads(tmp_path):
    with launcher_client(tmp_path) as c:
        try:
            page = c.get("/")
            assert page.status_code == 200 and "/static/home.js" in page.text and "/static/home.css" in page.text
            for asset in ("home.js", "home.css", "style.css", "app.js", "traces.js", "traces.css"):
                assert c.get("/static/" + asset).status_code == 200, asset

            # settings: every field the editor binds exists; no key value reaches the browser
            resp = c.get("/api/settings")
            assert not any(s in resp.text for s in SECRETS)
            data = resp.json()
            view = data["settings"]
            for section, key in re.findall(r'\bfield\("\w+", "(\w+)\.(\w+)"', HOME_JS):
                assert key in view[section], (section, key)
            profile_fields = (set(re.findall(r'\b(?:inp|sel)\("(\w+)"', HOME_JS))
                              | set(re.findall(r'data-pf="(\w+)"', HOME_JS))) - {"name", "new_key"}
            profile_fields |= {"api_key_set", "api_key_hint", "api_key_env_set", "api_key_env"}
            assert {"model", "base_url", "provider", "effort", "fallbacks", "extra"} <= profile_fields
            for name, profile in view["models"].items():
                assert profile_fields <= set(profile), (name, profile_fields - set(profile))
                assert profile["api_key"] is None and profile["api_key_set"] is True
            assert view["models"]["secret"]["api_key_hint"] == "…2222"
            assert all({"id", "label", "provider", "model", "models"} <= set(p) for p in data["providers"])
            assert data["path"].endswith("dreamteam.toml")
            # the role catalog: the presets (role cards, composer) and the merged catalog (TUI /roles, /team)
            assert [p["id"] for p in data["role_presets"]][:2] == ["generalist", "developer"]
            assert len(data["role_presets"]) == 8
            assert all(set(p["name"]) == set(p["description"]) == {"ru", "en"} for p in data["role_presets"])
            catalog_fields = {"key", "builtin", "customized", "hidden", "name", "description", "instructions", "model"}
            assert all(catalog_fields <= set(r) for r in data["role_catalog"]) and view["roles"] == []
            assert {"starting_roles", "start_agents", "independent_review"} <= set(view["swarm"])

            # a session created with the form's overrides
            overrides = {"swarm": {"language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 1.5},
                         "agents": {"max_steps_per_turn": 5}}
            resp = _json_post(c, "/api/sessions", {"title": "<b>T</b>", "goal": "Ship it", "description": "",
                                                   "overrides": overrides, "start": True})
            assert resp.status_code == 201, resp.text
            session = resp.json()["session"]
            sid = session["id"]
            card_fields = {"id", "title", "status", "paused", "legacy", "dir", "created_at", "last_activity", "model",
                           "model_profile", "max_cost_usd", "spent_usd", "config_error", "goal", "tasks", "agents", "url",
                           "workspace"}
            assert card_fields <= set(session) and session["url"] == f"/s/{sid}/"
            folder_fields = {"path", "default", "adopted", "exists"}  # the folder line of the card / the Files tab
            assert set(session["workspace"]) == folder_fields and session["workspace"]["default"] is True
            assert {"title", "description", "status"} <= set(session["goal"])
            assert {"total", "done"} <= set(session["tasks"]) and {"total", "working"} <= set(session["agents"])
            assert session["max_cost_usd"] == 1.5 and session["title"] == "<b>T</b>"
            assert not any(s in c.get("/api/sessions").text for s in SECRETS)

            # the board of the session: chrome fields, traces and their details
            assert "/static/traces.js" in c.get(f"/s/{sid}/").text
            state = c.get(f"/s/{sid}/api/state").json()
            assert state["swarm"]["session"] == {"id": sid, "title": "<b>T</b>", "status": "running",
                                                 "pending_goal": None}
            assert state["swarm"]["independent_review"] is True   # the board's "the board picks" reviewer option
            roles_view = c.get(f"/s/{sid}/api/roles").json()      # the board's hire dialog
            assert roles_view["language"] == "en"
            assert all({"key", "name", "description", "model"} <= set(r) for r in roles_view["roles"])
            assert state["swarm"]["mcp_url"] in (None, f"http://127.0.0.1:8765/s/{sid}/mcp")
            board_folder = state["swarm"]["workspace"]
            assert set(board_folder) == folder_fields and board_folder["default"] is True and board_folder["exists"] is True
            assert Path(board_folder["path"]).resolve() == Path(session["workspace"]["path"]).resolve()
            deadline = time.monotonic() + 20
            traces: list[dict] = []
            while time.monotonic() < deadline:
                traces = [x for x in c.get(f"/s/{sid}/api/traces?limit=50").json()["traces"] if x["status"] != "running"]
                if traces:
                    break
                time.sleep(0.1)
            assert traces, "no finished trace"
            summary_fields = {"id", "agent", "status", "kind", "steps", "duration_ms", "input_tokens", "cache_read_tokens",
                              "cache_write_tokens", "output_tokens", "cost_usd", "errors", "trigger", "started_at",
                              "tool_calls"}
            assert summary_fields <= set(traces[0])
            first = traces[-1]
            detail = c.get(f"/s/{sid}/api/traces/{first['id']}?payload=20000").json()
            assert {"spans", "tasks", "model", "llm_calls", "outcome", "ended_at"} <= set(detail)
            span_fields = {"id", "kind", "name", "step", "status", "started_at", "ended_at", "duration_ms", "parent_id",
                           "input", "output", "meta", "usage", "cost_usd", "error"}
            assert all(span_fields <= set(s) for s in detail["spans"])
            llm = next(s for s in detail["spans"] if s["kind"] == "llm")
            assert {"thinking", "text", "tool_calls"} <= set(llm["output"])
            assert llm["output"]["thinking"] == "Nothing <b>yet</b>"
            assert {"id", "name", "arguments"} <= set(llm["output"]["tool_calls"][0])
            assert c.get(f"/s/{sid}/api/spans/{llm['id']}").json()["id"] == llm["id"]
            ctx = c.get(f"/s/{sid}/api/spans/{llm['id']}/context").json()
            assert {"agent", "model", "system", "tools", "messages", "note"} <= set(ctx)
            assert {"role", "content"} <= set(ctx["messages"][0]) and {"name", "description"} <= set(ctx["tools"][0])
            stats = c.get(f"/s/{sid}/api/traces/stats?agent=" + urllib.parse.quote(first["agent"])).json()
            assert {"traces", "llm_calls", "tool_calls", "errors", "input_tokens", "output_tokens", "cache_read_tokens",
                    "cache_write_tokens", "cost_usd", "models", "tools"} <= set(stats)
            assert {"name", "calls", "errors", "avg_ms", "output_tokens", "cost_usd"} <= set(stats["models"][0])
            assert {"name", "calls", "errors", "avg_ms", "max_ms"} <= set(stats["tools"][0])
            export = c.get(f"/s/{sid}/api/traces/{first['id']}/export?contexts=1")
            assert export.status_code == 200 and "attachment" in export.headers["content-disposition"]
            jsonl = c.get(f"/s/{sid}/api/agents/{urllib.parse.quote(first['agent'])}/traces.jsonl")
            assert jsonl.status_code == 200 and jsonl.text.strip()
            assert c.get(f"/s/{sid}/api/traces/999999").json() == {"error": "Trace not found."}

            # stop: the launcher socket pushes the change, the board turns read-only with a readable error
            with c.websocket_connect("ws://127.0.0.1:8765/ws") as ws:
                assert _json_post(c, f"/api/sessions/{sid}/stop", {}).json()["session"]["status"] == "stopped"
                for _ in range(10):
                    event = ws.receive_json()
                    if event.get("change") == "stopped":
                        break
                assert event == {"type": "sessions.changed", "session": sid, "change": "stopped", "ts": event["ts"]}
            assert c.get(f"/s/{sid}/api/state").json()["swarm"]["session"]["status"] == "stopped"
            blocked = _json_post(c, f"/s/{sid}/api/act/task_create", {"title": "x", "goal": "y"})
            assert blocked.status_code == 409 and blocked.json()["result"].startswith("The session is stopped")
            assert c.get(f"/s/{sid}/api/traces").status_code == 200  # still viewable
            assert c.delete(f"/api/sessions/{sid}?confirm={sid}").json() == {"ok": True}

            # a session working in a folder of its own choosing: the card shows it, deleting keeps it
            project = tmp_path / "my-project"
            resp = _json_post(c, "/api/sessions", {"title": "Outside", "goal": "g", "start": False,
                                                   "workspace": str(project)})
            assert resp.status_code == 201, resp.text
            other = resp.json()["session"]
            assert resp.json()["result"]["mode"] == "created"
            folder = other["workspace"]
            assert folder["default"] is False and folder["adopted"] is False and folder["exists"] is True
            assert Path(folder["path"]).resolve() == project.resolve()
            listed = next(s for s in c.get("/api/sessions").json()["sessions"] if s["id"] == other["id"])
            assert listed["workspace"] == folder
            board = c.get(f"/s/{other['id']}/api/state").json()["swarm"]["workspace"]
            assert board == {**folder, "path": board["path"]} and Path(board["path"]).resolve() == project.resolve()
            gone = c.delete(f"/api/sessions/{other['id']}?confirm={other['id']}").json()
            assert gone == {"ok": True, "workspace_kept": folder["path"]} and project.is_dir()
        finally:
            c.portal.call(c.manager.shutdown)


@needs_node
def test_settings_saved_by_the_ui_keep_and_remove_keys_as_shown(tmp_path):
    from dreamteam import settings as st

    with launcher_client(tmp_path) as c:
        try:
            data = c.get("/api/settings").json()
            out = run_js(r"""
              const home = loadHomeJs();
              const data = JSON.parse(process.env.DT_SETTINGS);
              el("settings-form"); el("settings-nav"); el("profiles"); el("save-bar"); el("save-msg"); el("add-profile");
              home.H.view = data.settings; home.H.original = JSON.parse(JSON.stringify(data.settings)); home.H.providers = data.providers;
              home.renderSettings(true);
              const card = (name) => home.H.profiles.find((p) => p.name === name);
              home.H.rolePresets = data.role_presets;
              home.renderSettings(true);
              const tester = home.H.roleCards.find((c) => c.id === "tester");
              tester.model = "default"; tester.instructions = "Use pytest.";               // a built-in role customized
              home.H.roleCards.push({ uid: "own", builtin: false, name: "Data engineer", description: "Pipelines.",
                                      instructions: "", model: null, hidden: false });      // and an own role
              card("default").name = "main-model";                                        // rename the default profile
              card("secret").cleared = true; card("secret").newKey = "";                  // "Remove key"
              card("envkey").keyMode = "env"; card("envkey").data.api_key_env = "MY_ENV";  // use a variable instead
              home.H.profiles = home.H.profiles.filter((p) => p.name !== "reuse");        // delete a profile and add a
              home.H.profiles.push(home.makeProfile("reuse", { provider: "openai", model: "new", api_key_set: false }, null)); // new one with its name
              out.payload = home.collectSettings();
            """, env={"DT_SETTINGS": json.dumps(data)})
            payload = out["payload"]
            assert not any(s in json.dumps(payload) for s in SECRETS)
            resp = c.put("/api/settings", content=json.dumps({"settings": payload}),
                         headers={"content-type": "application/json"})
            assert resp.status_code == 200, resp.text
            assert not any(s in resp.text for s in SECRETS)
            raw = st.read_raw(c.manager.home)
            models = raw["models"]
            assert set(models) == {"main-model", "secret", "envkey", "reuse"}
            assert models["main-model"]["api_key"] == SECRETS[0]  # renamed: the key moves with the card
            assert "api_key" not in models["secret"]               # removed, as the form said
            assert "api_key" not in models["envkey"] and models["envkey"]["api_key_env"] == "MY_ENV"
            assert "api_key" not in models["reuse"]                # a new card does not inherit a deleted key
            assert raw["agents"]["default_model"] == "main-model"
            # the role follows the rename of its profile (the server maps renamed_from)
            assert raw["roles"] == [{"name": "tester", "instructions": "Use pytest.", "model": "main-model"},
                                    {"name": "Data engineer", "description": "Pipelines."}]
            assert resp.json()["role_catalog"][2]["model"] == "main-model"
            view = resp.json()["settings"]
            assert view["models"]["secret"]["api_key_set"] is False and view["models"]["main-model"]["api_key_set"] is True
        finally:
            c.portal.call(c.manager.shutdown)
