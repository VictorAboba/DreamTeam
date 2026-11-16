"""One plain line per journaled event, for people (docs/SPEC.md §21.6): the `text` of an event the HTTP API sends.

`event_text(event, lang, me=USER)` has the meaning of the terminal UI's verbose feed line as `me` sees it
(tui/render.py `event`), without its terminal parts: no wrapping, panels or colours, no clock (the event's `ts` has
the time; a held trigger's wake-up says for how long), no width limit. One line: whitespace collapsed, control
characters and the invisible bidi and zero-width format characters removed.

- A member's bullet (the coloured ⏺ of the feed) is left out, the other symbols stay: "◐ #3 Parse the document —
  Anna", "Anna Smith · task_claim(task_id=3)".
- A tool call is journaled before the tool runs, so its line is the call alone, and the tool's result has a line of
  its own: "Anna Smith · task_claim ⎿ Claimed task #3" (an error marked ✗).
- The goal panel is one line: "Goal: <title>", "✓ Goal achieved: <title> — <first line of the summary>".
- A run's start and end, which the session manager journals: "▶ The session started", "· The session stopped: the
  budget is spent", "⚠ The session did not start: <error>".
- What the agents wrote (a message, a comment, a result, a thought) is quoted with its whitespace collapsed and cut
  to about QUOTE_CHARS characters; a tool result and a wake-up by their first line.
- A crew task says so when it is created ("crew task, up to 3 members"), never with its working set (the event's copy
  of it is out of date as soon as a member takes or leaves a direction); every line of a subtask says "part of #N";
  a change of the crew number reads "now a crew task, up to N members" / "now an ordinary task".
- The text adds no absolute path of the hub's computer of its own. A notice that names one (a workspace move, a
  skill's build log) names it by its last component, and a skill's environment (its Python, its packages' folder)
  is left out of the notices that list it (_PATH_NOTES). Every other absolute path in a text of DreamTeam's own (a
  notice, a wake-up, an agent's error, a trigger's errors, a skill environment's failure or state) is named by its
  last component too (without_abs_paths), whatever it quotes; the `env` of `skill.deps` and a job's log are never
  shown. What the agents wrote (a message, a task, a tool call or result) is quoted as written, paths included.
- None only for the events no feed shows: `agent.usage` and `agent.compacted` (the live-only `trace.*` and
  `job.silent` are never sent). It never raises: an event it cannot word gets a generic `<type> · <actor>` line.

The pure helpers here are shared with the terminal UI: tui/render.py imports them back under the same names, and its
RenderContext is a TextContext. Stdlib, models, prompts, uitext and symbols only: hub code imports it, and it never
imports dreamteam.tui (whose package imports prompt_toolkit) or rich."""

from __future__ import annotations

import json
import logging
import os
import re
import string
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from . import prompts
from .models import USER
from .symbols import symbols
from .uitext import t, tn

log = logging.getLogger(__name__)

QUOTE_CHARS = 300         # what the agents wrote (a message, a comment, a result, a thought) is cut to about this
TOOL_CALL_CHARS = 80      # "task_claim(task_id=3)" is cut to about this many characters (the feed's normal lines)
ARG_CHARS = 30            # one string argument inside it
TOOL_LINE_CHARS = TOOL_CALL_CHARS * 2   # a call line here, as in the verbose feed

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[@-_]")
# Text written by agents must not drive the terminal: drop C0/C1 control characters (keep \n).
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# What a line never carries (event_text only, the terminal keeps them): the bidi embeddings, overrides and isolates
# and the invisible marks and spaces a quoted text could reorder or hide a line's words with ("Trojan Source"). The
# joiners U+200C/U+200D stay: emoji sequences and some scripts need them.
_INVISIBLE = re.compile("[\u061c\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")


@dataclass(kw_only=True)
class TextContext:
    """The language, the symbol table and the viewer of a line (`me`: the name that reads "you"), what the helpers
    shared with the terminal UI read. render.RenderContext adds the verbosity and the width. Keyword arguments only:
    the subclass orders its fields differently, so a positional call would bind the wrong one."""
    lang: str = "en"
    unicode: bool = True          # False -> ASCII symbols (symbols.SYMBOLS_ASCII)
    me: str = USER

    @property
    def sym(self) -> dict[str, str]:
        return symbols(self.unicode)

    def t(self, key: str, **kw: Any) -> str:
        return t(self.lang, key, **kw)

    def tn(self, key: str, n: int, **kw: Any) -> str:
        return tn(self.lang, key, n, **kw)

    def short(self, value: Any, limit: int) -> str:
        """One line, whitespace collapsed, cut to `limit` characters with an ellipsis (no cut for 0)."""
        text = " ".join(clean(value).split())
        if limit <= 0 or len(text) <= limit:
            return text
        ell = self.sym["ellipsis"]
        return text[: max(0, limit - len(ell))].rstrip() + ell


# ---- small helpers (shared with tui/render.py) -----------------------------------------------------
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


def _one_line(value: Any) -> str:
    """clean() on one line: every run of whitespace (line and paragraph separators included) one space."""
    return " ".join(clean(value).split())


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


