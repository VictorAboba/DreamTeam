"""Agent traces (docs/SPEC.md §16).

Every activation (wake-up) of an LLM agent is one *trace*; its *spans* are what happened inside:
the context that woke the agent, each LLM call (with retries), each tool execution, compaction and
runtime events. Payloads are stored in full (capped by `tracing.max_payload_chars`), so a trace
keeps what the agent saw and did even after compaction replaced its history; the history replaced
by compaction is archived in `history_archive`, which lets `span_context()` rebuild the exact
message list of any LLM call.

External members (e.g. Claude Code connected through the hub MCP) get traces too: their tool calls
are grouped into session traces that close after `EXTERNAL_IDLE_SECONDS` without calls.
"""

from __future__ import annotations

import json
import math
import time
from typing import TYPE_CHECKING, Any, Iterable

from .db import Database, dumps, loads, utf8_safe

if TYPE_CHECKING:
    from .events import EventBus

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT, kind TEXT DEFAULT 'llm', started_at REAL, ended_at REAL,
    status TEXT, outcome TEXT DEFAULT '', trigger TEXT DEFAULT '', from_user INTEGER DEFAULT 0,
    steps INTEGER DEFAULT 0, llm_calls INTEGER DEFAULT 0, tool_calls INTEGER DEFAULT 0, errors INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0, tasks TEXT DEFAULT '[]', model TEXT);
CREATE INDEX IF NOT EXISTS traces_agent ON traces(agent, id);
CREATE INDEX IF NOT EXISTS traces_status ON traces(status);
CREATE TABLE IF NOT EXISTS spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trace_id INTEGER, agent TEXT, parent_id INTEGER, step INTEGER,
    kind TEXT, name TEXT, started_at REAL, ended_at REAL, duration_ms INTEGER, status TEXT,
    input TEXT, output TEXT, meta TEXT DEFAULT '{}', usage TEXT, cost_usd REAL DEFAULT 0, error TEXT);
CREATE INDEX IF NOT EXISTS spans_trace ON spans(trace_id, id);
-- covers stats(): the payload columns precede usage/cost_usd, so a table scan would read every payload
CREATE INDEX IF NOT EXISTS spans_stats ON spans(kind, name, agent, started_at, status, duration_ms, cost_usd, usage);
CREATE TABLE IF NOT EXISTS history_archive (
    agent TEXT, epoch INTEGER, seq INTEGER, msg TEXT, archived_at REAL, PRIMARY KEY (agent, epoch, seq));
