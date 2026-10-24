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
    # a Markdown field (mdFieldHtml) renders its textarea and its formatted view: id and id-view
    for field in re.findall(r'mdFieldHtml\("([\w-]+)"', APP_JS):
        template_ids |= {field, field + "-view"}
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
    assert panes == ["board", "feed", "files", "mcp", "skills", "triggers", "jobs", "traces"]
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
    assert len(sections) == 10 and {"s_roles", "s_skills"} <= sections
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
def test_member_colours_are_the_ones_of_the_terminal_ui():
    from dreamteam.tui import theme

    names = ["Анна Смирнова", "Bob Stone", "  Олег   Борисов ", "ГРИГОРИЙ Козлов", "Ирина Титова", "x", '<b x="1">'] + [f"Agent {i}" for i in range(24)]
    out = run_js(f"""
      out.colors = {json.dumps(names, ensure_ascii=False)}.map(app.agentColor);
      out.avatar = app.avatar("Bob Stone");
    """)
    assert out["colors"] == [theme.agent_color(n) for n in names]
    assert len(set(out["colors"])) > 6  # the names spread over the palette
    assert out["avatar"] == f'<span class="avatar " style="--c:{theme.agent_color("Bob Stone")}">BS</span>'


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
    assert '<span class="badge warn"><i class="ic ic-eye"></i>waits for your review</span>' in out["cardUser"]
    assert '<span class="chip plain"><i class="ic ic-eye"></i>Bob Stone</span>' in out["cardAgent"] and "badge warn" not in out["cardAgent"]
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
    # the role description is Markdown (escaped like any text), the model profile a badge after it
    assert known == upper == other_language == ('<div class="md"><p>You check &lt;things&gt;.</p></div>'
                                                '<span class="badge plain">role profile: fast</span>')
    assert hidden == '<div class="md"><p>You find things out.</p></div>' and "Researcher" not in hire  # not offered
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
    assert 'data-goto-post="1"' in out["html"] and '<i class="ic ic-reply"></i>Анна Смирнова</button>' in out["html"] and "margin-left:44px" in out["html"]
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


def test_session_settings_fields_match_the_config():
    """The settings dialog's per-session fields name real [shell] / [agents] settings, with config.py's minimums."""
    from dreamteam.config import _MIN_ONE, AgentSettings

    block = re.search(r"const SESSION_FIELDS = \[(.*?)\n\];", APP_JS, re.S).group(1)
    fields = re.findall(r'section: "(\w+)", key: "(\w+)", label: "(\w+)", kind: "(\w+)", min: (\d+)', block)
    assert len(fields) == 11 and block.count("{ group:") == 12   # + the network choice (docs/SPEC.md §7.2)
    assert '{ group: "network", section: "swarm", key: "network", label: "sf_network", kind: "choice", options: ["on", "off"] }' in block
    known = {"shell": {f.name for f in dataclasses.fields(ShellSettings)},
             "agents": {f.name for f in dataclasses.fields(AgentSettings)}}
    for section, key, label, kind, low in fields:
        assert key in known[section], key
        assert int(low) == (1 if key in _MIN_ONE else 0), key
        assert label in i18n_blocks()["en"], label
    assert ("shell", "cpu_threads", "sf_cpu", "auto", "0") in fields
    assert ("agents", "idle_nudge_seconds", "sf_nudge", "num", "0") in fields
    # the planning hint (docs/SPEC.md §3): 0 turns a trigger off; both fields carry a hint text
    assert ("agents", "planning_hint_steps", "sf_plan_steps", "int", "0") in fields
    assert ("agents", "planning_hint_minutes", "sf_plan_minutes", "num", "0") in fields
    assert block.count('hint: "sf_plan_hint"') == 2 and "sf_plan_hint" in i18n_blocks()["en"]


@needs_node
def test_the_session_network_choice():
    out = run_js(r"""
      app.S.lang = "en";
      const f = app.SESSION_FIELDS.find((x) => x.key === "network");
      out.parsed = [app.parseSessionField(f, ""), app.parseSessionField(f, "off"), app.parseSessionField(f, "maybe")];
      const texts = Object.fromEntries(app.SESSION_FIELDS.map((x) => [`${x.section}.${x.key}`, ""]));
      out.patch = app.sessionSettingsPatch({ ...texts, "swarm.network": "off" }, { swarm: { language: "ru" } }).patch;
      out.same = app.sessionSettingsPatch({ ...texts, "swarm.network": "off" }, { swarm: { network: "off" } }).patch;
      out.cleared = app.sessionSettingsPatch(texts, { swarm: { network: "off" } }).patch;
      out.html = app.sessionFieldsHtml({ swarm: { network: "off" } }, { swarm: { network: "on" } }, {}, app.SESSION_FIELDS, "netns");
      out.hints = ["docker", "netns", "env", null].map((k) => app.networkHint(k));
    """)
    assert out["parsed"][:2] == [{"value": None}, {"value": "off"}]
    assert out["parsed"][2] == {"error": "“Internet for the agents' commands”: pick a value from the list"}
    assert out["patch"] == {"swarm": {"network": "off"}} and out["same"] == {}
    assert out["cleared"] == {"swarm": {"network": None}}   # the general setting again
    html = out["html"]
    assert html.index("Network") < html.index('id="sf-swarm-network"')
    assert '<option value="">as in the general settings (on)</option>' in html
    assert '<option value="off" selected>off</option>' in html and "network namespace" in html
    docker, netns, env, unknown = out["hints"]
    assert "firewall" in docker and "namespace" in netns and "Not strict isolation" in env
    assert "Windows and macOS only proxies" in unknown


SETTINGS_HARNESS = r"""
  const handlers = {};
  const on = (id) => el(id, { addEventListener: (type, fn) => { handlers[id + ":" + type] = fn; } });
  el("modal"); el("modal-backdrop", { classList: classList(["hidden"]) }); on("s-save"); el("s-cost"); el("s-agents");
  const toasts = [];
  el("toasts", { children: [], appendChild: (box) => toasts.push([box.className.trim(), box.textContent]) });
  document.createElement = () => ({ className: "", textContent: "", remove() {}, addEventListener() {} });
  for (const f of app.SESSION_FIELDS) el(app.sessionFieldId(f), { focus() { out.focused = this.id; } });
  app.S.lang = "en";
  app.S.state = { swarm: { limits: { max_cost_usd: 2, max_agents: 3 } } };
"""


@needs_node
def test_session_settings_helpers():
    out = run_js(r"""
      app.S.lang = "en";
      const F = app.SESSION_FIELDS;
      const texts = (o) => Object.fromEntries(F.map((f) => [`${f.section}.${f.key}`, o[`${f.section}.${f.key}`] ?? ""]));
      const overrides = { swarm: { language: "ru" }, shell: { max_jobs: 3, cpu_threads: "4" }, agents: { context_tokens: 50000 } };
      out.same = app.sessionSettingsPatch(texts({ "shell.max_jobs": "3", "shell.cpu_threads": "4", "agents.context_tokens": " 50000 " }), overrides);
      out.changed = app.sessionSettingsPatch(texts({ "shell.max_jobs": "", "shell.cpu_threads": " AUTO ", "agents.context_tokens": "50000",
                                                     "shell.max_jobs_per_member": "0", "agents.idle_nudge_seconds": "7,5" }), overrides);
      out.bad = app.sessionSettingsPatch(texts({ "shell.max_jobs": "-1", "agents.context_tokens": "0", "shell.cpu_threads": "many",
                                                 "agents.idle_nudge_seconds": "1e3", "agents.max_steps_per_turn": "2.5" }), {})
        .errors.map((e) => [e.field.key, e.text]);
      out.parsed = [app.parseSessionField(F[3], "auto"), app.parseSessionField(F[3], "0"), app.parseSessionField(F[4], "0x10")];
      const defaults = { shell: { max_jobs_per_member: 4, max_jobs: 16, job_max_minutes: 480, cpu_threads: "auto" },
                         agents: { max_steps_per_turn: 40, context_tokens: 120000, tool_output_chars: 12000, idle_nudge_seconds: 60,
                                   max_parallel_llm_calls: 8 } };
      out.html = app.sessionFieldsHtml(Object.assign({}, overrides, { agents: { context_tokens: '"><img src=x>' } }), defaults,
                                       { agents: { default_model: "local" }, models: { local: { context_tokens: 32000 } } });
      out.hint = app.contextHint({ agents: { default_model: "default" }, models: { default: { context_tokens: null } } });
      out.sid = app.sessionSid();
    """)
    assert out["same"] == {"patch": {}, "errors": []}
    assert out["changed"] == {"patch": {"shell": {"max_jobs": None, "cpu_threads": "auto", "max_jobs_per_member": 0},
                                        "agents": {"idle_nudge_seconds": 7.5}}, "errors": []}
    assert out["bad"] == [
        ["max_jobs", "“Background jobs per session”: a whole number ≥ 0 is needed"],
        ["cpu_threads", "“Compute threads per command (auto or a number)”: auto or a whole number ≥ 0"],
        ["max_steps_per_turn", "“Steps per wake-up”: a whole number ≥ 1 is needed"],
        ["context_tokens", "“Context window, tokens”: a whole number ≥ 1 is needed"],
        ["idle_nudge_seconds", "“Nudge idle agents after, s”: a number ≥ 0 is needed"],
    ]
    assert out["parsed"][:2] == [{"value": "auto"}, {"value": 0}] and "error" in out["parsed"][2]
    html = out["html"]
    assert html.index("Background jobs") < html.index('id="sf-shell-max_jobs"') < html.index("Agents")
    assert 'id="sf-shell-max_jobs" inputmode="numeric" autocomplete="off" value="3" placeholder="16"' in html
    assert 'id="sf-shell-cpu_threads" inputmode="text" autocomplete="off" value="4" placeholder="auto"' in html
    assert 'id="sf-shell-job_max_minutes" inputmode="numeric" autocomplete="off" value="" placeholder="480" data-empty-edit>' in html
    assert 'id="sf-agents-idle_nudge_seconds" inputmode="decimal"' in html and "<img" not in html
    assert "a context window set in the model profile wins; the profile “local” sets 32000 now" in html
    assert "An empty field takes the value of the general settings" in html
    assert out["hint"] == "a context window set in the model profile wins" and out["sid"] == ""


@needs_node
def test_settings_dialog_of_a_launcher_session():
    pre = r"""
      globalThis.location = { pathname: "/s/002-tuned/" };
      const routes = {
        "/api/sessions/002-tuned": { session: { id: "002-tuned", overrides: { swarm: { language: "ru" }, shell: { max_jobs: 3 } } },
                                     config: { agents: { default_model: "default" }, models: { default: {} } } },
        "/api/settings": { settings: { shell: { max_jobs: 16, max_jobs_per_member: 4 }, agents: { context_tokens: 120000 } } },
      };
      globalThis.fetch = async (url, opts) => {
        const body = opts && opts.body ? JSON.parse(opts.body) : null;
        calls.push({ url, method: (opts && opts.method) || "GET", body });
        let data = routes[url] || { ok: true, result: "Limits set" };
        if (opts && opts.method === "PATCH") data = { session: { overrides: { swarm: { language: "ru" }, agents: body.settings.agents } }, config: {} };
        return { ok: true, json: async () => data, text: async () => "" };
      };
    """
    out = run_js(SETTINGS_HARNESS + r"""
      await app.settingsModal();
      out.html = elements.modal.innerHTML;
      out.loaded = calls.map((c) => [c.method, c.url]);
      calls.length = 0;
      // an invalid value: nothing is sent, the field gets the focus
      Object.assign(elements["s-cost"], { value: "2" }); Object.assign(elements["s-agents"], { value: "3" });
      elements["sf-agents-context_tokens"].value = "0";
      await handlers["s-save:click"]();
      out.invalid = [calls.length, toasts.slice(), out.focused];
      // the limits unchanged: only the PATCH, with the changed keys (the emptied max_jobs is null)
      toasts.length = 0;
      elements["sf-agents-context_tokens"].value = "64000";
      elements["sf-shell-max_jobs"].value = "";
      await handlers["s-save:click"]();
      out.saved = calls.map((c) => [c.method, c.url, c.body]);
      out.toasts = toasts.slice();
      out.closed = elements["modal-backdrop"].classList.contains("hidden");
      // the budget changed: swarm_control as well; the session fields untouched this time: no PATCH
      await app.settingsModal();
      calls.length = 0;
      elements["sf-agents-context_tokens"].value = "";
      elements["sf-shell-max_jobs"].value = "3";
      elements["s-cost"].value = "5";
      await handlers["s-save:click"]();
      out.limits = calls.map((c) => [c.method, c.url, c.body]);
    """, pre=pre)
    html = out["html"]
    assert out["loaded"] == [["GET", "/api/sessions/002-tuned"], ["GET", "/api/settings"]]  # launcher routes, no BASE
    assert 'id="sf-shell-max_jobs" inputmode="numeric" autocomplete="off" value="3" placeholder="16"' in html
    assert 'id="sf-agents-context_tokens" inputmode="numeric" autocomplete="off" value="" placeholder="120000"' in html
    assert "general settings" in html and "are set in the general settings" not in html
    assert out["invalid"] == [0, [["toast err", "“Context window, tokens”: a whole number ≥ 1 is needed"]],
                              "sf-agents-context_tokens"]
    assert out["saved"] == [["PATCH", "/api/sessions/002-tuned",
                             {"settings": {"shell": {"max_jobs": None}, "agents": {"context_tokens": 64000}}}]]
    assert out["toasts"] == [["toast", "Saved"]] and out["closed"] is True
    assert out["limits"] == [["POST", "/s/002-tuned/api/act/swarm_control", {"max_cost_usd": 5, "max_agents": 3}]]


