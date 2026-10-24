# Agent traces

**English** · [Русский](../ru/traces.md) · [All docs](README.md)

Open the **"Traces"** tab on the session board, or press "Traces" in an agent's window.

* On the left is the list of agent wake-ups, with filters by agent and status (`running`, `done`, `error`, `step_limit`,
  `refusal`, `paused`, `stopped`, `interrupted`, "Errors only").
* On the right is the trace summary: steps, LLM calls, tools, errors, tokens (in/out/cache), cost and related tasks.
  Below it is a waterfall of the steps on a timeline.
* Click a step to expand it.
  * An LLM call shows the model, the stop reason, tokens, cost, thinking, the reply and the tool calls. The
    "Request context" button shows the system prompt, the tools and all the messages the model saw.
  * A tool shows its full arguments and result. If the model got a truncated result, this is marked.
* "Statistics" shows calls, errors and average time per tool and per model.
* The "Export JSON" and "JSON with contexts" buttons export the trace; "All agent traces (JSONL)" exports all of the
  agent's traces.

The same is available without the UI:

* Session REST (paths from `/s/<id>`): `/api/traces`, `/api/traces/<n>`, `/api/spans/<n>`, `/api/spans/<n>/context`,
  `/api/traces/stats`, `/api/traces/<n>/export`, `/api/agents/<name>/traces.jsonl`.
* Terminal: `dreamteam traces "Anna Smirnova" --status failed`.
* MCP: `agent_traces(agent=..., trace_id=..., span_id=..., context=true)`.

The settings are in the `[tracing]` section: `enabled`, `max_payload_chars` (the limit for one field) and
`retention_days` (after how many days old traces are deleted; `0` means keep forever).
