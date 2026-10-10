"""Pure renderers of the terminal UI (docs/TUI.md §3).

Inputs are dicts as returned by the hub API plus a RenderContext; outputs are rich renderables (printed
above the input box through `to_ansi`) or, for the bottom area, prompt_toolkit formatted text. Nothing
here does I/O, and no event payload, however malformed, makes a renderer raise. `ToolPairs` is the one
stateful helper: it keeps tool results under their calls in a printed feed."""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from io import StringIO
from typing import Any, Callable, Iterable

from prompt_toolkit.utils import get_cwidth
from rich import box
from rich.console import Console, Group, RenderableType
from rich.markdown import Heading, Markdown
from rich.measure import Measurement
from rich.padding import Padding
from rich.panel import Panel
from rich.segment import Segment
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .. import prompts, roles
from . import theme
from .i18n import t, tn

log = logging.getLogger(__name__)

VERBOSITY = ("quiet", "normal", "verbose")
SPINNER_FRAMES = theme.SPINNER_FRAMES_UNICODE  # ping-pong like Claude Code
SPINNER_FRAMES_ASCII = theme.SPINNER_FRAMES_ASCII

# every bus event type the server emits (SPEC §9), plus launcher and client pseudo events
EVENT_TYPES = (
    "task.created", "task.updated", "task.comment", "message.sent", "notification", "goal.set",
    "goal.completed", "goal.reopened", "agent.hired", "agent.retired", "agent.status", "agent.activity",
    "agent.compacted", "agent.usage", "file.changed", "file.locked", "file.unlocked", "trigger.created",
    "trigger.fired", "trigger.deleted", "mcp.created", "mcp.status", "swarm.paused", "swarm.resumed",
    "swarm.limits", "trace.started", "trace.span", "trace.finished", "sessions.changed", "_connection",
)
ACTIVITY_KINDS = ("wake", "thinking", "text", "tool_call", "tool_result", "error", "nudge", "compacted")

TOOL_CALL_CHARS = 80      # "task_claim(task_id=3)" is cut to about this many characters
HOUSEKEEPING_TOOLS = frozenset({"wait", "wait_for_events"})   # their calls and results show only in verbose
# Tools whose effect has its own line (the message, the task, the goal panel, the team change): in normal
# verbosity their successful calls would only repeat it ("⏺ Анна → вам …" + "send_message(…) ⎿ Sent message #2").
ECHOED_TOOLS = frozenset({"send_message", "task_claim", "task_create", "task_complete", "task_comment",
                          "goal_complete", "hire_agent", "retire_agent"})
TOOL_HOLD = 1.0           # ToolPairs: a call waits this long for its result before it is printed alone
ARG_CHARS = 30            # one string argument inside it
RESULT_LINES = 20         # tool result lines in verbose
THINKING_LINES = 12
MAX_FILE_LINES = 2000     # /cat: longer files are cut (the hub caps the size too)

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_]")
# Text written by agents must not drive the terminal: drop C0/C1 control characters (keep \n).
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


@dataclass
class RenderContext:
    lang: str = "ru"
    verbosity: str = "normal"
    width: int = 100
    unicode: bool = True          # False -> ASCII symbols (theme.SYMBOLS_ASCII)
    me: str = "user"

    @property
    def sym(self) -> dict[str, str]:
        return theme.symbols(self.unicode)

    @property
    def quiet(self) -> bool:
        return self.verbosity == "quiet"

    @property
    def verbose(self) -> bool:
        return self.verbosity == "verbose"

    def t(self, key: str, **kw: Any) -> str:
        return t(self.lang, key, **kw)

    def tn(self, key: str, n: int, **kw: Any) -> str:
        return tn(self.lang, key, n, **kw)

    def short(self, value: Any, limit: int) -> str:
        """One line, whitespace collapsed, cut to `limit` characters with an ellipsis."""
        text = " ".join(clean(value).split())
        if limit <= 0 or len(text) <= limit:
            return text
        ell = self.sym["ellipsis"]
        return text[: max(0, limit - len(ell))].rstrip() + ell


# ---- small helpers -------------------------------------------------------------------------------
def clean(value: Any) -> str:
    """Printable text of an API value: control and escape sequences removed, tabs expanded."""
    if value is None:
        return ""
    if not isinstance(value, str):
        if isinstance(value, (dict, list, tuple)):
            try:
                value = json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                value = str(value)
        else:
            value = str(value)
    if not value.isascii():
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:  # an unpaired surrogate from model output
            value = value.encode("utf-8", "replace").decode("utf-8")
    value = _ANSI.sub("", value.replace("\r\n", "\n").replace("\t", "    "))
    return _CONTROL.sub("", value)


