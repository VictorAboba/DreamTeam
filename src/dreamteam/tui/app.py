"""The interactive terminal UI (docs/TUI.md §4).

A prompt_toolkit Application (not full screen) whose layout is only the live bottom area: the spinner
line, the rounded input box (or a dialog in its place), the status line and the suggestions. Everything
else (welcome, events, command output) is rendered with rich and printed above it through
`run_in_terminal`, so the bottom area is redrawn under the new output. The swarm is followed over the
hub's WebSocket stream; the TUI never touches a database."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import time
import webbrowser
from pathlib import Path
from typing import Any, Callable, Coroutine

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, FormattedText, to_formatted_text
from prompt_toolkit.history import FileHistory, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.processors import AfterInput, ConditionalProcessor
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style

from .. import __version__
from ..models import USER
from . import commands, render, theme
from .bash import ShellCommand
from .client import Connection, HubError, HubUnreachable
from .dialogs import (
    APPROVE, REJECT, ConfirmDialog, Dialog, MCPApprovalDialog, SessionPicker, ascii_text, border_chars, fit,
    frag_len,
)
from .i18n import LANGS, t
from .input import (
    ALL, EXPLICIT_ADDRESSEE_SECONDS, Suggestion, bash_command, default_addressee, history_path, is_bash,
    load_prefs, members_of, parse_command, save_prefs, split_mention, suggest, window,
)

log = logging.getLogger(__name__)

SPINNER_TICK = 0.12          # spinner frame period (only while agents work)
VERB_SECONDS = 8.0           # the spinner verb changes this often
REFRESH_MIN = 2.0            # /api/state at most this often...
REFRESH_IDLE = 15.0          # ...and at least this often while attached
HINT_SECONDS = 2.0           # "press Ctrl+C again" window
LOST_GRACE = 1.5             # a reconnect within this is not reported (1012 after a session restart)
MAX_INPUT_LINES = 10
BASH_TIMEOUT = 120.0
HISTORY_MESSAGES = 6         # user <-> agent messages shown when attaching
SHUTDOWN_ROUNDS = 3
SHUTDOWN_WAIT = 2.0
CATCHUP_PAGE = 500
CATCHUP_PAGES = 4
MCP_WAIT_POLL = 0.25         # the approval dialog waits for an empty input box this often
BASH_HINT_CHARS = 40
# what is replayed after a reconnect (activity, usage and traces of the gap are skipped)
CATCHUP_TYPES = ["message.*", "notification", "goal.*", "task.*", "agent.hired", "agent.retired", "swarm.*",
                 "mcp.*"]
REFRESH_ON = ("goal.", "swarm.", "task.", "mcp.", "agent.hired", "agent.retired")


def _same_path(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return False


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _runs(text: ANSI) -> list[tuple[str, str]]:
    """The fragments of parsed ANSI text with equal styles merged (the parser yields one per character, and
    every fragment costs an escape sequence on the terminal)."""
    runs: list[tuple[str, list[str]]] = []
    for style, chars, *_ in to_formatted_text(text):
        if runs and runs[-1][0] == style:
            runs[-1][1].append(chars)
        else:
            runs.append((style, [chars]))
    return [(style, "".join(parts)) for style, parts in runs]


def _log_loop_error(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    log.error("unhandled error in the event loop: %s", context.get("message"), exc_info=context.get("exception"))


class _History(FileHistory):
    """The input history file; one that cannot be read or written (read-only, locked) only costs the
    history, never the input: the entry stays in memory."""

    def load_history_strings(self) -> Any:
        try:
            return list(super().load_history_strings())
        except OSError as exc:
            log.warning("cannot read the input history: %s", exc)
            return []

    def store_string(self, string: str) -> None:
        try:
            super().store_string(string)
        except OSError as exc:
            log.warning("cannot write the input history: %s", exc)


class TUI:
    """The application: view state, bottom-area layout, key bindings and the event loop of one hub."""

    def __init__(self, conn: Connection, *, lang: str | None = None, session: str | None = None,
                 continue_last: bool = False, initial_prompt: str | None = None, verbosity: str = "normal",
                 overrides: dict | None = None, input: Any = None, output: Any = None,
                 printer: Callable[[Any], None] | None = None):
        self.conn = conn
        self.client = conn.client
        self.home = Path(conn.home)
        prefs = load_prefs(self.home)
        self._lang_fixed = lang in LANGS or prefs.get("lang") in LANGS
        start_lang = lang if lang in LANGS else prefs.get("lang") if prefs.get("lang") in LANGS else "ru"
        self.session_arg = session
        self.continue_last = continue_last
        self.initial_prompt = initial_prompt
        self.overrides, self.title_override = commands.session_overrides(overrides)
        self._printer = printer
        self._real_terminal = output is None
        self.ctx = render.RenderContext(lang=start_lang,
                                        verbosity=verbosity if verbosity in render.VERBOSITY else "normal")
        self.hub: dict = {}
        self.local = bool(conn.owned)
        self.dialog: Dialog | None = None
        self.input_mode: tuple[str, Any] | None = None   # ("goal", None) | ("reject", server name)
        self.suggestions: list[Suggestion] = []
        self.selected = 0
        self.hint: str | None = None
        self.frame = 0
        self.ready = asyncio.Event()
        self._dismissed: str | None = None
        self._hint_handle: asyncio.TimerHandle | None = None
        self._ctrl_c_at = -1e9
        self._exiting = False
        self._out: list[str] = []
        self._tasks: set[asyncio.Task] = set()
        self._flush_task: asyncio.Task | None = None
        self._ticker: asyncio.Task | None = None
        self._refresher: asyncio.Task | None = None
        self._stream_task: asyncio.Task | None = None
        self._mcp_worker: asyncio.Task | None = None
        self._input_lock = asyncio.Lock()
        self._dialog_lock = asyncio.Lock()
        self._refresh_wanted = asyncio.Event()
        self._generation = 0          # +1 on every attach: answers for an earlier view are not applied
        self._bash: ShellCommand | None = None
        self._mcp_waiting: str | None = None   # a pending server whose dialog waits for an empty input
        self.tools = render.ToolPairs()
        self._tools_timer: asyncio.TimerHandle | None = None
        self._reset_session()
        self.app = self._build_app(input, output)
        self.ctx.unicode = theme.can_encode(self._encoding())
        self.ctx.width = self.width

    # ---- session view state ------------------------------------------------------------------------
    def _reset_session(self) -> None:
        self.sid: str | None = None
        self.attached = False
        self.session: dict | None = None
        self.state: dict | None = None
        self.last_event = 0           # the newest event id of the live stream (or the baseline)
        self._replayed: set[int] = set()        # newer ids already handled by a catch-up
        self._shown_messages: set[int] = set()  # message ids printed as history at the attach
        self.tools.reset()
        self.working: set[str] = set()
        self.work_since = time.monotonic()
        self.tokens_in = self.tokens_out = 0
        self.work_cost = 0.0
        self.paused = False
        self.spent = 0.0
        self.budget = 0.0
        self.last_sender: str | None = None
        self._addressee: str | None = None
        self._addressee_at: float | None = None
        self.mcp_queue: list[dict] = []
        self.mcp_answered: set[str] = set()   # not asked about again until an agent re-creates them
        self._mcp_dialog: MCPApprovalDialog | None = None
        self._reject_waiter: asyncio.Future | None = None
        self._echo_pending: list[str] = []
        self.connected = True
        self._first_connect = True
        self._lost_handle: asyncio.TimerHandle | None = None
        self._shell_program: str | None = None
        self._verb_offset = random.randrange(64)
        self._last_refresh = 0.0

    @property
    def members(self) -> list[str]:
        return members_of(self.state)

    @property
    def addressee(self) -> str | None:
        """Who plain text goes to: the user's sticky choice, else the default (docs/TUI.md §4.2)."""
        if not self.attached:
            return None
        if self._addressee and (self._addressee == ALL or self._addressee in self.members):
            return self._addressee
        return default_addressee(self.state, self.last_sender)

    @property
    def width(self) -> int:
        try:
            return max(20, int(self.app.output.get_size().columns))
        except Exception:
            return 100

    def _encoding(self) -> str | None:
        try:
            return self.app.output.encoding()
        except Exception:
            return None

    def set_addressee(self, name: str) -> None:
        self._addressee = name
        self._addressee_at = time.monotonic()
        self.invalidate()

    # ---- output ------------------------------------------------------------------------------------
    def print(self, renderable: Any) -> None:
        """Print a rich renderable above the bottom area (or hand it to the injected printer)."""
        if renderable is None:
            return
        self.tools.printed(renderable)
        if self._printer is not None:
            try:
                self._printer(renderable)
            except Exception:
                log.exception("printer failed")
            return
        self._out.append(render.to_ansi(renderable, max(20, self.width - 1)))
        if not self.app.is_running:
            self._write_out()
        elif not self._exiting and (self._flush_task is None or self._flush_task.done()):
            self._flush_task = self._spawn(self._flush())
        # while the app is closing, run() prints what is left once the bottom area is gone

    def info(self, text: str) -> None:
        self.print(render.info(self.ctx, text))

    def error(self, text: str) -> None:
        self.print(render.error(self.ctx, text))

    def hub_error(self, exc: HubError) -> str:
        """The text of a failed hub call; a hub that cannot be reached gets a clear, translated one."""
        if isinstance(exc, HubUnreachable):
            return self.t("hub_unreachable", url=self.client.base_url)
        return exc.message

    def t(self, key: str, **kw: Any) -> str:
        return t(self.ctx.lang, key, **kw)

    async def _flush(self) -> None:
        await asyncio.sleep(0)  # one redraw for everything printed in this loop iteration
        while self._out:
            await run_in_terminal(self._write_out)

    def _write_out(self) -> None:
        chunks, self._out = self._out, []
        if not chunks:
            return
        try:
            print_formatted_text(FormattedText(_runs(ANSI("\n".join(chunks)))), output=self.app.output)
        except Exception:
            log.exception("cannot print to the terminal")

    def invalidate(self) -> None:
        if self.app.is_running:
            self.app.invalidate()

    def set_hint(self, text: str | None, seconds: float | None = None) -> None:
        """A transient text in the status line (`seconds` None: until replaced)."""
        if self._hint_handle is not None:
            self._hint_handle.cancel()
            self._hint_handle = None
        self.hint = text
        if text and seconds:
            def clear() -> None:
                if self.hint == text:
                    self.hint = None
                    self.invalidate()

            self._hint_handle = asyncio.get_running_loop().call_later(seconds, clear)
        self.invalidate()

    # ---- tasks -------------------------------------------------------------------------------------
    def _spawn(self, coro: Coroutine, name: str | None = None) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        if name:
            task.set_name(name)
        self._tasks.add(task)

        def done(task: asyncio.Task) -> None:
            self._tasks.discard(task)
            if not task.cancelled() and task.exception() is not None:
                log.error("TUI task failed", exc_info=task.exception())

        task.add_done_callback(done)
        return task

    async def _cancel(self, task: asyncio.Task | None) -> None:
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    # ---- layout ------------------------------------------------------------------------------------
    def _build_app(self, input: Any, output: Any) -> Application:
        try:
            history: Any = _History(str(history_path(self.home)))
        except OSError:
            history = InMemoryHistory()
        self.buffer = Buffer(multiline=True, history=history, on_text_changed=self._on_text_changed,
                             name="input")
        placeholder = ConditionalProcessor(AfterInput(self._placeholder, style="class:placeholder"),
                                           filter=Condition(lambda: not self.buffer.text))
        self.input_window = Window(BufferControl(self.buffer, input_processors=[placeholder]),
                                   height=Dimension(min=1, max=MAX_INPUT_LINES), wrap_lines=True,
                                   get_line_prefix=self._line_prefix, dont_extend_height=True)

        def char(key: str) -> Callable[[], str]:
            return lambda: border_chars(self.ctx.unicode)[key]

        border = self._border_style
        top = VSplit([Window(FormattedTextControl(self._box_top), height=1, dont_extend_width=True),
                      Window(height=1, char=char("h"), style=border),
                      Window(width=1, height=1, char=char("tr"), style=border)])
        middle = VSplit([Window(width=1, char=char("v"), style=border), Window(width=1), self.input_window,
                         Window(width=1), Window(width=1, char=char("v"), style=border)])
        bottom = VSplit([Window(width=1, height=1, char=char("bl"), style=border),
                         Window(height=1, char=char("h"), style=border),
                         Window(width=1, height=1, char=char("br"), style=border)])
        in_dialog = Condition(lambda: self.dialog is not None)
        self.dialog_window = Window(FormattedTextControl(self._dialog_text, focusable=True, show_cursor=False),
                                    dont_extend_height=True)
        root = HSplit([
            ConditionalContainer(Window(FormattedTextControl(self._spinner_text), height=1),
                                 filter=Condition(self.spinner_visible)),
            ConditionalContainer(HSplit([top, middle, bottom]), filter=~in_dialog),
            ConditionalContainer(self.dialog_window, filter=in_dialog),
            Window(FormattedTextControl(self._status_text), height=1),
            ConditionalContainer(Window(FormattedTextControl(self._suggestions_text), dont_extend_height=True),
                                 filter=Condition(lambda: bool(self.suggestions) and self.dialog is None)),
        ])
        app: Application = Application(
            layout=Layout(root, focused_element=self.input_window), key_bindings=self._key_bindings(),
            style=Style.from_dict(theme.PT_STYLE), full_screen=False, erase_when_done=True, mouse_support=False,
            include_default_pygments_style=False, input=input, output=output)
        app.timeoutlen = 0.3     # a lone Esc acts after this (Alt+Enter arrives as Esc + Enter at once)
        app.ttimeoutlen = 0.05
        return app

    def _text(self, text: str) -> str:
        return text if self.ctx.unicode else ascii_text(text)

    def _border_style(self) -> str:
        if self.dialog is not None:
            return "class:box.border.dialog"
        if is_bash(self.buffer.text):
            return "class:box.border.bash"
        return "class:box.border"

    def box_title(self) -> str:
        """The input box's title: the addressee («→ Анна Смирнова») or «новая сессия»."""
        if self.input_mode is not None:
            kind, name = self.input_mode
            return f"MCP {name}" if kind == "reject" else self.t("box_new_session")
        if not self.attached:
            return self.t("box_new_session")
        name = self.addressee or ALL
        return f"{self.ctx.sym['arrow']} {self.t('everyone') if name == ALL else name}"

    def _box_top(self) -> list[tuple[str, str]]:
        c = border_chars(self.ctx.unicode)
        style = self._border_style()
        title = fit([("class:box.title", self._text(self.box_title()))], max(4, self.width - 8))
        title = [(s, x.rstrip()) for s, x in title if x.strip()]
        return [(style, c["tl"] + c["h"] + " "), *title, (style, " ")]

    def _line_prefix(self, line: int, wrap_count: int) -> list[tuple[str, str]]:
        if line == 0 and wrap_count == 0:
            return [("class:prompt.bash" if is_bash(self.buffer.text) else "class:prompt", "> ")]
        return [("", "  ")]

    def _placeholder(self) -> str:
        if self.input_mode is not None:
            key = "ph_goal" if self.input_mode[0] == "goal" else "mcp_reject_reason"
        else:
            key = "ph_session" if self.attached else "ph_no_session"
        text = self._text(self.t(key))
        # one line, like Claude Code: the box's borders and padding take 4 cells, the prompt 2
        room = max(4, self.width - 7)
        if render.cells(text) > room:
            ell = "…" if self.ctx.unicode else "..."
            text = render.cut_cells(text, room - len(ell)).rstrip() + ell
        return text

    def spinner_visible(self) -> bool:
        return self.attached and (bool(self.working) or self.paused)

    def _spinner_text(self) -> list[tuple[str, str]]:
        if not self.spinner_visible():
            return []
        self.ctx.width = self.width
        verbs = render.spinner_verbs(self.ctx.lang)
        elapsed = time.monotonic() - self.work_since if self.working else 0.0
        verb = verbs[(self._verb_offset + int(elapsed // VERB_SECONDS)) % len(verbs)]
        return render.spinner_line(self.ctx, frame=self.frame, verb=verb, working=sorted(self.working),
                                   elapsed=elapsed, tokens_in=self.tokens_in, tokens_out=self.tokens_out,
                                   cost=self.work_cost, paused=self.paused)

    def _status_text(self) -> list[tuple[str, str]]:
        width = self.width
        self.ctx.width = width
        session = None
        if self.attached:
            session = {"id": self.sid, "title": (self.session or {}).get("title") or self.sid or "?"}
        hint = self.hint
        if hint is None and self._bash is not None:
            command = " ".join(self._bash.command.split())
            if len(command) > BASH_HINT_CHARS:
                command = command[:BASH_HINT_CHARS - 1] + "…"
            hint = self._text(self.t("bash_running", command=command))
        elif hint is None and self._mcp_waiting:
            hint = self._text(self.t("mcp_waiting", name=self._mcp_waiting))
        left, right = render.status_line(
            self.ctx, session=session, addressee=self.addressee, agents_total=len(self.members),
            working=len(self.working), spent=self.spent, budget=self.budget,
            model=(self.session or {}).get("model"), verbosity=self.ctx.verbosity, connected=self.connected,
            hint=hint)
        gap = max(2, width - 4 - frag_len(left) - frag_len(right))
        return [("", "  "), *left, ("", " " * gap), *right]

    def _suggestions_text(self) -> list[tuple[str, str]]:
        items = self.suggestions
        if not items or self.dialog is not None:
            return []
        rows = window(len(items), self.selected)
        label_width = min(28, max(len(items[i].label) for i in rows))
        out: list[tuple[str, str]] = []
        for n, i in enumerate(rows):
            s = items[i]
            chosen = i == self.selected
            line = [("", "  "),
                    ("class:suggestion.selected" if chosen else "class:suggestion.name",
                     self._text(s.label).ljust(label_width))]
            if s.description:
                line += [("", "  "), ("class:suggestion.desc", self._text(s.description))]
            if n:
                out.append(("", "\n"))
            out += [(st, x) for st, x in fit(line, self.width - 1) if x]
        return out

    def _dialog_text(self) -> list[tuple[str, str]]:
        if self.dialog is None:
            return []
        return self.dialog.fragments(self.width, self.ctx.unicode)

    # ---- suggestions -------------------------------------------------------------------------------
    def _on_text_changed(self, _buffer: Buffer) -> None:
        self.selected = 0
        self._update_suggestions()

    def _update_suggestions(self) -> None:
        text = self.buffer.text
        if self.dialog is not None or self.input_mode is not None or (
                self._dismissed is not None and self._dismissed == text):
            self.suggestions = []
            return
        self._dismissed = None
        found = suggest(text, self.buffer.cursor_position, commands=commands.infos(self.ctx.lang),
                        members=self.members, tasks=(self.state or {}).get("tasks") or [],
                        everyone=self.t("everyone"), status=self._task_status)
        if not self.attached:
            found = [s for s in found if s.kind == "command"]
        self.suggestions = found
        self.selected = min(self.selected, max(0, len(found) - 1))

    def _task_status(self, status: str) -> str:
        label = self.t("s_" + status)
        return status if label == "s_" + status else label.lower()

    def _apply_suggestion(self, suggestion: Suggestion) -> None:
        text, cursor = suggestion.apply(self.buffer.text)
        self.buffer.document = Document(text, cursor)

    # ---- keys --------------------------------------------------------------------------------------
    def _key_bindings(self) -> KeyBindings:
        kb = KeyBindings()
        in_dialog = Condition(lambda: self.dialog is not None)
        typing = ~in_dialog
        suggesting = Condition(lambda: self.dialog is None and bool(self.suggestions))
        empty = Condition(lambda: not self.buffer.text and self.input_mode is None)

        @kb.add("enter", filter=typing)
        def _enter(event) -> None:
            self._on_enter()

        @kb.add("escape", "enter", filter=typing)
        @kb.add("c-j", filter=typing)
        def _newline(event) -> None:
            self.buffer.insert_text("\n")

        @kb.add("tab", filter=suggesting)
        def _tab(event) -> None:
            self._apply_suggestion(self.suggestions[self.selected])

        @kb.add("up", filter=suggesting)
        def _up(event) -> None:
            self.selected = (self.selected - 1) % len(self.suggestions)

        @kb.add("down", filter=suggesting)
        def _down(event) -> None:
            self.selected = (self.selected + 1) % len(self.suggestions)

        @kb.add("up", filter=typing & ~suggesting)
        def _history_up(event) -> None:
            self.buffer.auto_up(count=event.arg)

        @kb.add("down", filter=typing & ~suggesting)
        def _history_down(event) -> None:
            self.buffer.auto_down(count=event.arg)

        @kb.add("escape", filter=typing)
        def _escape(event) -> None:
            self._on_escape()

        @kb.add("s-tab")
        def _cycle(event) -> None:
            self.cycle_verbosity()

        @kb.add("c-o")
        def _verbose(event) -> None:
            self.set_verbosity("normal" if self.ctx.verbosity == "verbose" else "verbose")

        @kb.add("c-c")
        def _ctrl_c(event) -> None:
            self._on_ctrl_c()

        @kb.add("c-d", filter=typing)
        def _ctrl_d(event) -> None:
            if self.buffer.text:
                self.buffer.delete()
            elif self.input_mode is None:
                self.exit()

        @kb.add("c-l")
        def _clear(event) -> None:
            self.clear_screen()

        @kb.add("?", filter=typing & empty)
        def _shortcuts(event) -> None:
            self.print(render.shortcuts_view(self.ctx))

        # a dialog replaces the input box: it gets every key
        @kb.add(Keys.Any, filter=in_dialog)
        def _swallow(event) -> None:
            pass

        @kb.add("up", filter=in_dialog)
        def _dialog_up(event) -> None:
            self.dialog.move(-1)

        @kb.add("down", filter=in_dialog)
        def _dialog_down(event) -> None:
            self.dialog.move(1)

        @kb.add("enter", filter=in_dialog)
        def _dialog_enter(event) -> None:
            if self.dialog.ready():
                self.dialog.choose()

        @kb.add("escape", filter=in_dialog)
        def _dialog_escape(event) -> None:
            self.dialog.cancel()

        for digit in "123456789":
            @kb.add(digit, filter=in_dialog)
            def _dialog_digit(event) -> None:
                if self.dialog.ready():
                    self.dialog.pick_number(int(event.data))

        return kb

    def _on_enter(self) -> None:
        if self.suggestions and self.buffer.text:
            choice = self.suggestions[min(self.selected, len(self.suggestions) - 1)]
            typed = self.buffer.text[choice.start:choice.end]
            if choice.kind == "command" and choice.runnable:
                self._submit(choice.insert.strip())
                return
            if choice.kind == "command" or typed.strip().casefold() != choice.insert.strip().casefold():
                self._apply_suggestion(choice)
                return
            # "@Анна Смирнова" / "#12" typed in full: Enter sends
        doc = self.buffer.document
        if doc.is_cursor_at_the_end_of_line and doc.current_line_before_cursor.endswith("\\"):
            self.buffer.delete_before_cursor(1)
            self.buffer.insert_text("\n")
            return
        text = self.buffer.text
        if not text.strip() and self.input_mode is None:
            return
        self._submit(text)

    def _submit(self, text: str) -> None:
        mode, self.input_mode = self.input_mode, None
        if text.strip() and mode is None:
            self.buffer.document = Document(text, len(text))
            self.buffer.reset(append_to_history=True)
        else:
            self.buffer.reset()
        self._cleared()
        self._spawn(self.handle_input(text, mode))

    def _cleared(self) -> None:
        """After buffer.reset(), which does not fire on_text_changed: no suggestions for the old text."""
        self.selected = 0
        self._dismissed = None
        self._update_suggestions()
        self.invalidate()

    def _on_escape(self) -> None:
        if self.suggestions:
            self._dismissed = self.buffer.text
            self.suggestions = []
        elif self.input_mode is not None:
            self._cancel_mode()
        elif self._bash is not None:
            self._bash.interrupt()
        elif self.attached and self.working and not self.paused:
            self._spawn(self._act_quietly("swarm_control", {"action": "pause"}))

    def _on_ctrl_c(self) -> None:
        if self.dialog is not None:
            self.dialog.cancel()
        elif self.input_mode is not None:
            self._cancel_mode()
        elif self.buffer.text:
            self.buffer.reset()
            self._cleared()
        elif self._bash is not None:
            self._bash.interrupt()
        elif time.monotonic() - self._ctrl_c_at <= HINT_SECONDS:
            self.exit()
        else:
            self._ctrl_c_at = time.monotonic()
            self.set_hint(self.t("ctrl_c_again"), HINT_SECONDS)

    def _cancel_mode(self) -> None:
        mode, self.input_mode = self.input_mode, None
        self.buffer.reset()
        if mode is not None and mode[0] == "reject":
            self.mcp_answered.add(mode[1])
            self.info(self.t("mcp_deferred", name=mode[1]))
            self._release_reject()
        self._cleared()

    def _release_reject(self) -> None:
        waiter, self._reject_waiter = self._reject_waiter, None
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    # ---- actions used by the commands --------------------------------------------------------------
    def exit(self, code: int = 0) -> None:
        if self._exiting:
            return
        self._exiting = True
        with contextlib.suppress(Exception):
            self.app.exit(result=code)

    def clear_screen(self) -> None:
        if self.app.is_running:
            self.app.renderer.clear()

    def set_verbosity(self, verbosity: str) -> None:
        self.ctx.verbosity = verbosity
        self.invalidate()

    def cycle_verbosity(self) -> None:
        order = render.VERBOSITY
        current = order.index(self.ctx.verbosity) if self.ctx.verbosity in order else 1
        self.set_verbosity(order[(current + 1) % len(order)])

    def set_lang(self, lang: str) -> None:
        self.ctx.lang = lang
        self._lang_fixed = True
        save_prefs(self.home, lang=lang)
        self.info(self.t("lang_set"))
        self._update_suggestions()
        self.invalidate()

    def ask_goal(self) -> None:
        self.input_mode = ("goal", None)
        self.buffer.reset()
        self._cleared()

    def web_url(self, session: bool = True) -> str:
        base = (self.conn.base_url or self.client.base_url).rstrip("/")
        if session and self.attached and self.sid:
            return f"{base}/s/{self.sid}/"
        return base + ("/" if session else "")

    def open_url(self, url: str) -> None:
        self.info(self.t("opened_browser", url=url))
        try:
            webbrowser.open(url)
        except Exception as exc:
            self.error(str(exc))

    async def refresh_state(self) -> dict:
        """Fetch /api/state of the current session and update the view."""
        if not self.attached:
            return {}
        generation = self._generation
        state = await self.client.state(self.sid)
        if self.attached and self._generation == generation:  # not if another session was attached meanwhile
            self._apply_state(state)
        return state

    def request_refresh(self) -> None:
        self._refresh_wanted.set()

    async def toggle_pause(self) -> None:
        await self.refresh_state()
        action = "resume" if self.paused else "pause"
        await self._act_quietly("swarm_control", {"action": action})

    async def _act_quietly(self, tool: str, args: dict) -> bool:
        """A tool call whose effect the bus reports (pause, limits...): only a refusal is printed."""
        try:
            ok, text = await self.client.act(self.sid, tool, args)
        except HubError as exc:
            ok, text = False, self.hub_error(exc)
        if not ok:
            self.error(text)
        return ok

    async def status_info(self) -> dict:
        info: dict[str, Any] = {
            "version": __version__, "home": str(self.home), "web": self.web_url(session=False),
            "hub": self.t("hub_local" if self.conn.owned else "hub_external", url=self.client.base_url),
        }
        if self.client.mode == "launcher":
            with contextlib.suppress(HubError):
                view = (await self.client.settings()).get("settings") or {}
                default = (view.get("agents") or {}).get("default_model") or "default"
                info["models"] = [{"name": name, "model": (p or {}).get("model"), "default": name == default}
                                  for name, p in (view.get("models") or {}).items()]
        if self.attached:
            with contextlib.suppress(HubError):
                if self.client.mode == "launcher" and self.sid:
                    self.session = (await self.client.session(self.sid)).get("session") or self.session
                state = await self.refresh_state()
                swarm = state.get("swarm") or {}
                info["session"] = self.session or {"title": swarm.get("name"), "status": "running"}
                info["goal"] = swarm.get("goal")
                info["model"] = (self.session or {}).get("model")
                info["agents"] = {"total": len(self.members), "working": len(self.working)}
                info["budget"] = {"spent": self.spent, "limit": self.budget}
                info["tracing"] = bool(swarm.get("tracing"))
        info["language"] = self.ctx.lang
        info["verbosity"] = self.ctx.verbosity
        info["connected"] = self.connected
        return info

    # ---- sessions ----------------------------------------------------------------------------------
    async def new_session(self, goal: str) -> None:
        """Create and start a session with `goal` (and the CLI overrides), then attach to it."""
        if self.client.mode != "launcher":
            self.error(self.t("launcher_only"))
            return
        goal = goal.strip()
        if not goal:
            self.ask_goal()
            return
        title = self.title_override or " ".join(goal.split())[:60]
        self.set_hint(self.t("session_creating"))
        try:
            summary = await self.client.create_session(title, goal, overrides=self.overrides, start=True)
        finally:
            self.set_hint(None)
        self.title_override = None
        self.info(self.t("session_created", title=summary.get("title") or title, id=summary.get("id")))
        await self.attach(summary, announce=False, fresh=True)

    async def open_picker(self) -> None:
        sessions = await self.client.sessions()
        if not sessions:
            self.info(self.t("no_sessions"))
            return
        choice = await self.ask(SessionPicker(self.ctx.lang, sessions, self.sid, self.ctx.unicode))
        if choice is None:
            return
        summary = next((s for s in sessions if s.get("id") == choice), None)
        if summary is not None:
            await self.attach(summary)

    async def attach_id(self, sid: str) -> None:
        sessions = await self.client.sessions()
        summary = next((s for s in sessions if s.get("id") == sid), None)
        if summary is None:
            self.error(self.t("unknown_session", id=sid))
            return
        await self.attach(summary)

    async def stop_session(self) -> None:
        summary = await self.client.stop_session(self.sid)
        self.session = summary
        self._sync_working(set())
        self.paused = False
        self.info(self.t("session_stopped", title=summary.get("title") or self.sid, id=self.sid))
        self.invalidate()

    async def attach(self, summary: dict | None, *, announce: bool = True, fresh: bool = False) -> None:
        """Follow a session (None: the hub itself is one swarm): print its goal, a board summary and the
        latest messages, then its live events. Everything that can fail (start, events, state) runs before
        the current session is left, so a failed attach keeps it. `fresh`: a session just created here —
        its whole journal is replayed instead (the team joining, the goal, the first tasks), so nothing
        that happened before the stream connected is lost and the goal is not printed twice."""
        sid = summary.get("id") if summary else None
        if summary and self.client.mode == "launcher" and summary.get("status") != "running":
            self.info(self.t("session_starting", title=summary.get("title") or sid))
            summary = await self.client.start_session(sid)
        # the baseline first: a message sent while the history is fetched is replayed, and deduplicated by id
        latest = await self.client.events(sid, limit=1)
        state = await self.client.state(sid)
        history = await self._recent_messages(sid)
        await self._detach()
        self._reset_session()
        self._generation += 1
        self.sid, self.session, self.attached = sid, dict(summary) if summary else None, True
        self.last_event = 0 if fresh else max([e.get("id") or 0 for e in latest], default=0)
        self._apply_state(state)
        swarm = state.get("swarm") or {}
        if announce:
            title = (self.session or {}).get("title") or swarm.get("name") or sid
            self.info(self.t("attached", title=title, id=sid or self.client.base_url))
        if not fresh:
            self.print(render.goal_panel(self.ctx, swarm.get("goal")))
            if state.get("tasks"):
                self.print(render.board_summary(self.ctx, state["tasks"]))
            self._print_history(history)
        self._stream_task = self._spawn(self._follow(sid), "dreamteam-tui-stream")
        if self._refresher is None or self._refresher.done():
            self._refresher = self._spawn(self._refresh_loop(), "dreamteam-tui-refresh")
        self._update_suggestions()
        self.invalidate()

    async def _recent_messages(self, sid: str | None) -> list[dict]:
        """The latest messages between the user and the agents. The newest messages of everyone hold the
        user's own; the user's inbox keeps a question to the user that many agent-to-agent messages have
        pushed out of that window."""
        found: dict[int, dict] = {}
        with contextlib.suppress(HubError):
            for msg in await self.client.messages(sid, limit=200):
                if isinstance(msg, dict) and isinstance(msg.get("id"), int):
                    found[msg["id"]] = msg
        with contextlib.suppress(HubError):
            for msg in (await self.client.inbox(sid)).get("messages") or []:
                if isinstance(msg, dict) and isinstance(msg.get("id"), int):
                    found.setdefault(msg["id"], msg)
        convo = [found[i] for i in sorted(found)
                 if found[i].get("sender") == USER or USER in (found[i].get("recipients") or [])]
        return convo[-HISTORY_MESSAGES:]

    def _print_history(self, convo: list[dict]) -> None:
        for msg in convo:
            self.print(render.agent_message(self.ctx, msg))
            self._shown_messages.add(msg["id"])
        senders = [m.get("sender") for m in convo if m.get("sender") != USER]
        if senders:
            self.last_sender = senders[-1]
            for sender in dict.fromkeys(senders):
                self._spawn(self._mark_read(sender))

    async def _detach(self) -> None:
        task, self._stream_task = self._stream_task, None
        await self._cancel(task)
        worker, self._mcp_worker = self._mcp_worker, None
        await self._cancel(worker)
        self._mcp_waiting = None
        for handle in (self._lost_handle, self._tools_timer):
            if handle is not None:
                handle.cancel()
        self._lost_handle = self._tools_timer = None
        if self.input_mode is not None and self.input_mode[0] == "reject":
            self.input_mode = None
        self._release_reject()
        self.attached = False

    def _apply_state(self, state: dict) -> None:
        self.state = state
        self._last_refresh = time.monotonic()
        swarm = state.get("swarm") or {}
        self.paused = bool(swarm.get("paused"))
        self.spent = _float(swarm.get("spent_usd"))
        self.budget = _float((swarm.get("limits") or {}).get("max_cost_usd"))
        session = swarm.get("session") if isinstance(swarm.get("session"), dict) else None
        if session:
            self.session = {**(self.session or {}), "id": session.get("id") or self.sid,
                            "title": session.get("title") or (self.session or {}).get("title"),
                            "status": session.get("status") or (self.session or {}).get("status")}
        elif self.sid is None:  # a single-swarm hub runs its swarm while it serves it
            self.session = {**(self.session or {}), "title": swarm.get("name") or "DreamTeam", "status": "running"}
        self._sync_working({a["name"] for a in state.get("agents") or []
                            if isinstance(a, dict) and a.get("status") == "working" and a.get("name")})
        self._scan_mcp()
        self._update_suggestions()
        self.invalidate()

    async def _refresh_loop(self) -> None:
        while not self._exiting:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(REFRESH_IDLE):
                    await self._refresh_wanted.wait()
            self._refresh_wanted.clear()
            delay = REFRESH_MIN - (time.monotonic() - self._last_refresh)
            if delay > 0:
                await asyncio.sleep(delay)
            if self.attached and self.connected:
                try:
                    await self.refresh_state()
                except HubError as exc:
                    log.debug("state refresh failed: %s", exc)

    async def _ensure_running(self) -> None:
        if self.client.mode == "launcher" and self.sid and (self.session or {}).get("status") == "stopped":
            self.info(self.t("session_starting", title=(self.session or {}).get("title") or self.sid))
            self.session = await self.client.start_session(self.sid)

    # ---- input -------------------------------------------------------------------------------------
    async def handle_input(self, text: str, mode: tuple[str, Any] | None = None) -> None:
        """One submitted input (docs/TUI.md §4.2); inputs are handled one at a time, in order."""
        async with self._input_lock:
            try:
                await self._handle(text, mode)
            except HubError as exc:
                self.error(self.hub_error(exc))
            except Exception as exc:  # a bug must not end the session
                log.exception("input handling failed")
                self.error(f"{type(exc).__name__}: {exc}")

    async def _handle(self, text: str, mode: tuple[str, Any] | None) -> None:
        stripped = text.strip()
        if mode is not None:
            if mode[0] == "reject":
                try:
                    await self.decide_mcp(mode[1], False, stripped)
                finally:
                    self._release_reject()
            elif stripped:
                self.print(render.user_echo(self.ctx, stripped, None))
                await self.new_session(stripped)
            return
        if not stripped:
            return
        command = bash_command(stripped)
        if command is not None:
            await self.start_bash(command)  # runs on its own: other input is not held up by it
            return
        if parse_command(stripped) is not None:
            self.print(render.user_echo(self.ctx, stripped, None))
            await commands.execute(self, stripped)
            return
        if stripped.startswith("@") and self.attached:
            name, body = split_mention(stripped, self.members)
            if name is None:
                self.error(self.t("unknown_member", name=stripped.split()[0].lstrip("@") or "@"))
                return
            self.set_addressee(name)
            if body.strip():
                await self.send_message(name, body.strip())
            else:
                self.info(self.t("addressee_set", name=self.t("everyone") if name == ALL else name))
            return
        await self.send_plain(stripped)

    async def send_plain(self, text: str) -> None:
        if not self.attached:
            if self.client.mode == "launcher":
                self.print(render.user_echo(self.ctx, text, None))
                await self.new_session(text)
            else:
                self.error(self.t("no_session"))
            return
        goal = ((self.state or {}).get("swarm") or {}).get("goal") or {}
        if goal.get("status") == "done" and self.client.mode == "launcher":
            choice = await self.ask(ConfirmDialog(self.ctx.lang, self.t("confirm_new_title"),
                                                  self.t("confirm_new_text")))
            if choice is None:
                self.buffer.document = Document(text, len(text))  # nothing was sent: give the text back
                return
            if choice:
                self.print(render.user_echo(self.ctx, text, None))
                await self.new_session(text)
                return
        await self.send_message(self.addressee or ALL, text)

    async def send_message(self, to: str, text: str) -> None:
        self.print(render.user_echo(self.ctx, text, to))
        self._echo_pending.append(text)  # the bus copy of this message is not printed again
        try:
            await self._ensure_running()
            ok, result = await self.client.act(self.sid, "send_message", {"to": [to], "text": text})
        except BaseException:
            with contextlib.suppress(ValueError):
                self._echo_pending.remove(text)
            raise
        if not ok:
            with contextlib.suppress(ValueError):
                self._echo_pending.remove(text)
            self.error(result)

    async def start_bash(self, command: str) -> asyncio.Task | None:
        """`!command`: run it in the session workspace with the configured shell (local hubs only), as a
        task of its own; Esc / Ctrl+C interrupt it (the whole process tree is killed)."""
        if not self.local:
            self.error(self.t("bash_remote"))
            return None
        if not command:
            return None
        if self._bash is not None:
            self.error(self.t("bash_busy"))
            return None
        run = ShellCommand(command, cwd=self._workspace(), program=await self._shell())
        self._bash = run
        self.invalidate()
        return self._spawn(self._run_bash(run), "dreamteam-tui-bash")

    async def _run_bash(self, run: ShellCommand) -> None:
        try:
            code, output = await run.run(BASH_TIMEOUT)
        except (ValueError, OSError) as exc:
            self.error(str(exc))
            return
        finally:
            if self._bash is run:
                self._bash = None
            self.invalidate()
        self.print(render.bash_result(self.ctx, run.command, code, output))

    async def _shell(self) -> str:
        if self._shell_program is None:
            program = "auto"
            with contextlib.suppress(HubError, AttributeError, TypeError):
                if self.client.mode == "launcher" and self.attached and self.sid:
                    config = (await self.client.session(self.sid)).get("config") or {}
                elif self.client.mode == "launcher":
                    config = (await self.client.settings()).get("settings") or {}
                else:
                    config = {}
                program = (config.get("shell") or {}).get("program") or "auto"
            self._shell_program = program
        return self._shell_program

    def _workspace(self) -> Path:
        candidates: list[Path] = []
        if self.attached and (self.session or {}).get("dir"):
            directory = Path(self.session["dir"])
            candidates += [directory / "workspace", directory]
        elif self.attached and self.sid is None and self.hub.get("project"):
            candidates.append(Path(self.hub["project"]) / "workspace")
        candidates.append(self.home)
        return next((p for p in candidates if p.is_dir()), Path.cwd())

    # ---- dialogs -----------------------------------------------------------------------------------
    async def ask(self, dialog: Dialog) -> Any:
        """Show `dialog` in place of the input box until it is answered; its value (None = cancelled)."""
        async with self._dialog_lock:
            self.dialog = dialog
            dialog.open()
            self.suggestions = []
            with contextlib.suppress(ValueError):
                self.app.layout.focus(self.dialog_window)
            self.invalidate()
            try:
                return await dialog.wait()
            finally:
                self.dialog = None
                with contextlib.suppress(ValueError):
                    self.app.layout.focus(self.input_window)
                self._update_suggestions()
                self.invalidate()

    async def decide_mcp(self, name: str, approve: bool, comment: str = "") -> None:
        self.mcp_answered.add(name)
        ok, text = await self.client.act(self.sid, "mcp_approve",
                                         {"name": name, "approve": approve, "comment": comment})
        if ok:
            self.info(text)
            self._unqueue_mcp(name)
        else:
            self.error(text)

    def _queue_mcp(self, server: dict, *, fresh: bool = False) -> None:
        """Ask the user about a pending server; `fresh`: an agent (re)created it just now."""
        name = str(server.get("name") or "")
        if not name:
            return
        if fresh:
            self.mcp_answered.discard(name)
        elif name in self.mcp_answered:  # a state snapshot older than the answer
            return
        if self._mcp_dialog is not None and self._mcp_dialog.name == name:
            return
        if self.input_mode == ("reject", name):
            return
        for i, queued in enumerate(self.mcp_queue):
            if queued.get("name") == name:
                self.mcp_queue[i] = {**queued, **server}
                return
        self.mcp_queue.append(dict(server))
        if self._mcp_worker is None or self._mcp_worker.done():
            self._mcp_worker = self._spawn(self._mcp_loop(), "dreamteam-tui-mcp")

    def _unqueue_mcp(self, name: str) -> None:
        self.mcp_queue = [s for s in self.mcp_queue if s.get("name") != name]
        dialog = self._mcp_dialog
        if dialog is not None and dialog.name == name and not dialog.done:
            dialog.gone = True  # decided elsewhere (web UI, /approve)
            dialog.cancel()

    def _scan_mcp(self) -> None:
        for server in (self.state or {}).get("mcp") or []:
            if not isinstance(server, dict):
                continue
            if server.get("status") == "pending":
                self._queue_mcp(server)
            elif server.get("name"):
                self._unqueue_mcp(str(server["name"]))

    async def _mcp_loop(self) -> None:
        try:
            await self._ask_mcp()
        finally:
            self._mcp_waiting = None
            self.invalidate()

    async def _ask_mcp(self) -> None:
        while self.mcp_queue and self.attached:
            if self.buffer.text.strip() or self.input_mode is not None:
                # the user is typing: a dialog now would take the keys (Enter would approve)
                self._mcp_waiting = str(self.mcp_queue[0].get("name") or "")
                self.invalidate()
                await asyncio.sleep(MCP_WAIT_POLL)
                continue
            self._mcp_waiting = None
            server = self.mcp_queue.pop(0)
            dialog = MCPApprovalDialog(self.ctx.lang, server)
            self._mcp_dialog = dialog
            try:
                choice = await self.ask(dialog)
            finally:
                self._mcp_dialog = None
            if dialog.gone:
                continue
            self.mcp_answered.add(dialog.name)
            if choice == APPROVE:
                with contextlib.suppress(HubError):
                    await self.decide_mcp(dialog.name, True)
            elif choice == REJECT:
                self._reject_waiter = asyncio.get_running_loop().create_future()
                self.input_mode = ("reject", dialog.name)
                self.buffer.reset()
                self._cleared()
                await self._reject_waiter
            else:
                self.info(self.t("mcp_deferred", name=dialog.name))

    # ---- live events -------------------------------------------------------------------------------
    async def _follow(self, sid: str | None) -> None:
        stream = self.client.stream(sid)
        try:
            async for ev in stream:
                if not self.attached or self.sid != sid:
                    break
                try:
                    await self.on_event(ev)
                except HubError as exc:
                    log.debug("event handling: %s", exc)
                except Exception:
                    log.exception("cannot handle event %s", ev.get("type"))
                if not self.attached or self.sid != sid:  # e.g. the session was deleted
                    break
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()

    async def on_event(self, ev: dict, *, replayed: bool = False) -> None:
        """One bus event (or a `_connection` pseudo event): update the view state and print it. `replayed`:
        it comes from a catch-up over REST, not from the live stream."""
        etype = str(ev.get("type") or "")
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        if etype == "_connection":
            await self._on_connection(data.get("state"))
            return
        event_id = ev.get("id")
        if isinstance(event_id, int):
            # A catch-up replays only some types, so its newest id is no high-water mark: live events of the
            # other types buffered meanwhile must still be handled. Live ids only grow.
            if event_id <= self.last_event or event_id in self._replayed:
                return  # already shown
            if replayed:
                self._replayed.add(event_id)
            else:
                self.last_event = event_id
                if self._replayed:
                    self._replayed = {i for i in self._replayed if i > event_id}
        self._apply_event(etype, ev, data)
        if etype == "message.sent" and isinstance(data.get("message"), dict):
            msg = data["message"]
            if msg.get("id") in self._shown_messages:
                return  # printed as history at the attach
            sender = msg.get("sender") or ev.get("actor")
            if sender == USER:
                text = str(msg.get("text") or "")
                if text in self._echo_pending:
                    self._echo_pending.remove(text)
                    return
            elif USER in (msg.get("recipients") or []):
                self._on_agent_message(str(sender))
        lines = self.tools.feed(self.ctx, ev)
        if lines is None:
            self.print(render.event(self.ctx, ev))
        else:  # an agent's activity: each tool result printed under its own call
            for line in lines:
                self.print(line)
            self._schedule_tools()
        if etype.startswith(REFRESH_ON) or (etype == "notification" and data.get("kind") == "u_mcp_pending"):
            self.request_refresh()

    def _schedule_tools(self) -> None:
        if self._tools_timer is not None:
            self._tools_timer.cancel()
            self._tools_timer = None
        due = self.tools.next_due()
        if due is not None:
            self._tools_timer = asyncio.get_running_loop().call_later(max(0.0, due - time.monotonic()),
                                                                       self._flush_tools)

    def _flush_tools(self) -> None:
        """Calls that run long are printed alone (their result follows later)."""
        self._tools_timer = None
        if not self.attached:
            return
        for line in self.tools.due(self.ctx):
            self.print(line)
        self._schedule_tools()

    def _apply_event(self, etype: str, ev: dict, data: dict) -> None:
        if etype == "agent.status":
            name = data.get("name") or ev.get("actor")
            if name:
                names = set(self.working)
                if data.get("status") == "working":
                    names.add(name)
                else:
                    names.discard(name)
                self._sync_working(names)
                for agent in (self.state or {}).get("agents") or []:
                    if isinstance(agent, dict) and agent.get("name") == name:
                        agent["status"] = data.get("status")
        elif etype == "agent.usage":
            if self.working:
                self.tokens_in += int(_float(data.get("input_tokens")) + _float(data.get("cache_read_tokens")))
                self.tokens_out += int(_float(data.get("output_tokens")))
                self.work_cost += _float(data.get("cost"))
            if data.get("spent") is not None:
                self.spent = _float(data.get("spent"))
            self.invalidate()
        elif etype == "swarm.paused":
            self.paused = True
            self.invalidate()
        elif etype == "swarm.resumed":
            self.paused = False
            self._ensure_ticker()
            self.invalidate()
        elif etype == "swarm.limits":
            if data.get("max_cost_usd") is not None:
                self.budget = _float(data.get("max_cost_usd"))
        elif etype in ("goal.set", "goal.completed", "goal.reopened") and isinstance(data.get("goal"), dict):
            if self.state is not None:  # the confirm-before-new-session rule needs it before the refresh
                goal = dict(data["goal"])
                if etype == "goal.completed":
                    goal["status"] = "done"
                self.state.setdefault("swarm", {})["goal"] = goal
        elif etype == "mcp.created" and isinstance(data.get("server"), dict):
            server = data["server"]
            if server.get("status") == "pending":
                self._queue_mcp(server, fresh=True)
            elif server.get("name"):
                self._unqueue_mcp(str(server["name"]))
        elif etype == "mcp.status" and data.get("name"):
            if data.get("status") != "pending":
                self._unqueue_mcp(str(data["name"]))

    def _on_agent_message(self, sender: str) -> None:
        """A message to the user: its sender becomes the addressee unless the user chose one lately."""
        self.last_sender = sender
        if self._addressee_at is None or time.monotonic() - self._addressee_at > EXPLICIT_ADDRESSEE_SECONDS:
            self._addressee, self._addressee_at = sender, None
        self._spawn(self._mark_read(sender))
        self.invalidate()

    async def _mark_read(self, sender: str) -> None:
        with contextlib.suppress(HubError):
            await self.client.mark_read(self.sid, with_=sender)

    async def _on_connection(self, state: Any) -> None:
        if state == "rejected" and self.sid is not None:
            # the hub refused the stream: if the session is gone (deleted in the web UI), stop following it
            try:
                await self.client.state(self.sid)
            except HubError as exc:
                if exc.status == 404 and self.attached:
                    self.error(self.t("session_gone", id=self.sid))
                    await self._detach()
                    self._reset_session()
                    self._update_suggestions()
                    self.invalidate()
                    return
            state = "lost"
        if state in ("lost", "rejected"):
            if self.connected and self._lost_handle is None:
                self._lost_handle = asyncio.get_running_loop().call_later(LOST_GRACE, self._show_lost)
            return
        if state != "connected":
            return
        if self._lost_handle is not None:
            self._lost_handle.cancel()
            self._lost_handle = None
        first, self._first_connect = self._first_connect, False
        was_lost = not self.connected
        self.connected = True
        if was_lost:
            self.info(self.t("conn_restored"))
        self.invalidate()
        try:
            if first:
                self.request_refresh()  # agent states that changed before the stream was subscribed
            else:
                await self.refresh_state()
            await self._catch_up()
        except HubError as exc:
            log.debug("re-sync failed: %s", exc)

    def _show_lost(self) -> None:
        self._lost_handle = None
        if not self.connected:
            return
        self.connected = False
        self.print(render.event(self.ctx, {"type": "_connection", "data": {"state": "lost"}}))
        self.invalidate()

    async def _catch_up(self) -> None:
        """Replay what the stream missed (since the last live event), as far as it matters to the user."""
        since = self.last_event
        for _ in range(CATCHUP_PAGES):
            page = await self.client.events(self.sid, since=since, limit=CATCHUP_PAGE, types=CATCHUP_TYPES)
            for ev in page:
                await self.on_event(ev, replayed=True)
                if isinstance(ev.get("id"), int):
                    since = max(since, ev["id"])
            if len(page) < CATCHUP_PAGE:
                return
        latest = await self.client.events(self.sid, limit=1)  # too much missed: skip to now
        self.last_event = max([e.get("id") or 0 for e in latest] + [self.last_event])
        self._replayed = set()

    # ---- spinner -----------------------------------------------------------------------------------
    def _sync_working(self, names: set[str]) -> None:
        started = not self.working and bool(names)
        self.working = set(names)
        if started:
            self.work_since = time.monotonic()
            self.tokens_in = self.tokens_out = 0
            self.work_cost = 0.0
            self._verb_offset = random.randrange(64)
        self._ensure_ticker()
        self.invalidate()

    def _ensure_ticker(self) -> None:
        if self.working and self.app.is_running and (self._ticker is None or self._ticker.done()):
            self._ticker = self._spawn(self._tick(), "dreamteam-tui-spinner")

    async def _tick(self) -> None:
        while self.working and self.attached:
            self.frame += 1
            self.invalidate()
            await asyncio.sleep(SPINNER_TICK)
        self.invalidate()

    # ---- lifecycle ---------------------------------------------------------------------------------
    async def _startup(self) -> None:
        async with self._input_lock:  # what the user types meanwhile waits for the attach
            await self._open()
        if self.initial_prompt and self.initial_prompt.strip():
            await self.handle_input(self.initial_prompt)

    async def _open(self) -> None:
        missing = None
        try:
            self.hub = await self.client.hub_info()
            home = str(self.hub.get("home") or self.hub.get("project") or "")
            self.local = bool(self.conn.owned) or _same_path(home, str(self.home))
            sessions: list[dict] = []
            target: dict | None = None
            if self.client.mode == "launcher":
                if not self._lang_fixed:
                    with contextlib.suppress(HubError):
                        view = (await self.client.settings()).get("settings") or {}
                        lang = (view.get("swarm") or {}).get("language")
                        if lang in LANGS:
                            self.ctx.lang = lang
                sessions = await self.client.sessions()
                if self.session_arg:
                    target = next((s for s in sessions if s.get("id") == self.session_arg), None)
                    missing = None if target else self.session_arg
                elif self.continue_last and sessions:
                    target = max(sessions, key=lambda s: _float(s.get("last_activity") or s.get("created_at")))
            self.print(render.welcome(self.ctx, version=__version__, home=str(self.home),
                                      web_url=self.web_url(session=False), session=target))
            if missing:
                self.error(self.t("unknown_session", id=missing))
            elif self.continue_last and not sessions and self.client.mode == "launcher":
                self.info(self.t("no_sessions_continue"))
            if self.client.mode != "launcher":
                await self.attach(None, announce=False)
            elif target is not None:
                await self.attach(target, announce=False)
            else:
                self.print(render.tips(self.ctx, bool(sessions)))
        except HubError as exc:
            self.error(self.hub_error(exc))
        finally:
            self.ready.set()
            self.invalidate()

    def _pre_run(self) -> None:
        self.ctx.width = self.width
        self._spawn(self._startup(), "dreamteam-tui-startup")

    async def run(self) -> int:
        """Run until the user quits; the exit code (0)."""
        stdout = patch_stdout(raw=True) if self._real_terminal and self._printer is None \
            else contextlib.nullcontext()
        code: Any = 0
        # prompt_toolkit's own handler turns any stray error of the loop (which the in-process launcher
        # shares) into a modal "Press ENTER to continue" screen: log it instead
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(_log_loop_error)
        try:
            with stdout:
                code = await self.app.run_async(pre_run=self._pre_run, set_exception_handler=False)
        except (EOFError, KeyboardInterrupt):
            code = 0
        finally:
            self._exiting = True
            try:
                await self._shutdown()
            finally:
                loop.set_exception_handler(previous)
        if self.conn.owned:
            self.info(self.t("sessions_stopped"))
        self._write_out()
        return int(code or 0)

    async def _shutdown(self) -> None:
        for handle in (self._hint_handle, self._lost_handle, self._tools_timer):
            if handle is not None:
                handle.cancel()
        if self.dialog is not None:
            self.dialog.cancel()
        self._release_reject()
        self.attached = False
        # a task may start another one while it is cancelled, or (Python 3.11 wait_for race) lose one
        # cancellation: cancel what is left a few times; the connection's close ends any stream for good
        for _ in range(SHUTDOWN_ROUNDS):
            tasks = [task for task in self._tasks if not task.done() and task is not asyncio.current_task()]
            if not tasks:
                break
            for task in tasks:
                task.cancel()
            await asyncio.wait(tasks, timeout=SHUTDOWN_WAIT)


async def run_tui(conn: Connection, *, lang: str | None = None, session: str | None = None,
                  continue_last: bool = False, initial_prompt: str | None = None, verbosity: str = "normal",
                  input: Any = None, output: Any = None, printer: Callable[[Any], None] | None = None,
                  overrides: dict | None = None) -> int:
    """Run the interactive TUI on a hub connection until the user quits (docs/TUI.md §4). The connection
    is left open: the caller closes it. `printer(renderable)` replaces printing above the input box
    (tests capture `render.to_plain`); `input` / `output` are prompt_toolkit Input / Output."""
    tui = TUI(conn, lang=lang, session=session, continue_last=continue_last, initial_prompt=initial_prompt,
              verbosity=verbosity, overrides=overrides, input=input, output=output, printer=printer)
    return await tui.run()


__all__ = ["TUI", "run_tui"]