@needs_node
def test_settings_dialog_edge_cases():
    """A failed limits change after a saved PATCH, a cleared field as an edit, a failed load, a modal opened meanwhile."""
    pre = r"""
      globalThis.location = { pathname: "/s/002-tuned/" };
      const net = { down: false, hold: null, stopped: true };
      globalThis.fetch = async (url, opts) => {
        const body = opts && opts.body ? JSON.parse(opts.body) : null;
        calls.push({ url, method: (opts && opts.method) || "GET", body });
        if (net.hold) await net.hold;
        if (net.down && url.startsWith("/api/")) return { ok: false, status: 502, text: async () => '{"error":"down"}' };
        if (url.endsWith("/swarm_control") && net.stopped) return { ok: false, status: 409, text: async () => '{"error":"stopped"}' };
        const data = url === "/api/sessions/002-tuned"
          ? { session: { overrides: opts && opts.method === "PATCH" ? { shell: body.settings.shell } : { shell: { max_jobs: 3 } } }, config: {} }
          : url === "/api/settings" ? { settings: { shell: { max_jobs: 16 } } } : { ok: true, result: "Limits set" };
        return { ok: true, json: async () => data, text: async () => "" };
      };
    """
    out = run_js(SETTINGS_HARNESS + r"""
      let confirms = 0;
      globalThis.confirm = () => { confirms++; return false; };
      // the fake DOM gives the modal's inputs to openModal's snapshot and to modalHasUnsaved
      const inputs = ["s-cost", "s-agents", ...app.SESSION_FIELDS.map(app.sessionFieldId)].map((id) => elements[id]);
      for (const input of inputs) input.hasAttribute = (name) => name === "data-empty-edit" && input.id.startsWith("sf-");
      elements.modal.querySelectorAll = () => inputs;
      document.querySelectorAll = (sel) => (sel.startsWith("#modal input") ? inputs : []);
      Object.assign(elements["s-cost"], { value: "2" }); Object.assign(elements["s-agents"], { value: "3" });
      elements["sf-shell-max_jobs"].value = "3";
      // a cleared field is an edit: closing asks first
      await app.settingsModal();
      elements["sf-shell-max_jobs"].value = "";
      out.clearedAsks = [app.closeModal(), confirms];
      // the PATCH gets through, swarm_control does not (a stopped session): both are said, closing does not ask
      elements["s-cost"].value = "5";
      toasts.length = 0; calls.length = 0;
      await handlers["s-save:click"]();
      out.partial = [calls.map((c) => [c.method, c.url]), toasts.slice()];
      out.budgetAsks = [app.closeModal(), confirms];  // the budget is still unsaved
      elements["s-cost"].value = "2";
      out.partialClose = [app.closeModal(), confirms];
      // a failed load: no per-session fields, and no claim that they live in the general settings
      net.down = true; toasts.length = 0;
      await app.settingsModal();
      out.failed = [elements.modal.innerHTML, toasts.slice()];
      app.closeModal(true);
      // another modal opened while loading stays
      net.down = false;
      let release;
      net.hold = new Promise((resolve) => { release = resolve; });
      const loading = app.settingsModal();
      app.openModal("TASK MODAL");
      release();
      await loading;
      out.kept = elements.modal.innerHTML;
    """, pre=pre)
    assert out["clearedAsks"] == [False, 1]
    calls, toasts = out["partial"]
    assert calls == [["PATCH", "/api/sessions/002-tuned"], ["POST", "/s/002-tuned/api/act/swarm_control"]]
    assert toasts == [["toast err", "stopped"], ["toast", "The session parameters are saved, the budget and max agents are not"]]
    assert out["budgetAsks"] == [False, 2] and out["partialClose"] == [True, 2]
    html, toasts = out["failed"]
    assert toasts == [["toast err", "Could not load the session parameters: down"]]
    assert "sf-" not in html and "did not load" in html and "are set in the general settings" not in html
    assert out["kept"] == "TASK MODAL"


@needs_node
def test_settings_dialog_of_the_main_session_and_a_standalone_app():
    for path in ("/s/main/", None):
        pre = f"globalThis.location = {{ pathname: {json.dumps(path)} }};" if path else ""
        out = run_js(SETTINGS_HARNESS + r"""
          await app.settingsModal();
          out.html = elements.modal.innerHTML;
          out.loaded = calls.length;
          Object.assign(elements["s-cost"], { value: "2" }); Object.assign(elements["s-agents"], { value: "4" });
          await handlers["s-save:click"]();
          out.calls = calls.map((c) => [c.url, c.body]);
        """, pre=pre)
        assert out["loaded"] == 0, path
        assert "sf-" not in out["html"] and "The other parameters (background jobs, agents) are set in the general settings." in out["html"]
        base = "/s/main" if path else ""
        assert out["calls"] == [[f"{base}/api/act/swarm_control", {"max_cost_usd": 2, "max_agents": 4}]], path


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
      // [swarm] network (docs/SPEC.md §7.2): sent only when the box differs from the saved default
      out.offline = ov({ offline: true }).swarm.network;
      out.notOffline = "network" in ov({ offline: false }).swarm;
      out.nsHint = home.networkHint("env");
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
    assert out["offline"] == "off" and out["notOffline"] is False
    assert "Not strict isolation here" in out["nsHint"] and "PowerShell 5.1" in out["nsHint"]


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
      // the dialog's buttons: Delete (or, with pending proposals, Save then delete / Delete them too)
      let onActions = null;
      el("delete-actions", { addEventListener: (type, fn) => { onActions = fn; } });
      const confirmDelete = () => onActions({ target: { closest: () => ({ dataset: { deleteSkills: "" } }) } });
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
def test_jobs_tab_marks_a_silent_job():
    """A running job the hub reports silent (silent_since: jobs.SILENT_MINUTES without output) gets a warn badge
    with how long it has been silent; a talking or ended job gets none."""
    out = run_js(r"""
      el("job-list");
      const now = Date.now() / 1000;
      const job = (id, extra) => Object.assign({ id, label: "j" + id, owner: "Ann Lee", command: "serve", origin: "background",
        status: "running", reason: null, exit_code: null, started_at: now - 3000, ended_at: null, log: "", processes: [] }, extra);
      app.S.state = { jobs: [job(1, { silent_since: now - 2430, last_output_at: null }),
                             job(2, { silent_since: null, last_output_at: now - 30 }),
                             job(3, { status: "ended", exit_code: 0, ended_at: now - 10, silent_since: now - 2000 })] };
      app.S.tab = "jobs";
      app.S.lang = "en";
      app.renderJobs();
      out.en = elements["job-list"].innerHTML;
      app.S.lang = "ru";
      app.renderJobs();
      out.ru = elements["job-list"].innerHTML;
      app.handleEvent({ id: null, ts: 0, type: "job.silent", actor: "Ann Lee", data: { job: job(2, { silent_since: now - 700 }) } });
      out.feed = app.S.feed.length;
    """)
    cards = {int(c.split('"', 1)[0]): c for c in out["en"].split('data-job="')[1:]}
    hint = "It has printed nothing for a long time and may hang: look at its output, and stop it if it is stuck."
    assert (f'<span class="badge warn" title="{hint}"><i class="ic ic-clock"></i>no output for 40 min</span>'
            in cards[1])
    assert "ic-clock" not in cards[2] and "ic-clock" not in cards[3]
    assert out["en"].count("no output for") == 1
    assert "<i class=\"ic ic-clock\"></i>40 мин без вывода</span>" in out["ru"]
    assert out["feed"] == 0   # a job.silent event (live only, it schedules a state reload) is no feed line


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


# ------------------------------------------------------------------------------------------------ Markdown
MD_PRE = 'const M = require(staticDir + "/markdown.js"); const R = (s) => M.render(s);'


def md_js(cases: dict[str, str], body: str = "") -> dict:
    """Markdown.render of every case (and app.md through `body`)."""
    return run_js(MD_PRE + f"\nconst CASES = {json.dumps(cases)};\n"
                  + "out.r = Object.fromEntries(Object.entries(CASES).map(([k, v]) => [k, R(v)]));\n" + body)


@needs_node
def test_markdown_blocks():
    r = md_js({
        "plain": "Hello\nworld",
        "paras": "one\n\n\ntwo",
        "headings": "# H1\n###### H6 ###\n#12 is a task\n#no-space",
        "fence": "```python\nx = 1 < 2\n  **not bold**\n```\nafter",
        "tilde": "~~~\na\n~~~",
        "unclosed": "text\n```\ncode\n\nmore",
        "quote": "> a\n> > b\n>\n> c",
        "ul": "- a\n- b\n  - c\n    - d\n- e",
        "ol": "3. a\n4. b",
        "mixed": "1. a\n  - nested\n2. b",
        "loose": "- a\n\n- b",
        "tasks": "- [ ] todo\n- [x] done\n- [X] Done\n- [y] no",
        "lazy": "Steps:\n1. first\n2. second\ncontinued",
        "table": "| a | b | c |\n|:--|:-:|--:|\n| 1 | **2** | 3 \\| 4 |\n| only |",
        "notable": "a | b\n--- x",
        "hr": "a\n\n---\n\n* * *\n___",
        "dash": "-\n+1 to that\n3.14 is pi",
        "indent": "Results:\n    x = 1\n    y = 2",
        "spaces": "col1    col2\nval     1",
        "nestedLoose": "- a\n  - b\n\n  - c",
        "fenceInItem": "- step one:\n  ```py\n  a = 1\n\n  b = 2\n  ```\n- step two",
        "itemGap": "- a\n\n  more\n- b",
        "listThenPara": "- a\n- b\n\npara",
        "siblingGap": "- a\n  - b\n\n- c",
    })["r"]
    assert r["plain"] == "<p>Hello<br>world</p>"           # a single newline is a line break
    assert r["paras"] == "<p>one</p><br><p>two</p>"       # the blank lines past the first stay, as in the plain text
    # a text without Markdown looks as written: the lines keep their indentation (and .md p keeps runs of spaces)
    assert r["indent"] == "<p>Results:<br>    x = 1<br>    y = 2</p>"
    assert r["spaces"] == "<p>col1    col2<br>val     1</p>"
    # loose only by blank lines between the items or between an item's own blocks (CommonMark)
    assert r["nestedLoose"] == "<ul><li>a<ul><li><p>b</p></li><li><p>c</p></li></ul></li></ul>"
    assert r["fenceInItem"] == ('<ul><li>step one:<div class="md-lang">py</div><pre><code>a = <span class="hl-number">1</span>'
                                '\n\nb = <span class="hl-number">2</span></code></pre></li><li>step two</li></ul>')
    assert r["itemGap"] == "<ul><li><p>a</p><p>more</p></li><li><p>b</p></li></ul>"
    assert r["listThenPara"] == "<ul><li>a</li><li>b</li></ul><p>para</p>"
    assert r["siblingGap"] == "<ul><li><p>a</p><ul><li>b</li></ul></li><li><p>c</p></li></ul>"
    assert r["headings"] == "<h1>H1</h1><h6>H6</h6><p>#12 is a task<br>#no-space</p>"
    # a fence that names its language is highlighted (highlight.js), still escaped and never Markdown inside
    assert r["fence"] == ('<div class="md-lang">python</div><pre><code>x = <span class="hl-number">1</span> &lt; '
                          '<span class="hl-number">2</span>\n  **<span class="hl-keyword">not</span> bold**</code></pre>'
                          "<p>after</p>")
    assert r["tilde"] == "<pre><code>a</code></pre>"
    assert r["unclosed"] == "<p>text</p><pre><code>code\n\nmore</code></pre>"   # runs to the end
    assert r["quote"] == "<blockquote><p>a</p><blockquote><p>b</p></blockquote><p>c</p></blockquote>"
    assert r["ul"] == "<ul><li>a</li><li>b<ul><li>c<ul><li>d</li></ul></li></ul></li><li>e</li></ul>"
    assert r["ol"] == '<ol start="3"><li>a</li><li>b</li></ol>'
    assert r["mixed"] == "<ol><li>a<ul><li>nested</li></ul></li><li>b</li></ol>"
    assert r["loose"] == "<ul><li><p>a</p></li><li><p>b</p></li></ul>"
    assert r["tasks"] == ('<ul><li class="md-task"><input type="checkbox" disabled> todo</li>'
                          '<li class="md-task"><input type="checkbox" disabled checked> done</li>'
                          '<li class="md-task"><input type="checkbox" disabled checked> Done</li><li>[y] no</li></ul>')
    assert r["lazy"] == "<p>Steps:</p><ol><li>first</li><li>second<br>continued</li></ol>"
    assert r["table"] == ('<div class="md-table"><table><thead><tr><th class="md-l">a</th><th class="md-c">b</th>'
                          '<th class="md-r">c</th></tr></thead><tbody><tr><td class="md-l">1</td><td class="md-c">'
                          '<strong>2</strong></td><td class="md-r">3 | 4</td></tr><tr><td class="md-l">only</td>'
                          '<td class="md-c"></td><td class="md-r"></td></tr></tbody></table></div>')
    assert r["notable"] == "<p>a | b<br>--- x</p>"
    assert r["hr"] == "<p>a</p><hr><hr><hr>"
    assert r["dash"] == "<p>-<br>+1 to that<br>3.14 is pi</p>"


