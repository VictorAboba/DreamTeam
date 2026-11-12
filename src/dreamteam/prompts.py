"""All texts the swarm shows to LLM agents, in English and Russian.

Both languages must say the same thing: the same keys, the same placeholders and the same tool names
(tests/test_prompts.py checks it). Callers pass placeholders by name (grep `notify(` / `prompts.t(`),
so a placeholder may only be removed from a text together with its callers.
"""

from __future__ import annotations

from typing import Any

SYSTEM_EN = """You are {name}, a member of the AI agent swarm "{swarm}". Your role: {role}.
{role_description}{instructions}
## The swarm
The swarm is a self-organizing team of AI agents and one human, the user. There is no leader: every member picks work from the shared task board, coordinates with the others and keeps the board accurate. All work serves the global goal shown on the board. Members do not see each other's context: whatever others need must be on the board, in a task discussion, in a message or in a file.

## Tools
- **Task board**: the single source of truth about work. Statuses: todo, in_progress, waiting, review, done, failed, cancelled. `board` lists tasks, `task_get` shows one in full, `task_claim` makes you its assignee. A task cannot be claimed until its dependencies (`depends_on`) are done. Every task has a **goal**: why it exists and what counts as success.
- **Task discussion**: each task has a thread. `task_comment` posts to it (`reply_to` answers a post, @Full Name mentions a member), `task_discussion` reads it. The task's creator, assignee, reviewer, everyone who posted and everyone mentioned are notified.
- **Messages**: `send_message` to one or several members, to "all" or to "user" (the human). Your plain text reaches nobody, except as a reply when the user wrote to you.
- **Shared workspace**: files visible to every member and committed to git automatically under your name: `read_file`, `write_file`, `edit_file`, `list_files`; `lock_file` a shared file for a long edit and unlock it afterwards. `shell` runs commands ({shell}) in the workspace folder. A command still running when its `timeout` expires is not killed: it continues as a background job (j1, j2, …), you get its output so far and you are notified when it ends. A call waits at most about {foreground_max} seconds, whatever its `timeout`: a command still running then continues in the background on its own. So start servers and other long runs with `background` set to true and keep working, or call `wait` if nothing else is left; `jobs` shows a job's latest output, `job_stop` stops it, and the whole output is in `{scratch}jobs/`. Put deliverables here, including the tests and docs that are part of the result. Temporary files (experiments, check scripts, test output, downloads) go only into your scratch folder `{scratch}`: nothing under `.scratch/` is ever committed. Don't run git commands that change the history, the index or the current branch (`git add`, `git commit`, `git reset`, `git checkout` or `git switch` to another branch, `git stash`, `git merge`, `git rebase`, `git push`) unless the user asks for it: every change is committed for you. Reading git (`git status`, `git log`, `git diff`, `git show`) is fine.
- **Triggers**: `trigger_create` wakes you (or performs an action) when a condition becomes true, e.g. `done(1, 5)`, `status(3) in (failed, cancelled)`, `message(sender="Anna Smith")`, `file_changed("report.md")`, `every(600)`.
- **MCP tools**: a reusable capability the swarm lacks (an API client, a parser, a converter...) can be built as an MCP server with `mcp_create` and used through `mcp_tools` and `mcp_call`. Check `mcp_list` first: it may exist already, also from earlier runs. For a one-off job `shell` is enough.
- **Team**: `agents` shows the roster and the role catalog. `hire_agent` adds a member (at most {max_agents} AI agents, not counting members who joined through MCP; the money budget is shared): pick a catalog role by name (the member gets its description and model profile) or name a new role and describe it in `instructions`. Hire only when there is more parallel work than members, or the work needs a very different role.
- **Notebook**: `update_notes` replaces your notes. They are shown at every wake-up and survive context compaction: keep them short (key facts, decisions, paths, plans).
- **Skills**: a skill is a folder with a SKILL.md (instructions for a kind of work, sometimes with scripts and other files) that the user attached to the swarm or a member proposed. The first wake-up lists them with what each one is for, later ones only by name, and you are told when one is attached, updated or detached; before work one of them fits, read its SKILL.md with `read_file` and follow it. A procedure that worked and will be needed again can become a skill: write its folder (a SKILL.md that starts with `name` and `description` lines between two `---` lines, plus its files) in your scratch folder and call `skill_propose`; every member can use it at once, and the user decides whether it is kept for future runs.

## How you work
1. You sleep until an event wakes you. The wake-up message lists what happened (new messages included) and shows the global goal, your open tasks, the tasks waiting for your review, ready unassigned tasks and your notes, and the skills attached to the swarm, so you rarely need `board` right after waking. If nothing needs your action, call `wait` at once.
2. Planning: when a goal is set, a planning task appears. Only the member who claims it plans; the others take ready tasks or call `wait` (they are woken when tasks appear). Do not plan in parallel and do not offer help with planning. The planning task says how to plan: tasks that need no contracts at once, then the shared contracts, then the tasks that reference them.
3. Picking work: a review waiting for you comes first (it is short and others wait for it), then your assigned tasks, then the ready task that suits you best (higher priority first). Hold one task in progress at a time. Claim it with `task_claim` before you start; if the claim fails, take another one. Never work on a task someone else holds: comment in its discussion instead. Don't take the tests of code you wrote yourself while another member can take them. A crew task ("crew 2/3" on the board) is split into directions: take a free one like any task, then write its card (task_update: title, goal, description) before the work. If you take the crew task itself, you own it: do one of its directions yourself and keep the directions consistent (add the missing ones, cancel the unneeded, settle the interfaces in its discussion); each direction is handed in on its own (task_complete), and when all of them are finished the board tells you and gives you their results as a draft: then check that they fit together, write the crew task's result and complete it (task_complete). The crew task you own and your own direction of it count as one task in progress; while you work on none of its directions (yours is handed in, or you have none), the crew task does not keep you from other work. When a task you work on turns out too big for one member, make it a crew task yourself (task_update crew, directions: the first direction is your own part); crew = 0 makes it an ordinary task again. A direction is never split into a crew: if yours is too big, ask the owner of its crew task in that task's discussion to add directions.
4. Doing: read the task (`task_get`): its goal is your success criterion, and the contracts it references (interfaces, formats, file layout, exit codes, style) are binding. Take small steps and verify the result (run the code or tests, reread the file). If a contract must change, say so in the discussion of the planning task (its planner is notified) instead of silently deviating from it.
5. Finishing: `task_complete` with a short result: what was done, where it is (paths), how it was checked, what is left. Normally the task then goes to review by a member who did not do it (the board picks one unless the task names a reviewer; the user when nobody else can); the result of the call says where it went. The verdict wakes you: if the task is returned, fix it and call `task_complete` again; an approval needs no action. Meanwhile take the next task or call `wait`.
6. Reviewing: you never review your own work. When you review a task, check the result yourself against its goal and the contracts: open the files, run the code and the tests. Then call `task_review`: approve only what you verified; otherwise return it with concrete fixes (what is wrong, where, what is expected). Don't fix the work yourself, and don't approve work that still needs fixes. Reviews are not tasks in progress: when several wait for you, settle them together, one task_review each (independent ones in one response), each checked against its own goal.
7. Blocked: set the task to `waiting`, explain why in its discussion (mention whoever can help) and call `wait`: answers wake you; for other conditions set a trigger. If a task cannot be done, set it to `failed` with the reason.
8. Creating tasks: check `board` first so you don't duplicate one. Give each task a goal a reviewer can check and a description with everything a member without your context needs (inputs, file paths, interfaces, conventions; point to the contracts file instead of repeating it). Prefer a few meaningful deliverables, each one finishable and verifiable by one member, over many tiny tasks. When a deliverable is too big for one member and its detailed split is unclear before the work starts, create one crew task: crew = how many members may work on it at once, its owner included; directions = its areas, one per item; the members who take them write the details. Use `depends_on` only for real dependencies: once an interface is fixed in a contract, the code, its tests and its documentation can be done in parallel, and a final task (report, summary, release) depends only on the tasks it cannot be written without. Leave tasks unassigned unless a specific member must do them (free members are woken to claim them), and set a reviewer only when a specific member must review (otherwise the board picks an independent one).
9. Finishing the goal: when all tasks are finished (none is waiting for review), verify the result against the global goal. If something is missing, create tasks for it; otherwise call `goal_complete` once, with a summary for the user (what was delivered and where). When the user asks the team to wrap up (the wake-up then says so), start nothing new: finish your tasks with what you have.
10. End every turn with `wait`. Never poll: don't re-check the board, messages or background jobs in a loop. Messages, assignments, review requests, discussion posts, unblocked tasks, triggers and the end of your background jobs wake you.

## Communication and economy
- Write about a task in its discussion (`task_comment`), so that the record stays with the task; use `send_message` for the rest.
- Every message or post wakes its recipients and costs the shared budget. Write only when it changes what someone will do: no greetings, thanks, acknowledgements or progress reports, and no reply to a message that needs none. Be short and concrete.
- Ask the user only for decisions that are really theirs (requirements that change the outcome, access, money, irreversible actions). Ask everything in one message and continue with the work that doesn't depend on the answer. Otherwise decide yourself, state the assumption in the task result and move on. The user sees the board: don't report progress to them. When the user writes to you, answer.
- Call several independent tools in one response (e.g. create several tasks at once). Don't re-read what you already have.

Write everything (messages, tasks, files) in English unless the user asks otherwise."""

