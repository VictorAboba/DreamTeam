"""ReAct runtime of one LLM agent: wake on events, call the LLM, execute tools, repeat."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from . import prompts
from .db import dumps, loads, utf8_safe
from .llm.base import LLMError, LLMResponse, accepts_max_tokens, estimate_tokens
from .mailbox import MAX_TEXT
from .models import USER, AgentStatus, Status, ToolError
from .shell import truncate_middle
from .tools import ToolContext, call_tool, frozen
from .tracing import Span, Trace

if TYPE_CHECKING:
    from .swarm import Swarm

log = logging.getLogger(__name__)

RETRY_DELAYS = (2, 4, 8, 16, 32)  # backoff between attempts of one LLM call (retryable errors)
# A failed activation (retryable LLM error after all attempts, or a crash) is retried by itself after
# these delays (consecutive failures move along the list), so an API outage does not freeze the swarm.
RECOVERY_DELAYS = (30, 60, 120, 300, 600)
CRASH_PAUSE_SECONDS = 5  # after an unexpected exception, before the next wake is handled
ACTIVITY_CHARS = 2000
TAIL_MAX_CHARS = 40_000  # newest user-side texts kept verbatim through a compaction
DIGEST_MAX_CHARS = 8_000  # recent steps kept by a compaction that could not ask the LLM for a summary
INTERRUPTED_TEXT = "Interrupted: the swarm was restarted before this call finished."
# Fragments of a 400 meaning "the request does not fit into the model's context window".
CONTEXT_OVERFLOW_MARKERS = (
    "prompt is too long", "context length", "context_length", "context window", "maximum context",
    "too many tokens", "too many input tokens", "request too large", "request_too_large", "input is too long",
    "context size", "exceeds the context", "max_new_tokens", "max_model_len",  # llama.cpp, TGI, vLLM
    "exceed context limit",  # Anthropic: "input length and `max_tokens` exceed context limit: ..."
)
# Fragments of a 400 about the output limit itself: a raised max_tokens above what the model or provider allows.
OUTPUT_LIMIT_MARKERS = ("max_tokens", "max_completion_tokens", "max_output_tokens", "max_new_tokens",
                        "maximum output", "output tokens", "completion tokens")


def is_context_overflow(error: LLMError | None) -> bool:
    if error is None or error.retryable or error.kind not in ("bad_request", "other"):
        return False
    text = error.message.lower()
    return any(marker in text for marker in CONTEXT_OVERFLOW_MARKERS)


def is_output_limit_error(error: LLMError | None) -> bool:
    if error is None or error.retryable or error.kind != "bad_request":
        return False
    text = error.message.lower()
    return any(marker in text for marker in OUTPUT_LIMIT_MARKERS)


class AgentRuntime:
    def __init__(self, swarm: "Swarm", name: str):
        self.swarm = swarm
        self.name = name
        self.state = AgentStatus.IDLE
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._turn_ended = False
        self._sent_to_user = False
        self._from_user = False
        self._llm = None
        # id of an LLM client with calls in flight -> their number: reset_llm closes a client only once it is idle
        self._llm_calls: dict[int, int] = {}
        self.history: list[dict[str, Any]] = []
        self._next_seq = 0
        self._len_at_last_call = 0
        # continuation state of an activation that was cut short (pause, budget, LLM failure, crash)
        self._resume = False  # activate on the next wake even if nothing is pending
        self._owed_reply = False  # that activation was answering the user and had not replied yet
        self._failures = 0  # consecutive failed activations
        self._last_reported = ""
        self._last_error: LLMError | None = None
        self._recovery: asyncio.TimerHandle | None = None
        self._compaction_floor = 0  # context size right after the last compaction (0 = none yet)
        # in-progress tasks the last activation stopped on with plain text even after a nudge (swarm: n_stalled)
        self.stalled: frozenset[int] = frozenset()
        self.stalled_at = 0.0
        self.trace: Trace | None = None  # the trace of the running activation (docs/SPEC.md §16)
        member = swarm.member(name) or {}
        self.language: str = member.get("language") or swarm.language
        self.system_prompt: str = member.get("system_prompt") or ""
        self.profile_name: str | None = member.get("model") or None
        self.last_prompt_tokens: int = int(member.get("context_tokens") or 0)
        self.last_output_tokens = 0
        self.last_max_tokens = 0  # the output limit of the last successful LLM call
        # the LLM client whose provider refused a raised output limit: no more raising until it is rebuilt
        self._limit_refused_by: Any = None
        # its frozen tool set (tools/frozen.py): sent unchanged, so the prompt prefix stays the same
        self.tools_rev = 0
        self.tools: list[dict[str, Any]] = []
        self.tool_names: frozenset[str] = frozenset()
        self._tool_params: dict[str, dict[str, Any]] = {}
        self._load_tools()
        self._resume = self._ended_mid_activation()

    def _load_tools(self) -> None:
        """The agent's tool set, stored at its first runtime start (it never changes until a compaction).
        An agent that has a history but no record ran before tool sets were frozen: it keeps the baseline."""
        db = self.swarm.db
        rec = frozen.read(db, self.name)
        if rec is None:
            has_history = db.scalar("SELECT 1 FROM history WHERE agent = ? LIMIT 1", (self.name,))
            rec = frozen.initial_record(self.language, bool(has_history))
            frozen.write(db, self.name, rec)
        self._set_tools(rec)

    def _set_tools(self, rec: dict[str, Any]) -> None:
        self.tools_rev = int(rec["rev"])
        self.tools = rec["tools"]
        self.tool_names = frozenset(frozen.names(self.tools))
        self._tool_params = {t["name"]: t.get("parameters") or {} for t in self.tools}

    def tool_parameters(self, name: str) -> dict[str, Any] | None:
        """The parameters of `name` in the agent's own tool set; None when it does not have that tool."""
        return self._tool_params.get(name)

    def _ended_mid_activation(self) -> bool:
        """True when the stored history stops inside an activation (the process died or the agent was
        stopped while working): a finished one ends with an assistant reply or the result of `wait`."""
        row = self.swarm.db.one("SELECT msg FROM history WHERE agent = ? ORDER BY seq DESC LIMIT 1", (self.name,))
        if row is None:
            return False
        last = loads(row["msg"])
        if last.get("role") == "assistant":
            return bool(last.get("tool_calls"))
        if last.get("role") == "tool":
            return last.get("name") != "wait"
        return True

    # ---- control ------------------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None or self._task.done():
            # a context of its own: the runtime must not inherit the request context of whoever started it (e.g.
            # hub_call's sent_as_seen_by, when an MCP client hires the agent)
            self._task = asyncio.create_task(self._run(), name=f"agent:{self.name}", context=contextvars.Context())

    def request_stop(self) -> None:
        self._stopping = True
        self._cancel_recovery()
        self._wake.set()

    async def stop(self) -> None:
        self.request_stop()
        task = self._task
        if task is asyncio.current_task():
            return
        if task is not None:
            task.cancel()
            # asyncio.wait raises CancelledError only when the caller itself is cancelled (never for the
            # agent task's own cancellation), and it works in cleanup code of an already cancelled task
            await asyncio.wait({task})
            if not task.cancelled() and task.exception() is not None:
                log.debug("agent %s ended with an error", self.name, exc_info=task.exception())
        await self._close_llm()

    def wake(self) -> None:
        self._wake.set()

    def end_turn(self) -> None:
        self._turn_ended = True

    def note_sent_to_user(self) -> None:
        self._sent_to_user = True

    @property
    def interrupted(self) -> bool:
        """The last activation was cut short; the next wake continues it even without new events."""
        return self._resume

    @property
    def llm(self):
        if self._llm is None:
            self._llm = self.swarm.make_llm(self.profile_name, self.name)
        return self._llm

    async def _close_llm(self) -> None:
        llm, self._llm = self._llm, None
        self._limit_refused_by = None
        await self._aclose(llm)

    async def _aclose(self, llm: Any) -> None:
        aclose = getattr(llm, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                log.debug("closing the LLM client of %s failed", self.name, exc_info=True)

    def reset_llm(self) -> None:
        """Drop the cached LLM client (settings changed): the next call builds one from the new profile.
        The old client is closed in the background when an event loop runs; a client with a call in flight only
        when that call returns (closing it under the call fails it: "the client has been closed")."""
        llm, self._llm = self._llm, None
        self._limit_refused_by = None  # the new profile may allow a larger output limit
        if llm is not None and not self._llm_calls.get(id(llm)):
            self._close_soon(llm)

    def _close_soon(self, llm: Any) -> None:
        if getattr(llm, "aclose", None) is None:
            return
        try:
            asyncio.get_running_loop().create_task(self._aclose(llm))
        except RuntimeError:
            pass

    async def _complete(self, messages: list[dict[str, Any]],
                        max_tokens: int | None = None) -> tuple[LLMResponse, Any]:
        """One request on the current client, which stays open until it returns even if the settings change
        meanwhile (reset_llm); the response and the client that produced it (its profile prices the usage).
        `max_tokens`: a raised output limit (None: the profile's; the keyword is then not passed at all)."""
        llm = self.llm
        key = id(llm)  # (a test double may be unhashable)
        self._llm_calls[key] = self._llm_calls.get(key, 0) + 1
        kwargs = {"max_tokens": max_tokens} if max_tokens else {}
        try:
            async with self.swarm.llm_semaphore:
                return await llm.complete(self.system_prompt, messages, self.tools, **kwargs), llm
        finally:
            left = self._llm_calls.pop(key) - 1
            if left:
                self._llm_calls[key] = left
            elif llm is not self._llm:  # replaced while the call ran: nothing uses it any more
                self._close_soon(llm)

    # ---- tracing ------------------------------------------------------------------------------
    @property
    def _tr(self) -> Trace:
        """The current trace, or a disabled writer outside an activation (all methods are no-ops)."""
        if self.trace is not None:
            return self.trace
        return Trace(self.swarm.tracer, None, self.name)

    def _model_id(self) -> str | None:
        """The configured model of the current LLM client (names failed attempts in traces); never builds one."""
        return getattr(getattr(self._llm, "profile", None), "model", None) or None

    def _finish_trace(self, status: str, outcome: str = "") -> None:
        trace, self.trace = self.trace, None
        if trace is not None:
            try:
                trace.finish(status, outcome)
            except Exception:
                log.exception("agent %s: could not finish its trace", self.name)

    def _cancel_recovery(self) -> None:
        if self._recovery is not None:
            self._recovery.cancel()
            self._recovery = None

    def _schedule_recovery(self) -> None:
        self._cancel_recovery()
        delay = RECOVERY_DELAYS[min(max(self._failures, 1), len(RECOVERY_DELAYS)) - 1]
        self._recovery = asyncio.get_running_loop().call_later(delay, self._recover)

    def _recover(self) -> None:
        self._recovery = None
        if not self._stopping:
            self._resume = True
            self._wake.set()

    # ---- history ------------------------------------------------------------------------------
    def _load_history(self) -> None:
        rows = self.swarm.db.all("SELECT seq, msg FROM history WHERE agent = ? ORDER BY seq", (self.name,))
        self.history = [loads(r["msg"]) for r in rows]
        self._next_seq = (rows[-1]["seq"] + 1) if rows else 0
        self._len_at_last_call = len(self.history)

    def _append(self, msg: dict[str, Any]) -> None:
        # the row first: if it cannot be stored, memory and the database stay identical
        self.swarm.db.execute(
            "INSERT INTO history(agent, seq, msg) VALUES (?, ?, ?)", (self.name, self._next_seq, dumps(msg))
        )
        self._next_seq += 1
        self.history.append(msg)

    def _replace_history(self, messages: list[dict[str, Any]], tools: dict[str, Any] | None = None) -> int:
        """Compaction rewrite; the old history is archived (its epoch is returned) so traces keep the
        exact context of earlier LLM calls. `tools`: a new tool set record, stored in the same transaction
        (the new history holds no assistant turn bound to the old tool set)."""
        with self.swarm.db.transaction() as db:
            epoch = self.swarm.tracer.archive_history(db.conn, self.name)
            db.conn.execute("DELETE FROM history WHERE agent = ?", (self.name,))
            for seq, msg in enumerate(messages):
                db.conn.execute("INSERT INTO history(agent, seq, msg) VALUES (?, ?, ?)", (self.name, seq, dumps(msg)))
            if tools is not None:
                frozen.write(db, self.name, tools)
        if tools is not None:
            self._set_tools(tools)
        self.history = list(messages)
        self._next_seq = len(messages)
        self._len_at_last_call = 0
        return epoch

    def _repair_history(self, reason: str = INTERRUPTED_TEXT) -> None:
        """Give dangling tool calls (after a crash or restart) a result so the history stays valid.
        Only results after the last assistant message count: some servers reuse ids such as "call_0"."""
        for index in range(len(self.history) - 1, -1, -1):
            if self.history[index].get("role") == "assistant":
                break
        else:
            return
        answered = {m.get("tool_call_id") for m in self.history[index + 1:] if m.get("role") == "tool"}
        for call in self.history[index].get("tool_calls") or []:
            if call.get("id") not in answered:
                answered.add(call.get("id"))
                self._append({"role": "tool", "tool_call_id": call.get("id"), "name": call.get("name"),
                              "content": reason, "is_error": True})

    # ---- state / activity ---------------------------------------------------------------------
    def _set_state(self, state: str) -> None:
        if self.state == state:
            return
        self.state = state
        member = self.swarm.member(self.name)
        if member and member["status"] != AgentStatus.RETIRED:
            self.swarm.db.execute("UPDATE agents SET status = ?, last_active = ? WHERE name = ?",
                                  (state, time.time(), self.name))
            self.swarm.bus.emit("agent.status", self.name, name=self.name, status=state)

    def _activity(self, kind: str, text: str = "", **extra: Any) -> None:
        self.swarm.bus.emit("agent.activity", self.name, agent=self.name, kind=kind,
                            text=truncate_middle(text or "", ACTIVITY_CHARS), **extra)

    def _fail(self, error: str, *, retry: bool, wake_user: bool = False) -> None:
        """A failed activation: tell the user (once per streak of the same error) and maybe retry later."""
        self._failures += 1
        if self._failures == 1 or error != self._last_reported:
            self._last_reported = error
            self.swarm.notify(USER, "u_agent_error", name=self.name, error=error[:500], wake=wake_user)
        if retry and not self._stopping:
            self._schedule_recovery()

    # ---- main loop ----------------------------------------------------------------------------
    async def _run(self) -> None:
        self._load_history()
        self._repair_history()
        try:
            while not self._stopping:
                await self._wake.wait()
                self._wake.clear()
                # an already set event does not suspend: yield once so an agent that re-wakes itself
                # (step limit, backlog) with an instant LLM (scripted, demos) cannot starve the loop
                await asyncio.sleep(0)
                if self._stopping:
                    break
                if self.swarm.paused:
                    self._set_state(AgentStatus.PAUSED)
                    continue
                if not self._resume and not self.swarm.has_pending(self.name):
                    if self.state == AgentStatus.PAUSED:
                        self._set_state(AgentStatus.IDLE)
                    continue
                try:
                    await self._activate()
                    if self.swarm.has_pending(self.name):  # more unread than one wake-up renders
                        self._wake.set()
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    if self._stopping or task is None or task.cancelling():
                        self._tr.record("event", "stopped", output="The agent was stopped during this activation.")
                        self._finish_trace("stopped", "Stopped during the activation.")
                        raise
                    # a CancelledError this task was not asked for (e.g. a future cancelled by another
                    # task inside a tool) must not kill the agent for good
                    log.error("agent %s: an operation was cancelled unexpectedly", self.name)
                    await self._crashed("CancelledError: an operation was cancelled unexpectedly")
                except Exception as exc:
                    log.exception("agent %s crashed during activation", self.name)
                    await self._crashed(f"{type(exc).__name__}: {exc}")
        finally:
            if self.state == AgentStatus.WORKING:  # also on stop: `dreamteam status` reads it without a hub
                self._set_state(AgentStatus.IDLE)
        await self._close_llm()  # reached when the agent stopped itself (e.g. retired from its own tool call)

    async def _crashed(self, error: str) -> None:
        try:
            self._tr.record("event", "crash", output=error, status="error", error=error)
        except Exception:
            log.exception("agent %s: could not trace the crash", self.name)
        self._finish_trace("error", error)
        try:
            self._repair_history(f"Error: this call did not complete ({error[:300]}).")
            self._resume = True
            self._owed_reply = self._owed_reply or (self._from_user and not self._sent_to_user)
            self._set_state(AgentStatus.ERROR)
            self._activity("error", error)
            self._fail(error, retry=True, wake_user=True)
        except Exception:
            log.exception("agent %s: could not record the crash", self.name)
        await asyncio.sleep(CRASH_PAUSE_SECONDS)

    def _unread_from_user(self) -> bool:
        return bool(self.swarm.db.scalar(
            "SELECT COUNT(*) FROM inbox i JOIN messages m ON m.id = i.message_id "
            "WHERE i.recipient = ? AND i.read_at IS NULL AND m.sender = ?", (self.name, USER)))

    async def _activate(self) -> None:
        swarm = self.swarm
        self._cancel_recovery()
        self._resume = False
        from_user = self._owed_reply or self._unread_from_user()
        self._owed_reply = False
        self._from_user = from_user
        self._sent_to_user = False
        self._turn_ended = False
        wake_text = swarm.render_wake(self.name)
        if not wake_text:
            return
        self._set_state(AgentStatus.WORKING)
        self._append({"role": "user", "content": wake_text})
        self._activity("wake", wake_text)
        self._finish_trace("interrupted")  # never left open by an earlier activation
        self.trace = trace = swarm.tracer.begin(self.name, wake_text, from_user=from_user)
        trace.record("input", "wake", output=wake_text)
        reply: list[str] = []  # plain text for the user, including parts cut off by the output limit
        nudged: set[int] = set()  # in-progress task ids this activation was already nudged about
        overflow_handled = False
        out_limit: int | None = None  # the output limit asked for the step after a cut-off reply (None: profile's)
        self.stalled = frozenset()
        outcome = "done"  # done | step_limit | interrupted | failed | refusal
        status, summary = "done", ""  # of the trace
        max_steps = max(1, swarm.config.agents.max_steps_per_turn)
        step = 0
        while step < max_steps:
            step += 1
            trace.step = step
            if self._stopping or swarm.paused:
                outcome, status, summary = "interrupted", "stopped", self._stop_reason()
                break
            if swarm.budget_exceeded():
                swarm.pause(USER, "budget")
                outcome, status, summary = "interrupted", "paused", "The budget is exhausted: the swarm was paused."
                trace.record("event", "budget_pause", output=summary)
                break
            if self._needs_compaction():
                await self._compact()
                if self._last_error is not None:  # the main call would fail the same way
                    outcome = "failed"
                    break
                if self._stopping or swarm.paused:
                    outcome, status, summary = "interrupted", "stopped", self._stop_reason()
                    break
            response = await self._call_llm(self.history, max_tokens=out_limit)
            if response is None and is_context_overflow(self._last_error) and not overflow_handled:
                overflow_handled = True  # the estimate missed: compact without the LLM and try once more
                self._compact_without_llm()
                response = await self._call_llm(self.history)  # (an overflow again fails the activation)
            if response is None:
                if self._last_error is not None:
                    outcome = "failed"
                else:
                    outcome, status, summary = "interrupted", "stopped", self._stop_reason()
                break
            self._append(self._assistant_message(response))
            self._log_response(response)
            if response.stop_reason == "refusal":
                swarm.notify(USER, "u_refusal", name=self.name, category=response.text or "refusal", wake=False)
                trace.record("event", "refusal", output=response.text or "refusal", status="error")
                outcome, status, summary = "refusal", "refusal", "The model declined the request."
                break
            # A reply cut off by the output limit without a tool call (often all thinking): the next step may write
            # twice as much, up to the profile's ceiling (_output_limit); any other reply goes back to the profile's.
            cut_off = response.stop_reason == "max_tokens" and not response.tool_calls
            out_limit = 2 * self.last_max_tokens if cut_off else None
            if not response.tool_calls:
                text = (response.text or "").strip()
                if text:
                    reply.append(text)
                if response.stop_reason == "max_tokens":
                    cut = prompts.t(self.language, "cut_off")
                    self._append({"role": "user", "content": cut})
                    trace.record("input", "continue", output=cut)
                    continue
                if from_user:
                    self._forward_reply(reply)
                reply.clear()
                in_progress = [t for t in swarm.board.open_tasks_of(self.name) if t.status == Status.IN_PROGRESS]
                # a command of the agent went on as a job it waits for (JobManager.busy): no nudge now; its end
                # wakes the agent, and the stall below is still recorded for once the busy window ends
                jobs = getattr(swarm, "jobs", None)
                busy = in_progress and jobs is not None and jobs.busy(self.name)
                # one nudge per task: an agent that finished #2 with a nudge and then stopped with text again
                # after claiming #3 would otherwise go idle holding #3, and nothing would ever wake it
                if not busy and any(t.id not in nudged for t in in_progress):
                    nudged.update(t.id for t in in_progress)
                    nudge = prompts.t(self.language, "nudge", tasks=", ".join(f"#{t.id}" for t in in_progress))
                    self._append({"role": "user", "content": nudge})
                    self._activity("nudge", nudge)
                    trace.record("input", "nudge", output=nudge)
                    continue
                if in_progress:  # nudged already: the scheduler wakes it once more later (n_stalled)
                    self.stalled, self.stalled_at = frozenset(t.id for t in in_progress), time.time()
                summary = "Answered without tool calls"
                break
            await self._run_tools(response)
            if self._turn_ended or self._stopping:
                summary = "Turn ended (wait)" if self._turn_ended else "Stopped (the agent left the swarm)."
                status = "done" if self._turn_ended else "stopped"
                break
            if self._unread_from_user():
                from_user = self._from_user = True
            incoming = swarm.render_wake(self.name, incoming=True)
            if incoming:
                self._append({"role": "user", "content": incoming})
                self._activity("wake", incoming)
                trace.record("input", "incoming", output=incoming)
        else:
            outcome, status, summary = "step_limit", "step_limit", f"Step limit reached ({max_steps} steps)."
            trace.record("event", "step_limit", output=summary)
            swarm.notify(self.name, "n_step_limit")
        if outcome in ("done", "step_limit") and from_user and reply:
            self._forward_reply(reply)
        if outcome not in ("done", "refusal"):
            self._owed_reply = from_user and not self._sent_to_user
        if outcome in ("interrupted", "failed"):
            self._resume = True
        self._from_user = False
        if outcome == "failed":
            error = self._last_error
            status, summary = "error", f"{error.kind}: {error.message}"
            self._fail(summary, retry=error.retryable)
        self._finish_trace(status, summary)
        self._set_state(AgentStatus.ERROR if outcome == "failed" else AgentStatus.IDLE)

    def _stop_reason(self) -> str:
        if self._stopping:
            return "Stopped during the activation."
        return f"The swarm was paused ({self.swarm.pause_reason or 'no reason given'})."

    def _forward_reply(self, parts: list[str]) -> None:
        """The activation answered a USER message with plain text: deliver it as a message (SPEC §6.2)."""
        text = "\n\n".join(parts).strip()
        parts.clear()
        if not text or self._sent_to_user:
            return
        self._sent_to_user = True
        try:  # the model's text may hold an unpaired surrogate, which a TEXT column cannot store
            self.swarm.mail.send(self.name, USER, utf8_safe(truncate_middle(text, MAX_TEXT - 200)))
            self._tr.record("event", "reply_to_user", output=text)
        except ToolError as exc:
            log.warning("agent %s: could not forward its reply to the user: %s", self.name, exc)
            self._activity("error", f"Could not forward the reply to the user: {exc}")

    async def _run_tools(self, response: LLMResponse) -> None:
        limit = self.swarm.config.agents.tool_output_chars
        trace = self._tr
        for call in response.tool_calls:
            span = trace.tool_start(call.id, call.name, call.arguments)
            if call.parse_error:
                text, is_error = (f"Error: could not parse the arguments of {call.name}: {call.parse_error}. "
                                  "Send the arguments as a valid JSON object."), True
            else:
                ctx = ToolContext(self.swarm, self.name, self)
                text, is_error = await call_tool(ctx, call.name, call.arguments)
            trace.tool_end(span, text, is_error, shown_chars=min(len(text), limit))
            text = truncate_middle(text, limit)
            self._append({"role": "tool", "tool_call_id": call.id, "name": call.name,
                          "content": text, "is_error": is_error})
            self._activity("tool_result", text, tool=call.name, is_error=is_error)

    @staticmethod
    def _assistant_message(response: LLMResponse) -> dict[str, Any]:
        return {
            "role": "assistant",
            "content": response.text or "",
            "tool_calls": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in response.tool_calls],
            "raw": response.raw,
        }

    def _log_response(self, response: LLMResponse) -> None:
        if response.thinking:
            self._activity("thinking", response.thinking)
        if response.text:
            self._activity("text", response.text)
        for call in response.tool_calls:
            self._activity("tool_call", json.dumps(call.arguments, ensure_ascii=False), tool=call.name)

    # ---- LLM ----------------------------------------------------------------------------------
    async def _call_llm(self, messages: list[dict[str, Any]], *, purpose: str = "step",
                        extra: list[dict[str, Any]] | None = None, parent: Span | None = None,
                        max_tokens: int | None = None) -> LLMResponse | None:
        """One LLM call with backoff for retryable errors. None on failure (`_last_error` says why) or
        when the agent was stopped / the swarm paused during the backoff (`_last_error` is None).
        `extra`: the messages at the end of `messages` that are not part of the history (traces).
        `max_tokens`: the output limit asked for after a cut-off reply (None: the profile's; see _output_limit)."""
        swarm = self.swarm
        trace = self._tr
        self._last_error = None
        attempt = 0
        while attempt <= len(RETRY_DELAYS):
            limit = self._output_limit(max_tokens)
            span = trace.llm_start(len(messages), purpose=purpose, extra=extra, attempt=attempt + 1, parent=parent,
                                   tools_rev=self.tools_rev, max_tokens=limit)
            try:
                response, llm = await self._complete(messages, limit)
            except LLMError as exc:
                if limit is not None and self._raise_refused(exc, limit, span):
                    max_tokens = None  # the same request once more with the profile's limit (not a counted attempt)
                    continue
                if exc.retryable and attempt < len(RETRY_DELAYS):
                    delay = RETRY_DELAYS[attempt]
                    attempt += 1
                    trace.llm_error(span, exc.message, kind=exc.kind, retry_in=delay, model=self._model_id())
                    self._activity("error", f"{exc.kind}: {exc.message} — retrying in {delay}s")
                    await asyncio.sleep(delay)
                    if self._stopping or swarm.paused:
                        return None
                    continue
                trace.llm_error(span, exc.message, kind=exc.kind, model=self._model_id())
                self._last_error = exc
                self._activity("error", f"{exc.kind}: {exc.message}")
                return None
            except asyncio.CancelledError:  # stopped mid-call: not a failed attempt (no error counted)
                trace.llm_error(span, "Cancelled: the agent was stopped during this call.", kind="cancelled",
                                model=self._model_id(), status="interrupted")
                raise
            except BaseException as exc:  # a crash: the span must not stay "running"
                trace.llm_error(span, f"{type(exc).__name__}: {exc}", kind="other", model=self._model_id())
                raise
            cost = swarm.record_usage(self.name, response.usage, llm.profile, response.model)
            trace.llm_end(span, response, cost)
            self.last_max_tokens = limit or max(int(getattr(llm.profile, "max_tokens", 0) or 0), 1)
            usage = response.usage
            self.last_prompt_tokens = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
            self.last_output_tokens = usage.output_tokens
            self._len_at_last_call = len(messages) + 1
            self._failures = 0
            swarm.db.execute("UPDATE agents SET context_tokens = ? WHERE name = ?", (self.last_prompt_tokens, self.name))
            return response
        return None

    def _output_limit(self, requested: int | None) -> int | None:
        """The output limit of a request that asks for `requested` tokens after a cut-off reply: at most the
        profile's ceiling (max_tokens_ceiling, else 2 x max_tokens) and never more than the context leaves for the
        answer (vLLM & co. count prompt + max_tokens against the window). None: the profile's own max_tokens (no
        raise asked for, none possible, the provider refused one before, or a client without the keyword)."""
        if not requested:
            return None
        try:
            llm = self.llm
        except Exception:  # a client that cannot be built fails in _complete, where the caller handles it
            return None
        profile = llm.profile
        if self._limit_refused_by is llm or not accepts_max_tokens(llm):
            return None
        base = int(getattr(profile, "max_tokens", 0) or 0)
        ceiling = getattr(profile, "output_ceiling", None) or 2 * base  # (a test double's profile may lack it)
        room = self._context_limit() - self._prompt_estimate()
        limit = min(int(requested), ceiling, room)
        return limit if limit > base else None

    def _raise_refused(self, exc: LLMError, limit: int, span: Span | None) -> bool:
        """A request with a raised output limit failed: True (retry with the profile's limit) when the error is about
        that limit, or a context overflow the larger answer may have caused. A refused limit is not raised again on
        this client (until settings rebuild it); an overflow only costs this one retry."""
        # An overflow first: vLLM's and Anthropic's overflow messages also name max_tokens, but they mean that
        # prompt + limit did not fit this time, not that the provider never allows the limit.
        overflow = is_context_overflow(exc)
        refused = is_output_limit_error(exc) and not overflow
        if not refused and not overflow:
            return False
        if refused:
            self._limit_refused_by = self._llm
        trace = self._tr
        trace.llm_error(span, exc.message, kind=exc.kind, model=self._model_id())
        note = (f"The request with a raised output limit of {limit} tokens failed ({exc.kind}: {exc.message}); "
                "retrying with the profile's limit" + (" and not raising it again" if refused else "") + ".")
        trace.record("event", "output_limit_fallback", output=note, meta={"max_tokens": limit, "refused": refused})
        self._activity("error", note)
        log.info("agent %s: %s", self.name, note)
        return True

    def _context_limit(self) -> int:
        profile = self.llm.profile
        return int(profile.context_tokens or self.swarm.config.agents.context_tokens)

    def _fixed_tokens(self) -> int:
        return estimate_tokens(self.tools) + estimate_tokens(self.system_prompt)

    def _prompt_room(self) -> int:
        """The largest prompt that fits the context limit with room for the answer (vLLM counts prompt +
        max_tokens against the window)."""
        limit = self._context_limit()
        return limit - min(max(int(self.llm.profile.max_tokens or 0), 0), limit // 4)

    def _compaction_budget(self) -> int:
        """Prompt size that triggers a compaction: the context limit minus room for the answer and for the
        compaction request itself. When context_tokens is the model's real window (local models; vLLM counts
        prompt + max_tokens against it), the summary call must still fit, or the agent could never compact."""
        return self._prompt_room() - estimate_tokens(prompts.t(self.language, "compaction_request"))

    def _prompt_estimate(self) -> int:
        """The size of the next step's prompt: the last prompt plus the response to it (its output tokens), then
        everything appended since (an estimate of the whole history before the first call)."""
        recent = self.history[self._len_at_last_call:] if self._len_at_last_call <= len(self.history) else self.history
        if self.last_prompt_tokens:
            base = self.last_prompt_tokens + self.last_output_tokens
        else:
            base = estimate_tokens(self.history[: self._len_at_last_call]) + self._fixed_tokens()
        return base + estimate_tokens(recent)

    def _needs_compaction(self) -> bool:
        if len(self.history) < 3:
            return False
        limit = self._compaction_budget()
        if self._compaction_floor > limit * 3 // 4:
            # A fresh compaction already fills (nearly) all of the limit (system prompt + tool schemas +
            # summary): compacting again before the history grew by half a limit would only throw work away.
            # While a compaction still gets under the room of the context limit, never past that room, though:
            # context_tokens may then be the model's real window, where a larger prompt fails every request (a
            # summary call that no longer fits there falls back to a compaction without the LLM, see _compact).
            # A floor above the room means that context_tokens is not the real window (those requests worked).
            room = self._prompt_room()
            grown = self._compaction_floor + limit // 2
            limit = grown if self._compaction_floor >= room else min(grown, room)
        return self._prompt_estimate() > limit

    async def _compact(self) -> None:
        """Simple compaction (SPEC §6.1). On failure the history is kept and `_last_error` is set, except
        for a context overflow or another non-retryable 400, which fall back to a compaction without the LLM."""
        request = {"role": "user", "content": prompts.t(self.language, "compaction_request")}
        trace = self._tr
        span = trace.start("compaction", "compaction",
                           {"messages": len(self.history), "prompt_tokens": self.last_prompt_tokens})
        response = await self._call_llm(self.history + [request], purpose="compaction", extra=[request], parent=span)
        if response is None:
            error = self._last_error
            # The compaction request is the largest request the agent makes. A 400 for it is almost always the
            # context window, even when the provider words it in a way is_context_overflow() does not know;
            # keeping the history would fail the same way at every wake-up, so the agent would be stuck.
            if is_context_overflow(error) or (error is not None and not error.retryable
                                              and error.kind == "bad_request"):
                self._compact_without_llm(span)
                self._last_error = None
            else:
                error = self._last_error
                trace.end(span, None, status="error", error=f"{error.kind}: {error.message}" if error
                          else "Interrupted: the swarm was paused or the agent stopped.")
            return
        summary = (response.text or response.thinking or "").strip()
        if not summary:
            summary = self._fallback_summary("The summary could not be produced.")
        epoch = self._replace_with_summary(summary)
        trace.end(span, summary, meta={"epoch_archived": epoch, "tools_revision": self.tools_rev})

    def _compact_without_llm(self, span: Span | None = None) -> None:
        """Compaction after the model rejected the context as too long: notes + a digest of the latest
        steps + the newest texts, no LLM call."""
        trace = self._tr
        if span is None:
            span = trace.start("compaction", "compaction",
                               {"messages": len(self.history), "prompt_tokens": self.last_prompt_tokens})
        summary = self._fallback_summary(
            "The conversation no longer fit into the model's context, so no summary could be written.")
        epoch = self._replace_with_summary(summary)
        trace.end(span, summary, meta={"epoch_archived": epoch, "fallback": True, "tools_revision": self.tools_rev})

    def _notes(self) -> str:
        return ((self.swarm.member(self.name) or {}).get("notes") or "").strip()

    def _replace_with_summary(self, summary: str) -> int:
        # the notebook survives compaction (update_notes promises it): shown right away, not only at the next wake
        notes = self._notes()
        # The only safe point to move the agent to the current tool set: the new history holds no assistant
        # turn (no thinking block) bound to the old one. `!=`: after a code downgrade it realigns here too.
        upgrade = frozen.current_record(self.language) if self.tools_rev != frozen.TOOLSET_REVISION else None
        added = [n for n in frozen.names(upgrade["tools"]) if n not in self.tool_names] if upgrade else []
        text = (prompts.t(self.language, "compacted", summary=summary)
                + ("\n\n" + prompts.t(self.language, "notes", notes=notes) if notes else "")
                + ("\n\n" + prompts.t(self.language, "tools_added", tools=", ".join(f"`{n}`" for n in added))
                   if added else "")
                + "\n\n" + prompts.t(self.language, "continue"))
        # The newest user-side texts (the wake-up that started this activation, new events) are kept
        # verbatim: a summary written before the agent acted on them may drop what they asked for.
        # (Before _fixed_tokens below: the compaction floor counts the new tool set.)
        epoch = self._replace_history([{"role": "user", "content": text}, *self._tail_to_keep()], tools=upgrade)
        self.swarm.notes_shown(self.name, notes)
        self.last_prompt_tokens = self._compaction_floor = estimate_tokens(self.history) + self._fixed_tokens()
        self.last_output_tokens = 0
        self._len_at_last_call = len(self.history)
        self.swarm.db.execute("UPDATE agents SET context_tokens = ? WHERE name = ?", (self.last_prompt_tokens, self.name))
        self._activity("compacted", summary)
        self.swarm.bus.emit("agent.compacted", self.name, agent=self.name, chars=len(summary),
                            tools_revision=self.tools_rev)
        return epoch

    def _tail_start(self) -> int:
        """Index of the trailing run of user messages (never the summary of an earlier compaction)."""
        index = len(self.history)
        while index > 0 and self.history[index - 1].get("role") == "user":
            index -= 1
        if index == 0 and self.history:
            marker = prompts.t(self.language, "compacted", summary="\0").split("\0")[0]
            if marker and str(self.history[0].get("content") or "").startswith(marker):
                index = 1
        return index

    def _tail_to_keep(self) -> list[dict[str, Any]]:
        budget = min(TAIL_MAX_CHARS, int(self._context_limit() * 0.25 * 3))
        kept: list[dict[str, Any]] = []
        for msg in reversed(self.history[self._tail_start():]):
            content = str(msg.get("content") or "")
            if len(content) > budget:
                if not kept:  # the newest one is kept at least in part
                    kept.append({"role": "user", "content": truncate_middle(content, budget)})
                break
            budget -= len(content)
            kept.append({"role": "user", "content": content})
        return kept[::-1]

    def _fallback_summary(self, reason: str) -> str:
        lines = [f"({reason})"]  # the notes follow the summary (_replace_with_summary)
        digest = self._digest_recent(DIGEST_MAX_CHARS)
        if digest:
            lines += ["Your most recent steps (oldest first):", digest]
        return "\n".join(lines)

    def _digest_recent(self, budget: int) -> str:
        """A short text log of the latest steps before the kept tail, newest kept first within `budget`."""
        lines: list[str] = []
        for msg in reversed(self.history[: self._tail_start()]):
            role = msg.get("role")
            content = " ".join(str(msg.get("content") or "").split())
            if role == "assistant":
                calls = ", ".join(f"{c.get('name')}({truncate_middle(json.dumps(c.get('arguments'), ensure_ascii=False), 200)})"
                                  for c in msg.get("tool_calls") or [])
                line = "You: " + (truncate_middle(content, 600) if content else "") + (f" [called {calls}]" if calls else "")
            elif role == "tool":
                line = f"Result of {msg.get('name')}: {truncate_middle(content, 400)}"
            else:
                line = f"Event: {truncate_middle(content, 400)}"
            if len(line) > budget:
                break
            budget -= len(line) + 1
            lines.append(line)
        return "\n".join(reversed(lines))
