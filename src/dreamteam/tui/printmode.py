"""`dreamteam -p "goal"`: run one goal non-interactively against a hub and report the outcome
(docs/TUI.md §2).

Exit codes: 0 goal achieved, 1 error (or the hub unreachable for LOST_LIMIT seconds), 2 paused by the
budget, 3 timeout, 130 interrupted (Ctrl+C / cancellation; the session is left as it is).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from typing import Any, Callable, TextIO

from ..models import USER
from .client import Connection, HubError

log = logging.getLogger(__name__)

OUTPUT_FORMATS = ("text", "json", "stream-json")
EXIT_CODES = {"done": 0, "error": 1, "budget": 2, "timeout": 3, "interrupted": 130}
LOST_LIMIT = 60.0        # seconds without the hub before giving up
TICK = 0.5               # deadlines are checked at least this often
EVENTS_PAGE = 2000       # /api/events page size when catching up after a (re)connect
FINAL_STATE_TIMEOUT = 5.0
AUTO_REPLY_MAX = 50      # a safety net against an endless question / auto-answer ping-pong (not approvals)
PROGRESS_SKIP = ("agent.status", "agent.usage", "agent.activity", "trace.")  # fallback feed: too noisy

AUTO_REPLY = {
    "ru": "Пользователь недоступен (неинтерактивный режим): прими решение сам и продолжай работу.",
    "en": "The user is not available (non-interactive mode): decide yourself and continue.",
}
AUTO_REVIEW = {
    "ru": "Принято автоматически: пользователь недоступен (неинтерактивный режим), задачу никто не проверял.",
    "en": "Approved automatically: the user is not available (non-interactive mode); nobody reviewed the task.",
}
TEXTS = {
    "ru": {
        "timeout": "Время ожидания истекло ({seconds:g} с): цель не достигнута.",
        "budget": "Рой поставлен на паузу: бюджет исчерпан.",
        "lost": "Хаб недоступен дольше {seconds:g} с.",
        "stream_ended": "Соединение с хабом закрыто.",
        "stopped": "Сессия остановлена.",
        "interrupted": "Прервано.",
        "no_sessions": "Нет сессий, которые можно продолжить.",
        "unknown_session": "Неизвестная сессия «{sid}».",
        "session": "Сессия {sid}: {title}",
        "folder": "Папка сессии: {path}",
        "error": "Ошибка: {error}",
    },
    "en": {
        "timeout": "Timed out after {seconds:g} s: the goal is not achieved.",
        "budget": "The swarm was paused: the budget is exhausted.",
        "lost": "The hub has been unreachable for more than {seconds:g} s.",
        "stream_ended": "The connection to the hub was closed.",
        "stopped": "The session was stopped.",
        "interrupted": "Interrupted.",
        "no_sessions": "There is no session to continue.",
        "unknown_session": "Unknown session '{sid}'.",
        "session": "Session {sid}: {title}",
        "folder": "Session folder: {path}",
        "error": "Error: {error}",
    },
}


def _text(lang: str, key: str, **kw: Any) -> str:
    return TEXTS.get(lang, TEXTS["en"])[key].format(**kw)


class _Finished(Exception):
    def __init__(self, status: str, reason: str = ""):
        super().__init__(reason)
        self.status = status
        self.reason = reason


class _Out:
    """An output stream of the run. A reader that went away (`dreamteam -p ... | head -1`) closes it for
    good: the run goes on and reports its exit code instead of dying of a BrokenPipeError."""

    def __init__(self, stream: TextIO):
        self.stream = stream
        self.closed = False

    def write(self, text: str, fallback: str | None = None) -> None:
        if self.closed:
            return
        try:
            try:
                self.stream.write(text)
            except UnicodeEncodeError:  # a legacy console code page
                if fallback is None:
                    encoding = getattr(self.stream, "encoding", None) or "ascii"
                    fallback = text.encode(encoding, "replace").decode(encoding)
                self.stream.write(fallback)
            self.stream.flush()
        except (OSError, ValueError):  # BrokenPipeError, a closed file
            self._gone()

    def json(self, obj: Any) -> None:
        self.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n",
                   json.dumps(obj, ensure_ascii=True, default=str) + "\n")

    def _gone(self) -> None:
        self.closed = True
        for name in ("stdout", "stderr"):
            if self.stream is getattr(sys, name, None):
                # what the interpreter flushes at exit must not fail on the dead pipe again
                try:
                    devnull = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(devnull, self.stream.fileno())
                    os.close(devnull)
                except (OSError, ValueError, AttributeError):
                    pass


def _can_encode(stream: TextIO, sample: str = "⏺⎿☐◐☒✻") -> bool:
    try:
        sample.encode(getattr(stream, "encoding", None) or "utf-8")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def _summary_line(ev: dict) -> str | None:
    """One plain line about a bus event (the progress feed when render.py is not available)."""
    etype = str(ev.get("type") or "")
    if etype.startswith(PROGRESS_SKIP) or etype.startswith("_"):
        return None
    data = ev.get("data") or {}
    detail = ""
    if isinstance(data.get("message"), dict):
        msg = data["message"]
        detail = f"→ {', '.join(map(str, msg.get('recipients') or []))}: {msg.get('text', '')}"
    elif isinstance(data.get("task"), dict):
        task = data["task"]
        detail = f"#{task.get('id')} {task.get('title', '')} [{task.get('status', '')}]"
    elif isinstance(data.get("goal"), dict):
        goal = data["goal"]
        detail = goal.get("summary") or goal.get("title") or ""
    elif data.get("text"):
        detail = str(data["text"])
    elif data.get("reason"):
        detail = str(data["reason"])
    line = f"· {etype}" + (f" {ev['actor']}" if ev.get("actor") else "") + (f": {detail}" if detail else "")
    line = " ".join(line.split())
    return line if len(line) <= 160 else line[:159] + "…"


class _Progress:
    """The --verbose feed: render.event() at verbosity "normal" as plain text, with every tool result under
    its own call (render.ToolPairs); a one-line summary when rendering fails on an event."""

    def __init__(self, lang: str, width: int, unicode: bool):
        from .render import RenderContext, ToolPairs

        self.ctx = RenderContext(lang=lang, verbosity="normal", width=width, unicode=unicode)
        self.width = width
        self.tools = ToolPairs()

    def _text(self, renderables: list) -> list[str]:
        from .render import to_plain

        out = []
        for renderable in renderables:
            if renderable is not None:
                self.tools.printed(renderable)
                out.append(to_plain(renderable, self.width).rstrip("\n"))
        return [text for text in out if text]

    def feed(self, ev: dict) -> list[str]:
        from .render import event

        try:
            lines = self.tools.feed(self.ctx, ev)
            return self._text([event(self.ctx, ev)] if lines is None else lines)
        except Exception:
            log.debug("render failed for %s", ev.get("type"), exc_info=True)
            line = _summary_line(ev)
            return [line] if line else []

    def due(self) -> list[str]:
        try:
            return self._text(self.tools.due(self.ctx))
        except Exception:
            log.debug("render failed", exc_info=True)
            return []


def _coordinator(state: dict) -> str | None:
    """Who gets the prompt in an existing session: the planning task's assignee, else the first LLM agent."""
    agents = [a for a in state.get("agents") or [] if a.get("status") != "retired"]
    names = {a.get("name") for a in agents}
    for task in state.get("tasks") or []:
        if "planning" in (task.get("tags") or []) and task.get("assignee") in names:
            return task["assignee"]
    return next((a["name"] for a in agents if a.get("kind") == "llm"), None)