def _d(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return number if number == number and abs(number) != float("inf") else default


def _names(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    return [str(v) for v in _list(value) if v]


def first_name(name: Any) -> str:
    words = clean(name).split()
    return words[0] if words else ""


def _first_line(value: Any) -> str:
    for line in clean(value).splitlines():
        if line.strip():
            return line.strip()
    return ""


_WAKE_HEADER = re.compile(r"^=+ .* =+$")   # "=== Wake-up 2026-10-08 19:37 ===" (prompts.wake_header)
_WAKE_LABELS = frozenset(prompts.t(lang, "reasons") for lang in ("ru", "en"))   # "What happened:"


def _wake_summary(value: Any) -> str:
    """What woke an agent: the first line of its wake-up text after the "=== Wake-up … ===" header and the
    "What happened:" label; a line ending with ":" (a message header) is joined with the line after it."""
    lines = [line.strip() for line in clean(value).splitlines() if line.strip()]
    lines = [line for line in lines if not _WAKE_HEADER.match(line) and line not in _WAKE_LABELS]
    if not lines:
        return ""
    first = lines[0].removeprefix("•").strip()
    if first.endswith(":") and len(lines) > 1 and not lines[1].startswith(("•", "=")):
        first = f"{first} {lines[1]}"
    return first


def _clip_lines(ctx: RenderContext, value: Any, max_lines: int) -> tuple[str, int]:
    """The first `max_lines` non-trailing lines of `value` and how many lines were left out."""
    lines = clean(value).strip("\n").splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if max_lines <= 0 or len(lines) <= max_lines:
        return "\n".join(lines), 0
    return "\n".join(lines[:max_lines]), len(lines) - max_lines


def fmt_tokens(n: Any) -> str:
    """Token counts the way Claude Code prints them: 310, 4.2k, 123k, 1.3M."""
    n = max(0, _int(n))
    if n < 1000:
        return str(n)
    k = n / 1000
    if k < 99.95:
        return f"{k:.1f}k"
    if k < 999.5:
        return f"{k:.0f}k"
    return f"{n / 1_000_000:.1f}M"


def fmt_elapsed(seconds: Any) -> str:
    """12s, 3m 05s, 1h 02m."""
    s = max(0, int(_float(seconds)))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


def fmt_ms(ms: Any) -> str:
    ms = max(0, _int(ms))
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f}s"
    return fmt_elapsed(ms / 1000)


def fmt_cost(usd: Any, digits: int = 2) -> str:
    """$0.12; small non-zero amounts keep three decimals ($0.004)."""
    usd = _float(usd)
    if digits == 2 and 0 < abs(usd) < 0.01:
        return f"${usd:.3f}"
    return f"${usd:.{digits}f}"


def _clock(ts: Any, with_date: bool = False) -> str:
    value = _float(ts)
    if value <= 0:
        return "-"
    try:
        return time.strftime("%d.%m %H:%M" if with_date else "%H:%M", time.localtime(value))
    except (OverflowError, OSError, ValueError):
        return "-"


def _ago(ctx: RenderContext, ts: Any) -> str:
    value = _float(ts)
    if value <= 0:
        return "-"
    delta = max(0, int(time.time() - value))
    if delta < 60:
        return ctx.t("just_now")
    if delta < 3600:
        return ctx.t("min_ago", n=delta // 60)
    if delta < 86400:
        return ctx.t("h_ago", n=delta // 3600)
    return ctx.t("d_ago", n=delta // 86400)


def _who(ctx: RenderContext, name: Any, short: bool = False) -> str:
    """Display name: "вы" for the user, "система" for no actor, else the (first) name."""
    if name == ctx.me:
        return ctx.t("you")
    if not name:
        return ctx.t("system")
    return first_name(name) if short else clean(name)


def _status_label(ctx: RenderContext, prefix: str, value: Any) -> str:
    """Translated label of an enum value (`s_done`, `a_idle`...), the raw value when unknown."""
    raw = clean(value)
    label = ctx.t(prefix + raw) if raw else ""
    return raw if label == prefix + raw else label


# ---- ASCII fallback -------------------------------------------------------------------------------
def _ascii_char(ch: str) -> str | None:
    code = ord(ch)
    if 0x2500 <= code <= 0x257F:  # box drawing
        name = unicodedata.name(ch, "")
        if "HORIZONTAL" in name and "VERTICAL" not in name:
            return "-"
        if "VERTICAL" in name and "HORIZONTAL" not in name:
            return "|"
        return "+"
    if 0x2580 <= code <= 0x259F:  # block elements (markdown quotes)
        return "|"
    return None


_ASCII_MAP: dict[int, str] = {ord(k): v for k, v in {
    "—": "-", "–": "-", "−": "-", "…": ".", "→": ">", "←": "<", "↑": "^", "↓": "v", "↳": ">", "↻": "~",
    "·": "-", "•": "*", "«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "✓": "v",
    "×": "x", "❱": ">", "≥": ">", "≤": "<", "≈": "~", "‼": "!", "№": "N", "\u00a0": " ",
}.items()}
_ASCII_MAP.update({c: r for c in range(0x2500, 0x25A0) if (r := _ascii_char(chr(c))) is not None})


class AsciiSafe:
    """Renders its child and maps typography and box drawing to ASCII look-alikes, one cell for one
    cell, so widths do not change. Used for every renderable when RenderContext.unicode is False."""

    def __init__(self, renderable: RenderableType):
        self.renderable = renderable

    def __rich_console__(self, console: Console, options: Any) -> Iterable[Segment]:
        for seg in console.render(self.renderable, options):
            if seg.control or seg.text.isascii():
                yield seg
            else:
                yield Segment(seg.text.translate(_ASCII_MAP), seg.style, seg.control)

    def __rich_measure__(self, console: Console, options: Any) -> Measurement:
        return Measurement.get(console, options, self.renderable)


def _out(ctx: RenderContext, renderable: RenderableType | None) -> RenderableType | None:
    if renderable is None or ctx.unicode or isinstance(renderable, AsciiSafe):
        return renderable
    return AsciiSafe(renderable)


def _box(ctx: RenderContext) -> box.Box:
    return box.ROUNDED if ctx.unicode else box.ASCII


# ---- layout primitives ----------------------------------------------------------------------------
def _hang(prefix: Text, body: RenderableType) -> Table:
    """`prefix` followed by `body`; wrapped lines of the body stay indented under its first character."""
    grid = Table.grid(padding=0)
    grid.add_column(no_wrap=True)
    grid.add_column()
    grid.add_row(prefix, body)
    return grid


def _line(ctx: RenderContext, symbol: str, symbol_style: str, body: Text) -> Table:
    return _hang(Text(symbol + " ", style=symbol_style), body)


def _bullet(ctx: RenderContext, who: Any, *parts: Text | str, symbol: str | None = None,
            color: str | None = None) -> Table:
    """`⏺ Name · action`: the bullet in the member's colour, the name bold."""
    color = color or theme.agent_style(clean(who) if who else None)
    body = Text()
    body.append(_who(ctx, who), style=f"bold {color}")  # a base style of the Text would cover the parts too
    for part in parts:
        body.append(part)
    return _line(ctx, symbol or ctx.sym["bullet"], color, body)


def _cont_prefix(ctx: RenderContext) -> Text:
    mark = ctx.sym["cont"]  # "  ⎿  " / "  |_ ": five cells either way
    return Text(f"  {mark}" + " " * max(1, 3 - len(mark)), style="dim")


def _cont(ctx: RenderContext, body: RenderableType) -> Table:
    """`  ⎿  body` (the hook dim)."""
    return _hang(_cont_prefix(ctx), body)


def _more(ctx: RenderContext, rest: int) -> Text:
    return Text(ctx.tn("n_more_lines", rest, ell=ctx.sym["ellipsis"]), style="dim")


def _clipped_text(ctx: RenderContext, value: Any, max_lines: int, style: str = "") -> RenderableType:
    text, rest = _clip_lines(ctx, value, max_lines)
    body = Text(text, style=style)
    return Group(body, _more(ctx, rest)) if rest else body


def _one(ctx: RenderContext, value: Any, style: str = "", limit: int = 400) -> Text:
    """A single line that is cut with an ellipsis at the available width."""
    return Text(ctx.short(value, limit), style=style, no_wrap=True, overflow="ellipsis")


class _Heading(Heading):
    LEVEL_ALIGN = dict.fromkeys(("h1", "h2", "h3", "h4", "h5", "h6"), "left")  # a chat, not a document


class _Markdown(Markdown):
    elements = {**Markdown.elements, "heading_open": _Heading}


def _markdown(ctx: RenderContext, value: Any, style: str = "none") -> RenderableType:
    text = clean(value).strip()
    if not text:
        return Text(ctx.t("empty"), style="dim")
    try:
        return _Markdown(text, code_theme=theme.CODE_THEME, hyperlinks=False, style=style)
    except Exception:  # never fail on odd markdown
        return Text(text)


def _sep(ctx: RenderContext) -> Text:
    return Text(f" {ctx.sym['dot']} ", style="dim")


def _join(ctx: RenderContext, parts: Iterable[Text | str | None], style: str = "") -> Text:
    out = Text(style=style)
    first = True
    for part in parts:
        if part is None or part == "":
            continue
        if not first:
            out.append_text(_sep(ctx))
        out.append(part)
        first = False
    return out


# ---- welcome & tips -------------------------------------------------------------------------------
def welcome(ctx: RenderContext, *, version: str, home: str, web_url: str | None,
            session: dict | None) -> RenderableType:
    s = ctx.sym
    title = Text(f"{s['spark']} ", style=theme.ACCENT)
    title.append(ctx.t("welcome"), style=f"bold {theme.ACCENT}")
    if version:
        title.append(f"  v{clean(version)}", style="dim")
    rows: list[RenderableType] = [title, Text(""), Text(ctx.t("welcome_hint"), style="dim"), Text("")]
    rows.append(Text(f"home: {clean(home)}", style="dim"))
    rows.append(Text(f"web:  {clean(web_url) if web_url else ctx.t('off')}", style="dim"))
    sess = _d(session)
    if sess:
        line = Text(f"{ctx.t('w_session')}: ", style="dim")
        line.append(clean(sess.get("title") or sess.get("id") or "?"), style="bold")
        if sess.get("id") and sess.get("title") and sess.get("id") != sess.get("title"):
            line.append(f" ({clean(sess.get('id'))})", style="dim")
        if sess.get("status"):
            line.append_text(_sep(ctx))
            line.append(_session_status(ctx, sess)[0], style="dim")
        rows.append(line)
    return _out(ctx, Panel(Group(*rows), box=_box(ctx), border_style=theme.ACCENT, padding=(0, 1), expand=False))


def tips(ctx: RenderContext, has_sessions: bool) -> RenderableType:
    items = ["tip_goal", "tip_address", "tip_board", "tip_resume" if has_sessions else "tip_bash"]
    grid = Table.grid(padding=(0, 1))
    grid.add_column(no_wrap=True, style="dim")
    grid.add_column()
    for i, key in enumerate(items, 1):
        grid.add_row(f"{i}.", Text(ctx.t(key)))
    return _out(ctx, Padding(Group(Text(ctx.t("tips_title"), style="dim"), grid), (0, 0, 0, 1)))


# ---- messages -------------------------------------------------------------------------------------
def user_echo(ctx: RenderContext, text: str, to: str | None) -> RenderableType:
    """`> text` on a grey chip (the "you said" echo), then `→ addressee` dim."""
    lines = clean(text).strip("\n").splitlines() or [""]
    body = Text()
    prompt = ctx.sym["prompt"]
    for i, line in enumerate(lines):
        if i:
            body.append("\n")
        body.append(f"{prompt if i == 0 else ' ' * len(prompt)} {line} ", style=theme.USER_ECHO)
    if to:
        target = ctx.t("everyone") if to == "all" else clean(to)
        body.append(f"  {ctx.sym['arrow']} {target}", style="dim")
    return _out(ctx, body)


def _recipients_text(ctx: RenderContext, recipients: list[str], short: bool = False) -> str:
    names = [ctx.t("to_you") if r == ctx.me else (first_name(r) if short else clean(r)) for r in recipients]
    if len(names) > 4:
        return ", ".join(names[:3]) + f" +{len(names) - 3}"
    return ", ".join(names) or "?"


def _mail_line(ctx: RenderContext, sender: Any, recipients: list[str], text: Any, full: bool) -> RenderableType:
    """`✉ Анна → Борис: text` (dim): one line in normal, the whole text when `full`."""
    head = f"{_who(ctx, sender, short=True)} {ctx.sym['arrow']} {_recipients_text(ctx, recipients, True)}: "
    if not full:
        return _line(ctx, ctx.sym["mail"], "dim", _one(ctx, head + " ".join(clean(text).split()), "dim"))
    body, rest = _clip_lines(ctx, text, 40)
    out = Text(head + body, style="dim")
    if rest:
        out.append("\n").append_text(_more(ctx, rest))
    return _line(ctx, ctx.sym["mail"], "dim", out)


def agent_message(ctx: RenderContext, msg: dict) -> RenderableType:
    """`⏺ Анна Смирнова → вам` and the body as markdown (msg from /api/messages or message.sent)."""
    msg = _d(msg)
    sender = msg.get("sender")
    recipients = _names(msg.get("recipients"))
    if sender == ctx.me:
        return user_echo(ctx, msg.get("text") or "", _echo_target(ctx, recipients))
    if recipients and ctx.me not in recipients:
        return _out(ctx, _mail_line(ctx, sender, recipients, msg.get("text"), full=True))
    others = [r for r in recipients if r != ctx.me]
    tail = Text(f" {ctx.sym['arrow']} {ctx.t('to_you')}", style="dim")
    if others:
        tail.append(", " + _recipients_text(ctx, others), style="dim")
    if ctx.verbose and msg.get("ts"):
        tail.append_text(_sep(ctx)).append(_clock(msg.get("ts")), style="dim")
    head = _bullet(ctx, sender if sender else None, tail)
    return _out(ctx, Group(head, Padding(_markdown(ctx, msg.get("text")), (0, 0, 0, 2))))


def _echo_target(ctx: RenderContext, recipients: list[str]) -> str | None:
    if not recipients:
        return None
    if len(recipients) > 3:
        return "all"
    return ", ".join(clean(r) for r in recipients)


# ---- goal -----------------------------------------------------------------------------------------
def goal_panel(ctx: RenderContext, goal: dict | None) -> RenderableType:
    goal = _d(goal)
    if not goal:
        return _out(ctx, Panel(Text(ctx.t("no_goal"), style="dim"), title=ctx.t("goal"), title_align="left",
                               box=_box(ctx), border_style="dim", padding=(0, 1)))
    done = goal.get("status") == "done"
    rows: list[RenderableType] = [Text(clean(goal.get("title")) or "—", style="bold")]
    if goal.get("description"):
        rows += [Text(""), _markdown(ctx, goal.get("description"), style="dim")]
    if done:
        if goal.get("summary"):
            rows += [Text(""), _markdown(ctx, goal.get("summary"))]
        title = Text(f"{ctx.sym['check']} {ctx.t('goal_done')}", style=f"bold {theme.OK}")
        border = theme.OK
    else:
        title = Text(ctx.t("goal") + (f" ({ctx.t('goal_pending')})" if goal.get("status") == "pending" else ""),
                     style=f"bold {theme.ACCENT}")
        border = theme.ACCENT
    return _out(ctx, Panel(Group(*rows), title=title, title_align="left", box=_box(ctx), border_style=border,
                           padding=(0, 1)))


# ---- tasks ----------------------------------------------------------------------------------------
_TASK_SYMBOL = {"todo": "todo", "in_progress": "doing", "waiting": "doing", "review": "review", "done": "done",
                "failed": "failed", "cancelled": "failed"}


def _task_symbol(ctx: RenderContext, status: Any) -> tuple[str, str]:
    key = _TASK_SYMBOL.get(status if isinstance(status, str) else "", "todo")
    style = {"doing": theme.ACCENT, "review": theme.WARN, "done": theme.OK, "failed": theme.ERROR}.get(key, "")
    if status in ("waiting", "cancelled"):
        style = "dim"
    return ctx.sym[key], style


def _task_head(ctx: RenderContext, task: dict, status: Any = None) -> Text:
    status = status or task.get("status")
    body = Text()
    body.append(f"#{clean(task.get('id')) or '?'} ", style="bold" if status != "cancelled" else "dim")
    body.append(clean(task.get("title")) or "—", style="dim strike" if status == "cancelled" else "")
    return body


def _task_line(ctx: RenderContext, task: dict, body: Text, status: Any = None) -> Table:
    symbol, style = _task_symbol(ctx, status or task.get("status"))
    return _line(ctx, symbol, style, body)


def _ev_task_created(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    if ctx.quiet:
        return None
    task = _d(d.get("task"))
    body = _task_head(ctx, task, "todo")
    assignee = task.get("assignee")
    if assignee:
        body.append(f" {ctx.sym['dash']} {first_name(assignee)}", style=theme.agent_style(assignee))
    if task.get("goal"):
        goal = clean(task.get("goal")) if ctx.verbose else ctx.short(task.get("goal"), 100)
        body.append(f" {ctx.sym['dot']} {goal}", style="dim")
    if ctx.verbose and ev.get("actor"):
        body.append(f" {ctx.sym['dot']} {ctx.t('task_by', name=_who(ctx, ev.get('actor')))}", style="dim")
    return _task_line(ctx, task, body, task.get("status") or "todo")


def _ev_task_updated(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    if ctx.quiet:
        return None
    task = _d(d.get("task"))
    changes = _d(d.get("changes"))

    def new(field: str) -> Any:
        change = changes.get(field)
        if isinstance(change, (list, tuple)) and len(change) == 2:
            return change[1]
        return task.get(field)

    status = task.get("status") or new("status")
    assignee = task.get("assignee") if "assignee" in task else new("assignee")
    if "status" not in changes and not ("assignee" in changes and assignee):
        if not ctx.verbose:
            return None
        fields = ", ".join(_status_label(ctx, "f_", f) for f in changes) or ctx.t("task_changed")
        body = _task_head(ctx, task)
        body.append(f" {ctx.sym['dot']} {ctx.t('task_changed')}: {fields}", style="dim")
        return _line(ctx, ctx.sym["edit"], "dim", body)
    body = _task_head(ctx, task, status)
    who = Text(f" {ctx.sym['dash']} {first_name(assignee)}", style=theme.agent_style(assignee)) if assignee else None
    rows: list[RenderableType] = []
    if who is not None and (status in ("in_progress", "waiting", "done", "failed")
                            or (status == "todo" and "status" not in changes)):  # assigned, not started
        body.append_text(who)
    if status == "waiting":
        body.append(f" {ctx.sym['dot']} {ctx.t('task_waiting')}", style="dim")
    elif status == "review":
        reviewer = task.get("reviewer") if "reviewer" in task else new("reviewer")
        label, style = _review_label(ctx, reviewer)
        body.append(f" {ctx.sym['dash']} {label}", style=style)
    elif status == "done":
        if task.get("result"):
            rows.append(_task_result(ctx, task.get("result"), ""))
    elif status == "failed":
        body.append(f" {ctx.sym['dot']} {ctx.t('task_failed')}", style=theme.ERROR)
        if task.get("result"):
            rows.append(_task_result(ctx, task.get("result"), theme.ERROR))
    elif status == "cancelled":
        body.append(f" {ctx.sym['dot']} {ctx.t('task_cancelled')}", style="dim")
    elif status == "todo" and "status" in changes:
        body.append(f" {ctx.sym['dot']} {ctx.t('task_back_todo')}", style="dim")
    line = _task_line(ctx, task, body, status)
    return Group(line, *rows) if rows else line


def _review_label(ctx: RenderContext, reviewer: Any) -> tuple[str, str]:
    """The label of a task in review: "in review (Carl)", or "waits for your review" (bold) for the viewer."""
    if reviewer and reviewer == ctx.me:
        return ctx.t("task_review_you"), f"bold {theme.WARN}"
    return ctx.t("task_review") + (f" ({first_name(reviewer)})" if reviewer else ""), theme.WARN


def _task_result(ctx: RenderContext, result: Any, style: str) -> RenderableType:
    if ctx.verbose:
        return _cont(ctx, _clipped_text(ctx, result, RESULT_LINES, style or "dim"))
    return _cont(ctx, _one(ctx, _first_line(result), style or "dim"))


def _ev_task_comment(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    if ctx.quiet:
        return None
    post = _d(d.get("post"))
    author = post.get("author") or ev.get("actor")
    task_id = d.get("task_id") if d.get("task_id") is not None else post.get("task_id")
    head = f"#{clean(task_id) or '?'} {_who(ctx, author, short=True)}: "
    if not ctx.verbose:
        return _line(ctx, ctx.sym["chat"], "", _one(ctx, head + " ".join(clean(post.get("text")).split())))
    text, rest = _clip_lines(ctx, post.get("text"), RESULT_LINES)
    body = Text(head, style="bold")
    body.append(text)
    if rest:
        body.append("\n").append_text(_more(ctx, rest))
    return _line(ctx, ctx.sym["chat"], "", body)


# ---- event handlers -------------------------------------------------------------------------------
def _ev_message(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    msg = _d(d.get("message"))
    sender = msg.get("sender") or ev.get("actor")
    recipients = _names(msg.get("recipients"))
    if sender == ctx.me:
        return user_echo(ctx, msg.get("text") or "", _echo_target(ctx, recipients))
    if ctx.me in recipients:
        return agent_message(ctx, {**msg, "sender": sender, "recipients": recipients})
    if ctx.quiet:
        return None
    return _mail_line(ctx, sender, recipients, msg.get("text"), full=ctx.verbose)


_QUIET_SKIP_NOTES = frozenset({"u_goal_done", "u_member_joined"})  # the goal panel / hire line say it
# the user has to act on these: a budget pause, an MCP server to approve, a review that waits for the user
_WARN_NOTES = frozenset({"u_budget", "u_mcp_pending", "u_review_requested", "u_review_fallback",
                         "u_review_handed_over", "u_reviews_waiting"})


def _ev_notification(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    recipient, kind, text = d.get("recipient"), clean(d.get("kind")), d.get("text")
    if recipient != ctx.me:
        if not ctx.verbose:
            return None
        body = Text(f"{ctx.sym['arrow']} {first_name(recipient) or '?'}: ", style="dim")
        body.append(ctx.short(_first_line(text), 200), style="dim")
        return _line(ctx, ctx.sym["bell"], "dim", body)
    if kind in _QUIET_SKIP_NOTES:
        return None
    if kind == "u_agent_error":  # normal/verbose show the agent.activity error line instead
        return error(ctx, clean(text)) if ctx.quiet else None
    style = theme.WARN if kind in _WARN_NOTES else ""
    return _line(ctx, ctx.sym["bell"], style, Text(clean(text).strip() or kind, style=style))


def _ev_goal(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    goal = _d(d.get("goal"))
    if ev.get("type") == "goal.completed" and goal:
        goal = {**goal, "status": "done"}
    return goal_panel(ctx, goal or None)


def _ev_goal_reopened(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    body = Text()
    body.append(f" {ctx.sym['dot']} {ctx.t('goal_reopened')}", style=theme.WARN)
    if d.get("comment"):
        body.append(": " + clean(d.get("comment")).strip())
    return _bullet(ctx, ev.get("actor"), body)


def _ev_hired(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    if ctx.quiet:
        return None
    name = d.get("name") or ev.get("actor")
    body = Text()
    body.append(clean(name) or "?", style=f"bold {theme.agent_style(name)}")
    role = clean(d.get("role"))
    if d.get("kind") == "external":
        role = f"{role}, {ctx.t('external')}" if role else ctx.t("external")
    if role:
        body.append(f" ({role})", style="dim")
    body.append(" " + ctx.t("hired"))
    if ctx.verbose:
        if d.get("model"):
            body.append(f" {ctx.sym['dot']} {ctx.t('model')}: {clean(d.get('model'))}", style="dim")
        by = d.get("hired_by") or ev.get("actor")
        if by:
            body.append(f" {ctx.sym['dot']} {ctx.t('hired_by', name=_who(ctx, by))}", style="dim")
    return _line(ctx, ctx.sym["plus"], theme.OK, body)


def _ev_retired(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    if ctx.quiet:
        return None
    name = d.get("name") or ev.get("actor")
    body = Text()
    body.append(clean(name) or "?", style=f"bold {theme.agent_style(name)}")
    body.append(" " + ctx.t("retired"))
    if d.get("reason"):
        reason = clean(d.get("reason")) if ctx.verbose else ctx.short(d.get("reason"), 120)
        body.append(f": {reason}", style="dim")
    return _line(ctx, ctx.sym["minus"], "dim", body)


def _ev_status(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    status = d.get("status")
    if not ctx.verbose or status not in ("error", "paused", "retired"):
        return None
    name = d.get("name") or ev.get("actor")
    body = Text(f"{clean(name) or '?'}: {_status_label(ctx, 'a_', status)}",
                style=theme.ERROR if status == "error" else "dim")
    return _line(ctx, ctx.sym["dot"], "dim", body)


def tool_call_text(ctx: RenderContext, tool: Any, args: Any, limit: int = TOOL_CALL_CHARS) -> str:
    """`task_claim(task_id=3)`: arguments as k=v, strings quoted and shortened."""
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except ValueError:
            pass
    name = clean(tool) or "?"
    if isinstance(args, dict):
        inner = ", ".join(f"{clean(k)}={_arg(ctx, v)}" for k, v in args.items())
    elif args in (None, ""):
        inner = ""
    else:
        inner = ctx.short(args, limit)
    return ctx.short(f"{name}({inner})", limit)


def _arg(ctx: RenderContext, value: Any) -> str:
    if isinstance(value, str):
        return '"' + ctx.short(value, ARG_CHARS) + '"'
    if value is None or isinstance(value, (bool, int, float)):
        return json.dumps(value)
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return ctx.short(text, ARG_CHARS)


def _ev_activity(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    kind = d.get("kind")
    agent = d.get("agent") or ev.get("actor")
    text = d.get("text")
    s = ctx.sym
    if kind == "error":
        if ctx.quiet:  # quiet shows the u_agent_error notification (once per streak) instead
            return None
        body = Text()
        body.append(f"{clean(agent) or ctx.t('system')}: ", style=f"bold {theme.ERROR}")
        clipped, rest = _clip_lines(ctx, text, RESULT_LINES if ctx.verbose else 3)
        body.append(clipped, style=theme.ERROR)
        if rest:
            body.append("\n").append_text(_more(ctx, rest))
        return _line(ctx, s["warn"], theme.ERROR, body)
    if ctx.quiet:
        return None
    if kind in ("tool_call", "tool_result") and not ctx.verbose and not d.get("is_error") \
            and clean(d.get("tool")) in HOUSEKEEPING_TOOLS | ECHOED_TOOLS:
        return None
    if kind == "tool_call":
        # normal: one line ("⏺ " + name + " · " take 5 cells; the TUI prints one column narrower than ctx.width)
        limit = TOOL_CALL_CHARS * 2 if ctx.verbose else \
            max(24, min(TOOL_CALL_CHARS, ctx.width - 6 - cells(_who(ctx, agent))))
        call = tool_call_text(ctx, d.get("tool"), text, limit)
        return _bullet(ctx, agent, Text(f" {s['dot']} ", style="dim"), Text(call))
    if kind == "tool_result":
        style = theme.ERROR if d.get("is_error") else ""
        if ctx.verbose:
            return _cont(ctx, _clipped_text(ctx, text, RESULT_LINES, style or "dim"))
        line = _first_line(text) or ctx.t("empty")
        return _cont(ctx, _one(ctx, line, style or "dim"))
    if not ctx.verbose:
        return None
    if kind == "thinking":
        head = _bullet(ctx, agent, Text(" " + ctx.t("thinking"), style="dim italic"), symbol=s["spark"])
        return Group(head, Padding(_clipped_text(ctx, text, THINKING_LINES, "dim italic"), (0, 0, 0, 2)))
    if kind == "text":
        head = _bullet(ctx, agent, Text(f" {s['dot']} {ctx.t('k_text')}", style="dim"))
        return Group(head, Padding(_clipped_text(ctx, text, THINKING_LINES, "dim"), (0, 0, 0, 2)))
    if kind == "wake":
        body = Text(f"{clean(agent) or '?'} {ctx.t('wakes')}: ", style="dim")
        body.append(ctx.short(_wake_summary(text), 300), style="dim")
        return _line(ctx, s["alarm"], "dim", body)
    if kind == "nudge":
        body = Text(f"{clean(agent) or '?'}: {ctx.t('nudge')} {s['dash']} ", style="dim")
        body.append(ctx.short(_first_line(text), 300), style="dim")
        return _line(ctx, s["nudge"], "dim", body)
    if kind == "compacted":
        head = _bullet(ctx, agent, Text(f" {s['dot']} {ctx.t('compacted')}", style="dim"), symbol=s["spark"])
        return Group(head, _cont(ctx, _clipped_text(ctx, text, 3, "dim")))
    return _generic(ctx, ev)


def _ev_file(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    if not ctx.verbose:
        return None
    typ = ev.get("type")
    if typ == "file.changed":
        label = _status_label(ctx, "op_", d.get("op"))
    elif typ == "file.locked":
        label = ctx.t("file_locked")
    else:
        label = ctx.t("file_lock_expired") if d.get("expired") else ctx.t("file_unlocked")
    who = d.get("owner") if typ != "file.changed" else ev.get("actor")
    body = Text(f"{clean(d.get('path')) or '?'} {ctx.sym['dash']} {label}", style="dim")
    if who:
        body.append(f" {ctx.sym['dot']} {_who(ctx, who, short=True)}", style="dim")
    return _line(ctx, ctx.sym["edit"], "dim", body)


def _ev_trigger(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    trigger = _d(d.get("trigger"))
    tid = clean(trigger.get("id") if trigger.get("id") is not None else d.get("trigger_id")) or "?"
    errors = [clean(e) for e in _list(d.get("errors")) if e]
    if errors:  # errors show at every verbosity
        return error(ctx, ctx.t("trigger_errors", id=tid, errors="; ".join(errors)))
    if not ctx.verbose:
        return None
    key = {"trigger.created": "trigger_created", "trigger.fired": "trigger_fired"}.get(ev.get("type"),
                                                                                      "trigger_deleted")
    body = Text(ctx.t(key, id=tid, condition=ctx.short(trigger.get("condition"), 160)), style="dim")
    if ev.get("actor"):
        body.append(f" {ctx.sym['dot']} {_who(ctx, ev.get('actor'), short=True)}", style="dim")
    return _line(ctx, ctx.sym["trigger"], "dim", body)


def _mcp_style(status: Any) -> str:
    return {"pending": theme.WARN, "failed": theme.ERROR, "rejected": theme.ERROR, "active": theme.OK}.get(
        status if isinstance(status, str) else "", "dim")


def _ev_mcp(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    if ctx.quiet:  # pending servers reach quiet mode through the u_mcp_pending notification
        return None
    if ev.get("type") == "mcp.created":
        server = _d(d.get("server"))
        status = server.get("status")
        tail = Text(f" {ctx.sym['dot']} {ctx.t('mcp_created', name=clean(server.get('name')) or '?')} ")
        tail.append(f"{ctx.sym['dash']} {_status_label(ctx, 'mcp_', status)}", style=_mcp_style(status))
        if ctx.verbose and server.get("description"):
            tail.append(f" {ctx.sym['dot']} {ctx.short(server.get('description'), 200)}", style="dim")
        return _bullet(ctx, server.get("owner") or ev.get("actor"), tail)
    status = d.get("status")
    body = Text(f"MCP {clean(d.get('name')) or '?'}: ", style="dim")
    body.append(_status_label(ctx, "mcp_", status), style=_mcp_style(status))
    return _line(ctx, ctx.sym["dot"], "dim", body)


def _reason(ctx: RenderContext, reason: Any) -> str:
    raw = clean(reason)
    key = {"budget": "reason_budget", "paused by the user": "reason_user"}.get(raw)
    return ctx.t(key) if key else raw


def _ev_paused(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    reason = _reason(ctx, d.get("reason"))
    text = ctx.t("paused") + (f" ({reason})" if reason else "")
    return _line(ctx, ctx.sym["pause"], theme.WARN, Text(text, style=f"bold {theme.WARN}"))


def _ev_resumed(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    return _line(ctx, ctx.sym["play"], theme.OK, Text(ctx.t("resumed"), style=theme.OK))


def _ev_limits(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    cost = _float(d.get("max_cost_usd"))
    budget = fmt_cost(cost) if cost > 0 else ctx.t("unlimited")
    text = ctx.t("limits", budget=budget, agents=clean(d.get("max_agents")) or "?")
    return _line(ctx, ctx.sym["dot"], "dim", Text(text, style="dim"))


def _ev_session(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    if not ctx.verbose:
        return None
    session = d.get("session")
    sid = _d(session).get("id") if isinstance(session, dict) else session
    text = ctx.t("session_changed", id=clean(sid) or "?", change=clean(d.get("change")))
    return _line(ctx, ctx.sym["dot"], "dim", Text(text, style="dim"))


def _ev_connection(ctx: RenderContext, ev: dict, d: dict) -> RenderableType | None:
    if d.get("state") == "lost":
        return _line(ctx, ctx.sym["warn"], theme.WARN, Text(ctx.t("conn_lost"), style=theme.WARN))
    return None


def _none(ctx: RenderContext, ev: dict, d: dict) -> None:
    return None


_HANDLERS: dict[str, Callable[[RenderContext, dict, dict], RenderableType | None]] = {
    "task.created": _ev_task_created,
    "task.updated": _ev_task_updated,
    "task.comment": _ev_task_comment,
    "message.sent": _ev_message,
    "notification": _ev_notification,
    "goal.set": _ev_goal,
    "goal.completed": _ev_goal,
    "goal.reopened": _ev_goal_reopened,
    "agent.hired": _ev_hired,
    "agent.retired": _ev_retired,
    "agent.status": _ev_status,
    "agent.activity": _ev_activity,
    "agent.compacted": _none,   # the agent.activity "compacted" line carries the summary
    "agent.usage": _none,       # feeds the spinner
    "file.changed": _ev_file,
    "file.locked": _ev_file,
    "file.unlocked": _ev_file,
    "trigger.created": _ev_trigger,
    "trigger.fired": _ev_trigger,
    "trigger.deleted": _ev_trigger,
    "mcp.created": _ev_mcp,
    "mcp.status": _ev_mcp,
    "swarm.paused": _ev_paused,
    "swarm.resumed": _ev_resumed,
    "swarm.limits": _ev_limits,
    "sessions.changed": _ev_session,
    "_connection": _ev_connection,
}


def _generic(ctx: RenderContext, ev: dict) -> RenderableType:
    ev = _d(ev)
    body = Text(clean(ev.get("type")) or "?", style="dim")
    if ev.get("actor"):
        body.append(f" {clean(ev.get('actor'))}", style="dim")
    data = ev.get("data")
    if data:
        body.append(": " + ctx.short(data, 160), style="dim")
    return _line(ctx, ctx.sym["dot"], "dim", body)


def _event(ctx: RenderContext, ev: dict) -> RenderableType | None:
    if not isinstance(ev, dict):
        return None
    typ = ev.get("type") if isinstance(ev.get("type"), str) else ""
    if typ.startswith("trace."):
        return None
    handler = _HANDLERS.get(typ)
    if handler is None:
        return _generic(ctx, ev) if ctx.verbose else None
    return handler(ctx, ev, _d(ev.get("data")))


def event(ctx: RenderContext, ev: dict) -> RenderableType | None:
    """One bus event at the context's verbosity; None when nothing is shown."""
    try:
        return _out(ctx, _event(ctx, ev))
    except Exception:  # a malformed event must never break the UI
        log.debug("cannot render event %r", ev, exc_info=True)
        return _out(ctx, _generic(ctx, ev)) if ctx.verbose else None


def tool_header(ctx: RenderContext, agent: Any, tool: Any) -> RenderableType:
    """`⏺ Анна Смирнова · tool`: names the call of a result that is not printed right under its call line."""
    return _out(ctx, _bullet(ctx, agent, Text(f" {ctx.sym['dot']} ", style="dim"),
                             Text(clean(tool) or "?", style="dim")))


@dataclass
class _HeldCall:
    ev: dict
    tool: str
    since: float                  # when it became the agent's running call (the head of its queue)
    shown: Any = None             # its line once printed alone (_HIDDEN: nothing to print at that verbosity)


_HIDDEN = object()


class ToolPairs:
    """Keeps every tool result under its own call line in a printed feed (the TUI, `-p --verbose`).

    The only stateful helper here. An agent's step emits all its tool_call events first and then one
    tool_result per call, in call order, while other agents' events interleave; a `⎿ result` line alone
    would attach to whatever was printed last. So a call is held until its result and both are printed
    together: `⏺ Анна · task_claim(task_id=3)` + `  ⎿  Claimed task #3`. A call still running after `hold`
    seconds (a long shell command) is printed alone (`due()`); its result then gets a `tool_header()` unless
    the call line is still the last thing printed, as does a result whose call was never seen. Consumers pass
    every renderable they print to `printed()`."""

    def __init__(self, hold: float = TOOL_HOLD):
        self.hold = hold
        self._calls: dict[str, list[_HeldCall]] = {}
        self._last: Any = None    # what the consumer printed last

    def reset(self) -> None:
        self._calls.clear()
        self._last = None

    def printed(self, renderable: Any) -> None:
        self._last = renderable

    def feed(self, ctx: RenderContext, ev: dict, now: float | None = None) -> list[RenderableType] | None:
        """What to print for an `agent.activity` event (possibly nothing yet: a held call); None for every
        other event, which the caller renders with event()."""
        if not isinstance(ev, dict) or ev.get("type") != "agent.activity":
            return None
        d = _d(ev.get("data"))
        agent = clean(d.get("agent") or ev.get("actor"))
        kind = d.get("kind")
        now = time.monotonic() if now is None else now
        if kind == "tool_call":
            queue = self._calls.setdefault(agent, [])
            queue.append(_HeldCall(ev, clean(d.get("tool")), now))
            return []
        if kind != "tool_result":  # a new step of the agent: calls still held never got a result
            out = self._release(ctx, self._calls.pop(agent, []))
            line = event(ctx, ev)
            return out + ([line] if line is not None else [])
        tool = clean(d.get("tool"))
        queue = self._calls.get(agent, [])
        index = next((i for i, held in enumerate(queue) if held.tool == tool), None)
        out: list[RenderableType] = []
        call = None
        if index is not None:
            out += self._release(ctx, queue[:index])
            call = queue[index]
            del queue[:index + 1]
            if queue:
                queue[0].since = now
            else:
                self._calls.pop(agent, None)
        result = event(ctx, ev)
        if result is None:
            return out
        if call is not None and call.shown is None:
            line = event(ctx, call.ev)
            out.append(Group(line, result) if line is not None else Group(tool_header(ctx, agent, tool), result))
        elif call is not None and call.shown is self._last:
            out.append(result)  # right under its call line
        else:
            out.append(Group(tool_header(ctx, agent, tool), result))
        return out

    def due(self, ctx: RenderContext, now: float | None = None) -> list[RenderableType]:
        """The running calls held longer than `hold`, to be printed alone now."""
        now = time.monotonic() if now is None else now
        out: list[RenderableType] = []
        for queue in self._calls.values():
            head = queue[0]
            if head.shown is None and now - head.since >= self.hold:
                line = event(ctx, head.ev)
                head.shown = _HIDDEN if line is None else line
                if line is not None:
                    out.append(line)
        return out

    def next_due(self) -> float | None:
        """When due() has something next (a time.monotonic() value), None when nothing is held."""
        times = [queue[0].since + self.hold for queue in self._calls.values() if queue[0].shown is None]
        return min(times, default=None)

    def _release(self, ctx: RenderContext, calls: list[_HeldCall]) -> list[RenderableType]:
        out: list[RenderableType] = []
        for held in calls:
            if held.shown is None:
                line = event(ctx, held.ev)
                if line is not None:
                    out.append(line)
        return out


# ---- board ----------------------------------------------------------------------------------------
BOARD_COLUMNS = (("todo", ("todo",)), ("in_progress", ("in_progress",)), ("waiting", ("waiting",)),
                 ("review", ("review",)), ("done", ("done",)), ("closed", ("failed", "cancelled")))


def board_summary(ctx: RenderContext, tasks: list[dict] | dict) -> RenderableType:
    """`☐ 3 · ◐ 2 · ◑ 1 · ☒ 5 · ✗ 1` from a task list or snapshot counts ({status: n})."""
    if isinstance(tasks, dict):
        counts = {k: _int(v) for k, v in tasks.items()}
    else:
        counts = {}
        for task in _list(tasks):
            status = _d(task).get("status")
            if isinstance(status, str):
                counts[status] = counts.get(status, 0) + 1
    groups = [("todo", counts.get("todo", 0)),
              ("doing", counts.get("in_progress", 0) + counts.get("waiting", 0)),
              ("review", counts.get("review", 0)), ("done", counts.get("done", 0)),
              ("failed", counts.get("failed", 0) + counts.get("cancelled", 0))]
    parts = [Text(f"{ctx.sym[key]} {n}", style=_task_symbol(ctx, {"doing": "in_progress"}.get(key, key))[1])
             for key, n in groups if n]
    if not parts:
        return _out(ctx, Text(ctx.t("board_empty"), style="dim"))
    return _out(ctx, Text(f"{ctx.t('col_tasks')}: ", style="dim").append_text(_join(ctx, parts)))


def board(ctx: RenderContext, tasks: list[dict]) -> RenderableType:
    tasks = [_d(task) for task in _list(tasks) if isinstance(task, dict)]
    if not tasks:
        return _out(ctx, info(ctx, ctx.t("board_empty")))
    rows: list[RenderableType] = []
    for column, statuses in BOARD_COLUMNS:
        items = [task for task in tasks if task.get("status") in statuses]
        if column == "todo":  # unknown statuses go with todo
            known = {s for _, group in BOARD_COLUMNS for s in group}
            items += [task for task in tasks if task.get("status") not in known]
        if not items:
            continue
        if rows:
            rows.append(Text(""))
        rows.append(Text(f"{_status_label(ctx, 's_', column)} ({len(items)})", style="bold"))
        limit = 0 if ctx.verbose or column not in ("done", "closed") else 15
        shown = items[-limit:] if limit else items
        if len(shown) < len(items):
            rows.append(Text(f"  {ctx.sym['ellipsis']} +{len(items) - len(shown)}", style="dim"))
        for task in shown:
            rows.append(_board_row(ctx, task))
    return _out(ctx, Group(*rows))


def _board_row(ctx: RenderContext, task: dict) -> Table:
    s = ctx.sym
    body = _task_head(ctx, task)
    status = task.get("status")
    if task.get("assignee"):
        body.append(f" {s['dash']} {first_name(task.get('assignee'))}", style=theme.agent_style(task.get("assignee")))
    if status == "review" and task.get("reviewer"):
        label, style = _review_label(ctx, task.get("reviewer"))
        body.append(f" {s['dot']} {label}", style=style)
    priority = _int(task.get("priority"), 1)
    if priority >= 2:
        body.append(f" {s['dot']} {_status_label(ctx, 'p', priority)}",
                    style=theme.ERROR if priority >= 3 else theme.WARN)
    blocked = [f"#{_int(i)}" for i in _list(task.get("blocked_by"))]
    if blocked and status not in ("done", "failed", "cancelled"):
        body.append(f" {s['dot']} {ctx.t('task_blocked', ids=', '.join(blocked))}", style=theme.WARN)
    comments = _int(task.get("comments"))
    if comments:
        body.append(f" {s['dot']} {s['chat']}{comments}", style="dim")
    if ctx.verbose:
        tags = [clean(tag) for tag in _list(task.get("tags")) if tag]
        if tags:
            body.append(f" {s['dot']} " + " ".join("#" + tag for tag in tags), style="dim")
        if task.get("goal"):
            body.append("\n" + ctx.short(task.get("goal"), 300), style="dim")
    return Padding(_task_line(ctx, task, body), (0, 0, 0, 2))


def task_detail(ctx: RenderContext, data: dict) -> RenderableType:
    """/api/tasks/{id}: header, goal, description, result, discussion (and history in verbose)."""
    data = _d(data)
    task = _d(data.get("task"))
    if not task:
        return error(ctx, clean(data.get("error")) or ctx.t("td_not_found"))
    s = ctx.sym
    rows: list[RenderableType] = []
    head = _task_head(ctx, task)
    head.stylize("bold")
    rows.append(_task_line(ctx, task, head))
    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="dim", no_wrap=True)
    meta.add_column()

    def add(key: str, value: Any, style: str = "") -> None:
        if value not in (None, "", [], ()):
            meta.add_row(ctx.t(key), Text(clean(value), style=style))

    status = task.get("status")
    add("td_status", _status_label(ctx, "s_", status), _task_symbol(ctx, status)[1])
    add("td_assignee", task.get("assignee"), theme.agent_style(task.get("assignee")))
    add("td_reviewer", task.get("reviewer"), theme.agent_style(task.get("reviewer")))
    if _int(task.get("priority"), 1) != 1:
        add("td_priority", _status_label(ctx, "p", _int(task.get("priority"), 1)))
    add("td_tags", ", ".join(clean(tag) for tag in _list(task.get("tags"))))
    deps = _list(task.get("depends_on"))
    if deps:
        blocked = {_int(i) for i in _list(task.get("blocked_by"))}
        waiting, done = f" ({ctx.t('task_waiting')})", f" {s['check']}"
        add("td_deps", ", ".join(f"#{_int(i)}" + (waiting if _int(i) in blocked else done) for i in deps))
    if task.get("parent_id"):
        add("td_parent", f"#{clean(task.get('parent_id'))}")
    add("td_created_by", _who(ctx, task.get("created_by")) if task.get("created_by") else None)
    add("td_updated", _clock(task.get("updated_at"), True) if task.get("updated_at") else None, "dim")
    rows.append(Padding(meta, (0, 0, 0, 2)))
    if status == "review":  # the user may settle any review (instead of the reviewer, too)
        rows.append(Padding(Text(ctx.t("td_review_hint", id=clean(task.get("id"))), style=theme.WARN), (0, 0, 0, 2)))
    for key, field in (("td_goal", "goal"), ("td_description", "description")):
        if task.get(field):
            rows += [Text(""), Text(ctx.t(key), style="bold"), Padding(_markdown(ctx, task.get(field)), (0, 0, 0, 2))]
    if task.get("result"):
        color = theme.ERROR if status == "failed" else theme.OK
        rows += [Text(""), Text(ctx.t("td_result"), style=f"bold {color}"),
                 Padding(_markdown(ctx, task.get("result")), (0, 0, 0, 2))]
    posts = [_d(p) for p in _list(data.get("discussion")) if isinstance(p, dict)]
    rows += [Text(""), Text(f"{ctx.t('td_discussion')} ({len(posts)})", style="bold")]
    if not posts:
        rows.append(Padding(Text(ctx.t("td_no_posts"), style="dim"), (0, 0, 0, 2)))
    for post in posts:
        author = post.get("author")
        tail = Text(f" {s['dot']} {_clock(post.get('ts'))}", style="dim")
        if post.get("id") is not None:
            tail.append(f" {s['dot']} #{clean(post.get('id'))}", style="dim")
        if post.get("reply_to"):
            tail.append(f" {s['dot']} {s['reply']} {ctx.t('td_reply', id=clean(post.get('reply_to')))}", style="dim")
        rows.append(_bullet(ctx, author, tail, symbol=s["chat"]))
        rows.append(Padding(_markdown(ctx, post.get("text")), (0, 0, 0, 3)))
    events = [_d(e) for e in _list(data.get("events")) if isinstance(e, dict)]
    if ctx.verbose and events:
        rows += [Text(""), Text(ctx.t("td_history"), style="bold")]
        for ev in events[-30:]:
            rows.append(Padding(_history_line(ctx, ev), (0, 0, 0, 2)))
    return _out(ctx, Group(*rows))


def _history_line(ctx: RenderContext, ev: dict) -> Text:
    d = _d(ev.get("data"))
    line = Text(f"{_clock(ev.get('ts'), True)} ", style="dim")
    line.append(f"{_who(ctx, ev.get('actor'))}: ", style=theme.agent_style(ev.get("actor")))
    typ = ev.get("type")
    if typ == "task.updated":
        parts = []
        for field, change in _d(d.get("changes")).items():
            pair = change if isinstance(change, (list, tuple)) and len(change) == 2 else (None, change)
            old, new = (_value_text(ctx, field, v) for v in pair)
            parts.append(f"{_status_label(ctx, 'f_', field)}: {old} {ctx.sym['arrow']} {new}")
        line.append("; ".join(parts) or ctx.t("task_changed"), style="dim")
    elif typ == "task.comment":
        line.append(ctx.short(_d(d.get("post")).get("text"), 120), style="dim")
    elif typ == "task.created":
        line.append(ctx.t("td_created"), style="dim")
    else:
        line.append(clean(typ), style="dim")
    return line


def _value_text(ctx: RenderContext, field: str, value: Any) -> str:
    if value in (None, "", []):
        return "—"
    if field == "status":
        return _status_label(ctx, "s_", value)
    if field == "priority":
        return _status_label(ctx, "p", value)
    if isinstance(value, (list, tuple)):
        return ", ".join(clean(v) for v in value)
    return ctx.short(value, 40)


# ---- tables ---------------------------------------------------------------------------------------
def _table(ctx: RenderContext, title: str | None = None) -> Table:
    return Table(box=_box(ctx), title=title, title_justify="left", title_style="bold", header_style="bold",
                 border_style="dim", show_edge=True, pad_edge=True, expand=False)


def _agent_status_style(status: Any) -> str:
    return {"working": theme.ACCENT, "error": theme.ERROR, "paused": theme.WARN, "retired": "dim strike"}.get(
        status if isinstance(status, str) else "", "dim")


def agents_table(ctx: RenderContext, agents: list[dict]) -> RenderableType:
    agents = [_d(a) for a in _list(agents) if isinstance(a, dict)]
    if not agents:
        return _out(ctx, info(ctx, ctx.t("no_agents")))
    agents.sort(key=lambda a: a.get("status") == "retired")
    wide = ctx.width >= 80
    table = _table(ctx)
    table.add_column(ctx.t("col_agent"), overflow="fold")
    if wide:
        table.add_column(ctx.t("col_role"), overflow="fold")
    table.add_column(ctx.t("col_status"), no_wrap=wide)
    if wide:
        table.add_column(ctx.t("col_tasks"), overflow="fold")
        table.add_column(ctx.t("col_tokens"), justify="right", no_wrap=True)
    table.add_column(ctx.t("col_cost"), justify="right", no_wrap=True)
    if wide and ctx.width >= 110:
        table.add_column(ctx.t("col_model"), overflow="fold", style="dim")
    for a in agents:
        name, status = a.get("name"), a.get("status")
        cell = Text(f"{ctx.sym['bullet']} ", style=theme.agent_style(name))
        cell.append(clean(name) or "?", style="dim" if status == "retired" else f"bold {theme.agent_style(name)}")
        role = clean(a.get("role"))
        if a.get("kind") == "external":
            role = f"{role} ({ctx.t('external')})" if role else ctx.t("external")
        row: list[RenderableType] = [cell]
        if wide:
            row.append(Text(role, style="dim" if status == "retired" else ""))
        row.append(Text(_status_label(ctx, "a_", status), style=_agent_status_style(status)))
        if wide:
            row.append(Text(", ".join(f"#{_int(i)}" for i in _list(a.get("open_tasks"))), style="dim"))
            up, down = fmt_tokens(a.get("tokens_in")), fmt_tokens(a.get("tokens_out"))
            row.append(Text(f"{ctx.sym['up']}{up} {ctx.sym['down']}{down}", style="dim"))
        row.append(Text(fmt_cost(a.get("cost_usd"))))
        if wide and ctx.width >= 110:
            row.append(Text(clean(a.get("model"))))
        table.add_row(*row)
    return _out(ctx, table)


def _session_status(ctx: RenderContext, s: dict) -> tuple[str, str]:
    goal = _d(s.get("goal"))
    if s.get("config_error") or s.get("error"):
        return ctx.t("sess_error"), theme.ERROR
    if goal.get("status") == "done":
        return ctx.t("sess_done"), theme.OK
    if s.get("status") == "running":
        if s.get("paused"):
            return ctx.t("sess_paused"), theme.WARN
        return ctx.t("sess_running"), theme.ACCENT
    return ctx.t("sess_stopped"), "dim"


def sessions_table(ctx: RenderContext, sessions: list[dict], current: str | None = None) -> RenderableType:
    sessions = [_d(s) for s in _list(sessions) if isinstance(s, dict)]
    if not sessions:
        return _out(ctx, info(ctx, ctx.t("no_sessions")))
    wide = ctx.width >= 90
    table = _table(ctx)
    table.add_column("", no_wrap=True, width=len(ctx.sym["current"]))
    table.add_column(ctx.t("col_session"), overflow="fold")
    table.add_column(ctx.t("col_status"), overflow="fold")
    if wide:
        table.add_column(ctx.t("col_agents"), justify="right", no_wrap=True)
        table.add_column(ctx.t("col_tasks"), justify="right", no_wrap=True)
        table.add_column(ctx.t("col_spent"), justify="right", no_wrap=True)
        table.add_column(ctx.t("col_activity"), no_wrap=True, style="dim")
    for s in sessions:
        sid = clean(s.get("id"))
        mark = Text(ctx.sym["current"], style=theme.ACCENT) if current and s.get("id") == current else Text("")
        title = Text(clean(s.get("title")) or sid or "?", style="bold")
        if sid and sid != clean(s.get("title")):
            title.append(f"  {sid}", style="dim")
        goal = _d(s.get("goal"))
        if goal.get("title"):
            title.append("\n" + ctx.short(goal.get("title"), 120), style="dim")
        label, style = _session_status(ctx, s)
        row: list[RenderableType] = [mark, title, Text(label, style=style)]
        if wide:
            agents, tasks = _d(s.get("agents")), _d(s.get("tasks"))
            row.append(Text(f"{_int(agents.get('working'))}/{_int(agents.get('total'))}"))
            row.append(Text(f"{_int(tasks.get('done'))}/{_int(tasks.get('total'))}"))
            budget = _float(s.get("max_cost_usd"))
            spent = fmt_cost(s.get("spent_usd")) + (f"/{fmt_cost(budget)}" if budget > 0 else "")
            row.append(Text(spent))
            row.append(Text(_ago(ctx, s.get("last_activity") or s.get("created_at"))))
        table.add_row(*row)
    return _out(ctx, table)


def cost_table(ctx: RenderContext, state: dict) -> RenderableType:
    """Tokens and cost per agent; total against the budget (state = /api/state)."""
    state = _d(state)
    swarm = _d(state.get("swarm"))
    agents = sorted((_d(a) for a in _list(state.get("agents")) if isinstance(a, dict)),
                    key=lambda a: -_float(a.get("cost_usd")))
    wide = ctx.width >= 60
    table = _table(ctx)
    table.add_column(ctx.t("col_agent"), overflow="fold")
    if wide:
        table.add_column(ctx.t("col_in"), justify="right", no_wrap=True)
        table.add_column(ctx.t("col_out"), justify="right", no_wrap=True)
    table.add_column(ctx.t("col_cost"), justify="right", no_wrap=True)
    total_in = total_out = 0
    total_cost = 0.0
    for a in agents:
        total_in += _int(a.get("tokens_in"))
        total_out += _int(a.get("tokens_out"))
        total_cost += _float(a.get("cost_usd"))
        name = Text(clean(a.get("name")) or "?", style=theme.agent_style(a.get("name")))
        if wide and a.get("model"):
            name.append(f"  {clean(a.get('model'))}", style="dim")
        row: list[RenderableType] = [name]
        if wide:
            row += [Text(fmt_tokens(a.get("tokens_in"))), Text(fmt_tokens(a.get("tokens_out")))]
        row.append(Text(fmt_cost(a.get("cost_usd"))))
        table.add_row(*row)
    spent = _float(swarm.get("spent_usd"), total_cost) if "spent_usd" in swarm else total_cost
    table.add_section()
    footer: list[RenderableType] = [Text(ctx.t("total"), style="bold")]
    if wide:
        footer += [Text(fmt_tokens(total_in), style="bold"), Text(fmt_tokens(total_out), style="bold")]
    footer.append(Text(fmt_cost(spent), style="bold"))
    table.add_row(*footer)
    budget = _float(_d(swarm.get("limits")).get("max_cost_usd"))
    if budget > 0:
        pct = int(round(spent / budget * 100))
        style = theme.ERROR if pct >= 100 else theme.WARN if pct >= 80 else "dim"
        note = Text(ctx.t("spent_of", spent=fmt_cost(spent), budget=fmt_cost(budget), pct=pct), style=style)
    else:
        note = Text(ctx.t("spent_unlimited", spent=fmt_cost(spent)), style="dim")
    return _out(ctx, Group(table, note))


def models_table(ctx: RenderContext, settings: dict) -> RenderableType:
    """Model profiles of a public settings view (GET /api/settings -> "settings"): ✓ default, model,
    provider and key state (never the key itself)."""
    settings = _d(settings)
    models = _d(settings.get("models"))
    default = _d(settings.get("agents")).get("default_model") or "default"
    if not models:
        return _out(ctx, info(ctx, ctx.t("mt_none")))
    table = _table(ctx, ctx.t("mt_title"))
    table.add_column("", no_wrap=True)
    table.add_column(ctx.t("col_profile"), overflow="fold")
    table.add_column(ctx.t("col_model"), overflow="fold")
    if ctx.width >= 80:
        table.add_column(ctx.t("col_provider"), overflow="fold", style="dim")
    table.add_column(ctx.t("col_key"), overflow="fold")
    for name, profile in models.items():
        profile = _d(profile)
        mark = Text(ctx.sym["check"], style=theme.OK) if name == default else Text("")
        row: list[RenderableType] = [mark, Text(clean(name), style="bold" if name == default else ""),
                                     Text(clean(profile.get("model")))]
        if ctx.width >= 80:
            provider = clean(profile.get("provider"))
            if profile.get("base_url"):
                provider += f" {ctx.sym['dot']} {clean(profile.get('base_url'))}"
            row.append(Text(provider))
        row.append(_key_state(ctx, profile))
        table.add_row(*row)
    return _out(ctx, table)


def roles_table(ctx: RenderContext, catalog: list[dict]) -> RenderableType:
    """/roles: the role catalog [{key, name, description, model, builtin}] (GET /api/roles "roles"): the name, the
    model profile of the role and the first sentence of its description."""
    items = [_d(r) for r in _list(catalog) if isinstance(r, dict)]
    if not items:
        return _out(ctx, info(ctx, ctx.t("no_roles")))
    wide = ctx.width >= 80
    table = _table(ctx, ctx.t("roles_title"))
    table.add_column(ctx.t("col_role"), overflow="fold")
    if wide:
        table.add_column(ctx.t("col_model"), overflow="fold", style="dim")
    table.add_column(ctx.t("col_description"), overflow="fold")
    for role in items:
        row: list[RenderableType] = [Text(clean(role.get("name")) or clean(role.get("key")) or "?", style="bold")]
        if wide:
            row.append(Text(clean(role.get("model"))))
        row.append(Text(roles.summary(clean(role.get("description"))), style="dim"))
        table.add_row(*row)
    return _out(ctx, Group(table, Text(ctx.t("roles_hint"), style="dim")))


def role_detail(ctx: RenderContext, role: dict) -> RenderableType:
    """/roles Role: a panel with the role's name, its full description, extra instructions and model profile."""
    role = _d(role)
    title = Text(clean(role.get("name")) or clean(role.get("key")) or "?", style=f"bold {theme.ACCENT}")
    if role.get("builtin"):
        title.append(f" {ctx.sym['dot']} {ctx.t('builtin_role')}", style="dim")
    rows: list[RenderableType] = [_markdown(ctx, role.get("description") or "—")]
    if role.get("instructions"):
        rows += [Text(""), Text(ctx.t("role_instructions", text=clean(role.get("instructions"))), style="dim")]
    if role.get("model"):
        rows += [Text(""), Text(ctx.t("role_model", model=clean(role.get("model"))), style="dim")]
    return _out(ctx, Panel(Group(*rows), title=title, title_align="left", box=_box(ctx), border_style=theme.ACCENT,
                           padding=(0, 1)))


def _key_state(ctx: RenderContext, profile: dict) -> Text:
    env = clean(profile.get("api_key_env"))
    if profile.get("api_key_set"):
        return Text(ctx.t("key_set", hint=clean(profile.get("api_key_hint"))).strip(), style=theme.OK)
    if env:
        ok = bool(profile.get("api_key_env_set"))
        return Text(ctx.t("key_env" if ok else "key_env_missing", env=env), style=theme.OK if ok else theme.WARN)
    if profile.get("provider") == "scripted":
        return Text(ctx.t("key_not_needed"), style="dim")
    return Text(ctx.t("key_none"), style=theme.WARN)


def status_report(ctx: RenderContext, info: dict) -> RenderableType:  # `info` shadows info() here
    """`/status`: version, home, web, session, workspace, goal, models, language, agents, tracing (keys of `info`;
    others are listed after them as they are). Values may be plain or structured: session / goal dicts,
    agents {total, working}, models [{name, model, default}], budget {spent, limit}, booleans."""
    data = _d(info)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", no_wrap=True)
    grid.add_column(overflow="fold")
    order = ("version", "home", "web", "hub", "session", "workspace", "goal", "models", "model", "language",
             "agents", "budget", "tracing", "verbosity", "connected")
    for key in order + tuple(k for k in data if k not in order):
        if key not in data or data[key] is None:
            continue
        label = ctx.t("sr_" + key)
        grid.add_row(clean(key) if label == "sr_" + key else label, _status_value(ctx, key, data[key]))
    return _out(ctx, Group(Text(ctx.t("sr_title"), style="bold"), Padding(grid, (0, 0, 0, 2))))


def _status_value(ctx: RenderContext, key: str, value: Any) -> Text:
    s = ctx.sym
    if isinstance(value, bool):
        if key == "connected":
            return Text(ctx.t("connected" if value else "disconnected"), style=theme.OK if value else theme.WARN)
        return Text(ctx.t("on" if value else "off"), style=theme.OK if value else "dim")
    if key == "session" and isinstance(value, dict):
        label, style = _session_status(ctx, value)
        out = Text(clean(value.get("title") or value.get("id")) or "?", style="bold")
        if value.get("id") and value.get("title") and value.get("id") != value.get("title"):
            out.append(f" ({clean(value.get('id'))})", style="dim")
        out.append_text(_sep(ctx)).append(label, style=style)
        return out
    if key == "goal" and isinstance(value, dict):
        out = Text(clean(value.get("title")) or "—")
        if value.get("status"):
            done = value.get("status") == "done"
            label = _status_label(ctx, "goal_", value.get("status"))   # "Цель достигнута" is a panel title
            out.append_text(_sep(ctx)).append(label[:1].lower() + label[1:], style=theme.OK if done else "dim")
        return out
    if key == "agents" and isinstance(value, dict):
        out = Text(ctx.tn("n_agents", _int(value.get("total"))))
        if _int(value.get("working")):
            out.append(f" ({ctx.t('working_n', n=_int(value.get('working')))})", style=theme.ACCENT)
        return out
    if key == "agents" and isinstance(value, (list, tuple)):
        names = [clean(_d(a).get("name") if isinstance(a, dict) else a) for a in value]
        return Text(f"{len(names)}: " + ", ".join(names) if names else "0")
    if key == "models" and isinstance(value, (list, tuple)):
        parts = []
        for m in value:
            if isinstance(m, dict):
                text = clean(m.get("name")) + (f" ({clean(m.get('model'))})" if m.get("model") else "")
                parts.append(text + (f" {s['check']}" if m.get("default") else ""))
            else:
                parts.append(clean(m))
        return Text(", ".join(parts))
    if key == "models" and isinstance(value, dict):
        return Text(", ".join(clean(k) for k in value))
    if key == "budget" and isinstance(value, dict):
        spent, limit = _float(value.get("spent")), _float(value.get("limit"))
        return Text(fmt_cost(spent) + (f" / {fmt_cost(limit)}" if limit > 0 else f" ({ctx.t('unlimited')})"))
    if key == "verbosity" and isinstance(value, str):
        return Text(_status_label(ctx, "verbosity_", value))
    return Text(clean(value))


# ---- traces ---------------------------------------------------------------------------------------
def _trace_status_style(status: Any) -> str:
    return {"done": theme.OK, "running": theme.ACCENT, "error": theme.ERROR, "refusal": theme.ERROR,
            "interrupted": theme.WARN, "step_limit": theme.WARN, "paused": theme.WARN}.get(
        status if isinstance(status, str) else "", "dim")


def _trace_stats(ctx: RenderContext, tr: dict) -> list[str]:
    s = ctx.sym
    if tr.get("kind") == "external":
        return [ctx.t("tr_external"), ctx.tn("n_tools", _int(tr.get("tool_calls"))), fmt_ms(tr.get("duration_ms"))]
    prompt = _int(tr.get("input_tokens")) + _int(tr.get("cache_read_tokens")) + _int(tr.get("cache_write_tokens"))
    parts = [ctx.tn("n_steps", _int(tr.get("steps"))), f"{_int(tr.get('llm_calls'))} LLM",
             ctx.tn("n_tools", _int(tr.get("tool_calls")))]
    if _int(tr.get("errors")):
        parts.append(ctx.tn("n_errors", _int(tr.get("errors"))))
    parts += [fmt_ms(tr.get("duration_ms")),
              f"{s['up']} {fmt_tokens(prompt)} {s['down']} {fmt_tokens(tr.get('output_tokens'))}",
              fmt_cost(tr.get("cost_usd"), 4)]
    return parts


def _trace_head(ctx: RenderContext, tr: dict, title: bool = False) -> Table:
    status = tr.get("status")
    tid = clean(tr.get("id")) or "?"
    body = Text((ctx.t("tr_title", id=tid) if title else f"#{tid}") + " ", style="bold")
    agent = tr.get("agent")
    body.append(clean(agent) or "?", style=f"bold {theme.agent_style(agent)}")
    body.append_text(_sep(ctx))
    body.append(_status_label(ctx, "tr_", status), style=_trace_status_style(status))
    for part in _trace_stats(ctx, tr):
        body.append_text(_sep(ctx)).append(part, style="dim")
    tasks = [f"#{_int(i)}" for i in _list(tr.get("tasks"))]
    if tasks:
        body.append_text(_sep(ctx)).append(f"{ctx.t('tr_tasks')} {', '.join(tasks)}", style="dim")
    if tr.get("started_at") and not title:
        body.append_text(_sep(ctx)).append(_clock(tr.get("started_at"), True), style="dim")
    return _line(ctx, ctx.sym["bullet"], _trace_status_style(status), body)


def traces_list(ctx: RenderContext, traces: list[dict]) -> RenderableType:
    traces = [_d(tr) for tr in _list(traces) if isinstance(tr, dict)]
    if not traces:
        return _out(ctx, info(ctx, ctx.t("tr_none")))
    rows: list[RenderableType] = []
    for tr in traces:
        rows.append(_trace_head(ctx, tr))
        if ctx.verbose and tr.get("trigger"):
            rows.append(_cont(ctx, _one(ctx, f"{ctx.t('tr_woke')}: {_first_line(tr.get('trigger'))}", "dim")))
        if tr.get("outcome"):
            rows.append(_cont(ctx, _one(ctx, tr.get("outcome"), "dim")))
    rows.append(Text(ctx.t("tr_hint"), style="dim"))
    return _out(ctx, Group(*rows))


def trace_detail(ctx: RenderContext, trace: dict) -> RenderableType:
    """Header and the spans as ⏺/⎿ lines (llm: tokens, cost, stop reason, tool calls)."""
    trace = _d(trace)
    if not trace or (trace.get("error") and not trace.get("id")):  # an API error body
        return error(ctx, clean(trace.get("error")) or ctx.t("tr_none"))
    rows: list[RenderableType] = [_trace_head(ctx, trace, title=True)]
    times = ctx.t("tr_started", start=_clock(trace.get("started_at"), True), end=_clock(trace.get("ended_at"), True))
    if trace.get("model"):
        times += f" {ctx.sym['dot']} {ctx.t('model')}: {clean(trace.get('model'))}"
    rows.append(Padding(Text(times, style="dim"), (0, 0, 0, 2)))
    if trace.get("trigger"):
        rows.append(Padding(Text(ctx.t("tr_woke") + ":", style="bold"), (1, 0, 0, 2)))
        rows.append(Padding(_clipped_text(ctx, trace.get("trigger"), 30 if ctx.verbose else 6, "dim"), (0, 0, 0, 4)))
    rows.append(Text(""))
    for span in _list(trace.get("spans")):
        if isinstance(span, dict):
            rows += _span_rows(ctx, span, trace.get("agent"))
    if trace.get("outcome"):
        outcome = Text(f"{ctx.t('tr_outcome')}: {clean(trace.get('outcome'))}", style="bold")
        rows += [Text(""), _line(ctx, ctx.sym["cont"], "dim", outcome)]
    return _out(ctx, Group(*rows))


def _span_rows(ctx: RenderContext, span: dict, agent: Any) -> list[RenderableType]:
    s = ctx.sym
    kind, name, status = span.get("kind"), clean(span.get("name")), span.get("status")
    meta, out, inp = _d(span.get("meta")), span.get("output"), span.get("input")
    tag = Text(f"  #{clean(span.get('id'))}", style="dim") if span.get("id") is not None else Text("")
    step = Text(f"[{_int(span.get('step'))}] ", style="dim")
    lines = 6 if ctx.verbose else 2
    err_style = theme.ERROR if status == "error" else ""
    rows: list[RenderableType] = []
    if kind == "llm":
        body = step.copy()
        body.append(name or "llm", style=f"bold {err_style}".strip())
        usage = _d(span.get("usage"))
        parts = [fmt_ms(span.get("duration_ms"))]
        if usage:
            prompt = sum(_int(usage.get(k)) for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens"))
            parts.append(f"{s['up']} {fmt_tokens(prompt)} {s['down']} {fmt_tokens(usage.get('output_tokens'))}")
            parts.append(fmt_cost(span.get("cost_usd"), 4))
        if meta.get("purpose") == "compaction":
            parts.append(ctx.t("compacted"))
        stop = _d(out).get("stop_reason") or meta.get("stop_reason")
        if stop:
            parts.append(f"stop={clean(stop)}")
        for part in parts:
            body.append_text(_sep(ctx)).append(part, style="dim")
        body.append_text(tag)
        rows.append(_line(ctx, s["bullet"], err_style or theme.agent_style(agent), body))
        o = _d(out)
        if o.get("thinking"):
            rows.append(_cont(ctx, _clipped_text(ctx, o.get("thinking"), lines, "dim italic")))
        if o.get("text"):
            rows.append(_cont(ctx, _clipped_text(ctx, o.get("text"), lines)))
        for call in _list(o.get("tool_calls")):
            call = _d(call)
            text = tool_call_text(ctx, call.get("name"), call.get("arguments"))
            rows.append(_cont(ctx, Text(f"{s['arrow']} {text}", style=theme.INFO)))
    elif kind == "tool":
        body = step.copy()
        body.append(tool_call_text(ctx, name, inp), style=f"bold {err_style}".strip())
        body.append_text(_sep(ctx)).append(fmt_ms(span.get("duration_ms")), style="dim")
        body.append_text(tag)
        rows.append(_line(ctx, s["bullet"], err_style or theme.INFO, body))
        result = _clipped_text(ctx, out, lines, err_style or "dim") if ctx.verbose else \
            _one(ctx, _first_line(out) or ctx.t("empty"), err_style or "dim")
        rows.append(_cont(ctx, result))
    else:
        symbol = {"input": s["alarm"] if name in ("wake", "incoming") else s["nudge"],
                  "compaction": s["spark"]}.get(kind if isinstance(kind, str) else "", s["dot"])
        body = step.copy()
        body.append(f"{clean(kind) or '?'}/{name}: ", style=err_style or "dim")
        text = out if out not in (None, "") else inp
        body.append(ctx.short(_first_line(text) if not isinstance(text, (dict, list)) else text, 200),
                    style=err_style or "dim")
        body.append_text(tag)
        rows.append(_line(ctx, symbol, err_style or "dim", body))
    if span.get("error"):
        retry = f" ({ctx.t('retry_in', s=clean(meta.get('retry_in')))})" if meta.get("retry_in") else ""
        rows.append(_cont(ctx, Text(ctx.short(span.get("error"), 600) + retry, style=theme.ERROR)))
    return rows


def _json_block(ctx: RenderContext, value: Any, limit: int = 20000) -> RenderableType:
    if isinstance(value, str):
        text = clean(value)
        if len(text) > limit:
            text = text[:limit] + "\n" + ctx.t("chars_more", n=len(text) - limit)
        return Text(text)
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        text = str(value)
    text = clean(text)
    if len(text) > limit:
        return Text(text[:limit] + "\n" + ctx.t("chars_more", n=len(text) - limit))
    return Syntax(text, "json", theme=theme.CODE_THEME, background_color="default", word_wrap=True)


def span_detail(ctx: RenderContext, span: dict) -> RenderableType:
    span = _d(span)
    if not span or (span.get("error") and span.get("id") is None):  # an API error body
        return error(ctx, clean(span.get("error")) or ctx.t("tr_none"))
    s = ctx.sym
    kind, status = span.get("kind"), span.get("status")
    head = Text(ctx.t("span_title", id=clean(span.get("id")) or "?", trace=clean(span.get("trace_id")) or "?"),
                style="bold")
    agent = span.get("agent")
    if agent:
        head.append_text(_sep(ctx)).append(clean(agent), style=f"bold {theme.agent_style(agent)}")
    head.append_text(_sep(ctx)).append(ctx.t("span_step", n=_int(span.get("step"))), style="dim")
    sub = Text(f"{clean(kind) or '?'} {clean(span.get('name'))}".strip(), style="bold")
    sub.append_text(_sep(ctx)).append(clean(status) or "?", style=_trace_status_style(
        "done" if status == "ok" else status))
    sub.append_text(_sep(ctx)).append(fmt_ms(span.get("duration_ms")), style="dim")
    sub.append_text(_sep(ctx)).append(_clock(span.get("started_at"), True), style="dim")
    rows: list[RenderableType] = [head, sub]
    usage = _d(span.get("usage"))
    if usage:
        prompt = sum(_int(usage.get(k)) for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens"))
        rows.append(Text(f"{ctx.t('span_usage')}: {s['up']} {fmt_tokens(prompt)} {s['down']} "
                         f"{fmt_tokens(usage.get('output_tokens'))} "
                         f"(cache {fmt_tokens(usage.get('cache_read_tokens'))}) "
                         f"{s['dot']} {fmt_cost(span.get('cost_usd'), 4)}", style="dim"))
    meta = _d(span.get("meta"))
    if meta:
        rows.append(Text(f"{ctx.t('span_meta')}: {ctx.short(meta, 400)}", style="dim"))

    def section(key: str, body: RenderableType, style: str = "bold") -> None:
        rows.extend([Text(""), Text(ctx.t(key), style=style), Padding(body, (0, 0, 0, 2))])

    inp, out = span.get("input"), span.get("output")
    if kind == "llm":
        req = _d(inp)
        rows.append(Text(ctx.t("span_request", n=_int(req.get("messages")), epoch=_int(req.get("epoch")),
                               id=clean(span.get("id"))), style="dim"))
        inp = req.get("extra") or None
        o = _d(out)
        if o.get("thinking"):
            section("span_thinking", _clipped_text(ctx, o.get("thinking"), 400, "dim italic"))
        if o.get("text"):
            section("span_text", _clipped_text(ctx, o.get("text"), 400))
        calls = [_d(c) for c in _list(o.get("tool_calls"))]
        if calls:
            items: list[RenderableType] = []
            for call in calls:
                items.append(Text(f"{s['arrow']} {clean(call.get('name'))}", style=f"bold {theme.INFO}"))
                items.append(Padding(_json_block(ctx, call.get("arguments"), 4000), (0, 0, 0, 2)))
            section("span_calls", Group(*items))
        out = None if isinstance(out, dict) else out
    if inp not in (None, "", [], {}):
        section("span_input", _json_block(ctx, inp))
    if out not in (None, "", [], {}):
        section("span_output", _json_block(ctx, out), f"bold {theme.ERROR}" if status == "error" else "bold")
    if span.get("error"):
        section("span_error", Text(clean(span.get("error")), style=theme.ERROR), f"bold {theme.ERROR}")
    return _out(ctx, Group(*rows))


def context_view(ctx: RenderContext, context: dict, offset: int = 0, limit: int = 30) -> RenderableType:
    """The request an LLM span sent: system prompt (at offset 0) and messages[offset:offset+limit]."""
    context = _d(context)
    if not context:
        return error(ctx, ctx.t("tr_none"))
    s = ctx.sym
    messages = _list(context.get("messages"))
    offset, limit = max(0, _int(offset)), max(1, _int(limit, 30))
    chosen = messages[offset:offset + limit]
    span_id = clean(context.get("span_id")) or "?"
    head = Text(ctx.t("ctx_title", id=span_id, agent=clean(context.get("agent")) or "?",
                      model=clean(context.get("model")) or "?"), style="bold")
    if chosen:
        shown = ctx.t("ctx_summary", total=len(messages), a=offset + 1, b=offset + len(chosen),
                      tools=len(_list(context.get("tools"))), chars=len(clean(context.get("system"))))
    else:
        shown = ctx.t("ctx_none_from", offset=offset)
    rows: list[RenderableType] = [head, Text(shown, style="dim")]
    if context.get("note"):
        rows.append(Text(clean(context.get("note")), style=theme.WARN))
    if offset == 0 and context.get("system"):
        rows += [Text(""), Text("SYSTEM", style=f"bold {theme.ACCENT}"),
                 Padding(_clipped_text(ctx, ctx_clip(context.get("system"), 4000), 80 if ctx.verbose else 30, "dim"),
                         (0, 0, 0, 2))]
    for i, msg in enumerate(chosen, start=offset):
        msg = _d(msg)
        role = clean(msg.get("role")) or "?"
        color = {"user": theme.ACCENT, "assistant": theme.INFO}.get(role, "dim")
        label = Text(f"[{i}] {role.upper()}", style=f"bold {color}")
        if role == "tool" and msg.get("name"):
            label.append(f" {clean(msg.get('name'))}", style="bold")
        if msg.get("is_error"):
            label.append(" (error)", style=theme.ERROR)
        body = clean(msg.get("content")).strip()
        items: list[RenderableType] = []
        if body:
            items.append(_clipped_text(ctx, ctx_clip(body, 4000), 60 if ctx.verbose else 20))
        for call in _list(msg.get("tool_calls")):
            call = _d(call)
            text = tool_call_text(ctx, call.get("name"), call.get("arguments"), 200)
            items.append(Text(f"{s['arrow']} {text}", style=theme.INFO))
        rows += [Text(""), label, Padding(Group(*items), (0, 0, 0, 2))]
    rest = len(messages) - (offset + len(chosen))
    if rest > 0 and chosen:
        rows += [Text(""), Text(ctx.t("ctx_more", n=rest, id=span_id, offset=offset + len(chosen)), style="dim")]
    return _out(ctx, Group(*rows))


def ctx_clip(value: Any, limit: int) -> str:
    text = clean(value)
    return text if len(text) <= limit else text[:limit] + "…"


# ---- files, help, misc ----------------------------------------------------------------------------
def _size(n: Any) -> str:
    n = _int(n)
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def files_view(ctx: RenderContext, data: dict) -> RenderableType:
    """A directory listing, or a file with syntax highlighting (lexer from the extension)."""
    data = _d(data)
    if data.get("error"):
        return error(ctx, clean(data.get("error")))
    path = clean(data.get("path")) or "."
    if data.get("type") == "dir":
        entries = [_d(e) for e in _list(data.get("entries")) if isinstance(e, dict)]
        entries.sort(key=lambda e: (e.get("type") != "dir", clean(e.get("path")).lower()))
        head = Text(f"{ctx.sym['folder']} ", style=theme.INFO)
        head.append(path.rstrip("/") + "/", style=f"bold {theme.INFO}")
        rows: list[RenderableType] = [head]
        if not entries:
            rows.append(Padding(Text(ctx.t("fl_empty"), style="dim"), (0, 0, 0, 2)))
            return _out(ctx, Group(*rows))
        grid = Table.grid(padding=(0, 2))
        grid.add_column(overflow="fold")
        grid.add_column(justify="right", no_wrap=True, style="dim")
        if ctx.width >= 60:
            grid.add_column(no_wrap=True, style="dim")
        for e in entries:
            name = clean(e.get("path")).rstrip("/").rsplit("/", 1)[-1] or "?"
            is_dir = e.get("type") == "dir"
            label = Text(name + "/", style=f"bold {theme.INFO}") if is_dir else Text(name)
            row: list[RenderableType] = [label, Text("" if is_dir else _size(e.get("size")))]
            if ctx.width >= 60:
                row.append(Text(_clock(e.get("mtime"), True) if e.get("mtime") else ""))
            grid.add_row(*row)
        rows.append(Padding(grid, (0, 0, 0, 2)))
        return _out(ctx, Group(*rows))
    content = data.get("content")
    head = Text(path, style="bold")
    if data.get("truncated"):
        head.append(f"  {ctx.t('fl_truncated')}", style=theme.WARN)
    if not isinstance(content, str) or not content:
        return _out(ctx, Group(head, Padding(Text(ctx.t("fl_binary"), style="dim"), (0, 0, 0, 2))))
    code = clean(content).removesuffix("\n")   # the final newline is no line of its own (as in `cat -n`)
    lines = code.splitlines()
    tail: list[RenderableType] = []
    if len(lines) > MAX_FILE_LINES:
        code = "\n".join(lines[:MAX_FILE_LINES])
        tail.append(_more(ctx, len(lines) - MAX_FILE_LINES))
    try:
        lexer = Syntax.guess_lexer(path, code)
    except Exception:
        lexer = "text"
    syntax = Syntax(code, lexer, theme=theme.CODE_THEME, background_color="default", line_numbers=True,
                    word_wrap=True)
    return _out(ctx, Group(head, syntax, *tail))


def _command_fields(ctx: RenderContext, item: Any) -> tuple[str, str, str]:
    """(names, args, description) of a command given as a dict, a (name, args, description) tuple or an
    object with name/args/description (or help/desc)/aliases; a description may be {"ru": .., "en": ..}."""
    aliases: Any = None
    if isinstance(item, dict):
        name, args, aliases = item.get("name"), item.get("args"), item.get("aliases")
        desc = item.get("description", item.get("help"))
    elif isinstance(item, (list, tuple)):
        name, args, desc = (list(item) + [None, None, None])[:3]
    else:
        name, args, aliases = getattr(item, "name", item), getattr(item, "args", ""), getattr(item, "aliases", None)
        desc = getattr(item, "description", None) or getattr(item, "help", None) or getattr(item, "desc", None)
    if isinstance(desc, dict):
        desc = desc.get(ctx.lang) or desc.get("en") or next(iter(desc.values()), "")
    names = [clean(n) for n in [name, *_list(aliases)] if n]
    return ", ".join(n if n.startswith("/") else "/" + n for n in names), clean(args), clean(desc)


def help_view(ctx: RenderContext, commands: list) -> RenderableType:
    """Commands table (name, args, description) and the key hints."""
    grid = Table.grid(padding=(0, 2))
    wide = ctx.width >= 70
    grid.add_column(no_wrap=wide, overflow="fold", style=theme.ACCENT)
    if wide:
        grid.add_column(no_wrap=False, overflow="fold", style="dim")
    grid.add_column(overflow="fold")
    for item in _list(commands):
        name, args, desc = _command_fields(ctx, item)
        if wide:
            grid.add_row(Text(name), Text(args), Text(desc))
        else:
            cell = Text(name)
            if args:
                cell.append(" " + args, style="dim")
            grid.add_row(cell, Text(desc))
    return _out(ctx, Group(Text(ctx.t("help_title"), style="bold"), Padding(grid, (0, 0, 0, 2)), Text(""),
                           Text(ctx.t("help_footer"), style="dim")))


SHORTCUTS = (("Enter", "k_enter"), ("Alt+Enter, Ctrl+J", "k_newline"), ("/", "k_slash"),
             ("@", "k_at"), ("#", "k_hash"), ("!", "k_bang"), ("Tab", "k_tab"), ("{up}/{down}", "k_updown"),
             ("Esc", "k_esc"), ("Shift+Tab", "k_shift_tab"), ("Ctrl+O", "k_ctrl_o"), ("Ctrl+L", "k_ctrl_l"),
             ("Ctrl+C", "k_ctrl_c"), ("Ctrl+D", "k_ctrl_d"))


def shortcuts_view(ctx: RenderContext) -> RenderableType:
    """The "?" help: keys and what they do, in two columns when the terminal is wide enough."""
    items = [(key.format(up=ctx.sym["up"], down=ctx.sym["down"]), ctx.t(desc)) for key, desc in SHORTCUTS]
    columns = 2 if ctx.width >= 90 else 1
    grid = Table.grid(padding=(0, 2))
    for _ in range(columns):
        grid.add_column(no_wrap=ctx.width >= 60, style=theme.ACCENT)
        grid.add_column()
    per = (len(items) + columns - 1) // columns
    for i in range(per):
        row: list[RenderableType] = []
        for c in range(columns):
            j = c * per + i
            key, desc = items[j] if j < len(items) else ("", "")
            row += [Text(key), Text(desc, style="dim")]
        grid.add_row(*row)
    return _out(ctx, Group(Text(ctx.t("keys_title"), style="bold"), Padding(grid, (0, 0, 0, 2))))


def info(ctx: RenderContext, text: str) -> RenderableType:
    """Dim `  ⎿  text`."""
    return _out(ctx, _cont(ctx, Text(clean(text), style="dim")))


def error(ctx: RenderContext, text: str) -> RenderableType:
    """Red `⚠ text`."""
    return _out(ctx, _line(ctx, ctx.sym["warn"], theme.ERROR, Text(clean(text), style=theme.ERROR)))


def bash_result(ctx: RenderContext, command: str, code: int | None, output: str) -> RenderableType:
    """`! command` and its output under `  ⎿  `; a non-zero exit code or a kill is shown in colour."""
    head = Text("! ", style=f"bold {theme.BASH}")
    head.append(clean(command), style="bold")
    body: list[RenderableType] = []
    text = clean(output).rstrip()
    if text:
        body.append(_clipped_text(ctx, text, 1000 if ctx.verbose else 200))
    else:
        body.append(Text(ctx.t("bash_no_output"), style="dim"))
    if code is None:
        body.append(Text(ctx.t("bash_killed"), style=theme.WARN))
    elif code != 0:
        body.append(Text(ctx.t("bash_exit", code=code), style=theme.ERROR))
    return _out(ctx, Group(head, _cont(ctx, Group(*body))))


# ---- bottom area (prompt_toolkit formatted text) ---------------------------------------------------
_VERBS = {
    "ru": ["Совещаются", "Планируют", "Размышляют", "Колдуют", "Мозгуют", "Кодят", "Шуршат", "Корпят",
           "Соображают", "Химичат", "Мастерят", "Генерируют идеи"],
    "en": ["Huddling", "Scheming", "Pondering", "Tinkering", "Swarming", "Brewing", "Noodling", "Conjuring",
           "Hatching", "Percolating", "Crafting", "Mulling"],
}


def spinner_verbs(lang: str) -> list[str]:
    return list(_VERBS.get(lang) or _VERBS["en"])


def spinner_frame(ctx: RenderContext, frame: int) -> str:
    """Frame `frame` of the ping-pong animation (· ✢ ✳ ✶ ✻ ✽ ✻ ✶ ✳ ✢ ...)."""
    frames = SPINNER_FRAMES if ctx.unicode else SPINNER_FRAMES_ASCII
    cycle = frames + frames[-2:0:-1] if ctx.unicode else frames
    return cycle[_int(frame) % len(cycle)]


# prompt_toolkit fragments have no width to keep: "…" may become "..."
_ASCII_FRAGMENTS = {**_ASCII_MAP, ord("…"): "...", ord("→"): "->"}


def _frags(ctx: RenderContext, parts: list[tuple[str, str]]) -> list[tuple[str, str]]:
    if ctx.unicode:
        return parts
    return [(style, text.translate(_ASCII_FRAGMENTS)) for style, text in parts]


def cells(text: str) -> int:
    """Width of `text` in terminal cells the way prompt_toolkit lays the bottom area out (wcwidth per code
    point: "⚠️" is 1, "👍🏽" is 4), which is what keeps the box borders and the status line aligned. rich
    measures grapheme clusters instead, so its widths are not used for formatted text."""
    return get_cwidth(text) if text else 0


def cut_cells(text: str, width: int) -> str:
    """The longest prefix of `text` that is at most `width` cells wide (see cells())."""
    used = 0
    for i, ch in enumerate(text):
        used += get_cwidth(ch)
        if used > width:
            return text[:i]
    return text


def _fit(parts: list[tuple[str, str]], width: int, ell: str = "…") -> list[tuple[str, str]]:
    """Cut formatted text to `width` cells (the last fragment gets an ellipsis)."""
    out: list[tuple[str, str]] = []
    used = 0
    for style, text in parts:
        size = cells(text)
        if used + size <= width:
            out.append((style, text))
            used += size
            continue
        room = width - used
        if room > len(ell):
            out.append((style, cut_cells(text, room - len(ell)) + ell))
        break
    return out


def _flen(parts: list[tuple[str, str]]) -> int:
    return sum(cells(text) for _, text in parts)


def spinner_line(ctx: RenderContext, *, frame: int, verb: str, working: list[str], elapsed: float,
                 tokens_in: int, tokens_out: int, cost: float, paused: bool) -> list[tuple[str, str]]:
    """`✻ Совещаются… (12s · ↑ 4.2k ↓ 310 · $0.12 · esc — пауза)  Анна, Борис`."""
    s = ctx.sym
    stats: list[str] = []
    if not paused or _names(working):  # a paused swarm with nobody finishing a step has nothing to count
        stats.append(fmt_elapsed(elapsed))
        if _int(tokens_in) or _int(tokens_out):
            stats.append(f"{s['up']} {fmt_tokens(tokens_in)} {s['down']} {fmt_tokens(tokens_out)}")
        if _float(cost) > 0:
            stats.append(fmt_cost(cost))
    stats.append(ctx.t("sp_resume") if paused else ctx.t("sp_esc"))
    dot = f" {s['dot']} "
    if paused:
        head = [("class:spinner.paused", f"{s['pause']} {ctx.t('paused')}")]
    else:
        head = [("class:spinner", spinner_frame(ctx, frame) + " "),
                ("class:spinner.verb", f"{clean(verb) or spinner_verbs(ctx.lang)[0]}{s['ellipsis']}")]
    head = _frags(ctx, head)
    info_part = _frags(ctx, [("class:spinner.info", f" ({dot.join(stats)})")])
    names = [first_name(n) for n in _names(working)]
    width = max(10, _int(ctx.width, 100) - 1)
    agents: list[tuple[str, str]] = []
    if names:
        shown = names
        while shown:
            more = len(names) - len(shown)
            text = "  " + ", ".join(shown) + (f" +{more}" if more else "")
            if _flen(head + info_part) + cells(text) <= width or len(shown) == 1:
                agents = _frags(ctx, [("class:spinner.agents", text)])
                break
            shown = shown[:-1]
    line = head + info_part + agents
    if _flen(line) > width:
        line = head + info_part
    if _flen(line) > width:
        line = _fit(line, width, s["ellipsis"])
    return line


def status_line(ctx: RenderContext, *, session: dict | None, addressee: str | None, agents_total: int,
                working: int, spent: float, budget: float, model: str | None, verbosity: str, connected: bool,
                hint: str | None) -> tuple[list, list]:
    """(left, right): left = hint / mode / "? — горячие клавиши"; right = session · → addressee · agents ·
    $spent/$budget · model."""
    s = ctx.sym
    if not connected:
        left = [("class:status.warn", f"{s['warn']} {ctx.t('st_disconnected')}")]
    elif hint:
        left = [("class:status.hint", clean(hint))]
    elif verbosity in ("quiet", "verbose"):
        left = [("class:status.mode", ctx.t("st_mode", mode=_status_label(ctx, "verbosity_", verbosity)))]
    else:
        left = [("class:status.hint", ctx.t("st_shortcuts"))]
    left = _frags(ctx, left)
    items: list[tuple[int, list[tuple[str, str]]]] = []  # (drop priority, fragments): higher drops first
    sess = _d(session)
    if sess:
        items.append((1, [("class:status.session", ctx.short(sess.get("title") or sess.get("id") or "?", 32))]))
    else:
        items.append((1, [("class:status", ctx.t("st_no_session"))]))
    if addressee:
        target = ctx.t("everyone") if addressee == "all" else ctx.short(addressee, 24)
        items.append((2, [("class:status.addressee", f"{s['arrow']} {target}")]))
    if sess or _int(agents_total):
        text = ctx.tn("n_agents", _int(agents_total))
        if _int(working):
            text += f" ({ctx.t('working_n', n=_int(working))})"
        items.append((3, [("class:status", text)]))
    if sess or _float(spent):
        style = "class:status"
        if _float(budget) > 0:
            money = f"{fmt_cost(spent)}/{fmt_cost(budget)}"
            if _float(spent) >= _float(budget):
                style = "class:status.error"
            elif _float(spent) >= 0.8 * _float(budget):
                style = "class:status.warn"
        else:
            money = fmt_cost(spent)
        items.append((2, [(style, money)]))
    if model:
        items.append((4, [("class:status", ctx.short(model, 32))]))
    width = max(10, _int(ctx.width, 100) - 4)  # two-space margins on both sides

    def build(chosen: list[tuple[int, list[tuple[str, str]]]]) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for i, (_, frags) in enumerate(chosen):
            if i:
                out.append(("class:status.sep", f" {s['dot']} "))
            out += _frags(ctx, frags)
        return out

    right = build(items)
    while items and _flen(left) + 2 + _flen(right) > width:
        drop = max(range(len(items)), key=lambda i: (items[i][0], i))
        items.pop(drop)
        right = build(items)
    if _flen(left) + 2 + _flen(right) > width:
        left = _fit(left, max(0, width - _flen(right) - 2), s["ellipsis"])
    return left, right


# ---- output ---------------------------------------------------------------------------------------
def _console(width: int, ansi: bool) -> Console:
    return Console(file=StringIO(), force_terminal=ansi, color_system="truecolor" if ansi else None,
                   width=max(10, _int(width, 100)), legacy_windows=False, no_color=not ansi, highlight=False,
                   emoji=False, markup=False, force_jupyter=False, force_interactive=False, soft_wrap=False)


def to_ansi(renderable: RenderableType | None, width: int) -> str:
    """The renderable as an ANSI string at `width` columns, without the final newline."""
    if renderable is None:
        return ""
    console = _console(width, True)
    console.print(renderable)
    text = console.file.getvalue()
    text = text[:-1] if text.endswith("\n") else text
    # unstyled padding at line ends would only make lines wrap when the terminal gets narrower
    return "\n".join(line.rstrip(" ") for line in text.split("\n"))


def to_plain(renderable: RenderableType | None, width: int) -> str:
    """The renderable as plain text (no escape codes, no trailing spaces), e.g. for tests and `-p`."""
    if renderable is None:
        return ""
    console = _console(width, False)
    console.print(renderable)
    text = _ANSI.sub("", console.file.getvalue())
    return "\n".join(line.rstrip() for line in text.rstrip("\n").split("\n"))
