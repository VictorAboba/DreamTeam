"use strict";

/* ---------------------------------------------------------------- i18n */
const I18N = {
  ru: {
    goal: "Цель", budget: "Бюджет", budget_hint: "Потрачено / лимит бюджета", agents: "Агенты", team: "Команда",
    hire: "+ Нанять", everyone: "Все сообщения",
    broadcast_hint: "лента роя и сообщение всем", tab_board: "Доска", tab_feed: "Лента", tab_files: "Файлы",
    tab_mcp: "MCP", tab_triggers: "Триггеры", search: "Поиск задач…", new_task: "+ Задача", chat: "Чат",
    activity: "Активность", type_message: "Сообщение… (Ctrl+Enter — отправить)", send: "Отправить",
    pause: "Пауза", resume: "Продолжить", paused: "Рой на паузе",
    wrap_up: "Завершать", wrap_up_hint: "Попросить команду закругляться: агенты завершат свои задачи с тем, что есть, без новых вычислений и фоновых процессов",
    wrap_up_on_hint: "Режим завершения включён — нажмите, чтобы выключить",
    wrap_up_banner: "Режим завершения: агенты завершают свои задачи с тем, что есть; новые вычисления и фоновые процессы не запускаются",
    no_goal: "Цель не задана — нажмите, чтобы задать",
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
    session_not_started: "Сессия ещё не запускалась: агенты появятся и возьмутся за цель после запуска.",
    goal_pending: "при запуске", goal_pending_hint: "Рой получит эту цель, когда вы запустите сессию.",
    start_session: "Запустить сессию",
    open_task: "Открыть", disconnected: "Нет связи с хабом — переподключаюсь…", live: "Живые обновления подключены",
    review_needed: "Задача ждёт ревью",
    review_drop: "На ревью задача уходит сама, когда исполнитель её завершает",
    review_you: "ждёт вашего ревью", review_needed_you: "Задача ждёт вашего ревью",
    review_instead: "Вы можете принять или вернуть её вместо ревьюера.", review_reason: "Напишите в комментарии, что исправить.",
    reviewer_auto: "— (выберет доска)",
    role_new: "Новая роль — её нет в каталоге: опишите обязанности в инструкциях.",
    instructions_hint: "необязательно для роли из каталога", model_auto: "— профиль роли или сессии", role_model: "профиль роли: {model}",
    goal_split_hint: "первая строка — заголовок, остальные строки — описание (перед подробностями ниже)",
    required: "Заполните обязательные поля", required_task: "Заполните заголовок и цель задачи",
    invalid_limits: "Бюджет должен быть ≥ 0, а максимум агентов — целым числом ≥ 1",
    discard_changes: "Закрыть без сохранения? Введённый текст пропадёт.",
    edit: "Изменить", md_source: "Исходный текст", md_formatted: "Форматированный вид",
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
    ws_folder: "Папка сессии", ws_default: "своя папка", ws_adopted: "папка проекта", ws_moved: "перенесена", ws_missing_badge: "папки нет",
    ws_default_hint: "Собственная папка workspace сессии; удаляется вместе с сессией.",
    ws_adopted_hint: "Существующая папка (ваш проект): агенты работают с её файлами и коммитят в текущую ветку её репозитория. При удалении сессии папка остаётся.",
    ws_moved_hint: "Сессия перенесена в эту папку (/cd). При удалении сессии папка остаётся.",
    ws_missing: "Папка сессии не найдена: {path}. Сессия не запустится, пока её не перенесут в другую папку: /cd ПАПКА или /cd --default в dreamteam tui.",
    "reason_budget": "исчерпан бюджет — увеличьте его в настройках", "reason_paused by the user": "остановлен вами",
    tab_jobs: "Процессы",
    jobs_empty: "Фоновых процессов нет. Процесс появляется, когда команда агента работает дольше своего тайм-аута, запущена в фоне или оставляет работать запущенные процессы.",
    job_running: "работает {t}", job_origin_timeout: "ушёл в фон по тайм-ауту", job_origin_background: "запущен в фоне",
    job_origin_detached: "оставлен командой", job_ended_code: "завершён: код {code}", job_ended: "завершён", job_stopped: "остановлен",
    job_stopped_owner: "остановлен владельцем", job_stopped_user: "остановлен вами", job_stopped_lifetime: "остановлен по пределу времени",
    job_stopped_session: "остановлен вместе с сессией", job_stopped_retired: "остановлен: агент ушёл из роя", job_lost: "потерян при сбое",
    job_output: "Вывод", job_hide: "Скрыть вывод", job_stop: "Остановить", job_stop_confirm: "Остановить процесс {id} и всё, что он запустил?",
    job_log_hint: "Открыть журнал на вкладке «Файлы»", job_took: "проработал {t}", agent_jobs: "фоновых процессов: {n}",
    job_silent: "{t} без вывода", job_silent_hint: "Процесс давно ничего не выводит и, возможно, завис: посмотрите его вывод и остановите, если он застрял.",
    ev_job_started: "{who}: фоновый процесс {id} — {command}", ev_job_ended: "{who}: процесс {id} {result} ({t})",
    dur_s: "{n} с", dur_min: "{n} мин", dur_h: "{h} ч {m} мин",
    sg_jobs: "Фоновые процессы", sf_jobs: "Фоновых процессов на участника", sf_jobs_total: "Фоновых процессов на сессию",
    sf_job_minutes: "Предел жизни фонового процесса, мин (0 — без предела)", sf_cpu: "Потоков вычислений на команду (auto или число)",
    sf_steps: "Шагов за пробуждение", sf_context: "Окно контекста, токенов", sf_tool_out: "Лимит вывода инструмента (симв.)",
    sf_nudge: "Напоминание простаивающим, с", sf_parallel: "Параллельных вызовов LLM",
    sf_hint: "Пустое поле — значение из общих настроек (оно показано серым). Изменения действуют сразу: шаги — со следующего пробуждения агента, остальное — со следующей команды или вызова.",
    sf_context_hint: "окно контекста, заданное в профиле модели, важнее", sf_context_profile: "сейчас действует окно профиля «{profile}»: {n}",
    sf_general_only: "Остальные параметры (фоновые процессы, агенты) задаются в общих настройках.",
    sf_bad_int: "«{field}»: нужно целое число ≥ {min}", sf_bad_num: "«{field}»: нужно число ≥ {min}",
    sf_bad_auto: "«{field}»: auto или целое число ≥ 0", sf_load_failed: "Не удалось загрузить параметры сессии: {error}",
    sf_not_loaded: "Параметры сессии (фоновые процессы, агенты) не загрузились — откройте окно ещё раз.",
    sf_saved_part: "Параметры сессии сохранены, бюджет и максимум агентов — нет",
  },
  en: {
    goal: "Goal", budget: "Budget", budget_hint: "Spent / budget limit", agents: "Agents", team: "Team",
    hire: "+ Hire", everyone: "All messages",
    broadcast_hint: "swarm feed and message to all", tab_board: "Board", tab_feed: "Feed", tab_files: "Files",
    tab_mcp: "MCP", tab_triggers: "Triggers", search: "Search tasks…", new_task: "+ Task", chat: "Chat",
    activity: "Activity", type_message: "Message… (Ctrl+Enter to send)", send: "Send",
    pause: "Pause", resume: "Resume", paused: "The swarm is paused",
    wrap_up: "Wrap up", wrap_up_hint: "Ask the team to wrap up: the agents finish their tasks with what they have, with no new computations or background jobs",
    wrap_up_on_hint: "Wrap-up mode is on: click to switch it off",
    wrap_up_banner: "Wrap-up mode: the agents finish their tasks with what they have; no new computations or background jobs start",
    no_goal: "No goal yet — click to set one",
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
    session_not_started: "The session has not been started yet: the agents appear and take up the goal once you start it.",
    goal_pending: "on start", goal_pending_hint: "The swarm gets this goal when you start the session.",
    start_session: "Start the session",
    open_task: "Open", disconnected: "Lost connection to the hub — reconnecting…", live: "Live updates connected",
    review_needed: "Task waits for review",
    review_drop: "A task goes to review by itself when its assignee completes it",
    review_you: "waits for your review", review_needed_you: "The task waits for your review",
    review_instead: "You can approve or return it instead of the reviewer.", review_reason: "Write in the comment what to fix.",
    reviewer_auto: "— (the board picks)",
    role_new: "A new role, not in the catalog: describe its responsibilities in the instructions.",
    instructions_hint: "optional for a catalog role", model_auto: "— the role's or the session's profile", role_model: "role profile: {model}",
    goal_split_hint: "the first line is the title, the other lines the description (before the details below)",
    required: "Fill in the required fields", required_task: "Fill in the title and the task goal",
    invalid_limits: "The budget must be ≥ 0 and max agents a whole number ≥ 1",
    discard_changes: "Close without saving? Your text will be lost.",
    edit: "Edit", md_source: "Source", md_formatted: "Formatted",
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
    ws_folder: "Session folder", ws_default: "own folder", ws_adopted: "project folder", ws_moved: "moved", ws_missing_badge: "missing",
    ws_default_hint: "The session's own workspace folder; deleted with the session.",
    ws_adopted_hint: "An existing folder (your project): the agents work with its files and commit to the current branch of its repository. Deleting the session keeps the folder.",
    ws_moved_hint: "The session was moved to this folder (/cd). Deleting the session keeps the folder.",
    ws_missing: "The session folder is missing: {path}. The session cannot start until it is moved to another folder: /cd FOLDER or /cd --default in dreamteam tui.",
    "reason_budget": "the budget is spent — raise it in the settings", "reason_paused by the user": "paused by you",
    tab_jobs: "Jobs",
    jobs_empty: "No background jobs. A job appears when an agent's command runs past its timeout, is started in the background or leaves processes running.",
    job_running: "running {t}", job_origin_timeout: "went to the background at its timeout", job_origin_background: "started in the background",
    job_origin_detached: "left running by its command", job_ended_code: "ended: exit code {code}", job_ended: "ended", job_stopped: "stopped",
    job_stopped_owner: "stopped by its owner", job_stopped_user: "stopped by you", job_stopped_lifetime: "stopped at the time limit",
    job_stopped_session: "stopped with the session", job_stopped_retired: "stopped: its agent left the swarm", job_lost: "lost in a crash",
    job_output: "Output", job_hide: "Hide output", job_stop: "Stop", job_stop_confirm: "Stop job {id} and everything it started?",
    job_log_hint: "Open the log in the Files tab", job_took: "ran {t}", agent_jobs: "background jobs: {n}",
    job_silent: "no output for {t}", job_silent_hint: "It has printed nothing for a long time and may hang: look at its output, and stop it if it is stuck.",
    ev_job_started: "{who}: background job {id} — {command}", ev_job_ended: "{who}: job {id} {result} ({t})",
    dur_s: "{n} s", dur_min: "{n} min", dur_h: "{h} h {m} min",
    sg_jobs: "Background jobs", sf_jobs: "Background jobs per member", sf_jobs_total: "Background jobs per session",
    sf_job_minutes: "Background job time limit, min (0 = none)", sf_cpu: "Compute threads per command (auto or a number)",
    sf_steps: "Steps per wake-up", sf_context: "Context window, tokens", sf_tool_out: "Tool output limit (chars)",
    sf_nudge: "Nudge idle agents after, s", sf_parallel: "Parallel LLM calls",
    sf_hint: "An empty field takes the value of the general settings (shown in grey). Changes apply at once: the steps from the agent's next wake-up, the rest from the next command or call.",
    sf_context_hint: "a context window set in the model profile wins", sf_context_profile: "the profile “{profile}” sets {n} now",
    sf_general_only: "The other parameters (background jobs, agents) are set in the general settings.",
    sf_bad_int: "“{field}”: a whole number ≥ {min} is needed", sf_bad_num: "“{field}”: a number ≥ {min} is needed",
    sf_bad_auto: "“{field}”: auto or a whole number ≥ 0", sf_load_failed: "Could not load the session parameters: {error}",
    sf_not_loaded: "The session parameters (background jobs, agents) did not load: open the dialog again.",
    sf_saved_part: "The session parameters are saved, the budget and max agents are not",
  },
};

