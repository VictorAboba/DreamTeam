"use strict";
// Agent traces tab of the board UI (docs/SPEC.md §18.4). Loaded after app.js and uses its
// helpers (S, $, $$, esc, t, api, toast, avatar, members, fmtTime, short, openModal, openTask, BASE).

Object.assign(I18N.ru, {
  tr_all_agents: "Все агенты", tr_all_statuses: "Все статусы", tr_failed: "Только с ошибками", tr_refresh: "Обновить",
  tr_stats: "Статистика", tr_more: "Показать ещё", tr_empty: "Трейсов пока нет. Они появляются, когда агенты просыпаются и работают.",
  tr_pick: "Выберите трейс слева — увидите каждый шаг агента: вызовы LLM, мысли, инструменты, токены, стоимость и время.",
  tr_steps: "шагов", tr_llm: "LLM", tr_tools: "инстр.", tr_errors: "ошибок", tr_woke: "Разбудило", tr_outcome: "Итог",
  tr_timeline: "Шаги", tr_export: "Экспорт JSON", tr_export_ctx: "JSON с контекстами", tr_export_agent: "Все трейсы агента (JSONL)",
  tr_context: "Контекст запроса", tr_thinking: "Мысли", tr_text: "Ответ", tr_calls: "Вызовы инструментов", tr_args: "Аргументы",
  tr_result: "Результат", tr_error: "Ошибка", tr_shown: "модель увидела {shown} из {total} символов", tr_load_full: "Загрузить полностью",
  tr_tokens: "токены", tr_in: "вход", tr_out: "выход", tr_cache_r: "кэш (чтение)", tr_cache_w: "кэш (запись)", tr_cost: "стоимость",
  tr_model: "модель", tr_stop: "остановка", tr_attempt: "попытка", tr_retry_in: "повтор через {s} с", tr_session: "сессия",
  tr_ctx_title: "Контекст LLM-запроса #{id}", tr_ctx_note: "Так модель увидела запрос (нейтральный формат DreamTeam; адаптер провайдера переводит его в формат API).",
  tr_system: "Системный промпт", tr_tools_n: "Инструментов: {n}", tr_messages_n: "Сообщений: {n}", tr_tasks: "Задачи",
  tr_by_tool: "Инструменты", tr_by_model: "Модели", tr_calls_n: "вызовов", tr_avg: "сред.", tr_max: "макс.", tr_total: "Всего",
  tr_traces_n: "трейсов", tr_back: "К трейсу", tr_no_match: "Нет трейсов, подходящих под фильтр.",
  trs_running: "идёт", trs_done: "готово", trs_error: "ошибка", trs_step_limit: "лимит шагов", trs_refusal: "отказ модели",
  trs_paused: "пауза", trs_stopped: "остановлен", trs_interrupted: "прерван",
  trk_input: "вход", trk_llm: "LLM", trk_tool: "инструмент", trk_compaction: "сжатие", trk_event: "событие",
  trn_wake: "пробуждение", trn_incoming: "новые события", trn_nudge: "напоминание", trn_continue: "продолжение",
  trn_reply_to_user: "ответ пользователю", trn_refusal: "отказ", trn_budget_pause: "пауза по бюджету", trn_step_limit: "лимит шагов",
  trn_stopped: "остановка", trn_crash: "сбой", trn_step: "запрос", trn_compaction: "сжатие контекста", tr_external: "внешний участник",
});
Object.assign(I18N.en, {
  tr_all_agents: "All agents", tr_all_statuses: "All statuses", tr_failed: "Errors only", tr_refresh: "Refresh",
  tr_stats: "Statistics", tr_more: "Show more", tr_empty: "No traces yet. They appear when agents wake up and work.",
  tr_pick: "Pick a trace on the left to see every step of the agent: LLM calls, thinking, tools, tokens, cost and timing.",
  tr_steps: "steps", tr_llm: "LLM", tr_tools: "tools", tr_errors: "errors", tr_woke: "Woken by", tr_outcome: "Outcome",
  tr_timeline: "Steps", tr_export: "Export JSON", tr_export_ctx: "JSON with contexts", tr_export_agent: "All agent traces (JSONL)",
  tr_context: "Request context", tr_thinking: "Thinking", tr_text: "Reply", tr_calls: "Tool calls", tr_args: "Arguments",
  tr_result: "Result", tr_error: "Error", tr_shown: "the model saw {shown} of {total} chars", tr_load_full: "Load in full",
  tr_tokens: "tokens", tr_in: "in", tr_out: "out", tr_cache_r: "cache read", tr_cache_w: "cache write", tr_cost: "cost",
  tr_model: "model", tr_stop: "stop", tr_attempt: "attempt", tr_retry_in: "retry in {s} s", tr_session: "session",
  tr_ctx_title: "Context of LLM request #{id}", tr_ctx_note: "What the model received (DreamTeam's neutral format; the provider adapter converts it to the API format).",
  tr_system: "System prompt", tr_tools_n: "Tools: {n}", tr_messages_n: "Messages: {n}", tr_tasks: "Tasks",
  tr_by_tool: "Tools", tr_by_model: "Models", tr_calls_n: "calls", tr_avg: "avg", tr_max: "max", tr_total: "Total",
  tr_traces_n: "traces", tr_back: "Back to the trace", tr_no_match: "No traces match the filter.",
  trs_running: "running", trs_done: "done", trs_error: "error", trs_step_limit: "step limit", trs_refusal: "refusal",
  trs_paused: "paused", trs_stopped: "stopped", trs_interrupted: "interrupted",
  trk_input: "input", trk_llm: "LLM", trk_tool: "tool", trk_compaction: "compaction", trk_event: "event",
  trn_wake: "wake-up", trn_incoming: "new events", trn_nudge: "nudge", trn_continue: "continuation",
  trn_reply_to_user: "reply to the user", trn_refusal: "refusal", trn_budget_pause: "budget pause", trn_step_limit: "step limit",
  trn_stopped: "stop", trn_crash: "crash", trn_step: "request", trn_compaction: "context compaction", tr_external: "external member",
});