@needs_node
def test_markdown_inline():
    r = md_js({
        "em": "**b** __b__ *i* _i_ ~~s~~ ***bi***",
        "nested": "**a *b* c** *a **b** c*",
        "snake": "snake_case_name and file_v2_final.py, 2*3*4 and a * b",
        "code": "`a *b* <c> **d**` and ``x ` y`` and ` `` `",
        "lonely": "a ` b and ~5 min and ~~~x",
        "link": '[the docs](https://example.com/a_b?x=1&y=2 "title") and [mail](mailto:a@b.c)',
        "auto": "<https://example.com/x> and <ftp://x.y> and <b>",
        "bare": "see https://example.com/path_(x)/a_b, (https://x.y/z). or http://q.r/s!",
        "escape": r"\*not em\* \_x\_ \# \[a](b) a\\b",
        "image": "![a cat](https://img.example/cat.png)",
        "linkcode": "[`code` and **bold**](https://x.y)",
        "nolinkinlink": "[a [b](https://x.y) c](https://z.w)",
        "latex": "$x_1 + y_2 = z$",
        "latexEm": "Let $a*b$ and $c*d$ be, $x^*$ and $y^*$ too",
        "latexEsc": "$\\{x\\} \\|y\\|_2$ and $$\\begin{a} p &= q \\\\ r \\end{a}$$",
        "latexParens": "Inline \\(x_1\\) and display \\[a \\, b\\]",
        "latexLines": "$$\na_1 = *b*\n$$",
        "dollars": "costs $5 and $6, *em*\n\\$x\\$ and $ a $ *em*",
        "latexHtml": "$<b>x</b>$ and $#12$",
        "badge": "[![build](https://ci.example/badge.svg)](https://ci.example/run)",
        "imgInLink": "[a ![b](http://x.y) c](http://z.w)",
    })["r"]
    a = lambda href: f'<a href="{href}" target="_blank" rel="noopener noreferrer">'  # noqa: E731
    assert r["em"] == ("<p><strong>b</strong> <strong>b</strong> <em>i</em> <em>i</em> <del>s</del> "
                       "<em><strong>bi</strong></em></p>")
    assert r["nested"] == "<p><strong>a <em>b</em> c</strong> <em>a <strong>b</strong> c</em></p>"
    assert r["snake"] == "<p>snake_case_name and file_v2_final.py, 2<em>3</em>4 and a * b</p>"
    assert r["code"] == ("<p><code>a *b* &lt;c&gt; **d**</code> and <code>x ` y</code> and <code>``</code></p>")
    assert r["lonely"] == "<p>a ` b and ~5 min and ~~~x</p>"
    assert r["link"] == f'<p>{a("https://example.com/a_b?x=1&amp;y=2")}the docs</a> and {a("mailto:a@b.c")}mail</a></p>'
    assert r["auto"] == f'<p>{a("https://example.com/x")}https://example.com/x</a> and &lt;ftp://x.y&gt; and &lt;b&gt;</p>'
    assert r["bare"] == (f'<p>see {a("https://example.com/path_(x)/a_b")}https://example.com/path_(x)/a_b</a>, '
                         f'({a("https://x.y/z")}https://x.y/z</a>). or {a("http://q.r/s")}http://q.r/s</a>!</p>')
    assert r["escape"] == r"<p>*not em* _x_ # [a](b) a\b</p>"
    assert r["image"] == f'<p>{a("https://img.example/cat.png")}a cat</a></p>'   # no images: a link
    assert r["linkcode"] == f'<p>{a("https://x.y")}<code>code</code> and <strong>bold</strong></a></p>'
    assert r["nolinkinlink"].count("<a ") == 2 and r["nolinkinlink"].startswith(f'<p>[a {a("https://x.y")}b</a> c](')
    assert r["latex"] == "<p>$x_1 + y_2 = z$</p>"   # no math: the text as written
    # LaTeX is kept as written: no emphasis inside or across formulas, no backslash escapes
    assert r["latexEm"] == "<p>Let $a*b$ and $c*d$ be, $x^*$ and $y^*$ too</p>"
    assert r["latexEsc"] == r"<p>$\{x\} \|y\|_2$ and $$\begin{a} p &amp;= q \\ r \end{a}$$</p>"
    assert r["latexParens"] == r"<p>Inline \(x_1\) and display \[a \, b\]</p>"
    assert r["latexLines"] == "<p>$$<br>a_1 = *b*<br>$$</p>"
    assert r["dollars"] == "<p>costs $5 and $6, <em>em</em><br>$x$ and $ a $ <em>em</em></p>"   # prices are not math
    assert r["latexHtml"] == "<p>$&lt;b&gt;x&lt;/b&gt;$ and $#12$</p>"
    # an image in a link's text is its alt text: an <a> never holds another <a>
    assert r["badge"] == f'<p>{a("https://ci.example/run")}build</a></p>'
    assert r["imgInLink"] == f'<p>{a("http://z.w")}a b c</a></p>'


@needs_node
def test_markdown_task_links_and_mentions_only_in_plain_text():
    out = run_js(r"""
      app.S.state = { agents: [{ name: "Ann Lee", status: "idle" }] };
      out.plain = app.md("Done #12, ask @Ann Lee");
      out.code = app.md("`#12 @Ann Lee` and\n```\n#13 @Ann Lee\n```");
      out.link = app.md("[see #14 @Ann Lee](https://x.y/#15) https://x.y/#16");
      out.heading = app.md("## #17 for @user\n- #18");
      out.cached = app.md("Done #12, ask @Ann Lee") === out.plain && app.MD_CACHE.size > 0;
      app.S.state.agents.push({ name: "Bob", status: "idle" });
      out.newName = app.md("@Bob");   // the cache key holds the names: a new member is highlighted at once
    """)
    btn = lambda n: f'<button type="button" class="link" data-open-task="{n}">#{n}</button>'  # noqa: E731
    assert out["plain"] == f'<div class="md"><p>Done {btn(12)}, ask <span class="mention">@Ann Lee</span></p></div>'
    assert "data-open-task" not in out["code"] and "mention" not in out["code"]
    assert "data-open-task" not in out["link"] and "mention" not in out["link"]
    assert 'href="https://x.y/#15"' in out["link"] and ">https://x.y/#16</a>" in out["link"]
    assert btn(17) in out["heading"] and btn(18) in out["heading"] and '<span class="mention">@user</span>' in out["heading"]
    assert out["cached"] is True
    assert '<span class="mention">@Bob</span>' in out["newName"]


@needs_node
def test_markdown_is_safe_against_untrusted_input():
    vectors = {
        "script": "<script>alert(1)</script>",
        "img": '<img src=x onerror="alert(1)">',
        "js": "[x](javascript:alert(1))",
        "jsCase": "[x](JaVaScRiPt:alert(1))",
        "jsSpace": "[x]( javascript:alert(1))",
        "jsEntity": "[x](javascript&#58;alert(1))",
        "jsTab": "[x](java\tscript:alert(1))",
        "jsCtrl": "[x](\u0001javascript:alert(1))",
        "data": "[x](data:text/html;base64,PHNjcmlwdD4=)",
        "vb": "[x](vbscript:msgbox(1))",
        "auto": "<javascript:alert(1)>",
        "imgJs": "![x](javascript:alert(1))",
        "quote": '[x](https://a.b/"onmouseover="alert(1))',
        "quote2": "[x](<https://a.b/\" onclick=\"alert(1)>)",
        "bareQuote": 'https://a.b/"onmouseover="alert(1)',
        "codeHtml": "`<img src=x onerror=alert(1)>`\n```\n<script>alert(1)</script>\n```",
        "linkText": "[<img src=x onerror=alert(1)>](https://a.b)",
        "lang": "```\"><script>alert(1)</script>\nx\n```",
        "table": "| <b>a</b> |\n|---|\n| <i onclick=x>b</i> |",
        "heading": "# <svg onload=alert(1)>",
        # a highlighted fence: its tags and quotes become escaped text inside <span class="hl-…">
        "fenceHtml": '```html\n<img src=x onerror="alert(1)"><script>alert("</code>")</script>\n```',
    }
    out = md_js(vectors, r"""
      out.app = app.md("<script>alert(1)</script> [x](javascript:alert(1)) @<b>");
    """)
    r = out["r"]
    allowed = {"p", "/p", "a", "/a", "code", "/code", "pre", "/pre", "div", "/div", "h1", "/h1", "table", "/table",
               "thead", "/thead", "tbody", "/tbody", "tr", "/tr", "th", "/th", "td", "/td", "br", "span", "/span"}
    for name, html in list(r.items()) + [("app", out["app"])]:
        tags = set(re.findall(r"<(/?[a-z0-9]+)", html))
        assert tags <= allowed, (name, html)
        for href in re.findall(r'href="([^"]*)"', html):
            assert href.startswith("https://"), (name, href)
        # no attribute other than the fixed ones, and no quote of the input closes one
        attrs = set(re.findall(r'\s([a-z-]+)=', "".join(re.findall(r"<[a-z][^>]*>", html))))
        assert attrs <= {"href", "target", "rel", "class"}, (name, html)
        assert all(re.fullmatch(r'<span class="hl-[a-z]+">', t) for t in re.findall(r"<span[^>]*>", html)), (name, html)
    assert '<span class="hl-tag">img</span>' in r["fenceHtml"] and "&lt;/code&gt;" in r["fenceHtml"]
    assert r["script"] == "<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>"
    for name in ("js", "jsCase", "jsSpace", "jsEntity", "jsTab", "jsCtrl", "data", "vb", "auto", "imgJs"):
        assert "<a " not in r[name], (name, r[name])   # shown as text
    assert r["js"] == "<p>[x](javascript:alert(1))</p>"
    assert 'href="https://a.b/&quot;onmouseover=&quot;alert(1)"' in r["quote"]
    assert 'href="https://a.b/&quot;%20onclick=&quot;alert(1)"' in r["quote2"]
    assert 'href="https://a.b/&quot;onmouseover=&quot;alert(1)"' in r["bareQuote"]
    assert "&lt;img src=x onerror=alert(1)&gt;" in r["codeHtml"] and "&lt;script&gt;" in r["codeHtml"]
    assert r["linkText"] == ('<p><a href="https://a.b" target="_blank" rel="noopener noreferrer">'
                             "&lt;img src=x onerror=alert(1)&gt;</a></p>")
    assert '<div class="md-lang">&quot;&gt;&lt;script&gt;alert(1)&lt;/script&gt;</div>' in r["lang"]


@needs_node
def test_markdown_pathological_inputs_render_fast():
    out = run_js(MD_PRE + r"""
      const patterns = ["*a **a ", "**a*", "_a ", "a*", "*", "[a](", "[", "![[", "](", "[](<", "`a``", "` `` ``` ",
                        "<a:", "<", "http://", "https://x.y/)", "~~a ", "\\*", "> ", "- ", "  - a\n", "| ", "|-", "# ",
                        "a\n", "\n", "    ", "&#", "((((", "[a](b \"", "*_*_",
                        // a "(" title, a rejected bare URL, math openers without a closer
                        "[a](x (", "[a](<x> (", "(http://\u0001", "*http://\u007f", "[](a()", "$a ", "$$a ",
                        "\\(a ", "\\[", "- a\n\n"];
      out.times = {};
      for (const p of patterns) {
        const s = p.repeat(Math.ceil(100000 / p.length));
        const t0 = Date.now();
        const html = M.render(s);
        out.times[p] = Date.now() - t0;
        if (typeof html !== "string") throw new Error(p);
      }
      const nested = "> ".repeat(5000) + "x\n" + "- ".repeat(5000) + "y\n" + Array.from({ length: 300 }, (_, k) => " ".repeat(k * 2) + "- z").join("\n");
      const t1 = Date.now(); M.render(nested); out.times.nested = Date.now() - t1;
    """)
    # each is ~100 KB and takes well under 50 ms; a quadratic scan takes about a second or more at this size
    slow = {p: ms for p, ms in out["times"].items() if ms > 400}
    assert not slow, slow