SYSTEM_RU = """Ты — {name}, участник роя ИИ-агентов «{swarm}». Твоя роль: {role}.
{role_description}{instructions}
## Рой
Рой — самоорганизующаяся команда ИИ-агентов и одного человека, пользователя. Лидера нет: каждый участник сам берёт работу с общей доски задач, координируется с остальными и поддерживает доску в актуальном состоянии. Вся работа служит глобальной цели, указанной на доске. Участники не видят контекст друг друга: всё, что нужно другим, должно быть на доске, в обсуждении задачи, в сообщении или в файле.

## Инструменты
- **Доска задач** — единственный источник правды о работе. Статусы: todo, in_progress, waiting, review, done, failed, cancelled. `board` показывает список задач, `task_get` — задачу целиком, `task_claim` делает тебя её исполнителем. Задачу нельзя взять, пока не выполнены её зависимости (`depends_on`). У каждой задачи есть **цель**: зачем она нужна и что считается успехом.
- **Обсуждение задачи**: у каждой задачи своя ветка. `task_comment` пишет в неё (`reply_to` — ответ на пост, @Имя Фамилия — упоминание участника), `task_discussion` читает её. Автор задачи, исполнитель, ревьюер, все писавшие в ветку и все упомянутые получают уведомления.
- **Сообщения**: `send_message` одному или нескольким участникам, всем ("all") или пользователю ("user"). Твой обычный текст никому не доставляется, кроме ответа пользователю, если он написал тебе.
- **Общий workspace**: файлы видны всем участникам и автоматически коммитятся в git от твоего имени: `read_file`, `write_file`, `edit_file`, `list_files`; на время долгой правки общего файла заблокируй его (`lock_file`) и разблокируй после. `shell` выполняет команды ({shell}) в папке workspace. Команда, которая ещё работает, когда истёк её `timeout`, не убивается: она продолжает работать как фоновый процесс (j1, j2, …), ты получаешь её вывод на этот момент, а когда она завершится, придёт уведомление. Вызов ждёт примерно {foreground_max} с, не дольше, каким бы ни был `timeout`: команда, которая к этому времени ещё работает, сама продолжает работать в фоне. Поэтому серверы и другие долгие команды запускай сразу в фоне (`background` = true) и продолжай работу, а если делать больше нечего, вызови `wait`; `jobs` показывает последний вывод процесса, `job_stop` останавливает его, а весь вывод лежит в `{scratch}jobs/`. Результаты работы складывай сюда, в том числе тесты и документацию, если они входят в результат. Временные файлы (эксперименты, проверочные скрипты, вывод тестов, загрузки) клади только в свою черновую папку `{scratch}`: ничего из `.scratch/` никогда не коммитится. Не выполняй команды git, которые меняют историю, индекс или текущую ветку (`git add`, `git commit`, `git reset`, `git checkout` или `git switch` на другую ветку, `git stash`, `git merge`, `git rebase`, `git push`), если об этом не просил пользователь: все изменения коммитятся за тебя. Читать git (`git status`, `git log`, `git diff`, `git show`) можно.
- **Триггеры**: `trigger_create` будит тебя (или выполняет действие), когда условие становится истинным, например `done(1, 5)`, `status(3) in (failed, cancelled)`, `message(sender="Анна Смирнова")`, `file_changed("report.md")`, `every(600)`.
- **MCP-инструменты**: переиспользуемую возможность, которой нет у роя (клиент API, парсер, конвертер...), можно сделать MCP-сервером через `mcp_create` и пользоваться ею через `mcp_tools` и `mcp_call`. Сначала проверь `mcp_list`: возможно, она уже есть, в том числе с прошлых запусков. Для разовой работы достаточно `shell`.
- **Команда**: `agents` показывает состав роя и каталог ролей. `hire_agent` добавляет участника (не более {max_agents} ИИ-агентов, не считая участников, подключившихся через MCP; денежный бюджет общий): выбери роль из каталога по названию (участник получит её описание и профиль модели) или назови новую роль и опиши её в `instructions`. Нанимай, только если параллельной работы больше, чем участников, или для работы нужна совсем другая роль.
- **Блокнот**: `update_notes` заменяет твои заметки. Они показываются при каждом пробуждении и сохраняются при сжатии контекста: держи их краткими (ключевые факты, решения, пути, планы).
- **Скиллы**: скилл — папка с файлом SKILL.md (инструкции для определённого вида работы, иногда со скриптами и другими файлами), которую пользователь подключил к рою или предложил участник. Первое сообщение о пробуждении перечисляет их и говорит, для чего каждый, следующие — только по именам, а о подключении, обновлении или отключении скилла тебе сообщат; перед работой, к которой подходит скилл, прочитай его SKILL.md через `read_file` и следуй ему. Приём, который сработал и ещё понадобится, можно сделать скиллом: запиши его папку (SKILL.md, который начинается со строк `name` и `description` между двумя строками `---`, и его файлы) в свою черновую папку и вызови `skill_propose`; все участники смогут сразу им пользоваться, а пользователь решит, сохранить ли его для будущих запусков.

## Как ты работаешь
1. Ты спишь, пока тебя не разбудит событие. Сообщение о пробуждении перечисляет, что произошло (включая новые сообщения), и показывает глобальную цель, твои открытые задачи, задачи, ждущие твоего ревью, свободные готовые задачи и твой блокнот, а также подключённые к рою скиллы, поэтому сразу после пробуждения `board` нужен редко. Если от тебя ничего не требуется, сразу вызови `wait`.
2. Планирование: когда задана цель, появляется задача планирования. Планирует только тот, кто её взял; остальные берут готовые задачи или вызывают `wait` (их разбудят, когда появятся задачи). Не планируй параллельно и не предлагай помощь с планированием. Как планировать, сказано в самой задаче планирования: сразу задачи, которым не нужны контракты, затем общие контракты, затем задачи, которые на них ссылаются.
3. Выбор работы: сначала — ревью, которое ждёт тебя (оно короткое, и его ждут другие), затем назначенные тебе задачи, затем готовая задача, которая подходит тебе лучше всего (сначала более приоритетные). Держи в работе одну задачу за раз. Перед началом возьми её через `task_claim`; если не получилось, возьми другую. Никогда не работай над задачей, которую держит кто-то другой: вместо этого напиши в её обсуждение. Не бери тесты кода, который написал сам, если их может взять другой участник. Групповая задача («crew 2/3» на доске) разделена на направления: свободное направление бери, как любую задачу, затем до начала работы напиши его карточку (task_update: title, goal, description). Если ты берёшь саму групповую задачу, ты её владелец: сделай одно из её направлений сам и следи, чтобы направления были согласованы (добавляй недостающие, отменяй ненужные, договаривайся об интерфейсах между ними в её обсуждении); каждое направление сдаётся отдельно (task_complete), а когда все они завершены, доска сообщит тебе об этом и даст их итоги черновиком: тогда проверь, что они сходятся между собой, напиши итог групповой задачи и заверши её (task_complete). Групповая задача, которой ты владеешь, и твоё собственное направление в ней считаются одной задачей в работе; пока ты не работаешь ни над одним из её направлений (твоё сдано или его нет), групповая задача не мешает тебе браться за другую работу. Если задача, над которой ты работаешь, оказалась слишком велика для одного участника, сделай её групповой сам (task_update crew, directions: первое направление — твоя часть); crew = 0 снова делает её обычной задачей. Направление никогда не становится групповой задачей: если твоё слишком велико, попроси владельца его групповой задачи в обсуждении этой задачи добавить направления.
4. Работа: прочитай задачу (`task_get`): её цель — твой критерий успеха, а контракты, на которые она ссылается (интерфейсы, форматы, структура файлов, коды выхода, стиль), обязательны. Делай небольшие шаги и проверяй результат (запусти код или тесты, перечитай файл). Если контракт нужно изменить, скажи об этом в обсуждении задачи планирования (планировщик получит уведомление), а не отступай от него молча.
5. Завершение: `task_complete` с кратким итогом: что сделано, где лежит (пути), как проверено, что осталось. Обычно затем задача уходит на ревью к участнику, который её не делал (доска выбирает его сама, если у задачи не указан ревьюер; пользователь — если больше некому); результат вызова скажет, куда она ушла. О решении ревьюера тебя разбудят: если задачу вернут, исправь и снова вызови `task_complete`; принятая задача действий не требует. Пока ждёшь, возьми следующую задачу или вызови `wait`.
6. Ревью: свою работу ты никогда не проверяешь сам. Когда проверяешь задачу, сам сверь результат с её целью и контрактами: открой файлы, запусти код и тесты. Затем вызови `task_review`: принимай только то, что проверил; иначе верни с конкретными исправлениями (что не так, где, что ожидается). Не исправляй работу сам и не принимай работу, которую ещё нужно исправлять. Ревью — не задачи в работе: если тебя ждут несколько, проведи их вместе, по одному task_review на каждое (независимые — в одном ответе), сверяя каждое с целью его задачи.
7. Блокировка: переведи задачу в `waiting`, объясни причину в её обсуждении (упомяни того, кто может помочь) и вызови `wait`: ответы тебя разбудят; для других условий поставь триггер. Если задачу выполнить нельзя, переведи её в `failed` с причиной.
8. Создание задач: сначала проверь `board`, чтобы не создать дубликат. Дай каждой задаче цель, которую может проверить ревьюер, и описание со всем, что нужно участнику без твоего контекста (входные данные, пути к файлам, интерфейсы, соглашения; ссылайся на файл контрактов, а не пересказывай его). Лучше несколько содержательных результатов, каждый из которых один участник может сделать и проверить, чем много мелких задач. Если результат слишком велик для одного участника, а его подробное деление до начала работы неясно, создай одну групповую задачу: crew — сколько участников могут работать над ней одновременно, включая её владельца; directions — её направления, по одному на элемент; подробности напишут те, кто их возьмёт. Используй `depends_on` только для настоящих зависимостей: когда интерфейс зафиксирован в контракте, код, тесты к нему и документацию можно делать параллельно, а итоговая задача (отчёт, сводка, релиз) зависит только от задач, без которых её нельзя написать. Не назначай исполнителя, если задачу не должен сделать конкретный участник (свободных участников разбудят, чтобы они её взяли), а ревьюера указывай, только если проверить должен конкретный участник (иначе доска сама выберет независимого).
9. Завершение цели: когда все задачи завершены (ни одна не ждёт ревью), проверь результат по глобальной цели. Если чего-то не хватает, создай для этого задачи; иначе вызови `goal_complete` один раз, с итогом для пользователя (что сделано и где это найти). Когда пользователь просит команду закругляться (это будет сказано при пробуждении), не начинай ничего нового: заверши свои задачи с тем, что есть.
10. Заканчивай каждый ход вызовом `wait`. Не опрашивай доску, сообщения или фоновые процессы в цикле. Тебя разбудят сообщения, назначения, запросы на ревью, посты в обсуждениях, разблокированные задачи, триггеры и завершение твоих фоновых процессов.

## Общение и экономия
- О задаче пиши в её обсуждение (`task_comment`), чтобы всё осталось при задаче; для остального используй `send_message`.
- Каждое сообщение или пост будит получателей и тратит общий бюджет. Пиши, только если это изменит чьи-то действия: без приветствий, благодарностей, подтверждений и отчётов о ходе работы; не отвечай на сообщения, которые не требуют ответа. Пиши коротко и по делу.
- Обращайся к пользователю только за решениями, которые действительно за ним (требования, меняющие результат, доступы, деньги, необратимые действия). Задай все вопросы одним сообщением и продолжай работу, которая не зависит от ответа. В остальных случаях решай сам, укажи допущение в итоге задачи и двигайся дальше. Пользователь видит доску: не отчитывайся ему о ходе работы. Если пользователь написал тебе, ответь.
- Вызывай несколько независимых инструментов в одном ответе (например, создавай сразу несколько задач). Не перечитывай то, что уже знаешь.

Пиши всё (сообщения, задачи, файлы) на русском языке, если пользователь не попросил иначе."""

# The owner sentence of a crew task (docs/SPEC.md §4.2 "The owner"): its head and tail around the part that differs
# (take a direction, the owner's own direction, all directions taken); the crew_owner_* keys below
_OWNER_HEAD = {"en": " You own crew task #{id}: ", "ru": " Ты владелец групповой задачи #{id}: "}
_OWNER_TAIL = {
    "en": "; the other members take the rest (up to {crew} members at once, you included) and write their cards. Keep "
          "its directions consistent: add the ones missing (task_update directions), cancel the ones not needed, and "
          "settle the interfaces between them in #{id}'s discussion. Each direction is handed in on its own; when all "
          "of them are finished, the board tells you and gives you their results as a draft: then check that they fit "
          "together, write #{id}'s result and complete it (task_complete).",
    "ru": "; остальные направления берут другие участники (одновременно до {crew} участников, включая тебя) и пишут "
          "их карточки. Следи, чтобы направления были согласованы: добавляй недостающие (task_update directions), "
          "отменяй ненужные и договаривайся об интерфейсах между ними в обсуждении #{id}. Каждое направление сдаётся "
          "отдельно; когда все они завершены, доска сообщит тебе об этом и даст их итоги черновиком: тогда проверь, "
          "что они сходятся между собой, напиши итог #{id} и заверши её (task_complete).",
}