const TR = {
  agent: "", status: "", items: [], hasMore: false, openId: null, open: null, expanded: new Set(), full: {}, view: "trace",
  loaded: false, seq: 0, statsSeq: 0, openDetails: {},
};
const TRACE_STATUSES_UI = ["running", "done", "error", "step_limit", "refusal", "paused", "stopped", "interrupted"];
const FAILED_STATUSES = ["error", "step_limit", "refusal", "interrupted"]; // the "failed" filter (tracing.list_traces)
const TRACE_BADGE = { done: "done", running: "", error: "err", refusal: "err", interrupted: "err", step_limit: "warn", paused: "warn", stopped: "plain" };
const SPAN_ICONS = { input: "📥", llm: "🧠", tool: "🔧", compaction: "🗜", event: "📌" };
const CUT_MARK = /…\[\d+ chars cut\]…/;
const PAGE = 50;

const fmtMs = (ms) => { ms = Math.round(Number(ms) || 0); return ms < 1000 ? `${ms} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.floor(ms / 60000)}m ${Math.floor((ms % 60000) / 1000)}s`; };
const fmtTok = (n) => { n = Number(n) || 0; return n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n); };
const fmtUsd = (v) => "$" + (Number(v) || 0).toFixed((Number(v) || 0) < 0.1 ? 4 : 2);
const promptTokens = (u) => (u ? (u.input_tokens || 0) + (u.cache_read_tokens || 0) + (u.cache_write_tokens || 0) : 0);
function trStatusBadge(status) {
  const cls = Object.prototype.hasOwnProperty.call(TRACE_BADGE, status) ? TRACE_BADGE[status] : "plain";
  return `<span class="badge ${cls}">${status === "running" ? '<span class="tr-pulse"></span>' : ""}${esc(tv("trs_", status))}</span>`;
}
/** Whether a trace summary passes the list filters (agent; status, where "failed" is any failure). */
function traceMatches(tr, agent, status) {
  if (agent && tr.agent !== agent) return false;
  if (!status) return true;
  return status === "failed" ? FAILED_STATUSES.includes(tr.status) : tr.status === status;
}
/** The trace list after a live trace.* event: the summary is updated in place, inserted (newest first) when it
 * matches the filters, or dropped when it no longer matches them. Returns null when the list does not change. */