"""
# history_archive row with seq = -1 and no msg: marks an archived epoch (also one whose messages were not
# kept), so the epoch counter, derived from MAX(epoch) after a restart, never goes back.
EPOCH_MARK = -1

# Trace statuses. `running` is the only open one; `interrupted` marks traces of a crashed process.
RUNNING = "running"
TRACE_STATUSES = ("running", "done", "error", "step_limit", "refusal", "paused", "stopped", "interrupted")
# Span kinds: input = text added to the context (wake-up, incoming, nudge, continuation);
# llm = one LLM request; tool = one tool execution; compaction = history summarised;
# event = a runtime decision (auto reply to the user, budget pause, step limit...).
SPAN_KINDS = ("input", "llm", "tool", "compaction", "event")
TASK_ARG_KEYS = ("task_id", "parent_id")
EXTERNAL_IDLE_SECONDS = 15 * 60


def clip(value: Any, limit: int) -> Any:
    """`value` with every string longer than `limit` cut in the middle (limit <= 0: unchanged)."""
    if limit <= 0:
        return value
    if isinstance(value, str):
        if len(value) <= limit:
            return value
        head = limit * 2 // 3
        tail = limit - head
        return f"{value[:head]}\n…[{len(value) - limit} chars cut]…\n{value[-tail:] if tail else ''}"
    if isinstance(value, dict):
        return {k: clip(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clip(v, limit) for v in value]
    return value


def _payload(value: Any, limit: int) -> Any:
    """`value` ready to store: strings clipped (limit <= 0: kept whole), NaN / Infinity (json.loads accepts
    them in model tool arguments) as strings, because the HTTP JSON encoder rejects them."""
    if isinstance(value, str):
        return clip(value, limit)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {k: _payload(v, limit) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_payload(v, limit) for v in value]
    return value


def _text(value: Any, limit: int = 0) -> str:
    """A plain TEXT column value: sqlite3 cannot store an unpaired surrogate."""
    return utf8_safe(clip(str(value or ""), limit))


def task_ids(args: Any) -> list[int]:
    """Task ids referenced by tool arguments (task_id, parent_id, depends_on)."""
    found: list[int] = []
    if not isinstance(args, dict):
        return found
    values: list[Any] = [args.get(k) for k in TASK_ARG_KEYS]
    deps = args.get("depends_on")
    if isinstance(deps, list):
        values += deps
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in found:
            found.append(number)
    return found


def _usage_dict(usage: Any) -> dict[str, Any]:
    if usage is None:
        return {}
    if isinstance(usage, dict):
        return dict(usage)
    return {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(usage, "cache_read_tokens", 0) or 0),
        "cache_write_tokens": int(getattr(usage, "cache_write_tokens", 0) or 0),
    }


class Span:
    """An open span; finish it with `Trace.end()`."""

    __slots__ = ("id", "kind", "name", "step", "started_at", "parent_id")

    def __init__(self, span_id: int, kind: str, name: str, step: int, started_at: float, parent_id: int | None):
        self.id = span_id
        self.kind = kind
        self.name = name
        self.step = step
        self.started_at = started_at
        self.parent_id = parent_id


class Trace:
    """Writer for one trace (one activation of one agent). All methods are no-ops when tracing is off."""

    def __init__(self, tracer: "Tracer", trace_id: int | None, agent: str, tasks: Iterable[int] = ()):
        self.tracer = tracer
        self.id = trace_id
        self.agent = agent
        self.step = 0
        self.finished = trace_id is None
        self._tasks: list[int] = list(tasks)  # already stored (a reopened external session trace)

    @property
    def enabled(self) -> bool:
        return self.id is not None

    # ---- spans ------------------------------------------------------------------------------
    def start(self, kind: str, name: str, input: Any = None, *, parent: Span | None = None,
              meta: dict[str, Any] | None = None) -> Span | None:
        if not self.enabled:
            return None
        now = time.time()
        name = _text(name, 200)  # a tool name comes from the model as is
        span_id = self.tracer.db.insert(
            "INSERT INTO spans(trace_id, agent, parent_id, step, kind, name, started_at, status, input, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.id, self.agent, parent.id if parent else None, self.step, kind, name, now, RUNNING,
             self.tracer._dump(input), self.tracer._dump(meta or {})),
        )
        return Span(span_id, kind, name, self.step, now, parent.id if parent else None)

    def end(self, span: Span | None, output: Any = None, *, status: str = "ok", error: str | None = None,
            usage: Any = None, cost: float = 0.0, meta: dict[str, Any] | None = None,
            model: str | None = None) -> None:
        if span is None or not self.enabled:
            return
        now = time.time()
        duration = int(round((now - span.started_at) * 1000))
        usage_d = _usage_dict(usage)
        db = self.tracer.db
        model = _text(model, 200) or None
        sets = "ended_at = ?, duration_ms = ?, status = ?, output = ?, usage = ?, cost_usd = ?, error = ?"
        params: list[Any] = [now, duration, status, self.tracer._dump(output), dumps(usage_d) if usage_d else None,
                             float(cost or 0.0), None if error is None else _text(error, self.tracer.max_payload_chars)]
        if meta:
            sets += ", meta = json_patch(COALESCE(meta, '{}'), ?)"
            params.append(self.tracer._dump(meta))
        if model:
            sets += ", name = ?"
            params.append(model)
        db.execute(f"UPDATE spans SET {sets} WHERE id = ?", (*params, span.id))
        # roll the span up into the trace counters
        counters = ["steps = MAX(steps, ?)"]
        values: list[Any] = [span.step]
        if span.kind == "llm":
            counters.append("llm_calls = llm_calls + 1")
            if usage_d:
                counters += ["input_tokens = input_tokens + ?", "output_tokens = output_tokens + ?",
                             "cache_read_tokens = cache_read_tokens + ?", "cache_write_tokens = cache_write_tokens + ?"]
                values += [usage_d.get("input_tokens", 0), usage_d.get("output_tokens", 0),
                           usage_d.get("cache_read_tokens", 0), usage_d.get("cache_write_tokens", 0)]
            if model:
                counters.append("model = ?")
                values.append(model)
        elif span.kind == "tool":
            counters.append("tool_calls = tool_calls + 1")
        if cost:
            counters.append("cost_usd = cost_usd + ?")
            values.append(float(cost))
        if status == "error":
            counters.append("errors = errors + 1")
        db.execute(f"UPDATE traces SET {', '.join(counters)} WHERE id = ?", (*values, self.id))
        self.tracer._publish("trace.span", self.agent, trace=self.tracer.trace_summary(self.id),
                             span={"id": span.id, "kind": span.kind, "name": model or span.name, "step": span.step,
                                   "status": status, "duration_ms": duration, "cost_usd": float(cost or 0.0),
                                   "usage": usage_d, "parent_id": span.parent_id})

    def record(self, kind: str, name: str, input: Any = None, output: Any = None, *, status: str = "ok",
               error: str | None = None, meta: dict[str, Any] | None = None, parent: Span | None = None) -> None:
        """A span that starts and ends at once (inputs, runtime events)."""
        self.end(self.start(kind, name, input, parent=parent, meta=meta), output, status=status, error=error)

    def note_tasks(self, ids: Iterable[int]) -> None:
        if not self.enabled:
            return
        new = [i for i in ids if i not in self._tasks]
        if new:
            self._tasks += new
            self.tracer.db.execute("UPDATE traces SET tasks = ? WHERE id = ?", (dumps(self._tasks), self.id))

    # ---- convenience wrappers used by the runtime ---------------------------------------------
    def llm_start(self, messages: int, *, purpose: str = "step", extra: list[dict] | None = None,
                  attempt: int = 1, parent: Span | None = None) -> Span | None:
        if not self.enabled:
            return None
        epoch = self.tracer.current_epoch(self.agent)
        return self.start("llm", purpose, {"epoch": epoch, "upto": messages - len(extra or []),
                                           "messages": messages, "extra": extra or []},
                          parent=parent, meta={"purpose": purpose, "attempt": attempt})

    def llm_end(self, span: Span | None, response: Any, cost: float) -> None:
        output = {
            "stop_reason": getattr(response, "stop_reason", None),
            "thinking": getattr(response, "thinking", "") or "",
            "text": getattr(response, "text", "") or "",
            "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments,
                            **({"parse_error": c.parse_error} if getattr(c, "parse_error", None) else {})}
                           for c in getattr(response, "tool_calls", []) or []],
        }
        status = "error" if output["stop_reason"] == "refusal" else "ok"
        self.end(span, output, status=status, usage=getattr(response, "usage", None), cost=cost,
                 model=getattr(response, "model", None) or None,
                 meta={"stop_reason": output["stop_reason"]})

    def llm_error(self, span: Span | None, error: str, *, kind: str = "other", retry_in: float | None = None,
                  model: str | None = None, status: str = "error") -> None:
        """A failed attempt. `model`: the configured model id (the span is named after it, so stats group
        failed calls by model too); status "interrupted" for a cancellation, which is not an error."""
        meta: dict[str, Any] = {"error_kind": kind}
        if retry_in is not None:
            meta["retry_in"] = retry_in
        self.end(span, None, status=status, error=error, meta=meta, model=model)

    def tool_start(self, call_id: str, name: str, arguments: Any) -> Span | None:
        self.note_tasks(task_ids(arguments))
        return self.start("tool", name, arguments, meta={"call_id": call_id})

    def tool_end(self, span: Span | None, result: str, is_error: bool, shown_chars: int | None = None) -> None:
        meta: dict[str, Any] = {"chars": len(result or "")}
        if shown_chars is not None and shown_chars < len(result or ""):
            meta["shown_chars"] = shown_chars  # the model saw a shortened result
        self.end(span, result, status="error" if is_error else "ok", meta=meta)

    def finish(self, status: str = "done", outcome: str = "") -> None:
        if self.finished:
            return
        self.finished = True
        self.tracer.finish(self.id, status, outcome)


class Tracer:
    """Owns the trace tables of one swarm: writing (via `Trace`) and reading."""

    def __init__(self, db: Database, bus: "EventBus | None" = None, *, enabled: bool = True,
                 max_payload_chars: int = 200_000):
        self.db = db
        self.bus = bus
        self.enabled = enabled
        self.max_payload_chars = max_payload_chars
        self._epochs: dict[str, int] = {}
        with db._lock:
            db.conn.executescript(SCHEMA)

    # ---- writing ----------------------------------------------------------------------------
    def _dump(self, value: Any) -> str | None:
        if value is None:
            return None
        return dumps(_payload(value, self.max_payload_chars))

    def _publish(self, type: str, actor: str | None, **data: Any) -> None:
        if self.bus is not None:
            self.bus.publish(type, actor, **data)

    def begin(self, agent: str, trigger: str = "", *, kind: str = "llm", from_user: bool = False,
              started_at: float | None = None) -> Trace:
        """Open a trace for one activation; returns a disabled writer when tracing is off."""
        if not self.enabled:
            return Trace(self, None, agent)
        trace_id = self.db.insert(
            "INSERT INTO traces(agent, kind, started_at, status, trigger, from_user) VALUES (?, ?, ?, ?, ?, ?)",
            (agent, kind, started_at or time.time(), RUNNING, _text(trigger, self.max_payload_chars), int(from_user)),
        )
        self._publish("trace.started", agent, trace=self.trace_summary(trace_id))
        return Trace(self, trace_id, agent)

    def finish(self, trace_id: int | None, status: str = "done", outcome: str = "", *,
               ended_at: float | None = None) -> None:
        if trace_id is None:
            return
        now = time.time()
        self.db.execute("UPDATE spans SET status = 'interrupted', ended_at = ?, "
                        "duration_ms = CAST((? - started_at) * 1000 AS INTEGER) "
                        "WHERE trace_id = ? AND status = ?", (now, now, trace_id, RUNNING))
        self.db.execute("UPDATE traces SET status = ?, outcome = ?, ended_at = ? WHERE id = ?",
                        (status if status in TRACE_STATUSES else "done", _text(outcome)[:2000], ended_at or now,
                         trace_id))
        summary = self.trace_summary(trace_id)
        if summary:
            self._publish("trace.finished", summary["agent"], trace=summary)

    def recover(self) -> int:
        """Close traces left open by a previous process (call once at swarm start). Returns their count.
        An external member's session trace is not a failed activation: it is closed as done."""
        rows = self.db.all("SELECT id, kind FROM traces WHERE status = ?", (RUNNING,))
        for row in rows:
            last = self._last_activity(row["id"])
            if row["kind"] == "external":
                self.finish(row["id"], "done", "Session closed when the swarm stopped.", ended_at=last)
                continue
            self.db.execute("UPDATE spans SET status = 'interrupted' WHERE trace_id = ? AND status = ?",
                            (row["id"], RUNNING))
            self.db.execute("UPDATE traces SET status = 'interrupted', ended_at = COALESCE(?, started_at), "
                            "outcome = 'The swarm process stopped during this activation.' WHERE id = ?",
                            (last, row["id"]))
        return len(rows)

    def _last_activity(self, trace_id: int) -> float | None:
        return self.db.scalar("SELECT MAX(COALESCE(ended_at, started_at)) FROM spans WHERE trace_id = ?", (trace_id,))

    def external_call(self, agent: str, tool: str, arguments: Any, result: str, is_error: bool,
                      started_at: float) -> None:
        """Record a tool call of an external member (hub MCP) into its current session trace."""
        if not self.enabled:
            return
        row = self.db.one("SELECT id, tasks, started_at, (SELECT MAX(COALESCE(s.ended_at, s.started_at)) FROM spans s "
                          "WHERE s.trace_id = t.id) AS last FROM traces t "
                          "WHERE agent = ? AND kind = 'external' AND status = ? ORDER BY id DESC LIMIT 1",
                          (agent, RUNNING))
        if row and (row["last"] or row["started_at"] or 0) < started_at - EXTERNAL_IDLE_SECONDS:
            self.finish(row["id"], "done", "Session closed after inactivity.", ended_at=row["last"])
            row = None
        if row:
            trace = Trace(self, row["id"], agent, loads(row["tasks"], []) or [])
            if started_at < (row["started_at"] or started_at):  # concurrent calls end in any order
                self.db.execute("UPDATE traces SET started_at = ? WHERE id = ?", (started_at, row["id"]))
        else:  # the session starts with this call, which already happened
            trace = self.begin(agent, "", kind="external", started_at=started_at)
        if not trace.enabled:
            return
        trace.step = int(self.db.scalar("SELECT COUNT(*) FROM spans WHERE trace_id = ?", (trace.id,)) or 0) + 1
        span = trace.tool_start("", tool, arguments)
        if span is not None:  # the call already happened: back-date the span
            span.started_at = started_at
            self.db.execute("UPDATE spans SET started_at = ? WHERE id = ?", (started_at, span.id))
        trace.tool_end(span, result, is_error)

    def close_external(self, idle_seconds: float | None = None) -> int:
        """Close open external session traces: all of them (swarm stop), or with `idle_seconds` only those
        without calls for that long (the scheduler). Returns how many were closed."""
        closed = 0
        now = time.time()
        for row in self.db.all("SELECT id, started_at FROM traces WHERE kind = 'external' AND status = ?", (RUNNING,)):
            last = self._last_activity(row["id"]) or row["started_at"]
            if idle_seconds is None:
                self.finish(row["id"], "done", "Session closed when the swarm stopped.", ended_at=last)
            elif (last or 0) < now - idle_seconds:
                self.finish(row["id"], "done", "Session closed after inactivity.", ended_at=last)
            else:
                continue
            closed += 1
        return closed

    # ---- history epochs (compaction archive) --------------------------------------------------
    def current_epoch(self, agent: str) -> int:
        if agent not in self._epochs:
            self._epochs[agent] = self._stored_epoch(agent)
        return self._epochs[agent]

    def _stored_epoch(self, agent: str) -> int:
        """The epoch of the agent's live history: one past the last archived epoch."""
        last = self.db.scalar("SELECT MAX(epoch) FROM history_archive WHERE agent = ?", (agent,))
        return 0 if last is None else int(last) + 1

    def archive_history(self, conn: Any, agent: str) -> int:
        """Copy the agent's current history into the archive (inside the caller's transaction, before
        the history is replaced). Returns the archived epoch number.

        With tracing off the messages are kept only while the agent's newest trace may hold LLM calls of
        this epoch (tracing was turned off since); the epoch is always marked, so its number never repeats."""
        last = conn.execute("SELECT MAX(epoch) FROM history_archive WHERE agent = ?", (agent,)).fetchone()[0]
        epoch = 0 if last is None else int(last) + 1  # from the table: never overwrites an archived epoch
        now = time.time()
        conn.execute("INSERT OR REPLACE INTO history_archive(agent, epoch, seq, msg, archived_at) VALUES "
                     "(?, ?, ?, NULL, ?)", (agent, epoch, EPOCH_MARK, now))
        if self.enabled or self._epoch_traced(conn, agent, epoch):
            conn.execute("INSERT OR REPLACE INTO history_archive(agent, epoch, seq, msg, archived_at) "
                         "SELECT agent, ?, seq, msg, ? FROM history WHERE agent = ?", (epoch, now, agent))
        # derived again from the table: a rolled-back transaction must not leave a skipped epoch behind
        self._epochs.pop(agent, None)
        return epoch

    @staticmethod
    def _epoch_traced(conn: Any, agent: str, epoch: int) -> bool:
        began = conn.execute("SELECT MAX(archived_at) FROM history_archive WHERE agent = ? AND epoch < ?",
                             (agent, epoch)).fetchone()[0] or 0
        last = conn.execute("SELECT status, ended_at FROM traces WHERE agent = ? AND kind = 'llm' "
                            "ORDER BY id DESC LIMIT 1", (agent,)).fetchone()
        return last is not None and (last[0] == RUNNING or (last[1] or 0) >= began)

    # ---- reading ----------------------------------------------------------------------------
    @staticmethod
    def _trace_row(row: Any) -> dict[str, Any]:
        data = dict(row)
        data["tasks"] = loads(data.get("tasks"), [])
        data["from_user"] = bool(data.get("from_user"))
        end = data.get("ended_at") or (time.time() if data.get("status") == RUNNING else data.get("started_at"))
        data["duration_ms"] = int(round(((end or 0) - (data.get("started_at") or 0)) * 1000))
        return data

    def trace_summary(self, trace_id: int) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM traces WHERE id = ?", (trace_id,))
        if row is None:
            return None
        data = self._trace_row(row)
        data["trigger"] = (data.get("trigger") or "")[:300]
        return data

    def list_traces(self, agent: str | None = None, *, limit: int = 50, before: int | None = None,
                    status: str | None = None, task: int | None = None, kind: str | None = None,
                    trigger_chars: int = 300) -> list[dict[str, Any]]:
        sql = "SELECT * FROM traces WHERE 1=1"
        params: list[Any] = []
        if agent:
            sql += " AND agent = ?"
            params.append(agent)
        if before:
            sql += " AND id < ?"
            params.append(before)
        if status:
            if status == "failed":
                sql += " AND status IN ('error', 'step_limit', 'refusal', 'interrupted')"
            else:
                sql += " AND status = ?"
                params.append(status)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if task:
            sql += " AND EXISTS (SELECT 1 FROM json_each(traces.tasks) WHERE json_each.value = ?)"
            params.append(int(task))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        result = []
        for row in self.db.all(sql, params):
            data = self._trace_row(row)
            data["trigger"] = (data.get("trigger") or "")[:trigger_chars]
            result.append(data)
        return result

    @staticmethod
    def _span_row(row: Any, payload_chars: int = 0) -> dict[str, Any]:
        data = dict(row)
        for key in ("input", "output", "usage"):
            data[key] = loads(data.get(key))
        data["meta"] = loads(data.get("meta"), {}) or {}
        data["input"] = _payload(data["input"], payload_chars)
        data["output"] = _payload(data["output"], payload_chars)
        return data

    def get_trace(self, trace_id: int, *, payload_chars: int = 0) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM traces WHERE id = ?", (trace_id,))
        if row is None:
            return None
        data = self._trace_row(row)
        data["spans"] = [self._span_row(r, payload_chars)
                         for r in self.db.all("SELECT * FROM spans WHERE trace_id = ? ORDER BY id", (trace_id,))]
        return data

    def get_span(self, span_id: int, *, payload_chars: int = 0) -> dict[str, Any] | None:
        row = self.db.one("SELECT * FROM spans WHERE id = ?", (span_id,))
        return None if row is None else self._span_row(row, payload_chars)

    def span_context(self, span_id: int, tools: list[dict] | None = None, *,
                     _span: dict[str, Any] | None = None, _cache: dict | None = None) -> dict[str, Any] | None:
        """The request of an LLM span as the model received it: system prompt, tools and messages
        (neutral DreamTeam format; the provider adapter converts it). `tools` defaults to the agent's
        tool schemas. (`_span` / `_cache`: export() passes the loaded span and shares one read per epoch.)"""
        span = _span if _span is not None else self.get_span(span_id)
        if span is None or span["kind"] != "llm":
            return None
        req = span.get("input") or {}
        agent = span["agent"]
        epoch, upto = int(req.get("epoch") or 0), int(req.get("upto") or 0)
        cache = {} if _cache is None else _cache
        key = (agent, epoch)
        if key not in cache:
            bound = "" if _cache is not None else " AND seq < ?"  # export: the whole epoch once, sliced below
            # The live history is the one after the last archived epoch. Read from the table, not from the
            # writer's cache: another process (the CLI) may read while the swarm writes.
            if epoch >= self._stored_epoch(agent):
                sql, params = f"SELECT seq, msg FROM history WHERE agent = ?{bound} ORDER BY seq", [agent]
            else:
                sql = (f"SELECT seq, msg FROM history_archive WHERE agent = ? AND epoch = ? AND seq >= 0{bound} "
                       "ORDER BY seq")
                params = [agent, epoch]
            if bound:
                params.append(upto)
            cache[key] = [(r["seq"], loads(r["msg"])) for r in self.db.all(sql, params)]
        stored = [msg for seq, msg in cache[key] if seq < upto]
        # the history keeps model output verbatim, NaN included: shown as a string so the context stays JSON
        messages = _payload(stored + list(req.get("extra") or []), 0)
        member_key = ("member", agent)
        if member_key not in cache:
            cache[member_key] = self.db.one("SELECT system_prompt, language FROM agents WHERE name = ?", (agent,))
        member = cache[member_key]
        if tools is None:
            from .tools import schemas_for  # local import: tools import the swarm stack
            tools = schemas_for("llm", (member["language"] if member else None) or "en")
        complete = len(stored) == upto
        return {
            "span_id": span["id"], "agent": agent, "epoch": epoch, "model": span.get("name"),
            "system": member["system_prompt"] if member else "",
            "tools": tools, "messages": messages, "complete": complete,
            "note": "" if complete else f"Only {len(stored)} of {upto} history messages are still stored.",
        }

    def stats(self, agent: str | None = None, since: float | None = None) -> dict[str, Any]:
        where, params = "WHERE 1=1", []
        if agent:
            where += " AND agent = ?"
            params.append(agent)
        if since:
            where += " AND started_at >= ?"
            params.append(since)
        totals = self.db.one(
            "SELECT COUNT(*) AS traces, COALESCE(SUM(llm_calls),0) AS llm_calls, "
            "COALESCE(SUM(tool_calls),0) AS tool_calls, COALESCE(SUM(errors),0) AS errors, "
            "COALESCE(SUM(input_tokens),0) AS input_tokens, COALESCE(SUM(output_tokens),0) AS output_tokens, "
            "COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens, "
            "COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens, COALESCE(SUM(cost_usd),0) AS cost_usd "
            f"FROM traces {where}", params)
        statuses = {r["status"]: r["n"] for r in self.db.all(
            f"SELECT status, COUNT(*) AS n FROM traces {where} GROUP BY status", params)}
        tools = [dict(r) for r in self.db.all(
            "SELECT name, COUNT(*) AS calls, SUM(status = 'error') AS errors, CAST(AVG(duration_ms) AS INTEGER) "
            f"AS avg_ms, MAX(duration_ms) AS max_ms FROM spans {where} AND kind = 'tool' "
            "GROUP BY name ORDER BY calls DESC", params)]
        models = [dict(r) for r in self.db.all(
            "SELECT name, COUNT(*) AS calls, SUM(status = 'error') AS errors, CAST(AVG(duration_ms) AS INTEGER) "
            "AS avg_ms, COALESCE(SUM(cost_usd),0) AS cost_usd, "
            "COALESCE(SUM(json_extract(usage, '$.output_tokens')),0) AS output_tokens "
            f"FROM spans {where} AND kind = 'llm' GROUP BY name ORDER BY calls DESC", params)]
        return {"agent": agent, **dict(totals), "statuses": statuses, "tools": tools, "models": models}

    def export(self, trace_id: int, *, contexts: bool = False) -> dict[str, Any] | None:
        """A self-contained trace document (JSON). With contexts=True every LLM span carries its full
        request context."""
        trace = self.get_trace(trace_id)
        if trace is None:
            return None
        member = self.db.one("SELECT name, role, model, language, system_prompt FROM agents WHERE name = ?",
                             (trace["agent"],))
        trace["member"] = dict(member) if member else None
        if contexts:
            cache: dict = {}  # one read of each history epoch for all spans
            for span in trace["spans"]:
                if span["kind"] == "llm":
                    ctx = self.span_context(span["id"], tools=[], _span=span, _cache=cache)
                    span["context"] = {"messages": ctx["messages"], "complete": ctx["complete"]} if ctx else None
        return trace

    def prune(self, older_than_days: float) -> int:
        """Delete finished traces (and their spans) older than N days, and history archived before then
        (only LLM calls of those traces could need it). Returns the number of traces removed."""
        if older_than_days <= 0:
            return 0
        cutoff = time.time() - older_than_days * 86400
        ids = [r["id"] for r in self.db.all("SELECT id FROM traces WHERE status != ? AND started_at < ?",
                                             (RUNNING, cutoff))]
        with self.db.transaction() as db:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ",".join("?" * len(chunk))
                db.conn.execute(f"DELETE FROM spans WHERE trace_id IN ({marks})", chunk)
                db.conn.execute(f"DELETE FROM traces WHERE id IN ({marks})", chunk)
            # the newest epoch of every agent stays marked: the epoch counter is MAX(epoch) + 1
            db.conn.execute("INSERT OR IGNORE INTO history_archive(agent, epoch, seq, msg, archived_at) "
                            "SELECT agent, MAX(epoch), ?, NULL, MAX(archived_at) FROM history_archive GROUP BY agent",
                            (EPOCH_MARK,))
            db.conn.execute("DELETE FROM history_archive WHERE archived_at < ? AND NOT (seq = ? AND epoch = "
                            "(SELECT MAX(h.epoch) FROM history_archive h WHERE h.agent = history_archive.agent))",
                            (cutoff, EPOCH_MARK))
        return len(ids)