@needs_node
def test_markdown_call_sites():
    out = run_js(r"""
      app.S.lang = "en";
      app.S.state = { swarm: { goal: { title: "G", description: "**Ship** it", status: "done", summary: "- one\n- two" },
                               session: null, limits: {} }, agents: [{ name: "Ann Lee", role: "Dev", status: "idle" }],
                      mcp: [{ name: "csv", status: "active", origin: "workspace", scope: "swarm", description: "Reads `csv`", tools: [] }],
                      triggers: [{ id: 1, condition: "x", owner: "Ann Lee", once: true, note: "check **#3**" }] };
      out.msg = app.messageHtml({ sender: "Ann Lee", recipients: ["user"], text: "**done** see #3", ts: 0 });
      out.mine = app.messageHtml({ sender: "user", recipients: ["Ann Lee"], text: "- a\n- b", ts: 0 });
      out.text = app.activityHtml({ id: 1, ts: 0, data: { kind: "text", text: "# Plan\n- a" } });
      out.thinking = app.activityHtml({ id: 2, ts: 0, data: { kind: "thinking", text: "*hmm*" } });
      out.tool = app.activityHtml({ id: 3, ts: 0, data: { kind: "tool_call", tool: "shell", text: '{"cmd": "**x**"}' } });
      out.result = app.taskResultHtml({ result: "## Done\n`ok`" });
      out.thread = app.threadHtml([{ id: 1, author: "Ann Lee", text: "> quoted\n\n**LGTM**", reply_to: null, ts: 0 }]);
      out.field = app.mdFieldHtml("f-desc", "Description", "1. one\n2. two", 4);
      out.emptyField = app.mdFieldHtml("f-desc", "Description", "  ", 4);
      el("mcp-list"); el("trigger-list");
      app.renderMcp(); app.renderTriggers();
      out.mcp = elements["mcp-list"].innerHTML; out.triggers = elements["trigger-list"].innerHTML;
      // the goal dialog: the summary and the description are formatted, the description can still be edited
      el("modal"); el("modal-backdrop"); el("g-title"); el("g-desc"); el("g-save"); el("g-reopen-btn");
      app.goalModal();
      out.goal = elements.modal.innerHTML;
      // the task modal: the description is formatted, «Edit» opens its textarea; a live update refreshes the view
      const task = { id: 4, title: "T", goal: "G", description: "**old**", status: "todo", priority: 1, assignee: null,
                     reviewer: null, tags: [], depends_on: [], created_at: 0, updated_at: 0 };
      for (const id of Object.keys(app.TASK_FORM)) el(id, { focus: () => {} });
      el("f-desc-view"); el("task-form"); el("task-head"); el("task-meta"); el("task-result"); el("task-review");
      el("discussion-title"); el("thread"); el("task-history"); el("save-task"); el("post-btn"); el("post-text"); el("mention-menu");
      app.renderTaskModal({ task, discussion: [], events: [] });
      out.taskModal = elements.modal.innerHTML;
      out.descHidden = elements["f-desc"].classList.contains("hidden");
      out.descValue = elements["f-desc"].value;
      app.updateTaskModal({ task: Object.assign({}, task, { description: "*new*" }), discussion: [], events: [] });
      out.viewAfterUpdate = elements["f-desc-view"].innerHTML;
      app.editMdField("f-desc");
      out.editing = [elements["f-desc"].classList.contains("hidden"), elements["f-desc-view"].classList.contains("hidden")];
      elements["f-desc"].value = "*mine*";
      app.updateTaskModal({ task: Object.assign({}, task, { description: "*agent*" }), discussion: [], events: [] });
      out.keptEdit = [elements["f-desc"].value, elements["f-desc"].classList.contains("hidden")];
      app.updateTaskModal({ task: Object.assign({}, task, { description: "*mine*" }), discussion: [], events: [] }, true);
      out.afterSave = [elements["f-desc"].classList.contains("hidden"), elements["f-desc-view"].innerHTML];
      // the file viewer: a .md file formatted (or its source), any other file as text
      out.views = [app.fileView("docs/README.md", "# T", false), app.fileView("a.MARKDOWN", "*x*", false),
                   app.fileView("notes.md", "# T", true), app.fileView("main.py", "# comment", false)];
      el("file-content"); el("file-md"); el("file-mode"); el("file-path");
      app.S.openFile = "README.md"; app.S.fileText = "# Title\n\n<b>x</b>";
      app.renderFileContent();
      out.file = [elements["file-md"].innerHTML, elements["file-md"].classList.contains("hidden"),
                  elements["file-content"].classList.contains("hidden"), elements["file-mode"].textContent,
                  elements["file-mode"].classList.contains("hidden")];
      app.toggleFileSource();
      out.source = [elements["file-content"].innerHTML, elements["file-md"].classList.contains("hidden"),
                    elements["file-mode"].textContent];
      app.S.openFile = "main.py"; app.S.fileSource = false; app.S.fileText = "def f(): # <x>\n    return 1";
      app.renderFileContent();
      out.py = [elements["file-mode"].classList.contains("hidden"), elements["file-md"].classList.contains("hidden"),
                elements["file-content"].innerHTML, elements["file-content"].classList.contains("hidden")];
      app.S.openFile = "data.csv"; app.S.fileText = "a,<b>\n1,2"; app.renderFileContent();
      out.csv = elements["file-content"].innerHTML;
      // traces: the model's reply and thinking are formatted, tool arguments stay raw
      const tr = loadTracesJs();
      tr.TR.full = {};
      out.span = tr.spanDetailHtml({ id: 1, kind: "llm", name: "step", status: "done", usage: {}, meta: {},
                                     output: { thinking: "_think_", text: "**reply**", tool_calls: [{ name: "x", arguments: { a: "**raw**" } }] } });
    """)
    assert '<div class="md"><p><strong>done</strong> see <button type="button" class="link" data-open-task="3">#3</button></p></div>' in out["msg"]
    assert '<div class="msg me">' in out["mine"] and "<ul><li>a</li><li>b</li></ul>" in out["mine"]
    assert '<div class="md"><h1>Plan</h1><ul><li>a</li></ul></div>' in out["text"]
    assert "<em>hmm</em>" in out["thinking"]
    assert '<pre>{&quot;cmd&quot;: &quot;**x**&quot;}</pre>' in out["tool"] and 'class="md"' not in out["tool"]
    assert '<div class="result-box"><div class="md"><h2>Done</h2><p><code>ok</code></p></div></div>' in out["result"]
    assert '<div class="post-body"><div class="md"><blockquote><p>quoted</p></blockquote><p><strong>LGTM</strong></p></div></div>' in out["thread"]
    field = out["field"]
    assert '<div class="md-view" id="f-desc-view"><div class="md"><ol><li>one</li><li>two</li></ol></div></div>' in field
    assert '<textarea id="f-desc" rows="4" aria-label="Description" class="hidden"></textarea>' in field
    assert 'data-md-edit="f-desc"><i class="ic ic-edit"></i>Edit</button>' in field and 'class="link hidden"' not in field
    empty = out["emptyField"]   # nothing to read: the textarea at once
    assert '<textarea id="f-desc" rows="4" aria-label="Description"></textarea>' in empty and 'class="md-view hidden"' in empty
    assert '<div class="desc"><div class="md"><p>Reads <code>csv</code></p></div></div>' in out["mcp"]
    assert "<strong>" in out["triggers"] and 'data-open-task="3"' in out["triggers"]
    goal = out["goal"]
    assert '<div class="result-box"><div class="md"><ul><li>one</li><li>two</li></ul></div></div>' in goal
    assert '<div class="md"><p><strong>Ship</strong> it</p></div>' in goal and 'id="g-desc"' in goal
    assert '<div class="md"><p><strong>old</strong></p></div>' in out["taskModal"]
    assert out["descHidden"] is True and out["descValue"] == "**old**"   # the form still holds the text
    assert out["viewAfterUpdate"] == '<div class="md"><p><em>new</em></p></div>'
    assert out["editing"] == [False, True]
    assert out["keptEdit"] == ["*mine*", False]          # a live update never takes the textarea away
    assert out["afterSave"] == [True, '<div class="md"><p><em>mine</em></p></div>']
    views = out["views"]
    assert views[0] == {"markdown": True, "formatted": True, "html": '<div class="md"><h1>T</h1></div>'}
    assert views[1]["formatted"] is True and views[2] == {"markdown": True, "formatted": False, "html": ""}
    assert views[3] == {"markdown": False, "formatted": False, "html": ""}
    assert out["file"] == ['<div class="md"><h1>Title</h1><p>&lt;b&gt;x&lt;/b&gt;</p></div>', False, True, "Source", False]
    # the source of a .md file and any other text file: highlighted by the file's name (escaped, only spans)
    assert out["source"] == ['<span class="hl-section"># Title</span>\n\n&lt;b&gt;x&lt;/b&gt;', True, "Formatted"]
    assert out["py"] == [True, True, '<span class="hl-keyword">def</span> <span class="hl-title">f</span>(): '
                         '<span class="hl-comment"># &lt;x&gt;</span>\n    <span class="hl-keyword">return</span> '
                         '<span class="hl-number">1</span>', False]
    assert out["csv"] == "a,&lt;b&gt;\n1,2"   # a kind without highlighting: plain escaped text
    span = out["span"]
    assert '<div class="tr-md thinking"><div class="md"><p><em>think</em></p></div></div>' in span
    assert '<div class="tr-md "><div class="md"><p><strong>reply</strong></p></div></div>' in span
    assert "**raw**" in span


@needs_node
def test_modal_focus_skips_the_task_marks_of_a_formatted_text():
    # a finished goal's summary with a checklist comes before the reopen comment: its disabled checkbox takes no focus
    out = run_js(r"""
      el("modal"); el("modal-backdrop");
      out.focused = null;
      const mark = { focus() { out.focused = "mark"; } }, field = { focus() { out.focused = "field"; } };
      const html = app.md("- [x] done");
      // a stand-in for the browser's selector matching: the task mark is a disabled <input>
      const byId = document.querySelector;
      document.querySelector = (sel) => (!sel.includes(" input") ? byId(sel)
        : sel.split(",").every((s) => s.includes(":not([disabled])")) ? field : mark);
      app.openModal(html);
      out.html = html;
    """)
    assert '<input type="checkbox" disabled checked>' in out["html"]
    assert out["focused"] == "field"


def test_markdown_css_keeps_plain_text_layout():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    # paragraph lines keep their indentation and runs of spaces, as the pre-wrap plain text did
    assert re.search(r"\.md p, \.md li, \.md td, \.md th \{ white-space: pre-wrap; \}", css)
    # the hire dialog's role preview is a grid item: without min-width 0 a wide code block widens the modal
    assert re.search(r"\.role-preview \{[^}]*min-width: 0;", css)


def test_markdown_is_loaded_before_the_scripts_that_use_it():
    assert INDEX.index("/static/markdown.js") < INDEX.index("/static/app.js") < INDEX.index("/static/traces.js")
    md_src = (STATIC / "markdown.js").read_text(encoding="utf-8")
    assert "innerHTML" not in md_src and "eval(" not in md_src and "new Function" not in md_src
    # highlight.js before both (markdown.js highlights fences with it, app.js the file viewer)
    assert INDEX.index("/static/highlight.js") < INDEX.index("/static/markdown.js")
    hl_src = (STATIC / "highlight.js").read_text(encoding="utf-8")
    assert "innerHTML" not in hl_src and "eval(" not in hl_src and "new Function" not in hl_src


# ------------------------------------------------------------------------------------------------ syntax highlighting
HL_PRE = r"""
const H = require(staticDir + "/highlight.js");
const unesc = (h) => h.replace(/<span class="hl-[a-z]+">|<\/span>/g, "").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
  .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");
const onlySpans = (h) => (h.match(/<[^>]*>/g) || []).every((t) => /^<span class="hl-[a-z]+">$|^<\/span>$/.test(t));
"""

# a sample per language: the text content of the result must be the sample itself
HL_SAMPLES = {
    "python": '@app.route("/x")\ndef handler(req, n=0x1F):\n    """Doc\n    string"""\n    s = rb\'raw\' + f"{n}"  # note\n'
              "    return self.data / 2 if n else None\n",
    "js": "const a = `x${b + `y${c}`}z` / 2;\nlet r = /ab+c/g;\nconst q = a / b / c; // div\nfunction run() { return 1e3; }\n",
    "ts": "interface P { name: string }\nexport type Id = number;\n",
    "json": '{"name": "dt", "n": [1, -2.5e3, true, null], "nested": {"k": "v"}}\n',
    "toml": '# c\n[tool.pytest]\nname = "v"\nlist = [\n  [1, 2],\n]\na.b = true\n',
    "yaml": 'key: value\nlist:\n  - name: "x"  # c\n    n: 12\n    ok: true\nscript: |\n  a: b\nanchor: &a 1\n',
    "ini": "; c\n[section]\nkey = value\nexport TOKEN=\"x\"\n",
    "bash": '#!/bin/sh\n# c\necho "a # $HOME ${X:-y}" $1 # tail\nFOO=bar\nf() { local v; }\n',
    "dockerfile": "FROM python:3.12\nRUN pip install x && echo $HOME\n",
    "make": "CC := gcc\nall: main.o\n\t$(CC) -o $@ $^\n",
    "powershell": '# c\n$x = Get-Item -Path "a$env:PATH" -eq $true\n@\'\nhere $x\n\'@\n[int]$n = 5\n<# block #>\n',
    "batch": "@echo off\nREM c\n:label\nset X=%1 & echo !Y!\n",
    "markup": '<!DOCTYPE html><a href="x" id=y>t &amp; <script>let x = 1 < 2;</script></a><!-- c -->\n',
    "css": "a:hover, .c #d { color: #fff; margin: .5em 1px !important; }\n@media (max-width: 6px) { p { x: var(--y) } }\n",
    "scss": "// Don't touch\n.a { b: url(http://x/y); margin: 0 // tail\n}\n",
    "sql": "SELECT name, COUNT(*) FROM t WHERE x = 'it''s' -- c\n",
    "c": '#include <stdio.h>\nstruct point { int x; };\nint main(void) { printf("%d\\n", \'a\'); return 0; }\n',
    "cpp": "namespace n { class A : public B { auto x = nullptr; }; }\n",
    "csharp": 'public class A { string s = @"a""b"; var t = $"{x}"; }\n',
    "java": "@Override\npublic final class A { String s = \"x\"; char c = 'y'; }\n",
    "kotlin": 'fun main() { val s = """raw"""; println("$s") }\n',
    "go": "package main\nfunc main() { s := `raw\nline`; r := 'x'; fmt.Println(s, r, nil) }\n",
    "rust": "#[derive(Debug)]\nfn f<'a>(x: &'a str) -> Option<u8> { let c = 'x'; println!(\"{}\", r#\"raw\"#); None }\n",
    "ruby": "def f(x)\n  @y = :sym # c\n  puts \"#{x}\"\nend\n",
    "php": "<?php\nfunction f($x) { return $x . 'a'; } // c\n",
    "lua": "--[[ block ]]\nlocal function f(x) return [[long]] .. 'a' end -- c\n",
    "r": "x <- c(1, 2L, NA) %in% y # c\nf <- function(a) TRUE\n",
    "julia": "#= block =#\nfunction f(x::Int)\n  @show x\n  return \"s\"\nend\n",
    "latex": "\\begin{document}\n% c\nText $x^2$ \\textbf{b}\n\\end{document}\n",
    "markdown": "# Title\n- item **b** and *e* `code` [l](https://x.y)\n```python\nx = 1\n```\n> quote\n",
    "diff": "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n ctx\n",
}