function mergeTraceEvent(items, tr, agent, status, hasMore) {
  const i = items.findIndex((x) => x.id === tr.id);
  const merged = i >= 0 ? Object.assign({}, items[i], tr) : tr;
  if (!traceMatches(merged, agent, status)) return i >= 0 ? items.filter((x) => x.id !== tr.id) : null;
  if (i >= 0) { const next = items.slice(); next[i] = merged; return next; }
  // an id below the last loaded one belongs to a page that is not loaded yet ("Show more" brings it)
  if (hasMore && items.length && tr.id < items[items.length - 1].id) return null;
  return items.concat([merged]).sort((a, b) => b.id - a.id);
}
/** Open state of a <details> the user may have toggled (kept across the live re-renders of the detail). */
function detailsOpen(key, fallback) { return Object.prototype.hasOwnProperty.call(TR.openDetails, key) ? TR.openDetails[key] : fallback; }
function onDetailsToggle(e) {
  const el = e.target;
  if (el && el.dataset && el.dataset.keep) TR.openDetails[el.dataset.keep] = el.open;
}
function spanName(s) {
  if (s.kind === "llm") return s.name === "step" || s.name === "compaction" ? t("trn_" + s.name) : s.name;
  if (s.kind === "tool") return s.name;
  return tv("trn_", s.name);
}
function pre(text, cls = "") { return `<pre class="tr-pre ${cls}">${esc(text)}</pre>`; }
function json(value) { return JSON.stringify(value, null, 2); }

/* ---------------------------------------------------------------- list */
function agentOptionsHtml() {
  const agents = members(true).map((m) => m.name);
  const opts = [`<option value="">${esc(t("tr_all_agents"))}</option>`].concat(agents.map((n) => `<option value="${esc(n)}"${n === TR.agent ? " selected" : ""}>${esc(n)}</option>`));
  if (TR.agent && !agents.includes(TR.agent)) opts.push(`<option value="${esc(TR.agent)}" selected>${esc(TR.agent)}</option>`);
  return opts.join("");
}
function tracesPaneHtml() {
  const sopts = [`<option value="">${esc(t("tr_all_statuses"))}</option>`, `<option value="failed"${TR.status === "failed" ? " selected" : ""}>⚠ ${esc(t("tr_failed"))}</option>`]
    .concat(TRACE_STATUSES_UI.map((s) => `<option value="${s}"${TR.status === s ? " selected" : ""}>${esc(tv("trs_", s))}</option>`));
  return `<div class="traces-layout">
    <div class="traces-side">
      <div class="traces-filters">
        <select id="tr-agent">${agentOptionsHtml()}</select>
        <select id="tr-status">${sopts.join("")}</select>
        <button class="icon-btn small" type="button" id="tr-refresh" title="${esc(t("tr_refresh"))}">⟳</button>
        <button class="btn small${TR.view === "stats" ? " primary" : ""}" type="button" id="tr-stats">📊 ${esc(t("tr_stats"))}</button>
      </div>
      <div class="trace-list" id="trace-list"></div>
    </div>
    <div class="trace-detail" id="trace-detail"><div class="empty">${esc(t("tr_pick"))}</div></div>
  </div>`;
}

function traceItemHtml(tr) {
  const tokens = promptTokens(tr) ;
  const external = tr.kind === "external";
  const sub = external
    ? `${esc(t("tr_external"))} · ${tr.tool_calls || 0} ${esc(t("tr_tools"))}`
    : `${tr.steps || 0} ${esc(t("tr_steps"))} · ${fmtMs(tr.duration_ms)} · ${fmtTok(tokens)}→${fmtTok(tr.output_tokens)} · ${fmtUsd(tr.cost_usd)}`;
  return `<button type="button" class="trace-item${tr.id === TR.openId ? " active" : ""}${tr.errors ? " has-err" : ""}" data-trace="${Number(tr.id)}">
    <div class="ti-head">${avatar(tr.agent, "small")}<b>${esc(tr.agent)}</b><span class="muted">#${Number(tr.id)} · ${esc(fmtTime(tr.started_at))}</span><span class="spacer"></span>${trStatusBadge(tr.status)}</div>
    <div class="ti-sub">${sub}${tr.errors ? ` · <span class="err-text">${Number(tr.errors)} ${esc(t("tr_errors"))}</span>` : ""}</div>
    ${tr.trigger ? `<div class="ti-trigger">${esc(short(tr.trigger, 140))}</div>` : ""}
  </button>`;
}

function renderTraceList() {
  const box = $("#trace-list");
  if (!box) return;
  const html = TR.items.map(traceItemHtml).join("") || `<div class="empty">${esc(t(TR.agent || TR.status ? "tr_no_match" : "tr_empty"))}</div>`;
  box.innerHTML = html + (TR.hasMore ? `<button class="btn small tr-more" type="button" id="tr-more">${esc(t("tr_more"))}</button>` : "");
}