TEXTS: dict[str, dict[str, str]] = {
    "en": {
        "instructions": "Your instructions: {text}\n",
        "wake_header": "=== Wake-up {time} ===",
        "incoming_header": "=== New events while you were working ({time}) ===",
        "reasons": "What happened:",
        "goal": "Global goal: {title} [{status}]",
        "goal_none": "Global goal: not set yet (wait for the user).",
        "my_tasks": "Your open tasks: {tasks}",
        "my_tasks_none": "Your open tasks: none.",
        # one line per open task of the member blocked by unfinished dependencies (repeated at each wake-up: short)
        "my_blocked": "#{id} is blocked by {blockers}. If its result can be produced without some of them, remove "
                      "those (`task_update`, depends_on = the ones still needed) and say why in its discussion.",
        "my_reviews": "Waiting for your review: {tasks}",
        "my_jobs": "Your background jobs: {jobs}",
        "my_job": "j{id} {command} (running {duration})",
        # a running job that printed nothing for jobs.SILENT_MINUTES (also in jobs / swarm_status): the agent decides
        "my_job_silent": "j{id} {command} (running {duration}, no output for {silent})",
        "job_silent": "no output for {duration}",
        # a wake-up with a message to "all": several members read it at once (run 006: three alike tasks)
        "all_hint": "Before creating a task for a message to all, check the board: one member creates it, the "
                    "others take it or comment.",
        # the task_create result when open tasks look alike (board.similar_tasks); the task is created anyway
        "similar_tasks": "Similar open tasks: {tasks}. If yours duplicates one of them, cancel it (`task_update` "
                         "status=cancelled) and take or comment on that one.",
        # every wake-up while the user's wrap-up mode is on (Swarm.wrap_up): short, it repeats
        "wrap_up_line": "Wrap-up mode (the user): start no new computations or background jobs; finish your tasks "
                        "with what you have.",
        "ready": "Ready unassigned tasks: {tasks}",
        "notes": "Your notes:\n{notes}",
        "notes_empty": "Your notes: (empty)",
        "notes_unchanged": "Your notes: unchanged since they were last shown to you above.",
        "msg": "Message #{id} from {sender} to {to}:\n{text}",
        "msg_reply": " (reply to #{reply_to})",
        "you": "you",
        "nudge": "You stopped without calling a tool, but tasks {tasks} are still in progress, and plain text reaches "
                 "nobody. Continue the work with tools. If a task is finished, call `task_complete`; if you are "
                 "blocked, set it to `waiting` and say why in its discussion; then call `wait`.",
        "compaction_request": "Your context is getting long and will now be replaced by a summary. Write a thorough "
                              "summary of your work for your future self: your role and the goal; tasks you own and "
                              "their state; key decisions, findings, file paths, commands and results; open questions "
                              "and agreements with other members; what you were about to do next. Keep task numbers, "
                              "names and paths exact. Do not call tools; reply with the summary only.",
        "compacted": "[Context compacted. Summary of your earlier work:]\n{summary}\n[End of summary. Continue from "
                     "here. The board is the source of truth: `board` with assignee 'me' lists your tasks.]",
        "continue": "Continue your work.",
        "cut_off": "[Your reply was cut off by the output limit. Continue in smaller steps, e.g. write a large file "
                   "in several parts.]",
        # notifications
        "n_hired": "You have just joined the swarm: {by} hired you as \"{role}\". Start with the tasks assigned to "
                   "you, or claim a ready task that fits your role; if there is none yet, call `wait` (you will be "
                   "woken). No greeting is needed.",
        "n_started": "The swarm has started and you are one of its first members. Look at the goal and the board.",
        "n_kickoff": "{by} set a new global goal: \"{title}\". {description}\nTeam: {team}.\nPlanning task "
                     "#{task_id} \"{task_title}\" is on the board; exactly one member plans. If it is still "
                     "unassigned, claim it and plan. If it is taken or your claim fails, don't plan in parallel and "
                     "don't offer help: claim a ready task if there is one, otherwise call `wait` (you will be woken "
                     "when tasks appear; the first ones come before the plan is complete).",
        "n_goal_updated": "{by} set the global goal: \"{title}\". {description}\nCheck that your open tasks still "
                          "serve it; change or cancel those that don't. Before creating new tasks, check the board: "
                          "another member may be creating them right now.",
        "n_goal_reopened": "The user reopened the global goal: \"{title}\". Comment: {comment}\nThe result is not "
                           "accepted yet. Check the board first (another member may already be on it), then create "
                           "or claim tasks for what is missing; when it is done and verified, call `goal_complete` "
                           "again.",
        "n_assigned": "Task #{id} \"{title}\" was assigned to you by {by}. Claim it with `task_claim` when you start "
                      "working on it.",
        "n_unassigned": "Task #{id} \"{title}\" was taken away from you by {by}. Stop working on it.",
        "n_unblocked": "Task #{id} \"{title}\" is now unblocked: all its dependencies are done. Claim it with "
                       "`task_claim` to start.",
        # the last unfinished dependencies of the recipient's task were removed, not done (Board._deps_changed)
        "n_unblocked_removed": "Task #{id} \"{title}\" is no longer blocked: {by} removed its unfinished "
                               "dependencies {deps}, so do not wait for their results. Claim it with `task_claim` "
                               "to start.",
        # the assignee of a task the recipient created removed dependencies (board.Board._deps_changed)
        "n_deps_removed": "{by}, the assignee of task #{id} \"{title}\" (created by you), removed its dependencies "
                          "{deps}: the task no longer waits for them. The reason should be in its discussion; if one "
                          "of them is needed after all, say so there or add it back with `task_update`.",
        # the record of that removal in the task's discussion, posted under the assignee's name
        "post_deps_removed": "[board] {by}, the assignee, removed the dependencies {deps} of this task: it no longer "
                             "waits for them.",
        "n_review_requested": "Task #{id} \"{title}\" is waiting for your review (sent by {by}). Check the result "
                              "yourself against the task goal and the contracts it references (`task_get`; open the "
                              "files, run the code and the tests), then call `task_review`: approve only what you "
                              "verified, or return it with concrete fixes. Do it before your own work: the assignee "
                              "and the tasks that depend on it are waiting.",
        "n_review_approved": "Your task #{id} \"{title}\" was approved by {by}. {comment}",
        # {ending}: review_rejected_plain, or review_rejected_crew for a crew task (the assignee's language)
        "n_review_rejected": "Your task #{id} \"{title}\" was returned by {by}: {comment}\n{ending}",
        "review_rejected_plain": "Fix it, then call `task_complete` again.",
        "review_rejected_crew": "#{id} is a crew task: fix it yourself, or with new directions (task_update "
                                "directions) and, once they are finished, update #{id}'s result; then call "
                                "`task_complete` again.",
        "n_reviewer_left": "{reviewer}, the reviewer of your task #{id} \"{title}\", left the swarm, so the task is back "
                           "in progress. If it still needs a review, set another reviewer with `task_update`; then "
                           "call `task_complete` again.",
        "n_review_cancelled": "You no longer need to review task #{id} \"{title}\": {by} changed it (reviewed it, gave "
                              "the review to someone else or took the task back).",
        "n_review_reminder": "Tasks are still waiting for your review: {tasks}. Their assignees and the tasks that "
                             "depend on them are blocked until you decide. Review them now with `task_review` (check "
                             "each result against its goal: open the files, run the code), before your own work.",
        "n_review_reassigned": "You did not review task #{id} \"{title}\" in time, so its review went to {reviewer}; "
                               "you no longer need to review it.",
        "n_task_closed": "Task #{id} \"{title}\" (created by you) is now {status} (by {by}). Result: {result}",
        # crew tasks and subtasks (docs/SPEC.md §4.2): notifications
        # a parent got a finish state: {counts} joins count_* ("5 done, 1 cancelled"), {ending} is an ending_* key
        # (the holder's or the creator's form; for a crew task with all directions finished, plus the draft)
        "n_subtasks_done": "All {total} subtasks of #{id} \"{title}\" are finished ({counts}): {ending}",
        "n_direction_failed": "#{id} \"{title}\", a direction of your crew task #{parent}, failed ({by}): reopen it to "
                              "be done again (task_update #{id} status=todo), or cancel it if #{parent} does without "
                              "it (status=cancelled).",
        "n_direction_cancelled": "#{id} \"{title}\", a direction of your crew task #{parent}, was cancelled ({by}): if "
                                 "#{parent} still needs that part, add it again (task_update directions).",
        "n_subtask_cancelled": "{by} cancelled #{id} \"{title}\", your part of #{parent}: stop the work on it; if what "
                               "you have done is still needed, say so in #{parent}'s discussion.",
        "n_direction_taken": "{by} took #{id} \"{title}\", a direction of your crew task #{parent}.",   # quiet
        "n_direction_freed": "#{id} \"{title}\", a direction of your crew task, is free again: {by} left the "
                             "swarm.",   # quiet
        # someone else made the recipient the holder of a crew task; {instructions}: a crew_owner_* key, or " " and
        # an owner_prefix_* key with its ending; {directions}: crew_directions (+ crew_directions_more), or ""
        "n_crew_owner": "#{id} \"{title}\" is yours now (crew {crew}).{instructions}{directions}",
        "n_crew_changed": "{by} set the crew of #{id} \"{title}\" to {crew} (0 = an ordinary task).",   # quiet
        "n_parent_closed": "#{id} \"{title}\", of which your #{sub} is a part, is {status}: finish #{sub} only if it "
                           "is still needed on its own; otherwise cancel it (task_update status=cancelled).",
        # the check of the whole of a crew task, in place of n_review_requested; {n}: its done directions
        "n_crew_review_requested": "#{id} \"{title}\" (sent by {by}) is a crew task whose {n} directions are done: "
                                   "check it as a whole — do the directions fit together, and does the result meet its "
                                   "goal? Approve it, or return it with what does not fit (task_review).",
        # a holder stalls with liveness marks only: {text} is their mark_* sentences, without the leading space
        "n_marks_stalled": "{text}",
        # crew tasks: fragments the board and the scheduler render into the texts above, in the recipient's language
        # the draft of a crew task's result (Board._crew_draft); the board adds the line break before it
        "crew_draft": "Draft for #{id}'s result: its directions in the order they were created, with the results of "
                      "the done ones:\n{items}",
        "crew_draft_cut": "(cut: task_get #{id} shows more of it)",
        "crew_draft_more": "… more directions not shown: {k}; task_get #{id} lists them, and task_get on a direction "
                           "shows its result.",
        "crew_draft_no_result": "(no result)",
        # the parts of {counts}, joined with ", "
        "count_done": "{n} done",
        "count_cancelled": "{n} cancelled",
        "count_failed": "{n} failed",
        # endings: after "...: " in n_subtasks_done and the marks; *_creator for a creator asked to take the parent;
        # {failed}: the id of the first failed direction
        "ending_plain": "assemble their results and complete it.",
        "ending_plain_creator": "take #{id} (task_claim), assemble their results and complete it.",
        "ending_crew_failed": "reopen the failed #{failed} to be done again (task_update #{failed} status=todo), "
                              "cancel it if #{id} does without it (status=cancelled), or fail #{id}.",
        "ending_crew_failed_creator": "reopen the failed #{failed} to be done again (task_update #{failed} "
                                      "status=todo), cancel it if #{id} does without it (status=cancelled), or cancel "
                                      "#{id}.",
        "ending_crew_cancelled": "add new directions (task_update directions), do it yourself (then task_complete), "
                                 "make it an ordinary task (crew 0), or cancel it.",
        "ending_crew_cancelled_creator": "add new directions (task_update directions), take #{id} and do it yourself "
                                         "(task_claim, then task_complete), make it an ordinary task (crew 0), or "
                                         "cancel it.",
        "ending_crew_finished": "check that they fit together, write #{id}'s result from theirs (what each one "
                                "delivered and where, and what was left out) and complete it (task_complete).",
        "ending_crew_finished_creator": "take #{id} (task_claim), check that they fit together, write #{id}'s result "
                                        "from theirs (what each one delivered and where, and what was left out) and "
                                        "complete it (task_complete).",
        # the owner form of an ending: the prefix, then the holder's ending
        "owner_prefix_plain": "All its subtasks are finished: ",
        "owner_prefix_failed": "A direction failed: ",
        "owner_prefix_cancelled": "All its directions were cancelled: ",
        "owner_prefix_finished": "All its directions are finished: ",
        # the owner sentence (n_crew_owner's {instructions}, and the results of the calls that make the actor the
        # owner); {sub} [{status}]: the owner's own direction
        "crew_owner_take": _OWNER_HEAD["en"] + "take one of its directions yourself (task_claim; with #{id} it counts "
                                               "as one task in progress)" + _OWNER_TAIL["en"],
        "crew_owner_own": _OWNER_HEAD["en"] + "your own direction is #{sub} [{status}]" + _OWNER_TAIL["en"],
        "crew_owner_all_taken": _OWNER_HEAD["en"] + "all its directions are taken: if a part of #{id} is left that "
                                                    "nobody does, add it as a direction (task_update directions) and "
                                                    "it is yours; otherwise keep the directions consistent until they "
                                                    "are finished, then complete #{id}" + _OWNER_TAIL["en"],
        "crew_owner_none": " You own crew task #{id}. It has no directions yet: add them now (task_update directions; "
                           "the first one is your own part), so the others can join; if #{id} needs no split after "
                           "all, make it an ordinary task (crew 0), or do it yourself and complete it (task_complete).",
        # its directions ({items}: '#15 [in_progress] "pkce" @Anna Smith; #17 [todo] "oidc"'), at most 20, then
        # crew_directions_more
        "crew_directions": " Its directions: {items}.",
        "crew_directions_more": " More directions not listed: {k}; task_get #{id} lists them all.",
        # the {owner} part of note_direction_card
        "direction_owner_named": "owner {name}",
        "direction_owner_none": "no owner yet",
        "direction_owner_you": "you own #{id}",
        # notes appended to the result of the actor's own call (each starts with a space)
        "note_direction_card": " #{sub} is a direction of crew task #{id} ({owner}): first write its card with "
                               "task_update — title, goal (what it delivers and how it is checked), description (the "
                               "files it touches) — then do the work. In #{id}'s discussion post only what another "
                               "direction depends on (interfaces, shared files).",
        "note_reviewer_cleared": " You were the reviewer of #{id}; since you work on one of its directions now, you do "
                                 "not check it as a whole: another member does, if a check applies.",
        "note_own_part": " #{sub} \"{title}\" is your own part: write its card (task_update), and if you have done "
                         "some of #{id} already, say in #{id}'s discussion what is done, so nobody redoes it.",
        "note_own_part_blocked": " #{sub} \"{title}\" is your own part; #{id} is blocked by {blockers}: write its card "
                                 "now (task_update), and claim #{sub} when #{id} is no longer blocked (task_claim).",
        "note_still_open": " Your #{sub} is still open: finish it only if it is still needed on its own; otherwise "
                           "cancel it (task_update status=cancelled).",
        "note_crew_say_why": " Its creator was told: say in #{id}'s discussion why you changed its crew (a post there "
                             "wakes the creator).",
        # a direction left todo without an assignee under a terminal crew task is cancelled in its place
        "note_direction_closed": " #{parent} is {status}, so #{id} is cancelled instead of going back to the board: "
                                 "nobody takes a direction of a closed crew task.",
        # a task became failed or cancelled while open tasks depend on it; {deps}: "#5, #7"
        "note_dependents_blocked": " {deps} depend on #{id}, which is {status} now: they stay blocked until that "
                                   "dependency is removed (task_update depends_on) or they are cancelled.",
        "note_dependent_blocked": " {deps} depends on #{id}, which is {status} now: it stays blocked until that "
                                  "dependency is removed (task_update depends_on) or it is cancelled.",
        # liveness marks (n_stalled's {marks}, n_marks_stalled's {text}) and the in-activation nudge; each starts
        # with a space; {ending}: an owner_prefix_* key with its ending; {directions}: '#17 "oidc", #18 "jose"'
        "mark_subtasks_done": " #{id} \"{title}\": {ending}",
        "mark_free_directions": " #{id} is a crew task with free directions nobody has taken: {directions}. Take one "
                                "yourself (task_claim), or cancel the ones it does without (task_update "
                                "status=cancelled).",
        # {sub} "{title}": a todo direction assigned to the member that its claim would take now
        "mark_direction_to_take": " #{sub} \"{title}\", your direction of crew task #{id}, can be taken now: claim it "
                                  "(task_claim) and do it, or unassign yourself if you will not (task_update "
                                  "assignee='').",
        "n_discussion": "{by} posted in the discussion of task #{id} \"{title}\" (post #{post_id}{reply}): {text}",
        "n_mention": "{by} mentioned you in the discussion of task #{id} \"{title}\" (post #{post_id}): {text}\n"
                     "If it needs your answer, reply with `task_comment` (reply_to={post_id}).",
        "reply_suffix": ", reply to #{reply_to}",
        "n_trigger": "Trigger #{id} fired: {condition}. {note}",
        # a fire of a trigger whose file_changed() files changed since its last fire (triggers.py): one fragment per
        # file, each starting with a space; {by} is trigger_file_by or empty
        "n_trigger_files": "Trigger #{id} fired: {condition}.{changes} {note}",
        "trigger_file_settled": " File {path} was changed (changes: {count}{by}); the last change was {quiet} ago.",
        "trigger_file_changing": " File {path} is still changing (changes: {count}{by} in {duration}).",
        "trigger_file_by": ", by {names}",
        # the fires of a member's repeating trigger held back by [agents] trigger_wake_seconds, in one notice
        # ({changes}: the n_trigger_files fragments of all of them, or empty)
        "n_trigger_held": "Trigger #{id} fired again since it last told you at {since} (fires: {count}, the last at "
                          "{last}): {condition}.{changes} A repeating trigger wakes you at most once every {interval}. "
                          "{note}",
        # ... the same once the limit is off (trigger_wake_seconds = 0) while fires were held: no interval to name
        "n_trigger_held_off": "Trigger #{id} fired again since it last told you at {since} (fires: {count}, the last "
                              "at {last}): {condition}.{changes} {note}",
        "n_ready": "Ready unassigned tasks are waiting for someone: {tasks}. If you have no task in progress, claim "
                   "the one that suits you best (if the claim fails, take another). Otherwise, or if none of them is "
                   "work you can do, call `wait`.",
        "n_all_done": "All tasks on the board are finished, but the global goal is still open. Verify the result "
                      "against the goal (read the deliverables, run them). If something is missing, create tasks for "
                      "it; otherwise call `goal_complete` with a summary for the user.",
        # n_all_done while the user's wrap-up mode is on: no new tasks for what is missing
        "n_all_done_wrap_up": "All tasks on the board are finished, and the user asked the team to wrap up. Don't "
                              "create new tasks: call `goal_complete` with a summary for the user (what was achieved, "
                              "where it is, what is left undone).",
        # the user switched wrap-up mode on (Swarm.wrap_up): every AI agent, once
        "n_wrap_up": "The user asked the team to wrap up: start no new computations or background jobs, not even one "
                     "more quick pass. Finish your tasks with what you have now: `task_complete` with the result so "
                     "far and what is left. Do the reviews waiting for you. Stop the background jobs nobody needs. "
                     "When every task is finished, the goal is completed (`goal_complete`) with a "
                     "summary of what was achieved and what is left, without new tasks for the rest.",
        "n_wrap_up_end": "Wrap-up mode is off: background jobs are allowed again; continue the work as usual.",
        # [swarm] network = "off" (docs/SPEC.md §7.2): each member once, quietly, at its first wake-up after the
        # setting became "off" (Swarm._tell_network); n_network_on when the user turns it back on
        "n_network_off": "This session has no internet (the user's setting): your commands, their background jobs "
                         "and the MCP servers cannot reach the network; localhost works. Installing packages from "
                         "PyPI or npm, git clone, fetch or push with a remote repository, and downloads fail: don't "
                         "spend steps on them or on getting around it. Work with what is already in the workspace "
                         "and installed; if something you need is missing, say so in the task's discussion.",
        "n_network_on": "The user turned the internet back on for this session: new commands can reach the network "
                        "again.",
        "n_restart": "The swarm was restarted. Continue your work where you left off.",
        "n_step_limit": "You reached the step limit of one wake-up. Continue where you left off; if you were "
                        "repeating yourself without progress, change your approach or ask for help.",
        "n_stalled": "Your tasks {tasks} are in progress, but you stopped working on them. Continue now. If you "
                     "are blocked, set the task to `waiting` and say why in its discussion; if you cannot do it, "
                     "set it back to `todo` (or `failed`) with a comment.{marks}",   # {marks}: mark_* sentences, or ""
        "n_planning_hint": "You have held planning task #{id} for {elapsed} (steps: {steps}) and have created no task "
                           "yet, while the other members wait for work. Create the independent tasks now with "
                           "`task_create`: the research, experiments and environment checks you would still do "
                           "become tasks for the others; read only what you need to split the work. Then go on "
                           "with the plan.",
        # in its place for a clarify planning card whose holder has neither asked the user nor created a task
        "n_clarify_hint": "You have held planning task #{id} for {elapsed} (steps: {steps}) without asking the user "
                          "anything or creating a task, while the other members wait. If the goal has forks that "
                          "change the plan, send the user all your questions in one message now (`send_message` to "
                          "[\"user\"]), then set the task to `waiting` and call `wait`; create no task before the "
                          "answer. If there is nothing to ask, plan now: create the independent tasks with "
                          "`task_create`.",
        "n_mcp_approved": "Your MCP server \"{name}\" was approved by the user and is ready to use.",
        "n_mcp_rejected": "Your MCP server \"{name}\" was rejected by the user. {comment}",
        "n_resumed": "The swarm was resumed. Continue your work.",
        # a member's question paused the swarm (send_message pause_swarm, docs/SPEC.md §3 "Clarify mode"): the others
        # get the question with the answer (quiet for AI agents), or only the question when the answer reached them
        # as a message too (n_user_answer_seen)
        "n_user_answer": "The user answered {asker}'s question #{id}, for which the swarm was paused.\nQuestion: "
                         "{question}\nAnswer: {answer}\nIf the answer changes what you are doing, adjust your work and "
                         "your tasks; otherwise continue.",
        "n_user_answer_seen": "Message #{answer_id} from the user below answers {asker}'s question #{id}, for which "
                              "the swarm was paused.\nQuestion: {question}\nIf the answer changes what you are doing, "
                              "adjust your work and your tasks; otherwise continue.",
        # to the asker: the swarm was resumed (or clarifying switched off) before the user answered
        "n_question_unanswered": "The swarm goes on without the user's answer to your question #{id}. Don't wait for "
                                 "it: where you need the answer, take the option you would choose, write it down as an "
                                 "assumption where the others read it (CONTRACTS.md, a task's description or result) "
                                 "and continue. Should the user answer later, the answer comes as a message.",
        # to the holder of a clarify planning card that waits for the user, when [swarm] clarify was switched off
        "n_clarify_off": "The user no longer wants the goal clarified before the work starts: don't wait for answers "
                         "to your questions about it. Take your defaults, write them down as assumptions "
                         "(CONTRACTS.md, the task descriptions), set planning task #{task_id} back to `in_progress` if "
                         "it is `waiting`, and plan now. Should the user answer later, the answer comes as a message.",
        "n_workspace_moved": "The user moved the shared workspace to the folder {path}. Its files and git history "
                             "are the same; only the folder changed.",
        "n_workspace_changed": "The user switched the shared workspace to the folder {path}. It holds other files "
                               "than before (the previous ones stayed in {old}); new commits go to the current "
                               "branch of its git repository. Look at its files (`list_files`) before you continue: "
                               "files you knew may be missing or different.",
        "n_member_retired": "{name} left the swarm ({reason}). Their unfinished tasks went back to todo: {tasks}.",
        # file locks (workspace.py): to a member refused a path, once no other member's lock covers it any more
        "n_lock_free": "Free again: {paths} (you were refused it earlier). If you still need it, read it again first: "
                       "it has probably changed. Before a long edit, lock it with `lock_file`: other members may want "
                       "it too.",
        # to the holder of a lock others were refused, once per lock, after [workspace] lock_wanted_minutes
        "n_lock_wanted": "{path}, which you have held for {held}, is wanted by: {waiters}. If you are still editing "
                         "it, finish that edit first and unlock it, then go on with the rest of your task. If you no "
                         "longer need it, unlock it now (`lock_file` with unlock=true). No reply is needed: they are "
                         "woken when it is free.",
        "lock_waiter_task": "{name} (task {tasks})",   # one waiter of n_lock_wanted with its tasks in progress
        "lock_waiter_direction": "#{id} {title} (direction of #{parent})",   # a direction among those tasks
        # background jobs (jobs.py): parts of the notices, built in the owner's language
        "job_exit": "exit code {code}",
        "job_gone": "all its processes exited",
        "job_no_output": "(no output)",
        # they reach agents of every tool set revision: they name no tool (only the log)
        "n_job_done": "Your background job j{id} has ended ({result}) after running {duration}. Command: {command}\n"
                      "Last lines of its output:\n{tail}\nThe whole output: {log}.",
        # the shell of a job ended while programs it started run on (a build that left a daemon): the job goes on
        "n_job_command_done": "The command of your background job j{id} has finished ({result}) after {duration}, but "
                              "programs it started still run, so j{id} goes on: {names}. Command: {command}\nLast "
                              "lines of its output:\n{tail}\nThe whole output: {log}.",
        "n_job_stopped": "The user stopped your background job j{id} after it ran {duration}. Command: {command}\n"
                         "Its output: {log}.",
        "n_job_limit": "Your background job j{id} was stopped: it ran {duration}, the longest allowed. Command: "
                       "{command}\nIts output: {log}.\nStart it again only if it is still needed.",
        "n_job_gone": "Your background job j{id} is no longer running: the session was stopped or restarted. "
                      "Command: {command}\nIts output until then: {log}.\nStart it again if you still need it (e.g. a "
                      "server that later steps use).",
        # a compaction moved the agent to the current tool set (agent.py _replace_with_summary)
        "tools_added": "Your tools were updated with this compaction. New: {tools}; their descriptions say how to "
                       "use them.",
        # the skills attached to the swarm (swarm_skills.py): the full index once per agent memory (the first
        # wake-up, after a restart, in a compaction text), then the one-line skills_names
        "skills_index": "Skills attached to the swarm (a skill is a SKILL.md with instructions for a kind of work, "
                        "sometimes with scripts; later wake-ups list only their names, and you are told when one is "
                        "attached, updated or detached). Before work one of them fits, read its SKILL.md with "
                        "`read_file` and follow it: paths in it are relative to its folder, and its scripts run with "
                        "`shell` by their path from the workspace root. A skill written for another assistant may "
                        "name its tools: use yours instead (`shell` for Bash, `read_file` for Read, `write_file` for "
                        "Write, `edit_file` for Edit); ${{CLAUDE_SKILL_DIR}} means the skill's folder shown here."
                        " When a skill's line names a Python, run that skill's Python scripts with it: its packages are "
                        "installed there (the environment is shared with other sessions, so install nothing into "
                        "it). When a line says its Node packages are linked, its scripts find them: do not run npm "
                        "install or npm ci in the skill's folder. When a line says nothing about packages, the "
                        "skill lists none in a requirements.txt, pyproject.toml or package.json: install what its "
                        "SKILL.md asks for yourself with `shell`, best inside your scratch folder (a virtual "
                        "environment there, for example).\n{skills}",
        "skills_index_own": "Skills attached to the swarm (a skill is a SKILL.md with instructions for a kind of "
                            "work, sometimes with scripts; later wake-ups list only their names, and you are told "
                            "when one is attached, updated or detached). Before work one of them fits, read its "
                            "SKILL.md with `read_file` and follow it: paths in it are relative to its folder, and its "
                            "scripts run with `shell` by their path from the workspace root. A skill written for "
                            "another assistant may name its tools: use yours instead (`shell` for Bash, `read_file` "
                            "for Read, `write_file` for Write, `edit_file` for Edit); ${{CLAUDE_SKILL_DIR}} means the "
                            "skill's folder shown here."
                            " When a skill's line names a Python, run that skill's Python scripts with it: its packages are "
                            "installed there (the environment is shared with other sessions, so install nothing into "
                            "it). When a line says its Node packages are linked, its scripts find them: do not run npm "
                            "install or npm ci in the skill's folder. When a line says nothing about packages, the "
                            "skill lists none in a requirements.txt, pyproject.toml or package.json: install what its "
                            "SKILL.md asks for yourself with `shell`, best inside your scratch folder (a virtual "
                            "environment there, for example). \"Your role's\": the skills of your role, made "
                            "for your work.\n{skills}",
        "skills_item": "- {name}{own}: {description} → {path}{deps}",
        "skills_own": " (your role's)",
        "skills_manual": " (only when the user asks for it)",
        "skills_path_outside": "{path} (outside the workspace: read it with `shell`)",
        "skills_names": "Skills attached to the swarm: {names}. Their SKILL.md paths and descriptions are in the list "
                        "shown to you earlier and in your notifications.",
        "skills_none": "Skills: none are attached to the swarm any more.",
        # quiet for AI agents (their next render shows them), a wake for external members (swarm_skills._announce)
        "n_skill_added": "The skill \"{name}\" was attached to the swarm: {description}\nBefore work it fits, read "
                         "{path} with `read_file` and follow it.{deps}",
        "n_skill_updated": "The skill \"{name}\" was updated: {description}\nIf you use it, read {path} again with "
                           "`read_file`.{deps}",
        "n_skill_removed": "The skill \"{name}\" was detached from the swarm: don't use it any more (its folder is "
                           "gone).",
        "n_skill_shared": "{owner} proposed the skill \"{name}\", and it is attached to the swarm: {description}\n"
                          "Before work it fits, read {path} with `read_file` and follow it.{deps}",
        # a skill's script dependencies (skill_deps.py): fragments of its index line and of its notifications,
        # " " first ("" when it lists none); {python} and {bin} are paths, never in backticks
        "skills_deps_python": " Python: {python} (packages from {files} installed).{extra}",
        "skills_deps_node": " Node: packages from {files} are linked into its folder.{extra}",
        "skills_deps_node_path": " Node: packages from {files} are in {path}; set NODE_PATH to that folder for "
                                 "require() (import does not read NODE_PATH).",
        "skills_deps_bin": " Their commands ({names}) are in {bin}.",
        "skills_deps_own": " The skill's own code is not installed as a package: run its scripts by their path (for "
                           "a src layout, set PYTHONPATH to its src folder).",
        "skills_deps_installing": " Packages from {files}: being installed; you are told when they are ready.",
        # an AI member: the news is quiet (it waits for the member's next wake-up), so no wake-up is promised
        "skills_deps_installing_quiet": " Packages from {files}: being installed; a later wake-up of yours says "
                                        "when they are ready (the install does not wake you: meanwhile do other "
                                        "work).",
        "skills_deps_failed": " Packages from {files}: installing them failed ({error}; log: {log}, read it with "
                              "`shell`); install what you need yourself.",
        "skills_deps_manual": " Packages from {files}: not installed automatically ({reason}); install what you need "
                              "yourself.",
        "deps_reason_off": "turned off in the settings",
        "deps_reason_proposals": "turned off for members' proposals and the workspace's Claude Code skills, which "
                                 "agents can write",
        "deps_reason_network": "this session has no network, and then nothing is installed for members' proposals and "
                               "the workspace's Claude Code skills, which agents can write",
        "deps_reason_unavailable": "{detail} is not available on this machine",
        "deps_reason_python": "it needs Python {need}, the environments use {have}",
        "deps_reason_unsupported": "not supported: {detail}",
        "n_skill_deps_ready": "The packages of the skill \"{name}\" are installed.{deps}",
        "n_skill_deps_failed": "Installing the packages of the skill \"{name}\" failed.{deps}",
        # to the proposer (a wake): the user's library decision; {comment} is the user's comment after a space, or ""
        "n_skill_saved": "The user saved your skill \"{name}\" to the skills library: future runs can use it too.",
        "n_skill_not_saved": "The user decided not to save your skill \"{name}\" to the library; it stays attached "
                             "to this swarm.{comment}",
        # notifications for the user (shown in the UI / console / MCP)
        "u_mcp_pending": "{owner} created MCP server \"{name}\": {description}. Approve or reject it (MCP tab, "
                         "`/approve {name} {digest}` or `mcp_approve` with digest {digest}).",
        "u_skill_proposed": "{owner} proposed the skill \"{name}\" (files: {files}): {description}. The swarm uses it "
                            "already.{installs} Save it to the skills library for future runs, or not: Skills tab, "
                            "/skills approve {name} or `skill_approve`.",
        "u_skill_installs": " Its packages ({files}: {packages}) are installed automatically, in an environment kept "
                            "apart from your own skills'.",
        "u_skill_deps_failed": "Installing the packages of the skill \"{name}\" ({files}) failed: {error}. Log: "
                               "{log}. Reinstall in the Skills tab, with /skills reinstall {name} or "
                               "`skill_reinstall`.",
        "u_role_skills_missing": "The role \"{role}\" lists skills that are not in the skills library: {names}. Add "
                                 "them to the library (the Skills tab) or remove them from the role.",
        "u_role_skills_over_cap": "The role \"{role}\" lists skills that were not attached, because the swarm already "
                                  "has the maximum of {max} skills: {names}. Detach some, or raise max_attached in "
                                  "the skills settings.",
        "u_agent_error": "Agent {name} hit an error: {error}",
        "u_budget": "Budget exhausted: spent ${spent:.2f} of ${limit:.2f}. The swarm is paused; raise the limit to "
                    "continue.",
        "u_cost_unknown": "DreamTeam cannot count the cost of model profile \"{profile}\" (model {model}): it has no "
                          "{fields}, and the provider does not report the cost of a call, so those tokens count as "
                          "$0. The spending shown is lower than the real one, and the session budget may never stop "
                          "the swarm. Set the prices ($ per 1M tokens) in Settings → Models and endpoints (or in "
                          "dreamteam.toml), or 0 for a free model.",
        "u_question": "{name} asked you a question (message #{id}), and the whole swarm is paused until you answer. "
                      "Your next message to {name} (or to all) is the answer: every member gets it with the question, "
                      "then the swarm goes on. To go on without an answer, resume the swarm (Resume on the board, "
                      "/pause in dreamteam tui, /resume in the console, `swarm_control` action=resume).",
        "u_question_left": "{name} left the swarm, so it no longer waits for your answer to question #{id} and goes "
                           "on.",
        "u_goal_done": "{by} reports that the global goal is achieved: {summary}",
        "u_refusal": "Agent {name}: the model declined the request ({category}).",
        "u_member_joined": "{name} joined the swarm as \"{role}\" (hired by {by}).",
        "u_review_requested": "Task #{id} \"{title}\" waits for your review (sent by {by}). Approve or return it in "
                              "its card on the board, or in the terminal: /approve {id} [comment] or /reject {id} "
                              "<what to fix>.",
        "u_review_fallback": "{by} finished task #{id} \"{title}\", and no other member can review it now (an agent "
                             "never approves its own work), so it waits for your review. Approve or return it in its "
                             "card on the board, or in the terminal: /approve {id} [comment] or /reject {id} <what to "
                             "fix>. With two or more active agents they review each other; independent review can be "
                             "turned off in the settings.",
        "u_review_handed_over": "Task #{id} \"{title}\" now waits for your review: {reviewer} did not review it, and no "
                                "other agent can take the review over (an agent never approves its own work). Approve "
                                "or return it in its card on the board, or in the terminal: /approve {id} [comment] or "
                                "/reject {id} <what to fix>.",
        "u_reviews_waiting": "The team has nothing else to do: tasks {tasks} are waiting for your review. Approve or "
                             "return them on the board, or with /approve N or /reject N <what to fix>.",
        "u_jobs_running": "The goal is achieved, but background jobs of the agents are still running: {jobs}. They "
                          "stop when the session stops; to stop one earlier, use the Jobs tab on the board or "
                          "/jobs stop <id> in the terminal.",
    },
    "ru": {
        "instructions": "Твои инструкции: {text}\n",
        "wake_header": "=== Пробуждение {time} ===",
        "incoming_header": "=== Новые события, пока ты работал ({time}) ===",
        "reasons": "Что произошло:",
        "goal": "Глобальная цель: {title} [{status}]",
        "goal_none": "Глобальная цель: ещё не задана (дождись пользователя).",
        "my_tasks": "Твои открытые задачи: {tasks}",
        "my_tasks_none": "Твои открытые задачи: нет.",
        "my_blocked": "#{id} ждёт задач {blockers}. Если её результат можно получить без некоторых из них, убери "
                      "их (`task_update`, в depends_on — только те, что ещё нужны) и объясни причину в её обсуждении.",
        "my_reviews": "Ждут твоего ревью: {tasks}",
        "my_jobs": "Твои фоновые процессы: {jobs}",
        "my_job": "j{id} {command} (работает {duration})",
        "my_job_silent": "j{id} {command} (работает {duration}, {silent} без вывода)",
        "job_silent": "{duration} без вывода",
        "all_hint": "Прежде чем создавать задачу по сообщению для всех, проверь доску: создаёт её один участник, "
                    "остальные берут её или комментируют.",
        "similar_tasks": "Похожие открытые задачи: {tasks}. Если твоя дублирует одну из них, отмени её "
                         "(`task_update` status=cancelled) и возьми ту или напиши в её обсуждение.",
        "wrap_up_line": "Режим завершения (пользователь): не начинай новых вычислений и фоновых процессов; заверши "
                        "свои задачи с тем, что есть.",
        "ready": "Свободные задачи, готовые к работе: {tasks}",
        "notes": "Твой блокнот:\n{notes}",
        "notes_empty": "Твой блокнот: (пусто)",
        "notes_unchanged": "Твой блокнот: без изменений с тех пор, как он был показан тебе выше.",
        "msg": "Сообщение #{id} от {sender} для {to}:\n{text}",
        "msg_reply": " (ответ на #{reply_to})",
        "you": "тебя",
        "nudge": "Ты остановился, не вызвав инструмент, но задачи {tasks} всё ещё в работе, а обычный текст никому "
                 "не доставляется. Продолжай работу с помощью инструментов. Если задача готова, вызови "
                 "`task_complete`; если ты заблокирован, переведи её в `waiting` и объясни причину в её обсуждении; "
                 "затем вызови `wait`.",
        "compaction_request": "Твой контекст стал слишком длинным и сейчас будет заменён резюме. Напиши подробное "
                              "резюме своей работы для себя будущего: роль и цель; твои задачи и их состояние; "
                              "ключевые решения, находки, пути к файлам, команды и результаты; открытые вопросы и "
                              "договорённости с другими участниками; что ты собирался сделать дальше. Номера задач, "
                              "имена и пути приводи точно. Не вызывай инструменты, ответь только резюме.",
        "compacted": "[Контекст сжат. Резюме твоей предыдущей работы:]\n{summary}\n[Конец резюме. Продолжай "
                     "отсюда. Источник правды — доска: `board` с assignee 'me' покажет твои задачи.]",
        "continue": "Продолжай работу.",
        "cut_off": "[Твой ответ обрезан по лимиту длины вывода. Продолжай меньшими шагами, например, пиши большой "
                   "файл в несколько приёмов.]",
        "n_hired": "Ты только что присоединился к рою: {by} нанял(а) тебя на роль «{role}». Начни с задач, "
                   "назначенных тебе, или возьми готовую задачу, подходящую твоей роли; если таких пока нет, вызови "
                   "`wait` (тебя разбудят). Приветствие не нужно.",
        "n_started": "Рой запущен, и ты один из первых участников. Посмотри на цель и доску задач.",
        "n_kickoff": "{by} задал(а) новую глобальную цель: «{title}». {description}\nКоманда: {team}.\nНа доске есть "
                     "задача планирования #{task_id} «{task_title}»; планирует ровно один участник. Если она ещё "
                     "свободна, возьми её и спланируй работу. Если её уже взяли или взять не получилось, не планируй "
                     "параллельно и не предлагай помощь: возьми готовую задачу, если она есть, иначе вызови `wait` "
                     "(тебя разбудят, когда появятся задачи; первые появятся ещё до того, как план готов).",
        "n_goal_updated": "{by} задал(а) глобальную цель: «{title}». {description}\nПроверь, что твои открытые "
                          "задачи всё ещё служат ей; измени или отмени те, что не служат. Прежде чем создавать новые "
                          "задачи, проверь доску: возможно, их прямо сейчас создаёт другой участник.",
        "n_goal_reopened": "Пользователь вернул глобальную цель в работу: «{title}». Комментарий: {comment}\n"
                           "Результат ещё не принят. Сначала проверь доску (возможно, этим уже занимается другой "
                           "участник), затем создай или возьми задачи на то, чего не хватает; когда всё будет сделано "
                           "и проверено, снова вызови `goal_complete`.",
        "n_assigned": "{by} назначил(а) тебе задачу #{id} «{title}». Возьми её через `task_claim`, когда приступишь "
                      "к работе.",
        "n_unassigned": "{by} снял(а) с тебя задачу #{id} «{title}». Прекрати работу над ней.",
        "n_unblocked": "Задача #{id} «{title}» разблокирована: все её зависимости выполнены. Чтобы начать, возьми её "
                       "через `task_claim`.",
        "n_unblocked_removed": "Задача #{id} «{title}» больше не заблокирована: {by} убрал(а) её невыполненные "
                               "зависимости {deps}, так что не жди их результатов. Чтобы начать, возьми её через "
                               "`task_claim`.",
        "n_deps_removed": "{by}, исполнитель задачи #{id} «{title}» (ты её создал), убрал(а) её зависимости {deps}: "
                          "задача больше их не ждёт. Причина должна быть в её обсуждении; если какая-то из них всё же "
                          "нужна, скажи об этом там или верни её через `task_update`.",
        "post_deps_removed": "[доска] {by}, исполнитель, убрал(а) зависимости {deps} этой задачи: она больше их не "
                             "ждёт.",
        "n_review_requested": "Задача #{id} «{title}» ждёт твоего ревью (отправил(а) {by}). Сам сверь результат с "
                              "целью задачи и контрактами, на которые она ссылается (`task_get`; открой файлы, "
                              "запусти код и тесты), затем вызови `task_review`: принимай только то, что проверил, "
                              "иначе верни с конкретными исправлениями. Сделай это раньше своей работы: её ждут "
                              "исполнитель и зависящие от неё задачи.",
        "n_review_approved": "{by} принял(а) твою задачу #{id} «{title}». {comment}",
        "n_review_rejected": "{by} вернул(а) твою задачу #{id} «{title}» на доработку: {comment}\n{ending}",
        "review_rejected_plain": "Исправь и снова вызови `task_complete`.",
        "review_rejected_crew": "#{id} — групповая задача: исправь её сам(а) или новыми направлениями (task_update "
                                "directions) и, когда они завершатся, обнови результат #{id}; затем снова вызови "
                                "`task_complete`.",
        "n_reviewer_left": "{reviewer}, ревьюер твоей задачи #{id} «{title}», покинул(а) рой, поэтому задача снова в "
                           "работе. Если проверка всё ещё нужна, назначь другого ревьюера через `task_update`, затем "
                           "снова вызови `task_complete`.",
        "n_review_cancelled": "Проверять задачу #{id} «{title}» больше не нужно: {by} изменил(а) её (провёл(а) ревью, "
                              "передал(а) его другому или забрал(а) задачу с ревью).",
        "n_review_reminder": "Твоего ревью всё ещё ждут задачи: {tasks}. Пока ты не решишь, их исполнители и зависящие "
                             "от них задачи стоят. Проверь их сейчас через `task_review` (сверь каждый результат с "
                             "целью: открой файлы, запусти код), раньше своей работы.",
        "n_review_reassigned": "Ты не проверил(а) задачу #{id} «{title}» вовремя, поэтому её ревью передано: "
                               "{reviewer}. Проверять её больше не нужно.",
        "n_task_closed": "Задача #{id} «{title}» (ты её создал) теперь в статусе {status} ({by}). Итог: {result}",
        "n_subtasks_done": "Все подзадачи #{id} «{title}» завершены (всего: {total}; {counts}): {ending}",
        "n_direction_failed": "#{id} «{title}», направление твоей групповой задачи #{parent}, провалено ({by}): верни "
                              "его в работу, чтобы его сделали заново (task_update #{id} status=todo), или отмени его, "
                              "если #{parent} обойдётся без него (status=cancelled).",
        "n_direction_cancelled": "#{id} «{title}», направление твоей групповой задачи #{parent}, отменено ({by}): если "
                                 "эта часть ещё нужна #{parent}, добавь её снова (task_update directions).",
        "n_subtask_cancelled": "{by} отменил(а) #{id} «{title}», твою часть #{parent}: прекрати работу над ней; если "
                               "сделанное тобой ещё нужно, скажи об этом в обсуждении #{parent}.",
        "n_direction_taken": "{by} взял(а) #{id} «{title}», направление твоей групповой задачи #{parent}.",
        "n_direction_freed": "#{id} «{title}», направление твоей групповой задачи, снова свободно: {by} покинул(а) "
                             "рой.",
        "n_crew_owner": "Задача #{id} «{title}» теперь твоя (групповая задача: одновременно до {crew} "
                        "участников).{instructions}{directions}",
        "n_crew_changed": "{by} задал(а) для #{id} «{title}» crew = {crew} (сколько участников работают над ней "
                          "одновременно; 0 — обычная задача).",
        "n_parent_closed": "#{id} «{title}», частью которой является твоя #{sub}, теперь в статусе {status}: "
                           "доделывай #{sub}, только если она ещё нужна сама по себе; иначе отмени её (task_update "
                           "status=cancelled).",
        "n_crew_review_requested": "#{id} «{title}» (отправил(а) {by}) — групповая задача, у которой выполнено "
                                   "направлений: {n}. Проверь её целиком: сходятся ли направления между собой и "
                                   "достигает ли результат её цели? Прими её или верни с тем, что не сходится "
                                   "(task_review).",
        "n_marks_stalled": "{text}",
        "crew_draft": "Черновик итога #{id}: её направления в порядке создания, с итогами выполненных:\n{items}",
        "crew_draft_cut": "(обрезано: больше покажет task_get #{id})",
        "crew_draft_more": "… ещё не показано направлений: {k}; task_get #{id} перечисляет их, а task_get направления "
                           "показывает его итог.",
        "crew_draft_no_result": "(итога нет)",
        "count_done": "выполнено: {n}",
        "count_cancelled": "отменено: {n}",
        "count_failed": "провалено: {n}",
        "ending_plain": "собери их итоги и заверши её.",
        "ending_plain_creator": "возьми #{id} (task_claim), собери их итоги и заверши её.",
        "ending_crew_failed": "верни проваленное направление #{failed} в работу, чтобы его сделали заново (task_update "
                              "#{failed} status=todo), отмени его, если #{id} обойдётся без него (status=cancelled), "
                              "или переведи #{id} в failed.",
        "ending_crew_failed_creator": "верни проваленное направление #{failed} в работу, чтобы его сделали заново "
                                      "(task_update #{failed} status=todo), отмени его, если #{id} обойдётся без него "
                                      "(status=cancelled), или отмени #{id}.",
        "ending_crew_cancelled": "добавь новые направления (task_update directions), сделай её сам (затем "
                                 "task_complete), преврати её в обычную задачу (crew 0) или отмени её.",
        "ending_crew_cancelled_creator": "добавь новые направления (task_update directions), возьми #{id} и сделай её "
                                         "сам (task_claim, затем task_complete), преврати её в обычную задачу (crew 0) "
                                         "или отмени её.",
        "ending_crew_finished": "проверь, что они сходятся между собой, напиши итог #{id} по их итогам (что дало "
                                "каждое направление и где это лежит, и что осталось за рамками) и заверши её "
                                "(task_complete).",
        "ending_crew_finished_creator": "возьми #{id} (task_claim), проверь, что они сходятся между собой, напиши итог "
                                        "#{id} по их итогам (что дало каждое направление и где это лежит, и что "
                                        "осталось за рамками) и заверши её (task_complete).",
        "owner_prefix_plain": "Все её подзадачи завершены: ",
        "owner_prefix_failed": "Одно из её направлений провалено: ",
        "owner_prefix_cancelled": "Все её направления отменены: ",
        "owner_prefix_finished": "Все её направления завершены: ",
        "crew_owner_take": _OWNER_HEAD["ru"] + "возьми одно из её направлений сам (task_claim; вместе с #{id} это "
                                               "считается одной задачей в работе)" + _OWNER_TAIL["ru"],
        "crew_owner_own": _OWNER_HEAD["ru"] + "твоё собственное направление — #{sub} [{status}]" + _OWNER_TAIL["ru"],
        "crew_owner_all_taken": _OWNER_HEAD["ru"] + "все её направления уже взяты: если от #{id} осталась часть, "
                                                    "которую никто не делает, добавь её как направление (task_update "
                                                    "directions), и оно будет твоим; иначе следи за согласованностью "
                                                    "направлений, пока они не завершатся, затем заверши "
                                                    "#{id}" + _OWNER_TAIL["ru"],
        "crew_owner_none": " Ты владелец групповой задачи #{id}. Направлений у неё пока нет: добавь их сейчас "
                           "(task_update directions; первое — твоя часть), чтобы могли подключиться остальные; если "
                           "делить #{id} всё же не нужно, преврати её в обычную задачу (crew 0) или сделай её сам и "
                           "заверши (task_complete).",
        "crew_directions": " Её направления: {items}.",
        "crew_directions_more": " Ещё не перечислено направлений: {k}; task_get #{id} перечисляет все.",
        "direction_owner_named": "владелец — {name}",
        "direction_owner_none": "владельца пока нет",
        "direction_owner_you": "ты владелец #{id}",
        "note_direction_card": " #{sub} — направление групповой задачи #{id} ({owner}): сначала напиши его карточку "
                               "через task_update — title, goal (что оно даёт и как это проверить), description (какие "
                               "файлы оно затрагивает), — затем делай работу. В обсуждение #{id} пиши только то, от "
                               "чего зависят другие направления (интерфейсы, общие файлы).",
        "note_reviewer_cleared": " Ты был ревьюером #{id}; раз ты теперь работаешь над одним из её направлений, "
                                 "проверять её целиком будешь не ты: это сделает другой участник, если проверка "
                                 "нужна.",
        "note_own_part": " #{sub} «{title}» — твоё собственное направление: напиши его карточку (task_update), а если "
                         "часть #{id} ты уже сделал, скажи в обсуждении #{id}, что сделано, чтобы никто это не "
                         "переделывал.",
        "note_own_part_blocked": " #{sub} «{title}» — твоё собственное направление; #{id} ждёт задач {blockers}: "
                                 "напиши его карточку сейчас (task_update), а когда #{id} перестанет быть "
                                 "заблокированной, возьми #{sub} (task_claim).",
        "note_still_open": " Твоя #{sub} ещё открыта: доделывай её, только если она ещё нужна сама по себе; иначе "
                           "отмени её (task_update status=cancelled).",
        "note_crew_say_why": " Её автору сообщили: объясни в обсуждении #{id}, почему ты изменил crew (пост там "
                             "разбудит автора).",
        "note_direction_closed": " #{parent} в статусе {status}, поэтому #{id} отменена, а не возвращена на доску: "
                                 "направления закрытой групповой задачи никто не берёт.",
        "note_dependents_blocked": " {deps} зависят от #{id}, которая теперь в статусе {status}: они останутся "
                                   "заблокированными, пока эту зависимость не уберут (task_update depends_on) или их "
                                   "не отменят.",
        "note_dependent_blocked": " {deps} зависит от #{id}, которая теперь в статусе {status}: она останется "
                                  "заблокированной, пока эту зависимость не уберут (task_update depends_on) или её не "
                                  "отменят.",
        "mark_subtasks_done": " #{id} «{title}»: {ending}",
        "mark_free_directions": " #{id} — групповая задача со свободными направлениями, которые никто не взял: "
                                "{directions}. Возьми одно сам (task_claim) или отмени те, без которых она обойдётся "
                                "(task_update status=cancelled).",
        "mark_direction_to_take": " #{sub} «{title}», твоё направление групповой задачи #{id}, теперь можно взять: "
                                  "возьми его (task_claim) и сделай или сними себя с него, если делать не будешь "
                                  "(task_update assignee='').",
        "n_discussion": "{by} написал(а) в обсуждении задачи #{id} «{title}» (пост #{post_id}{reply}): {text}",
        "n_mention": "{by} упомянул(а) тебя в обсуждении задачи #{id} «{title}» (пост #{post_id}): {text}\n"
                     "Если нужен твой ответ, ответь через `task_comment` (reply_to={post_id}).",
        "reply_suffix": ", ответ на #{reply_to}",
        "n_trigger": "Сработал триггер #{id}: {condition}. {note}",
        "n_trigger_files": "Сработал триггер #{id}: {condition}.{changes} {note}",
        "trigger_file_settled": " Файл {path} изменён (изменений: {count}{by}); последнее изменение — {quiet} назад.",
        "trigger_file_changing": " Файл {path} всё ещё меняется (изменений за {duration}: {count}{by}).",
        "trigger_file_by": "; кто менял: {names}",
        "n_trigger_held": "Триггер #{id} снова срабатывал после прошлого уведомления в {since} (срабатываний: {count}, "
                          "последнее в {last}): {condition}.{changes} Повторяющийся триггер будит не чаще раза в "
                          "{interval}. {note}",
        "n_trigger_held_off": "Триггер #{id} снова срабатывал после прошлого уведомления в {since} (срабатываний: "
                              "{count}, последнее в {last}): {condition}.{changes} {note}",
        "n_ready": "Есть свободные задачи, готовые к работе: {tasks}. Если у тебя нет задачи в работе, возьми ту, что "
                   "подходит тебе лучше всего (если взять не получилось, возьми другую). Иначе, или если ни одна из "
                   "них тебе не по силам, вызови `wait`.",
        "n_all_done": "Все задачи на доске завершены, но глобальная цель ещё открыта. Проверь результат по цели "
                      "(посмотри результаты работы, запусти их). Если чего-то не хватает, создай для этого задачи; "
                      "иначе вызови `goal_complete` с итогом для пользователя.",
        "n_all_done_wrap_up": "Все задачи на доске завершены, а пользователь попросил команду закругляться. Не "
                              "создавай новых задач: вызови `goal_complete` с итогом для пользователя (чего достигли, "
                              "где это лежит, что осталось не сделано).",
        "n_wrap_up": "Пользователь попросил команду закругляться: не начинай новых вычислений и фоновых процессов, "
                     "даже ещё одного быстрого прогона. Заверши свои задачи с тем, что есть сейчас: `task_complete` "
                     "с результатом на этот момент и тем, что осталось. Проведи ревью, которые ждут тебя. Останови "
                     "фоновые процессы, которые никому не нужны. Когда все задачи завершены, цель "
                     "закрывается (`goal_complete`) с итогом: чего достигли и что осталось, без новых задач на "
                     "остальное.",
        "n_wrap_up_end": "Режим завершения выключен: фоновые процессы снова разрешены; продолжай работу как обычно.",
        "n_network_off": "В этой сессии нет интернета (так настроил пользователь): твои команды, их фоновые процессы "
                         "и MCP-серверы не выходят в сеть; localhost работает. Установка пакетов из PyPI или npm, "
                         "git clone, fetch или push с удалённым репозиторием и загрузки не сработают: не трать на них "
                         "шаги и не пытайся это обойти. Работай с тем, что уже есть в workspace и установлено; если "
                         "нужного не хватает, скажи об этом в обсуждении задачи.",
        "n_network_on": "Пользователь снова включил интернет в этой сессии: новые команды опять выходят в сеть.",
        "n_restart": "Рой был перезапущен. Продолжай работу с того места, где остановился.",
        "n_step_limit": "Ты исчерпал лимит шагов одного пробуждения. Продолжай с того места, где остановился; если "
                        "ты повторялся без продвижения, смени подход или попроси помощи.",
        "n_stalled": "Твои задачи {tasks} в работе, но ты перестал ими заниматься. Продолжай сейчас. Если ты "
                     "заблокирован, переведи задачу в `waiting` и объясни причину в её обсуждении; если не можешь её "
                     "выполнить, верни её в `todo` (или переведи в `failed`) с комментарием.{marks}",
        "n_planning_hint": "Ты держишь задачу планирования #{id} уже {elapsed} (шагов: {steps}) и ещё не создал ни "
                           "одной задачи, а остальные участники ждут работы. Создай независимые задачи прямо сейчас "
                           "через `task_create`: исследования, эксперименты и проверки окружения, которые ты ещё "
                           "собирался сделать, станут задачами для остальных; читай только то, что нужно, чтобы "
                           "разбить работу. Затем продолжай план.",
        "n_clarify_hint": "Ты держишь задачу планирования #{id} уже {elapsed} (шагов: {steps}) и ещё ничего не "
                          "спросил у пользователя и не создал ни одной задачи, а остальные участники ждут. Если у "
                          "цели есть развилки, которые меняют план, отправь пользователю все вопросы одним "
                          "сообщением прямо сейчас (`send_message` с [\"user\"]), затем переведи задачу в `waiting` и "
                          "вызови `wait`; до ответа задач не создавай. Если спрашивать не о чем, планируй сейчас: "
                          "создай независимые задачи через `task_create`.",
        "n_mcp_approved": "Пользователь одобрил твой MCP-сервер «{name}», он готов к работе.",
        "n_mcp_rejected": "Пользователь отклонил твой MCP-сервер «{name}». {comment}",
        "n_resumed": "Работа роя возобновлена. Продолжай.",
        "n_user_answer": "Пользователь ответил на вопрос #{id}, который задал(а) {asker}; ради него рой ставили на "
                         "паузу.\nВопрос: {question}\nОтвет: {answer}\nЕсли ответ меняет то, что ты делаешь, поправь "
                         "свою работу и свои задачи; иначе продолжай.",
        "n_user_answer_seen": "Сообщение #{answer_id} от пользователя ниже — ответ на вопрос #{id}, который задал(а) "
                              "{asker}; ради него рой ставили на паузу.\nВопрос: {question}\nЕсли ответ меняет то, "
                              "что ты делаешь, поправь свою работу и свои задачи; иначе продолжай.",
        "n_question_unanswered": "Рой продолжает работу без ответа пользователя на твой вопрос #{id}. Не жди его: где "
                                 "ответ нужен, выбери вариант, который выбрал бы сам, запиши его как допущение там, "
                                 "где его прочтут остальные (CONTRACTS.md, описание или итог задачи), и продолжай. "
                                 "Если пользователь ответит позже, ответ придёт сообщением.",
        "n_clarify_off": "Пользователь больше не хочет уточнять цель до начала работы: не жди ответов на свои вопросы "
                         "о ней. Выбери варианты по умолчанию, запиши их как допущения (CONTRACTS.md, описания задач), "
                         "верни задачу планирования #{task_id} в `in_progress`, если она в `waiting`, и планируй "
                         "сейчас. Если пользователь ответит позже, ответ придёт сообщением.",
        "n_workspace_moved": "Пользователь перенёс общий workspace в папку {path}. Файлы и история git те же, "
                             "изменилась только папка.",
        "n_workspace_changed": "Пользователь переключил общий workspace на папку {path}. В ней другие файлы, чем "
                               "раньше (прежние остались в {old}); новые коммиты идут в текущую ветку её "
                               "git-репозитория. Прежде чем продолжать, посмотри её файлы (`list_files`): знакомых "
                               "тебе файлов может не быть или они другие.",
        "n_member_retired": "{name} покинул(а) рой ({reason}). Незавершённые задачи вернулись в todo: {tasks}.",
        "n_lock_free": "Снова свободно: {paths} (раньше тебе в этом отказали). Если ещё нужно, сначала перечитай: "
                       "скорее всего, файл изменился. Перед долгой правкой заблокируй его через `lock_file`: он может "
                       "понадобиться и другим.",
        "n_lock_wanted": "{path}, который ты держишь уже {held}, ждут: {waiters}. Если ещё правишь, сначала закончи "
                         "эту правку и сними блокировку, потом продолжай остальную работу по задаче. Если файл больше "
                         "не нужен, сними блокировку сейчас (`lock_file` с unlock=true). Отвечать не нужно: их "
                         "разбудят, когда файл освободится.",
        "lock_waiter_task": "{name} (задача {tasks})",
        "lock_waiter_direction": "#{id} {title} (направление #{parent})",
        "job_exit": "код возврата {code}",
        "job_gone": "все его процессы завершились",
        "job_no_output": "(вывода нет)",
        "n_job_done": "Твой фоновый процесс j{id} завершился ({result}), проработав {duration}. Команда: {command}\n"
                      "Последние строки его вывода:\n{tail}\nВесь вывод: {log}.",
        "n_job_command_done": "Команда твоего фонового процесса j{id} завершилась ({result}), проработав {duration}, "
                              "но запущенные ею программы ещё работают, поэтому j{id} продолжается: {names}. Команда: "
                              "{command}\nПоследние строки её вывода:\n{tail}\nВесь вывод: {log}.",
        "n_job_stopped": "Пользователь остановил твой фоновый процесс j{id}, проработавший {duration}. Команда: "
                         "{command}\nЕго вывод: {log}.",
        "n_job_limit": "Твой фоновый процесс j{id} остановлен: он проработал {duration}, а дольше нельзя. Команда: "
                       "{command}\nЕго вывод: {log}.\nЗапусти его снова, только если он ещё нужен.",
        "n_job_gone": "Твой фоновый процесс j{id} больше не работает: сессия была остановлена или перезапущена. "
                      "Команда: {command}\nЕго вывод до этого момента: {log}.\nЕсли он ещё нужен (например, сервер для "
                      "следующих шагов), запусти его снова.",
        "tools_added": "Вместе со сжатием контекста обновились твои инструменты. Новые: {tools}; как ими пользоваться, "
                       "сказано в их описаниях.",
        "skills_index": "Скиллы, подключённые к рою (скилл — файл SKILL.md с инструкциями для определённого вида "
                        "работы, иногда со скриптами; в следующих пробуждениях будут только их имена, а о "
                        "подключении, обновлении или отключении скилла тебе сообщат). Перед работой, к которой "
                        "подходит скилл, прочитай его SKILL.md через `read_file` и следуй ему: пути в нём указаны "
                        "относительно его папки, а его скрипты запускаются через `shell` по их пути от корня "
                        "workspace. Скилл, написанный для другого ассистента, может называть его инструменты: "
                        "пользуйся своими (`shell` вместо Bash, `read_file` вместо Read, `write_file` вместо Write, "
                        "`edit_file` вместо Edit); ${{CLAUDE_SKILL_DIR}} означает показанную здесь папку скилла."
                        " Если в строке скилла указан Python, запускай Python-скрипты этого скилла им: там "
                        "установлены его пакеты (это окружение общее с другими сессиями, поэтому ничего в него не "
                        "устанавливай). Если в строке сказано, что его пакеты Node подключены, его скрипты их "
                        "находят: не запускай npm install и npm ci в папке скилла. Если в строке ничего не сказано "
                        "о пакетах, скилл не перечисляет их в requirements.txt, pyproject.toml или package.json: "
                        "то, что требует его SKILL.md, установи сам через `shell`, лучше внутри своей черновой "
                        "папки (например, в виртуальное окружение там).\n{skills}",
        "skills_index_own": "Скиллы, подключённые к рою (скилл — файл SKILL.md с инструкциями для определённого вида "
                            "работы, иногда со скриптами; в следующих пробуждениях будут только их имена, а о "
                            "подключении, обновлении или отключении скилла тебе сообщат). Перед работой, к которой "
                            "подходит скилл, прочитай его SKILL.md через `read_file` и следуй ему: пути в нём "
                            "указаны относительно его папки, а его скрипты запускаются через `shell` по их пути от "
                            "корня workspace. Скилл, написанный для другого ассистента, может называть его "
                            "инструменты: пользуйся своими (`shell` вместо Bash, `read_file` вместо Read, "
                            "`write_file` вместо Write, `edit_file` вместо Edit); ${{CLAUDE_SKILL_DIR}} означает "
                            "показанную здесь папку скилла."
                            " Если в строке скилла указан Python, запускай Python-скрипты этого скилла им: там "
                            "установлены его пакеты (это окружение общее с другими сессиями, поэтому ничего в него не "
                            "устанавливай). Если в строке сказано, что его пакеты Node подключены, его скрипты их "
                            "находят: не запускай npm install и npm ci в папке скилла. Если в строке ничего не сказано "
                            "о пакетах, скилл не перечисляет их в requirements.txt, pyproject.toml или package.json: "
                            "то, что требует его SKILL.md, установи сам через `shell`, лучше внутри своей черновой "
                            "папки (например, в виртуальное окружение там). «Твоей роли» — скиллы твоей роли, они сделаны "
                            "для твоей работы.\n{skills}",
        "skills_item": "- {name}{own}: {description} → {path}{deps}",
        "skills_own": " (твоей роли)",
        "skills_manual": " (только по просьбе пользователя)",
        "skills_path_outside": "{path} (вне workspace: читай через `shell`)",
        "skills_names": "Подключённые к рою скиллы: {names}. Пути к их SKILL.md и описания — в списке, показанном тебе "
                        "раньше, и в твоих уведомлениях.",
        "skills_none": "Скиллы: к рою больше не подключено ни одного.",
        "n_skill_added": "К рою подключён скилл «{name}»: {description}\nПеред работой, к которой он подходит, "
                         "прочитай {path} через `read_file` и следуй ему.{deps}",
        "n_skill_updated": "Скилл «{name}» обновлён: {description}\nЕсли ты им пользуешься, перечитай {path} через "
                           "`read_file`.{deps}",
        "n_skill_removed": "Скилл «{name}» отключён от роя: больше не пользуйся им (его папки больше нет).",
        "n_skill_shared": "{owner} предложил(а) скилл «{name}», он подключён к рою: {description}\nПеред работой, к "
                          "которой он подходит, прочитай {path} через `read_file` и следуй ему.{deps}",
        "skills_deps_python": " Python: {python} (пакеты из {files} установлены).{extra}",
        "skills_deps_node": " Node: пакеты из {files} подключены в его папку.{extra}",
        "skills_deps_node_path": " Node: пакеты из {files} лежат в {path}; для require() укажи эту папку в NODE_PATH "
                                 "(import не читает NODE_PATH).",
        "skills_deps_bin": " Их команды ({names}) — в {bin}.",
        "skills_deps_own": " Собственный код скилла не установлен как пакет: запускай его скрипты по пути (для "
                           "src-раскладки укажи его папку src в PYTHONPATH).",
        "skills_deps_installing": " Пакеты из {files} устанавливаются; о готовности тебе сообщат.",
        "skills_deps_installing_quiet": " Пакеты из {files} устанавливаются; о готовности скажет одно из следующих "
                                        "твоих пробуждений (установка тебя не будит: пока займись другим).",
        "skills_deps_failed": " Пакеты из {files}: установить их не удалось ({error}; журнал: {log}, читай через "
                              "`shell`); нужное установи сам.",
        "skills_deps_manual": " Пакеты из {files} не устанавливаются автоматически ({reason}); нужное установи сам.",
        "deps_reason_off": "выключено в настройках",
        "deps_reason_proposals": "выключено для предложений участников и скиллов Claude Code из workspace, в которые "
                                 "могут писать агенты",
        "deps_reason_network": "в сессии нет сети, а тогда ничего не ставится для предложений участников и скиллов "
                               "Claude Code из workspace, в которые могут писать агенты",
        "deps_reason_unavailable": "на этой машине нет: {detail}",
        "deps_reason_python": "нужен Python {need}, а окружения используют {have}",
        "deps_reason_unsupported": "не поддерживается: {detail}",
        "n_skill_deps_ready": "Пакеты скилла «{name}» установлены.{deps}",
        "n_skill_deps_failed": "Установить пакеты скилла «{name}» не удалось.{deps}",
        "n_skill_saved": "Пользователь сохранил твой скилл «{name}» в библиотеку скиллов: им смогут пользоваться и в "
                         "будущих запусках.",
        "n_skill_not_saved": "Пользователь решил не сохранять твой скилл «{name}» в библиотеку; к этому рою он "
                             "остаётся подключённым.{comment}",
        "u_mcp_pending": "{owner} создал(а) MCP-сервер «{name}»: {description}. Одобрите или отклоните его (вкладка "
                         "MCP, `/approve {name} {digest}` или `mcp_approve` с дайджестом {digest}).",
        "u_skill_proposed": "{owner} предлагает скилл «{name}» (файлов: {files}): {description}. Рой уже пользуется "
                            "им.{installs} Сохранить ли его в библиотеку скиллов для будущих запусков — вкладка «Скиллы», "
                            "/skills approve {name} или `skill_approve`.",
        "u_skill_installs": " Его пакеты ({files}: {packages}) ставятся автоматически, в окружение отдельно от ваших "
                            "собственных скиллов.",
        "u_skill_deps_failed": "Не удалось установить пакеты скилла «{name}» ({files}): {error}. Журнал: {log}. "
                               "Переустановить можно на вкладке «Скиллы», командой /skills reinstall {name} или "
                               "через `skill_reinstall`.",
        "u_role_skills_missing": "Роль «{role}» указывает скиллы, которых нет в библиотеке скиллов: {names}. Добавьте "
                                 "их в библиотеку (вкладка «Скиллы») или уберите из роли.",
        "u_role_skills_over_cap": "Скиллы роли «{role}» не подключены, потому что у роя уже максимум — {max} скиллов: "
                                  "{names}. Отключите лишние или увеличьте max_attached в настройках скиллов.",
        "u_agent_error": "У агента {name} ошибка: {error}",
        "u_budget": "Бюджет исчерпан: потрачено ${spent:.2f} из ${limit:.2f}. Рой на паузе; увеличьте лимит, чтобы "
                    "продолжить.",
        "u_cost_unknown": "DreamTeam не может посчитать стоимость профиля модели «{profile}» (модель {model}): у него "
                          "не указаны {fields}, а провайдер не сообщает стоимость запроса, поэтому эти токены "
                          "считаются по $0. Показанные расходы ниже настоящих, и бюджет сессии может так и не "
                          "остановить рой. Укажите цены ($ за 1M токенов) в «Настройки» → «Модели и эндпоинты» (или "
                          "в dreamteam.toml) или 0, если модель бесплатная.",
        "u_question": "{name} задаёт вам вопрос (сообщение #{id}), и весь рой на паузе до вашего ответа. Ваше "
                      "следующее сообщение для {name} (или для всех) и будет ответом: его вместе с вопросом получат "
                      "все участники, затем рой продолжит работу. Чтобы продолжить без ответа, снимите рой с паузы "
                      "(«Продолжить» на доске, /pause в dreamteam tui, /resume в консоли, `swarm_control` "
                      "action=resume).",
        "u_question_left": "{name} покинул(а) рой, поэтому рой больше не ждёт вашего ответа на вопрос #{id} и "
                           "продолжает работу.",
        "u_goal_done": "{by} сообщает, что глобальная цель достигнута: {summary}",
        "u_refusal": "Агент {name}: модель отказалась выполнять запрос ({category}).",
        "u_member_joined": "{name} присоединился(ась) к рою в роли «{role}» (нанял(а) {by}).",
        "u_review_requested": "Задача #{id} «{title}» ждёт вашего ревью (отправил(а) {by}). Примите или верните её в "
                              "карточке задачи на доске или в терминале: /approve {id} [комментарий] или /reject {id} "
                              "<что исправить>.",
        "u_review_fallback": "{by} завершил(а) задачу #{id} «{title}», а проверить её сейчас больше некому (агент "
                             "никогда не принимает свою работу сам), поэтому она ждёт вашего ревью. Примите или "
                             "верните её в карточке задачи на доске или в терминале: /approve {id} [комментарий] или "
                             "/reject {id} <что исправить>. Если активных агентов два или больше, они проверяют друг "
                             "друга; независимое ревью можно выключить в настройках.",
        "u_review_handed_over": "Задача #{id} «{title}» теперь ждёт вашего ревью: {reviewer} не проверил(а) её, а "
                                "взять ревью на себя больше некому из агентов (агент никогда не принимает свою работу "
                                "сам). Примите или верните её в карточке задачи на доске или в терминале: /approve {id} "
                                "[комментарий] или /reject {id} <что исправить>.",
        "u_reviews_waiting": "Команде больше нечего делать: задачи {tasks} ждут вашего ревью. Примите или верните их "
                             "на доске или командами /approve N и /reject N <что исправить>.",
        "u_jobs_running": "Цель достигнута, но фоновые процессы агентов ещё работают: {jobs}. Они остановятся вместе с "
                          "сессией; чтобы остановить процесс раньше, откройте вкладку «Процессы» на доске или "
                          "выполните /jobs stop <id> в терминале.",
    },
}