def fmt_elapsed(seconds: Any) -> str:
    """12s, 3m 05s, 1h 02m."""
    s = max(0, int(_float(seconds)))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"


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


def _who(ctx: TextContext, name: Any, short: bool = False) -> str:
    """Display name: "вы" for the user, "система" for no actor, else the (first) name."""
    if name == ctx.me:
        return ctx.t("you")
    if not name:
        return ctx.t("system")
    return first_name(name) if short else clean(name)


def _status_label(ctx: TextContext, prefix: str, value: Any) -> str:
    """Translated label of an enum value (`s_done`, `a_idle`...), the raw value when unknown."""
    raw = clean(value)
    label = ctx.t(prefix + raw) if raw else ""
    return raw if label == prefix + raw else label


def _recipients_text(ctx: TextContext, recipients: list[str], short: bool = False) -> str:
    names = [ctx.t("to_you") if r == ctx.me else (first_name(r) if short else clean(r)) for r in recipients]
    if len(names) > 4:
        return ", ".join(names[:3]) + f" +{len(names) - 3}"
    return ", ".join(names) or "?"


def _echo_target(ctx: TextContext, recipients: list[str]) -> str | None:
    if not recipients:
        return None
    if len(recipients) > 3:
        return "all"
    return ", ".join(clean(r) for r in recipients)


# a task's status -> the key of its symbol
_TASK_SYMBOL = {"todo": "todo", "in_progress": "doing", "waiting": "doing", "review": "review", "done": "done",
                "failed": "failed", "cancelled": "failed"}


def _review_text(ctx: TextContext, reviewer: Any) -> str:
    """The label of a task in review: "in review (Carl)", or "waits for your review" for the viewer."""
    if reviewer and reviewer == ctx.me:
        return ctx.t("task_review_you")
    return ctx.t("task_review") + (f" ({first_name(reviewer)})" if reviewer else "")


def _crew_size(task: dict) -> int:
    """`crew` of a crew task (an integer >= 2), else 0."""
    crew = task.get("crew")
    return crew if isinstance(crew, int) and not isinstance(crew, bool) and crew >= 2 else 0


def tool_call_text(ctx: TextContext, tool: Any, args: Any, limit: int = TOOL_CALL_CHARS) -> str:
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


def _arg(ctx: TextContext, value: Any) -> str:
    if isinstance(value, str):
        return '"' + ctx.short(value, ARG_CHARS) + '"'
    if value is None or isinstance(value, (bool, int, float)):
        return json.dumps(value)
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return ctx.short(text, ARG_CHARS)


def _reason(ctx: TextContext, reason: Any) -> str:
    raw = clean(reason)
    key = {"budget": "reason_budget", "paused by the user": "reason_user", "question": "reason_question"}.get(raw)
    return ctx.t(key) if key else raw


def _job_result(ctx: TextContext, job: dict) -> str:
    """How a background job ended: "завершился с кодом 0", "остановлен вами"..."""
    status = clean(job.get("status"))
    if status == "lost":
        return ctx.t("job_lost")
    if status == "stopped":
        key = "job_stopped_" + clean(job.get("reason"))
        label = ctx.t(key)
        return ctx.t("job_stopped") if label == key else label
    if job.get("exit_code") is not None:
        return ctx.t("job_ended_code", code=clean(job.get("exit_code")))
    return ctx.t("job_ended")


# the user's own surfaces (the pages, the terminal; a role's attach): the feed names only the others (MCP clients)
_PLAIN_VIA = frozenset({"", "act", "home", "cli", "role", "create"})


def _via_text(ctx: TextContext, via: Any) -> str:
    """`via MCP` for a change an MCP client made (MCP, the launcher MCP); "" for the user's own surfaces."""
    via = clean(via)
    if via in _PLAIN_VIA:
        return ""
    label = ctx.t(f"via_{via}")
    return ctx.t("ev_skill_via", via=via if label == f"via_{via}" else label)


def _deps_word(ctx: TextContext, status: Any) -> str:
    """ready / installing… / failed / manual (off, unsupported, unavailable: the agents install by hand)."""
    status = clean(status)
    return ctx.t("skp_" + status) if status in ("ready", "installing", "failed") else ctx.t("skp_manual")


def _deps_reason(value: Any) -> str:
    """The detail of an off / unsupported / unavailable state: {code, detail | need, have} or a plain string."""
    if not isinstance(value, dict):
        return clean(value)
    if value.get("need"):
        return f"{clean(value.get('need'))} ({clean(value.get('have'))})" if value.get("have") else clean(value["need"])
    return clean(value.get("detail") or value.get("code"))


# ---- the lines ------------------------------------------------------------------------------------
def _join(ctx: TextContext, parts: list[str]) -> str:
    """The non-empty parts, joined with " · "."""
    return f" {ctx.sym['dot']} ".join(part for part in parts if part)


def _quote(ctx: TextContext, value: Any) -> str:
    """What an agent wrote, on one line, cut to about QUOTE_CHARS characters."""
    return ctx.short(value, QUOTE_CHARS)