function showPick() {
  const box = $("#trace-detail");
  if (box) box.innerHTML = `<div class="empty">${esc(t("tr_pick"))}</div>`;
}

function buildTracesPane() {
  const pane = $("#pane-traces");
  if (!pane) return false;
  pane.innerHTML = tracesPaneHtml();
  TR.loaded = true;
  wireTracesPane(pane);
  return true;
}

async function loadTraces(append = false) {
  if (!$("#pane-traces")) return;
  if (!TR.loaded || !$("#trace-list")) buildTracesPane();
  else setHtml($("#tr-agent"), agentOptionsHtml()); // members hired since the pane was built
  const params = new URLSearchParams({ limit: String(PAGE) });
  if (TR.agent) params.set("agent", TR.agent);
  if (TR.status) params.set("status", TR.status);
  if (append && TR.items.length) params.set("before", String(TR.items[TR.items.length - 1].id));
  const seq = ++TR.seq;
  try {
    const data = await api(`/api/traces?${params}`);
    if (seq !== TR.seq) return; // a newer request (another filter, a refresh) superseded this one
    const items = data.traces || [];
    TR.items = append ? TR.items.concat(items.filter((x) => !TR.items.some((y) => y.id === x.id))) : items;
    TR.hasMore = items.length === PAGE;
    renderTraceList();
    if (append) return;
    if (TR.view === "stats") loadTraceStats();
    else if (TR.openId) loadTrace(TR.openId, true);
  } catch (e) { if (seq === TR.seq) toast(errText(e), "err"); }
}

function wireTracesPane(pane) {
  $("#tr-agent").addEventListener("change", (e) => {
    TR.agent = e.target.value; TR.openId = null; TR.open = null;
    if (TR.view !== "stats") showPick();
    loadTraces();
  });
  $("#tr-status").addEventListener("change", (e) => { TR.status = e.target.value; loadTraces(); });
  $("#tr-refresh").addEventListener("click", () => loadTraces());
  $("#tr-stats").addEventListener("click", () => {
    TR.view = TR.view === "stats" ? "trace" : "stats";
    $("#tr-stats").classList.toggle("primary", TR.view === "stats");
    if (TR.view === "stats") loadTraceStats(); else if (TR.openId) loadTrace(TR.openId); else showPick();
  });
  // the pane element itself survives rebuilds: its delegated listeners are added once (a second click
  // listener made every span toggle twice, i.e. not at all, after the drawer's "Traces" button)
  if (!pane._trWired) {
    pane._trWired = true;
    pane.addEventListener("click", onTracesClick);
    pane.addEventListener("toggle", onDetailsToggle, true);
  }
}

/** Open the traces tab, optionally filtered to one agent (button in the agent drawer). */
function openTraces(agent) {
  TR.agent = agent || "";
  TR.openId = null;
  TR.open = null;
  TR.view = "trace";
  TR.loaded = false;
  setTab("traces");
}

/** The language changed (app.js setLang): rebuild the pane in the new language with the data already loaded. */
function tracesLangChanged() {
  if (!TR.loaded || !$("#trace-list")) return;
  if (!buildTracesPane()) return;
  renderTraceList();
  if (TR.view === "stats") loadTraceStats();
  else if (TR.open && TR.open.id === TR.openId) renderTraceDetail(false);
  else showPick();
}

/* ---------------------------------------------------------------- detail */
async function loadTrace(id, quiet = false) {
  if (TR.openId !== id) TR.openDetails = {};
  TR.openId = id;
  TR.view = "trace";
  $$(".trace-item").forEach((el) => el.classList.toggle("active", Number(el.dataset.trace) === id));
  try {
    const data = await api(`/api/traces/${id}?payload=20000`);
    if (TR.openId !== id || TR.view !== "trace") return; // another trace or the statistics were opened meanwhile
    TR.open = data;
    renderTraceDetail(quiet);
  } catch (e) { if (!quiet) toast(errText(e), "err"); }
}