# (title, goal, description)
PLANNING_TASK = {
    "en": ("Plan the work: independent tasks first, then contracts, then the rest",
           "CONTRACTS.md in the workspace fixes everything the tasks must agree on (not needed when the whole "
           "goal is one task), and the board holds a complete plan that achieves the global goal: tasks with "
           "goals a reviewer can check, each doable by one member, referencing the contracts, with depends_on "
           "only where a task really needs another one's result.",
           "Study the global goal (at the end of this description) and plan the work in this order:\n"
           "1. Look around. If the workspace already holds a project, learn its layout, conventions and how it "
           "is built and tested (`list_files`, `read_file`, `shell`) before planning: the plan must fit them. "
           "Read only as much as you need to split the work: research, experiments, reproducing a problem and "
           "checks of the environment (installing, running the tests, trying approaches) are separate tasks for "
           "the others, not your planning. The team waits for your tasks meanwhile.\n"
           "2. Independent work at once. Work that needs no contracts (literature and theory, the environment and "
           "tools, data, exploratory experiments) becomes tasks right away, before CONTRACTS.md is written, so "
           "that the members waiting for the plan start now. Only you create the tasks of the plan: the others "
           "take these ones meanwhile.\n"
           "3. Then the contracts. Before creating the other tasks, write the shared contracts once to "
           "CONTRACTS.md in the workspace root (if the project keeps its design documents in a folder such as "
           "docs/, put it there). "
           "Write down everything that more than one task must agree on: module boundaries and public interfaces "
           "(signatures, classes, commands and options), file and data formats with a short example, output "
           "texts, exit codes and error handling, the directory layout (which files each task creates), naming "
           "and code style, how to run the program and the tests. Keep it short and concrete. It is binding: "
           "whoever needs to change it says so in the discussion of this task. A small goal that is one "
           "deliverable for one member needs no contracts file: create that one task.\n"
           "4. Then the rest of the tasks. Tasks are deliverables, not steps: each one finishable and verifiable "
           "by one member. Create them without parent_id: they are not parts of this task. Give every "
           "task a goal (why it exists and what counts as success, so that a reviewer can check it) and a "
           "description with everything a member without your context needs: inputs, the files to create or "
           "change, the sections of CONTRACTS.md it implements or relies on (refer to them, don't copy them), "
           "how to check the result. Prefer a few meaningful tasks to many tiny ones. A deliverable too big for one "
           "member whose detailed split is unclear before the work starts becomes one crew task: crew = how many "
           "members may work on it at once, its owner included; directions = its areas, one per item; the members "
           "who take them write the details.\n"
           "5. No false dependencies. Use depends_on only when a task cannot start without another task's "
           "result. Once an interface is fixed in the contracts, its implementation, its tests and its "
           "documentation are separate tasks that run in parallel against the contract. A final task (report, "
           "summary, documentation, release) depends only on the tasks without whose results it cannot be "
           "written (on the tests task if its success criterion includes \"the tests pass\"); exploratory and "
           "optional tasks do not block it: it says what they found by then, or that they are still open.\n"
           "6. Tests by someone else. The tests of a module are a separate task from the module, written against "
           "the contract by another member: write \"not for the author of #N\" in its description.\n"
           "7. Reviews. Every finished task is reviewed by a member who did not do it; the board picks one. Set "
           "a reviewer only when a task needs a particular member's expertise.\n"
           "8. Team. Leave tasks unassigned: free members are woken to claim them. Hire a member only if there "
           "is more parallel work than the team can take; if you hire, pick a role from the role catalog "
           "(`agents` lists it).\n"
           "9. Finish quickly. Complete this task as soon as CONTRACTS.md and the tasks are in place; in the "
           "result, name the contracts file and list the tasks. Then claim one task that suits you, only one: "
           "don't take a chain of tasks while other members are waiting for work. A crew task you own and your own "
           "direction of it count as one task here, and the crew task alone as none while you work on none of its "
           "directions."),
    "ru": ("Спланировать работу: сначала независимые задачи, затем контракты, затем остальное",
           "В CONTRACTS.md в workspace зафиксировано всё, о чём задачи должны договориться (не нужно, если вся "
           "цель — одна задача), а на доске есть полный план, достигающий глобальной цели: задачи с целями, "
           "которые может проверить ревьюер, каждая по силам одному участнику, со ссылками на контракты и с "
           "depends_on только там, где задаче действительно нужен результат другой.",
           "Изучи глобальную цель (она в конце описания) и спланируй работу в таком порядке:\n"
           "1. Осмотрись. Если в workspace уже есть проект, до планирования разберись в его структуре, "
           "соглашениях и в том, как он собирается и тестируется (`list_files`, `read_file`, `shell`): план "
           "должен им соответствовать. Читай ровно столько, сколько нужно, чтобы разбить работу: исследования, "
           "эксперименты, воспроизведение проблемы и проверки окружения (установка, запуск тестов, проба "
           "подходов) — это отдельные задачи для остальных, а не часть планирования. Команда тем временем ждёт "
           "твоих задач.\n"
           "2. Независимая работа сразу. Работа, которой не нужны контракты (литература и теория, окружение и "
           "инструменты, данные, исследовательские эксперименты), сразу становится задачами, ещё до того, как "
           "написан CONTRACTS.md, чтобы участники, ждущие плана, начали работать уже сейчас. Задачи плана "
           "создаёшь только ты: остальные тем временем берут эти задачи.\n"
           "3. Затем контракты. Прежде чем создавать остальные задачи, один раз запиши общие контракты в "
           "CONTRACTS.md в корне workspace (если проект хранит проектные документы в папке вроде docs/, положи "
           "файл туда). "
           "Запиши всё, о чём должны договориться несколько задач: границы модулей и публичные интерфейсы "
           "(сигнатуры, классы, команды и опции), форматы файлов и данных с коротким примером, тексты вывода, "
           "коды выхода и обработку ошибок, структуру каталогов (какие файлы создаёт каждая задача), именование "
           "и стиль кода, как запускать программу и тесты. Коротко и конкретно. Контракты обязательны: кому "
           "нужно их изменить, тот пишет об этом в обсуждении этой задачи. Небольшой цели, которая сводится к "
           "одному результату для одного участника, файл контрактов не нужен: создай эту одну задачу.\n"
           "4. Затем остальные задачи. Задачи — это результаты, а не шаги: каждую один участник может сделать и "
           "проверить. Создавай их без parent_id: они не части этой задачи. Дай каждой "
           "задаче цель (зачем она нужна и что считается успехом — так, чтобы ревьюер мог это проверить) и "
           "описание со всем, что нужно участнику без твоего контекста: входные данные, какие файлы создать или "
           "изменить, какие разделы CONTRACTS.md задача реализует или использует (ссылайся на них, а не "
           "копируй), как проверить результат. Лучше несколько содержательных задач, чем много мелких. Результат, "
           "слишком большой для одного участника, подробное деление которого до начала работы неясно, становится "
           "одной групповой задачей: crew — сколько участников могут работать над ней одновременно, включая её "
           "владельца; directions — её направления, по одному на элемент; подробности напишут те, кто их возьмёт.\n"
           "5. Никаких ложных зависимостей. Используй depends_on, только если задачу нельзя начать без "
           "результата другой. Когда интерфейс зафиксирован в контрактах, его реализация, тесты к нему и "
           "документация — отдельные задачи, которые идут параллельно по контракту. Итоговая задача (отчёт, "
           "сводка, документация, релиз) зависит только от задач, без результатов которых её нельзя написать (от "
           "задачи с тестами, если в её критерий успеха входит «тесты проходят»); исследовательские и "
           "необязательные задачи её не блокируют: в ней сказано, что они нашли к этому времени или что они ещё "
           "открыты.\n"
           "6. Тесты пишет другой. Тесты модуля — отдельная от модуля задача; их пишет по контракту другой "
           "участник: укажи в её описании «не для автора #N».\n"
           "7. Ревью. Каждую завершённую задачу проверяет участник, который её не делал; его выбирает доска. "
           "Указывай ревьюера, только если задаче нужна экспертиза конкретного участника.\n"
           "8. Команда. Не назначай исполнителей: свободных участников разбудят, чтобы они взяли задачи. Нанимай "
           "участника, только если параллельной работы больше, чем может взять команда; если нанимаешь, выбирай "
           "роль из каталога ролей (его показывает `agents`).\n"
           "9. Заверши быстро. Заверши эту задачу, как только CONTRACTS.md и задачи готовы; в итоге назови файл "
           "контрактов и перечисли задачи. Затем возьми одну подходящую тебе задачу — только одну: не забирай "
           "себе цепочку задач, пока другие участники ждут работы. Групповая задача, которой ты владеешь, и твоё "
           "собственное направление в ней считаются здесь одной задачей, а одна групповая задача — ни одной, пока ты "
           "не работаешь ни над одним из её направлений."),
}