# An absolute path of the hub's computer in a text of DreamTeam's own: a POSIX one ("/home/me/x"), a drive's
# ("C:\x", "C:/x"), a UNC one ("\\server\share", "\\?\C:\x") or the home's ("~/x"). Quoted, it runs to the closing
# quote, spaces included ("Permission denied: '/home/me/my dir/x'"); else to the next space, quote, "<", ">" or "|",
# less the punctuation that ends a sentence. It starts a word ("Log:/x/y" too): "https://host/x" (a "/" after a
# "/" starts none) and "and/or" are no paths.
_ABS_START = r"(?:[A-Za-z]:[\\/]|\\\\(?=[^\s\\])|~[\\/]|/(?=[^\s/]))"
_ABS_PATH = re.compile(r"""(?P<q>['"`])(?P<quoted>""" + _ABS_START + r"""[^'"`\n]*)(?P=q)"""
                       r"""|(?<![^\s(\[{<=,;:'"`])(?P<bare>""" + _ABS_START + r"""[^\s'"`<>|]*)""")
_PATH_END = ".,;:!?)]}"


def _short_path(match: re.Match) -> str:
    quote = match.group("q") or ""
    path = match.group("quoted") if quote else match.group("bare").rstrip(_PATH_END)
    tail = "" if quote else match.group("bare")[len(path):]
    parts = [part for part in re.split(r"[\\/]+", path) if part]
    if path.startswith("/") and len(parts) < 2:   # "/skills reinstall pdf" is a command, "/tmp" no one's folder
        return match.group(0)
    return f"{quote}{parts[-1] if parts else ''}{quote}{tail}"


# where a named folder ends in a text: the end, or what ends a word ("." only at the end of a sentence: "acme.old" is
# another folder); a folder that may be named with a path below it also ends at a separator
_WORD_END = r"""$|[\s'"`<>|,;:!?()\[\]{}]|\.(?=$|\s)"""


def _named_pattern(path: str, below: bool) -> re.Pattern[str] | None:
    """Absolute path `path` as a text may name it: with either separator, single or doubled (str(), as_posix() and
    the repr an OSError shows alike), in any case (matching too much only shortens a text), whole components only and
    not inside a longer path; `below`: also as the start of a path below it. None for a root."""
    parts = re.split(r"[\\/]+", path.rstrip("\\/"))
    if len(parts) < 2 or not parts[-1]:
        return None
    body = r"[\\/]+".join(re.escape(part) for part in parts)
    end = r"(?=[\\/]|" + _WORD_END + ")" if below else "(?=" + _WORD_END + ")"
    return re.compile(r"(?<![\w.~-])" + body + end, re.IGNORECASE)


def without_abs_paths(value: Any, named: Iterable[tuple[Any, str]] = (),
                      exact: Iterable[tuple[Any, str]] = ()) -> str:
    """clean(value) with every absolute path named by its last component (`'/home/me/x/lib'` → `'lib'`): for the texts
    of DreamTeam's own a line quotes (a notice, a wake-up, an error), which may name a folder of the hub's computer.
    The session manager journals the error of a failed start through it too (`session.stopped`, docs/SPEC.md §21.6).
    `named` and `exact`: (absolute path, name) pairs of folders the caller knows, replaced by their names first,
    longest first, wherever the text names them, quoted or not: an unquoted path with a space in it is otherwise cut
    at that space, and its later words would stay ("does not exist: C:\\Users\\Jane Doe\\jobs\\acme site." would keep
    "Doe\\jobs\\acme site"). A `named` folder is also replaced at the start of a path below it, which then stays after
    its name as a relative path (the folders whose content the reader may see: a session's own); an `exact` one only
    where the text names that very folder (a folder above them: a path below it may be anyone's, "C:\\Users\\alice\\x"
    must not become "Users\\alice\\x")."""
    text = clean(value)
    pairs = [(os.fspath(path), name, True) for path, name in named]
    pairs += [(os.fspath(path), name, False) for path, name in exact]
    for path, name, below in sorted(pairs, key=lambda pair: len(pair[0]), reverse=True):
        pattern = _named_pattern(path, below)
        if pattern is not None:
            text = pattern.sub(lambda _match, name=name: name, text)
    return _ABS_PATH.sub(_short_path, text)


def _ref(value: Any) -> str:
    """A task id as text: an int (not a bool) or a non-empty str; "" for anything else."""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    return _one_line(value) if isinstance(value, str) else ""


def _task_head(ctx: TextContext, task: dict, status: Any = None) -> str:
    """`◐ #3 Parse the document`: the symbol of the status, the id and the title."""
    status = status or task.get("status")
    key = _TASK_SYMBOL.get(status if isinstance(status, str) else "", "todo")
    return f"{ctx.sym[key]} #{clean(task.get('id')) or '?'} {clean(task.get('title')) or ctx.sym['dash']}"


def _part_of(ctx: TextContext, parent: Any) -> str:
    ref = _ref(parent)
    return ctx.t("ev_part_of", id=ref) if ref else ""


