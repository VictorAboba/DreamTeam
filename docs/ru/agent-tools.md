# Инструменты агентов

[English](../en/agent-tools.md) · **Русский** · [Вся документация](README.md)

Набор инструментов минимален: только взаимодействие с роем и консоль. Всё остальное агенты пишут себе
сами в виде MCP-серверов.

* Доска: `board`, `task_get`, `task_create`, `task_claim`, `task_update`, `task_complete`, `task_review`,
  `task_comment`, `task_discussion` ([групповые задачи](features.md): `task_create` и `task_update` принимают `crew` —
  сколько участников могут работать над задачей одновременно, включая владельца, от 2 до 8, или 0, чтобы задача
  снова стала обычной (в `task_update` его меняет автор задачи или её исполнитель), — и `directions`, названия её
  направлений: каждое направление становится подзадачей-черновиком, карточку которой пишет взявший её участник.
  `task_update` добавляет направления; если их называет владелец задачи, у которого ещё нет своего направления, его
  часть — первое из них. `task_get` задачи с подзадачами перечисляет их с первой строкой итога, а у групповой
  задачи — ещё и кто над ней работает и какие направления свободны; `board` показывает подзадачу с отступом под
  родителем).
* Общение: `send_message` (в [режиме уточнения цели](features.md), `[swarm] clarify`, `pause_swarm=true` у важного
  вопроса пользователю ставит весь рой на паузу до ответа; ответ получают все участники), `read_messages`.
* Команда: `agents`, `hire_agent`, `retire_agent`, `update_notes` (блокнот, который переживает сжатие контекста),
  `wait`, `goal_complete`.
* Триггеры: `trigger_create`, `trigger_list`, `trigger_delete`.
* Файлы и консоль: `read_file`, `write_file`, `edit_file`, `list_files`, `lock_file`, `shell` (с `background` —
  команда сразу уходит в фон).
* Фоновые процессы: `jobs` (список, состояние и вывод процесса), `job_stop` (остановить процесс и всё, что он
  запустил).
* MCP: `mcp_create`, `mcp_list`, `mcp_tools`, `mcp_call`.
* Скиллы: `skill_propose` (предложить рою свой скилл; сохранить его в библиотеку решаете вы, см.
  [Скиллы](skills.md)).

Шаблон MCP-сервера, который пишут агенты (mcp 2.x):

```python
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("csv_tools")

@mcp.tool()
def count_rows(path: str) -> int:
    """Count data rows in a CSV file."""
    ...

if __name__ == "__main__":
    mcp.run()
```