function renderTraceDetail(keepScroll) {
  const box = $("#trace-detail");
  const tr = TR.open;
  if (!box || !tr) return;
  const scroll = box.scrollTop;
  const tokens = promptTokens(tr);
  const tasks = (tr.tasks || []).map((id) => `<button type="button" class="link" data-open-task="${Number(id)}">#${Number(id)}</button>`).join(" ");
  const exportBase = `${BASE}/api/traces/${Number(tr.id)}/export`;
  const spans = tr.spans || [];
  box.innerHTML = `<div class="td-head">
      ${avatar(tr.agent)}<div class="td-who"><b>${esc(tr.agent)}</b> <span class="muted">#${Number(tr.id)}${tr.model ? " · " + esc(tr.model) : ""}</span>
        <div class="muted">${esc(fmtTime(tr.started_at))} · ${fmtMs(tr.duration_ms)}</div></div>
      <span class="spacer"></span>${trStatusBadge(tr.status)}
    </div>
    <div class="td-stats">
      <div><b>${tr.steps || 0}</b><span>${esc(t("tr_steps"))}</span></div>
      <div><b>${tr.llm_calls || 0}</b><span>${esc(t("tr_llm"))}</span></div>
      <div><b>${tr.tool_calls || 0}</b><span>${esc(t("tr_tools"))}</span></div>
      <div class="${tr.errors ? "bad" : ""}"><b>${tr.errors || 0}</b><span>${esc(t("tr_errors"))}</span></div>
      <div title="${esc(t("tr_cache_r"))}: ${fmtTok(tr.cache_read_tokens)} · ${esc(t("tr_cache_w"))}: ${fmtTok(tr.cache_write_tokens)}"><b>${fmtTok(tokens)} → ${fmtTok(tr.output_tokens)}</b><span>${esc(t("tr_tokens"))}</span></div>
      <div><b>${fmtUsd(tr.cost_usd)}</b><span>${esc(t("tr_cost"))}</span></div>
    </div>
    <div class="td-actions">
      ${tasks ? `<span class="muted">${esc(t("tr_tasks"))}:</span> ${tasks}` : ""}
      <span class="spacer"></span>
      <a class="btn small" href="${exportBase}" download>⬇ ${esc(t("tr_export"))}</a>
      <a class="btn small" href="${exportBase}?contexts=1" download>⬇ ${esc(t("tr_export_ctx"))}</a>
      <a class="btn small" href="${BASE}/api/agents/${encodeURIComponent(tr.agent)}/traces.jsonl" download>⬇ JSONL</a>
    </div>
    ${tr.trigger ? `<details class="td-block" data-keep="trigger"${detailsOpen("trigger", spans.length < 3) ? " open" : ""}><summary>📥 ${esc(t("tr_woke"))}</summary>${pre(tr.trigger)}</details>` : ""}
    <div class="td-section">${esc(t("tr_timeline"))}</div>
    <div class="waterfall">${waterfallHtml(Object.assign({}, tr, { spans }))}</div>
    ${tr.outcome ? `<div class="td-outcome"><b>${esc(t("tr_outcome"))}:</b> ${esc(tr.outcome)}</div>` : ""}`;
  if (keepScroll) box.scrollTop = scroll;
}

function waterfallHtml(tr) {
  const start = Number(tr.started_at) || 0;
  // a running trace ends "now"; reduce instead of Math.max(...spread): a long trace must not overflow the stack
  const end = tr.spans.reduce((m, s) => Math.max(m, Number(s.ended_at || s.started_at) || 0), Number(tr.ended_at) || Date.now() / 1000);
  const total = Math.max(0.001, end - start);
  const depth = {};
  for (const s of tr.spans) depth[s.id] = s.parent_id && depth[s.parent_id] !== undefined ? Math.min(6, depth[s.parent_id] + 1) : 0;
  return tr.spans.map((s) => {
    const from = Number(s.started_at) || start;
    const left = Math.max(0, Math.min(100, ((from - start) / total) * 100));
    const width = Math.max(0.6, Math.min(100 - left, (((Number(s.ended_at) || end) - from) / total) * 100));
    const open = TR.expanded.has(s.id);
    const err = s.status === "error" || s.status === "interrupted";
    return `<div class="wf-row${open ? " open" : ""}${err ? " err" : ""}" data-span="${Number(s.id)}">
      <div class="wf-label" style="padding-left:${depth[s.id] * 16 + 4}px">
        <span class="wf-step">${s.step == null ? "" : Number(s.step)}</span>${SPAN_ICONS[s.kind] || "•"} <b>${esc(spanName(s))}</b>
        <span class="wf-sum">${esc(spanSummary(s))}</span>
      </div>
      <div class="wf-track"><div class="wf-bar k-${esc(s.kind)}${err ? " err" : ""}${s.status === "running" ? " running" : ""}" style="left:${left}%;width:${width}%"></div></div>
      <div class="wf-dur">${s.kind === "input" || s.kind === "event" ? "" : fmtMs(s.duration_ms)}</div>
    </div>${open ? `<div class="wf-detail" data-span-detail="${Number(s.id)}">${spanDetailHtml(s)}</div>` : ""}`;
  }).join("");
}