def _crew_change(ctx: TextContext, change: Any) -> str:
    """A change [old, new] of the crew number: "now a crew task, up to 3 members", "now an ordinary task"; "" for a
    change that says neither (the field is then named like any other)."""
    if not (isinstance(change, (list, tuple)) and len(change) == 2):
        return ""
    before, after = (_crew_size({"crew": value}) for value in change)
    if after:
        return ctx.t("ev_crew_task" if before else "ev_crew_on", n=after)
    return ctx.t("ev_crew_off") if before else ""


def _ev_task_created(ctx: TextContext, ev: dict, d: dict) -> str | None:
    task = _d(d.get("task"))
    head = _task_head(ctx, task, task.get("status") or "todo")
    if task.get("assignee"):
        head += f" {ctx.sym['dash']} {first_name(task.get('assignee'))}"
    crew = _crew_size(task)
    parts = [head, ctx.t("ev_crew_task", n=crew) if crew else "", _part_of(ctx, task.get("parent_id")),
             _quote(ctx, task.get("goal"))]
    if ev.get("actor"):
        parts.append(ctx.t("task_by", name=_who(ctx, ev.get("actor"))))
    return _join(ctx, parts)


def _ev_task_updated(ctx: TextContext, ev: dict, d: dict) -> str | None:
    task = _d(d.get("task"))
    changes = _d(d.get("changes"))
    s = ctx.sym

    def new(field: str) -> Any:
        change = changes.get(field)
        if isinstance(change, (list, tuple)) and len(change) == 2:
            return change[1]
        return task.get(field)

    status = task.get("status") or new("status")
    assignee = task.get("assignee") if "assignee" in task else new("assignee")
    crew = _crew_change(ctx, changes.get("crew")) if "crew" in changes else ""
    if "status" not in changes and not ("assignee" in changes and assignee):
        # a change of other fields: the crew number and a new parent in words, the rest by name; a subtask's line
        # says whose part it is whatever changed
        moved = _part_of(ctx, new("parent_id")) if "parent_id" in changes else ""
        parent = moved or ("" if "parent_id" in changes else _part_of(ctx, task.get("parent_id")))
        fields = [f for f in changes if not (f == "crew" and crew) and not (f == "parent_id" and moved)]
        listed = ", ".join(_status_label(ctx, "f_", f) for f in fields)
        changed = ctx.t("task_changed") + (f": {listed}" if listed else "")
        head = f"{s['edit']} #{clean(task.get('id')) or '?'} {clean(task.get('title')) or s['dash']}"
        return _join(ctx, [head, crew, parent, changed if fields or not (crew or moved) else ""])
    head = _task_head(ctx, task, status)
    if assignee and (status in ("in_progress", "waiting", "done", "failed")
                     or (status == "todo" and "status" not in changes)):  # assigned, not started
        head += f" {s['dash']} {first_name(assignee)}"
    tails: list[str] = []
    result: Any = None
    if status == "waiting":
        tails.append(ctx.t("task_waiting"))
    elif status == "review":
        head += f" {s['dash']} {_review_text(ctx, task.get('reviewer') if 'reviewer' in task else new('reviewer'))}"
    elif status == "done":
        result = task.get("result")
    elif status == "failed":
        tails.append(ctx.t("task_failed"))
        result = task.get("result")
    elif status == "cancelled":
        tails.append(ctx.t("task_cancelled"))
    elif status == "todo" and "status" in changes:
        tails.append(ctx.t("task_back_todo"))
    line = _join(ctx, [head, *tails, crew, _part_of(ctx, task.get("parent_id"))])
    quoted = _quote(ctx, result)
    return f"{line} {s['cont']} {quoted}" if quoted else line


def _ev_task_comment(ctx: TextContext, ev: dict, d: dict) -> str | None:
    post = _d(d.get("post"))
    author = post.get("author") or ev.get("actor")
    task_id = d.get("task_id") if d.get("task_id") is not None else post.get("task_id")
    who = _who(ctx, author, short=True)
    return f"{ctx.sym['chat']} #{clean(task_id) or '?'} {who}: {_quote(ctx, post.get('text'))}"


def _ev_message(ctx: TextContext, ev: dict, d: dict) -> str | None:
    msg = _d(d.get("message"))
    sender = msg.get("sender") or ev.get("actor")
    recipients = _names(msg.get("recipients"))
    text = _quote(ctx, msg.get("text"))
    s = ctx.sym
    if sender == ctx.me:  # the user's own message, as the feed echoes it: "> text → Anna Smith"
        target = _echo_target(ctx, recipients)
        line = f"{s['prompt']} {text}"
        if target:
            line += f" {s['arrow']} {ctx.t('everyone') if target == 'all' else target}"
        return line
    if ctx.me in recipients:
        head = f"{_who(ctx, sender if sender else None)} {s['arrow']} {ctx.t('to_you')}"
        others = [r for r in recipients if r != ctx.me]
        if others:
            head += ", " + _recipients_text(ctx, others)
        return f"{head}: {text}" if text else head
    return (f"{s['mail']} {_who(ctx, sender, short=True)} {s['arrow']} {_recipients_text(ctx, recipients, True)}: "
            f"{text}")


