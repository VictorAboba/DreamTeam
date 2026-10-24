# Agent tools

**English** · [Русский](../ru/agent-tools.md) · [All docs](README.md)

The tool set is minimal: only working with the swarm and the console. Everything else agents write for
themselves as MCP servers.

* Board: `board`, `task_get`, `task_create`, `task_claim`, `task_update`, `task_complete`, `task_review`,
  `task_comment`, `task_discussion`.
* Communication: `send_message`, `read_messages`.
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