class _PrintRun:
    def __init__(self, conn: Connection, prompt: str, *, session: str | None, continue_last: bool,
                 title: str | None, overrides: dict | None, output_format: str, verbose: bool,
                 timeout: float | None, auto_reply: bool, lang: str | None, out: TextIO, err: TextIO,
                 on_session: Callable[[str], Any] | None = None, workspace: str | None = None):
        self.client = conn.client
        self.workspace = workspace
        self.on_session = on_session
        self.prompt = prompt
        self.session = session
        self.continue_last = continue_last
        self.title = title
        self.overrides = overrides
        self.format = output_format
        self.verbose = verbose
        self.timeout = timeout
        self.auto_reply = auto_reply
        self.ui_lang = lang
        self.lang = lang or "ru"            # the session's language once known (auto-replies)
        self.out, self.err = _Out(out), _Out(err)
        self.sid: str | None = None
        self.state: dict | None = None
        self.goal: dict | None = None
        self.last_event = 0                 # newest journaled event id already handled
        self.seen_messages: set[int] = set()
        self.auto_reviewed: set[tuple[int, Any]] = set()
        self.replies = 0
        self.started = time.monotonic()
        self._progress: _Progress | None = None

    # ---- output ---------------------------------------------------------------------------------
    @property
    def text_lang(self) -> str:
        return self.ui_lang or self.lang

    def progress(self, text: str) -> None:
        if self.verbose and self.format != "stream-json" and text:
            if self._progress is not None:
                self._progress.tools.printed(None)  # a line of its own: a held call's result needs a header
            self.err.write(text + "\n")

    def emit(self, ev: dict) -> None:
        if self.format == "stream-json":
            self.out.json({"type": ev.get("type"), "ts": ev.get("ts"), "actor": ev.get("actor"),
                           "data": ev.get("data")})
        elif self.verbose:
            for line in self.feed().feed(ev):
                self.err.write(line + "\n")

    def feed(self) -> _Progress:
        if self._progress is None:
            width = max(40, min(shutil.get_terminal_size((100, 24)).columns, 160))
            self._progress = _Progress(self.text_lang, width, _can_encode(self.err.stream))
        return self._progress

    def flush_progress(self) -> None:
        """Tool calls that run long are printed alone (their results follow)."""
        if self.verbose and self.format != "stream-json" and self._progress is not None:
            for line in self._progress.due():
                self.err.write(line + "\n")

    def started_session(self, sid: str) -> None:
        self.sid = sid
        if self.on_session is not None:
            self.on_session(sid)

    # ---- setup ----------------------------------------------------------------------------------
    async def prepare(self) -> None:
        client = self.client
        if not client.mode:
            await client.hub_info()
        if client.mode != "launcher":
            await self.attach(None)
            return
        if not (self.session or self.continue_last):
            title = self.title or ""  # "": the hub derives it from the goal's first line
            summary = await client.create_session(title, self.prompt, overrides=self.overrides, start=True,
                                                  workspace=self.workspace)
            self.started_session(summary["id"])
            self.lang = summary.get("language") or self.lang
            self.progress(_text(self.text_lang, "session", sid=self.sid, title=summary.get("title", "")))
            if self.workspace:
                folder = (summary.get("workspace_result") or {}).get("to") or self.workspace
                self.progress(_text(self.text_lang, "folder", path=folder))
            return
        sessions = await client.sessions()
        if self.session:
            summary = next((s for s in sessions if s.get("id") == self.session), None)
            if summary is None:
                raise _Finished("error", _text(self.text_lang, "unknown_session", sid=self.session))
        else:
            if not sessions:
                raise _Finished("error", _text(self.text_lang, "no_sessions"))
            summary = max(sessions, key=lambda s: s.get("last_activity") or s.get("created_at") or 0)
        if summary.get("status") != "running":
            summary = await client.start_session(summary["id"])
        self.started_session(summary["id"])
        self.lang = summary.get("language") or self.lang
        self.progress(_text(self.text_lang, "session", sid=summary["id"], title=summary.get("title", "")))
        await self.attach(summary["id"])

    async def attach(self, sid: str | None) -> None:
        """An existing session: the prompt becomes the goal when there is none, reopens a completed goal,
        else goes to the coordinator as a message. Only what happens from now on is followed."""
        client = self.client
        self.sid = sid
        latest = await client.events(sid, limit=1)
        self.last_event = max([e.get("id") or 0 for e in latest], default=0)
        state = await client.state(sid)
        self.state = state
        self.lang = (state.get("swarm") or {}).get("language") or self.lang
        for task in state.get("tasks") or []:  # reviews that were already waiting for the user
            if isinstance(task, dict):
                await self.on_task(task)
        goal = (state.get("swarm") or {}).get("goal")
        if not goal:
            ok, text = await client.act(sid, "set_goal", {"title": self.prompt})
        elif goal.get("status") == "done":
            ok, text = await client.act(sid, "swarm_control", {"reopen_goal_comment": self.prompt})
        else:
            to = _coordinator(state) or "all"
            ok, text = await client.act(sid, "send_message", {"to": [to], "text": self.prompt})
        if not ok:
            raise _Finished("error", _text(self.text_lang, "error", error=text))
        swarm = state.get("swarm") or {}
        if swarm.get("paused") and swarm.get("pause_reason") != "budget":
            # paused by the user: the new instruction is meant to be worked on (a budget pause ends the run)
            ok, text = await client.act(sid, "swarm_control", {"action": "resume"})
            if not ok:
                raise _Finished("error", _text(self.text_lang, "error", error=text))

    # ---- following ------------------------------------------------------------------------------
    def check_deadline(self) -> None:
        if self.timeout is not None and time.monotonic() - self.started >= self.timeout:
            raise _Finished("timeout", _text(self.text_lang, "timeout", seconds=self.timeout))

    def remaining(self) -> float | None:
        if self.timeout is None:
            return None
        return max(0.0, self.timeout - (time.monotonic() - self.started))

    async def follow(self) -> None:
        queue: asyncio.Queue = asyncio.Queue()

        async def pump() -> None:
            events = self.client.stream(self.sid)
            try:
                async for ev in events:
                    queue.put_nowait(ev)
            finally:
                await events.aclose()

        pump_task = asyncio.create_task(pump(), name="dreamteam-print-stream")
        lost_since: float | None = time.monotonic()  # not connected yet
        try:
            while True:
                self.check_deadline()
                if lost_since is not None and time.monotonic() - lost_since >= LOST_LIMIT:
                    raise _Finished("error", _text(self.text_lang, "lost", seconds=LOST_LIMIT))
                if pump_task.done() and queue.empty():
                    error = None if pump_task.cancelled() else pump_task.exception()
                    raise _Finished("error", str(error) if error else _text(self.text_lang, "stream_ended"))
                try:
                    ev = queue.get_nowait()
                except asyncio.QueueEmpty:
                    self.flush_progress()
                    remaining = self.remaining()
                    try:
                        async with asyncio.timeout(TICK if remaining is None else min(TICK, remaining)):
                            ev = await queue.get()
                    except TimeoutError:
                        continue
                if ev.get("type") == "_connection":
                    state = (ev.get("data") or {}).get("state")
                    if state == "connected":
                        lost_since = None
                        await self.bounded(self.catch_up())
                    else:
                        if lost_since is None:
                            lost_since = time.monotonic()
                        if state == "rejected":
                            await self.bounded(self.check_session())
                    continue
                await self.bounded(self.on_event(ev))
        finally:
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)

    async def bounded(self, coro) -> None:
        """Run a step of the follow loop within the timeout; a hub error there is transient (the stream
        reports a lost connection)."""
        try:
            async with asyncio.timeout(self.remaining()):
                await coro
        except TimeoutError:
            self.check_deadline()
        except HubError as exc:
            log.debug("print mode: %s", exc)

    async def check_session(self) -> None:
        """The hub refused the stream: a session that no longer exists ends the run."""
        if self.sid is None:
            return
        try:
            await self.client.state(self.sid)
        except HubError as exc:
            if exc.status == 404:
                raise _Finished("error", _text(self.text_lang, "unknown_session", sid=self.sid)) from None
            raise

    async def catch_up(self) -> None:
        """After a (re)connect: the journaled events missed meanwhile, then the state (a goal completed or a
        budget pause before the stream connected)."""
        await self.replay()
        state = await self.client.state(self.sid)
        self.state = state
        swarm = state.get("swarm") or {}
        goal = swarm.get("goal")
        stopped = (swarm.get("session") or {}).get("status") == "stopped"
        if (goal and goal.get("status") == "done") or swarm.get("paused") or stopped:
            # it happened while the page above was read: the events that led there (journaled before the
            # state changed) are reported first; goal.completed / a budget pause end the run in on_event
            await self.replay()
        if goal and goal.get("status") == "done":
            self.goal = goal
            raise _Finished("done")
        if swarm.get("paused") and swarm.get("pause_reason") == "budget":
            raise _Finished("budget", _text(self.text_lang, "budget"))
        if stopped:
            raise _Finished("error", _text(self.text_lang, "stopped"))

    async def replay(self) -> None:
        """Handle the journaled events newer than the last one handled."""
        while True:
            page = await self.client.events(self.sid, since=self.last_event, limit=EVENTS_PAGE)
            for ev in page:
                await self.on_event(ev)
            if len(page) < EVENTS_PAGE:
                break

    async def on_event(self, ev: dict) -> None:
        event_id = ev.get("id")
        if isinstance(event_id, int):
            if event_id <= self.last_event:
                return  # already handled (caught up over REST)
            self.last_event = event_id
        self.emit(ev)
        etype = ev.get("type")
        data = ev.get("data") or {}
        if etype == "message.sent" and isinstance(data.get("message"), dict):
            await self.on_message(data["message"])
        elif etype == "task.updated" and isinstance(data.get("task"), dict):
            await self.on_task(data["task"])
        elif etype == "goal.completed":
            self.goal = data.get("goal") if isinstance(data.get("goal"), dict) else self.goal
            raise _Finished("done")
        elif etype == "swarm.paused" and data.get("reason") == "budget":
            raise _Finished("budget", _text(self.text_lang, "budget"))

    async def on_message(self, msg: dict) -> None:
        mid = msg.get("id")
        if not isinstance(mid, int) or mid in self.seen_messages:
            return
        self.seen_messages.add(mid)
        sender = msg.get("sender")
        if sender == USER or USER not in (msg.get("recipients") or []):
            return
        if not self.auto_reply or self.replies >= AUTO_REPLY_MAX:
            return
        self.replies += 1
        ok, text = await self.client.act(self.sid, "send_message", {
            "to": [sender], "text": AUTO_REPLY.get(self.lang, AUTO_REPLY["en"]), "reply_to": mid})
        if not ok:
            log.warning("auto-reply to %s failed: %s", sender, text)

    async def on_task(self, task: dict) -> None:
        """A task waiting for the user's review would block the run: with auto-replies on, approve it (the
        counterpart of AUTO_REPLY for questions; --no-auto-reply leaves it waiting). Approvals do not count
        against AUTO_REPLY_MAX: an approval settles the task (no ping-pong), and a one-agent run sends every
        task, the planning task included, to the user."""
        if task.get("status") != "review" or task.get("reviewer") != USER or not isinstance(task.get("id"), int):
            return
        key = (task["id"], task.get("updated_at"))
        if key in self.auto_reviewed or not self.auto_reply:
            return
        self.auto_reviewed.add(key)
        ok, text = await self.client.act(self.sid, "task_review", {
            "task_id": task["id"], "approve": True, "comment": AUTO_REVIEW.get(self.lang, AUTO_REVIEW["en"])})
        if not ok:
            log.warning("auto-approval of task #%s failed: %s", task["id"], text)

    # ---- result ---------------------------------------------------------------------------------
    async def report(self, status: str, reason: str) -> int:
        state = self.state
        if self.sid is not None or self.client.mode == "swarm":
            try:
                state = await asyncio.wait_for(self.client.state(self.sid), FINAL_STATE_TIMEOUT)
            except (HubError, TimeoutError):
                pass
        swarm = (state or {}).get("swarm") or {}
        goal = swarm.get("goal") or self.goal
        if status == "done" and self.goal and (goal or {}).get("status") != "done":
            goal = self.goal  # the state may already be newer (e.g. reopened)
        summary = (goal or {}).get("summary", "") if status == "done" else ""
        result: dict[str, Any] = {
            "type": "result", "status": status, "session": self.sid, "goal": goal, "summary": summary,
            "tasks": [{"id": t.get("id"), "title": t.get("title"), "status": t.get("status"),
                       "assignee": t.get("assignee")} for t in (state or {}).get("tasks") or []],
            "cost_usd": float(swarm.get("spent_usd") or 0.0),
            "duration_s": round(time.monotonic() - self.started, 3),
            "agents": [a.get("name") for a in (state or {}).get("agents") or [] if a.get("status") != "retired"],
        }
        if status != "done" and reason:
            result["error"] = reason
        if self.format == "text":
            if status == "done":
                self.out.write((summary or (goal or {}).get("title") or "").rstrip() + "\n")
            else:
                self.err.write((reason or status) + "\n")
        else:
            self.out.json(result)
        return EXIT_CODES[status]