@needs_node
def test_highlight_language_detection():
    names = ["src/app.py", "x.PYI", "a/b.tsx", "c.mjs", "nb.ipynb", "data.jsonl", "pyproject.toml", "ci.yml", "setup.cfg",
             ".env", ".env.local", "run.sh", "Dockerfile", "Dockerfile.dev", "Makefile", "x.mk", "build.ps1", "go.bat",
             "page.HTM", "icon.svg", "a.css", "a.scss", "b.less", "q.sql", "x.h", "x.hpp", "x.cs", "A.java", "a.kt", "m.go", "lib.rs",
             "a.rb", "a.php", "a.lua", "a.R", "a.jl", "paper.tex", "README.md", "fix.patch", "table.csv", "notes.txt",
             "LICENSE", "", None, "a.unknownext",
             # fence info strings
             "python", "py", "JS", "typescript", "yml", "console", "pwsh", "c++", "c#", "golang", "rs", "diff", "text"]
    out = run_js(HL_PRE + f"out.langs = {json.dumps(names)}.map(H.language); out.all = H.languages;")
    langs = dict(zip([str(n) for n in names], out["langs"]))
    assert langs == {
        "src/app.py": "python", "x.PYI": "python", "a/b.tsx": "ts", "c.mjs": "js", "nb.ipynb": "json",
        "data.jsonl": "json", "pyproject.toml": "toml", "ci.yml": "yaml", "setup.cfg": "ini", ".env": "ini",
        ".env.local": "ini", "run.sh": "bash", "Dockerfile": "dockerfile", "Dockerfile.dev": "dockerfile",
        "Makefile": "make", "x.mk": "make", "build.ps1": "powershell", "go.bat": "batch", "page.HTM": "markup",
        "icon.svg": "markup", "a.css": "css", "a.scss": "scss", "b.less": "scss", "q.sql": "sql", "x.h": "c", "x.hpp": "cpp", "x.cs": "csharp",
        "A.java": "java", "a.kt": "kotlin", "m.go": "go", "lib.rs": "rust", "a.rb": "ruby", "a.php": "php",
        "a.lua": "lua", "a.R": "r", "a.jl": "julia", "paper.tex": "latex", "README.md": "markdown", "fix.patch": "diff",
        "table.csv": None, "notes.txt": None, "LICENSE": None, "": None, "None": None, "a.unknownext": None,
        "python": "python", "py": "python", "JS": "js", "typescript": "ts", "yml": "yaml", "console": "bash",
        "pwsh": "powershell", "c++": "cpp", "c#": "csharp", "golang": "go", "rs": "rust", "diff": "diff", "text": None,
    }
    assert set(out["all"]) == set(HL_SAMPLES)


@needs_node
def test_highlight_marks_the_key_constructs_of_each_language():
    out = run_js(HL_PRE + f"const S = {json.dumps(HL_SAMPLES)};\n"
                 "out.h = Object.fromEntries(Object.entries(S).map(([l, s]) => [l, H.render(s, l)]));\n"
                 "out.ok = Object.fromEntries(Object.entries(S).map(([l, s]) => [l, unesc(out.h[l]) === s && onlySpans(out.h[l])]));")
    assert all(out["ok"].values()), out["ok"]
    h = out["h"]

    def has(lang: str, *spans: tuple[str, str]) -> None:
        for cls, text in spans:
            assert f'<span class="hl-{cls}">{text}</span>' in h[lang], (lang, cls, text, h[lang])

    has("python", ("meta", "@app.route"), ("keyword", "def"), ("title", "handler"), ("number", "0x1F"),
        ("string", "&quot;&quot;&quot;Doc\n    string&quot;&quot;&quot;"), ("string", "rb&#39;raw&#39;"),
        ("string", "f&quot;{n}&quot;"), ("comment", "# note"), ("literal", "self"), ("literal", "None"))
    assert "self</span>.data / <span" in h["python"]   # a division, a member name stays plain
    # a template literal with nested ${…}; a regex where an operand is expected, a division after a name
    has("js", ("string", "`x"), ("string", "`y"), ("string", "z`"), ("regexp", "/ab+c/g"), ("comment", "// div"),
        ("title", "run"), ("number", "1e3"))
    assert "q = a / b / c;" in h["js"] and h["js"].count("hl-regexp") == 1
    has("ts", ("keyword", "interface"), ("title", "P"), ("type", "string"), ("keyword", "type"), ("type", "number"))
    # JSON: keys and string values apart
    has("json", ("key", "&quot;name&quot;"), ("string", "&quot;dt&quot;"), ("number", "2.5e3"), ("literal", "true"),
        ("literal", "null"), ("key", "&quot;k&quot;"), ("string", "&quot;v&quot;"))
    # TOML: a section, keys, but not a line of a multi-line array
    has("toml", ("comment", "# c"), ("section", "[tool.pytest]"), ("key", "name"), ("key", "a"), ("key", "b"))
    assert "hl-section\">[1" not in h["toml"]
    # YAML: keys after indentation and "- ", comments, the text of a block scalar
    has("yaml", ("key", "key"), ("key", "name"), ("key", "n"), ("number", "12"), ("literal", "true"),
        ("comment", "# c"), ("string", "a: b"), ("variable", "&amp;a"))
    has("ini", ("comment", "; c"), ("section", "[section]"), ("key", "key"), ("keyword", "export"), ("key", "TOKEN"))
    # shell: a comment vs "#" inside a string, variables inside and outside strings
    has("bash", ("meta", "#!/bin/sh"), ("comment", "# c"), ("string", "&quot;a # "), ("variable", "$HOME"),
        ("variable", "${X:-y}"), ("variable", "$1"), ("comment", "# tail"), ("variable", "FOO"), ("title", "f"))
    has("dockerfile", ("keyword", "FROM"), ("keyword", "RUN"), ("variable", "$HOME"))
    has("make", ("variable", "CC"), ("title", "all"), ("variable", "$(CC)"), ("variable", "$@"))
    has("powershell", ("comment", "# c"), ("variable", "$x"), ("builtin", "Get-Item"), ("attr", "-Path"),
        ("variable", "$env:PATH"), ("keyword", "-eq"), ("literal", "$true"), ("string", "@&#39;\nhere $x\n&#39;@"),
        ("type", "[int]"), ("comment", "&lt;# block #&gt;"))
    has("batch", ("comment", "REM c"), ("title", ":label"), ("variable", "%1"), ("variable", "!Y!"))
    # HTML: tags, attributes, values, entities, comments, and the script inside highlighted as JavaScript
    has("markup", ("meta", "&lt;!DOCTYPE html&gt;"), ("tag", "a"), ("attr", "href"), ("string", "&quot;x&quot;"),
        ("string", "y"), ("literal", "&amp;amp;"), ("keyword", "let"), ("comment", "&lt;!-- c --&gt;"))
    has("css", ("tag", "a"), ("meta", ":hover"), ("title", ".c"), ("title", "#d"), ("attr", "color"),
        ("number", "#fff"), ("number", ".5em"), ("keyword", "!important"), ("keyword", "@media"), ("builtin", "var"))
    # SCSS / LESS: // comments (an apostrophe in one is text), but not the // of a url
    has("scss", ("comment", "// Don&#39;t touch"), ("title", ".a"), ("builtin", "url"), ("comment", "// tail"))
    assert "(http://x/y)" in h["scss"]
    has("sql", ("keyword", "SELECT"), ("builtin", "COUNT"), ("string", "&#39;it&#39;&#39;s&#39;"), ("comment", "-- c"))
    has("c", ("meta", "#include &lt;stdio.h&gt;"), ("keyword", "struct"), ("title", "point"), ("type", "int"),
        ("string", "&#39;a&#39;"), ("builtin", "printf"))
    has("cpp", ("keyword", "namespace"), ("title", "n"), ("keyword", "class"), ("title", "A"), ("literal", "nullptr"))
    has("csharp", ("keyword", "class"), ("string", "@&quot;a&quot;&quot;b&quot;"), ("string", "$&quot;{x}&quot;"))
    has("java", ("meta", "@Override"), ("keyword", "class"), ("title", "A"), ("type", "String"), ("string", "&#39;y&#39;"))
    has("kotlin", ("keyword", "fun"), ("title", "main"), ("string", "&quot;&quot;&quot;raw&quot;&quot;&quot;"))
    has("go", ("keyword", "package"), ("keyword", "func"), ("string", "`raw\nline`"), ("string", "&#39;x&#39;"),
        ("literal", "nil"))
    has("rust", ("meta", "#[derive(Debug)]"), ("keyword", "fn"), ("title", "f"), ("meta", "&#39;a"),
        ("string", "&#39;x&#39;"), ("builtin", "println"), ("string", "r#&quot;raw&quot;#"), ("builtin", "None"))
    has("ruby", ("keyword", "def"), ("title", "f"), ("variable", "@y"), ("literal", ":sym"), ("comment", "# c"))
    has("php", ("meta", "&lt;?php"), ("keyword", "function"), ("variable", "$x"), ("comment", "// c"))
    has("lua", ("comment", "--[[ block ]]"), ("keyword", "local"), ("string", "[[long]]"), ("comment", "-- c"))
    has("r", ("number", "2L"), ("literal", "NA"), ("keyword", "%in%"), ("comment", "# c"), ("keyword", "function"))
    has("julia", ("comment", "#= block =#"), ("title", "f"), ("type", "Int"), ("meta", "@show"))
    has("latex", ("keyword", "\\begin"), ("title", "document"), ("comment", "% c"), ("string", "$x^2$"),
        ("keyword", "\\textbf"))
    # the source of a .md file: headings, markers, emphasis, code, links; a fence's code in its own language
    has("markdown", ("section", "# Title"), ("keyword", "-"), ("strong", "**b**"), ("em", "*e*"),
        ("string", "`code`"), ("attr", "https://x.y"), ("meta", "```python"), ("number", "1"), ("keyword", "&gt;"))
    has("diff", ("meta", "diff --git a/x b/x"), ("meta", "--- a/x"), ("meta", "+++ b/x"), ("hunk", "@@ -1 +1 @@"),
        ("del", "-old"), ("add", "+new"))
    assert "\n ctx\n" in h["diff"]


@needs_node
def test_highlight_is_safe_and_keeps_the_text():
    out = run_js(HL_PRE + r"""
      const evil = '<script>alert("x")</script> & &amp; \'q\' "dq" <img src=x onerror=alert(1)> \u00a0 </span><span class="x">';
      out.evil = {};
      for (const l of H.languages) {
        const h = H.render(evil, l);
        out.evil[l] = unesc(h) === evil && onlySpans(h) && !/<(?!\/?span)/.test(h) && !h.includes("\u0000");
      }
      out.unknown = [H.render(evil, null), H.render(evil, "nope"), H.render(evil, "constructor"), H.render(evil, "__proto__")];
      // random texts of the characters that matter to the tokenizers: the text is always kept, only spans appear
      let seed = 7;
      const rnd = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
      const chars = [..."abXY_09 \t\n\r\"'`$#@{}[]()<>/\\*-+=:;,.!?%&|~^\u00e9\u{1F600}"];
      out.fuzz = [];
      for (const l of H.languages) for (let k = 0; k < 150; k++) {
        let s = "";
        const len = Math.floor(rnd() * 160);
        for (let j = 0; j < len; j++) s += chars[Math.floor(rnd() * chars.length)];
        const h = H.render(s, l);
        if (unesc(h) !== s || !onlySpans(h)) out.fuzz.push([l, s, h]);
      }
      // above the size cap: plain escaped text
      const big = "x = 1 # <c>\n".repeat(Math.ceil((H.MAX_CHARS + 1) / 12));
      out.big = H.render(big, "python") === big.replace(/</g, "&lt;").replace(/>/g, "&gt;");
      out.cap = H.MAX_CHARS;
    """)
    assert all(out["evil"].values()), out["evil"]
    plain = ("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &amp;amp; &#39;q&#39; &quot;dq&quot; "
             "&lt;img src=x onerror=alert(1)&gt; \u00a0 &lt;/span&gt;&lt;span class=&quot;x&quot;&gt;")
    assert out["unknown"] == [plain] * 4
    assert out["fuzz"] == []
    assert out["big"] is True and out["cap"] >= 100_000


@needs_node
def test_highlight_pathological_inputs_stay_linear():
    out = run_js(HL_PRE + r"""
      const patterns = ['"', "'", "`", "/*", "#", "${", "$", "$'", "<", "<a ", "<!--", "[", "](", "*", "**", "_", "`a",
                        "@", "@'\n", "-", "--[[", "[[", "%", "\\", "{", "}", "#[", 'r#"', "a: ", "- ", "/[", "(/[",
                        "x = ", "'''", '\"\"\"', "<#", "&", "!", ":", ".", "1", "a.b", "*a ", "$x{", "a\n", " ", "```\n",
                        "<<a\n", "<<a\na\n", "<<-'x' <<y\n\tx\ny\n", 'R"(', 'R"d(x)"', "<?", "?>", "<?php '\n?>'",
                        "- k: |\n", "@@ -1,3 +1,3 @@\n", "--- a\n", "\\😀", "x//"];
      out.slow = {};
      for (const l of H.languages) for (const p of patterns) {
        const s = p.repeat(Math.ceil(40000 / p.length));
        const t0 = Date.now();
        const h = H.render(s, l);
        const ms = Date.now() - t0;
        if (ms > 250 || h.length < s.length) out.slow[l + " " + JSON.stringify(p)] = ms;   // (the text: the fuzz test)
      }
      const line = "q".repeat(1000000) + '"' + "w".repeat(1000);   // one 1 MB line: over the cap, plain
      const t1 = Date.now(); H.render(line, "js"); out.longLine = Date.now() - t1;
    """)
    # each input is ~40 KB and takes a few ms; a quadratic scan takes seconds at this size
    assert out["slow"] == {}, out["slow"]
    assert out["longLine"] < 300