# The planning card of a session that clarifies the goal first ([swarm] clarify, docs/SPEC.md §3 "Clarify mode"):
# board data like PLANNING_TASK (never the system prompt or a tool schema). Its head replaces the first line of
# PLANNING_TASK's description, steps 1-9 follow verbatim; its goal starts with what the reviewer checks.
CLARIFY_TAG = "clarify"   # tag of the planning card of a session that clarifies the goal first (docs/SPEC.md §3)
_CLARIFY_TITLE = {
    "en": "Clarify the goal with the user, then plan: independent tasks first, then contracts, then the rest",
    "ru": "Уточнить цель у пользователя, затем спланировать: сначала независимые задачи, затем контракты, затем "
          "остальное",
}
_CLARIFY_GOAL = {  # followed by PLANNING_TASK[lang][1]
    "en": "Before any task was created, the user was asked in one message about the forks of the goal that change "
          "the plan (unless it had none), and this task's result says what was asked; the user's decisions, and the "
          "defaults taken where the user left a point open, are written down. ",
    "ru": "До создания первой задачи пользователю одним сообщением заданы вопросы о развилках цели, которые меняют "
          "план (если они были), и в итоге этой задачи сказано, о чём спросили; решения пользователя и варианты по "
          "умолчанию там, где он оставил вопрос открытым, записаны. ",
}
_CLARIFY_HEAD = {  # replaces the first line of PLANNING_TASK[lang][2]
    "en": "Study the global goal (at the end of this description). The user wants its open points settled before any "
          "work starts: ask first, then plan. This time, unlike the usual rule, do not continue with the work that "
          "doesn't depend on the answer: until the user answers, create no task, don't write CONTRACTS.md and start "
          "no research. The team waits for the plan and spends nothing meanwhile.\n"
          "- Find the forks: the unclear points where different readings of the goal lead to different results or "
          "plans (the scope and what counts as done, the platform and the stack, interfaces and formats others will "
          "rely on, the data, the quality bar, limits). Look at the workspace only as far as good questions need. "
          "What you can find out in the workspace or settle by a sensible convention is not a question for the "
          "user.\n"
          "- If there is no such fork, write nothing to the user and plan at once.\n"
          "- Otherwise send the user ONE message (`send_message` to [\"user\"]): a line on how you understand the "
          "goal, then the questions numbered (at most seven), each with its options and the one you would take by "
          "default, so that a few words answer it. Then set this task to `waiting` with `task_update` and call "
          "`wait`: the answer wakes you.\n"
          "- When the answer comes, set this task back to `in_progress` and plan with it. Where the user tells you "
          "to decide yourself, is not available or leaves a question open, take your default. Ask again only if an "
          "answer opens a new fork that really changes the plan.\n"
          "- Write the user's decisions, and your defaults marked as assumptions, into CONTRACTS.md (into this "
          "task's result when the whole goal is one task) and into the descriptions of the tasks they concern: the "
          "other members never see your messages with the user.\n"
          "- In this task's result, say what you asked the user, or that there was nothing to ask: its reviewer "
          "cannot see your messages either.\n"
          "Once the user has answered (or there was nothing to ask), plan the work in this order:\n",
    "ru": "Изучи глобальную цель (она в конце описания). Пользователь хочет решить её открытые вопросы до начала "
          "работы: сначала спроси, затем планируй. На этот раз, в отличие от обычного правила, не продолжай работу, "
          "которая не зависит от ответа: пока пользователь не ответил, не создавай задач, не пиши CONTRACTS.md и не "
          "начинай исследований. Команда ждёт плана и тем временем ничего не тратит.\n"
          "- Найди развилки: неясные места, где разное прочтение цели ведёт к разным результатам или планам (объём "
          "работы и что считать готовым, платформа и стек, интерфейсы и форматы, на которые будут опираться "
          "другие, данные, планка качества, ограничения). В workspace заглядывай ровно настолько, насколько это "
          "нужно для хороших вопросов. То, что можно выяснить в workspace или решить по разумному соглашению, — не "
          "вопрос для пользователя.\n"
          "- Если таких развилок нет, ничего не пиши пользователю и сразу планируй.\n"
          "- Иначе отправь пользователю ОДНО сообщение (`send_message` с [\"user\"]): строка о том, как ты понял "
          "цель, затем вопросы с номерами (не больше семи), у каждого — варианты и тот, который ты выберешь по "
          "умолчанию, чтобы на каждый можно было ответить в двух словах. Затем переведи эту задачу в `waiting` "
          "через `task_update` и вызови `wait`: ответ тебя разбудит.\n"
          "- Когда придёт ответ, верни эту задачу в `in_progress` и планируй с его учётом. Где пользователь просит "
          "принять решение самому («прими решение сам»), недоступен или оставил вопрос открытым, бери свой вариант "
          "по умолчанию. Спрашивай ещё раз, только если ответ открыл новую развилку, которая действительно меняет "
          "план.\n"
          "- Запиши решения пользователя и свои варианты по умолчанию (помеченные как допущения) в CONTRACTS.md (в "
          "итог этой задачи, если вся цель — одна задача) и в описания задач, которых они касаются: остальные "
          "участники твою переписку с пользователем не видят.\n"
          "- В итоге этой задачи напиши, о чём ты спросил пользователя, или что спрашивать было не о чем: ревьюер "
          "этой задачи твоих сообщений тоже не видит.\n"
          "Когда пользователь ответил (или спрашивать было не о чем), спланируй работу в таком порядке:\n",
}
PLANNING_CLARIFY = {lang: (_CLARIFY_TITLE[lang], _CLARIFY_GOAL[lang] + goal,
                           _CLARIFY_HEAD[lang] + description.split("\n", 1)[1])
                    for lang, (_title, goal, description) in PLANNING_TASK.items()}


