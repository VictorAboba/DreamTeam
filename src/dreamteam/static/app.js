"use strict";

/* ---------------------------------------------------------------- i18n */
const I18N = {
  ru: {
    goal: "Цель", budget: "Бюджет", budget_hint: "Потрачено / лимит бюджета", agents: "Агенты", team: "Команда",
    hire: "+ Нанять", everyone: "Все сообщения",
    broadcast_hint: "лента роя и сообщение всем", tab_board: "Доска", tab_feed: "Лента", tab_files: "Файлы",
    tab_mcp: "MCP", tab_triggers: "Триггеры", search: "Поиск задач…", new_task: "+ Задача", chat: "Чат",
    activity: "Активность", type_message: "Сообщение… (Ctrl+Enter — отправить)", send: "Отправить",
    pause: "Пауза", resume: "Продолжить", paused: "Рой на паузе", no_goal: "Цель не задана — нажмите, чтобы задать",
    goal_active: "в работе", goal_done: "достигнута", s_todo: "К работе", s_in_progress: "В работе", s_waiting: "Ожидание",
    s_review: "Ревью", s_done: "Готово", s_failed: "Провалено", s_cancelled: "Отменено", s_closed: "Закрыто",
    a_idle: "свободен", a_working: "работает", a_paused: "пауза", a_error: "ошибка", a_retired: "покинул рой",
    empty: "пусто", blocked_by: "ждёт", task: "Задача", title: "Заголовок", goal_field: "Цель задачи",
    goal_hint: "Зачем нужна задача и что считается успехом", description: "Описание", status: "Статус",
    assignee: "Исполнитель", reviewer: "Ревьюер", priority: "Приоритет", tags: "Теги (через запятую)",
    depends_on: "Зависит от задач (номера через запятую)", parent: "Родительская задача", none: "— нет —",
    user: "пользователь (вы)", save: "Сохранить", cancel: "Отмена", create: "Создать", close: "Закрыть",
    result: "Итог", discussion: "Обсуждение", history: "История", write_post: "Написать в обсуждение… (@Имя — упомянуть, Ctrl+Enter — отправить)",
    post: "Опубликовать", reply: "Ответить", replying_to: "Ответ для", reply_to_post: "Перейти к посту, на который это ответ",
    approve: "Принять", reject: "Вернуть",
    review_comment: "Комментарий к ревью", p0: "низкий", p1: "обычный", p2: "высокий", p3: "критичный",
    set_goal: "Глобальная цель роя", goal_title: "Цель", goal_desc: "Подробности, критерии успеха, ограничения",
    goal_summary: "Итог роя", reopen: "Вернуть в работу", reopen_comment: "Что ещё нужно сделать?",
    hire_title: "Нанять агента", hire_btn: "Нанять", role: "Роль", instructions: "Инструкции", model: "Профиль модели",
    name_optional: "Имя (необязательно)", settings: "Настройки роя", max_cost: "Бюджет, $ (0 — без лимита)",
    max_agents: "Максимум агентов", retire: "Уволить", retire_confirm: "Вывести агента из роя?",
    no_messages: "Сообщений пока нет", no_activity: "Активности пока нет", created_by: "Создал(а)", updated: "изменено",
    approve_mcp: "Одобрить", reject_mcp: "Отклонить", tools: "Инструменты", no_mcp: "MCP-серверов пока нет. Агенты создают их сами через mcp_create.",
    no_triggers: "Активных триггеров нет", delete: "Удалить", owner: "Владелец", fired: "срабатываний",
    mcp_hint: "Подключить рой к Claude Code:", copy_hint: "Нажмите, чтобы скопировать", saved: "Сохранено", copied: "Скопировано",
    tab_traces: "Трейсы", traces: "Трейсы", sessions: "Все сессии", session_stopped: "Сессия остановлена: агенты не работают, доска только для просмотра.",
    start_session: "Запустить сессию",
    open_task: "Открыть", disconnected: "Нет связи с хабом — переподключаюсь…", live: "Живые обновления подключены",
    review_needed: "Задача ждёт ревью",
    review_drop: "На ревью задачу отправляет исполнитель, когда завершает её с назначенным ревьюером",
    required: "Заполните обязательные поля", required_task: "Заполните заголовок и цель задачи",
    invalid_limits: "Бюджет должен быть ≥ 0, а максимум агентов — целым числом ≥ 1",
    discard_changes: "Закрыть без сохранения? Введённый текст пропадёт.",
    truncated: "(обрезано)", system: "система", theme: "Тема: светлая / тёмная", language: "Язык интерфейса",
    collapse_team: "Свернуть панель команды", expand_team: "Развернуть панель команды",
    k_wake: "пробуждение", k_thinking: "размышление", k_text: "ответ", k_tool_call: "вызов инструмента",
    k_tool_result: "результат", k_error: "ошибка", k_nudge: "напоминание", k_compacted: "сжатие контекста",
    mcp_active: "работает", mcp_pending: "ждёт одобрения", mcp_failed: "ошибка", mcp_rejected: "отклонён",
    mcp_available: "в библиотеке", mcp_removed: "удалён",
    origin_workspace: "создан роем", origin_config: "из конфига", origin_library: "из библиотеки",
    scope_private: "личный", scope_task: "для задачи", scope_swarm: "для всего роя",
    reject_reason: "Причина отказа (необязательно)", trigger_delete_confirm: "Удалить триггер #{id}?",
    periodic: "повторяющийся", retired_chat: "Агент покинул рой — написать ему нельзя",
    f_title: "заголовок", f_goal: "цель", f_description: "описание", f_status: "статус", f_assignee: "исполнитель",
    f_reviewer: "ревьюер", f_priority: "приоритет", f_tags: "теги", f_depends_on: "зависимости", f_result: "итог",
    f_parent_id: "родитель",
    op_create: "создал(а) файл", op_write: "перезаписал(а) файл", op_edit: "изменил(а) файл",
    locked_by: "Заблокирован: {owner}", select_file: "Выберите файл слева", workspace: "workspace",
    "reason_budget": "исчерпан бюджет — увеличьте его в настройках", "reason_paused by the user": "остановлен вами",
  },
  en: {
    goal: "Goal", budget: "Budget", budget_hint: "Spent / budget limit", agents: "Agents", team: "Team",
    hire: "+ Hire", everyone: "All messages",
    broadcast_hint: "swarm feed and message to all", tab_board: "Board", tab_feed: "Feed", tab_files: "Files",
    tab_mcp: "MCP", tab_triggers: "Triggers", search: "Search tasks…", new_task: "+ Task", chat: "Chat",
    activity: "Activity", type_message: "Message… (Ctrl+Enter to send)", send: "Send",
    pause: "Pause", resume: "Resume", paused: "The swarm is paused", no_goal: "No goal yet — click to set one",
    goal_active: "active", goal_done: "achieved", s_todo: "To do", s_in_progress: "In progress", s_waiting: "Waiting",
    s_review: "Review", s_done: "Done", s_failed: "Failed", s_cancelled: "Cancelled", s_closed: "Closed",
    a_idle: "idle", a_working: "working", a_paused: "paused", a_error: "error", a_retired: "retired",
    empty: "empty", blocked_by: "blocked by", task: "Task", title: "Title", goal_field: "Task goal",
    goal_hint: "Why the task exists and what counts as success", description: "Description", status: "Status",
    assignee: "Assignee", reviewer: "Reviewer", priority: "Priority", tags: "Tags (comma-separated)",
    depends_on: "Depends on tasks (ids, comma-separated)", parent: "Parent task", none: "— none —",
    user: "user (you)", save: "Save", cancel: "Cancel", create: "Create", close: "Close",
    result: "Result", discussion: "Discussion", history: "History", write_post: "Write to the discussion… (@Name to mention, Ctrl+Enter to post)",
    post: "Post", reply: "Reply", replying_to: "Replying to", reply_to_post: "Go to the post this replies to",
    approve: "Approve", reject: "Return",
    review_comment: "Review comment", p0: "low", p1: "normal", p2: "high", p3: "critical",
    set_goal: "Global goal of the swarm", goal_title: "Goal", goal_desc: "Details, success criteria, constraints",
    goal_summary: "Swarm summary", reopen: "Reopen", reopen_comment: "What else has to be done?",
    hire_title: "Hire an agent", hire_btn: "Hire", role: "Role", instructions: "Instructions", model: "Model profile",
    name_optional: "Name (optional)", settings: "Swarm settings", max_cost: "Budget, $ (0 = unlimited)",
    max_agents: "Max agents", retire: "Retire", retire_confirm: "Remove this agent from the swarm?",
    no_messages: "No messages yet", no_activity: "No activity yet", created_by: "Created by", updated: "updated",
    approve_mcp: "Approve", reject_mcp: "Reject", tools: "Tools", no_mcp: "No MCP servers yet. Agents build them with mcp_create.",
    no_triggers: "No active triggers", delete: "Delete", owner: "Owner", fired: "fired",
    mcp_hint: "Connect the swarm to Claude Code:", copy_hint: "Click to copy", saved: "Saved", copied: "Copied",
    tab_traces: "Traces", traces: "Traces", sessions: "All sessions", session_stopped: "The session is stopped: agents are not working, the board is read-only.",
    start_session: "Start the session",
    open_task: "Open", disconnected: "Lost connection to the hub — reconnecting…", live: "Live updates connected",
    review_needed: "Task waits for review",
    review_drop: "A task goes to review when its assignee completes it with a reviewer set",
    required: "Fill in the required fields", required_task: "Fill in the title and the task goal",
    invalid_limits: "The budget must be ≥ 0 and max agents a whole number ≥ 1",
    discard_changes: "Close without saving? Your text will be lost.",
    truncated: "(truncated)", system: "system", theme: "Theme: light / dark", language: "Interface language",
    collapse_team: "Collapse the team panel", expand_team: "Expand the team panel",
    k_wake: "wake-up", k_thinking: "thinking", k_text: "reply", k_tool_call: "tool call",
    k_tool_result: "tool result", k_error: "error", k_nudge: "nudge", k_compacted: "context compacted",
    mcp_active: "active", mcp_pending: "pending approval", mcp_failed: "failed", mcp_rejected: "rejected",
    mcp_available: "in the library", mcp_removed: "removed",
    origin_workspace: "built by the swarm", origin_config: "from the config", origin_library: "from the library",
    scope_private: "private", scope_task: "task", scope_swarm: "whole swarm",
    reject_reason: "Reason (optional)", trigger_delete_confirm: "Delete trigger #{id}?",
    periodic: "periodic", retired_chat: "This agent has left the swarm — you cannot message them",
    f_title: "title", f_goal: "goal", f_description: "description", f_status: "status", f_assignee: "assignee",
    f_reviewer: "reviewer", f_priority: "priority", f_tags: "tags", f_depends_on: "dependencies", f_result: "result",
    f_parent_id: "parent",
    op_create: "created file", op_write: "rewrote file", op_edit: "edited file",
    locked_by: "Locked by {owner}", select_file: "Select a file on the left", workspace: "workspace",
    "reason_budget": "the budget is spent — raise it in the settings", "reason_paused by the user": "paused by you",
  },
};