# constructs where one stray quote used to colour the rest of a file (or split a character)
HL_EDGE = {
    "sh": ("bash", "cat <<-'EOF' | grep a # c\n\tIt's a heredoc\n\tEOF\necho \"$HOME\" # after\nx=$((1 << n)) 'ok'\n"
                   "cat <<A <<B\na's\nA\nb's\nB\nexit 0\n"),
    "yaml": ("yaml", "steps:\n  - run: |\n      echo a\n    env:\n      X: 1\n  - name: next\n"),
    "jsx": ("js", "const el = <li><p>a</p><p>b</p></li>;\nconst t = <p>Don't stop</p>;\nlet n = 1; // c\n"),
    "ruby": ("ruby", "x.gsub(/'/, \"\")  # strip quotes\nmsg = <<~EOS\n  Don't forget\n  EOS\n@count += 1 # c\n"
                     "y = a / b / 2\n"),
    "php": ("php", "<html>\n<p>Don't worry</p>\n<?php echo $x; ?>\n<p>ok</p>\n"),
    "phpSnippet": ("php", "$x = 'a'; // c\n"),
    "cpp": ("cpp", "auto a = R\"(raw \"q\" /* x)\";\nauto s = u8R\"d(\nmulti \"line\"\n)d\";\nint x; // c\n"),
    "diff": ("diff", "--- a/x.sql\n+++ b/x.sql\n@@ -1,2 +1,2 @@\n--- a removed sql comment\n+++ an added line\n ctx\n"
                     "--- a/y\n+++ b/y\n@@ -1 +1 @@\n-old\n+new\n"),
}


@needs_node
def test_highlight_keeps_the_rest_of_the_file_after_tricky_constructs():
    out = run_js(HL_PRE + f"const E = {json.dumps(HL_EDGE)};\n" + r"""
      out.h = {};
      for (const [k, [l, s]] of Object.entries(E)) {
        out.h[k] = H.render(s, l);
        if (unesc(out.h[k]) !== s || !onlySpans(out.h[k])) out.h[k] = "BROKEN " + out.h[k];
      }
      // a backslash before an emoji (LaTeX): the span never ends between the two halves of the character
      const emoji = String.fromCodePoint(0x1F600);
      out.pairs = H.languages.filter((l) => ["\\", "%", "'", "$", "!", "@", "#", "`", '"'].some((p) => {
        const h = H.render("a " + p + emoji + " " + p + p + emoji, l);
        return /[\uD800-\uDBFF]</.test(h) || />[\uDC00-\uDFFF]/.test(h);
      }));
      out.latex = H.render("\\" + emoji + " x", "latex");
    """)
    h = out["h"]
    for k, v in h.items():
        assert not v.startswith("BROKEN"), (k, v)

    def has(key: str, *spans: tuple[str, str]) -> None:
        for cls, text in spans:
            assert f'<span class="hl-{cls}">{text}</span>' in h[key], (key, cls, text, h[key])

    # a heredoc body is one string; the code after its delimiter line is highlighted again
    has("sh", ("string", "&lt;&lt;-&#39;EOF&#39;"), ("comment", "# c"),
        ("string", "\tIt&#39;s a heredoc\n\tEOF"), ("variable", "$HOME"), ("comment", "# after"),
        ("string", "&#39;ok&#39;"), ("string", "a&#39;s\nA\nb&#39;s\nB"), ("keyword", "exit"))
    assert "1 &lt;&lt; n" in h["sh"]   # a shift is not a heredoc (no line "n" follows)
    # "- run: |": the item's next keys are keys, not text of the block scalar
    has("yaml", ("string", "echo a"), ("key", "env"), ("key", "X"), ("number", "1"), ("key", "name"))
    # JSX: "</p>" is not a regex, an apostrophe in its text not a string
    assert "hl-regexp" not in h["jsx"] and "Don&#39;t stop" in h["jsx"]
    has("jsx", ("keyword", "let"), ("number", "1"), ("comment", "// c"))
    # Ruby: a regex literal and a heredoc keep their quotes to themselves
    has("ruby", ("regexp", "/&#39;/"), ("comment", "# strip quotes"), ("string", "&lt;&lt;~EOS"),
        ("string", "  Don&#39;t forget\n  EOS"), ("variable", "@count"), ("comment", "# c"))
    assert "a / b / <span" in h["ruby"]   # a division stays one
    # PHP: the HTML around <?php … ?> is markup; a snippet without <?php is all PHP
    has("php", ("tag", "p"), ("meta", "&lt;?php"), ("keyword", "echo"), ("variable", "$x"), ("meta", "?&gt;"))
    assert "Don&#39;t worry" in h["php"] and "hl-string" not in h["php"]
    has("phpSnippet", ("variable", "$x"), ("string", "&#39;a&#39;"), ("comment", "// c"))
    # C++ raw strings: quotes and /* inside are text
    has("cpp", ("string", "R&quot;(raw &quot;q&quot; /* x)&quot;"), ("string", "u8R&quot;d(\nmulti &quot;line&quot;\n)d&quot;"),
        ("type", "int"), ("comment", "// c"))
    # diff: inside a counted hunk "--- x" / "+++ x" are a removed / an added line; the next file's headers are headers
    has("diff", ("meta", "--- a/x.sql"), ("hunk", "@@ -1,2 +1,2 @@"), ("del", "--- a removed sql comment"),
        ("add", "+++ an added line"), ("meta", "--- a/y"), ("meta", "+++ b/y"), ("del", "-old"), ("add", "+new"))
    assert out["pairs"] == []
    assert out["latex"] == '<span class="hl-keyword">\\\U0001F600</span> x'


def test_syntax_colours_are_readable_on_every_code_background():
    """WCAG 4.5:1 for each --hl-* colour on --code-bg over the surfaces code is shown on: the file viewer (--panel),
    messages and posts (--panel-2) and one's own messages (--accent-soft over --panel), in both themes."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    def tokens(block: str) -> dict[str, str]:
        return dict(re.findall(r"--([\w-]+):\s*([^;]+);", block))

    light = tokens(css[css.index(":root {"):css.index('}', css.index(":root {"))])
    dark = tokens(css[css.index(':root[data-theme="dark"] {'):css.index('}', css.index(':root[data-theme="dark"] {'))])
    media = css[css.index("@media (prefers-color-scheme: dark)"):]
    auto_dark = tokens(media[:media.index("}")])
    assert {k: v for k, v in auto_dark.items() if k.startswith("hl-")} == {k: v for k, v in dark.items() if k.startswith("hl-")}

    def rgba(v: str) -> tuple[list[float], float]:
        v = v.strip()
        if v.startswith("#"):
            return [int(v[k:k + 2], 16) for k in (1, 3, 5)], 1.0
        *rgb, a = [float(x) for x in re.findall(r"[\d.]+", v)]
        return rgb, a

    def over(fg: str, bg: list[float]) -> list[float]:
        rgb, a = rgba(fg)
        return [x * a + y * (1 - a) for x, y in zip(rgb, bg)]

    def lum(rgb: list[float]) -> float:
        f = [(x / 255) / 12.92 if x / 255 <= .03928 else ((x / 255 + .055) / 1.055) ** 2.4 for x in rgb]
        return .2126 * f[0] + .7152 * f[1] + .0722 * f[2]

    low = []
    for name, t in (("light", light), ("dark", dark)):
        panel = rgba(t["panel"])[0]
        surfaces = {"panel": panel, "panel-2": rgba(t["panel-2"])[0], "me": over(t["accent-soft"], panel)}
        for sname, base in surfaces.items():
            bg = over(t["code-bg"], base)
            for k in ("comment", "keyword", "string", "number", "title", "type", "tag"):
                a, b = lum(rgba(t["hl-" + k])[0]), lum(bg)
                ratio = (max(a, b) + .05) / (min(a, b) + .05)
                if ratio < 4.5:
                    low.append((name, sname, k, round(ratio, 2)))
    assert low == []


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

        # the Skills tab: a library skill attached (imported from a folder first); the keys the cards read
        from conftest import make_skill
        make_skill(tmp_path / "skill-src" / "pdf", "pdf", "Reads PDFs.", {"run.py": "print(1)\n"})
        await act(base, "skill_attach", source=str(tmp_path / "skill-src" / "pdf"))
        skill = (await get(base, "/api/state"))["skills"][0]
        assert {"name", "description", "from", "owner", "library", "comment", "attached_via", "files", "size", "digest",
                "manual", "path", "roles", "deps"} <= set(skill)
        assert skill["from"] == "library" and skill["files"] == 2 and isinstance(skill["deps"], list)
        listing = await get(base, "/api/skills")
        assert {"library_dir", "library", "claude", "host", "sandbox", "limits", "install", "attached"} <= set(listing)
        assert listing["attached"][0]["source_digest"] == skill["digest"]   # "Update" compares with it

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
            for asset in ("home.js", "home.css", "style.css", "highlight.js", "markdown.js", "app.js", "traces.js", "traces.css"):
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


# ------------------------------------------------------------------------------------------------ skills (§18.4)
SKILLS_JS = (STATIC / "skills.js").read_text(encoding="utf-8")


def test_skills_texts_exist_on_both_pages_and_the_settings_cover_the_config():
    """skills.js takes each page's t(): every key it uses is in both pages' dictionaries, in both languages; the home
    settings' «Скиллы» section names every [skills] setting."""
    from dreamteam.config import SkillsSettings

    used = used_keys(SKILLS_JS)
    assert {"sk_pkgs_preview", "sk_pkgs_trust", "sk_replace_q", "sk_add_attach", "sk_zip_big"} <= used
    for name, blocks in (("home", home_i18n()), ("board", i18n_blocks())):
        for lang in ("ru", "en"):
            missing = sorted(used - blocks[lang])
            assert not missing, (name, lang, missing)
    fields = set(re.findall(r'field\("sk_f_\w+", "skills\.(\w+)"', HOME_JS))
    assert fields == {f.name for f in dataclasses.fields(SkillsSettings)}
    # the shared script comes before the page scripts; the board's skills tab and the home tab exist
    assert HOME_HTML.index("/static/skills.js") < HOME_HTML.index("/static/home.js")
    assert INDEX.index("/static/markdown.js") < INDEX.index("/static/skills.js") < INDEX.index("/static/app.js")
    assert 'data-tab="skills"' in INDEX and 'id="pane-skills"' in INDEX and 'id="pane-skills"' in HOME_HTML
    assert ".ic-skill" in (STATIC / "icons.css").read_text(encoding="utf-8")


