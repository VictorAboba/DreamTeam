# Connecting to Claude Code

**English** · [Русский](../ru/claude-code.md) · [All docs](README.md)

If DreamTeam is running, connect over HTTP:

```bash
claude mcp add --transport http dreamteam http://127.0.0.1:8765/mcp
```

Without a running server, Claude Code can start DreamTeam itself over stdio (the `dreamteam` command must be in PATH,
see ["Install"](getting-started.md#install)). If the server is already running, the command becomes a proxy to it:

```bash
claude mcp add dreamteam -- dreamteam mcp
```

Through `/mcp` you get the session tools: `sessions_list`, `session_create(title, goal, ..., workspace)`,
`session_start`, `session_stop` and `session_cd(session, path)` (move to another folder, `path="default"` moves it
back to its own folder), and also `skill_library` (the skill library); `session_create` and `session_start` accept
`skills=[...]`. The other tools work inside a session, which the `session="<id>"` parameter sets; by default the
running session is chosen. To connect to one session directly, use `http://127.0.0.1:8765/s/<id>/mcp`.

By default, Claude Code acts as the user of the swarm: it sets the goal, looks at the board, writes to the agents,
approves MCP servers, reads traces (`agent_traces`) and waits for messages with `wait_for_events`. To work inside the
swarm, call `join_swarm(role=...)` and then pass `as_agent="<name>"`: this lets it take tasks, write files, run
commands and create MCP servers.
