"use strict";
// DreamTeam launcher page: sessions list, new session, global settings (docs/SPEC.md §18.4).

const I18N = {
  ru: {
    tagline: "рой ИИ-агентов", tab_sessions: "Сессии", tab_settings: "Настройки", language: "Язык", theme: "Тема",
    sessions_title: "Сессии",
    sessions_hint: "Каждая сессия — отдельный рой агентов со своей целью, доской задач, командой, перепиской, файлами и трейсами.",
    new_session: "Начать новую сессию", search_sessions: "Поиск по сессиям", f_all: "Все", f_running: "Работают", f_stopped: "Остановлены",
    empty_title: "Пока нет ни одной сессии",
    empty_hint: "Опишите цель — рой сам спланирует задачи, распределит их между агентами и начнёт работу. Перед стартом проверьте модель в настройках.",
    no_match: "Ничего не найдено.", loading: "Загрузка…",
    st_running: "Работает", st_paused: "На паузе", st_stopped: "Остановлена", st_done: "Цель достигнута", st_pending: "ждёт запуска",
    no_goal: "Цель не задана", tasks_done: "{done} из {total} задач", agents_n: "агентов", working_n: "работают",
    spent: "Потрачено", model: "Модель", last_activity: "Активность", created: "Создана",
    open: "Открыть", start: "Запустить", stop: "Остановить", delete: "Удалить", legacy: "проект",
    started: "Сессия «{title}» запущена", stopped: "Сессия «{title}» остановлена", deleted: "Сессия удалена",
    delete_title: "Удалить сессию?",
    delete_text: "Сессия «{title}» будет удалена вместе с доской задач, перепиской, файлами workspace, MCP-серверами сессии и трейсами. Это действие необратимо.",
    cancel: "Отмена", create_open: "Создать и открыть", create: "Создать",
    ns_title: "Новая сессия", ns_lead: "Рой получит цель, сам создаст план (задачу планирования) и распределит работу между агентами.",
    f_title: "Название", f_title_ph: "Например: Игра «Змейка»", f_goal: "Цель", f_goal_ph: "Что должен сделать рой? Чем конкретнее, тем лучше.",
    f_desc: "Подробности", f_desc_ph: "Требования, ограничения, критерии готовности, ссылки…",
    f_model: "Модель", f_lang: "Язык агентов", f_start_agents: "Агентов на старте", f_max_agents: "Максимум агентов",
    f_budget: "Бюджет, $", f_roles: "Стартовые роли", f_roles_ph: "Аналитик, Разработчик, Тестировщик", f_roles_hint: "через запятую; остальные получат роль по умолчанию",
    f_more: "Дополнительно", f_steps: "Шагов за пробуждение", f_approval: "Подтверждать MCP-серверы агентов", f_start_now: "Запустить сразу",
    goal_required: "Укажите цель сессии.", creating: "Создаю сессию…",
    // settings
    s_models: "Модели и эндпоинты", s_models_hint: "Профили подключения к LLM. Агенты используют профиль по умолчанию; при найме агента можно указать другой профиль (например, дешёвый «fast»).",
    s_swarm: "Новые сессии по умолчанию", s_swarm_hint: "Значения для новых сессий; при создании сессии их можно изменить.",
    s_agents: "Агенты", s_agents_hint: "Поведение ReAct-цикла агентов.",
    s_shell: "Консоль", s_shell_hint: "Команды, которые агенты выполняют в workspace.",
    s_workspace: "Workspace", s_workspace_hint: "Общая папка файлов сессии.",
    s_mcp: "MCP", s_mcp_hint: "MCP-серверы, которые создают агенты, и внешние MCP-серверы, доступные рою.",
    s_tracing: "Трейсы", s_tracing_hint: "Полная запись работы агентов: вызовы LLM, мысли, инструменты, токены, стоимость и время.",
    s_server: "Сервер", s_server_hint: "Изменения применяются после перезапуска `dreamteam serve`.",
    p_name: "Имя профиля", p_default: "по умолчанию", p_make_default: "Сделать основным", p_delete: "Удалить профиль",
    p_provider: "Провайдер", p_preset: "Заполнить из пресета…", p_base_url: "Base URL", p_base_url_ph: "по умолчанию у провайдера",
    p_key: "API-ключ", p_key_env: "Переменная окружения", p_key_value: "Ключ", p_env_ph: "ANTHROPIC_API_KEY",
    p_env_set: "✓ задана на сервере", p_env_unset: "✗ не задана на сервере", p_env_later: "проверится после сохранения",
    p_key_saved: "сохранён {hint} — оставьте пустым, чтобы не менять", p_key_new: "вставьте ключ", p_key_clear: "Удалить ключ",
    p_key_cleared: "ключ будет удалён при сохранении", p_key_note: "Ключ хранится в dreamteam.toml на этом компьютере и не передаётся в браузер.",
    p_model: "Модель", p_load_models: "Загрузить список", p_max_tokens: "max_tokens", p_context: "Окно контекста (токены)",
    p_context_hint: "при превышении история сжимается", p_effort: "Effort", p_thinking: "Thinking", p_thinking_display: "Показ мыслей",
    p_fallbacks: "Серверный fallback при отказе (beta)", p_mismatch: "Thinking при изменённом префиксе", p_max_param: "Параметр лимита вывода",
    p_prices: "Цены, $ за 1M токенов", p_price_in: "Вход", p_price_out: "Выход", p_price_cr: "Чтение кэша", p_price_cw: "Запись кэша",
    p_prices_hint: "Пусто — известные цены модели (или 0). OpenRouter сообщает стоимость сам.", p_extra: "Доп. параметры запроса (JSON)",
    p_check: "Проверить", p_live: "Тестовый запрос", p_checking: "Проверяю…", p_models_found: "Эндпоинт доступен, моделей: {n}.",
    p_model_ok: "Модель «{model}» есть в списке.", p_model_missing: "Модели «{model}» нет в списке эндпоинта.",
    p_live_ok: "Ответ за {ms} мс: «{reply}» (стоимость ${cost}).", p_live_bad: "Запрос не прошёл: {error}",
    p_add: "Добавить профиль", p_add_from: "Пресет:", unsaved_check: "Проверка использует несохранённые значения формы.",
    default_marker: "основной", none_opt: "—",
    sw_name: "Имя роя", sw_lang: "Язык промптов агентов", sw_start: "Агентов на старте", sw_max: "Максимум агентов",
    sw_budget: "Бюджет сессии, $", sw_budget_hint: "0 — без лимита", sw_roles: "Стартовые роли",
    ag_steps: "Шагов за пробуждение", ag_context: "Окно контекста по умолчанию", ag_tool_out: "Лимит вывода инструмента (симв.)",
    ag_stagger: "Пауза между стартами агентов, с", ag_nudge: "Напоминание простаивающим, с", ag_parallel: "Параллельных вызовов LLM",
    sh_program: "Оболочка", sh_timeout: "Таймаут по умолчанию, с", sh_max_timeout: "Максимальный таймаут, с",
    sh_pass_env: "Пробрасывать переменные", sh_pass_env_hint: "секреты из окружения скрыты от агентов, кроме перечисленных",
    ws_git: "Git с автокоммитами", ws_lock: "Время блокировки файла, мин",
    mcp_approval: "Подтверждать новые MCP-серверы агентов", mcp_library: "Папка библиотеки MCP", mcp_startup: "Таймаут запуска, с",
    mcp_call: "Таймаут вызова, с", mcp_external: "Внешние MCP-серверы (JSON)",
    mcp_env_hint: "значения env скрыты (••••…); оставьте как есть, чтобы сохранить, или впишите новое",
    mcp_external_hint: "{\"fetch\": {\"command\": [\"uvx\", \"mcp-server-fetch\"], \"description\": \"Загрузка страниц\"}}",
    tr_enabled: "Записывать трейсы", tr_payload: "Макс. размер поля (симв.)", tr_retention: "Хранить, дней", tr_retention_hint: "0 — всегда",
    sv_host: "Адрес", sv_port: "Порт", sv_resume: "Возобновлять работавшие сессии при старте",
    discard: "Отменить изменения", save: "Сохранить", saving: "Сохраняю…", unsaved: "Есть несохранённые изменения",
    saved: "Настройки сохранены.", saved_applied: "Настройки сохранены и применены к сессиям: {list}.",
    invalid_json: "Некорректный JSON в поле «{field}».", conn_lost: "Нет связи с сервером",
    leave_unsaved: "Есть несохранённые изменения настроек. Уйти?", err: "Ошибка",
    retry: "Повторить", discard_new: "Закрыть без создания? Введённый текст пропадёт.",
    p_key_env_drop: "сохранённый ключ {hint} будет удалён при сохранении",
    p_name_bad: "Имя профиля «{name}»: нужны латинские буквы, цифры, «-» или «_».",
    p_name_dup: "Два профиля с именем «{name}».", p_model_required: "Укажите модель в профиле «{name}».",
  },
  en: {
    tagline: "AI agent swarm", tab_sessions: "Sessions", tab_settings: "Settings", language: "Language", theme: "Theme",
    sessions_title: "Sessions",
    sessions_hint: "Each session is a separate agent swarm with its own goal, task board, team, messages, files and traces.",
    new_session: "Start a new session", search_sessions: "Search sessions", f_all: "All", f_running: "Running", f_stopped: "Stopped",
    empty_title: "No sessions yet",
    empty_hint: "Describe a goal: the swarm plans the tasks, shares them out between agents and gets to work. Check the model in Settings first.",
    no_match: "Nothing found.", loading: "Loading…",
    st_running: "Running", st_paused: "Paused", st_stopped: "Stopped", st_done: "Goal reached", st_pending: "waiting to start",
    no_goal: "No goal yet", tasks_done: "{done} of {total} tasks", agents_n: "agents", working_n: "working",
    spent: "Spent", model: "Model", last_activity: "Activity", created: "Created",
    open: "Open", start: "Start", stop: "Stop", delete: "Delete", legacy: "project",
    started: "Session “{title}” started", stopped: "Session “{title}” stopped", deleted: "Session deleted",
    delete_title: "Delete the session?",
    delete_text: "Session “{title}” will be deleted with its task board, messages, workspace files, session MCP servers and traces. This cannot be undone.",
    cancel: "Cancel", create_open: "Create and open", create: "Create",
    ns_title: "New session", ns_lead: "The swarm gets the goal, makes a plan (a planning task) and shares the work between agents.",
    f_title: "Title", f_title_ph: "e.g. Snake game", f_goal: "Goal", f_goal_ph: "What should the swarm achieve? The more concrete, the better.",
    f_desc: "Details", f_desc_ph: "Requirements, constraints, definition of done, links…",
    f_model: "Model", f_lang: "Agents' language", f_start_agents: "Agents at start", f_max_agents: "Max agents",
    f_budget: "Budget, $", f_roles: "Starting roles", f_roles_ph: "Analyst, Developer, Tester", f_roles_hint: "comma-separated; the rest get the default role",
    f_more: "More", f_steps: "Steps per wake-up", f_approval: "Require approval of agents' MCP servers", f_start_now: "Start right away",
    goal_required: "Enter the session goal.", creating: "Creating the session…",
    s_models: "Models and endpoints", s_models_hint: "LLM connection profiles. Agents use the default profile; a hired agent can use another one (e.g. a cheap “fast”).",
    s_swarm: "Defaults for new sessions", s_swarm_hint: "Values for new sessions; you can change them when creating a session.",
    s_agents: "Agents", s_agents_hint: "Behaviour of the agents' ReAct loop.",
    s_shell: "Console", s_shell_hint: "Commands agents run in the workspace.",
    s_workspace: "Workspace", s_workspace_hint: "The shared file folder of a session.",
    s_mcp: "MCP", s_mcp_hint: "MCP servers built by agents, and external MCP servers available to the swarm.",
    s_tracing: "Traces", s_tracing_hint: "A full record of the agents' work: LLM calls, thinking, tools, tokens, cost and timing.",
    s_server: "Server", s_server_hint: "Changes apply after restarting `dreamteam serve`.",
    p_name: "Profile name", p_default: "default", p_make_default: "Make default", p_delete: "Delete profile",
    p_provider: "Provider", p_preset: "Fill from a preset…", p_base_url: "Base URL", p_base_url_ph: "provider default",
    p_key: "API key", p_key_env: "Environment variable", p_key_value: "Key", p_env_ph: "ANTHROPIC_API_KEY",
    p_env_set: "✓ set on the server", p_env_unset: "✗ not set on the server", p_env_later: "checked after saving",
    p_key_saved: "saved {hint} — leave empty to keep it", p_key_new: "paste the key", p_key_clear: "Remove key",
    p_key_cleared: "the key will be removed on save", p_key_note: "The key is stored in dreamteam.toml on this computer and never sent to the browser.",
    p_model: "Model", p_load_models: "Load list", p_max_tokens: "max_tokens", p_context: "Context window (tokens)",
    p_context_hint: "the history is compacted beyond it", p_effort: "Effort", p_thinking: "Thinking", p_thinking_display: "Thinking display",
    p_fallbacks: "Server-side fallback on refusal (beta)", p_mismatch: "Thinking on a changed prefix", p_max_param: "Output limit parameter",
    p_prices: "Prices, $ per 1M tokens", p_price_in: "Input", p_price_out: "Output", p_price_cr: "Cache read", p_price_cw: "Cache write",
    p_prices_hint: "Empty — the model's known prices (or 0). OpenRouter reports the cost itself.", p_extra: "Extra request parameters (JSON)",
    p_check: "Check", p_live: "Test request", p_checking: "Checking…", p_models_found: "The endpoint answers, {n} model(s).",
    p_model_ok: "Model “{model}” is in the list.", p_model_missing: "Model “{model}” is not in the endpoint's list.",
    p_live_ok: "Answered in {ms} ms: “{reply}” (cost ${cost}).", p_live_bad: "The request failed: {error}",
    p_add: "Add profile", p_add_from: "Preset:", unsaved_check: "The check uses the unsaved form values.",
    default_marker: "default", none_opt: "—",
    sw_name: "Swarm name", sw_lang: "Agents' prompt language", sw_start: "Agents at start", sw_max: "Max agents",
    sw_budget: "Session budget, $", sw_budget_hint: "0 — no limit", sw_roles: "Starting roles",
    ag_steps: "Steps per wake-up", ag_context: "Default context window", ag_tool_out: "Tool output limit (chars)",
    ag_stagger: "Delay between agent starts, s", ag_nudge: "Nudge idle agents after, s", ag_parallel: "Parallel LLM calls",
    sh_program: "Shell", sh_timeout: "Default timeout, s", sh_max_timeout: "Max timeout, s",
    sh_pass_env: "Pass environment variables", sh_pass_env_hint: "secrets are hidden from agents except the listed ones",
    ws_git: "Git with auto-commits", ws_lock: "File lock time, min",
    mcp_approval: "Require approval of new agent MCP servers", mcp_library: "MCP library folder", mcp_startup: "Startup timeout, s",
    mcp_call: "Call timeout, s", mcp_external: "External MCP servers (JSON)",
    mcp_env_hint: "env values are hidden (••••…); keep them to keep the saved value, or type a new one",
    mcp_external_hint: "{\"fetch\": {\"command\": [\"uvx\", \"mcp-server-fetch\"], \"description\": \"Fetch web pages\"}}",
    tr_enabled: "Record traces", tr_payload: "Max field size (chars)", tr_retention: "Keep for, days", tr_retention_hint: "0 — forever",
    sv_host: "Host", sv_port: "Port", sv_resume: "Resume running sessions on start",
    discard: "Discard changes", save: "Save", saving: "Saving…", unsaved: "Unsaved changes",
    saved: "Settings saved.", saved_applied: "Settings saved and applied to sessions: {list}.",
    invalid_json: "Invalid JSON in “{field}”.", conn_lost: "No connection to the server",
    leave_unsaved: "You have unsaved settings. Leave?", err: "Error",
    retry: "Retry", discard_new: "Close without creating? Your text will be lost.",
    p_key_env_drop: "the saved key {hint} will be removed on save",
    p_name_bad: "Profile name “{name}”: use Latin letters, digits, “-” or “_”.",
    p_name_dup: "Two profiles are named “{name}”.", p_model_required: "Enter the model of profile “{name}”.",
  },
};