# ---- text rendering (tools, console, CLI) ------------------------------------------------------
def _fmt_tokens(n: int | float | None) -> str:
    n = int(n or 0)
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _fmt_ms(ms: int | float | None) -> str:
    ms = int(ms or 0)
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return f"{ms // 60_000}m{(ms % 60_000) // 1000:02d}s"


def _one_line(text: Any, limit: int) -> str:
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clock(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-"


def trace_line(t: dict[str, Any]) -> str:
    tokens = (t.get("input_tokens") or 0) + (t.get("cache_read_tokens") or 0) + (t.get("cache_write_tokens") or 0)
    parts = [f"#{t['id']} {t['agent']}", _clock(t.get("started_at")), f"[{t['status']}]"]
    if t.get("kind") == "external":
        parts.append(f"external session, {t.get('tool_calls', 0)} tool call(s), {_fmt_ms(t.get('duration_ms'))}")
    else:
        parts.append(f"{t.get('steps', 0)} step(s), {t.get('llm_calls', 0)} LLM, {t.get('tool_calls', 0)} tool(s)"
                     f"{', ' + str(t['errors']) + ' error(s)' if t.get('errors') else ''}, "
                     f"{_fmt_ms(t.get('duration_ms'))}, {_fmt_tokens(tokens)} in / "
                     f"{_fmt_tokens(t.get('output_tokens'))} out, ${float(t.get('cost_usd') or 0):.4f}")
    line = " ".join(parts)
    if t.get("tasks"):
        line += " | tasks " + ", ".join(f"#{i}" for i in t["tasks"])
    if t.get("trigger"):
        line += f"\n    woke by: {_one_line(t['trigger'], 160)}"
    if t.get("outcome"):
        line += f"\n    outcome: {_one_line(t['outcome'], 160)}"
    return utf8_safe(line)  # printed by the CLI and sent to MCP clients: no unpaired surrogates


def span_lines(s: dict[str, Any], detail_chars: int = 300) -> list[str]:
    head = f"  [{s.get('step', 0)}] #{s['id']} {s['kind']} {s.get('name') or ''}".rstrip()
    head += f" {_fmt_ms(s.get('duration_ms'))}" if s.get("kind") in ("llm", "tool", "compaction") else ""
    if s.get("status") not in ("ok", None):
        head += f" [{s['status']}]"
    lines = [head]
    out, inp, meta = s.get("output"), s.get("input"), s.get("meta") or {}
    pad = "      "
    if s["kind"] == "llm":
        usage = s.get("usage") or {}
        if usage:
            prompt = sum(usage.get(k, 0) for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens"))
            lines[0] += (f" {_fmt_tokens(prompt)}→{_fmt_tokens(usage.get('output_tokens'))} tok"
                         f" (cache {_fmt_tokens(usage.get('cache_read_tokens'))}) ${float(s.get('cost_usd') or 0):.4f}")
        if meta.get("purpose") == "compaction":
            lines[0] += " (compaction)"
        if isinstance(out, dict):
            if out.get("stop_reason"):
                lines[0] += f" stop={out['stop_reason']}"
            if out.get("thinking"):
                lines.append(f"{pad}thinking: {_one_line(out['thinking'], detail_chars)}")
            if out.get("text"):
                lines.append(f"{pad}text: {_one_line(out['text'], detail_chars)}")
            for call in out.get("tool_calls") or []:
                lines.append(f"{pad}→ {call['name']} {_one_line(call.get('arguments'), detail_chars)}")
        if s.get("error"):
            retry = f" (retry in {meta['retry_in']}s)" if meta.get("retry_in") else ""
            lines.append(f"{pad}error: {_one_line(s['error'], detail_chars)}{retry}")
    elif s["kind"] == "tool":
        lines[0] += f" {_one_line(inp, min(detail_chars, 200))}" if inp else ""
        lines.append(f"{pad}{'ERROR' if s.get('status') == 'error' else 'result'}: {_one_line(out or '', detail_chars)}")
    else:
        body = out if out not in (None, "") else inp
        if body not in (None, ""):
            lines.append(f"{pad}{_one_line(body, detail_chars)}")
        if s.get("error"):
            lines.append(f"{pad}error: {_one_line(s['error'], detail_chars)}")
    return lines


def render_trace(trace: dict[str, Any], detail_chars: int = 300) -> str:
    rows = [trace_line({**trace, "trigger": ""}), f"Started {_clock(trace.get('started_at'))}, "
            f"ended {_clock(trace.get('ended_at'))}" + (f", model {trace['model']}" if trace.get("model") else "")]
    if trace.get("trigger"):
        rows.append("Woke by:\n" + _one_line(trace["trigger"], max(detail_chars * 3, 600)))
    rows.append("Spans (span id → full payload via span_id):")
    for span in trace.get("spans", []):
        rows += span_lines(span, detail_chars)
    if trace.get("outcome"):
        rows.append(f"Outcome: {trace['outcome']}")
    return utf8_safe("\n".join(rows))


def render_span(span: dict[str, Any], limit: int = 20000) -> str:
    def block(title: str, value: Any) -> str:
        if value in (None, "", [], {}):
            return ""
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)
        if len(text) > limit:
            text = text[:limit] + f"\n…[{len(text) - limit} more chars]"
        return f"{title}:\n{text}"

    head = (f"Span #{span['id']} of trace #{span['trace_id']} — {span['agent']} — step {span.get('step')} — "
            f"{span['kind']} {span.get('name') or ''} [{span.get('status')}] {_fmt_ms(span.get('duration_ms'))}")
    parts = [head, f"Started {_clock(span.get('started_at'))}"]
    if span.get("usage"):
        parts.append("Usage: " + json.dumps(span["usage"]) + f", cost ${float(span.get('cost_usd') or 0):.4f}")
    if span.get("meta"):
        parts.append("Meta: " + json.dumps(span["meta"], ensure_ascii=False))
    inp = span.get("input")
    if span["kind"] == "llm" and isinstance(inp, dict):
        parts.append(f"Request: {inp.get('messages')} message(s) in the context (history epoch {inp.get('epoch')}); "
                     "pass context=true to see them.")
        inp = inp.get("extra") or None
    out = span.get("output")
    if span["kind"] == "llm" and isinstance(out, dict):
        parts += [b for b in (block("Thinking", out.get("thinking")), block("Text", out.get("text")),
                              block("Tool calls", out.get("tool_calls"))) if b]
        out = None
    parts += [b for b in (block("Input", inp), block("Output", out), block("Error", span.get("error"))) if b]
    return utf8_safe("\n\n".join(parts))


def render_context(ctx: dict[str, Any], offset: int = 0, limit: int = 30, chars: int = 4000) -> str:
    messages = ctx["messages"]
    total = len(messages)
    chosen = messages[offset:offset + limit]
    shown = f"showing {offset + 1}-{offset + len(chosen)}" if chosen else f"none from offset {offset}"
    parts = [f"Context of LLM span #{ctx['span_id']} ({ctx['agent']}, model {ctx.get('model')}): "
             f"{total} message(s), {shown}; "
             f"{len(ctx['tools'])} tool(s); system prompt {len(ctx['system'] or '')} chars."
             + (f" {ctx['note']}" if ctx.get("note") else "")]
    if offset == 0:
        parts.append("SYSTEM:\n" + clip(ctx["system"] or "", chars))
    for i, msg in enumerate(chosen, start=offset):
        role = msg.get("role")
        body = msg.get("content")
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)
        extra = ""
        if msg.get("tool_calls"):
            extra = "\n" + "\n".join(f"→ {c.get('name')} {json.dumps(c.get('arguments'), ensure_ascii=False)}"
                                     for c in msg["tool_calls"])
        name = f" {msg['name']}" if role == "tool" and msg.get("name") else ""
        error = " (error)" if msg.get("is_error") else ""
        parts.append(f"[{i}] {role.upper() if role else '?'}{name}{error}:\n{clip(text or '', chars)}{clip(extra, chars)}")
    return utf8_safe("\n\n".join(parts))