@needs_node
def test_skills_js_import_flow_preview_and_base64():
    out = run_js(r"""
      const SK = require(staticDir + "/skills.js");
      const T = { sk_pkgs_preview: "P:{packages}({files})", sk_pkgs_trust: "TRUST", sk_pkgs_off: "OFF",
                  sk_pkgs_unsupported: "U:{files}:{reason}", sk_imported: "I:{names}", sk_attached: "A:{name}",
                  sk_attached_same: "S:{name}", sk_skipped: "K:{path}:{error}" };
      const t = (k, v) => (T[k] || k).replace(/\{(\w+)\}/g, (m, n) => (v && n in v ? v[n] : m));
      out.b64 = [SK.toBase64(new Uint8Array([0, 1, 2, 250, 255])), SK.toBase64(new Uint8Array(70000).fill(65)).length];
      const deps = { python: { files: ["requirements.txt"], packages: ["a", "b", "c", "d", "e", "f", "g", "h", "i"] },
                     node: { files: ["package.json"], unsupported: "a git dependency" } };
      out.preview = [SK.previewLine(t, deps, { enabled: true, proposals: false }),
                     SK.previewLine(t, deps, { enabled: true, proposals: false }, true),
                     SK.previewLine(t, { node: deps.node }, null), SK.previewLine(t, {}, null), SK.fmtSize(2048)];
      // a source with several skills asks which; a clash asks before replacing; "no" cancels
      const posted = [];
      const answers = [
        { status: 200, data: { need_pick: true, candidates: [{ name: "pdf" }, { name: "csv" }], imported: [] } },
        { status: 409, data: { error: "clash", exists: ["pdf"] } },
        { status: 200, data: { imported: [{ name: "pdf" }], attached: [{ name: "pdf", status: "attached" }],
                               failed: [{ name: "csv", error: "cap" }], skipped: [{ path: "x.bin", error: "too big" }] } },
      ];
      const post = async (body) => { posted.push(body); return answers.shift(); };
      const asked = [];
      const res = await SK.runImport(post, { git: "https://g/r" }, {
        pick: async (c) => { asked.push(["pick", c.map((x) => x.name)]); return ["pdf"]; },
        replace: async (names) => { asked.push(["replace", names]); return true; } });
      out.flow = [posted, asked, res.ok, SK.reportLines(t, res.data)];
      const cancelled = await SK.runImport(async () => ({ status: 409, data: { exists: ["x"] } }), { path: "/p" },
                                           { replace: async () => false, pick: async () => null });
      const failed = await SK.runImport(async () => ({ status: 400, data: { error: "bad zip" } }), { zip: "", filename: "a.zip" },
                                        { replace: async () => true, pick: async () => null });
      out.ends = [cancelled, failed.ok, failed.error];
      out.form = SK.formHtml(t, (s) => String(s), { attach: true });
      out.sandboxForm = SK.formHtml(t, (s) => String(s), { sandbox: true });
      // the hint of a form mounted before the listing said `sandbox` is shown afterwards
      const hint = { on: false, classList: { toggle: (c, hidden) => { hint.on = !hidden; } } };
      SK.setSandbox({ querySelectorAll: (sel) => (sel === "[data-sk-sandbox]" ? [hint] : []) }, true);
      out.hintOn = hint.on;
      SK.setSandbox(null, true);   // no root: nothing to do
    """)
    assert out["b64"] == ["AAEC+v8=", 93336]                        # chunked: a large buffer does not overflow
    p, proposal, unsupported, nothing, size = out["preview"]
    assert p == "P:a, b, c, d, e, f, g, h, …(requirements.txt) U:package.json:a git dependency TRUST"
    assert proposal == "OFF"                                         # install_proposals off: a member's skill
    assert unsupported == "U:package.json:a git dependency" and nothing == "" and size == "2.0 KB"
    posted, asked, ok, lines = out["flow"]
    assert posted == [{"git": "https://g/r"}, {"git": "https://g/r", "pick": ["pdf"]},
                      {"git": "https://g/r", "pick": ["pdf"], "replace": True}]
    assert asked == [["pick", ["pdf", "csv"]], ["replace", ["pdf"]]] and ok is True
    assert lines == [{"text": "I:pdf"}, {"text": "A:pdf"}, {"text": "csv: cap", "warn": True},
                     {"text": "K:x.bin:too big", "warn": True}]
    assert out["ends"] == [{"cancelled": True}, False, "bad zip"]
    assert 'data-sk-src="folder"' in out["form"] and 'data-sk-in="zip"' in out["form"] and "sk_add_attach" in out["form"]
    # in the Docker sandbox a folder path is the container's: the hint says so (hidden otherwise)
    assert 'class="field-hint hidden" data-sk-sandbox>sk_folder_sandbox' in out["form"]
    assert 'class="field-hint" data-sk-sandbox>sk_folder_sandbox' in out["sandboxForm"] and out["hintOn"] is True


@needs_node
def test_skills_js_enter_imports_and_never_submits_the_enclosing_form():
    # regression: the add form sits inside the new-session <form>; Enter in its folder or Git field submitted the
    # whole form, so the session was created without the skill being added
    out = run_js(r"""
      const SK = require(staticDir + "/skills.js");
      const handlers = {};
      const field = (props) => Object.assign({ tagName: "INPUT", type: "text", value: "", dataset: {} }, props);
      const folder = field({ dataset: { skIn: "folder" }, value: " /x/pdf " });
      const go = { dataset: { skGo: "" }, disabled: false, click: () => handlers.click({ target: { closest: () => go } }) };
      const nodes = { "[data-sk-status]": { textContent: "", classList: classList() }, "[data-sk-pick]": { classList: classList() },
                      "[data-sk-go]": go, '[data-sk-in="folder"]': folder };
      const box = { classList: classList(), contains: () => true, querySelector: (s) => nodes[s] || null,
                    querySelectorAll: () => [], addEventListener: (type, fn) => { handlers[type] = fn; } };
      const posted = [];
      SK.mount(box, { t: (k) => k, esc: String, attach: true,
                      post: async (body) => { posted.push(body); return { status: 200, data: { imported: [{ name: "pdf" }] } }; } });
      const press = (target, extra) => { const e = Object.assign({ key: "Enter", target, prevented: false,
                                                                   preventDefault() { this.prevented = true; } }, extra);
                                         handlers.keydown(e); return e.prevented; };
      out.folder = press(folder);
      await new Promise((r) => setImmediate(r));
      out.posted = posted.slice();
      out.cleared = folder.value;
      out.checkbox = press(field({ type: "checkbox", value: "pdf" }));       // a pick-list row: no submit, no import
      out.file = press(field({ type: "file", dataset: { skIn: "zip" } }));   // Enter opens the file picker as usual
      out.composing = press(folder, { isComposing: true });
      out.otherKey = press(folder, { key: "a" });
      out.count = posted.length;
    """)
    assert out["folder"] is True and out["posted"] == [{"path": "/x/pdf", "attach": True}] and out["cleared"] == ""
    assert out["checkbox"] is True and out["file"] is False and out["composing"] is False and out["otherKey"] is False
    assert out["count"] == 1


@needs_node
def test_home_proposal_preview_and_cleanup_toast_without_the_skills_tab_loaded():
    # regressions: the proposal dialog read the install switches only from the Skills tab's data (null until that
    # tab or Settings opened), so with install_proposals off it promised an automatic install; the cleanup toast
    # said "(in use)" whatever was kept, even nothing
    out = run_js(r"""
      const home = loadHomeJs();
      home.H.lang = "en";
      home.H.skills = null;
      const deps = { python: { files: ["requirements.txt"], packages: ["pypdf"] } };
      home.H.pendingShown = { tidy: { files_list: [], skill_md: "x", deps, origin: "agent",
                                      install: { enabled: true, proposals: false } } };
      out.off = home.pendingSkillHtml({ name: "tidy", owner: "Ann", files: 1, size: 9 });
      home.H.pendingShown.tidy.install = { enabled: true, proposals: true };
      out.on = home.pendingSkillHtml({ name: "tidy", owner: "Ann", files: 1, size: 9 });
      out.toasts = [home.envsCleanedText({ removed: ["a"], kept: [] }),
                    home.envsCleanedText({ removed: [], kept: [{ why: "in use" }, { why: "being built" }, { why: "in use" }] }),
                    home.envsCleanedText({})];
      home.H.lang = "ru";
      out.ru = home.envsCleanedText({ removed: [], kept: [{ why: "being built" }] });
    """)
    assert "Package installation is off in the settings." in out["off"] and "own install code" not in out["off"]
    assert "Installs automatically: pypdf (requirements.txt)." in out["on"] and "own install code" in out["on"]
    assert out["toasts"] == ["Removed 1 environment(s)", "Removed 0 environment(s); kept 3 (in use, being built)",
                             "Removed 0 environment(s)"]
    assert out["ru"] == "Удалено окружений: 0; оставлено: 1 (собираются)"


@needs_node
def test_board_skill_md_is_shown_without_its_front_matter():
    out = run_js(r"""
      const SK = require(staticDir + "/skills.js");
      out.bodies = [SK.bodyOf("---\nname: pdf\ndescription: Reads\n---\n# PDF\nSteps."),
                    SK.bodyOf("﻿---\r\nname: x\r\n---\r\nBody"), SK.bodyOf("---\n---\nEmpty"),
                    SK.bodyOf("---\nnot closed\n# T"), SK.bodyOf("Text\n---\nname: x\n---\n"), SK.bodyOf(null)];
      app.S.lang = "en";
      out.html = app.skillPreviewHtml({ files_list: [], deps: {},
                                        skill_md: "---\nname: pdf\ndescription: Reads **PDF** <img src=x>\n---\n# PDF\nSteps." });
    """)
    assert out["bodies"] == ["# PDF\nSteps.", "Body", "Empty", "---\nnot closed\n# T", "Text\n---\nname: x\n---\n", ""]
    html = out["html"]
    assert "<hr>" not in html and "name: pdf" not in html and "<h1>PDF</h1>" in html and "Steps." in html


def test_ui_sources_carry_no_mojibake():
    # regression: a "→" saved through the CP1251 codepage came back as "в†’" in app.js
    marks = ("в†", "вЂ", "Ð°", "Ñ€")
    paths = [*STATIC.glob("*.js"), *STATIC.glob("*.html"), *STATIC.glob("*.css"),
             *(STATIC.parent / "tui").glob("*.py"), STATIC.parent / "prompts.py"]
    found = [(p.name, m) for p in paths for m in marks if m in p.read_text(encoding="utf-8")]
    assert not found


SKILLS_PRE = r"""
  globalThis.skillsState = (E) => [
    { name: "pdf", description: "Reads **PDF**. " + E, from: "library", owner: null, library: "none", digest: "d1",
      attached_via: "role:Tester", roles: ["Tester"], files: 3, size: 4096, manual: false,
      deps: [{ ecosystem: "python", status: "ready", files: ["requirements.txt"], packages: ["pypdf"], python: "/envs/p/bin/python" },
             { ecosystem: "node", status: "failed", files: ["package.json"], packages: ["sharp"], error: E }] },
    { name: "notes", description: "Notes", from: "claude-user", owner: null, library: "none", digest: "n1",
      attached_via: "user", roles: [], files: 1, size: 10, manual: true,
      deps: [{ ecosystem: "python", status: "installing", files: ["requirements.txt"], packages: [], queued: true },
             { ecosystem: "node", status: "unsupported", files: ["package.json"], reason: { code: "unsupported", detail: "a git dependency" } }] },
    { name: "csv-cleanup", description: "Cleans CSV", from: "agent", owner: E, library: "pending", digest: "c1",
      attached_via: "create", roles: [], files: 2, size: 20, manual: false,
      deps: [{ ecosystem: "python", status: "off", files: ["requirements.txt"], reason: { code: "proposals" } }] },
  ];
  globalThis.skillsData = (E) => ({
    library_dir: "/lib", install: { enabled: true, proposals: false }, limits: { max_archive_mb: 50 },
    library: [{ name: "pdf", description: "x" }, { name: "web", description: E, changed_outside: true }, { name: "bad", error: "broken" }],
    claude: [{ name: "notes", origin: "claude-user", description: "n" }, { name: "proj", origin: "claude-project", description: "p", agent_writable: true }],
    host: [],
    attached: [{ name: "pdf", source_digest: "d2" }, { name: "notes", source_digest: null }, { name: "csv-cleanup", source_digest: "zz" }],
  });
"""