# Notices whose text names a path on the hub's computer (prompts.TEXTS), and how a line shows each such placeholder:
# "name" by its last component, "drop" not at all (a skill's environment: its Python, its packages' folder)
_PATH_NOTES: dict[str, dict[str, str]] = {
    "n_workspace_moved": {"path": "name", "old": "name"},
    "n_workspace_changed": {"path": "name", "old": "name"},
    "n_skill_deps_ready": {"deps": "drop"},
    "n_skill_deps_failed": {"deps": "drop"},
    "u_skill_deps_failed": {"log": "name"},
}
_NOTE_TEMPLATES: dict[str, list[tuple[str, re.Pattern]]] = {}


def _template_pattern(template: str) -> re.Pattern:
    """A regular expression that matches the texts `template` formats to, a group per placeholder (its first
    occurrence)."""
    out: list[str] = []
    seen: set[str] = set()
    for literal, field, _spec, _conversion in string.Formatter().parse(template):
        out.append(re.escape(literal))
        if field is not None:
            out.append(f"(?P<{field}>.*?)" if field.isidentifier() and field not in seen else ".*?")
            seen.add(field)
    return re.compile("".join(out), re.DOTALL)


def _last_component(path: str) -> str:
    parts = [part for part in re.split(r"[\\/]+", path.strip()) if part]
    return parts[-1] if parts else ""


def _without_paths(kind: str, text: Any) -> str | None:
    """The text of a notice of _PATH_NOTES with its paths shortened: its template (of the language that matches)
    formatted again with each path as its last component or left out; None when no template matches the text
    (another version's wording)."""
    templates = _NOTE_TEMPLATES.get(kind)
    if templates is None:
        templates = [(texts[kind], _template_pattern(texts[kind])) for texts in prompts.TEXTS.values() if kind in texts]
        _NOTE_TEMPLATES[kind] = templates
    for template, pattern in templates:
        match = pattern.fullmatch(text) if isinstance(text, str) else None
        if match is None:
            continue
        values = match.groupdict()
        for field, how in _PATH_NOTES[kind].items():
            if field in values:
                values[field] = _last_component(values[field]) if how == "name" else ""
        try:
            return template.format(**values)
        except (KeyError, IndexError, ValueError):
            return None
    return None


def _ev_notification(ctx: TextContext, ev: dict, d: dict) -> str | None:
    recipient, kind, text = d.get("recipient"), clean(d.get("kind")), d.get("text")
    s = ctx.sym
    if kind in _PATH_NOTES:  # the paths of the hub's computer shortened (spaces in them too), never shown whole
        text = _without_paths(kind, text) or kind
    # any other a notice names (a job's log in the data folder, a skill outside the workspace, an installer's error)
    text = without_abs_paths(text)
    if recipient != ctx.me:
        return f"{s['bell']} {s['arrow']} {first_name(recipient) or '?'}: {ctx.short(_first_line(text), 200)}"
    if kind == "u_agent_error":  # the feeds' error line
        return f"{s['warn']} {ctx.short(text, 0)}"
    return f"{s['bell']} {ctx.short(text, 0) or kind}"


def _ev_goal(ctx: TextContext, ev: dict, d: dict) -> str | None:
    goal = _d(d.get("goal"))
    if not goal:
        return ctx.t("no_goal")
    s = ctx.sym
    title = ctx.short(goal.get("title"), 0) or s["dash"]
    if ev.get("type") == "goal.completed" or goal.get("status") == "done":
        summary = ctx.short(_first_line(goal.get("summary")), QUOTE_CHARS)
        line = f"{s['check']} {ctx.t('goal_done')}: {title}"
        return f"{line} {s['dash']} {summary}" if summary else line
    pending = f" ({ctx.t('goal_pending')})" if goal.get("status") == "pending" else ""
    return f"{ctx.t('goal')}{pending}: {title}"


def _ev_goal_reopened(ctx: TextContext, ev: dict, d: dict) -> str | None:
    line = f"{_who(ctx, ev.get('actor'))} {ctx.sym['dot']} {ctx.t('goal_reopened')}"
    comment = _quote(ctx, d.get("comment"))
    return f"{line}: {comment}" if comment else line


def _ev_hired(ctx: TextContext, ev: dict, d: dict) -> str | None:
    name = d.get("name") or ev.get("actor")
    role = clean(d.get("role"))
    if d.get("kind") == "external":
        role = f"{role}, {ctx.t('external')}" if role else ctx.t("external")
    line = f"{ctx.sym['plus']} {clean(name) or '?'}" + (f" ({role})" if role else "") + " " + ctx.t("hired")
    parts = [line, f"{ctx.t('model')}: {clean(d.get('model'))}" if d.get("model") else ""]
    by = d.get("hired_by") or ev.get("actor")
    if by:
        parts.append(ctx.t("hired_by", name=_who(ctx, by)))
    return _join(ctx, parts)


def _ev_retired(ctx: TextContext, ev: dict, d: dict) -> str | None:
    line = f"{ctx.sym['minus']} {clean(d.get('name') or ev.get('actor')) or '?'} {ctx.t('retired')}"
    reason = _quote(ctx, d.get("reason"))
    return f"{line}: {reason}" if reason else line