function spanSummary(s) {
  const out = s.output;
  if (s.kind === "llm") {
    if (s.status === "error") return short(s.error || "", 90);
    const u = s.usage || {};
    const calls = out && out.tool_calls && out.tool_calls.length ? " → " + out.tool_calls.map((c) => c.name).join(", ") : "";
    return `${fmtTok(promptTokens(u))}→${fmtTok(u.output_tokens)} · ${fmtUsd(s.cost_usd)}${calls}`;
  }
  if (s.kind === "tool") return short(typeof s.input === "object" ? JSON.stringify(s.input) : String(s.input ?? ""), 90);
  if (s.kind === "compaction") return short(typeof out === "string" ? out : "", 90);
  return short(typeof out === "string" ? out : JSON.stringify(out ?? ""), 90);
}

function cutNote(value) { return typeof value === "string" ? CUT_MARK.test(value) : value && CUT_MARK.test(JSON.stringify(value)); }

function spanDetailHtml(s) {
  const full = TR.full[s.id] || s;
  const meta = s.meta || {};
  const parts = [];
  if (s.kind === "llm") {
    const u = s.usage || {};
    const facts = [
      s.name && s.name !== "step" && s.name !== "compaction" ? `${t("tr_model")}: ${s.name}` : "",
      meta.stop_reason ? `${t("tr_stop")}: ${meta.stop_reason}` : "",
      u.input_tokens !== undefined ? `${t("tr_in")} ${fmtTok(u.input_tokens)} · ${t("tr_cache_r")} ${fmtTok(u.cache_read_tokens)} · ${t("tr_cache_w")} ${fmtTok(u.cache_write_tokens)} · ${t("tr_out")} ${fmtTok(u.output_tokens)}` : "",
      s.cost_usd ? `${t("tr_cost")}: ${fmtUsd(s.cost_usd)}` : "",
      meta.attempt > 1 ? `${t("tr_attempt")} ${meta.attempt}` : "",
      meta.retry_in ? t("tr_retry_in", { s: meta.retry_in }) : "",
    ].filter(Boolean);
    parts.push(`<div class="sd-facts">${facts.map((f) => `<span>${esc(f)}</span>`).join("")}</div>`);
    if (s.error) parts.push(`<div class="sd-title err-text">${esc(t("tr_error"))}</div>${pre(s.error, "err")}`);
    const out = full.output || {};
    if (out.thinking) parts.push(`<details class="sd-think" data-keep="think-${Number(s.id)}"${detailsOpen("think-" + Number(s.id), !(out.text || (out.tool_calls || []).length)) ? " open" : ""}><summary>💭 ${esc(t("tr_thinking"))}</summary>${pre(out.thinking, "thinking")}</details>`);
    if (out.text) parts.push(`<div class="sd-title">💬 ${esc(t("tr_text"))}</div>${pre(out.text)}`);
    if ((out.tool_calls || []).length) {
      parts.push(`<div class="sd-title">🔧 ${esc(t("tr_calls"))}</div>` + out.tool_calls.map((c) =>
        `<div class="sd-call"><code>${esc(c.name)}</code>${c.parse_error ? ` <span class="err-text">${esc(c.parse_error)}</span>` : ""}${pre(json(c.arguments))}</div>`).join(""));
    }
    parts.push(`<div class="sd-actions"><button class="btn small" type="button" data-span-context="${Number(s.id)}">🧾 ${esc(t("tr_context"))}</button>${cutNote(s.output) && !TR.full[s.id] ? `<button class="btn small" type="button" data-span-full="${Number(s.id)}">${esc(t("tr_load_full"))}</button>` : ""}</div>`);
  } else if (s.kind === "tool") {
    parts.push(`<div class="sd-title">${esc(t("tr_args"))}</div>${pre(typeof full.input === "string" ? full.input : json(full.input ?? {}))}`);
    const note = meta.shown_chars ? ` <span class="muted">(${esc(t("tr_shown", { shown: meta.shown_chars, total: meta.chars }))})</span>` : "";
    parts.push(`<div class="sd-title${s.status === "error" ? " err-text" : ""}">${esc(t(s.status === "error" ? "tr_error" : "tr_result"))}${note}</div>${pre(String(full.output ?? ""), s.status === "error" ? "err" : "")}`);
    if ((cutNote(s.output) || cutNote(s.input)) && !TR.full[s.id]) parts.push(`<div class="sd-actions"><button class="btn small" type="button" data-span-full="${Number(s.id)}">${esc(t("tr_load_full"))}</button></div>`);
  } else {
    const body = full.output ?? full.input;
    if (body !== null && body !== undefined && body !== "") parts.push(pre(typeof body === "string" ? body : json(body)));
    if (s.error) parts.push(pre(s.error, "err"));
    if (cutNote(s.output) && !TR.full[s.id]) parts.push(`<div class="sd-actions"><button class="btn small" type="button" data-span-full="${Number(s.id)}">${esc(t("tr_load_full"))}</button></div>`);
  }
  return parts.join("");
}