const COLUMNS = ["todo", "in_progress", "waiting", "review", "done", "closed"];
const ALL_STATUSES = ["todo", "in_progress", "waiting", "review", "done", "failed", "cancelled"];
const ACTIVITY_PROSE = new Set(["text", "thinking", "compacted"]);   // activity kinds the LLM wrote: shown as Markdown
// the feed asks the server only for these types: agent.activity / agent.usage would crowd everything else out
const FEED_TYPES = ["task.*", "message.*", "goal.*", "agent.hired", "agent.retired", "file.*", "trigger.*", "mcp.*", "swarm.*", "job.*"];
const FEED_SKIP = new Set(["agent.activity", "agent.usage", "notification", "agent.status", "agent.compacted"]);
const DND_TYPE = "application/x-dreamteam-task";
const MAX_SEEN = 5000;
const MAX_NODES = 800;
const RAIL_QUERY = "(max-width: 1279px)";
const NARROW_QUERY = "(max-width: 900px)";
const JOB_TAIL = 200;        // lines of a job's output the Jobs tab shows
const JOB_REFRESH = 3000;    // ms: an open output of a running job (and the running times) is refreshed this often
const S = {
  state: null, lang: "ru", tab: "board", chatWith: null, dtab: "chat", fileDir: ".", fileEntries: [], openFile: null,
  search: "", feed: [], feedMaxId: 0, activityMaxId: 0, seenEvents: new Set(), userInbox: [], replyTo: null,
  openTaskId: null, taskBase: null, lastPostId: 0, ws: null, wsRetry: 0, wsEverOpen: false, dragging: null,
  boardDirty: false, appliedSeq: 0, modalInitial: null, markingRead: new Set(), drafts: {},
  jobOpen: new Set(), jobOutputs: {}, jobLoading: new Set(),   // Jobs tab: ids whose output is shown, its text
  fileText: "", fileSource: false,   // Files tab: the open file's text; a Markdown file shown as its source
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
/** Colours of the members: the palette of the terminal UI (tui/theme.py AGENT_PALETTE) and the same choice
 * (crc32 of the case-folded name), so an agent has one colour in the terminal and in the browser. */
const AGENT_PALETTE = "#5f87d7 #2aa198 #9c6ade #d33682 #859900 #268bd2 #b58900 #6c71c4 #3a9e6e #c0607e #4f97a3 #a0794f".split(" ");
let CRC_TABLE = null;
function crc32(text) {
  if (!CRC_TABLE) {
    CRC_TABLE = new Uint32Array(256);
    for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; CRC_TABLE[n] = c >>> 0; }
  }
  let crc = 0xffffffff;
  for (const byte of new TextEncoder().encode(text)) crc = CRC_TABLE[(crc ^ byte) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}
function agentColor(name) { return AGENT_PALETTE[crc32(String(name).split(/\s+/).filter(Boolean).join(" ").toLowerCase()) % AGENT_PALETTE.length]; }
function initials(name) { return String(name).split(/\s+/).filter(Boolean).slice(0, 2).map((p) => p[0].toUpperCase()).join(""); }
function avatar(name, cls = "") {
  if (name === "user") return `<span class="avatar user ${cls}">U</span>`;
  return `<span class="avatar ${cls}" style="--c:${agentColor(name)}">${esc(initials(name))}</span>`;
}
function displayName(name) { return name === "user" ? t("user") : name === null || name === undefined ? t("system") : name; }
function reEscape(text) { return String(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
function norm(text) { return String(text ?? "").replace(/\r\n?/g, "\n"); }
function short(text, n = 160) { text = String(text || "").replace(/\s+/g, " "); return text.length > n ? text.slice(0, n - 1) + "…" : text; }

let mentionMemo = { key: null, re: null };   // the mention regex of the last list of names (md() calls it per text run)
/** Wraps `@Full Name` mentions of known members (and `@user`) in already escaped HTML. */
function highlightMentions(html, names) {
  const list = (names || members(true).map((a) => a.name)).concat(["user"]).filter(Boolean);
  const key = list.join("\u0001");
  if (mentionMemo.key !== key) {
    const alts = Array.from(new Set(list)).sort((a, b) => b.length - a.length).map((n) => reEscape(esc(n)).replace(/\s+/g, "\\s+"));
    mentionMemo = { key, re: new RegExp(`(^|[^\\p{L}\\p{N}_&])@(${alts.join("|")})(?![\\p{L}\\p{N}_])`, "giu") };
  }
  return html.replace(mentionMemo.re, '$1<span class="mention">@$2</span>');
}
/** Task links (`#12`) and mentions in already escaped HTML. */
function decorate(html, names) {
  return highlightMentions(html.replace(/(^|[\s(\[,:])#(\d+)\b/g, '$1<button type="button" class="link" data-open-task="$2">#$2</button>'), names);
}
/** Escapes text, turns `#12` into task links and highlights mentions. Everything except the inserted markup is escaped. */
function linkify(text, names) { return decorate(esc(text), names); }

// markdown.js: a global of the page (loaded before this script), a module in the Node tests
const MARKDOWN = typeof Markdown !== "undefined" ? Markdown : require("./markdown.js");
const HIGHLIGHT = typeof Highlight !== "undefined" ? Highlight : require("./highlight.js");   // highlight.js, the same way
const MD_CACHE = new Map();
const MD_CACHE_MAX = 400;      // rendered texts kept (the chat alone re-renders up to 300 messages on a refresh)
const MD_CACHE_CHARS = 50000;  // longer texts (a big .md file) are rendered every time instead of being kept
/** A text written by an agent, an external member or the user, formatted as Markdown (markdown.js: escaped, no raw
 * HTML) with the board's task links and mentions in its plain text: `<div class="md">…</div>`. */
function md(text, names) {
  const src = String(text ?? "");
  const list = names || members(true).map((a) => a.name);
  const key = list.join("\u0001") + "\u0002" + src;
  let html = MD_CACHE.get(key);
  if (html !== undefined) { MD_CACHE.delete(key); MD_CACHE.set(key, html); return html; }   // most recently used last
  html = `<div class="md">${MARKDOWN.render(src, { decorate: (h) => decorate(h, list) })}</div>`;
  if (src.length <= MD_CACHE_CHARS) {
    MD_CACHE.set(key, html);
    if (MD_CACHE.size > MD_CACHE_MAX) MD_CACHE.delete(MD_CACHE.keys().next().value);
  }
  return html;
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
/** A launcher route (/api/sessions/{sid}, /api/settings): it is not under the session's BASE. */
async function launcherApi(path, options) {
  const resp = await fetch(path, options);
  if (!resp.ok) throw new Error(responseError(resp.status, await resp.text()));
  return resp.json();
}
/** The id of the launcher session this board belongs to; "" for a standalone swarm app. */
function sessionSid() { return BASE ? BASE.split("/").pop() : ""; }
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
function memberOptions(selected, withUser = true, noneLabel = t("none")) {
  selected = selected || "";
  const opts = [`<option value=""${selected ? "" : " selected"}>${esc(noneLabel)}</option>`];
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
/** The "no reviewer" option: with independent review the board picks a reviewer when the task is completed. */
function reviewerNone() { return S.state && S.state.swarm && S.state.swarm.independent_review ? t("reviewer_auto") : t("none"); }
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
  if (S.tab === "files") { renderCrumbs(); renderFileList(); if (S.openFile) renderFileContent(); }
  if (S.chatWith) { loadMessages(); if (S.dtab === "activity") loadActivity(); }
  if (typeof tracesLangChanged === "function") tracesLangChanged();
}

function renderAll() {
  if (!S.state) return;
  syncWorkspace();
  renderHeader();
  renderAgents();
  if (S.tab === "board") renderBoard();
  if (S.tab === "mcp") renderMcp();
  if (S.tab === "triggers") renderTriggers();
  if (S.tab === "jobs") renderJobs();
  if (S.tab === "files") renderFileList();
  if (S.chatWith) renderDrawerHead();
}

/* ---------------------------------------------------------------- header */
/** The wrap-up toggle: on (the team finishes with what it has, no new background jobs) or back off. */
function toggleWrapUp() {
  if (!S.state) return undefined;
  return act("swarm_control", { wrap_up: !S.state.swarm.wrap_up });
}

function renderHeader() {
  const sw = S.state.swarm;
  const session = sw.session || null;
  const title = (session && session.title) || sw.name;
  $("#swarm-name").textContent = title;
  document.title = `DreamTeam — ${title}`;
  $("#back-link").classList.toggle("hidden", !BASE);
  $("#stopped-banner").classList.toggle("hidden", !(session && session.status === "stopped"));
  const goal = sw.goal;
  // a session created without starting keeps its goal until the first start: show it, marked as such
  const pendingGoal = !goal && session && session.pending_goal ? session.pending_goal : null;
  const shown = goal || pendingGoal;
  $("#stopped-banner span").textContent = t(pendingGoal ? "session_not_started" : "session_stopped");
  $("#goal-title").textContent = shown ? shown.title : t("no_goal");
  $("#goal-btn").title = shown ? shown.title + (shown.description ? "\n\n" + shown.description : "") +
    (pendingGoal ? "\n\n" + t("goal_pending_hint") : "") : t("no_goal");
  const gs = $("#goal-status");
  gs.textContent = goal ? (goal.status === "done" ? t("goal_done") : t("goal_active")) : pendingGoal ? t("goal_pending") : "";
  gs.className = "badge" + (goal && goal.status === "done" ? " done" : "") + (pendingGoal ? " warn" : "");
  gs.classList.toggle("hidden", !shown);
  const limit = sw.limits.max_cost_usd;
  $("#budget-text").textContent = `$${(sw.spent_usd || 0).toFixed(2)}` + (limit > 0 ? ` / $${limit.toFixed(2)}` : " / ∞");
  const pct = limit > 0 ? Math.min(100, (sw.spent_usd / limit) * 100) : 0;
  const bar = $("#budget-bar");
  bar.style.width = pct + "%";
  bar.className = pct >= 100 ? "err" : pct >= 80 ? "warn" : "";
  // what max_agents counts: the AI agents (members who joined through MCP do not count)
  const counted = sw.agent_count ?? members().filter((a) => a.kind === "llm").length;
  $("#agents-count").textContent = `${counted} / ${sw.limits.max_agents}`;
  const pause = $("#pause-btn");
  setHtml(pause, `<span aria-hidden="true">${sw.paused ? '<i class="ic ic-play"></i>' : '<i class="ic ic-pause"></i>'}</span><span class="btn-label">${esc(sw.paused ? t("resume") : t("pause"))}</span>`);
  pause.title = sw.paused ? t("resume") : t("pause");
  pause.classList.toggle("primary", !!sw.paused);
  const banner = $("#paused-banner");
  banner.classList.toggle("hidden", !sw.paused);
  banner.textContent = sw.paused ? `${t("paused")}${sw.pause_reason ? ": " + tv("reason_", sw.pause_reason) : ""}` : "";
  // the user's wrap-up mode: a toggle next to Pause (pressed while on) and a line under the header
  const wrapping = !!sw.wrap_up;
  const wrap = $("#wrap-btn");
  setHtml(wrap, `<span aria-hidden="true"><i class="ic ic-check"></i></span><span class="btn-label">${esc(t("wrap_up"))}</span>`);
  wrap.title = wrapping ? t("wrap_up_on_hint") : t("wrap_up_hint");
  wrap.classList.toggle("on", wrapping);
  wrap.setAttribute("aria-pressed", String(wrapping));
  const wrapBanner = $("#wrapup-banner");
  wrapBanner.classList.toggle("hidden", !wrapping);
  wrapBanner.textContent = wrapping ? t("wrap_up_banner") : "";
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
  const jobs = runningJobs().length;
  const jobsBadge = $("#jobs-badge");
  jobsBadge.textContent = jobs || "";
  jobsBadge.classList.toggle("hidden", !jobs);
  const unread = S.userInbox.filter((m) => !m.read).length;
  const teamBadge = $("#team-unread");
  teamBadge.textContent = unread || "";
  teamBadge.classList.toggle("hidden", !unread);
  // the folder the session works in: a missing one (deleted, unplugged drive) is said at the top of every tab
  const ws = sw.workspace || null;
  const missing = workspaceMissing(ws);
  const wsBanner = $("#workspace-banner");
  wsBanner.classList.toggle("hidden", !missing);
  wsBanner.textContent = missing ? t("ws_missing", { path: ws.path }) : "";
  const filesTab = $('#tabs > button[data-tab="files"]');
  if (filesTab) filesTab.title = ws && ws.path ? `${t("ws_folder")}: ${ws.path}` : "";
  setHtml($("#workspace-info"), workspaceHtml(ws));
}

/* ---------------------------------------------------------------- session folder */
/** "default" (the session's own workspace/), "adopted" (an existing folder, e.g. the user's project) or
 * "moved" (a folder the session was moved to with /cd); null when the state has no `swarm.workspace`. */
function workspaceKind(ws) {
  if (!ws || typeof ws !== "object" || !ws.path) return null;
  return ws.default ? "default" : ws.adopted ? "adopted" : "moved";
}
/** A folder outside the session that does not exist (its own folder is created on demand: never missing). */
function workspaceMissing(ws) { return !!(workspaceKind(ws) && !ws.default && !ws.exists); }
/** The head of the Files tab: the absolute path of the session folder (click to copy) and what kind it is
 * (a missing folder is explained by the banner above the tabs). */
function workspaceHtml(ws) {
  const kind = workspaceKind(ws);
  if (!kind) return "";
  const missing = workspaceMissing(ws);
  const label = missing ? t("ws_missing_badge") : t("ws_" + kind);
  return `<span class="ws-label"><i class="ic ic-folder"></i>${esc(t("ws_folder"))}</span>`
    + `<span class="badge${missing ? " err" : kind === "default" ? " plain" : ""}" title="${esc(t("ws_" + kind + "_hint"))}">${esc(label)}</span>`
    + `<code class="ws-path" id="ws-path" title="${esc(t("copy_hint"))}">${esc(ws.path)}</code>`;
}
/** After /cd (or the folder coming back) the Files tab starts again from the root of the new folder. */
function syncWorkspace() {
  const ws = S.state && S.state.swarm ? S.state.swarm.workspace : null;
  const key = ws ? `${ws.path}\n${ws.exists}` : "";
  if (S.workspaceKey === undefined || S.workspaceKey === key) { S.workspaceKey = key; return; }
  S.workspaceKey = key;
  S.fileDir = "."; S.fileEntries = []; S.openFile = null; S.fileText = "";
  $("#file-path").textContent = t("select_file");
  renderFileContent();
  if (S.tab === "files") openDir(".");
}

/* ---------------------------------------------------------------- board */
function taskMatches(task) {
  if (!S.search) return true;
  const hay = [task.title, task.goal, task.description, task.assignee, (task.tags || []).join(" "), "#" + task.id].join(" ").toLowerCase();
  return hay.includes(S.search.toLowerCase());
}
function columnOf(task) { return task.status === "failed" || task.status === "cancelled" ? "closed" : task.status; }
/** What dropping `task` on column `col` does: null (nothing), {error: i18n key}, {review: true} (approve),
 * {open: true} (open the card: returning a task needs a reason) or {status}. */
function dropAction(task, col) {
  if (!task || !COLUMNS.includes(col) || columnOf(task) === col) return null;
  if (col === "review") return { error: "review_drop" };
  if (task.status === "review" && col === "done") return { review: true };
  if (task.status === "review" && col !== "closed") return { open: true };
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
    ? `<span class="blocked"><i class="ic ic-blocked"></i>${esc(t("blocked_by"))} ${task.blocked_by.map((b) => "#" + Number(b)).join(", ")}</span>` : "";
  const assignee = task.assignee ? `<span class="chip">${avatar(task.assignee, "sm")}${esc(displayName(task.assignee))}</span>` : "";
  const tags = (task.tags || []).map((g) => `<span class="tag">${esc(g)}</span>`).join("");
  const comments = task.comments ? `<span class="chip plain"><i class="ic ic-chat"></i>${Number(task.comments)}</span>` : "";
  const review = task.status === "review" && task.reviewer ? (task.reviewer === "user"
    ? `<span class="badge warn"><i class="ic ic-eye"></i>${esc(t("review_you"))}</span>` : `<span class="chip plain"><i class="ic ic-eye"></i>${esc(displayName(task.reviewer))}</span>`) : "";
  const failed = task.status === "failed" ? `<span class="badge err">${esc(t("s_failed"))}</span>` : task.status === "cancelled" ? `<span class="badge st-cancelled">${esc(t("s_cancelled"))}</span>` : "";
  const prio = Math.max(0, Math.min(3, Number(task.priority) || 0));
  return `<div class="card p${prio}" draggable="true" tabindex="0" role="button" data-id="${Number(task.id)}" title="${esc(t("p" + prio))}">
    <div class="card-top"><span class="card-id">#${Number(task.id)}</span><span class="card-title">${esc(task.title)}</span></div>
    ${task.goal ? `<div class="card-goal"><i class="ic ic-goal"></i>${esc(task.goal)}</div>` : ""}
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
  if (action.review) { act("task_review", { task_id: id, approve: true }, true); return; }
  if (action.open) { openTask(id).then(() => { $("#review-comment")?.focus(); toast(t("review_reason")); }); return; }
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
    const jobs = Number(a.jobs) || 0;   // its running background jobs: ⚙N (a member asleep while they work)
    const tip = `${a.name} — ${a.role} · ${status}${tasks ? " · " + tasks : ""}${jobs ? " · " + t("agent_jobs", { n: jobs }) : ""}`;
    const mark = jobs ? `<span class="jobs-mark">⚙${jobs}</span> ` : "";
    return `<button type="button" class="agent ${a.status === "retired" ? "retired" : ""}${S.chatWith === a.name ? " active" : ""}" data-agent="${esc(a.name)}" title="${esc(tip)}">
      <span class="avatar-wrap">${avatar(a.name)}<span class="status-dot ${esc(a.status)}"></span></span>
      <span class="agent-info"><span class="agent-name">${esc(a.name)}</span><span class="agent-role">${esc(a.role)}${a.kind === "external" ? " · MCP" : ""}</span></span>
      <span class="agent-side"><span>${esc(status)}</span><span>${mark}${esc(tasks)} $${(a.cost_usd || 0).toFixed(2)}</span></span>
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
      <button class="btn small" type="button" data-traces="${esc(a.name)}"><i class="ic ic-pulse"></i>${esc(t("traces"))}</button>
      ${a.status !== "retired" ? `<button class="btn small danger" type="button" data-retire="${esc(a.name)}">${esc(t("retire"))}</button>` : ""}`;
  }
  setHtml($("#drawer-title"), html);
  // the member's running background jobs, each with a stop button
  const jobs = a ? runningJobs(a.name) : [];
  const jobsBox = $("#drawer-jobs");
  setHtml(jobsBox, jobs.map((j) => `<div class="drawer-job"><span class="job-label">⚙ ${esc(jobLabel(j))}</span>
    <code title="${esc(j.command)}">${esc(short(j.command, 90))}</code><span class="muted">${esc(fmtDur(nowSeconds() - j.started_at))}</span>
    <button class="btn small danger" type="button" data-job-stop="${Number(j.id)}">${esc(t("job_stop"))}</button></div>`).join(""));
  jobsBox.classList.toggle("hidden", !jobs.length);
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
  return `<div class="msg ${me ? "me" : ""}"><div class="meta">${esc(displayName(m.sender))}${to} · ${esc(fmtTime(m.ts))}</div>${md(m.text)}</div>`;
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
  const head = `<b>${esc(tv("k_", kind))}</b>${d.tool ? `<code>${esc(d.tool)}</code>` : ""}<span>${esc(fmtTime(item.ts))}</span>`;
  // the agent's own words (its reply, its thinking) are Markdown; tool calls, results and errors stay as they are
  const body = !d.text ? "" : ACTIVITY_PROSE.has(kind) ? md(d.text) : `<pre>${esc(d.text)}</pre>`;
  return `<div class="act ${esc(kind)}${d.is_error ? " err" : ""}"><div class="act-head">${head}</div>${body}</div>`;
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
  // not a disabled input: a formatted text's task marks (.md li.md-task) come before the fields and take no focus
  if (focus) { const first = $("#modal input:not([disabled]), #modal textarea:not([disabled])"); if (first) first.focus(); }
}
function modalOpen() { return !$("#modal-backdrop").classList.contains("hidden"); }
/** True when closing the modal would throw away something the user typed. */
function modalHasUnsaved() {
  if (!modalOpen()) return false;
  if (S.taskBase) {
    if (($("#post-text")?.value || "").trim() || ($("#review-comment")?.value || "").trim()) return true;
    return Object.keys(dirtyFields(S.taskBase, readTaskForm())).length > 0;
  }
  // an emptied field is no edit, except where emptying it means something (data-empty-edit: a session setting
  // cleared to take the global value again)
  return $$("#modal input, #modal textarea").some((el) => el.id && S.modalInitial && S.modalInitial.has(el.id)
    && el.value !== S.modalInitial.get(el.id) && (el.value.trim() !== "" || el.hasAttribute("data-empty-edit")));
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
    else if (id === "f-assignee" || id === "f-reviewer") el.innerHTML = memberOptions(valueOf(task), true, id === "f-reviewer" ? reviewerNone() : undefined);
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
  if ((task.blocked_by || []).length) parts.push(`<span class="blocked"><i class="ic ic-blocked"></i>${esc(t("blocked_by"))}</span> ${task.blocked_by.map((b) => `<button type="button" class="link" data-open-task="${Number(b)}">#${Number(b)}</button>`).join(", ")}`);
  return parts.join(" · ");
}
function taskResultHtml(task) {
  return task.result ? `<div class="section-title">${esc(t("result"))}</div><div class="result-box">${md(task.result)}</div>` : "";
}
/** A long text field shown formatted (Markdown) with «Изменить» that opens its textarea. The textarea is always in the
 * form (hidden while the text is read), so the form reads and compares it as before; an empty text starts editable. */
function mdFieldHtml(id, label, text, rows) {
  const reading = norm(text).trim() !== "";
  return `<div class="field full md-field"><div class="md-field-head"><span>${esc(label)}</span>
      <button class="link${reading ? "" : " hidden"}" type="button" data-md-edit="${esc(id)}"><i class="ic ic-edit"></i>${esc(t("edit"))}</button></div>
    <div class="md-view${reading ? "" : " hidden"}" id="${esc(id)}-view">${reading ? md(text) : ""}</div>
    <textarea id="${esc(id)}" rows="${Number(rows)}" aria-label="${esc(label)}"${reading ? ' class="hidden"' : ""}></textarea></div>`;
}
/** The field after a live update: a text being read shows the new one (`read`: back to reading, after a save);
 * one being edited stays in its textarea; an emptied one opens it. */
function syncMdField(id, text, read = false) {
  const box = $("#" + id), view = $("#" + id + "-view"), btn = $(`[data-md-edit="${id}"]`);
  if (!box || !view) return;
  const reading = norm(text).trim() !== "" && (read || box.classList.contains("hidden"));
  setHtml(view, reading ? md(text) : "");
  view.classList.toggle("hidden", !reading);
  box.classList.toggle("hidden", reading);
  if (btn) btn.classList.toggle("hidden", !reading);
}
/** «Изменить»: the textarea in place of the formatted text. */
function editMdField(id) {
  const box = $("#" + id);
  if (!box) return;
  $("#" + id + "-view")?.classList.add("hidden");
  $(`[data-md-edit="${id}"]`)?.classList.add("hidden");
  box.classList.remove("hidden");
  box.focus();
}
function taskReviewHtml(task) {
  if (task.status !== "review") return "";
  const title = task.reviewer === "user" ? esc(t("review_needed_you"))
    : esc(t("review_needed")) + (task.reviewer ? ` · ${esc(t("reviewer"))}: ${esc(displayName(task.reviewer))}` : "");
  return `<div class="section-title">${title}</div>
    ${task.reviewer && task.reviewer !== "user" ? `<p class="muted">${esc(t("review_instead"))}</p>` : ""}
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
    <div class="modal-head"><h3 id="task-head">${taskHeadHtml(task)}</h3><button class="icon-btn" type="button" data-close title="${esc(t("close"))}" aria-label="${esc(t("close"))}"><i class="ic ic-close"></i></button></div>
    <div class="form-grid" id="task-form">
      <label class="field full"><span>${esc(t("title"))}</span><input id="f-title" maxlength="200"></label>
      <label class="field full"><span><i class="ic ic-goal"></i>${esc(t("goal_field"))}</span><textarea id="f-goal" rows="2" placeholder="${esc(t("goal_hint"))}"></textarea></label>
      ${mdFieldHtml("f-desc", t("description"), task.description, 4)}
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
  syncMdField("f-desc", task.description, true);
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
  syncMdField("f-desc", task.description, force);
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
    const replyLink = p.reply_to ? ` <button class="link reply-link" type="button" data-goto-post="${Number(p.reply_to)}" title="${esc(t("reply_to_post"))}"><i class="ic ic-reply"></i>${esc(parent ? displayName(parent.author) : "#" + Number(p.reply_to))}</button>` : "";
    return `<div class="post${depth ? " reply" : ""}" id="post-${Number(p.id)}" style="margin-left:${Math.min(depth, 4) * 22}px">
    <div class="post-head">${avatar(p.author, "sm")}<b>${esc(displayName(p.author))}</b>${replyLink}<span>· ${esc(fmtTime(p.ts))}</span>
    <span class="post-actions"><button class="link" type="button" data-reply="${Number(p.id)}">${esc(t("reply"))}</button></span></div>
    <div class="post-body">${md(p.text)}</div></div>`;
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
  el.innerHTML = `${esc(t("replying_to"))} <b>${esc(parent ? parent.textContent : "#" + postId)}</b> <button class="link" type="button" id="reply-cancel" title="${esc(t("cancel"))}" aria-label="${esc(t("cancel"))}"><i class="ic ic-close"></i></button>`;
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
  // a return without a reason gives the agent nothing to act on
  if (!approve && !comment) { toast(t("review_reason"), "err"); $("#review-comment")?.focus(); return; }
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
  openModal(`<div class="modal-head"><h3>${esc(t("new_task"))}</h3><button class="icon-btn" type="button" data-close title="${esc(t("close"))}" aria-label="${esc(t("close"))}"><i class="ic ic-close"></i></button></div>
    <div class="form-grid">
      <label class="field full"><span>${esc(t("title"))} *</span><input id="n-title" maxlength="200"></label>
      <label class="field full"><span><i class="ic ic-goal"></i>${esc(t("goal_field"))} *</span><textarea id="n-goal" rows="2" placeholder="${esc(t("goal_hint"))}"></textarea></label>
      <label class="field full"><span>${esc(t("description"))}</span><textarea id="n-desc" rows="4"></textarea></label>
      <label class="field"><span>${esc(t("assignee"))}</span><select id="n-assignee">${memberOptions("")}</select></label>
      <label class="field"><span>${esc(t("reviewer"))}</span><select id="n-reviewer">${memberOptions("", true, reviewerNone())}</select></label>
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
  const session = S.state.swarm.session;
  const goal = S.state.swarm.goal || (session && session.pending_goal) || null;
  const done = goal && goal.status === "done";
  openModal(`<div class="modal-head"><h3><i class="ic ic-goal"></i> ${esc(t("set_goal"))}</h3><button class="icon-btn" type="button" data-close title="${esc(t("close"))}" aria-label="${esc(t("close"))}"><i class="ic ic-close"></i></button></div>
    ${done ? `<div class="section-title">${esc(t("goal_summary"))}</div><div class="result-box">${md(goal.summary || "")}</div>
      <label class="field" style="margin-top:10px"><span>${esc(t("reopen_comment"))}</span><textarea id="g-reopen" rows="2"></textarea></label>
      <div class="modal-actions start"><button class="btn" id="g-reopen-btn" type="button">${esc(t("reopen"))}</button></div>` : ""}
    <div class="form-grid" style="margin-top:10px">
      <label class="field full"><span>${esc(t("goal_title"))} * <span class="hint">${esc(t("goal_split_hint"))}</span></span><textarea id="g-title" rows="3"></textarea></label>
      ${mdFieldHtml("g-desc", t("goal_desc"), goal ? goal.description : "", 6)}
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

/** The catalog role a typed name means, as the hire resolves it (roles.find_role; case-insensitive on its name, its
 * key or another of its `names`, e.g. a built-in role's name in the other language), or null: a new role. */
function findRole(catalog, name) {
  const norm = (s) => String(s || "").trim().replace(/\s+/g, " ").toLowerCase();
  const q = norm(name);
  if (!q) return null;
  return (catalog || []).find((r) => [r.name, r.key, ...(r.names || [])].some((s) => norm(s) === q)) || null;
}
/** The line under the role input: the catalog description of the role (and its model profile), or role_new. */
function rolePreviewHtml(catalog, name) {
  if (!String(name || "").trim()) return "";
  const role = findRole(catalog, name);
  if (!role) return esc(t("role_new"));
  return md(role.description) + (role.model ? `<span class="badge plain">${esc(t("role_model", { model: role.model }))}</span>` : "");
}
/** hire_agent arguments of the hire form. `instructions` is always sent ("" is fine for a catalog role, but the
 * tool requires the parameter); `model` only when one is chosen, so that the role's own profile applies. */
function hireArgs(role, instructions, model, name) {
  const args = { role: String(role || "").trim(), instructions: String(instructions || "").trim() };
  if (model) args.model = model;
  if (String(name || "").trim()) args.name = String(name).trim();
  return args;
}
async function hireModal() {
  let catalog = [], known = [];   // offered roles; all roles a hire resolves (the hidden ones too)
  try {
    const data = await api("/api/roles");
    catalog = data.roles || [];
    known = [...catalog, ...(data.hidden || [])];
  } catch (e) { console.error(e); }  // no catalog: any role still works
  const models = S.state.swarm.models && S.state.swarm.models.length ? S.state.swarm.models : ["default"];
  openModal(`<div class="modal-head"><h3>${esc(t("hire_title"))}</h3><button class="icon-btn" type="button" data-close title="${esc(t("close"))}" aria-label="${esc(t("close"))}"><i class="ic ic-close"></i></button></div>
    <div class="form-grid">
      <label class="field"><span>${esc(t("role"))} *</span><input id="h-role" list="h-roles" autocomplete="off"><datalist id="h-roles">${catalog.map((r) => `<option value="${esc(r.name)}"></option>`).join("")}</datalist></label>
      <label class="field"><span>${esc(t("model"))}</span><select id="h-model"><option value="">${esc(t("model_auto"))}</option>${models.map((m) => `<option value="${esc(m)}">${esc(m)}</option>`).join("")}</select></label>
      <div class="full muted role-preview" id="h-role-desc"></div>
      <label class="field full"><span>${esc(t("instructions"))} <span class="hint">${esc(t("instructions_hint"))}</span></span><textarea id="h-instr" rows="4"></textarea></label>
      <label class="field"><span>${esc(t("name_optional"))}</span><input id="h-name"></label>
    </div>
    <div class="modal-actions"><button class="btn" type="button" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="h-save" type="button">${esc(t("hire_btn"))}</button></div>`);
  $("#h-role").addEventListener("input", () => setHtml($("#h-role-desc"), rolePreviewHtml(known, $("#h-role").value)));
  $("#h-save").addEventListener("click", async () => {
    const role = $("#h-role").value.trim();
    if (!role) { toast(t("required"), "err"); $("#h-role").focus(); return; }
    const res = await act("hire_agent", hireArgs(role, $("#h-instr").value, $("#h-model").value, $("#h-name").value));
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

// Per-session settings of the dialog (overrides of a launcher session, PATCH /api/sessions/{sid} "settings"; the
// budget and max agents live in the session's database instead). kind: "int" a whole number >= min, "num" a number
// >= min, "auto" ([shell] cpu_threads) "auto" or a whole number >= 0; the minimums are config.py's.
const SESSION_FIELDS = [
  { group: "jobs", section: "shell", key: "max_jobs_per_member", label: "sf_jobs", kind: "int", min: 0 },
  { group: "jobs", section: "shell", key: "max_jobs", label: "sf_jobs_total", kind: "int", min: 0 },
  { group: "jobs", section: "shell", key: "job_max_minutes", label: "sf_job_minutes", kind: "int", min: 0 },
  { group: "jobs", section: "shell", key: "cpu_threads", label: "sf_cpu", kind: "auto", min: 0 },
  { group: "agents", section: "agents", key: "max_steps_per_turn", label: "sf_steps", kind: "int", min: 1 },
  { group: "agents", section: "agents", key: "context_tokens", label: "sf_context", kind: "int", min: 1 },
  { group: "agents", section: "agents", key: "tool_output_chars", label: "sf_tool_out", kind: "int", min: 1 },
  { group: "agents", section: "agents", key: "idle_nudge_seconds", label: "sf_nudge", kind: "num", min: 0 },
  { group: "agents", section: "agents", key: "max_parallel_llm_calls", label: "sf_parallel", kind: "int", min: 1 },
];
function sessionFieldId(f) { return `sf-${f.section}-${f.key}`; }
/** The session's own value of a field; undefined when it inherits the global one. */
function sessionOverride(overrides, f) {
  const section = overrides && overrides[f.section];
  return section && typeof section === "object" && section[f.key] !== null && section[f.key] !== "" ? section[f.key] : undefined;
}
/** What a field's text means: {value} (null for an empty field: the global value) or {error}. */
function parseSessionField(f, text) {
  const raw = String(text ?? "").trim();
  if (raw === "") return { value: null };
  if (f.kind === "auto" && raw.toLowerCase() === "auto") return { value: "auto" };
  const ok = f.kind === "num" ? /^\d+([.,]\d+)?$/.test(raw) : /^\d+$/.test(raw);
  const value = Number(raw.replace(",", "."));
  if (ok && Number.isFinite(value) && value <= Number.MAX_SAFE_INTEGER && value >= f.min) return { value };
  const key = f.kind === "auto" ? "sf_bad_auto" : f.kind === "num" ? "sf_bad_num" : "sf_bad_int";
  return { error: t(key, { field: t(f.label), min: f.min }) };
}
/** {patch, errors} of the dialog's texts ({"section.key": text}): the "settings" of PATCH /api/sessions/{sid} with
 * only the keys that differ from the session's overrides (an emptied field is null: the global value again), and
 * [{field, text}] for the invalid ones. */
function sessionSettingsPatch(texts, overrides, fields = SESSION_FIELDS) {
  const patch = {}, errors = [];
  for (const f of fields) {
    const parsed = parseSessionField(f, texts[`${f.section}.${f.key}`]);
    if (parsed.error) { errors.push({ field: f, text: parsed.error }); continue; }
    const own = sessionOverride(overrides, f);
    // a value stored as text ("4" in a hand-edited file) is the same as the number
    const same = parsed.value === null ? own === undefined : own !== undefined && String(own).trim().toLowerCase() === String(parsed.value);
    if (!same) (patch[f.section] = patch[f.section] || {})[f.key] = parsed.value;
  }
  return { patch, errors };
}
/** The hint of the context field: a model profile's own window wins over this one (and the session's profile has one). */
function contextHint(config) {
  const profile = config && config.agents ? config.agents.default_model : null;
  const own = profile && config.models && config.models[profile] ? config.models[profile].context_tokens : null;
  return t("sf_context_hint") + (own ? "; " + t("sf_context_profile", { profile, n: own }) : "");
}
/** The per-session groups of the dialog: each input holds the session's own value, its placeholder the global one. */
function sessionFieldsHtml(overrides, defaults, config, fields = SESSION_FIELDS) {
  const field = (f) => {
    const own = sessionOverride(overrides, f);
    const inherited = defaults && defaults[f.section] ? defaults[f.section][f.key] : undefined;
    const hint = f.key === "context_tokens" ? ` <span class="hint">${esc(contextHint(config))}</span>` : "";
    const mode = f.kind === "int" ? "numeric" : f.kind === "num" ? "decimal" : "text";
    return `<label class="field"><span>${esc(t(f.label))}${hint}</span><input id="${sessionFieldId(f)}" inputmode="${mode}" autocomplete="off" value="${esc(own ?? "")}" placeholder="${esc(inherited ?? "")}" data-empty-edit></label>`;
  };
  const group = (name, title) => `<div class="section-title">${esc(title)}</div>
    <div class="form-grid">${fields.filter((f) => f.group === name).map(field).join("")}</div>`;
  return group("jobs", t("sg_jobs")) + group("agents", t("agents"))
    + `<p class="muted" style="font-size:12px;margin:10px 0 0">${esc(t("sf_hint"))}</p>`;
}

async function settingsModal() {
  const lim = S.state.swarm.limits;
  const cost = Number(lim.max_cost_usd) || 0, maxAgents = Number(lim.max_agents) || 1;
  // a launcher session also gets its own background-job limits and agents' parameters; the main session (the
  // global settings themselves) and a standalone swarm app keep them in the general settings
  const sid = sessionSid();
  const perSession = Boolean(sid && sid !== "main");
  let session = null;
  if (perSession) {
    // ⚙ stays disabled while loading: the click shows something is happening, and a second one does not open it twice
    const btn = $("#settings-btn");
    if (btn) btn.disabled = true;
    try {
      const [detail, global] = await Promise.all([launcherApi(`/api/sessions/${encodeURIComponent(sid)}`), launcherApi("/api/settings")]);
      session = { overrides: detail.session.overrides || {}, config: detail.config, defaults: global.settings };
    } catch (e) { toast(t("sf_load_failed", { error: errText(e) }), "err"); }
    finally { if (btn) btn.disabled = false; }
    // a modal opened while loading (a task with a half-typed comment, say) is not replaced behind the user's back
    if (modalOpen()) return;
  }
  const muted = (key) => `<p class="muted" style="font-size:12px;margin:10px 0 0">${esc(t(key))}</p>`;
  openModal(`<div class="modal-head"><h3>${esc(t("settings"))}</h3><button class="icon-btn" type="button" data-close title="${esc(t("close"))}" aria-label="${esc(t("close"))}"><i class="ic ic-close"></i></button></div>
    <div class="form-grid">
      <label class="field"><span>${esc(t("max_cost"))}</span><input id="s-cost" type="number" min="0" step="0.5" value="${cost}"></label>
      <label class="field"><span>${esc(t("max_agents"))}</span><input id="s-agents" type="number" min="1" step="1" value="${maxAgents}"></label>
    </div>
    ${session ? sessionFieldsHtml(session.overrides, session.defaults, session.config)
      : muted(perSession ? "sf_not_loaded" : "sf_general_only")}
    <div class="modal-actions"><button class="btn" type="button" data-close>${esc(t("cancel"))}</button><button class="btn primary" id="s-save" type="button">${esc(t("save"))}</button></div>`);
  $("#s-save").addEventListener("click", async () => {
    const args = limitsArgs($("#s-cost").value, $("#s-agents").value);
    if (!args) { toast(t("invalid_limits"), "err"); return; }
    let patch = {};
    if (session) {
      const texts = Object.fromEntries(SESSION_FIELDS.map((f) => [`${f.section}.${f.key}`, $("#" + sessionFieldId(f)).value]));
      const result = sessionSettingsPatch(texts, session.overrides);
      if (result.errors.length) { toast(result.errors[0].text, "err"); $("#" + sessionFieldId(result.errors[0].field)).focus(); return; }
      patch = result.patch;
    }
    // first the session settings (a stopped session takes them too); swarm_control only when the limits changed
    let saved = false;
    if (Object.keys(patch).length) {
      try {
        const data = await launcherApi(`/api/sessions/${encodeURIComponent(sid)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ settings: patch }) });
        session.overrides = data.session.overrides || {};  // a second save (after a failed limits change) does not resend them
        // stored now: closing the dialog after a failed limits change does not offer to discard them
        if (S.modalInitial) for (const f of SESSION_FIELDS) S.modalInitial.set(sessionFieldId(f), $("#" + sessionFieldId(f)).value);
        saved = true;
      } catch (e) { toast(errText(e), "err"); return; }
    }
    if (args.max_cost_usd !== cost || args.max_agents !== maxAgents) {
      const res = await act("swarm_control", args);
      // the dialog stays open on the limits' error; say that the session settings did get through
      if (!res.ok) { if (saved) toast(t("sf_saved_part")); return; }
    } else if (saved) toast(t("saved"));
    closeModal(true);
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
    <div class="desc">${md(s.description || "")}</div>
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
    <div class="item-head"><b>#${Number(x.id)}</b><span class="cond">${esc(x.condition)}</span>${x.once ? "" : `<span class="badge" title="${esc(t("periodic"))}"><i class="ic ic-repeat"></i></span>`}
      <span class="muted">${esc(t("owner"))}: ${esc(displayName(x.owner))} · ${esc(t("fired"))}: ${Number(x.fire_count) || 0}</span>
      <span class="spacer"></span><button class="btn small danger" data-trigger-del="${Number(x.id)}" type="button">${esc(t("delete"))}</button></div>
    ${x.note ? `<div class="desc">${md(x.note)}</div>` : ""}
    ${x.actions && JSON.stringify(x.actions) !== '[{"type":"wake"}]' ? `<div class="desc"><code>${esc(JSON.stringify(x.actions))}</code></div>` : ""}
    ${x.last_error ? `<div class="desc err-text">${esc(x.last_error)}</div>` : ""}
  </div>`).join("") : `<div class="empty">${esc(t("no_triggers"))}</div>`);
}

/* ---------------------------------------------------------------- background jobs */
function nowSeconds() { return Date.now() / 1000; }
/** "45 с", "12 мин", "2 ч 05 мин": a duration as the hub words it for the agents (jobs.fmt_duration). */
function fmtDur(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  if (total < 60) return t("dur_s", { n: total });
  const minutes = Math.floor(total / 60);
  if (minutes < 60) return t("dur_min", { n: minutes });
  return t("dur_h", { h: Math.floor(minutes / 60), m: String(minutes % 60).padStart(2, "0") });
}
/** The agents' running background jobs (all, or `owner`'s) from the state. */
function runningJobs(owner) {
  return (S.state?.jobs || []).filter((j) => j.status === "running" && (owner === undefined || j.owner === owner));
}
function jobLabel(job) { return job && job.id !== undefined && job.id !== null ? `j${Number(job.id)}` : "?"; }
/** "node.exe ×2, python.exe" */
function processNames(names) {
  const counts = new Map();
  for (const n of Array.isArray(names) ? names : []) if (n) counts.set(String(n), (counts.get(String(n)) || 0) + 1);
  return Array.from(counts, ([n, c]) => (c > 1 ? `${n} ×${c}` : n)).join(", ");
}
/** [badge class, text] of a job's state: running (for how long), how it ended, stopped (why) or lost. */
function jobState(job) {
  if (job.status === "running") return ["", t("job_running", { t: fmtDur(nowSeconds() - job.started_at) })];
  if (job.status === "lost") return ["err", t("job_lost")];
  if (job.status === "stopped") {
    return [job.reason === "lifetime" ? "warn" : "plain", I18N.en["job_stopped_" + job.reason] ? t("job_stopped_" + job.reason) : t("job_stopped")];
  }
  if (job.exit_code !== null && job.exit_code !== undefined) return [Number(job.exit_code) === 0 ? "done" : "err", t("job_ended_code", { code: job.exit_code })];
  return ["done", t("job_ended")];
}
/** A log the Files tab can open: workspace-relative (the data_dir fallback is an absolute path outside it). */
function workspaceLog(log) { return typeof log === "string" && log !== "" && !/^(?:[A-Za-z]:)?[\\/]/.test(log); }

function jobCardHtml(job) {
  const id = Number(job.id);
  const running = job.status === "running";
  const [cls, state] = jobState(job);
  const open = S.jobOpen.has(id);
  const meta = [];
  const names = running ? processNames(job.processes) : "";
  if (names) meta.push(esc(names));
  if (!running && job.ended_at) meta.push(`${esc(t("job_took", { t: fmtDur(job.ended_at - job.started_at) }))} · ${esc(fmtTime(job.ended_at))}`);
  if (job.log) meta.push(workspaceLog(job.log) ? `<button type="button" class="link" data-job-log="${esc(job.log)}" title="${esc(t("job_log_hint"))}">${esc(job.log)}</button>` : `<code>${esc(job.log)}</code>`);
  const output = S.jobOutputs[id];
  // silent for more than jobs.SILENT_MINUTES (the hub says since when; a job.silent event refreshes the state)
  const silentSince = running ? Number(job.silent_since) || 0 : 0;
  const silent = silentSince ? `<span class="badge warn" title="${esc(t("job_silent_hint"))}"><i class="ic ic-clock"></i>${esc(t("job_silent", { t: fmtDur(nowSeconds() - silentSince) }))}</span>` : "";
  return `<div class="item job${running ? "" : " dim"}" data-job="${id}">
    <div class="item-head"><b>${esc(jobLabel(job))}</b><span class="chip">${avatar(job.owner, "sm")}${esc(displayName(job.owner))}</span>
      <span class="badge ${cls}">${esc(state)}</span>${silent}<span class="badge plain">${esc(tv("job_origin_", job.origin))}</span>
      <span class="spacer"></span><button class="btn small" type="button" data-job-out="${id}" aria-expanded="${open}">${esc(t(open ? "job_hide" : "job_output"))}</button>
      ${running ? `<button class="btn small danger" type="button" data-job-stop="${id}">${esc(t("job_stop"))}</button>` : ""}</div>
    <div class="job-cmd"><code>${esc(job.command)}</code></div>
    ${meta.length ? `<div class="desc">${meta.join(" · ")}</div>` : ""}
    ${open ? `<pre class="job-output" data-job-pre="${id}">${esc(output === undefined ? "…" : output)}</pre>` : ""}
  </div>`;
}

/** The Jobs tab: one card per job of the state (the running ones, then those ended in the last hour). An open output
 * keeps its scroll position, and one scrolled to its end follows the new output. */
function renderJobs() {
  const box = $("#job-list");
  const items = (S.state && S.state.jobs) || [];
  const scrolls = {};
  $$("pre[data-job-pre]", box).forEach((p) => { scrolls[p.dataset.jobPre] = nearBottom(p) ? null : p.scrollTop; });
  // the cards are rebuilt while jobs run: a focused button keeps the focus (keyboard users)
  const active = document.activeElement;
  const attr = active && box.contains && box.contains(active)
    ? ["data-job-out", "data-job-stop", "data-job-log"].find((a) => active.hasAttribute(a)) : null;
  const refocus = attr ? `[${attr}="${CSS.escape(active.getAttribute(attr))}"]` : null;
  setHtml(box, items.length ? items.map(jobCardHtml).join("") : `<div class="empty">${esc(t("jobs_empty"))}</div>`);
  $$("pre[data-job-pre]", box).forEach((p) => { const s = scrolls[p.dataset.jobPre]; p.scrollTop = s === undefined || s === null ? p.scrollHeight : s; });
  if (refocus && !box.contains(document.activeElement)) box.querySelector(refocus)?.focus();
}

/** The end of a job's output (the `jobs` tool, as the user) for its open card. */
async function loadJobOutput(id) {
  if (S.jobLoading.has(id)) return;
  S.jobLoading.add(id);
  try {
    const res = await post("/api/act/jobs", { job_id: `j${id}`, tail: JOB_TAIL });
    S.jobOutputs[id] = String(res.result ?? "");   // a refusal (e.g. an unknown job) explains itself
  } catch (e) {
    S.jobOutputs[id] = errText(e);
  } finally {
    S.jobLoading.delete(id);
  }
  if (S.tab === "jobs" && S.state) renderJobs();
}
function toggleJobOutput(id) {
  if (S.jobOpen.has(id)) S.jobOpen.delete(id);
  else { S.jobOpen.add(id); loadJobOutput(id); }
  if (S.state) renderJobs();
}
/** Stop a job (and every process it started): synchronous on the hub, its result is the toast. */
async function stopJob(id) {
  if (!confirm(t("job_stop_confirm", { id: `j${Number(id)}` }))) return null;
  const res = await act("job_stop", { job_id: `j${Number(id)}` });
  if (S.jobOpen.has(Number(id))) loadJobOutput(Number(id));
  return res;
}
/** A job's log in the Files tab. */
function openJobLog(path) {
  S.openFile = path;
  S.fileDir = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : ".";
  setTab("files");
}
/** Every JOB_REFRESH ms while the Jobs tab is shown: the open outputs of running jobs and the running times. */
function tickJobs() {
  if (S.tab !== "jobs" || !S.state || (typeof document !== "undefined" && document.hidden)) return;
  for (const id of S.jobOpen) if (runningJobs().some((j) => Number(j.id) === id)) loadJobOutput(id);
  renderJobs();
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
    case "swarm.wrap_up": return d.on ? (ru ? `${who} включил(а) режим завершения: агенты завершают задачи с тем, что есть` : `${who} switched wrap-up mode on: the agents finish their tasks with what they have`)
                                      : (ru ? `${who} выключил(а) режим завершения` : `${who} switched wrap-up mode off`);
    case "swarm.limits": return ru ? `лимиты: $${d.max_cost_usd}, агентов ${d.max_agents}` : `limits: $${d.max_cost_usd}, ${d.max_agents} agents`;
    case "job.started": {
      const job = d.job || {};
      return t("ev_job_started", { who: displayName(job.owner || ev.actor), id: jobLabel(job), command: short(job.command, 120) });
    }
    case "job.ended": {
      const job = d.job || {};
      const took = job.duration ?? (Number(job.ended_at) || 0) - (Number(job.started_at) || 0);
      return t("ev_job_ended", { who: displayName(job.owner || ev.actor), id: jobLabel(job), result: jobState(job)[1], t: fmtDur(took) });
    }
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
  if (workspaceMissing(S.state && S.state.swarm && S.state.swarm.workspace)) {
    // nothing to list (#workspace-banner says why): no error toast on every refresh
    S.fileDir = "."; S.fileEntries = [];
    renderCrumbs(); renderFileList();
    return;
  }
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
    const lock = locks.has(e.path) ? ` <span class="lock" title="${esc(t("locked_by", { owner: displayName(locks.get(e.path)) }))}"><i class="ic ic-lock"></i></span>` : "";
    return `<div class="file-entry${e.path === S.openFile ? " active" : ""}" role="button" tabindex="0" data-${dir ? "dir" : "file"}="${esc(e.path)}"><span class="file-name">${dir ? '<i class="ic ic-folder"></i>' : '<i class="ic ic-file"></i>'}${esc(name)}${lock}</span><span class="size">${dir ? "" : esc(fmtSize(e.size))}</span></div>`;
  }).join("") || `<div class="empty">${esc(t("empty"))}</div>`);
}
function isMarkdownPath(path) { return /\.(md|markdown)$/i.test(String(path || "")); }
/** What the file viewer shows: a Markdown file formatted (unless the user switched to its source), any other as text. */
function fileView(path, content, source) {
  const markdown = isMarkdownPath(path);
  const formatted = markdown && !source;
  return { markdown, formatted, html: formatted ? md(content) : "" };
}
/** The text of a file (or a .md source) with syntax highlighting by its name (highlight.js: escaped, only spans; a
 * file of an unknown kind or above Highlight.MAX_CHARS stays plain). */
function codeHtml(path, text) { return HIGHLIGHT.render(text, HIGHLIGHT.language(path)); }
/** The open file's text in the viewer (S.fileText), formatted or as its source (S.fileSource, the toggle). */
function renderFileContent() {
  const view = fileView(S.openFile, S.fileText || "", S.fileSource);
  setHtml($("#file-content"), view.formatted ? "" : codeHtml(S.openFile, S.fileText || ""));
  $("#file-content").classList.toggle("hidden", view.formatted);
  const box = $("#file-md");
  setHtml(box, view.html);
  box.classList.toggle("hidden", !view.formatted);
  const mode = $("#file-mode");
  mode.classList.toggle("hidden", !view.markdown);
  mode.textContent = t(S.fileSource ? "md_formatted" : "md_source");
}
function toggleFileSource() {
  S.fileSource = !S.fileSource;
  store("dt-md-source", S.fileSource ? "1" : "0");
  renderFileContent();
}
async function openFile(path, quiet = false) {
  try {
    const data = await api(`/api/files?path=${encodeURIComponent(path)}`);
    const view = $(".file-view");
    const keep = S.openFile === path ? view.scrollTop : 0;
    S.openFile = path;
    S.fileText = data.content || "";
    $("#file-path").textContent = path + (data.truncated ? " " + t("truncated") : "");
    renderFileContent();
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
  if (ev.type === "notification" && d.recipient === "user") toast(short(d.text, 400));
  if (ev.type === "message.sent") {
    const m = d.message || {};
    const rcpt = m.recipients || [];
    const involvesOpen = S.chatWith === "all" || (S.chatWith && (m.sender === S.chatWith || rcpt.includes(S.chatWith)) && (m.sender === "user" || rcpt.includes("user")));
    if (involvesOpen) scheduleMessages(100);
    else if (rcpt.includes("user")) toast(`${m.sender}: ${short(m.text, 200)}`, "", () => openChat(m.sender));
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
  if (ev.type.startsWith("job.") && d.job && S.jobOpen.has(Number(d.job.id))) loadJobOutput(Number(d.job.id));
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
  for (const name of ["board", "feed", "files", "mcp", "triggers", "jobs", "traces"]) $("#pane-" + name).classList.toggle("hidden", name !== tab);
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
  const target = e.target.closest("[data-open-task],[data-close],[data-dir],[data-file],[data-reply],[data-goto-post],[data-mcp-approve],[data-mcp-reject],[data-trigger-del],[data-job-out],[data-job-stop],[data-job-log],[data-md-edit],[data-retire],[data-traces],[data-agent],#mcp-cmd,#ws-path");
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
  else if ("jobOut" in ds) toggleJobOutput(Number(ds.jobOut));
  else if ("jobStop" in ds) stopJob(Number(ds.jobStop));
  else if ("jobLog" in ds) openJobLog(ds.jobLog);
  else if ("mdEdit" in ds) editMdField(ds.mdEdit);
  else if ("retire" in ds) { if (confirm(t("retire_confirm"))) act("retire_agent", { name: ds.retire, reason: "retired by the user" }); }
  else if ("traces" in ds) { if (typeof openTraces === "function") { if (isNarrow()) closeDrawer(); openTraces(ds.traces); } }
  else if ("agent" in ds) openChat(ds.agent);
  else if (target.id === "mcp-cmd" || target.id === "ws-path") navigator.clipboard?.writeText(target.textContent).then(() => toast(t("copied")));
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
  S.fileSource = store("dt-md-source") === "1";
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
  $("#wrap-btn").addEventListener("click", toggleWrapUp);
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
  $("#file-mode").addEventListener("click", toggleFileSource);
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
  setInterval(tickJobs, JOB_REFRESH);
}

if (typeof module !== "undefined" && module.exports) {
  // pure helpers, exported for the Node-based UI tests (tests/test_ui.py); the browser ignores this
  module.exports = {
    I18N, S, COLUMNS, ALL_STATUSES, TASK_FORM, esc, t, tv, linkify, highlightMentions, memberOptions, statusOptions,
    dropAction, makeScheduler, threadOrder, threadHtml, dirtyFields, taskChanges, mentionQuery, mentionCandidates,
    limitsArgs, describeEvent, changesText, activityHtml, messageHtml, cardHtml, boardHtml, boardColumns, rejectMcp,
    deleteTrigger, fillTaskForm, autoMarkRead, loadFeed, FEED_TYPES,
    // review, the hire dialog (role catalog), the goal dialog
    reviewerNone, taskReviewHtml, reviewTask, moveTask, findRole, rolePreviewHtml, hireArgs, hireModal, goalModal,
    // used by traces.js (the tests load it on top of these) and by the session-chrome tests
    BASE, $, $$, api, post, act, toast, avatar, agentColor, members, fmtTime, short, openModal, closeModal, setTab, store, setHtml,
    renderHeader, toggleWrapUp, responseError, errText, startSession, connect,
    // the session folder (the Files tab head, the missing-folder banner)
    workspaceKind, workspaceMissing, workspaceHtml, syncWorkspace, openDir, renderAll,
    // background jobs: the Jobs tab, the agent marker and the drawer, live events
    fmtDur, jobState, jobCardHtml, renderJobs, loadJobOutput, toggleJobOutput, stopJob, openJobLog, tickJobs,
    processNames, runningJobs, renderAgents, renderDrawerHead, handleEvent, onDocumentClick,
    // the settings dialog: the session's own background-job limits and agents' parameters
    SESSION_FIELDS, sessionSid, launcherApi, sessionFieldId, sessionOverride, parseSessionField, sessionSettingsPatch, contextHint,
    sessionFieldsHtml, settingsModal,
    // Markdown (markdown.js) where agents', members' and the user's texts are shown
    md, decorate, MD_CACHE, mdFieldHtml, syncMdField, editMdField, taskResultHtml, renderTaskModal, updateTaskModal,
    renderMcp, renderTriggers, fileView, renderFileContent, toggleFileSource, openFile, isMarkdownPath, codeHtml,
  };
}
if (typeof document !== "undefined") document.addEventListener("DOMContentLoaded", init);