const H = {
  lang: "ru", tab: "sessions", sessions: null, filter: "all", search: "",
  view: null, original: null, providers: [], path: "", profiles: [], defaultUid: null,
  dirty: false, ws: null, wsRetry: 0, reloadTimer: null, uid: 0, busy: new Set(), settingsError: null,
};
// profile names the settings accept (settings.apply_update)
const PROFILE_NAME = /^[A-Za-z0-9_-]+$/;

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function t(key, vars) {
  const text = (I18N[H.lang] && I18N[H.lang][key]) ?? I18N.en[key] ?? key;
  // one pass: a value that itself contains "{name}" (a session title, a model reply) is not substituted again
  return vars ? text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m)) : text;
}
function store(key, value) { try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch (e) { return null; } return null; }
function clone(value) { return JSON.parse(JSON.stringify(value)); }

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  let resp;
  try { resp = await fetch(path, opts); } catch (e) { throw new Error(t("conn_lost")); }
  let data = null;
  try { data = await resp.json(); } catch (e) { data = null; }
  if (!resp.ok) throw new Error((data && (data.error || data.result)) || `${resp.status} ${resp.statusText}`);
  return data;
}

function toast(text, isErr = false) {
  const box = document.createElement("div");
  box.className = "toast" + (isErr ? " err" : "");
  box.textContent = text;
  $("#toasts").appendChild(box);
  setTimeout(() => box.remove(), isErr ? 9000 : 4500);
}

