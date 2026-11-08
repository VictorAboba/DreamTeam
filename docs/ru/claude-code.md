# Подключение к Claude Code

[English](../en/claude-code.md) · **Русский** · [Вся документация](README.md)

Если DreamTeam запущен, подключитесь по HTTP:

```bash
claude mcp add --transport http dreamteam http://127.0.0.1:8765/mcp
```

Без запущенного сервера Claude Code может сам поднять DreamTeam через stdio (нужна команда `dreamteam` в PATH,
см. [«Установка»](getting-started.md#установка)). Если сервер уже запущен, команда станет прокси к нему:

```bash
claude mcp add dreamteam -- dreamteam mcp
```

Хаб песочницы Docker требует токен (см. [Песочница Docker](sandbox.md#токен-доступа)): добавьте к HTTP-команде
заголовок, который печатает `dreamteam sandbox`, — `--header "Authorization: Bearer <токен>"`. Токен новый при каждом
запуске песочницы; stdio-команда находит его сама, для каждого запроса, поэтому продолжает работать и после
перезапуска песочницы (`dreamteam mcp --home <папка>` для домашней папки не по умолчанию).

Через `/mcp` доступны инструменты сессий: `sessions_list`, `session_create(title, goal, ..., workspace)`,
`session_start`, `session_stop` и `session_cd(session, path)` (перенос в другую папку, `path="default"` — в
собственную), а также `skill_library` (библиотека скиллов); `session_create` и `session_start` принимают
`skills=[...]`, `session_create(..., team_name=...)` запускает сессию с командой, сохранённой в настройках, а
`session_create(..., clarify=true)` включает [уточнение цели](features.md) (`false` выключает его поверх общей
настройки). Остальные инструменты работают внутри сессии, её задаёт параметр `session="<id>"`; по
умолчанию выбирается работающая сессия. Подключиться к одной сессии напрямую можно по адресу
`http://127.0.0.1:8765/s/<id>/mcp`.

По умолчанию Claude Code действует как пользователь роя: задаёт цель, смотрит доску, пишет агентам, одобряет MCP,
читает трейсы (`agent_traces`) и ждёт сообщений через `wait_for_events`. Когда вопрос агента ставит рой на паузу,
`sessions_list` показывает сессию как `paused (waiting for your answer to Anna Smith, question #12)`, и `swarm_status`
говорит то же самое: следующий `send_message` этому агенту (или всем) и есть ответ, его вместе с вопросом получат все
участники, и рой продолжит работу; `swarm_control(action="resume")` продолжает без ответа. Чтобы работать внутри
роя, нужно вызвать `join_swarm(role=...)` и затем передавать `as_agent="<имя>"`: так можно брать задачи, писать
файлы, выполнять команды и создавать MCP.