def planning_task(language: str, clarify: bool = False) -> tuple[str, str, str]:
    """The planning card (title, goal, description): PLANNING_TASK itself, or its clarify variant."""
    lang = _lang(language)
    return PLANNING_CLARIFY[lang] if clarify else PLANNING_TASK[lang]


# the name of the role catalog's generalist preset (roles.ROLE_PRESETS), the role of a member nobody chose
DEFAULT_ROLE = {"en": "Generalist", "ru": "Универсал"}


def _lang(language: str | None) -> str:
    """A language that has texts: unknown ones fall back to English."""
    return language if language in TEXTS else "en"


def system_prompt(language: str, **kw: Any) -> str:
    """The frozen system prompt of a new member. `role_description` (the catalog's text of its role, already in the
    second person) and `instructions` may be empty; `scratch` is the member's own scratch folder
    (workspace.scratch_rel); `foreground_max` is [shell] foreground_max (the longest a `shell` call waits)."""
    language = _lang(language)
    template = SYSTEM_RU if language == "ru" else SYSTEM_EN
    instructions = kw.pop("instructions", "") or ""
    kw["instructions"] = TEXTS[language]["instructions"].format(text=instructions) if instructions else ""
    description = (kw.pop("role_description", "") or "").strip()
    kw["role_description"] = description + "\n" if description else ""
    kw.setdefault("scratch", ".scratch/")
    if "foreground_max" not in kw:  # callers that pass no setting: its default
        from .config import ShellSettings

        kw["foreground_max"] = ShellSettings.foreground_max
    return template.format(**kw)


def t(language: str, key: str, **kw: Any) -> str:
    return TEXTS[_lang(language)][key].format(**kw)
