# Agent tools

**English** · [Русский](../ru/agent-tools.md) · [All docs](README.md)

The tool set is minimal: only working with the swarm and the console. Everything else agents write for
themselves as MCP servers.

* Board: `board`, `task_get`, `task_create`, `task_claim`, `task_update`, `task_complete`, `task_review`,
  `task_comment`, `task_discussion` ([crew tasks](features.md): `task_create` and `task_update` take `crew`, how many
  members may work on the task at once, its owner included (2 to 8, or 0 to make it an ordinary task again; in
  `task_update` the task's creator or assignee sets it), and `directions`, the titles of its directions: each one
  becomes a draft subtask whose taker writes its card. `task_update` adds directions; when the owner names them and
  has no direction of its own yet, the first one becomes its own. `task_get` of a task with subtasks lists
  them with the first line of their results and, for a crew task, who works on it and its free directions; `board`
  lists a subtask indented under its parent).
* Communication: `send_message` (in [clarify mode](features.md), `[swarm] clarify`, `pause_swarm=true` on a critical
  question to the user pauses the whole swarm until the user answers; the answer reaches every member),
  `read_messages`.
* Team: `agents`, `hire_agent`, `retire_agent`, `update_notes` (notes that survive context compaction),
  `wait`, `goal_complete`.
* Triggers: `trigger_create`, `trigger_list`, `trigger_delete`.
* Files and console: `read_file`, `write_file`, `edit_file`, `list_files`, `lock_file`, `shell` (with `background`,
  the command goes to the background at once).
* Background jobs: `jobs` (the list, and a job's state and output), `job_stop` (stop a job and everything it
  started).
* MCP: `mcp_create`, `mcp_list`, `mcp_tools`, `mcp_call`.
* Skills: `skill_propose` (offer the swarm a skill of its own; you decide whether it goes into the library,
  see [Skills](skills.md)).

The MCP server template that agents write (mcp 2.x):

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