const COLUMNS = ["todo", "in_progress", "waiting", "review", "done", "closed"];
const ALL_STATUSES = ["todo", "in_progress", "waiting", "review", "done", "failed", "cancelled"];
const ACTIVITY_ICONS = { wake: "⏰", thinking: "💭", text: "💬", tool_call: "🔧", tool_result: "📄", error: "⚠️", nudge: "👉", compacted: "🗜" };
// the feed asks the server only for these types: agent.activity / agent.usage would crowd everything else out
const FEED_TYPES = ["task.*", "message.*", "goal.*", "agent.hired", "agent.retired", "file.*", "trigger.*", "mcp.*", "swarm.*"];
const FEED_SKIP = new Set(["agent.activity", "agent.usage", "notification", "agent.status", "agent.compacted"]);
const DND_TYPE = "application/x-dreamteam-task";
const MAX_SEEN = 5000;
const MAX_NODES = 800;
const RAIL_QUERY = "(max-width: 1279px)";
const NARROW_QUERY = "(max-width: 900px)";
const S = {
  state: null, lang: "ru", tab: "board", chatWith: null, dtab: "chat", fileDir: ".", fileEntries: [], openFile: null,
  search: "", feed: [], feedMaxId: 0, activityMaxId: 0, seenEvents: new Set(), userInbox: [], replyTo: null,
  openTaskId: null, taskBase: null, lastPostId: 0, ws: null, wsRetry: 0, wsEverOpen: false, dragging: null,
  boardDirty: false, appliedSeq: 0, modalInitial: null, markingRead: new Set(), drafts: {},
};

