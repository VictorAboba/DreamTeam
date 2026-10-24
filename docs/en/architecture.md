# How it works

**English** · [Русский](../ru/architecture.md) · [All docs](README.md)

```
 Start page ─────────REST+WS─► Launcher (SessionManager, settings, /mcp)
                                  │  /s/<id>/…  — board, REST, WS and MCP of one session
 Web board / console ──REST+WS──┐ ▼                       ┌── LLM agents (ReAct, own asyncio task) ── Tracer
 Claude Code ───────────MCP─────┤   Session Swarm         ├── external members via MCP (join_swarm) ── Tracer
                                └─► Board · Mailbox · Workspace(git) · Triggers · MCPForge · Budget · Tracer
                                     └── session SQLite: tasks, discussions, messages, notifications, agent history,
                                         history archive, traces and spans, event log
```

* An agent sleeps while there are no events for it. It is woken by messages, assignments, posts in discussions,
  unblocked tasks and triggers. On wake-up the agent gets a summary: what happened, the swarm goal,
  its tasks, free tasks and its notes. Then the ReAct loop runs: LLM, tools, LLM… until the agent calls `wait`.
* The agent history is append-only: Claude's thinking blocks are tied to the exact prefix of the dialog, and
  the prompt cache works for the same reason. The system prompt is fixed at hiring, the agent's tool set is
  constant, and MCP tools are called through `mcp_call`. When the context overflows, the history is compacted into
  a summary. Only at that moment does an agent of an already running session get new DreamTeam tools (for
  example, `jobs` and `job_stop`): changing the set breaks nothing then. Until then, background jobs work for it
  too: a command goes to the background on timeout, and `kill j3` in the console stops the job.
* Every change is written to the event log. Live UI updates, the feed and trigger checks are built from it.
* The module contract is described in [docs/SPEC.md](../SPEC.md).

The ideas come from Claude Code Agent Teams (a shared task list, task claiming, mailboxes), Gas Town / Beads
(state in git, resuming after a crash), CAMEL Workforce and Magentic-One (task channels, replanning),
MetaGPT (event subscription), Strands Swarm (loop protection), Letta (notes that survive compaction),
Alita (agents create MCP servers themselves and put them in a library), A2A (a task lifecycle with waiting for input)
and blackboard architectures (agents volunteer for tasks themselves).