function ago(ts) {
  if (!ts) return "—";
  const d = Math.max(0, Date.now() / 1000 - ts);
  const ru = H.lang === "ru";
  if (d < 60) return ru ? "только что" : "just now";
  if (d < 3600) return ru ? `${Math.floor(d / 60)} мин назад` : `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return ru ? `${Math.floor(d / 3600)} ч назад` : `${Math.floor(d / 3600)} h ago`;
  return new Date(ts * 1000).toLocaleDateString(ru ? "ru-RU" : "en-US", { day: "numeric", month: "short", year: "numeric" });
}
const money = (v) => "$" + Number(v || 0).toFixed(2);

// ---- i18n / theme ----------------------------------------------------------------------------------
function applyI18n() {
  document.documentElement.lang = H.lang;
  $$("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  $$("[data-i18n-placeholder]").forEach((el) => { el.placeholder = t(el.dataset.i18nPlaceholder); });
  $$("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); });
  $("#lang-btn").textContent = H.lang.toUpperCase();
  document.title = "DreamTeam — " + t(H.tab === "settings" ? "tab_settings" : "tab_sessions");
}
function setLang(lang) {
  H.lang = lang; store("dt-lang", lang); applyI18n(); renderSessions();
  if (H.view) renderSettings(false);
}
function applyTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
}

// ---- tabs ------------------------------------------------------------------------------------------
function setTab(tab, push = true) {
  H.tab = tab;
  $$(".home-tabs > button").forEach((b) => b.classList.toggle("active", b.dataset.htab === tab));
  $("#pane-sessions").classList.toggle("hidden", tab !== "sessions");
  $("#pane-settings").classList.toggle("hidden", tab !== "settings");
  if (push) history.replaceState(null, "", tab === "settings" ? "#settings" : location.pathname);
  if (tab === "settings" && !H.view) loadSettingsPane();
  if (tab === "sessions") loadSessions();
  updateSaveBar();
  applyI18n();
}

/** Loads the settings into the pane; a failure is shown there with a retry button (not "Loading…" forever). */
async function loadSettingsPane() {
  try {
    await loadSettings();
  } catch (e) {
    H.settingsError = e.message;
    $("#settings-form").innerHTML = `<div class="err-text">${esc(t("err"))}: ${esc(e.message)}</div>
      <div><button class="btn" type="button" id="settings-retry">⟳ ${esc(t("retry"))}</button></div>`;
    $("#settings-retry").addEventListener("click", () => {
      $("#settings-form").innerHTML = `<div class="settings-loading muted">${esc(t("loading"))}</div>`;
      loadSettingsPane();
    });
  }
}

/** Leaving the settings tab: true when allowed (unsaved edits are discarded after a confirmation). */
function leaveSettings() {
  if (H.tab !== "settings" || !H.dirty) return true;
  if (!confirm(t("leave_unsaved"))) return false;
  resetSettings();
  return true;
}

// ---- sessions --------------------------------------------------------------------------------------
async function loadSessions() {
  const seq = H.listSeq = (H.listSeq || 0) + 1; // the poll and live events overlap: only the newest answer counts
  try {
    const data = await api("GET", "/api/sessions");
    if (seq !== H.listSeq) return;
    H.sessions = data.sessions || [];
    renderSessions();
  } catch (e) {
    if (seq === H.listSeq && H.sessions === null) {
      const grid = $("#sessions");
      grid.innerHTML = `<div class="err-text">${esc(e.message)}</div>`;
      grid._html = null;
    }
  }
}
function scheduleSessions() {
  clearTimeout(H.reloadTimer);
  H.reloadTimer = setTimeout(loadSessions, 250);
}

function sessionState(s) {
  if (s.goal && s.goal.status === "done") return "done";
  if (s.status === "running") return s.paused ? "paused" : "running";
  return "stopped";
}

function renderSessions() {
  const grid = $("#sessions");
  if (!grid || H.sessions === null) return;
  const all = H.sessions;
  $("#sessions-empty").classList.toggle("hidden", all.length > 0);
  $(".session-tools").classList.toggle("hidden", all.length === 0);
  const items = filterSessions(all, H.filter, H.search);
  const html = !items.length && all.length ? `<div class="muted">${esc(t("no_match"))}</div>` : items.map(sessionCard).join("");
  // the list is polled every few seconds: keep the DOM (focus, hover, a button being clicked) when nothing changed
  if (grid._html !== html) { grid.innerHTML = html; grid._html = html; }
}

function filterSessions(all, filter, search) {
  const q = String(search || "").toLowerCase();
  return all.filter((s) => {
    if (filter === "running" && s.status !== "running") return false;
    if (filter === "stopped" && s.status === "running") return false;
    if (!q) return true;
    return [s.title, s.id, s.goal && s.goal.title, s.goal && s.goal.description].join(" ").toLowerCase().includes(q);
  });
}

function sessionCard(s) {
  const state = sessionState(s);
  const tasks = s.tasks || { total: 0, done: 0 };
  const agents = s.agents || { total: 0, working: 0 };
  const pct = tasks.total ? Math.round(100 * tasks.done / tasks.total) : 0;
  const badgeCls = { running: "done", paused: "warn", done: "", stopped: "plain" }[state];
  const goal = s.goal && s.goal.title
    ? `<div class="sc-goal" title="${esc(s.goal.description || s.goal.title)}">🎯 ${esc(s.goal.title)}${s.goal.status === "pending" ? ` <span class="muted">(${esc(t("st_pending"))})</span>` : ""}</div>`
    : `<div class="sc-goal none">${esc(t("no_goal"))}</div>`;
  const budget = s.max_cost_usd > 0 ? `${money(s.spent_usd)} / ${money(s.max_cost_usd)}` : money(s.spent_usd);
  const running = s.status === "running";
  const error = s.error || s.config_error;
  const busy = H.busy.has(s.id) ? " disabled" : "";
  return `<article class="session-card ${state}" data-sid="${esc(s.id)}">
    <div class="sc-head">
      <span class="dot ${state === "done" ? "" : state}"></span>
      <a class="sc-title" href="/s/${encodeURIComponent(s.id)}/" title="${esc(s.title)}">${esc(s.title)}</a>
      <span class="badge ${badgeCls}">${esc(t("st_" + state))}</span>
    </div>
    ${goal}
    <div class="sc-progress"><div class="meter"><div style="width:${pct}%"></div></div><span>${esc(t("tasks_done", { done: Number(tasks.done) || 0, total: Number(tasks.total) || 0 }))}</span></div>
    <div class="sc-meta">
      <span>👥 <b>${Number(agents.total) || 0}</b> ${esc(t("agents_n"))}${agents.working ? ` · <b>${Number(agents.working) || 0}</b> ${esc(t("working_n"))}` : ""}</span>
      <span>💲 <b>${esc(budget)}</b></span>
      ${s.model ? `<span title="${esc(t("model"))}: ${esc(s.model_profile)}">🧠 ${esc(s.model)}</span>` : ""}
      <span title="${esc(t("created"))}: ${esc(ago(s.created_at))}">🕒 ${esc(ago(s.last_activity || s.created_at))}</span>
    </div>
    ${error ? `<div class="sc-error">${esc(error)}</div>` : ""}
    <div class="sc-actions">
      <a class="btn primary small" href="/s/${encodeURIComponent(s.id)}/">${esc(t("open"))}</a>
      ${running ? `<button class="btn small" type="button" data-stop="${esc(s.id)}"${busy}>⏹ ${esc(t("stop"))}</button>`
                : `<button class="btn small" type="button" data-start="${esc(s.id)}"${busy}>▶ ${esc(t("start"))}</button>`}
      <span class="spacer"></span>
      <span class="sc-id" title="${esc(s.dir || "")}">${esc(s.legacy ? t("legacy") + " · " + s.id : s.id)}</span>
      ${s.legacy ? "" : `<button class="icon-btn small" type="button" data-delete="${esc(s.id)}" title="${esc(t("delete"))}">🗑</button>`}
    </div>
  </article>`;
}

function findSession(sid) { return (H.sessions || []).find((s) => s.id === sid) || { id: sid, title: sid }; }

/** Start or stop a session. Its button stays disabled until the request ends, also across the re-renders of
 * the polled list (starting a swarm can take a few seconds). */
async function sessionAction(sid, action, button) {
  if (H.busy.has(sid)) return;
  H.busy.add(sid);
  if (button) button.disabled = true;
  try {
    const data = await api("POST", `/api/sessions/${encodeURIComponent(sid)}/${action}`, {});
    toast(t(action === "start" ? "started" : "stopped", { title: data.session.title }));
  } catch (e) { toast(e.message, true); }
  H.busy.delete(sid);
  renderSessions();
  loadSessions();
}
function startSession(sid, button) { return sessionAction(sid, "start", button); }
function stopSession(sid, button) { return sessionAction(sid, "stop", button); }

function deleteModal(sid) {
  const s = findSession(sid);
  openModal(`<div class="modal-head"><h3>${esc(t("delete_title"))}</h3></div>
    <p>${esc(t("delete_text", { title: s.title }))}</p>
    <div class="modal-actions">
      <button class="btn" type="button" data-close>${esc(t("cancel"))}</button>
      <button class="btn primary danger-fill" type="button" id="confirm-delete">🗑 ${esc(t("delete"))}</button>
    </div>`);
  const btn = $("#confirm-delete");
  btn.style.background = "var(--err)"; btn.style.borderColor = "var(--err)";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      await api("DELETE", `/api/sessions/${encodeURIComponent(sid)}?confirm=${encodeURIComponent(sid)}`);
      closeModal(); toast(t("deleted")); loadSessions();
    } catch (e) { btn.disabled = false; toast(e.message, true); }
  });
}

// ---- new session -----------------------------------------------------------------------------------
// the defaults the form starts from and compares with: the SAVED settings (H.view may hold unsaved edits)
function savedDefaults() { return H.original || { swarm: {}, agents: {}, models: {}, mcp: {} }; }

async function newSessionModal() {
  if (!H.original) { try { await loadSettings(); } catch (e) { /* the form still works with blanks */ } }
  const v = savedDefaults();
  const sw = v.swarm || {};
  const profiles = Object.entries(v.models || {});
  const def = (v.agents || {}).default_model || "default";
  const modelOpts = profiles.map(([name, p]) =>
    `<option value="${esc(name)}"${name === def ? " selected" : ""}>${esc(name)} — ${esc(p.model || "?")}${name === def ? ` (${esc(t("default_marker"))})` : ""}</option>`).join("");
  openModal(`<div class="modal-head"><h3>✨ ${esc(t("ns_title"))}</h3><button class="icon-btn small" type="button" data-close>✕</button></div>
    <p class="lead">${esc(t("ns_lead"))}</p>
    <form class="new-session" id="ns-form" novalidate>
      <div class="form-grid">
        <label class="field full"><span>${esc(t("f_title"))}</span><input id="ns-title" maxlength="120" placeholder="${esc(t("f_title_ph"))}"></label>
        <label class="field full"><span>🎯 ${esc(t("f_goal"))} *</span><textarea id="ns-goal" rows="3" placeholder="${esc(t("f_goal_ph"))}"></textarea></label>
        <label class="field full"><span>${esc(t("f_desc"))}</span><textarea id="ns-desc" rows="3" placeholder="${esc(t("f_desc_ph"))}"></textarea></label>
        <label class="field two"><span>${esc(t("f_model"))}</span><select id="ns-model">${modelOpts}</select></label>
        <label class="field"><span>${esc(t("f_lang"))}</span><select id="ns-lang">
          <option value="ru"${sw.language === "ru" ? " selected" : ""}>Русский</option>
          <option value="en"${sw.language === "en" ? " selected" : ""}>English</option></select></label>
        <label class="field"><span>${esc(t("f_start_agents"))}</span><input id="ns-start" type="number" min="0" max="50" value="${esc(sw.start_agents ?? 3)}"></label>
        <label class="field"><span>${esc(t("f_max_agents"))}</span><input id="ns-max" type="number" min="1" max="200" value="${esc(sw.max_agents ?? 8)}"></label>
        <label class="field"><span>${esc(t("f_budget"))}</span><input id="ns-budget" type="number" min="0" step="0.5" value="${esc(sw.max_cost_usd ?? 5)}"></label>
        <label class="field full"><span>${esc(t("f_roles"))} <span class="hint">${esc(t("f_roles_hint"))}</span></span>
          <input id="ns-roles" placeholder="${esc(t("f_roles_ph"))}" value="${esc((sw.starting_roles || []).join(", "))}"></label>
      </div>
      <details class="more"><summary>${esc(t("f_more"))}</summary>
        <div class="form-grid">
          <label class="field"><span>${esc(t("f_steps"))}</span><input id="ns-steps" type="number" min="1" max="500" value="${esc((v.agents || {}).max_steps_per_turn ?? 40)}"></label>
          <label class="check full"><input type="checkbox" id="ns-approval"${(v.mcp || {}).require_approval ? " checked" : ""}> ${esc(t("f_approval"))}</label>
        </div>
      </details>
      <div class="err-text" id="ns-error"></div>
      <div class="modal-actions">
        <label class="check" style="margin-right:auto"><input type="checkbox" id="ns-run" checked> ${esc(t("f_start_now"))}</label>
        <button class="btn" type="button" data-close>${esc(t("cancel"))}</button>
        <button class="btn primary" type="submit" id="ns-submit">🚀 ${esc(t("create_open"))}</button>
      </div>
    </form>`);
  $("#ns-goal").focus();
  $("#ns-run").addEventListener("change", (e) => { $("#ns-submit").textContent = "🚀 " + t(e.target.checked ? "create_open" : "create"); });
  $("#ns-form").addEventListener("submit", submitNewSession);
}

/** Session overrides (sessions.create) for the values of the new-session form, relative to the saved
 * defaults `v`. Empty number fields keep the default; starting roles are sent when they differ (even emptied). */
function sessionOverrides(v, form) {
  const num = (x) => { const s = String(x ?? "").trim(); if (s === "") return null; const n = Number(s); return Number.isFinite(n) ? n : null; };
  const sw = v.swarm || {};
  const swarm = { language: form.lang };
  for (const [key, value] of [["start_agents", num(form.start)], ["max_agents", num(form.max)], ["max_cost_usd", num(form.budget)]]) {
    if (value !== null) swarm[key] = value;
  }
  const roles = String(form.roles || "").split(",").map((r) => r.trim()).filter(Boolean);
  if (roles.join("\n") !== (sw.starting_roles || []).join("\n")) swarm.starting_roles = roles;
  const overrides = { swarm };
  const agents = {};
  const steps = num(form.steps);
  if (form.model && form.model !== (v.agents || {}).default_model) agents.default_model = form.model;
  if (steps && steps !== (v.agents || {}).max_steps_per_turn) agents.max_steps_per_turn = steps;
  if (Object.keys(agents).length) overrides.agents = agents;
  if (!!form.approval !== !!(v.mcp || {}).require_approval) overrides.mcp = { require_approval: !!form.approval };
  return overrides;
}

async function submitNewSession(e) {
  e.preventDefault();
  const goal = $("#ns-goal").value.trim();
  if (!goal) { $("#ns-error").textContent = t("goal_required"); $("#ns-goal").focus(); return; }
  const overrides = sessionOverrides(savedDefaults(), {
    lang: $("#ns-lang").value, start: $("#ns-start").value, max: $("#ns-max").value, budget: $("#ns-budget").value,
    roles: $("#ns-roles").value, model: $("#ns-model").value, steps: $("#ns-steps").value, approval: $("#ns-approval").checked,
  });
  const run = $("#ns-run").checked;
  const btn = $("#ns-submit");
  if (btn.disabled) return;
  btn.disabled = true; $("#ns-error").textContent = t("creating");
  try {
    const data = await api("POST", "/api/sessions", { title: $("#ns-title").value.trim(), goal, description: $("#ns-desc").value.trim(), overrides, start: run });
    if (run) { location.href = data.session.url; return; }
    closeModal(true); loadSessions();
  } catch (err) {
    btn.disabled = false; $("#ns-error").textContent = err.message;
  }
}

// ---- modal -----------------------------------------------------------------------------------------
function openModal(html) {
  $("#modal").innerHTML = html;
  $("#modal-backdrop").classList.remove("hidden");
}
/** Closes the modal; a new-session form with typed text asks first (a stray click must not lose a long goal). */
function closeModal(force = false) {
  const typed = ["#ns-goal", "#ns-desc"].some((sel) => { const el = $("#modal " + sel); return el && el.value.trim(); });
  if (!force && typed && !confirm(t("discard_new"))) return false;
  $("#modal-backdrop").classList.add("hidden");
  $("#modal").innerHTML = "";
  return true;
}

// ---- settings: load / render -----------------------------------------------------------------------
async function loadSettings() {
  const data = await api("GET", "/api/settings");
  H.view = data.settings; H.original = clone(data.settings);
  H.providers = data.providers || []; H.path = data.path || "";
  H.settingsError = null;
  renderSettings(true);
}

const SECTIONS = [
  ["models", "s_models"], ["swarm", "s_swarm"], ["agents", "s_agents"], ["shell", "s_shell"],
  ["workspace", "s_workspace"], ["mcp", "s_mcp"], ["tracing", "s_tracing"], ["server", "s_server"],
];

function renderSettings(fromServer) {
  if (fromServer) {
    H.profiles = Object.entries(H.view.models).map(([name, p]) => makeProfile(name, p, name));
    // the default profile is tracked by card, not by name: renaming must not move the star
    const def = H.profiles.find((p) => p.name === H.view.agents.default_model) || H.profiles[0];
    H.defaultUid = def ? def.uid : null;
    H.dirty = false;
  }
  const v = H.view;
  $("#settings-nav").innerHTML = SECTIONS.map(([id, key]) => `<a href="#s-${id}" data-nav="${id}">${esc(t(key))}</a>`).join("");
  $("#settings-form").innerHTML = `
    ${section("models", `<div class="profiles" id="profiles"></div>
      <div class="add-profile"><span class="muted">${esc(t("p_add_from"))}</span>
        <select id="add-preset">${H.providers.map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join("")}</select>
        <button class="btn" type="button" id="add-profile">＋ ${esc(t("p_add"))}</button></div>
      <p class="field-hint">${esc(t("p_key_note"))} <code>${esc(H.path)}</code></p>`)}
    ${section("swarm", grid([
      field("sw_name", "swarm.name", "str"),
      field("sw_lang", "swarm.language", "select", { options: [["ru", "Русский"], ["en", "English"]] }),
      field("sw_start", "swarm.start_agents", "int", { min: 0 }),
      field("sw_max", "swarm.max_agents", "int", { min: 1 }),
      field("sw_budget", "swarm.max_cost_usd", "float", { min: 0, step: 0.5, hint: "sw_budget_hint" }),
      field("sw_roles", "swarm.starting_roles", "list", { full: true, placeholder: t("f_roles_ph") }),
    ]))}
    ${section("agents", grid([
      field("ag_steps", "agents.max_steps_per_turn", "int", { min: 1 }),
      field("ag_context", "agents.context_tokens", "int", { min: 1000, step: 1000 }),
      field("ag_tool_out", "agents.tool_output_chars", "int", { min: 500, step: 500 }),
      field("ag_stagger", "agents.stagger_seconds", "float", { min: 0, step: 0.5 }),
      field("ag_nudge", "agents.idle_nudge_seconds", "float", { min: 0, step: 5 }),
      field("ag_parallel", "agents.max_parallel_llm_calls", "int", { min: 1 }),
    ]))}
    ${section("shell", grid([
      field("sh_program", "shell.program", "select", { options: ["auto", "bash", "sh", "powershell", "pwsh", "cmd"].map((x) => [x, x]) }),
      field("sh_timeout", "shell.timeout", "int", { min: 1 }),
      field("sh_max_timeout", "shell.max_timeout", "int", { min: 1 }),
      field("sh_pass_env", "shell.pass_env", "list", { full: true, hint: "sh_pass_env_hint", placeholder: "GITHUB_TOKEN, HF_TOKEN" }),
    ]))}
    ${section("workspace", grid([
      field("ws_git", "workspace.git", "bool"),
      field("ws_lock", "workspace.lock_ttl_minutes", "int", { min: 1 }),
    ]))}
    ${section("mcp", grid([
      field("mcp_approval", "mcp.require_approval", "bool", { full: true }),
      field("mcp_library", "mcp.library_dir", "str", { full: true }),
      field("mcp_startup", "mcp.startup_timeout", "float", { min: 1 }),
      field("mcp_call", "mcp.call_timeout", "float", { min: 1 }),
      field("mcp_external", "mcp.external", "json", { full: true, placeholder: t("mcp_external_hint"), hint: "mcp_env_hint" }),
    ]))}
    ${section("tracing", grid([
      field("tr_enabled", "tracing.enabled", "bool", { full: true }),
      field("tr_payload", "tracing.max_payload_chars", "int", { min: 1000, step: 1000 }),
      field("tr_retention", "tracing.retention_days", "float", { min: 0, hint: "tr_retention_hint" }),
    ]))}
    ${section("server", grid([
      field("sv_host", "server.host", "str"),
      field("sv_port", "server.port", "int", { min: 1, max: 65535 }),
      field("sv_resume", "server.resume_sessions", "bool", { full: true }),
    ]))}`;
  renderProfiles();
  $("#add-profile").addEventListener("click", addProfile);
  updateSaveBar();
}

function section(id, body) {
  const key = SECTIONS.find((s) => s[0] === id)[1];
  return `<section class="settings-section" id="s-${id}"><h2>${esc(t(key))}</h2><p class="section-hint">${esc(t(key + "_hint"))}</p>${body}</section>`;
}
function grid(fields) { return `<div class="settings-grid">${fields.join("")}</div>`; }

function getPath(path) { return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), H.view); }
function setPath(path, value) {
  const keys = path.split(".");
  let o = H.view;
  for (const k of keys.slice(0, -1)) o = o[k] = o[k] ?? {};
  o[keys[keys.length - 1]] = value;
}

function field(labelKey, path, type, opts = {}) {
  const value = getPath(path);
  const cls = opts.full ? " full" : "";
  const hint = opts.hint ? ` <span class="hint">${esc(t(opts.hint))}</span>` : "";
  const attrs = `data-path="${esc(path)}" data-type="${type}"`;
  if (type === "bool") {
    return `<label class="check${cls}"><input type="checkbox" ${attrs}${value ? " checked" : ""}> ${esc(t(labelKey))}</label>`;
  }
  let input;
  if (type === "select") {
    input = `<select ${attrs}>${opts.options.map(([val, label]) => `<option value="${esc(val)}"${String(value) === val ? " selected" : ""}>${esc(label)}</option>`).join("")}</select>`;
  } else if (type === "json") {
    // text that does not parse yet is kept as typed (a re-render, e.g. a language switch, must not replace it)
    const text = value && value.__invalid ? value.__text || "" : value && Object.keys(value).length ? JSON.stringify(value, null, 2) : "";
    input = `<textarea class="mono" rows="4" ${attrs} data-label="${esc(t(labelKey))}" placeholder="${esc(opts.placeholder || "")}">${esc(text)}</textarea>`;
  } else {
    const shown = type === "list" ? (value || []).join(", ") : (value ?? "");
    const num = type === "int" || type === "float";
    input = `<input ${attrs} ${num ? `type="number" step="${opts.step ?? (type === "int" ? 1 : "any")}"` : 'type="text"'}${opts.min !== undefined ? ` min="${opts.min}"` : ""}${opts.max !== undefined ? ` max="${opts.max}"` : ""} value="${esc(shown)}" placeholder="${esc(opts.placeholder || "")}">`;
  }
  return `<label class="field${cls}"><span>${esc(t(labelKey))}${hint}</span>${input}</label>`;
}

function readInput(el) {
  const type = el.dataset.type;
  if (type === "bool") return el.checked;
  const raw = el.value.trim();
  if (type === "int") return raw === "" ? null : Math.round(Number(raw));
  if (type === "float") return raw === "" ? null : Number(raw);
  if (type === "list") return raw ? raw.split(",").map((x) => x.trim()).filter(Boolean) : [];
  if (type === "json") {
    if (!raw) return {};
    try { return JSON.parse(raw); } catch (e) { return { __invalid: el.dataset.label || "JSON", __text: el.value }; }
  }
  return raw;
}

function onSettingsInput(e) {
  const el = e.target;
  if (el.dataset.path) { setPath(el.dataset.path, readInput(el)); markDirty(); return; }
  const card = el.closest(".profile-card");
  if (card && el.dataset.pf) {
    const p = H.profiles.find((x) => x.uid === card.dataset.uid);
    if (!p) return;
    const pf = el.dataset.pf;
    if (pf === "name") p.name = el.value.trim();
    else if (pf === "new_key") p.newKey = el.value;
    else if (pf === "fallbacks") p.data.fallbacks = el.checked ? "default" : null;
    else if (pf === "extra") p.extraText = el.value;
    else {
      const value = readInput(el);
      p.data[pf] = value === "" ? null : value;
    }
    if (pf === "provider") renderProfiles();
    markDirty();
  }
}

// ---- model profiles --------------------------------------------------------------------------------
function makeProfile(name, data, original) {
  const p = { uid: "p" + (++H.uid), name, original, data: clone(data), newKey: null, cleared: false, keyMode: "env", check: null, models: [] };
  p.keyMode = data.api_key_set || (!data.api_key_env && data.provider !== "anthropic") ? "key" : "env";
  p.extraText = data.extra && Object.keys(data.extra).length ? JSON.stringify(data.extra, null, 2) : "";
  return p;
}

function renderProfiles() {
  const box = $("#profiles");
  if (!box) return;
  // keep the "More" sections the user opened (provider switch, checks and key-mode clicks re-render the cards)
  $$(".profile-card", box).forEach((card) => {
    const p = H.profiles.find((x) => x.uid === card.dataset.uid);
    const more = card.querySelector("details.more");
    if (p && more) p.moreOpen = more.open;
  });
  box.innerHTML = H.profiles.map(profileCard).join("");
}

function profileCard(p) {
  const d = p.data;
  const anthropic = (d.provider || "").toLowerCase() === "anthropic";
  const isDefault = p.uid === H.defaultUid;
  const providers = [["anthropic", "Anthropic"], ["openai", "OpenAI-compatible"]];
  if (d.provider && !providers.some(([v]) => v === d.provider)) providers.push([d.provider, d.provider]);
  const sel = (pf, options, value) => `<select data-pf="${pf}">${options.map(([v, l]) => `<option value="${esc(v)}"${String(value ?? "") === v ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
  const inp = (pf, value, type = "str", extra = "") =>
    `<input data-pf="${pf}" data-type="${type}" ${type === "int" || type === "float" ? 'type="number" step="any"' : 'type="text"'} value="${esc(value ?? "")}" ${extra}>`;
  let keyState = "";
  if (p.keyMode === "env") {
    const saved = ((H.original && H.original.models) || {})[p.original] || {};
    const changed = (d.api_key_env || "") !== (saved.api_key_env || "");
    keyState = !d.api_key_env ? "" : changed ? `<span class="key-state">${esc(t("p_env_later"))}</span>`
      : d.api_key_env_set ? `<span class="key-state ok">${esc(t("p_env_set"))}</span>` : `<span class="key-state bad">${esc(t("p_env_unset"))}</span>`;
    // a stored key wins over the variable (ModelProfile.resolve_api_key), so env mode removes it on save
    if (d.api_key_set) keyState += ` <span class="key-state">${esc(t("p_key_env_drop", { hint: d.api_key_hint || "" }))}</span>`;
  }
  const keyInput = p.keyMode === "env"
    ? `<input data-pf="api_key_env" type="text" value="${esc(d.api_key_env || "")}" placeholder="${esc(t("p_env_ph"))}">`
    : `<div class="input-row"><input data-pf="new_key" type="password" autocomplete="new-password" value="${esc(p.newKey || "")}"
         placeholder="${esc(p.cleared ? t("p_key_cleared") : d.api_key_set ? t("p_key_saved", { hint: d.api_key_hint || "" }) : t("p_key_new"))}">
         ${d.api_key_set && !p.cleared ? `<button class="btn small" type="button" data-clear-key title="${esc(t("p_key_clear"))}">✕</button>` : ""}</div>`;
  const check = p.check ? `<div class="check-result ${p.check.ok ? "ok" : "bad"}">${esc(p.check.text)}</div>` : "";
  const list = `<datalist id="dl-${p.uid}">${(p.models.length ? p.models : presetModels(d)).map((m) => `<option value="${esc(m)}">`).join("")}</datalist>`;
  return `<div class="profile-card${isDefault ? " default" : ""}" data-uid="${p.uid}">
    <div class="profile-head">
      <input class="pname" data-pf="name" value="${esc(p.name)}" title="${esc(t("p_name"))}" maxlength="40">
      ${isDefault ? `<span class="badge">★ ${esc(t("p_default"))}</span>` : `<button class="btn small" type="button" data-make-default>☆ ${esc(t("p_make_default"))}</button>`}
      <span class="spacer"></span>
      <select data-apply-preset><option value="">${esc(t("p_preset"))}</option>${H.providers.map((x) => `<option value="${esc(x.id)}">${esc(x.label)}</option>`).join("")}</select>
      ${H.profiles.length > 1 && !isDefault ? `<button class="icon-btn small" type="button" data-del-profile title="${esc(t("p_delete"))}">🗑</button>` : ""}
    </div>
    <div class="settings-grid">
      <label class="field"><span>${esc(t("p_provider"))}</span>${sel("provider", providers, d.provider)}</label>
      <label class="field"><span>${esc(t("p_base_url"))}</span>${inp("base_url", d.base_url, "str", `placeholder="${esc(t("p_base_url_ph"))}"`)}</label>
      <label class="field full"><span>${esc(t("p_model"))}</span><div class="input-row">${inp("model", d.model, "str", `list="dl-${p.uid}"`)}
        <button class="btn small" type="button" data-load-models${p.busy ? " disabled" : ""}>⟳ ${esc(t("p_load_models"))}</button></div>${list}</label>
      <div class="field full"><span class="key-label">${esc(t("p_key"))}
        <span class="key-mode"><button type="button" data-key-mode="env" class="${p.keyMode === "env" ? "active" : ""}">${esc(t("p_key_env"))}</button><button type="button" data-key-mode="key" class="${p.keyMode === "key" ? "active" : ""}">${esc(t("p_key_value"))}</button></span>
        ${keyState}</span>${keyInput}</div>
      <label class="field"><span>${esc(t("p_max_tokens"))}</span>${inp("max_tokens", d.max_tokens, "int")}</label>
      <label class="field"><span>${esc(t("p_context"))} <span class="hint">${esc(t("p_context_hint"))}</span></span>${inp("context_tokens", d.context_tokens, "int")}</label>
      <label class="field"><span>${esc(t("p_effort"))}</span>${sel("effort", [["", t("none_opt")], ["low", "low"], ["medium", "medium"], ["high", "high"], ["xhigh", "xhigh"], ["max", "max"]], d.effort)}</label>
      ${anthropic ? `<label class="field"><span>${esc(t("p_thinking"))}</span>${sel("thinking", [["adaptive", "adaptive"], ["off", "off"]], d.thinking)}</label>`
                  : `<label class="field"><span>${esc(t("p_max_param"))}</span>${sel("max_tokens_param", [["", "auto"], ["max_tokens", "max_tokens"], ["max_completion_tokens", "max_completion_tokens"]], d.max_tokens_param)}</label>`}
    </div>
    <details class="more"${p.moreOpen ? " open" : ""}><summary>${esc(t("f_more"))}</summary>
      <div class="settings-grid">
        ${anthropic ? `<label class="field"><span>${esc(t("p_thinking_display"))}</span>${sel("thinking_display", [["summarized", "summarized"], ["omitted", "omitted"]], d.thinking_display || "summarized")}</label>
        <label class="field"><span>${esc(t("p_mismatch"))}</span>${sel("thinking_mismatch", [["", t("none_opt")], ["drop_block", "drop_block"], ["error", "error"]], d.thinking_mismatch)}</label>
        <label class="check full"><input type="checkbox" data-pf="fallbacks"${d.fallbacks ? " checked" : ""}> ${esc(t("p_fallbacks"))}</label>` : ""}
        <div class="field full"><span style="font-size:12px;color:var(--muted);font-weight:600">${esc(t("p_prices"))} <span class="hint">${esc(t("p_prices_hint"))}</span></span></div>
        <label class="field"><span>${esc(t("p_price_in"))}</span>${inp("input_price", d.input_price, "float")}</label>
        <label class="field"><span>${esc(t("p_price_out"))}</span>${inp("output_price", d.output_price, "float")}</label>
        <label class="field"><span>${esc(t("p_price_cr"))}</span>${inp("cache_read_price", d.cache_read_price, "float")}</label>
        <label class="field"><span>${esc(t("p_price_cw"))}</span>${inp("cache_write_price", d.cache_write_price, "float")}</label>
        <label class="field full"><span>${esc(t("p_extra"))}</span><textarea class="mono" rows="3" data-pf="extra" placeholder='{"top_p": 0.9}'>${esc(p.extraText || "")}</textarea></label>
      </div>
    </details>
    <div class="profile-actions">
      <button class="btn small" type="button" data-check="0"${p.busy ? " disabled" : ""}>🔌 ${esc(t("p_check"))}</button>
      <button class="btn small" type="button" data-check="1"${p.busy ? " disabled" : ""}>💬 ${esc(t("p_live"))}</button>
      ${H.dirty ? `<span class="field-hint">${esc(t("unsaved_check"))}</span>` : ""}
    </div>
    ${check}
  </div>`;
}

function presetModels(d) {
  const preset = H.providers.find((x) => x.provider === d.provider && (x.base_url || null) === (d.base_url || null));
  return preset ? preset.models || [] : [];
}

function uniqueName(base) {
  const taken = new Set(H.profiles.map((p) => p.name));
  const clean = String(base).replace(/[^A-Za-z0-9_-]/g, "-") || "profile";
  let name = clean;
  for (let i = 2; taken.has(name); i++) name = `${clean}-${i}`;
  return name;
}

function presetData(id) {
  const preset = H.providers.find((x) => x.id === id);
  if (!preset) return null;
  const data = { provider: preset.provider, base_url: preset.base_url || null, model: preset.model || "",
                 api_key_env: preset.api_key_env || null, effort: preset.effort || null };
  for (const k of ["context_tokens", "max_tokens", "input_price", "output_price"]) if (preset[k] !== undefined) data[k] = preset[k];
  return { preset, data };
}

function addProfile() {
  const found = presetData($("#add-preset").value);
  if (!found) return;
  const base = { provider: "openai", model: "", max_tokens: 16000, thinking: "adaptive", thinking_display: "summarized", api_key_set: false };
  const p = makeProfile(uniqueName(found.preset.id), Object.assign(base, found.data), null);
  if (found.preset.api_key) { p.keyMode = "key"; p.newKey = found.preset.api_key; }
  else p.keyMode = "env";
  H.profiles.push(p);
  renderProfiles(); markDirty();
  $(`.profile-card[data-uid="${p.uid}"] .pname`)?.focus();
}

function applyPreset(p, id) {
  const found = presetData(id);
  if (!found) return;
  Object.assign(p.data, found.data);
  if (found.preset.api_key) { p.keyMode = "key"; p.newKey = found.preset.api_key; }
  else if (found.preset.api_key_env) p.keyMode = "env";
  p.models = []; p.check = null;
  renderProfiles(); markDirty();
}

/** The profile as PUT /api/settings expects it. api_key: null keeps the stored key (of `renamed_from` for a
 * renamed card), "" removes it, a string sets it. The stored key never reaches the browser. */
function profilePayload(p) {
  const d = clone(p.data);
  delete d.api_key_set; delete d.api_key_hint; delete d.api_key_env_set; delete d.api_key;
  if (p.extraText && p.extraText.trim()) {
    try { d.extra = JSON.parse(p.extraText); } catch (e) { throw new Error(t("invalid_json", { field: t("p_extra") + " (" + p.name + ")" })); }
    if (!d.extra || typeof d.extra !== "object" || Array.isArray(d.extra)) throw new Error(t("invalid_json", { field: t("p_extra") + " (" + p.name + ")" }));
  } else d.extra = {};
  for (const k of Object.keys(d)) if (d[k] === "") d[k] = null;
  const typed = p.keyMode === "key" && p.newKey && p.newKey.trim() ? p.newKey.trim() : "";
  if (typed) d.api_key = typed;
  // env mode removes a stored key (it would win over the variable); so does "remove key"; a new card has
  // no stored key of its own and must not inherit the key of a deleted profile that had the same name
  else if (p.keyMode === "env" || p.cleared || !p.original) d.api_key = "";
  else d.api_key = null;
  if (p.original && p.original !== p.name) d.renamed_from = p.original;
  return d;
}

/** `text` with the key typed into the form masked (an endpoint may echo the key in its error message). */
function redactKey(text, p) {
  const key = p.newKey && p.newKey.trim();
  return key && key.length >= 4 ? String(text).split(key).join("…" + key.slice(-4)) : String(text);
}

async function checkProfile(p, live, button) {
  let profile;
  try { profile = profilePayload(p); } catch (e) { toast(e.message, true); return; }
  // the server falls back to the stored key of `name` only when the card keeps it (not in env mode, not
  // after "remove key"): otherwise the check would pass with a key that saving is about to delete
  const keepsStored = profile.api_key === null;
  if (profile.api_key === "") profile.api_key = null;
  if (p.busy) return;
  p.busy = true;
  if (button) button.disabled = true;
  p.check = { ok: true, text: t("p_checking") }; renderProfiles();
  try {
    const r = await api("POST", "/api/settings/check", { profile, name: keepsStored ? p.original || null : null, live });
    const lines = [];
    if (r.models && r.models.length) {
      p.models = r.models;
      lines.push(t("p_models_found", { n: r.models.length }));
      if (profile.model) lines.push(t(r.model_found ? "p_model_ok" : "p_model_missing", { model: profile.model }));
    } else if (r.models_error) lines.push("models: " + r.models_error);
    if (live) lines.push(r.ok ? t("p_live_ok", { ms: r.latency_ms, reply: r.reply || "", cost: (Number(r.cost_usd) || 0).toFixed(5) }) : t("p_live_bad", { error: r.error || "?" }));
    p.check = { ok: !!r.ok, text: redactKey(lines.join("\n"), p) };
  } catch (e) {
    p.check = { ok: false, text: redactKey(e.message, p) };
  }
  p.busy = false;
  renderProfiles();
}

function onSettingsClick(e) {
  const nav = e.target.closest("[data-nav]");
  if (nav) { e.preventDefault(); $("#s-" + nav.dataset.nav)?.scrollIntoView({ behavior: "smooth", block: "start" }); return; }
  const card = e.target.closest(".profile-card");
  if (!card) return;
  const p = H.profiles.find((x) => x.uid === card.dataset.uid);
  if (!p) return;
  const btn = e.target.closest("button");
  if (!btn) return;
  if ("makeDefault" in btn.dataset) { H.defaultUid = p.uid; renderProfiles(); markDirty(); }
  else if ("delProfile" in btn.dataset) { if (p.uid !== H.defaultUid) H.profiles = H.profiles.filter((x) => x !== p); renderProfiles(); markDirty(); }
  else if ("keyMode" in btn.dataset) { p.keyMode = btn.dataset.keyMode; renderProfiles(); markDirty(); }
  else if ("clearKey" in btn.dataset) { p.cleared = true; p.newKey = ""; renderProfiles(); markDirty(); }
  else if ("loadModels" in btn.dataset) checkProfile(p, false, btn);
  else if ("check" in btn.dataset) checkProfile(p, btn.dataset.check === "1", btn);
}

function onSettingsChange(e) {
  const el = e.target;
  if (el.matches("[data-apply-preset]")) {
    const card = el.closest(".profile-card");
    const p = H.profiles.find((x) => x.uid === card.dataset.uid);
    if (p && el.value) applyPreset(p, el.value);
    return;
  }
  onSettingsInput(e);
}

// ---- settings: save --------------------------------------------------------------------------------
// every edit refreshes the bar: a save error shown there is stale once the user changes something
function markDirty() { H.dirty = true; updateSaveBar(); }
function updateSaveBar(message, isErr) {
  const bar = $("#save-bar");
  const show = H.tab === "settings" && (H.dirty || !!message);
  bar.classList.toggle("hidden", !show);
  const msg = $("#save-msg");
  msg.textContent = message || (H.dirty ? t("unsaved") : "");
  msg.className = "save-msg" + (isErr ? " err" : "");
}

function collectSettings() {
  const v = clone(H.view);
  for (const [section, values] of Object.entries(v)) {
    if (section === "models" || !values || typeof values !== "object") continue;
    for (const [k, val] of Object.entries(values)) {
      if (val && typeof val === "object" && val.__invalid) throw new Error(t("invalid_json", { field: val.__invalid }));
    }
  }
  const names = new Set();
  const models = {};
  for (const p of H.profiles) {
    if (!PROFILE_NAME.test(p.name || "")) throw new Error(t("p_name_bad", { name: p.name || "" }));
    if (names.has(p.name)) throw new Error(t("p_name_dup", { name: p.name }));
    if (!String(p.data.model || "").trim()) throw new Error(t("p_model_required", { name: p.name }));
    names.add(p.name);
    models[p.name] = profilePayload(p);
  }
  v.models = models;
  const def = H.profiles.find((p) => p.uid === H.defaultUid) || H.profiles[0];
  v.agents = Object.assign({}, v.agents, { default_model: def ? def.name : undefined });
  return v;
}

async function saveSettings() {
  let payload;
  try { payload = collectSettings(); } catch (e) { updateSaveBar(e.message, true); return; }
  const btn = $("#settings-save");
  btn.disabled = true; updateSaveBar(t("saving"));
  try {
    const r = await api("PUT", "/api/settings", { settings: payload });
    H.view = r.settings; H.original = clone(r.settings);
    renderSettings(true);
    const applied = r.applied_to || [];
    toast(applied.length ? t("saved_applied", { list: applied.join(", ") }) : t("saved"));
    updateSaveBar();
  } catch (e) {
    updateSaveBar(e.message, true);
  } finally { btn.disabled = false; }
}

function resetSettings() {
  if (!H.original) return;
  H.view = clone(H.original);
  renderSettings(true);
}

// ---- live updates ----------------------------------------------------------------------------------
function connectWs() {
  const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
  let ws;
  try { ws = new WebSocket(url); } catch (e) { setTimeout(connectWs, 15000); return; }
  H.ws = ws;
  ws.onopen = () => {
    H.wsRetry = 0; $("#conn").classList.add("on"); $("#conn").title = "";
    if (H.wsEverOpen) { // changes made while the socket was down were not pushed: reload
      loadSessions();
      if (H.view && !H.dirty) loadSettings().catch(() => {});
    }
    H.wsEverOpen = true;
  };
  ws.onmessage = (msg) => {
    let ev; try { ev = JSON.parse(msg.data); } catch (e) { return; }
    if (ev.type !== "sessions.changed") return;
    if (ev.change === "settings" && H.view && !H.dirty) loadSettings().catch(() => {});
    else scheduleSessions();
  };
  ws.onclose = () => {
    $("#conn").classList.remove("on"); $("#conn").title = t("conn_lost");
    const delay = Math.min(15000, 1000 * 2 ** H.wsRetry++);
    setTimeout(connectWs, delay);
  };
}

// ---- init ------------------------------------------------------------------------------------------
function init() {
  H.lang = store("dt-lang") || "ru";
  applyTheme(store("dt-theme"));
  applyI18n();
  $$(".home-tabs > button").forEach((b) => b.addEventListener("click", () => {
    if (b.dataset.htab !== H.tab && !leaveSettings()) return;
    setTab(b.dataset.htab);
  }));
  $("#lang-btn").addEventListener("click", () => setLang(H.lang === "ru" ? "en" : "ru"));
  $("#theme-btn").addEventListener("click", () => {
    const cur = document.documentElement.dataset.theme;
    const dark = cur ? cur === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    const next = dark ? "light" : "dark";
    store("dt-theme", next); applyTheme(next);
  });
  $("#new-session-btn").addEventListener("click", newSessionModal);
  $("#session-search").addEventListener("input", (e) => { H.search = e.target.value.trim(); renderSessions(); });
  $$("#session-filter > button").forEach((b) => b.addEventListener("click", () => {
    H.filter = b.dataset.filter;
    $$("#session-filter > button").forEach((x) => x.classList.toggle("active", x === b));
    renderSessions();
  }));
  document.addEventListener("click", (e) => {
    const el = e.target.closest("[data-start],[data-stop],[data-delete],[data-close],[data-new-session]");
    if (!el) return;
    const ds = el.dataset;
    if ("start" in ds) startSession(ds.start, el);
    else if ("stop" in ds) stopSession(ds.stop, el);
    else if ("delete" in ds) deleteModal(ds.delete);
    else if ("close" in ds) closeModal();
    else if ("newSession" in ds) newSessionModal();
  });
  $("#modal-backdrop").addEventListener("mousedown", (e) => { if (e.target.id === "modal-backdrop") closeModal(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#modal-backdrop").classList.contains("hidden")) closeModal(); });
  const form = $("#settings-form");
  form.addEventListener("input", onSettingsInput);
  form.addEventListener("change", onSettingsChange);
  form.addEventListener("click", onSettingsClick);
  form.addEventListener("submit", (e) => e.preventDefault());
  $("#settings-nav").addEventListener("click", onSettingsClick);
  $("#settings-save").addEventListener("click", saveSettings);
  $("#settings-reset").addEventListener("click", resetSettings);
  window.addEventListener("beforeunload", (e) => { if (H.dirty) { e.preventDefault(); e.returnValue = ""; } });
  setTab(location.hash === "#settings" ? "settings" : "sessions", false);
  connectWs();
  setInterval(() => { if (H.tab === "sessions" && !document.hidden) loadSessions(); }, 5000);
}

if (typeof module !== "undefined" && module.exports) {
  // pure helpers, exported for the Node-based UI tests (tests/test_ui.py); the browser runs init() instead
  module.exports = {
    I18N, H, esc, t, ago, money, sessionState, sessionCard, filterSessions, renderSessions, sessionOverrides,
    makeProfile, profileCard, profilePayload, collectSettings, presetData, uniqueName, redactKey, field, readInput,
    renderSettings, checkProfile, onSettingsInput, onSettingsClick, leaveSettings, closeModal, openModal,
    deleteModal, newSessionModal, startSession, stopSession, setTab, loadSettingsPane,
  };
} else if (typeof document !== "undefined") {
  init();
}
