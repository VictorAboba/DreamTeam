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

The hub of the Docker sandbox asks for a token (see [Docker sandbox](sandbox.md#access-token)): add the header that
`dreamteam sandbox` prints, `--header "Authorization: Bearer <token>"`, to the HTTP command. The token is new at every
start of the sandbox; the stdio command finds it itself, for every request, so it keeps working when the sandbox
restarts (`dreamteam mcp --home <home>` for a home outside the default one).

Through `/mcp` you get the session tools: `sessions_list`, `session_create(title, goal, ..., workspace)`,
`session_start`, `session_stop` and `session_cd(session, path)` (move to another folder, `path="default"` moves it
back to its own folder), and also `skill_library` (the skill library); `session_create` and `session_start` accept
`skills=[...]`, `session_create(..., team_name=...)` starts with a team saved in the settings, and
`session_create(..., clarify=true)` turns on [clarify mode](features.md) (`false` turns it off over the global
setting). The other tools work inside a session, which the `session="<id>"` parameter sets; by
default the running session is chosen. To connect to one session directly, use `http://127.0.0.1:8765/s/<id>/mcp`.

By default, Claude Code acts as the user of the swarm: it sets the goal, looks at the board, writes to the agents,
approves MCP servers, reads traces (`agent_traces`) and waits for messages with `wait_for_events`. When an agent's
question pauses the swarm, `sessions_list` shows the session as `paused (waiting for your answer to Anna Smith,
question #12)` and `swarm_status` says the same: the next `send_message` to that agent (or to all) is the answer,
every member gets it with the question, and the swarm goes on; `swarm_control(action="resume")` goes on without it.
To work inside the swarm, call `join_swarm(role=...)` and then pass `as_agent="<name>"`: this lets it take tasks,
write files, run commands and create MCP servers.