def _ev_status(ctx: TextContext, ev: dict, d: dict) -> str | None:
    name = d.get("name") or ev.get("actor")
    return f"{ctx.sym['dot']} {clean(name) or '?'}: {_status_label(ctx, 'a_', d.get('status')) or '?'}"


def _ev_activity(ctx: TextContext, ev: dict, d: dict) -> str | None:
    kind = d.get("kind")
    agent = d.get("agent") or ev.get("actor")
    text = d.get("text")
    s = ctx.sym
    if kind == "error":   # the hub's or the provider's error text: a path in it shortened
        return f"{s['warn']} {clean(agent) or ctx.t('system')}: {_quote(ctx, without_abs_paths(text))}"
    if kind == "tool_call":
        return f"{_who(ctx, agent)} {s['dot']} {tool_call_text(ctx, d.get('tool'), text, TOOL_LINE_CHARS)}"
    if kind == "tool_result":
        first = ctx.short(_first_line(text), QUOTE_CHARS) or ctx.t("empty")
        mark = f"{s['failed']} " if d.get("is_error") else ""
        return f"{_who(ctx, agent)} {s['dot']} {clean(d.get('tool')) or '?'} {s['cont']} {mark}{first}"
    if kind == "thinking":
        return f"{s['spark']} {_who(ctx, agent)} {ctx.t('thinking')} {_quote(ctx, text)}"
    if kind == "text":
        return f"{_who(ctx, agent)} {s['dot']} {ctx.t('k_text')}: {_quote(ctx, text)}"
    if kind == "wake":   # the first notice of the wake-up text: a workspace move, a skill's Python... shortened
        summary = without_abs_paths(_wake_summary(text))
        return f"{s['alarm']} {clean(agent) or '?'} {ctx.t('wakes')}: {ctx.short(summary, QUOTE_CHARS)}"
    if kind == "nudge":
        return (f"{s['nudge']} {clean(agent) or '?'}: {ctx.t('nudge')} {s['dash']} "
                f"{ctx.short(without_abs_paths(_first_line(text)), QUOTE_CHARS)}")
    if kind == "compacted":
        return f"{s['spark']} {_who(ctx, agent)} {s['dot']} {ctx.t('compacted')} {s['cont']} {_quote(ctx, text)}"
    return _generic(ctx, ev)


def _ev_file(ctx: TextContext, ev: dict, d: dict) -> str | None:
    typ = ev.get("type")
    if typ == "file.changed":
        label = _status_label(ctx, "op_", d.get("op"))
    elif typ == "file.locked":
        label = ctx.t("file_locked")
    else:
        label = ctx.t("file_lock_expired") if d.get("expired") else ctx.t("file_unlocked")
    who = d.get("owner") if typ != "file.changed" else ev.get("actor")
    line = f"{ctx.sym['edit']} {clean(d.get('path')) or '?'} {ctx.sym['dash']} {label}"
    return _join(ctx, [line, _who(ctx, who, short=True) if who else ""])


def _ev_trigger(ctx: TextContext, ev: dict, d: dict) -> str | None:
    trigger = _d(d.get("trigger"))
    tid = clean(trigger.get("id") if trigger.get("id") is not None else d.get("trigger_id")) or "?"
    errors = [without_abs_paths(e) for e in _list(d.get("errors")) if e]
    if errors:
        return f"{ctx.sym['warn']} {ctx.t('trigger_errors', id=tid, errors='; '.join(errors))}"
    key = {"trigger.created": "trigger_created", "trigger.fired": "trigger_fired"}.get(ev.get("type"),
                                                                                      "trigger_deleted")
    line = f"{ctx.sym['trigger']} {ctx.t(key, id=tid, condition=ctx.short(trigger.get('condition'), 160))}"
    if key == "trigger_fired" and _float(trigger.get("held")) > 0 and trigger.get("wake_at"):
        # the owner's notice waits for the wake limit ([agents] trigger_wake_seconds): for how long, never until
        # when (no clock: the hub's time zone is not the reader's; data.trigger.wake_at has the time)
        fired, wake_at = _float(ev.get("ts")), _float(trigger.get("wake_at"))
        held = ctx.t("ev_trigger_held_for", t=fmt_elapsed(wake_at - fired)) if 0 < fired < wake_at else \
            ctx.t("ev_trigger_held")
        line += f" ({held})"
    return _join(ctx, [line, _who(ctx, ev.get("actor"), short=True) if ev.get("actor") else ""])


def _ev_mcp(ctx: TextContext, ev: dict, d: dict) -> str | None:
    s = ctx.sym
    if ev.get("type") == "mcp.created":
        server = _d(d.get("server"))
        line = (f"{_who(ctx, server.get('owner') or ev.get('actor'))} {s['dot']} "
                f"{ctx.t('mcp_created', name=clean(server.get('name')) or '?')} {s['dash']} "
                f"{_status_label(ctx, 'mcp_', server.get('status'))}")
        return _join(ctx, [line, ctx.short(server.get("description"), 200)])
    return f"{s['dot']} MCP {clean(d.get('name')) or '?'}: {_status_label(ctx, 'mcp_', d.get('status'))}"