@needs_node
def test_board_skills_tab_cards_attach_dialog_and_decisions():
    out = run_js(r"""
      const E = process.env.EVIL;
      app.S.lang = "en";
      el("skill-list"); el("skill-library");
      app.S.state = { swarm: { name: "S", goal: null, limits: { max_cost_usd: 0, max_agents: 3 }, spent_usd: 0 },
                      agents: [], mcp: [], triggers: [], jobs: [], skills: skillsState(E) };
      app.S.skillData = skillsData(E);
      app.S.tab = "skills";
      app.renderSkills();
      out.html = elements["skill-list"].innerHTML;
      out.cards = app.S.state.skills.map((s) => app.skillCardHtml(s));
      out.sources = app.S.state.skills.map((s) => app.skillSourceState(s));
      out.unloaded = app.skillSourceState(app.S.state.skills[0], null);
      out.rows = app.attachRows(app.S.skillData, app.S.state.skills).map((r) => [r.ref, r.origin]);
      out.rowHtml = app.attachRows(app.S.skillData, []).map(app.attachRowHtml).join("");
      out.roleSkills = [app.roleSkillsOf("tester"), app.roleSkillsOf(" Tester "), app.roleSkillsOf("Coder")];
      // Save to the library: the clash text asks, then approves with replace
      const results = [{ ok: false, result: "The library already has a different skill 'csv-cleanup'. Approve with replace=true to replace it." },
                       { ok: true, result: "Saved" }];
      globalThis.fetch = async (url, opts) => {
        calls.push({ url, body: opts && opts.body ? JSON.parse(opts.body) : null });
        const r = url.includes("/api/act/") ? (results.shift() || { ok: true, result: "done" }) : { ok: true };
        return { ok: true, status: 200, json: async () => r, text: async () => JSON.stringify(r) };
      };
      calls.length = 0;
      answers.confirm = true;
      await app.saveSkill("csv-cleanup");
      out.save = calls.filter((c) => c.url.includes("/api/act/")).map((c) => [c.url, c.body]);
      calls.length = 0;
      await app.updateSkill("pdf");
      await app.updateSkill("csv-cleanup");             // a member's proposal: nothing to update from
      answers.prompt = "too narrow";
      await app.rejectSkill("csv-cleanup");
      answers.prompt = null;
      out.rejectCancelled = await app.rejectSkill("csv-cleanup");
      await app.reinstallSkill("pdf|node");
      answers.confirm = false;
      out.detachCancelled = await app.detachSkill("pdf");
      out.acts = calls.filter((c) => c.url.includes("/api/act/")).map((c) => [c.url, c.body]);
      // attaching a row a member could have written asks first
      calls.length = 0;
      answers.confirm = false;
      out.attachNo = await app.attachSkill("claude-project:proj");
      answers.confirm = true;
      await app.attachSkill("claude-project:proj");
      out.attach = calls.filter((c) => c.url.includes("/api/act/")).map((c) => [c.url, c.body]);
      const ev = (type, data, actor = "user") => app.describeEvent({ id: 1, ts: 0, type, actor, data });
      out.events = [ev("skill.attached", { name: "pdf", replaced: false, from: "library" }),
                    ev("skill.attached", { name: "pdf", replaced: true }),
                    ev("skill.detached", { name: "pdf" }),
                    ev("skill.proposed", { name: "x", owner: "Ann Lee" }, "Ann Lee"),
                    ev("skill.library", { name: "x", status: "saved" }),
                    ev("skill.library", { name: "x", status: "rejected" }),
                    ev("skill.deps", { name: "pdf", status: "ready", files: ["requirements.txt"], seconds: 14 }, null),
                    ev("skill.deps", { name: "pdf", status: "failed", error: "boom", files: [] }, null),
                    // whose skill, and a save an MCP client made says so
                    ev("skill.library", { name: "x", owner: "Ann Lee", status: "saved", via: "mcp" }),
                    ev("skill.library", { name: "x", owner: "Ann Lee", status: "rejected", via: "act" }),
                    ev("skill.attached", { name: "pdf", replaced: false, via: "launcher-mcp" })];
      out.feed = app.FEED_TYPES;
    """, pre=SKILLS_PRE, env={"EVIL": EVIL})
    html = out["html"]
    assert_escaped(html, at_least=3)                                 # description, an error, an owner
    pdf, notes, proposal = out["cards"]
    assert out["sources"] == ["changed", "gone", None] and out["unloaded"] is None
    assert "data-skill-update=\"pdf\"" in pdf and "data-skill-update" not in notes + proposal   # Update: own source changed
    assert "source gone" in notes and "source gone" not in pdf
    assert "<strong>PDF</strong>" in pdf                              # the description as Markdown (md())
    assert "role: Tester" in pdf and "brought by role Tester" in pdf and "4.0 KB" in pdf
    assert 'data-copy="/envs/p/bin/python"' in pdf and 'data-skill-log="pdf|node"' in pdf
    assert 'data-skill-reinstall="pdf|node"' in pdf and ">failed<" in pdf
    assert "queued" in notes and "a git dependency" in notes and "only when asked" in notes
    assert 'data-skill-save="csv-cleanup"' in proposal and 'data-skill-nosave="csv-cleanup"' in proposal
    assert "waits for your decision" in proposal and "data-skill-save" not in pdf + notes
    assert out["rows"] == [["library:web", "library"], ["claude-project:proj", "claude-project"]]
    assert "claude-project" in out["rowHtml"] and "agents can write here" in out["rowHtml"]
    assert "changed outside DreamTeam" in out["rowHtml"] and "broken" not in out["rowHtml"]
    assert out["roleSkills"] == [["pdf"], ["pdf"], []]
    assert out["save"] == [["/api/act/skill_approve", {"name": "csv-cleanup", "approve": True}],
                           ["/api/act/skill_approve", {"name": "csv-cleanup", "approve": True, "replace": True}]]
    assert out["acts"] == [["/api/act/skill_attach", {"name": "library:pdf", "replace": True}],
                           ["/api/act/skill_approve", {"name": "csv-cleanup", "approve": False, "comment": "too narrow"}],
                           ["/api/act/skill_reinstall", {"name": "pdf", "ecosystem": "node"}]]
    assert out["rejectCancelled"] is None and out["detachCancelled"] is None
    assert out["attachNo"] is None
    assert out["attach"] == [["/api/act/skill_attach", {"name": "claude-project:proj"}]]
    assert out["events"][0].endswith("attached skill pdf") and out["events"][1].endswith("updated skill pdf")
    assert out["events"][2].endswith("detached skill pdf") and "Ann Lee" in out["events"][3]
    assert "saved x to the library" in out["events"][4] and "did not save x" in out["events"][5]
    assert out["events"][6] == "installed the packages of skill pdf (requirements.txt, 14 s)"
    assert out["events"][7] == "could not install the packages of skill pdf: boom"
    assert out["events"][8].endswith("saved Ann Lee's skill x to the library (via MCP)")
    assert out["events"][9].endswith("did not save Ann Lee's skill x to the library")
    assert out["events"][10].endswith("attached skill pdf (via the launcher MCP)")
    assert "via" not in out["events"][0] + out["events"][4]
    assert "skill.*" in out["feed"]


@needs_node
def test_home_skills_tab_new_session_picker_cards_and_delete():
    out = run_js(r"""
      const E = process.env.EVIL;
      const home = loadHomeJs();
      home.H.lang = "en";
      for (const id of ["skill-library", "skill-lib-dir", "skill-claude", "skill-host-section", "skill-host", "modal",
                        "modal-backdrop", "pending-skills"]) el(id);
      el("toasts", { appendChild: () => {} });
      document.createElement = () => ({ className: "", textContent: "", remove() {} });
      home.H.skills = {
        library_dir: "/lib", sandbox: false, install: { enabled: true, proposals: true },
        library: [{ name: "pdf", description: E, origin: "git", files: 2, size: 100, changed_outside: true,
                    source: { url: "https://github.com/o/r", ref: "main", subdir: "pdf" } },
                  { name: "bad", error: "broken SKILL.md" }],
        claude: [{ name: "notes", origin: "claude-user", description: "n", library: "different", path: "/u/.claude/skills/notes" },
                 { name: "proj", origin: "claude-project", description: "p" }],
        host: [],
      };
      home.H.skillShown["library:pdf"] = { files_list: [{ path: "SKILL.md", size: 10 }], skill_md: "# Title\n" + E,
                                           deps: { python: { files: ["requirements.txt"], packages: ["pypdf"] } } };
      home.renderSkillsPane();
      out.lib = elements["skill-library"].innerHTML;
      out.claude = elements["skill-claude"].innerHTML;
      out.hostHidden = elements["skill-host-section"].classList.contains("hidden");
      home.H.skills.sandbox = true;   // in the Docker sandbox: where the user's Claude Code skills come from
      home.renderSkillsPane();
      out.claudeSandbox = elements["skill-claude"].innerHTML;
      out.hostShown = !elements["skill-host-section"].classList.contains("hidden");
      home.H.skills.sandbox = false;
      home.renderSkillsPane();
      out.refs = home.skillSources(home.H.skills).map((s) => s.ref);
      out.resolves = ["pdf", "notes", "proj", "bad", "nope"].map((n) => home.skillResolves(n));
      out.picker = home.nsSkillsHtml(home.H.skills, new Set(["claude-user:notes"]));
      // ticking a library entry changed outside DreamTeam asks first ("no" leaves it unticked)
      home.H.nsSkills = new Set();
      const asked = [];
      globalThis.confirm = (q) => { asked.push(q); return answers.confirm; };
      const tick = (ref) => { const target = { checked: true, dataset: { nsSkill: ref } }; home.onNsSkillChange({ target }); return target.checked; };
      answers.confirm = false;
      out.tickNo = [tick("pdf"), [...home.H.nsSkills]];
      answers.confirm = true;
      out.tickYes = [tick("pdf"), tick("claude-user:notes"), [...home.H.nsSkills]];
      out.asked = asked.slice();
      const catalog = [{ key: "tester", name: "Tester", skills: ["pdf", "ghost"] }, { key: "dev", name: "Developer", skills: ["pdf"] }];
      const team = home.teamSkills([{ role: "tester", count: 1 }, { role: "dev", count: 2 }, { role: "x", custom: true, count: 1 }], catalog);
      out.team = team;
      out.teamHtml = home.nsRoleSkillsHtml(team);
      out.card = home.sessionSkillsHtml({ id: "s1", skills: ["pdf", "csv"], skills_pending: ["csv"] });
      out.cardNone = home.sessionSkillsHtml({ id: "s2", skills: [] });
      // the library decision of a pending proposal: a clash asks, then replaces
      home.H.sessions = [{ id: "s1", title: "S", status: "stopped", skills_pending: ["csv"] }];
      const replies = [{ status: 409, body: { error: "clash", exists: ["csv"] } }, { status: 200, body: { text: "Saved csv" } },
                       { status: 200, body: { text: "Not saved" } }];
      globalThis.fetch = async (url, opts) => {
        calls.push({ url, method: opts && opts.method, body: opts && opts.body ? JSON.parse(opts.body) : null });
        const r = url.includes("/decide") ? replies.shift() : { status: 200, body: { sessions: [] } };
        return { ok: r.status < 400, status: r.status, statusText: "", json: async () => r.body };
      };
      answers.confirm = true;
      await home.decidePendingSkill("s1", "csv", true);
      answers.prompt = "meh";
      await home.decidePendingSkill("s1", "csv", false);
      out.decide = calls.filter((c) => c.url.includes("/decide")).map((c) => [c.url, c.body]);
      // deleting with proposals waiting: two choices, each a DELETE with ?skills=
      let onActions = null;
      el("delete-actions", { addEventListener: (type, fn) => { onActions = fn; } });
      home.H.sessions = [{ id: "s1", title: "S", status: "stopped", skills_pending: ["csv"] }];   // (a reload emptied it)
      home.deleteModal("s1");
      out.del = elements.modal.innerHTML;
      calls.length = 0;
      globalThis.fetch = async (url, opts) => {
        calls.push({ url, method: opts && opts.method });
        return { ok: true, status: 200, json: async () => ({ ok: true, sessions: [] }) };
      };
      await onActions({ target: { closest: () => ({ dataset: { deleteSkills: "save" } }) } });
      // a delete refused for proposals made meanwhile opens the dialog again with them
      home.H.sessions = [{ id: "s2", title: "T", status: "stopped" }];
      globalThis.fetch = async (url, opts) => {
        calls.push({ url, method: opts && opts.method });
        if (opts && opts.method === "DELETE") return { ok: false, status: 409, statusText: "", json: async () => ({ error: "pending", pending_skills: ["late"] }) };
        return { ok: true, status: 200, json: async () => ({ sessions: [] }) };
      };
      await home.confirmDelete("s2", "", null);
      out.again = elements.modal.innerHTML;
      out.deletes = calls.filter((c) => c.method === "DELETE").map((c) => c.url);
      // a role card's skills: added (checked like a skill name), listed in the saved entry, cleared by Reset
      home.H.rolePresets = JSON.parse(process.env.DT_PRESETS);
      const cards = home.makeRoleCards([{ name: "tester", skills: ["pdf"] }, { name: "Scout", description: "Looks", skills: ["web"] }]);
      const card = cards.find((c) => c.id === "tester");
      out.cardSkills = card.skills.slice();
      out.entries = home.roleEntries(cards).filter((e) => e.skills);
      out.roleHtml = home.roleSkillsHtml({ ...card, skills: ["pdf", "ghost"] });
      const cardEl = { querySelector: () => ({ value: " Csv-Tools " }) };
      out.added = [home.addRoleSkill(card, cardEl), card.skills.slice()];
      out.bad = home.addRoleSkill(card, { querySelector: () => ({ value: "not ok!" }) });
    """, env={"EVIL": EVIL, "DT_PRESETS": json.dumps(role_presets())})
    lib = out["lib"]
    assert_escaped(lib, at_least=2)                                  # the description and SKILL.md (in a <pre>)
    assert f'<pre class="skill-md"># Title\n{EVIL_ESC}</pre>' in lib  # SKILL.md stays text on the home page
    assert "changed outside DreamTeam" in lib and "https://github.com/o/r#main:pdf" in lib
    assert "Installs automatically: pypdf (requirements.txt)." in lib and "broken SKILL.md" in lib
    claude = out["claude"]
    assert "notes" in claude and "proj" not in claude and "different in the library" in claude
    assert out["hostHidden"] is True
    assert "does not see your ~/.claude/skills" in out["claudeSandbox"] and "notes" in out["claudeSandbox"]
    assert "does not see" not in claude and out["hostShown"] is True
    assert out["refs"] == ["pdf", "bad", "claude-user:notes", "claude-project:proj"]
    assert out["resolves"] == [True, True, False, False, False]      # the workspace's skills never resolve a role's
    assert 'data-ns-skill="claude-user:notes" checked' in out["picker"] and 'data-ns-skill="pdf"' in out["picker"]
    assert "claude-project" not in out["picker"] and 'data-ns-skill="bad"' not in out["picker"]
    pdf_row = out["picker"].split('data-ns-skill="pdf"')[1].split("</label>")[0]
    assert "changed outside DreamTeam" in pdf_row and "changed outside" not in out["picker"].split('data-ns-skill="claude-user:notes"')[1]
    assert out["tickNo"] == [False, []] and out["tickYes"] == [True, True, ["pdf", "claude-user:notes"]]
    assert out["asked"] == ["Skill “pdf”: changed outside DreamTeam. Attach it anyway?"] * 2   # notes: no question
    assert out["team"] == [{"name": "pdf", "resolves": True}, {"name": "ghost", "resolves": False}]
    assert "badge warn" in out["teamHtml"] and "ghost · not in the library" in out["teamHtml"]
    assert 'data-skills-pending="s1"' in out["card"]["warn"] and "skills: 2" in out["card"]["count"]
    assert out["cardNone"] == {"count": "", "warn": ""}
    url = "/api/sessions/s1/skills/csv/decide"
    assert out["decide"] == [[url, {"approve": True, "comment": ""}], [url, {"approve": True, "comment": "", "replace": True}],
                             [url, {"approve": False, "comment": "meh"}]]
    assert 'data-delete-skills="save"' in out["del"] and 'data-delete-skills="discard"' in out["del"]
    assert 'data-delete-skills=""' not in out["del"] and "csv" in out["del"]
    assert "late" in out["again"] and 'data-delete-skills="discard"' in out["again"]
    assert out["deletes"] == ["/api/sessions/s1?confirm=s1&skills=save", "/api/sessions/s2?confirm=s2"]
    assert out["cardSkills"] == ["pdf"]
    assert [(e["name"], e["skills"]) for e in out["entries"]] == [("tester", ["pdf"]), ("Scout", ["web"])]
    assert out["added"] == [True, ["pdf", "csv-tools"]] and out["bad"] is False
    assert 'data-rc-skill-del="ghost"' in out["roleHtml"] and "not in the library" in out["roleHtml"]