async def run_print(conn: Connection, prompt: str, *, session: str | None = None, continue_last: bool = False,
                    title: str | None = None, overrides: dict | None = None, output_format: str = "text",
                    verbose: bool = False, timeout: float | None = None, auto_reply: bool = True,
                    lang: str | None = None, out: TextIO | None = None, err: TextIO | None = None,
                    on_session: Callable[[str], Any] | None = None, workspace: str | None = None) -> int:
    """Run `prompt` as a goal (a new session), or as the next instruction to an existing session
    (`session` / `continue_last`), follow the session until it ends and print the result.

    `out` / `err` default to sys.stdout / sys.stderr at call time; `lang` sets the language of the
    error lines and the progress feed (default: the session's language). `on_session(sid)` is called with
    the session the run works in, once it is created or started (main_print stops it at the end). `workspace`:
    the absolute folder a new session works in (docs/SPEC.md §17.2: an empty or missing folder gets the files, one
    with files is adopted; a git repository with uncommitted changes is refused, an error)."""
    if output_format not in OUTPUT_FORMATS:
        raise ValueError(f"output_format must be one of {', '.join(OUTPUT_FORMATS)}")
    run = _PrintRun(conn, prompt, session=session, continue_last=continue_last, title=title, overrides=overrides,
                    output_format=output_format, verbose=verbose, timeout=timeout, auto_reply=auto_reply,
                    lang=lang, out=out or sys.stdout, err=err or sys.stderr, on_session=on_session,
                    workspace=workspace)
    status, reason = "error", ""
    try:
        try:
            async with asyncio.timeout(timeout):
                await run.prepare()
            await run.follow()
        except _Finished as fin:
            status, reason = fin.status, fin.reason
        except HubError as exc:
            status, reason = "error", _text(run.text_lang, "error", error=exc.message)
        except TimeoutError as exc:
            if timeout is None:
                status, reason = "error", _text(run.text_lang, "error", error=str(exc) or "timeout")
            else:
                status, reason = "timeout", _text(run.text_lang, "timeout", seconds=timeout)
        except Exception as exc:  # an unexpected answer: still report a result
            log.exception("print mode failed")
            status, reason = "error", _text(run.text_lang, "error", error=f"{type(exc).__name__}: {exc}")
    except (asyncio.CancelledError, KeyboardInterrupt) as exc:
        if isinstance(exc, asyncio.CancelledError):
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()  # Ctrl+C under asyncio.run cancels the main task: report and exit 130
        status, reason = "interrupted", _text(run.text_lang, "interrupted")
    return await run.report(status, reason)


__all__ = ["run_print", "AUTO_REPLY", "AUTO_REVIEW", "EXIT_CODES", "OUTPUT_FORMATS"]