def _ev_paused(ctx: TextContext, ev: dict, d: dict) -> str | None:
    if d.get("reason") == "question" and d.get("asker"):  # a member's question (send_message pause_swarm)
        text = ctx.t("paused_question", name=first_name(d.get("asker")))
    else:
        reason = _reason(ctx, d.get("reason"))
        text = ctx.t("paused") + (f" ({reason})" if reason else "")
    return f"{ctx.sym['pause']} {text}"


def _ev_resumed(ctx: TextContext, ev: dict, d: dict) -> str | None:
    q = _d(d.get("question"))   # the question this resume ended: answered, or gone on without an answer
    if not q:
        text = ctx.t("resumed")
    elif q.get("outcome") == "answered":
        text = ctx.t("resumed_answered", id=clean(q.get("message_id")) or "?")
    elif q.get("why") == "clarify_off":
        text = ctx.t("resumed_clarify_off", id=clean(q.get("message_id")) or "?")
    elif q.get("why") == "left":
        text = ctx.t("resumed_left", name=first_name(q.get("asker")) or "?", id=clean(q.get("message_id")) or "?")
    else:
        text = ctx.t("resumed_unanswered", id=clean(q.get("message_id")) or "?")
    return f"{ctx.sym['play']} {text}"


def _ev_wrap_up(ctx: TextContext, ev: dict, d: dict) -> str | None:
    who = _who(ctx, ev.get("actor") or USER)
    if d.get("on"):
        return f"{ctx.sym['warn']} {ctx.t('wrap_up_on', who=who)}"
    return f"{ctx.sym['dot']} {ctx.t('wrap_up_off', who=who)}"


def _ev_limits(ctx: TextContext, ev: dict, d: dict) -> str | None:
    cost = _float(d.get("max_cost_usd"))
    budget = fmt_cost(cost) if cost > 0 else ctx.t("unlimited")
    return f"{ctx.sym['dot']} {ctx.t('limits', budget=budget, agents=clean(d.get('max_agents')) or '?')}"


def _ev_job(ctx: TextContext, ev: dict, d: dict) -> str | None:
    """`⚙ Анна Смирнова · процесс j3 завершился с кодом 0 (4m 05s): npm test` (never the log's path)."""
    job = _d(d.get("job"))
    who = _who(ctx, job.get("owner") or ev.get("actor"))
    label = clean(job.get("label")) or (f"j{clean(job.get('id'))}" if job.get("id") is not None else "?")
    command = ctx.short(job.get("command"), 160) or "?"
    if ev.get("type") == "job.started":
        text = ctx.t("ev_job_started", who=who, id=label, command=command)
    else:
        seconds = job.get("duration")
        if seconds is None:
            seconds = _float(job.get("ended_at")) - _float(job.get("started_at"))
        text = ctx.t("ev_job_ended", who=who, id=label, result=_job_result(ctx, job), t=fmt_elapsed(seconds),
                     command=command)
    return f"{ctx.sym['job']} {text}"


def _ev_skill(ctx: TextContext, ev: dict, d: dict) -> str | None:
    """`вы · подключил(а) скилл pdf` (attached, updated, detached, proposed, the library decision)."""
    typ, name = ev.get("type"), clean(d.get("name")) or "?"
    actor = ev.get("actor") or USER
    via = _via_text(ctx, d.get("via"))
    if typ == "skill.attached":
        text = ctx.t("ev_skill_updated" if d.get("replaced") else "ev_skill_attached", name=name)
        tails = [clean(d.get("from")), via]
    elif typ == "skill.detached":
        text, tails = ctx.t("ev_skill_detached", name=name), [via]
    elif typ == "skill.proposed":
        owner = _who(ctx, d.get("owner") or ev.get("actor"))
        return f"{owner} {ctx.sym['dot']} {ctx.t('ev_skill_proposed', name=name)}"
    else:
        # whose skill and through which surface: a save no UI click made shows as such (docs/SPEC.md §19)
        key = "ev_skill_saved" if d.get("status") == "saved" else "ev_skill_not_saved"
        owner = clean(d.get("owner"))
        text = ctx.t(key + "_of", name=name, owner=owner) if owner and owner != actor else ctx.t(key, name=name)
        tails = [via, clean(d.get("comment"))]
    return _join(ctx, [f"{_who(ctx, actor)} {ctx.sym['dot']} {text}", *(ctx.short(tail, 160) for tail in tails)])


def _ev_skill_deps(ctx: TextContext, ev: dict, d: dict) -> str | None:
    """A skill's packages: ready, failed, a build starting and the manual states (never the environment's path: the
    `env` is not shown, and a path in the installer's error or a state's detail, a configured Python, is shortened)."""
    status = clean(d.get("status"))
    name, files = clean(d.get("name")) or "?", ", ".join(_names(d.get("files")))
    if status == "failed":
        error = ctx.short(without_abs_paths(d.get("error")), 200) or "?"
        return f"{ctx.sym['warn']} {ctx.t('ev_skill_deps_failed', name=name, error=error)}"
    if status == "ready":
        took = "" if d.get("reused") or d.get("seconds") is None else f", {fmt_elapsed(d.get('seconds'))}"
        return f"{ctx.sym['check']} {ctx.t('ev_skill_deps_ready', name=name, files=files, t=took)}"
    if status == "installing":
        return f"{ctx.sym['dot']} {ctx.t('ev_skill_deps_installing', name=name, files=files)}"
    return (f"{ctx.sym['dot']} {name}: {clean(d.get('ecosystem'))} {_deps_word(ctx, status)} "
            f"({without_abs_paths(_deps_reason(d.get('reason')))})")


