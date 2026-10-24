# Инструменты агентов

[English](../en/agent-tools.md) · **Русский** · [Вся документация](README.md)

Набор инструментов минимален: только взаимодействие с роем и консоль. Всё остальное агенты пишут себе
сами в виде MCP-серверов.

* Доска: `board`, `task_get`, `task_create`, `task_claim`, `task_update`, `task_complete`, `task_review`,
  `task_comment`, `task_discussion`.
* Общение: `send_message`, `read_messages`.
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