async function loadFullSpan(id) {
  try {
    const span = await api(`/api/spans/${id}`);
    if (!TR.open || !(TR.open.spans || []).some((x) => x.id === id)) return; // another trace is open now
    TR.full[id] = span;
    renderTraceDetail(true);
  } catch (e) { toast(errText(e), "err"); }
}

function contextHtml(ctx, id) {
  const msgs = (ctx.messages || []).map((m, i) => {
    const role = m.role || "?";
    const body = m.content === null || m.content === undefined ? "" : typeof m.content === "string" ? m.content : json(m.content);
    const calls = (m.tool_calls || []).map((c) => `<div class="sd-call"><code>→ ${esc(c.name)}</code>${pre(json(c.arguments))}</div>`).join("");
    const label = role === "tool" ? `tool · ${m.name || ""}${m.is_error ? " · error" : ""}` : role;
    return `<div class="ctx-msg r-${esc(role)}${m.is_error ? " err" : ""}"><div class="ctx-role">[${i}] ${esc(label)}</div>${body ? pre(body) : ""}${calls}</div>`;
  }).join("");
  const tools = ctx.tools || [];
  return `<div class="modal-head"><h3>🧾 ${esc(t("tr_ctx_title", { id: Number(id) }))}</h3><button class="icon-btn small" type="button" data-close>✕</button></div>
    <p class="muted">${esc(t("tr_ctx_note"))}${ctx.note ? " " + esc(ctx.note) : ""}</p>
    <div class="ctx-facts"><span>${esc(ctx.agent)}</span><span>${esc(ctx.model || "")}</span><span>${esc(t("tr_tools_n", { n: tools.length }))}</span><span>${esc(t("tr_messages_n", { n: (ctx.messages || []).length }))}</span></div>
    <details class="ctx-system"><summary>${esc(t("tr_system"))}</summary>${pre(ctx.system || "")}</details>
    <details class="ctx-system"><summary>${esc(t("tr_tools_n", { n: tools.length }))}</summary>${pre(tools.map((x) => `${x.name} — ${x.description}`).join("\n\n"))}</details>
    <div class="ctx-msgs">${msgs}</div>`;
}

async function showSpanContext(id) {
  let ctx;
  try { ctx = await api(`/api/spans/${id}/context`); } catch (e) { toast(errText(e), "err"); return; }
  openModal(contextHtml(ctx, id), { focus: false });
  $("#modal").classList.add("wide");
}

function statsHtml(st) {
  const num = (v) => Number(v) || 0;
  const rows = (items, cols) => items.map((r) => `<tr>${cols.map((c) => `<td>${esc(c(r))}</td>`).join("")}</tr>`).join("");
  return `<div class="td-head"><b>📊 ${esc(t("tr_stats"))}${TR.agent ? " · " + esc(TR.agent) : ""}</b></div>
      <div class="td-stats">
        <div><b>${num(st.traces)}</b><span>${esc(t("tr_traces_n"))}</span></div>
        <div><b>${num(st.llm_calls)}</b><span>${esc(t("tr_llm"))}</span></div>
        <div><b>${num(st.tool_calls)}</b><span>${esc(t("tr_tools"))}</span></div>
        <div class="${st.errors ? "bad" : ""}"><b>${num(st.errors)}</b><span>${esc(t("tr_errors"))}</span></div>
        <div><b>${fmtTok(promptTokens(st))} → ${fmtTok(st.output_tokens)}</b><span>${esc(t("tr_tokens"))}</span></div>
        <div><b>${fmtUsd(st.cost_usd)}</b><span>${esc(t("tr_cost"))}</span></div>
      </div>
      <div class="td-section">${esc(t("tr_by_model"))}</div>
      <table class="tr-table"><thead><tr><th>${esc(t("tr_model"))}</th><th>${esc(t("tr_calls_n"))}</th><th>${esc(t("tr_errors"))}</th><th>${esc(t("tr_avg"))}</th><th>${esc(t("tr_out"))}</th><th>${esc(t("tr_cost"))}</th></tr></thead>
        <tbody>${rows(st.models || [], [(r) => r.name, (r) => num(r.calls), (r) => num(r.errors), (r) => fmtMs(r.avg_ms), (r) => fmtTok(r.output_tokens), (r) => fmtUsd(r.cost_usd)])}</tbody></table>
      <div class="td-section">${esc(t("tr_by_tool"))}</div>
      <table class="tr-table"><thead><tr><th>${esc(t("tr_tools"))}</th><th>${esc(t("tr_calls_n"))}</th><th>${esc(t("tr_errors"))}</th><th>${esc(t("tr_avg"))}</th><th>${esc(t("tr_max"))}</th></tr></thead>
        <tbody>${rows(st.tools || [], [(r) => r.name, (r) => num(r.calls), (r) => num(r.errors), (r) => fmtMs(r.avg_ms), (r) => fmtMs(r.max_ms)])}</tbody></table>`;
}