def _session_run(ctx: TextContext, ev: dict, d: dict) -> tuple[str, str]:
    """(symbol key, text) of a run's start or end, journaled by the session manager: `session.started` {} and
    `session.stopped` {reason, error?} ("The session stopped: the budget is spent", "The session did not start:
    <error>"; an unknown reason is named as it is). The error of a failed start is DreamTeam's own text: an absolute
    path in it is named by its last component."""
    if ev.get("type") == "session.started":
        return "play", ctx.t("ev_session_started")
    reason = _one_line(d.get("reason"))
    if reason == "failed":
        error = ctx.short(without_abs_paths(d.get("error")), 200)
        return "warn", ctx.t("ev_session_stopped_failed") + (f": {error}" if error else "")
    key = "ev_session_stopped_" + reason
    text = ctx.t(key)
    if text == key:   # no reason, or one this version has no words for
        text = ctx.t("ev_session_stopped") + (f" ({reason})" if reason else "")
    return "dot", text


def _ev_session_run(ctx: TextContext, ev: dict, d: dict) -> str | None:
    symbol, text = _session_run(ctx, ev, d)
    return f"{ctx.sym[symbol]} {text}"


def _ev_session(ctx: TextContext, ev: dict, d: dict) -> str | None:
    session = d.get("session")
    sid = _d(session).get("id") if isinstance(session, dict) else session
    return f"{ctx.sym['dot']} {ctx.t('session_changed', id=clean(sid) or '?', change=clean(d.get('change')))}"


def _ev_connection(ctx: TextContext, ev: dict, d: dict) -> str | None:
    """(the terminal client's own pseudo event: never in a journal)"""
    return f"{ctx.sym['warn']} {ctx.t('conn_lost')}" if d.get("state") == "lost" else None


def _none(ctx: TextContext, ev: dict, d: dict) -> None:
    return None


_HANDLERS: dict[str, Callable[[TextContext, dict, dict], str | None]] = {
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
    "agent.usage": _none,       # the cost and the tokens: no feed shows it as a line
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
    "swarm.wrap_up": _ev_wrap_up,
    "swarm.limits": _ev_limits,
    "job.started": _ev_job,
    "job.ended": _ev_job,
    "job.silent": _none,        # (live only, never sent)
    "skill.attached": _ev_skill,
    "skill.detached": _ev_skill,
    "skill.proposed": _ev_skill,
    "skill.library": _ev_skill,
    "skill.deps": _ev_skill_deps,
    "session.started": _ev_session_run,
    "session.stopped": _ev_session_run,
    "sessions.changed": _ev_session,
    "_connection": _ev_connection,
}


def _generic(ctx: TextContext, ev: dict) -> str:
    """`<type> · <actor>`: the line of an event no handler words."""
    actor = ev.get("actor")
    return _join(ctx, [_one_line(ev.get("type")) or "?", _who(ctx, actor) if actor else ""])


def _event(ctx: TextContext, ev: dict) -> str | None:
    typ = ev.get("type") if isinstance(ev.get("type"), str) else ""
    if typ.startswith("trace."):  # (live only, never sent)
        return None
    handler = _HANDLERS.get(typ)
    if handler is None:
        return _generic(ctx, ev)
    return handler(ctx, ev, _d(ev.get("data")))


def event_text(event: Any, lang: str = "en", *, me: str = USER) -> str | None:
    """The line of a journaled event (`{id, ts, type, actor, data}`) in `lang` ("ru" or "en"; another falls back to
    English) as `me` sees it; None for agent.usage, agent.compacted, the live-only events and a value that is no
    event (not a dict). Never raises."""
    if not isinstance(event, dict):
        return None
    ctx = TextContext(lang=lang if isinstance(lang, str) else "en", me=me)
    try:
        text = _event(ctx, event)
        if text is None:
            return None
        line = _line_of(text)
        if line:
            return line
    except Exception:  # a malformed event never breaks a reader
        log.debug("cannot word event %r", event, exc_info=True)
    try:   # the generic line, the user named as every line names them ("you")
        return _line_of(_generic(ctx, event)) or "?"
    except Exception:
        pass
    try:
        actor = _line_of(event.get("actor"))
        typ = _line_of(event.get("type")) or "?"
    except Exception:
        return "?"
    return f"{typ} · {actor}" if actor else typ


def _line_of(text: Any) -> str:
    """The one line event_text answers: clean, without the invisible format characters, whitespace collapsed."""
    return " ".join(_INVISIBLE.sub("", clean(text)).split())


__all__ = ["QUOTE_CHARS", "TOOL_CALL_CHARS", "TextContext", "clean", "event_text", "first_name", "fmt_cost",
           "fmt_elapsed", "tool_call_text"]