/* ---------------------------------------------------------------- utils */
const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function t(key, vars) {
  let text = (I18N[S.lang] && I18N[S.lang][key]) || I18N.en[key] || key;
  if (vars) text = text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
  return text;
}
/** Translation of a dynamic key (`prefix + value`), falling back to the raw value when there is none. */
function tv(prefix, value) { return I18N.en[prefix + value] ? t(prefix + value) : String(value ?? ""); }
function store(key, value) { try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch (e) { return null; } return null; }
function setHtml(el, html) { if (el && el._html !== html) { el.innerHTML = html; el._html = html; } }
function clearHtml(el) { if (el) { el.innerHTML = ""; el._html = ""; } }
function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const today = new Date();
  const hm = d.toLocaleTimeString(S.lang === "ru" ? "ru-RU" : "en-GB", { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === today.toDateString() ? hm : d.toLocaleDateString(S.lang === "ru" ? "ru-RU" : "en-GB") + " " + hm;
}
function hue(name) { let h = 0; for (const ch of String(name)) h = (h * 31 + ch.codePointAt(0)) % 360; return h; }
function initials(name) { return String(name).split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0].toUpperCase()).join(""); }
function avatar(name, cls = "") {
  if (name === "user") return `<span class="avatar user ${cls}">U</span>`;
  return `<span class="avatar ${cls}" style="background:hsl(${hue(name)} 55% 45%)">${esc(initials(name))}</span>`;
}
function displayName(name) { return name === "user" ? t("user") : name === null || name === undefined ? t("system") : name; }
function reEscape(text) { return String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
function norm(text) { return String(text ?? "").replace(/\r\n?/g, "\n"); }
function short(text, n = 160) { text = String(text || "").replace(/\s+/g, " "); return text.length > n ? text.slice(0, n - 1) + "…" : text; }

/** Wraps `@Full Name` mentions of known members (and `@user`) in already escaped HTML. */
function highlightMentions(html, names) {
  const list = (names || members(true).map((a) => a.name)).concat(["user"]).filter(Boolean);
  const alts = Array.from(new Set(list)).sort((a, b) => b.length - a.length).map((n) => reEscape(esc(n)).replace(/\s+/g, "\\s+"));
  const re = new RegExp(`(^|[^\\p{L}\\p{N}_&])@(${alts.join("|")})(?![\\p{L}\\p{N}_])`, "giu");
  return html.replace(re, '$1<span class="mention">@$2</span>');
}
/** Escapes text, turns `#12` into task links and highlights mentions. Everything except the inserted markup is escaped. */
function linkify(text, names) {
  const html = esc(text).replace(/(^|[\s(\[,:])#(\d+)\b/g, '$1<button type="button" class="link" data-open-task="$2">#$2</button>');
  return highlightMentions(html, names);
}

function toast(text, kind = "", onClick = null) {
  const el = document.createElement("div");
  el.className = "toast " + kind + (onClick ? " clickable" : "");
  el.textContent = text;
  if (onClick) el.addEventListener("click", () => { el.remove(); onClick(); });
  const box = $("#toasts");
  box.appendChild(el);
  while (box.children.length > 5) box.firstElementChild.remove();
  setTimeout(() => el.remove(), kind === "err" ? 9000 : 6000);
}
// The board of one session lives under /s/{sid}/ on the launcher; a standalone swarm app serves it at /.
const BASE = typeof location === "undefined" ? "" : (location.pathname.match(/^\/s\/[a-z0-9][a-z0-9-]{0,63}(?=\/|$)/) || [""])[0];
/** Readable text of a failed response: the JSON `error` / `result` field when there is one. */
function responseError(status, body) {
  let data = null;
  try { data = JSON.parse(body); } catch (e) { /* not JSON */ }
  const text = data && typeof data === "object" && (data.error || data.result);
  return typeof text === "string" && text ? text : `${status} ${String(body || "").slice(0, 300)}`.trim();
}
function errText(e) { return (e && e.message) || String(e); }
async function api(path, options) {
  const resp = await fetch(path.startsWith("/api/") ? BASE + path : path, options);
  if (!resp.ok) throw new Error(responseError(resp.status, await resp.text()));
  return resp.json();
}
async function post(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
}
async function act(tool, args, quiet = false) {
  try {
    const res = await post(`/api/act/${tool}`, args || {});
    if (!res.ok) toast(res.result, "err");
    else if (!quiet) toast(res.result);
    scheduleRefresh();
    return res;
  } catch (e) {
    // a stopped session answers every action with 409 (English text from the launcher): say it in the UI language
    const stopped = S.state && S.state.swarm && S.state.swarm.session && S.state.swarm.session.status === "stopped";
    const text = stopped ? t("session_stopped") : errText(e);
    toast(text, "err");
    return { ok: false, result: text };
  }
}
function members(includeRetired = false) {
  return (S.state?.agents || []).filter((a) => includeRetired || a.status !== "retired");
}
/** <option>s for an assignee/reviewer select. The current value is always present, even when it is a
 * retired member: otherwise the select silently shows "none" and saving the form would unassign. */
function memberOptions(selected, withUser = true) {
  selected = selected || "";
  const opts = [`<option value=""${selected ? "" : " selected"}>${esc(t("none"))}</option>`];
  const seen = new Set([""]);
  if (withUser) { opts.push(`<option value="user"${selected === "user" ? " selected" : ""}>${esc(t("user"))}</option>`); seen.add("user"); }
  for (const a of members()) {
    opts.push(`<option value="${esc(a.name)}"${selected === a.name ? " selected" : ""}>${esc(a.name)} — ${esc(a.role)}</option>`);
    seen.add(a.name);
  }
  if (!seen.has(selected)) {
    const m = members(true).find((x) => x.name === selected);
    opts.push(`<option value="${esc(selected)}" selected>${esc(selected)}${m ? ` (${esc(t("a_" + m.status))})` : ""}</option>`);
  }
  return opts.join("");
}
/** Status <option>s: "review" is only reachable through task_complete, so it is disabled unless current. */
function statusOptions(current) {
  return ALL_STATUSES.map((s) => `<option value="${s}"${s === current ? " selected" : ""}${s === "review" && current !== "review" ? " disabled" : ""}>${esc(t("s_" + s))}</option>`).join("");
}
function priorityOptions(selected) {
  return [0, 1, 2, 3].map((p) => `<option value="${p}"${Number(selected) === p ? " selected" : ""}>${esc(t("p" + p))}</option>`).join("");
}
function parseIds(text) { return String(text || "").split(/[,\s]+/).map((x) => x.replace("#", "")).filter(Boolean).map(Number).filter((n) => Number.isInteger(n)); }
function parseTags(text) { return String(text || "").split(",").map((x) => x.trim()).filter(Boolean); }

/**
 * A timer that runs `fn` at most once per window and is never starved: a pending run is only moved
 * earlier, never later. (A plain debounce that restarts on every event never fires while a busy
 * swarm emits events faster than the delay.)
 */
function makeScheduler(fn, clock) {
  const c = clock || { setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (h) => clearTimeout(h), now: () => Date.now() };
  let timer = null;
  let due = 0;
  return function schedule(delay = 250) {
    const at = c.now() + delay;
    if (timer !== null && due <= at) return;
    if (timer !== null) c.clearTimeout(timer);
    due = at;
    timer = c.setTimeout(() => { timer = null; fn(); }, delay);
  };
}

/* ---------------------------------------------------------------- state & refresh */
const scheduleRefresh = makeScheduler(() => refresh());
let refreshSeq = 0;
async function refresh() {
  const seq = ++refreshSeq;
  try {
    const [state, inbox] = await Promise.all([api("/api/state"), api("/api/inbox")]);
    if (seq < S.appliedSeq) return; // a newer response was already applied
    S.appliedSeq = seq;
    S.state = state;
    S.userInbox = inbox.messages || [];
    if (!store("dt-lang") && state.swarm.language && S.lang !== state.swarm.language) setLang(state.swarm.language, false);
    renderAll();
    autoMarkRead();
  } catch (e) {
    console.error(e);
  }
}

function applyI18n() {
  document.documentElement.lang = S.lang;
  $$("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  $$("[data-i18n-placeholder]").forEach((el) => { el.placeholder = t(el.dataset.i18nPlaceholder); });
  $$("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); el.setAttribute("aria-label", el.title); });
  $("#lang-btn").textContent = S.lang.toUpperCase();
  if (!S.openFile) $("#file-path").textContent = t("select_file");
  setConnected(!!(S.ws && S.ws.readyState === 1), true);
  applyTeamLayout();
}

function setLang(lang, remember = true) {
  S.lang = lang;
  if (remember) store("dt-lang", lang);
  applyI18n();
  renderAll();
  renderFeed();
  if (S.tab === "files") { renderCrumbs(); renderFileList(); }
  if (S.chatWith) { loadMessages(); if (S.dtab === "activity") loadActivity(); }
  if (typeof tracesLangChanged === "function") tracesLangChanged();
}

function renderAll() {
  if (!S.state) return;
  renderHeader();
  renderAgents();
  if (S.tab === "board") renderBoard();
  if (S.tab === "mcp") renderMcp();
  if (S.tab === "triggers") renderTriggers();
  if (S.tab === "files") renderFileList();
  if (S.chatWith) renderDrawerHead();
}

/* ---------------------------------------------------------------- header */
function renderHeader() {
  const sw = S.state.swarm;
  const session = sw.session || null;
  const title = (session && session.title) || sw.name;
  $("#swarm-name").textContent = title;
  document.title = `DreamTeam — ${title}`;
  $("#back-link").classList.toggle("hidden", !BASE);
  $("#stopped-banner").classList.toggle("hidden", !(session && session.status === "stopped"));
  const goal = sw.goal;
  $("#goal-title").textContent = goal ? goal.title : t("no_goal");
  $("#goal-btn").title = goal ? goal.title + (goal.description ? "\n\n" + goal.description : "") : t("no_goal");
  const gs = $("#goal-status");
  gs.textContent = goal ? (goal.status === "done" ? t("goal_done") : t("goal_active")) : "";
  gs.className = "badge" + (goal && goal.status === "done" ? " done" : "");
  gs.classList.toggle("hidden", !goal);
  const limit = sw.limits.max_cost_usd;
  $("#budget-text").textContent = `$${(sw.spent_usd || 0).toFixed(2)}` + (limit > 0 ? ` / $${limit.toFixed(2)}` : " / ∞");
  const pct = limit > 0 ? Math.min(100, (sw.spent_usd / limit) * 100) : 0;
  const bar = $("#budget-bar");
  bar.style.width = pct + "%";
  bar.className = pct >= 100 ? "err" : pct >= 80 ? "warn" : "";
  $("#agents-count").textContent = `${members().length} / ${sw.limits.max_agents}`;
  const pause = $("#pause-btn");
  setHtml(pause, `<span aria-hidden="true">${sw.paused ? "▶" : "⏸"}</span><span class="btn-label">${esc(sw.paused ? t("resume") : t("pause"))}</span>`);
  pause.title = sw.paused ? t("resume") : t("pause");
  pause.classList.toggle("primary", !!sw.paused);
  const banner = $("#paused-banner");
  banner.classList.toggle("hidden", !sw.paused);
  banner.textContent = sw.paused ? `${t("paused")}${sw.pause_reason ? ": " + tv("reason_", sw.pause_reason) : ""}` : "";
  const mcpUrl = sw.mcp_url || `${location.origin}${BASE}/mcp`;
  setHtml($("#mcp-hint"), `${esc(t("mcp_hint"))}<br><code id="mcp-cmd" title="${esc(t("copy_hint"))}">claude mcp add --transport http dreamteam ${esc(mcpUrl)}</code>`);
  const pending = (S.state.mcp || []).filter((s) => s.status === "pending").length;
  const mcpBadge = $("#mcp-badge");
  mcpBadge.textContent = pending || "";
  mcpBadge.classList.toggle("hidden", !pending);
  const triggers = (S.state.triggers || []).length;
  const trBadge = $("#triggers-badge");
  trBadge.textContent = triggers || "";
  trBadge.classList.toggle("hidden", !triggers);
  const unread = S.userInbox.filter((m) => !m.read).length;
  const teamBadge = $("#team-unread");
  teamBadge.textContent = unread || "";
  teamBadge.classList.toggle("hidden", !unread);
}

/* ---------------------------------------------------------------- board */
function taskMatches(task) {
  if (!S.search) return true;
  const hay = [task.title, task.goal, task.description, task.assignee, (task.tags || []).join(" "), "#" + task.id].join(" ").toLowerCase();
  return hay.includes(S.search.toLowerCase());
}
function columnOf(task) { return task.status === "failed" || task.status === "cancelled" ? "closed" : task.status; }
/** What dropping `task` on column `col` does: null (nothing), {error: i18n key} or {status}. */
function dropAction(task, col) {
  if (!task || !COLUMNS.includes(col) || columnOf(task) === col) return null;
  if (col === "review") return { error: "review_drop" };
  return { status: col === "closed" ? "cancelled" : col };
}

function boardHtml() {
  const tasks = (S.state.tasks || []).filter(taskMatches);
  return COLUMNS.map((col) => {
    const items = tasks.filter((x) => columnOf(x) === col)
      .sort((a, b) => (col === "done" || col === "closed") ? b.updated_at - a.updated_at : (b.priority - a.priority) || (a.id - b.id));
    return `<div class="column${items.length ? "" : " is-empty"}" data-col="${col}">
      <div class="column-head"><span class="dot" style="background:var(--c-${col === "closed" ? "failed" : col})"></span>${esc(t("s_" + col))}<span class="count">${items.length}</span></div>
      <div class="column-body">${items.map(cardHtml).join("") || `<div class="empty">${esc(t("empty"))}</div>`}</div>
    </div>`;
  }).join("");
}

function renderBoard() {
  if (S.dragging && Date.now() - S.dragging.since < 30000) { S.boardDirty = true; return; }
  const board = $("#board");
  const html = boardHtml();
  if (board._html === html) return; // nothing changed: keep the DOM (hover, focus, scroll) as it is
  const scrolls = {};
  $$(".column", board).forEach((c) => { scrolls[c.dataset.col] = $(".column-body", c).scrollTop; });
  setHtml(board, html);
  $$(".column", board).forEach((c) => { if (scrolls[c.dataset.col]) $(".column-body", c).scrollTop = scrolls[c.dataset.col]; });
  board.style.setProperty("--cols", boardColumns((S.state.tasks || []).filter(taskMatches)));
}

/** Grid columns of the board: empty columns get less room so the cards have more. */
function boardColumns(tasks) {
  const used = new Set(tasks.map(columnOf));
  return COLUMNS.map((c) => (used.has(c) ? "minmax(140px, 1fr)" : "minmax(110px, .6fr)")).join(" ");
}

function cardHtml(task) {
  const blocked = (task.blocked_by || []).length && task.status === "todo"
    ? `<span class="blocked">⛔ ${esc(t("blocked_by"))} ${task.blocked_by.map((b) => "#" + Number(b)).join(", ")}</span>` : "";
  const assignee = task.assignee ? `<span class="chip">${avatar(task.assignee, "sm")}${esc(displayName(task.assignee))}</span>` : "";
  const tags = (task.tags || []).map((g) => `<span class="tag">${esc(g)}</span>`).join("");
  const comments = task.comments ? `<span class="chip plain">💬 ${Number(task.comments)}</span>` : "";
  const review = task.status === "review" && task.reviewer ? `<span class="chip plain">👁 ${esc(displayName(task.reviewer))}</span>` : "";
  const failed = task.status === "failed" ? `<span class="badge err">${esc(t("s_failed"))}</span>` : task.status === "cancelled" ? `<span class="badge">${esc(t("s_cancelled"))}</span>` : "";
  const prio = Math.max(0, Math.min(3, Number(task.priority) || 0));
  return `<div class="card p${prio}" draggable="true" tabindex="0" role="button" data-id="${Number(task.id)}" title="${esc(t("p" + prio))}">
    <div class="card-top"><span class="card-id">#${Number(task.id)}</span><span class="card-title">${esc(task.title)}</span></div>
    ${task.goal ? `<div class="card-goal">🎯 ${esc(task.goal)}</div>` : ""}
    <div class="card-meta">${assignee}${review}${comments}${tags}${blocked}${failed}</div>
  </div>`;
}

function wireBoard() {
  const board = $("#board");
  board.addEventListener("click", (e) => {
    const card = e.target.closest(".card");
    if (card && !e.target.closest("[data-open-task]")) openTask(Number(card.dataset.id));
  });
  board.addEventListener("keydown", (e) => {
    const card = e.target.closest(".card");
    if (card && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openTask(Number(card.dataset.id)); }
  });
  board.addEventListener("dragstart", (e) => {
    const card = e.target.closest(".card");
    if (!card) return;
    S.dragging = { id: Number(card.dataset.id), since: Date.now() };
    e.dataTransfer.setData(DND_TYPE, card.dataset.id);
    e.dataTransfer.setData("text/plain", "#" + card.dataset.id);
    e.dataTransfer.effectAllowed = "move";
    card.classList.add("dragging");
  });
  board.addEventListener("dragend", () => {
    S.dragging = null;
    $$(".column.drop, .card.dragging", board).forEach((el) => el.classList.remove("drop", "dragging"));
    if (S.boardDirty) { S.boardDirty = false; renderBoard(); }
  });
  board.addEventListener("dragover", (e) => {
    const col = e.target.closest(".column");
    if (!col || !Array.from(e.dataTransfer.types || []).includes(DND_TYPE)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    $$(".column.drop", board).forEach((c) => { if (c !== col) c.classList.remove("drop"); });
    col.classList.add("drop");
  });
  board.addEventListener("dragleave", (e) => {
    const col = e.target.closest(".column");
    if (col && !col.contains(e.relatedTarget)) col.classList.remove("drop");
  });
  board.addEventListener("drop", (e) => {
    const col = e.target.closest(".column");
    if (!col) return;
    e.preventDefault();
    col.classList.remove("drop");
    const id = Number(e.dataTransfer.getData(DND_TYPE));
    if (!id) return;
    moveTask(id, col.dataset.col);
  });
}

function moveTask(id, col) {
  const task = (S.state.tasks || []).find((x) => x.id === id);
  const action = dropAction(task, col);
  if (!action) return;
  if (action.error) { toast(t(action.error), "err"); return; }
  act("task_update", { task_id: id, status: action.status }, true);
}

/* ---------------------------------------------------------------- agents sidebar */
function unreadFrom(name) { return S.userInbox.filter((m) => !m.read && m.sender === name).length; }

function renderAgents() {
  const list = members(true).sort((a, b) => (a.status === "retired") - (b.status === "retired"));
  setHtml($("#agent-list"), list.map((a) => {
    const unread = unreadFrom(a.name);
    const tasks = (a.open_tasks || []).map((id) => "#" + Number(id)).join(", ");
    const status = t("a_" + a.status);
    const tip = `${a.name} — ${a.role} · ${status}${tasks ? " · " + tasks : ""}`;
    return `<button type="button" class="agent ${a.status === "retired" ? "retired" : ""}${S.chatWith === a.name ? " active" : ""}" data-agent="${esc(a.name)}" title="${esc(tip)}">
      <span class="avatar-wrap">${avatar(a.name)}<span class="status-dot ${esc(a.status)}"></span></span>
      <span class="agent-info"><span class="agent-name">${esc(a.name)}</span><span class="agent-role">${esc(a.role)}${a.kind === "external" ? " · MCP" : ""}</span></span>
      <span class="agent-side"><span>${esc(status)}</span><span>${esc(tasks)} $${(a.cost_usd || 0).toFixed(2)}</span></span>
      ${unread ? `<span class="unread">${unread}</span>` : ""}
    </button>`;
  }).join(""));
}

function isNarrow() { return typeof window !== "undefined" && window.matchMedia && window.matchMedia(NARROW_QUERY).matches; }
function applyTeamLayout() {
  const pref = store("dt-team");
  const rail = pref ? pref === "rail" : !!(window.matchMedia && window.matchMedia(RAIL_QUERY).matches);
  document.body.classList.toggle("team-rail", rail);
  const btn = $("#team-toggle");
  btn.textContent = rail ? "«" : "»";
  btn.title = t(rail ? "expand_team" : "collapse_team");
  btn.setAttribute("aria-label", btn.title);
}
function toggleTeamOverlay(open) {
  document.body.classList.toggle("team-open", open === undefined ? !document.body.classList.contains("team-open") : open);
}

/* ---------------------------------------------------------------- drawer (chat + activity) */
function renderDrawerHead() {
  const name = S.chatWith;
  if (!name) return;
  let html;
  const a = members(true).find((x) => x.name === name);
  if (name === "all") {
    html = `<span class="avatar all">∗</span><div class="drawer-who"><b>${esc(t("everyone"))}</b><div class="sub">${esc(t("broadcast_hint"))}</div></div>`;
  } else if (!a) {
    html = `${avatar(name)}<div class="drawer-who"><b>${esc(displayName(name))}</b></div>`;
  } else {
    html = `${avatar(a.name)}<div class="drawer-who"><b>${esc(a.name)}</b>
      <div class="sub">${esc(a.role)} · <span class="status-dot ${esc(a.status)}"></span> ${esc(t("a_" + a.status))} · ${esc(a.model || a.kind)} · $${(a.cost_usd || 0).toFixed(3)}</div></div>
      <button class="btn small" type="button" data-traces="${esc(a.name)}">📈 ${esc(t("traces"))}</button>
      ${a.status !== "retired" ? `<button class="btn small danger" type="button" data-retire="${esc(a.name)}">${esc(t("retire"))}</button>` : ""}`;
  }
  setHtml($("#drawer-title"), html);
  const retired = !!a && a.status === "retired";
  const box = $("#composer-text");
  box.disabled = retired;
  box.placeholder = retired ? t("retired_chat") : t("type_message");
  $("#composer button[type=submit]").disabled = retired;
}

async function openChat(name) {
  const box = $("#composer-text");
  if (S.chatWith !== name) {
    if (S.chatWith) S.drafts[S.chatWith] = box.value;
    box.value = S.drafts[name] || "";
    clearHtml($("#messages"));
    clearHtml($("#activity"));
    S.activityMaxId = 0;
  }
  S.chatWith = name;
  toggleTeamOverlay(false);
  $("#drawer").classList.remove("hidden");
  $$(".drawer-tabs button").forEach((b) => b.classList.toggle("hidden", name === "all" && b.dataset.dtab === "activity"));
  if (name === "all" && S.dtab !== "chat") setDrawerTab("chat", false);
  renderDrawerHead();
  if (S.state) renderAgents();
  await loadMessages(true);
  if (S.dtab === "activity") await loadActivity(true);
  if (!box.disabled && !isNarrow()) box.focus();
}

function closeDrawer() {
  if (S.chatWith) S.drafts[S.chatWith] = $("#composer-text").value;
  $("#drawer").classList.add("hidden");
  S.chatWith = null;
  if (S.state) renderAgents();
}

function setDrawerTab(tab, load = true) {
  S.dtab = tab;
  $$(".drawer-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.dtab === tab));
  $("#drawer-chat").classList.toggle("hidden", tab !== "chat");
  $("#drawer-activity").classList.toggle("hidden", tab !== "activity");
  if (load && tab === "activity") loadActivity(true);
  if (load && tab === "chat") loadMessages(true);
}

function messageHtml(m) {
  const me = m.sender === "user";
  const to = S.chatWith === "all" ? ` → ${(m.recipients || []).map(displayName).map(esc).join(", ")}` : "";
  return `<div class="msg ${me ? "me" : ""}"><div class="meta">${esc(displayName(m.sender))}${to} · ${esc(fmtTime(m.ts))}</div>${linkify(m.text)}</div>`;
}

function nearBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 80; }

async function loadMessages(toBottom = false) {
  const name = S.chatWith;
  if (!name) return;
  const url = name === "all" ? "/api/messages?limit=300" : `/api/messages?with=${encodeURIComponent(name)}&limit=300`;
  try {
    const data = await api(url);
    if (S.chatWith !== name) return;
    const body = $("#drawer-chat");
    const stick = toBottom || nearBottom(body);
    setHtml($("#messages"), (data.messages || []).map(messageHtml).join("") || `<div class="empty">${esc(t("no_messages"))}</div>`);
    if (stick) body.scrollTop = body.scrollHeight;
    autoMarkRead();
  } catch (e) { toast(errText(e), "err"); }
}
const scheduleMessages = makeScheduler(() => loadMessages());

/** Messages from the agent whose chat is open are read: tell the hub so the unread badge goes away. */
function autoMarkRead() {
  const name = S.chatWith;
  if (!name || name === "all" || (typeof document !== "undefined" && document.hidden)) return;
  if (!unreadFrom(name) || S.markingRead.has(name)) return;
  S.markingRead.add(name);
  post("/api/read", { with: name })
    .then(() => { S.userInbox.forEach((m) => { if (m.sender === name) m.read = true; }); scheduleRefresh(); })
    .catch((e) => console.error(e))
    .finally(() => S.markingRead.delete(name));
}

function activityHtml(item) {
  const d = item.data || {};
  const kind = String(d.kind || "");
  const head = `${ACTIVITY_ICONS[kind] || "•"} ${esc(tv("k_", kind))}${d.tool ? ` · <code>${esc(d.tool)}</code>` : ""} · ${esc(fmtTime(item.ts))}`;
  return `<div class="act ${esc(kind)}${d.is_error ? " err" : ""}"><div class="act-head">${head}</div>${d.text ? `<pre>${esc(d.text)}</pre>` : ""}</div>`;
}

async function loadActivity(toBottom = false) {
  const name = S.chatWith;
  if (!name || name === "all") return;
  try {
    const data = await api(`/api/agents/${encodeURIComponent(name)}/activity?limit=300`);
    if (S.chatWith !== name) return;
    const items = data.activity || [];
    const body = $("#drawer-activity");
    const stick = toBottom || nearBottom(body);
    S.activityMaxId = items.reduce((m, x) => Math.max(m, Number(x.id) || 0), 0);
    setHtml($("#activity"), items.map(activityHtml).join("") || `<div class="empty">${esc(t("no_activity"))}</div>`);
    if (stick) body.scrollTop = body.scrollHeight;
  } catch (e) { toast(errText(e), "err"); }
}

function appendActivity(ev) {
  if (ev.id <= S.activityMaxId) return; // already part of the loaded history
  S.activityMaxId = ev.id;
  const box = $("#activity");
  const body = $("#drawer-activity");
  const stick = nearBottom(body);
  if (box.querySelector(".empty")) box.innerHTML = "";
  box.insertAdjacentHTML("beforeend", activityHtml(ev));
  while (box.children.length > MAX_NODES) box.firstElementChild.remove();
  box._html = null;
  if (stick) body.scrollTop = body.scrollHeight;
}

async function sendComposer() {
  const box = $("#composer-text");
  const text = box.value.trim();
  const name = S.chatWith;
  if (!text || !name || box.disabled) return;
  const btn = $("#composer button[type=submit]");
  btn.disabled = true;
  const res = await act("send_message", { to: [name === "all" ? "all" : name], text }, true);
  btn.disabled = false;
  if (res.ok) {
    if (S.chatWith === name && box.value.trim() === text) box.value = "";
    delete S.drafts[name];
    loadMessages(true);
  }
}

/* ---------------------------------------------------------------- modal helpers */
function openModal(html, { focus = true } = {}) {
  S.openTaskId = null;
  S.taskBase = null;
  S.replyTo = null;
  const modal = $("#modal");
  modal.classList.remove("wide"); // the trace context modal widens it; every other modal is normal
  modal.innerHTML = html;
  const backdrop = $("#modal-backdrop");
  backdrop.classList.remove("hidden");
  backdrop.scrollTop = 0;
  S.modalInitial = new Map($$("input, textarea, select", modal).filter((el) => el.id).map((el) => [el.id, el.value]));
  if (focus) { const first = $("#modal input, #modal textarea"); if (first) first.focus(); }
}
function modalOpen() { return !$("#modal-backdrop").classList.contains("hidden"); }
/** True when closing the modal would throw away something the user typed. */
function modalHasUnsaved() {
  if (!modalOpen()) return false;
  if (S.taskBase) {
    if (($("#post-text")?.value || "").trim() || ($("#review-comment")?.value || "").trim()) return true;
    return Object.keys(dirtyFields(S.taskBase, readTaskForm())).length > 0;
  }
  return $$("#modal input, #modal textarea").some((el) => el.id && S.modalInitial && S.modalInitial.has(el.id)
    && el.value !== S.modalInitial.get(el.id) && el.value.trim() !== "");
}
function closeModal(force = false) {
  if (!force && modalHasUnsaved() && !confirm(t("discard_changes"))) return false;
  $("#modal-backdrop").classList.add("hidden");
  $("#modal").innerHTML = "";
  $("#modal").classList.remove("wide");
  S.openTaskId = null;
  S.taskBase = null;
  S.replyTo = null;
  return true;
}

/* ---------------------------------------------------------------- task modal */
// form element id -> the value the form shows for that field of a task
const TASK_FORM = {
  "f-title": (task) => norm(task.title),
  "f-goal": (task) => norm(task.goal),
  "f-desc": (task) => norm(task.description),
  "f-status": (task) => task.status,
  "f-priority": (task) => String(task.priority ?? 1),
  "f-assignee": (task) => task.assignee || "",
  "f-reviewer": (task) => task.reviewer || "",
  "f-tags": (task) => (task.tags || []).join(", "),
  "f-deps": (task) => (task.depends_on || []).join(", "),
};
/** Form fields whose current value differs from what the form showed for `base` (= unsaved edits). */
function dirtyFields(base, values) {
  const out = {};
  for (const [id, valueOf] of Object.entries(TASK_FORM)) {
    if (id in values && norm(values[id]) !== valueOf(base)) out[id] = values[id];
  }
  return out;
}
function readTaskForm() {
  const values = {};
  for (const id of Object.keys(TASK_FORM)) { const el = $("#" + id); if (el) values[id] = el.value; }
  return values;
}
/** task_update arguments for the edits in `values` relative to `task`, or null when nothing changed. */
function taskChanges(task, values) {
  const changes = {};
  const title = norm(values["f-title"]).trim();
  const goal = norm(values["f-goal"]).trim();
  const desc = norm(values["f-desc"]);
  if (title !== norm(task.title)) changes.title = title;
  if (goal !== norm(task.goal).trim()) changes.goal = goal;
  if (desc !== norm(task.description)) changes.description = desc;
  if (Number(values["f-priority"]) !== Number(task.priority)) changes.priority = Number(values["f-priority"]);
  if ((values["f-assignee"] || "") !== (task.assignee || "")) changes.assignee = values["f-assignee"] || "";
  if ((values["f-reviewer"] || "") !== (task.reviewer || "")) changes.reviewer = values["f-reviewer"] || "";
  const tags = parseTags(values["f-tags"]);
  if (tags.join(",") !== (task.tags || []).join(",")) changes.tags = tags;
  const deps = parseIds(values["f-deps"]);
  if (deps.join(",") !== (task.depends_on || []).join(",")) changes.depends_on = deps;
  if (values["f-status"] && values["f-status"] !== task.status) changes.status = values["f-status"];
  return Object.keys(changes).length ? changes : null;
}
/** Puts the task values into the form. Fields with unsaved edits are kept unless `force`. */
function fillTaskForm(task, old, force) {
  for (const [id, valueOf] of Object.entries(TASK_FORM)) {
    const el = $("#" + id);
    if (!el) continue;
    if (!force && old && norm(el.value) !== valueOf(old)) continue; // the user is editing this field
    if (id === "f-status") el.innerHTML = statusOptions(task.status);
    else if (id === "f-assignee" || id === "f-reviewer") el.innerHTML = memberOptions(valueOf(task));
    if (el.value !== valueOf(task)) el.value = valueOf(task);
  }
}

function taskHeadHtml(task) {
  const prio = Number(task.priority) >= 2 ? ` <span class="badge ${Number(task.priority) === 3 ? "err" : "warn"}">${esc(t("p" + Number(task.priority)))}</span>` : "";
  return `<span class="muted">#${Number(task.id)}</span> <span class="modal-title-text">${esc(task.title)}</span> <span class="badge st-${esc(task.status)}">${esc(t("s_" + task.status))}</span>${prio}`;
}
function taskMetaHtml(task) {
  const parts = [`${esc(t("created_by"))}: ${esc(displayName(task.created_by || "-"))} · ${esc(fmtTime(task.created_at))}`];
  if (task.updated_at && task.updated_at - task.created_at > 1) parts.push(`${esc(t("updated"))} ${esc(fmtTime(task.updated_at))}`);
  if (task.parent_id) parts.push(`${esc(t("parent"))}: <button type="button" class="link" data-open-task="${Number(task.parent_id)}">#${Number(task.parent_id)}</button>`);
  if ((task.blocked_by || []).length) parts.push(`⛔ ${esc(t("blocked_by"))} ${task.blocked_by.map((b) => `<button type="button" class="link" data-open-task="${Number(b)}">#${Number(b)}</button>`).join(", ")}`);
  return parts.join(" · ");
}
function taskResultHtml(task) {
  return task.result ? `<div class="section-title">${esc(t("result"))}</div><div class="result-box">${linkify(task.result)}</div>` : "";
}
function taskReviewHtml(task) {
  if (task.status !== "review") return "";
  return `<div class="section-title">${esc(t("review_needed"))}${task.reviewer ? ` · ${esc(t("reviewer"))}: ${esc(displayName(task.reviewer))}` : ""}</div>
    <label class="field"><span>${esc(t("review_comment"))}</span><textarea id="review-comment" rows="2"></textarea></label>
    <div class="modal-actions start"><button class="btn primary" id="approve-btn" type="button">${esc(t("approve"))}</button>
    <button class="btn danger" id="reject-btn" type="button">${esc(t("reject"))}</button></div>`;
}
function discussionTitle(task, posts) { return `${t("discussion")} (${Math.max(Number(task.comments) || 0, posts.length)})`; }
function historyHtml(events) { return (events || []).map(historyLine).join("") || `<div>${esc(t("empty"))}</div>`; }

async function openTask(id) {
  let data;
  try { data = await api(`/api/tasks/${id}`); } catch (e) { toast(errText(e), "err"); return; }
  if (S.openTaskId === id && S.taskBase) { updateTaskModal(data); return; }
  if (modalOpen() && modalHasUnsaved() && !confirm(t("discard_changes"))) return;
  renderTaskModal(data);
}
const refreshOpenTask = makeScheduler(async () => {
  const id = S.openTaskId;
  if (!id) return;
  try {
    const data = await api(`/api/tasks/${id}`);
    if (S.openTaskId === id) updateTaskModal(data);
  } catch (e) { console.error(e); }
});

function renderTaskModal(data) {
  const task = data.task;
  const posts = data.discussion || [];
  openModal(`
    <div class="modal-head"><h3 id="task-head">${taskHeadHtml(task)}</h3><button class="icon-btn" type="button" data-close title="${esc(t("close"))}" aria-label="${esc(t("close"))}">✕</button></div>
    <div class="form-grid" id="task-form">
      <label class="field full"><span>${esc(t("title"))}</span><input id="f-title" maxlength="200"></label>
      <label class="field full"><span>🎯 ${esc(t("goal_field"))}</span><textarea id="f-goal" rows="2" placeholder="${esc(t("goal_hint"))}"></textarea></label>
      <label class="field full"><span>${esc(t("description"))}</span><textarea id="f-desc" rows="4"></textarea></label>
      <label class="field"><span>${esc(t("status"))}</span><select id="f-status"></select></label>
      <label class="field"><span>${esc(t("priority"))}</span><select id="f-priority">${priorityOptions(task.priority)}</select></label>
      <label class="field"><span>${esc(t("assignee"))}</span><select id="f-assignee"></select></label>
      <label class="field"><span>${esc(t("reviewer"))}</span><select id="f-reviewer"></select></label>
      <label class="field"><span>${esc(t("tags"))}</span><input id="f-tags"></label>
      <label class="field"><span>${esc(t("depends_on"))}</span><input id="f-deps"></label>
    </div>
    <div class="task-meta muted" id="task-meta">${taskMetaHtml(task)}</div>
    <div class="modal-actions"><button class="btn" type="button" data-close>${esc(t("close"))}</button><button class="btn primary" id="save-task" type="button">${esc(t("save"))}</button></div>
    <div id="task-result">${taskResultHtml(task)}</div>
    <div id="task-review">${taskReviewHtml(task)}</div>
    <div class="section-title" id="discussion-title">${esc(discussionTitle(task, posts))}</div>
    <div class="thread" id="thread">${threadHtml(posts)}</div>
    <div class="reply-to hidden" id="reply-to"></div>
    <div class="post-box"><div class="mention-wrap"><textarea id="post-text" rows="2" placeholder="${esc(t("write_post"))}"></textarea><div class="mention-menu hidden" id="mention-menu" role="listbox"></div></div>
      <button class="btn primary" id="post-btn" type="button">${esc(t("post"))}</button></div>
    <div class="section-title">${esc(t("history"))}</div>
    <div class="history" id="task-history">${historyHtml(data.events)}</div>`, { focus: false });
  fillTaskForm(task, null, true);
  S.openTaskId = task.id;
  S.taskBase = task;
  S.lastPostId = posts.reduce((m, p) => Math.max(m, p.id), 0);
  $("#save-task").addEventListener("click", saveTask);
  $("#post-btn").addEventListener("click", postComment);
  const box = $("#post-text");
  attachMentions(box, $("#mention-menu"));
  box.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); postComment(); } });
  wireReview();
}

/** Live update of the open task modal: never touches the discussion draft, the review comment or
 * form fields with unsaved edits (unless `force`, after the user's own save). */
function updateTaskModal(data, force = false) {
  const task = data.task;
  const old = S.taskBase;
  if (!old || old.id !== task.id || !$("#task-form")) return;
  const posts = data.discussion || [];
  fillTaskForm(task, old, force);
  $("#task-head").innerHTML = taskHeadHtml(task);
  setHtml($("#task-meta"), taskMetaHtml(task));
  setHtml($("#task-result"), taskResultHtml(task));
  if ((old.status === "review") !== (task.status === "review")) { $("#task-review").innerHTML = taskReviewHtml(task); wireReview(); }
  $("#discussion-title").textContent = discussionTitle(task, posts);
  const thread = $("#thread");
  const html = threadHtml(posts);
  if (thread._html !== html) {
    setHtml(thread, html);
    posts.filter((p) => p.id > S.lastPostId).forEach((p) => { const el = $("#post-" + Number(p.id)); if (el) el.classList.add("new"); });
  }
  S.lastPostId = posts.reduce((m, p) => Math.max(m, p.id), S.lastPostId);
  setHtml($("#task-history"), historyHtml(data.events));
  if (S.replyTo && !posts.some((p) => p.id === S.replyTo)) clearReply();
  S.taskBase = task;
}

function wireReview() {
  const approve = $("#approve-btn");
  if (!approve) return;
  approve.addEventListener("click", () => reviewTask(true));
  $("#reject-btn").addEventListener("click", () => reviewTask(false));
}

/** Discussion posts in thread order: every reply right under its parent, with its depth. */
function threadOrder(posts) {
  const byId = new Map(posts.map((p) => [p.id, p]));
  const children = new Map();
  const roots = [];
  for (const p of posts) {
    if (p.reply_to && p.reply_to !== p.id && byId.has(p.reply_to)) {
      if (!children.has(p.reply_to)) children.set(p.reply_to, []);
      children.get(p.reply_to).push(p);
    } else roots.push(p);
  }
  const out = [];
  const seen = new Set();
  const stack = roots.slice().reverse().map((p) => [p, 0]);
  while (stack.length) {
    const [p, depth] = stack.pop();
    if (seen.has(p.id)) continue;
    seen.add(p.id);
    out.push({ post: p, depth });
    (children.get(p.id) || []).slice().reverse().forEach((c) => stack.push([c, depth + 1]));
  }
  for (const p of posts) if (!seen.has(p.id)) out.push({ post: p, depth: 0 }); // only reachable through a cycle
  return out;
}

function threadHtml(posts) {
  if (!posts || !posts.length) return `<div class="empty">${esc(t("empty"))}</div>`;
  const byId = new Map(posts.map((p) => [p.id, p]));
  return threadOrder(posts).map(({ post: p, depth }) => {
    const parent = p.reply_to ? byId.get(p.reply_to) : null;
    const replyLink = p.reply_to ? ` <button class="link reply-link" type="button" data-goto-post="${Number(p.reply_to)}" title="${esc(t("reply_to_post"))}">↪ ${esc(parent ? displayName(parent.author) : "#" + Number(p.reply_to))}</button>` : "";
    return `<div class="post${depth ? " reply" : ""}" id="post-${Number(p.id)}" style="margin-left:${Math.min(depth, 4) * 22}px">
    <div class="post-head">${avatar(p.author, "sm")}<b>${esc(displayName(p.author))}</b>${replyLink}<span>· ${esc(fmtTime(p.ts))}</span>
    <span class="post-actions"><button class="link" type="button" data-reply="${Number(p.id)}">${esc(t("reply"))}</button></span></div>
    <div class="post-body">${linkify(p.text)}</div></div>`;
  }).join("");
}

function historyLine(ev) {
  return `<div>${esc(fmtTime(ev.ts))} — ${esc(describeEvent(ev))}</div>`;
}

function gotoPost(id) {
  const el = $("#post-" + Number(id));
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
}

function setReply(postId) {
  S.replyTo = postId;
  const parent = $("#post-" + Number(postId) + " .post-head b");
  const el = $("#reply-to");
  el.classList.remove("hidden");
  el.innerHTML = `${esc(t("replying_to"))} <b>${esc(parent ? parent.textContent : "#" + postId)}</b> <button class="link" type="button" id="reply-cancel" title="${esc(t("cancel"))}">✕</button>`;
  $("#reply-cancel").addEventListener("click", clearReply);
  $("#post-text").focus();
}
function clearReply() {
  S.replyTo = null;
  const el = $("#reply-to");
  if (el) { el.classList.add("hidden"); el.innerHTML = ""; }
}

async function postComment() {
  const taskId = S.openTaskId;
  const box = $("#post-text");
  const text = box ? box.value.trim() : "";
  if (!text || !taskId) return;
  const args = { task_id: taskId, text };
  if (S.replyTo) args.reply_to = S.replyTo;
  const btn = $("#post-btn");
  btn.disabled = true;
  const res = await act("task_comment", args, true);
  btn.disabled = false;
  if (res.ok && S.openTaskId === taskId) {
    if (box.value.trim() === text) box.value = "";
    clearReply();
    refreshOpenTask(0);
  }
}

async function reviewTask(approve) {
  const taskId = S.openTaskId;
  const comment = ($("#review-comment")?.value || "").trim();
  const res = await act("task_review", { task_id: taskId, approve, comment });
  if (res.ok && S.openTaskId === taskId) {
    const box = $("#review-comment");
    if (box) box.value = "";
    refreshOpenTask(0);
  }
}

async function saveTask() {
  const task = S.taskBase;
  if (!task) return;
  const changes = taskChanges(task, readTaskForm());
  if (!changes) { closeModal(true); return; }
  const btn = $("#save-task");
  btn.disabled = true;
  const res = await act("task_update", { task_id: task.id, ...changes }, true);
  btn.disabled = false;
  if (!res.ok || S.openTaskId !== task.id) return;
  toast(t("saved"));
  try {
    const data = await api(`/api/tasks/${task.id}`);
    if (S.openTaskId === task.id) updateTaskModal(data, true);
  } catch (e) { console.error(e); }
}

/* ---------------------------------------------------------------- @mentions */
/** The `@query` being typed right before the caret, or null. */
function mentionQuery(text, caret) {
  const before = String(text).slice(0, caret);
  const m = before.match(/(?:^|[\s(])@([^\s@()]*(?: [^\s@()]*)?)$/u);
  if (!m) return null;
  return { start: before.length - m[1].length - 1, query: m[1] };
}
/** Member names that match a mention query by full-name prefix or by the prefix of any word. */
function mentionCandidates(query, names, limit = 8) {
  const q = String(query || "").toLowerCase();
  const out = names.filter((n) => {
    const l = n.toLowerCase();
    return l.startsWith(q) || (!q.includes(" ") && l.split(/\s+/).some((w) => w.startsWith(q)));
  });
  if (out.length === 1 && out[0].toLowerCase() === q.trim()) return []; // already complete
  return out.slice(0, limit);
}
function attachMentions(textarea, menu) {
  let items = [];
  let active = 0;
  let range = null;
  const hide = () => { menu.classList.add("hidden"); items = []; };
  const render = () => {
    menu.innerHTML = items.map((n, i) => `<div class="mention-item${i === active ? " active" : ""}" role="option" data-i="${i}">${avatar(n, "sm")}<span>${esc(n)}</span></div>`).join("");
    menu.classList.remove("hidden");
  };
  const update = () => {
    const q = textarea.selectionStart === textarea.selectionEnd ? mentionQuery(textarea.value, textarea.selectionStart) : null;
    items = q ? mentionCandidates(q.query, members().map((a) => a.name)) : [];
    if (!items.length) { hide(); return; }
    range = q;
    active = Math.min(active, items.length - 1);
    render();
  };
  const pick = (i) => {
    const name = items[i];
    if (!name || !range) return;
    const caret = textarea.selectionStart;
    const before = textarea.value.slice(0, range.start);
    const after = textarea.value.slice(caret).replace(/^ /, "");
    const insert = "@" + name + " ";
    textarea.value = before + insert + after;
    const pos = before.length + insert.length;
    textarea.setSelectionRange(pos, pos);
    textarea.focus();
    hide();
  };
  textarea.addEventListener("input", () => { active = 0; update(); });
  textarea.addEventListener("click", update);
  textarea.addEventListener("keydown", (e) => {
    if (menu.classList.contains("hidden") || !items.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      active = (active + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
      render();
    } else if ((e.key === "Enter" && !e.ctrlKey && !e.metaKey && !e.shiftKey) || e.key === "Tab") {
      e.preventDefault();
      e.stopPropagation();
      pick(active);
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      hide();
    }
  });
  textarea.addEventListener("blur", () => setTimeout(hide, 150));
  menu.addEventListener("mousedown", (e) => {
    e.preventDefault();
    const item = e.target.closest(".mention-item");
    if (item) pick(Number(item.dataset.i));
  });
}

/* ---------------------------------------------------------------- new task, goal, hire, settings */
function newTaskModal() {
  openModal(`<div class="modal-head"><h3>${esc(t("new_task"))}</h3><button class="icon-btn" type="button" data-close title="${esc(t("close"))}" aria-label="${esc(t("close"))}">✕</button></div>
    <div class="form-grid">
      <label class="field full"><span>${esc(t("title"))} *</span><input id="n-title" maxlength="200"></label>
      <label class="field full"><span>🎯 ${esc(t("goal_field"))} *</span><textarea id="n-goal" rows="2" placeholder="${esc(t("goal_hint"))}"></textarea></label>
      <label class="field full"><span>${esc(t("description"))}</span><textarea id="n-desc" rows="4"></textarea></label>
      <label class="field"><span>${esc(t("assignee"))}</span><select id="n-assignee">${memberOptions("")}</select></label>
      <label class="field"><span>${esc(t("reviewer"))}</span><select id="n-reviewer">${memberOptions("")}</select></label>
      <label class="field"><span>${esc(t("priority"))}</span><select id="n-priority">${priorityOptions(1)}</select></label>
      <label class="field"><span>${esc(t("tags"))}</span><input id="n-tags"></label>
      <label class="field"><span>${esc(t("depends_on"))}</span><input id="n-deps"></label>
      <label class="field"><span>${esc(t("parent"))}</span><input id="n-parent" inputmode="numeric"></label>
    </div>
    <div class="modal-actions"><button class="btn" type="button" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="create-task" type="button">${esc(t("create"))}</button></div>`);
  $("#create-task").addEventListener("click", async () => {
    const title = $("#n-title").value.trim();
    const goal = $("#n-goal").value.trim();
    if (!title || !goal) { toast(t("required_task"), "err"); ($("#n-title").value.trim() ? $("#n-goal") : $("#n-title")).focus(); return; }
    const args = { title, goal, description: $("#n-desc").value, priority: Number($("#n-priority").value) };
    if ($("#n-assignee").value) args.assignee = $("#n-assignee").value;
    if ($("#n-reviewer").value) args.reviewer = $("#n-reviewer").value;
    const tags = parseTags($("#n-tags").value); if (tags.length) args.tags = tags;
    const deps = parseIds($("#n-deps").value); if (deps.length) args.depends_on = deps;
    const parent = parseIds($("#n-parent").value)[0]; if (parent) args.parent_id = parent;
    const res = await act("task_create", args);
    if (res.ok) closeModal(true);
  });
}

function goalModal() {
  const goal = S.state.swarm.goal;
  const done = goal && goal.status === "done";
  openModal(`<div class="modal-head"><h3>🎯 ${esc(t("set_goal"))}</h3><button class="icon-btn" type="button" data-close title="${esc(t("close"))}" aria-label="${esc(t("close"))}">✕</button></div>
    ${done ? `<div class="section-title">${esc(t("goal_summary"))}</div><div class="result-box">${linkify(goal.summary || "")}</div>
      <label class="field" style="margin-top:10px"><span>${esc(t("reopen_comment"))}</span><textarea id="g-reopen" rows="2"></textarea></label>
      <div class="modal-actions start"><button class="btn" id="g-reopen-btn" type="button">${esc(t("reopen"))}</button></div>` : ""}
    <div class="form-grid" style="margin-top:10px">
      <label class="field full"><span>${esc(t("goal_title"))} *</span><input id="g-title"></label>
      <label class="field full"><span>${esc(t("goal_desc"))}</span><textarea id="g-desc" rows="6"></textarea></label>
    </div>
    <div class="modal-actions"><button class="btn" type="button" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="g-save" type="button">${esc(t("save"))}</button></div>`);
  $("#g-title").value = goal ? goal.title : "";
  $("#g-desc").value = goal ? goal.description || "" : "";
  S.modalInitial = new Map([["g-title", $("#g-title").value], ["g-desc", $("#g-desc").value], ["g-reopen", ""]]);
  $("#g-save").addEventListener("click", async () => {
    const title = $("#g-title").value.trim();
    if (!title) { toast(t("required"), "err"); $("#g-title").focus(); return; }
    const res = await act("set_goal", { title, description: $("#g-desc").value.trim() });
    if (res.ok) closeModal(true);
  });
  if (done) $("#g-reopen-btn").addEventListener("click", async () => {
    const comment = $("#g-reopen").value.trim() || "-";
    const res = await act("swarm_control", { reopen_goal_comment: comment });
    if (res.ok) closeModal(true);
  });
}

function hireModal() {
  const models = S.state.swarm.models && S.state.swarm.models.length ? S.state.swarm.models : ["default"];
  openModal(`<div class="modal-head"><h3>${esc(t("hire_title"))}</h3><button class="icon-btn" type="button" data-close title="${esc(t("close"))}" aria-label="${esc(t("close"))}">✕</button></div>
    <div class="form-grid">
      <label class="field"><span>${esc(t("role"))} *</span><input id="h-role"></label>
      <label class="field"><span>${esc(t("model"))}</span><select id="h-model">${models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("")}</select></label>
      <label class="field full"><span>${esc(t("instructions"))}</span><textarea id="h-instr" rows="4"></textarea></label>
      <label class="field"><span>${esc(t("name_optional"))}</span><input id="h-name"></label>
    </div>
    <div class="modal-actions"><button class="btn" type="button" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="h-save" type="button">${esc(t("hire_btn"))}</button></div>`);
  $("#h-save").addEventListener("click", async () => {
    const role = $("#h-role").value.trim();
    if (!role) { toast(t("required"), "err"); $("#h-role").focus(); return; }
    const args = { role, instructions: $("#h-instr").value.trim() || role, model: $("#h-model").value };
    if ($("#h-name").value.trim()) args.name = $("#h-name").value.trim();
    const res = await act("hire_agent", args);
    if (res.ok) closeModal(true);
  });
}

/** Validated swarm_control arguments for the settings form, or null when the values are invalid. */
function limitsArgs(costText, agentsText) {
  const cost = Number(String(costText).trim().replace(",", "."));
  const agents = Number(String(agentsText).trim());
  if (String(costText).trim() === "" || !Number.isFinite(cost) || cost < 0) return null;
  if (!Number.isInteger(agents) || agents < 1) return null;
  return { max_cost_usd: cost, max_agents: agents };
}

function settingsModal() {
  const lim = S.state.swarm.limits;
  openModal(`<div class="modal-head"><h3>⚙ ${esc(t("settings"))}</h3><button class="icon-btn" type="button" data-close title="${esc(t("close"))}" aria-label="${esc(t("close"))}">✕</button></div>
    <div class="form-grid">
      <label class="field"><span>${esc(t("max_cost"))}</span><input id="s-cost" type="number" min="0" step="0.5" value="${Number(lim.max_cost_usd) || 0}"></label>
      <label class="field"><span>${esc(t("max_agents"))}</span><input id="s-agents" type="number" min="1" step="1" value="${Number(lim.max_agents) || 1}"></label>
    </div>
    <div class="modal-actions"><button class="btn" type="button" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="s-save" type="button">${esc(t("save"))}</button></div>`);
  $("#s-save").addEventListener("click", async () => {
    const args = limitsArgs($("#s-cost").value, $("#s-agents").value);
    if (!args) { toast(t("invalid_limits"), "err"); return; }
    const res = await act("swarm_control", args);
    if (res.ok) closeModal(true);
  });
}

/* ---------------------------------------------------------------- MCP & triggers */
function renderMcp() {
  const items = (S.state.mcp || []).slice().sort((a, b) => (a.status === "removed") - (b.status === "removed"));
  const cls = { active: "done", failed: "err", rejected: "err", pending: "warn" };
  setHtml($("#mcp-list"), items.length ? items.map((s) => `<div class="item${s.status === "removed" ? " dim" : ""}">
    <div class="item-head"><b>${esc(s.name)}</b><span class="badge ${cls[s.status] || ""}">${esc(tv("mcp_", s.status))}</span>
      <span class="badge plain">${esc(tv("origin_", s.origin))}</span>
      <span class="badge plain">${esc(tv("scope_", s.scope))}${s.scope === "task" && s.task_id ? ` <button type="button" class="link" data-open-task="${Number(s.task_id)}">#${Number(s.task_id)}</button>` : ""}</span>
      ${s.owner ? `<span class="muted">${esc(t("owner"))}: ${esc(displayName(s.owner))}</span>` : ""}
      ${s.status === "pending" ? `<span class="spacer"></span><button class="btn small primary" data-mcp-approve="${esc(s.name)}" type="button">${esc(t("approve_mcp"))}</button>
      <button class="btn small danger" data-mcp-reject="${esc(s.name)}" type="button">${esc(t("reject_mcp"))}</button>` : ""}</div>
    <div class="desc">${esc(s.description || "")}</div>
    ${(s.tools || []).length ? `<div class="tools">${s.tools.map((x) => `<code>${esc(x)}</code>`).join("")}</div>` : ""}
    ${s.error ? `<pre class="muted err-text">${esc(s.error)}</pre>` : ""}
  </div>`).join("") : `<div class="empty">${esc(t("no_mcp"))}</div>`);
}

function rejectMcp(name) {
  const comment = prompt(t("reject_reason"), "");
  if (comment === null) return null; // the user cancelled: do not reject
  return act("mcp_approve", { name, approve: false, comment: comment.trim() });
}

function deleteTrigger(id) {
  if (!confirm(t("trigger_delete_confirm", { id }))) return null;
  return act("trigger_delete", { trigger_id: id });
}

function renderTriggers() {
  const items = S.state.triggers || [];
  setHtml($("#trigger-list"), items.length ? items.map((x) => `<div class="item">
    <div class="item-head"><b>#${Number(x.id)}</b><span class="cond">${esc(x.condition)}</span>${x.once ? "" : `<span class="badge" title="${esc(t("periodic"))}">↻</span>`}
      <span class="muted">${esc(t("owner"))}: ${esc(displayName(x.owner))} · ${esc(t("fired"))}: ${Number(x.fire_count) || 0}</span>
      <span class="spacer"></span><button class="btn small danger" data-trigger-del="${Number(x.id)}" type="button">${esc(t("delete"))}</button></div>
    ${x.note ? `<div class="desc">${linkify(x.note)}</div>` : ""}
    ${x.actions && JSON.stringify(x.actions) !== '[{"type":"wake"}]' ? `<div class="desc"><code>${esc(JSON.stringify(x.actions))}</code></div>` : ""}
    ${x.last_error ? `<div class="desc err-text">${esc(x.last_error)}</div>` : ""}
  </div>`).join("") : `<div class="empty">${esc(t("no_triggers"))}</div>`);
}

/* ---------------------------------------------------------------- feed */
function fmtValue(field, value) {
  if (value === null || value === undefined || value === "") return "—";
  if (field === "status") return tv("s_", value);
  if (field === "priority") return tv("p", value);
  if (field === "assignee" || field === "reviewer" || field === "created_by") return displayName(value);
  if (Array.isArray(value)) return value.join(", ") || "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
function changesText(changes) {
  return Object.entries(changes || {}).map(([k, v]) => {
    const [a, b] = Array.isArray(v) ? v : [undefined, v];
    return `${tv("f_", k)}: ${short(fmtValue(k, a), 30)} → ${short(fmtValue(k, b), 40)}`;
  }).join("; ");
}

function describeEvent(ev) {
  const d = ev.data || {};
  const who = displayName(ev.actor);
  const ru = S.lang === "ru";
  const task = d.task || {};
  switch (ev.type) {
    case "task.created": return ru ? `${who} создал(а) задачу #${task.id} «${short(task.title, 90)}»` : `${who} created task #${task.id} "${short(task.title, 90)}"`;
    case "task.updated": return ru ? `${who} изменил(а) #${task.id}: ${changesText(d.changes)}` : `${who} updated #${task.id}: ${changesText(d.changes)}`;
    case "task.comment": return ru ? `${who} в обсуждении #${d.task_id}: ${short(d.post && d.post.text)}` : `${who} in discussion of #${d.task_id}: ${short(d.post && d.post.text)}`;
    case "message.sent": { const m = d.message || {}; return `${displayName(m.sender)} → ${(m.recipients || []).map(displayName).join(", ")}: ${short(m.text)}`; }
    case "goal.set": return ru ? `${who} задал(а) цель: ${short(d.goal && d.goal.title)}` : `${who} set the goal: ${short(d.goal && d.goal.title)}`;
    case "goal.completed": return ru ? `${who}: цель достигнута — ${short(d.goal && d.goal.summary)}` : `${who}: goal achieved — ${short(d.goal && d.goal.summary)}`;
    case "goal.reopened": return ru ? `${who} вернул(а) цель в работу${d.comment ? ": " + short(d.comment) : ""}` : `${who} reopened the goal${d.comment ? ": " + short(d.comment) : ""}`;
    case "agent.hired": return ru ? `${who} нанял(а) ${d.name} (${d.role})` : `${who} hired ${d.name} (${d.role})`;
    case "agent.retired": return ru ? `${d.name} покинул(а) рой${d.reason ? ": " + short(d.reason, 80) : ""}` : `${d.name} left the swarm${d.reason ? ": " + short(d.reason, 80) : ""}`;
    case "file.changed": return `${who} ${tv("op_", d.op)} ${d.path}`;
    case "file.locked": return ru ? `${who} заблокировал(а) ${d.path}` : `${who} locked ${d.path}`;
    case "file.unlocked": return ru ? `${who} разблокировал(а) ${d.path}` : `${who} unlocked ${d.path}`;
    case "trigger.created": return ru ? `${who} поставил(а) триггер: ${short(d.trigger && d.trigger.condition)}` : `${who} set a trigger: ${short(d.trigger && d.trigger.condition)}`;
    case "trigger.fired": return ru ? `${who}: сработал триггер #${d.trigger && d.trigger.id} — ${short(d.trigger && d.trigger.condition, 100)}` : `${who}: trigger #${d.trigger && d.trigger.id} fired — ${short(d.trigger && d.trigger.condition, 100)}`;
    case "trigger.deleted": return ru ? `${who} удалил(а) триггер: ${short(d.trigger && d.trigger.condition, 100)}` : `${who} deleted a trigger: ${short(d.trigger && d.trigger.condition, 100)}`;
    case "mcp.created": return ru ? `${who} создал(а) MCP «${d.server && d.server.name}»` : `${who} created MCP "${d.server && d.server.name}"`;
    case "mcp.status": return `MCP ${d.name}: ${tv("mcp_", d.status)}`;
    case "swarm.paused": return ru ? `рой на паузе (${tv("reason_", d.reason)})` : `swarm paused (${tv("reason_", d.reason)})`;
    case "swarm.resumed": return ru ? "рой продолжил работу" : "swarm resumed";
    case "swarm.limits": return ru ? `лимиты: $${d.max_cost_usd}, агентов ${d.max_agents}` : `limits: $${d.max_cost_usd}, ${d.max_agents} agents`;
    default: return `${ev.type} ${short(JSON.stringify(d), 120)}`;
  }
}

function feedLine(ev) {
  return `<div class="ev"><time>${esc(fmtTime(ev.ts))}</time><span class="ev-text">${linkify(describeEvent(ev))}</span></div>`;
}

async function loadFeed() {
  try {
    const data = await api(`/api/events?limit=400&types=${encodeURIComponent(FEED_TYPES.join(","))}`);
    S.feed = (data.events || []).filter((e) => !FEED_SKIP.has(e.type));
    S.feedMaxId = S.feed.reduce((m, e) => Math.max(m, e.id), 0);
    renderFeed();
  } catch (e) { toast(errText(e), "err"); }
}
function renderFeed() {
  const box = $("#feed");
  box.innerHTML = S.feed.slice().reverse().map(feedLine).join("") || `<div class="empty">${esc(t("empty"))}</div>`;
}

/* ---------------------------------------------------------------- files */
function fmtSize(n) { if (n == null) return ""; if (n < 1024) return n + " B"; if (n < 1048576) return (n / 1024).toFixed(1) + " KB"; return (n / 1048576).toFixed(1) + " MB"; }
async function openDir(path) {
  const dir = path || ".";
  try {
    const data = await api(`/api/files?path=${encodeURIComponent(dir)}`);
    S.fileDir = dir;
    S.fileEntries = data.entries || [];
    renderCrumbs();
    renderFileList();
  } catch (e) {
    if (dir !== ".") openDir(dir.includes("/") ? dir.slice(0, dir.lastIndexOf("/")) : "."); // the directory is gone
    else toast(errText(e), "err");
  }
}
function renderCrumbs() {
  const parts = S.fileDir === "." ? [] : S.fileDir.split("/");
  setHtml($("#crumbs"), [`<button type="button" class="link" data-dir=".">${esc(t("workspace"))}</button>`]
    .concat(parts.map((p, i) => `/ <button type="button" class="link" data-dir="${esc(parts.slice(0, i + 1).join("/"))}">${esc(p)}</button>`)).join(" "));
}
function renderFileList() {
  const locks = new Map(((S.state && S.state.locks) || []).map((l) => [l.path, l.owner]));
  const entries = S.fileEntries.slice().sort((a, b) => (a.type === "dir" ? 0 : 1) - (b.type === "dir" ? 0 : 1) || a.path.localeCompare(b.path));
  setHtml($("#file-list"), entries.map((e) => {
    const name = e.path.split("/").pop();
    const dir = e.type === "dir";
    const lock = locks.has(e.path) ? ` <span class="lock" title="${esc(t("locked_by", { owner: displayName(locks.get(e.path)) }))}">🔒</span>` : "";
    return `<div class="file-entry${e.path === S.openFile ? " active" : ""}" role="button" tabindex="0" data-${dir ? "dir" : "file"}="${esc(e.path)}"><span class="file-name">${dir ? "📁" : "📄"} ${esc(name)}${lock}</span><span class="size">${dir ? "" : esc(fmtSize(e.size))}</span></div>`;
  }).join("") || `<div class="empty">${esc(t("empty"))}</div>`);
}
async function openFile(path, quiet = false) {
  try {
    const data = await api(`/api/files?path=${encodeURIComponent(path)}`);
    const view = $(".file-view");
    const keep = S.openFile === path ? view.scrollTop : 0;
    S.openFile = path;
    $("#file-path").textContent = path + (data.truncated ? " " + t("truncated") : "");
    $("#file-content").textContent = data.content || "";
    view.scrollTop = keep;
    renderFileList();
    if (!quiet && isNarrow()) view.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (e) { if (!quiet) toast(errText(e), "err"); }
}
const scheduleFiles = makeScheduler(() => { if (S.tab === "files") openDir(S.fileDir); }, undefined);

/* ---------------------------------------------------------------- live events */
function handleEvent(ev) {
  if (ev.id !== undefined && ev.id !== null) {
    if (S.seenEvents.has(ev.id)) return;
    S.seenEvents.add(ev.id);
    if (S.seenEvents.size > MAX_SEEN) S.seenEvents.delete(S.seenEvents.values().next().value);
  }
  const d = ev.data || {};
  if (ev.type.startsWith("trace.")) {
    if (typeof onTraceEvent === "function") onTraceEvent(ev);
    return;
  }
  if (ev.type === "agent.activity") {
    if (S.chatWith === d.agent && S.dtab === "activity") appendActivity(ev);
    return;
  }
  if (ev.type === "notification" && d.recipient === "user") toast("🔔 " + short(d.text, 400));
  if (ev.type === "message.sent") {
    const m = d.message || {};
    const rcpt = m.recipients || [];
    const involvesOpen = S.chatWith === "all" || (S.chatWith && (m.sender === S.chatWith || rcpt.includes(S.chatWith)) && (m.sender === "user" || rcpt.includes("user")));
    if (involvesOpen) scheduleMessages(100);
    else if (rcpt.includes("user")) toast(`💬 ${m.sender}: ${short(m.text, 200)}`, "", () => openChat(m.sender));
  }
  if (S.openTaskId && ((ev.type === "task.updated" && d.task && d.task.id === S.openTaskId)
      || (ev.type === "task.comment" && d.task_id === S.openTaskId))) refreshOpenTask(150);
  if (!FEED_SKIP.has(ev.type) && ev.id > S.feedMaxId) {
    S.feedMaxId = ev.id;
    S.feed.push(ev);
    if (S.feed.length > 1000) S.feed.shift();
    if (S.tab === "feed") {
      const box = $("#feed");
      if (box.querySelector(".empty")) box.innerHTML = "";
      box.insertAdjacentHTML("afterbegin", feedLine(ev));
      while (box.children.length > 1000) box.lastElementChild.remove();
    }
  }
  if (S.tab === "files" && ev.type.startsWith("file.")) {
    scheduleFiles(300);
    if (ev.type === "file.changed" && d.path === S.openFile) openFile(S.openFile, true);
  }
  scheduleRefresh(ev.type === "agent.usage" ? 1000 : 250);
}

/** After a reconnect: everything that happened while the socket was down is reloaded. */
function resync() {
  refresh();
  if (S.tab === "feed") loadFeed(); else { S.feed = []; S.feedMaxId = 0; }
  if (S.tab === "files") { openDir(S.fileDir); if (S.openFile) openFile(S.openFile, true); }
  if (S.chatWith) { loadMessages(); if (S.dtab === "activity") loadActivity(); }
  if (S.openTaskId) refreshOpenTask(0);
}

let connTimer = null;
function setConnected(on, quiet = false) {
  const dot = $("#conn");
  dot.classList.toggle("on", on);
  dot.title = t(on ? "live" : "disconnected");
  clearTimeout(connTimer);
  const banner = $("#conn-banner");
  banner.textContent = t("disconnected");
  if (on || quiet) banner.classList.toggle("hidden", on || banner.classList.contains("hidden"));
  else connTimer = setTimeout(() => banner.classList.remove("hidden"), 3000);
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}${BASE}/ws`);
  S.ws = ws;
  ws.onopen = () => {
    S.wsRetry = 0;
    setConnected(true);
    if (S.wsEverOpen) resync(); else refresh();
    S.wsEverOpen = true;
  };
  ws.onmessage = (msg) => { try { handleEvent(JSON.parse(msg.data)); } catch (e) { console.error(e); } };
  ws.onclose = () => {
    if (S.ws !== ws) return;
    setConnected(false);
    const delay = Math.min(10000, 1000 * 2 ** Math.min(S.wsRetry++, 4));
    setTimeout(connect, delay);
  };
  ws.onerror = () => ws.close();
}

/* ---------------------------------------------------------------- wiring */
function setTab(tab) {
  S.tab = tab;
  $$("#tabs > button[data-tab]").forEach((b) => { b.classList.toggle("active", b.dataset.tab === tab); b.setAttribute("aria-selected", b.dataset.tab === tab); });
  for (const name of ["board", "feed", "files", "mcp", "triggers", "traces"]) $("#pane-" + name).classList.toggle("hidden", name !== tab);
  if (tab === "traces" && typeof loadTraces === "function") loadTraces();
  if (tab === "feed") loadFeed();
  if (tab === "files") { openDir(S.fileDir); if (S.openFile) openFile(S.openFile, true); }
  renderAll();
}

/** Start this session (board served by the launcher at /s/{sid}/) and reload the page. */
async function startSession() {
  const sid = BASE.split("/").pop();
  if (!sid) return;
  $("#start-session-btn").disabled = true;
  try {
    const resp = await fetch(`/api/sessions/${encodeURIComponent(sid)}/start`, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).error || resp.statusText);
    location.reload();
  } catch (e) {
    $("#start-session-btn").disabled = false;
    toast(String(e.message || e), "err");
  }
}

function applyTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}

function onDocumentClick(e) {
  const target = e.target.closest("[data-open-task],[data-close],[data-dir],[data-file],[data-reply],[data-goto-post],[data-mcp-approve],[data-mcp-reject],[data-trigger-del],[data-retire],[data-traces],[data-agent],#mcp-cmd");
  if (!target) {
    if (document.body.classList.contains("team-open") && !e.target.closest(".sidebar, #team-btn")) toggleTeamOverlay(false);
    return;
  }
  const ds = target.dataset;
  if ("openTask" in ds) openTask(Number(ds.openTask));
  else if ("close" in ds) closeModal();
  else if ("dir" in ds) openDir(ds.dir);
  else if ("file" in ds) openFile(ds.file);
  else if ("reply" in ds) setReply(Number(ds.reply));
  else if ("gotoPost" in ds) gotoPost(ds.gotoPost);
  else if ("mcpApprove" in ds) act("mcp_approve", { name: ds.mcpApprove, approve: true });
  else if ("mcpReject" in ds) rejectMcp(ds.mcpReject);
  else if ("triggerDel" in ds) deleteTrigger(Number(ds.triggerDel));
  else if ("retire" in ds) { if (confirm(t("retire_confirm"))) act("retire_agent", { name: ds.retire, reason: "retired by the user" }); }
  else if ("traces" in ds) { if (typeof openTraces === "function") { if (isNarrow()) closeDrawer(); openTraces(ds.traces); } }
  else if ("agent" in ds) openChat(ds.agent);
  else if (target.id === "mcp-cmd") navigator.clipboard?.writeText(target.textContent).then(() => toast(t("copied")));
}

function onKeydown(e) {
  if (e.key === "Escape") {
    if (modalOpen()) closeModal();
    else if (document.body.classList.contains("team-open")) toggleTeamOverlay(false);
    else if (S.chatWith) closeDrawer();
    return;
  }
  if ((e.key === "Enter" || e.key === " ") && e.target.matches && e.target.matches(".file-entry")) { e.preventDefault(); e.target.click(); }
}

function init() {
  S.lang = store("dt-lang") || "ru";
  applyTheme(store("dt-theme"));
  applyI18n();
  const onResize = () => { applyTeamLayout(); if (!isNarrow()) toggleTeamOverlay(false); };
  window.addEventListener("resize", onResize);
  // keep a reference: an unreferenced MediaQueryList may be garbage-collected together with its listener
  S.railQuery = window.matchMedia ? window.matchMedia(RAIL_QUERY) : null;
  S.railQuery?.addEventListener?.("change", onResize);
  wireBoard();
  $$("#tabs > button[data-tab]").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
  $$(".drawer-tabs button").forEach((b) => b.addEventListener("click", () => setDrawerTab(b.dataset.dtab)));
  $("#new-task-btn").addEventListener("click", newTaskModal);
  $("#goal-btn").addEventListener("click", () => { if (S.state) goalModal(); });
  $("#hire-btn").addEventListener("click", () => { if (S.state) hireModal(); });
  $("#settings-btn").addEventListener("click", () => { if (S.state) settingsModal(); });
  $("#broadcast-btn").addEventListener("click", () => openChat("all"));
  $("#team-toggle").addEventListener("click", () => { store("dt-team", document.body.classList.contains("team-rail") ? "full" : "rail"); applyTeamLayout(); });
  $("#team-close").addEventListener("click", () => toggleTeamOverlay(false));
  $("#team-btn").addEventListener("click", () => toggleTeamOverlay());
  $("#pause-btn").addEventListener("click", () => act("swarm_control", { action: S.state && S.state.swarm.paused ? "resume" : "pause" }));
  $("#lang-btn").addEventListener("click", () => setLang(S.lang === "ru" ? "en" : "ru"));
  $("#theme-btn").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme;
    const dark = cur ? cur === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    store("dt-theme", next);
    applyTheme(next);
  });
  $("#drawer-close").addEventListener("click", closeDrawer);
  $("#start-session-btn").addEventListener("click", startSession);
  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); sendComposer(); });
  $("#composer-text").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendComposer(); } });
  $("#search").addEventListener("input", (e) => { S.search = e.target.value.trim(); if (S.tab === "board") renderBoard(); });
  const backdrop = $("#modal-backdrop");
  let downOnBackdrop = false; // a text selection that starts in the modal and ends outside must not close it
  backdrop.addEventListener("mousedown", (e) => { downOnBackdrop = e.target === backdrop; });
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop && downOnBackdrop) closeModal(); downOnBackdrop = false; });
  document.addEventListener("keydown", onKeydown);
  document.addEventListener("click", onDocumentClick);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) autoMarkRead(); });
  refresh();
  connect();
  setInterval(() => { if (!S.ws || S.ws.readyState !== 1) refresh(); }, 10000);
}

if (typeof module !== "undefined" && module.exports) {
  // pure helpers, exported for the Node-based UI tests (tests/test_ui.py); the browser ignores this
  module.exports = {
    I18N, S, COLUMNS, ALL_STATUSES, TASK_FORM, esc, t, tv, linkify, highlightMentions, memberOptions, statusOptions,
    dropAction, makeScheduler, threadOrder, threadHtml, dirtyFields, taskChanges, mentionQuery, mentionCandidates,
    limitsArgs, describeEvent, changesText, activityHtml, messageHtml, cardHtml, boardHtml, boardColumns, rejectMcp,
    deleteTrigger, fillTaskForm, autoMarkRead, loadFeed, FEED_TYPES,
    // used by traces.js (the tests load it on top of these) and by the session-chrome tests
    BASE, $, $$, api, post, act, toast, avatar, members, fmtTime, short, openModal, closeModal, setTab, store, setHtml,
    renderHeader, responseError, errText, startSession, connect,
  };
}
if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", init);