async function loadTraceStats() {
  if (!$("#trace-detail")) return;
  const seq = ++TR.statsSeq;
  try {
    const st = await api(`/api/traces/stats${TR.agent ? "?agent=" + encodeURIComponent(TR.agent) : ""}`);
    const box = $("#trace-detail");
    if (seq !== TR.statsSeq || TR.view !== "stats" || !box) return; // superseded, or a trace was opened meanwhile
    box.innerHTML = statsHtml(st);
  } catch (e) { if (seq === TR.statsSeq) toast(errText(e), "err"); }
}

function onTracesClick(e) {
  const el = e.target.closest("[data-trace],[data-span],[data-span-context],[data-span-full],#tr-more");
  if (!el) return;
  if (el.id === "tr-more") { el.disabled = true; loadTraces(true); return; }
  const ds = el.dataset;
  if ("spanContext" in ds) { showSpanContext(Number(ds.spanContext)); return; }
  if ("spanFull" in ds) { loadFullSpan(Number(ds.spanFull)); return; }
  if ("trace" in ds) {
    TR.expanded = new Set(); TR.full = {};
    $("#tr-stats")?.classList.remove("primary");
    loadTrace(Number(ds.trace));
    return;
  }
  if ("span" in ds && !e.target.closest(".wf-detail")) {
    const id = Number(ds.span);
    if (TR.expanded.has(id)) TR.expanded.delete(id); else TR.expanded.add(id);
    renderTraceDetail(true);
  }
}

/* ---------------------------------------------------------------- live updates */
const scheduleTraceReload = makeScheduler(() => { if (S.tab === "traces" && TR.openId && TR.view === "trace") loadTrace(TR.openId, true); });

function onTraceEvent(ev) {
  if (S.tab !== "traces" || !TR.loaded) return; // the tab reloads everything when it is opened
  const tr = (ev.data || {}).trace;
  if (!tr || tr.id === undefined || tr.id === null) return;
  const had = TR.items.some((x) => x.id === tr.id);
  const next = mergeTraceEvent(TR.items, tr, TR.agent, TR.status, TR.hasMore);
  if (next) {
    TR.items = next;
    const now = next.find((x) => x.id === tr.id);
    const node = had && now ? $(`.trace-item[data-trace="${Number(tr.id)}"]`) : null;
    if (node) node.outerHTML = traceItemHtml(now);
    else renderTraceList();
  }
  if (tr.id === TR.openId) scheduleTraceReload(ev.type === "trace.finished" ? 100 : 700);
}

if (typeof module !== "undefined" && module.exports) {
  // pure helpers, exported for the Node-based UI tests (tests/test_ui.py); the browser ignores this
  module.exports = {
    TR, TRACE_STATUSES_UI, FAILED_STATUSES, fmtMs, fmtTok, fmtUsd, trStatusBadge, traceMatches, mergeTraceEvent,
    traceItemHtml, tracesPaneHtml, renderTraceList, renderTraceDetail, waterfallHtml, spanSummary, spanDetailHtml,
    contextHtml, statsHtml, onTraceEvent, onTracesClick, loadTraces, loadTrace, loadTraceStats, openTraces,
    tracesLangChanged, wireTracesPane, showSpanContext, spanName,
  };
}
