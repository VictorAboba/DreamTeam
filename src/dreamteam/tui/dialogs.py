"""Dialogs that replace the input box (docs/TUI.md §4.1, §4.5): the select-list widget, a confirm dialog,
the MCP approval dialog, the session picker and a busy notice.

A dialog renders itself as prompt_toolkit formatted text inside a rounded box (yellow border) and is driven
by the app's key bindings: `move()`, `pick_number()`, `choose()`, `cancel()` and `remove()` (the Delete key).
`wait()` returns the chosen option's value, or None when the dialog was cancelled (Esc)."""

from __future__ import annotations

import asyncio
import textwrap
import time
from dataclasses import dataclass
from typing import Any, Sequence

from .i18n import t
from .render import RenderContext, _size, cells, cut_cells, skill_md_body, skill_states_preview

Fragments = list[tuple[str, str]]

BORDER = "class:box.border.dialog"
TITLE = "class:dialog.title"
TEXT = "class:dialog.text"
DIM = "class:dialog.dim"
OPTION = "class:dialog.option"
SELECTED = "class:dialog.option.selected"


# ---- formatted text helpers -----------------------------------------------------------------------
_ASCII = str.maketrans({"—": "-", "–": "-", "…": "...", "«": '"', "»": '"', "→": "->", "·": "-", "↑": "^",
                        "↓": "v", "▶": ">", "❯": ">", "✓": "v"})


def ascii_text(text: str) -> str:
    """UI text for a terminal that cannot show the typographic symbols (theme.SYMBOLS_ASCII mode)."""
    return text.translate(_ASCII)


def frag_len(frags: Fragments) -> int:
    """Width in cells as prompt_toolkit lays it out (render.cells)."""
    return sum(cells(text) for _, text in frags)


def fit(frags: Fragments, width: int, ellipsis: str = "…") -> Fragments:
    """Cut a one-line formatted text to `width` cells (an ellipsis marks the cut) and pad it to `width`."""
    out: Fragments = []
    used = 0
    for style, text in frags:
        text = text.replace("\n", " ")
        size = cells(text)
        if used + size <= width:
            out.append((style, text))
            used += size
            continue
        room = width - used
        if room > len(ellipsis):
            text = cut_cells(text, room - len(ellipsis)) + ellipsis
        else:
            text = cut_cells(text, max(0, room))
        if text:
            out.append((style, text))
        used += cells(text)
        break
    if used < width:
        out.append(("", " " * (width - used)))
    return out


def wrap(text: str, width: int, max_lines: int = 6, ellipsis: str = "…") -> list[str]:
    """Plain text wrapped to `width` columns, at most `max_lines` lines (the last one marked when cut)."""
    lines: list[str] = []
    for paragraph in str(text or "").splitlines() or [""]:
        lines += textwrap.wrap(paragraph, max(8, width)) or [""]
    while len(lines) > 1 and not lines[-1].strip():
        lines.pop()
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][: max(0, width - len(ellipsis))] + ellipsis
    return lines


def box(lines: Sequence[Fragments], width: int, *, title: str = "", unicode: bool = True,
        border: str = BORDER, title_style: str = TITLE) -> Fragments:
    """`lines` inside a rounded box `width` cells wide (the title in the top border)."""
    tl, tr, bl, br, h, v = ("╭", "╮", "╰", "╯", "─", "│") if unicode else ("+", "+", "+", "+", "-", "|")
    ell = "…" if unicode else "..."
    width = max(12, width)
    inner = width - 4
    out: Fragments = [(border, tl + h)]
    head = [(title_style, f" {title} ")] if title else []
    head = fit(head, min(frag_len(head), width - 4), ell) if head else []
    out += head
    out.append((border, h * max(0, width - 3 - frag_len(head)) + tr + "\n"))
    for line in lines:
        out.append((border, v + " "))
        out += fit(line, inner, ell)
        out.append((border, " " + v + "\n"))
    out.append((border, bl + h * (width - 2) + br))
    return out


# ---- widgets --------------------------------------------------------------------------------------
@dataclass
class Option:
    label: str
    value: Any
    hint: str = ""          # dim text after the label


class SelectList:
    """A list of options with a selection: ↑↓ move, digits pick, `rows` visible at a time."""

    def __init__(self, options: Sequence[Option], index: int = 0, rows: int = 10, numbered: bool = True):
        self.options = list(options)
        self.index = max(0, min(index, len(self.options) - 1)) if self.options else 0
        self.rows = rows
        self.numbered = numbered

    @property
    def selected(self) -> Option | None:
        return self.options[self.index] if self.options else None

    def move(self, delta: int) -> None:
        if self.options:
            self.index = (self.index + delta) % len(self.options)

    def pick_number(self, number: int) -> bool:
        """Select option `number` (1-based); False if there is none."""
        if 1 <= number <= len(self.options):
            self.index = number - 1
            return True
        return False

    def visible(self) -> range:
        count = len(self.options)
        if count <= self.rows:
            return range(count)
        first = min(max(0, self.index - self.rows + 1), count - self.rows)
        return range(first, first + self.rows)

    def lines(self, unicode: bool = True) -> list[Fragments]:
        pointer = "❯" if unicode else ">"
        out: list[Fragments] = []
        for i in self.visible():
            option = self.options[i]
            chosen = i == self.index
            style = SELECTED if chosen else OPTION
            number = f"{i + 1}. " if self.numbered else ""
            line: Fragments = [(style, f"{pointer if chosen else ' '} {number}{option.label}")]
            if option.hint:
                line.append((DIM, f"  {option.hint}"))
            out.append(line)
        return out


# ---- dialogs --------------------------------------------------------------------------------------
class Dialog:
    """Base dialog: a title, body lines, a select list and a footer hint."""

    kind = "dialog"
    settle = 0.0    # seconds after open() during which Enter and digits do not choose

    def __init__(self, lang: str, title: str, options: Sequence[Option], *, index: int = 0, footer: str = "",
                 numbered: bool = True, rows: int = 10):
        self.lang = lang
        self.title = title
        self.select = SelectList(options, index, rows=rows, numbered=numbered)
        self.footer = footer
        self._future: asyncio.Future | None = None
        self._result: Any = None
        self._done = False
        self.opened_at = time.monotonic()

    def open(self) -> None:
        """The dialog is shown from now on (the app calls it)."""
        self.opened_at = time.monotonic()

    def ready(self) -> bool:
        """Whether Enter / a digit may choose now: a dialog that pops up by itself (MCP approval) ignores
        them for `settle` seconds, so keys typed for the input box do not answer it."""
        return time.monotonic() - self.opened_at >= self.settle

    # ---- result
    def _resolve(self, value: Any) -> None:
        if self._done:
            return
        self._done = True
        self._result = value
        if self._future is not None and not self._future.done():
            self._future.set_result(value)

    @property
    def done(self) -> bool:
        return self._done

    async def wait(self) -> Any:
        if self._done:
            return self._result
        self._future = asyncio.get_running_loop().create_future()
        return await self._future

    def choose(self) -> None:
        option = self.select.selected
        self._resolve(option.value if option is not None else None)

    def cancel(self) -> None:
        self._resolve(None)

    def remove(self) -> None:
        """The Delete key: nothing, unless the dialog lists things that can be deleted."""

    def move(self, delta: int) -> None:
        self.select.move(delta)

    def pick_number(self, number: int) -> bool:
        """Digit keys: select and choose option `number`."""
        if self.select.numbered and self.select.pick_number(number):
            self.choose()
            return True
        return False

    # ---- rendering
    def body(self, width: int, unicode: bool) -> list[Fragments]:
        return []

    def fragments(self, width: int, unicode: bool = True) -> Fragments:
        lines = self.body(width - 4, unicode)
        if lines:
            lines.append([])
        lines += self.select.lines(unicode)
        if self.footer:
            lines += [[], [(DIM, self.footer)]]
        title = self.title
        if not unicode:
            lines = [[(style, ascii_text(text)) for style, text in line] for line in lines]
            title = ascii_text(title)
        return box(lines, width, title=title, unicode=unicode)


class ConfirmDialog(Dialog):
    kind = "confirm"
    max_lines = 10   # of the text (a /cd question names folders: long paths on a narrow terminal)

    def __init__(self, lang: str, title: str, text: str = "", *, default: bool = True):
        options = [Option(t(lang, "confirm_yes"), True), Option(t(lang, "confirm_no"), False)]
        super().__init__(lang, title, options, index=0 if default else 1)
        self.text = text

    def body(self, width: int, unicode: bool) -> list[Fragments]:
        return [[(TEXT, line)] for line in wrap(self.text, width, self.max_lines)] if self.text else []


class ChoiceDialog(ConfirmDialog):
    """A question with its own answers (`options`: (label, value) pairs; Esc: None), e.g. deleting a session whose
    skill proposals wait: save them, discard them, or cancel."""

    kind = "choice"

    def __init__(self, lang: str, title: str, text: str, options: Sequence[tuple[str, Any]], *, index: int = 0):
        Dialog.__init__(self, lang, title, [Option(label, value) for label, value in options], index=index)
        self.text = text


class BusyDialog(Dialog):
    """A line of text while something runs (a deletion): it takes the keys, so nothing typed meanwhile is
    submitted, and it cannot be answered; the app closes it."""

    kind = "busy"

    def __init__(self, lang: str, title: str, text: str = ""):
        super().__init__(lang, title, [], numbered=False)
        self.text = text

    def choose(self) -> None:
        pass

    def cancel(self) -> None:
        pass

    def body(self, width: int, unicode: bool) -> list[Fragments]:
        return [[(DIM, line)] for line in wrap(self.text, width)] if self.text else []


APPROVE, REJECT = "approve", "reject"
DELETE = "delete"


class MCPApprovalDialog(Dialog):
    """«1. Да, разрешить» / «2. Нет, отклонить (с комментарием)» for a pending MCP server; Esc = later."""

    kind = "mcp"
    settle = 0.6

    def __init__(self, lang: str, server: dict):
        options = [Option(t(lang, "mcp_yes"), APPROVE), Option(t(lang, "mcp_no"), REJECT)]
        super().__init__(lang, t(lang, "mcp_title"), options, footer=t(lang, "mcp_later"))
        self.server = dict(server or {})
        self.gone = False   # decided elsewhere meanwhile (the web UI, /approve): closed without a choice

    @property
    def name(self) -> str:
        return str(self.server.get("name") or "")

    def body(self, width: int, unicode: bool) -> list[Fragments]:
        s = self.server
        lang = self.lang
        lines: list[Fragments] = [[("class:dialog.text bold", self.name or "?")]]

        def field(key: str, value: Any, max_lines: int = 1) -> None:
            text = " ".join(str(value).split()) if max_lines == 1 else str(value)
            label = t(lang, key) + ": "
            wrapped = wrap(text, max(10, width - len(label)), max_lines)
            for i, part in enumerate(wrapped):
                lines.append([(DIM, label if i == 0 else " " * len(label)), (TEXT, part)])

        if s.get("owner"):
            field("mcp_owner", s["owner"])
        scope = str(s.get("scope") or "")
        if scope:
            label = t(lang, "mcp_scope_" + scope)
            label = scope if label == "mcp_scope_" + scope else label
            field("mcp_scope", label + (f" #{s['task_id']}" if s.get("task_id") else ""))
        if s.get("description"):
            field("mcp_description", s["description"], max_lines=4)
        # a forged server has no command of its own (`command: []`): the forge runs its server.py in
        # workspace/mcp/<name>, with the requirements installed by uv
        code = f"mcp/{self.name}/server.py"
        command = s.get("command") or ["python", code]
        if isinstance(command, (list, tuple)):
            command = " ".join(map(str, command))
        requirements = [str(x) for x in s.get("requirements") or [] if x]
        if requirements:
            command += " + " + ", ".join(requirements)
        field("mcp_command", command, 2)
        tools = [str(x) for x in s.get("tools") or [] if x]
        if tools:
            field("mcp_tools", ", ".join(tools), max_lines=2)
        if self.name:
            lines.append([(DIM, t(lang, "mcp_code_hint", path=code))])
        return lines


class SkillApprovalDialog(Dialog):
    """«1. Сохранить в библиотеку» / «2. Не сохранять (с причиной)» for a member's skill proposal; Esc = later. The
    swarm uses the skill already: the decision is only whether the library keeps it for future runs."""

    kind = "skill"
    settle = 0.6
    SKILL_MD_LINES = 8   # the start of SKILL.md (after its front matter) the dialog shows

    def __init__(self, lang: str, skill: dict):
        options = [Option(t(lang, "skill_yes"), APPROVE), Option(t(lang, "skill_no"), REJECT)]
        super().__init__(lang, t(lang, "skill_title"), options, footer=t(lang, "skill_later"))
        self.skill = dict(skill or {})
        self.gone = False   # decided elsewhere meanwhile (the web UI, /skills approve): closed without a choice

    @property
    def name(self) -> str:
        return str(self.skill.get("name") or "")

    def body(self, width: int, unicode: bool) -> list[Fragments]:
        s = self.skill
        lang = self.lang
        lines: list[Fragments] = [[("class:dialog.text bold", self.name or "?")]]

        def field(key: str, value: Any, max_lines: int = 1) -> None:
            text = " ".join(str(value).split())
            label = t(lang, key) + ": "
            for i, part in enumerate(wrap(text, max(10, width - len(label)), max_lines)):
                lines.append([(DIM, label if i == 0 else " " * len(label)), (TEXT, part)])

        if s.get("owner"):
            field("skill_owner", s["owner"])
        listed = [f for f in s.get("files_list") or [] if isinstance(f, dict) and f.get("path")]
        if listed:   # the names and sizes (the session copy's), else the snapshot's count
            files = ", ".join(f"{f['path']} ({_size(f.get('size'))})" for f in listed)
            field("skill_files_label", files, max_lines=2)
        elif s.get("files"):
            field("skill_files_label", s["files"])
        if s.get("description"):
            field("mcp_description", s["description"], max_lines=4)
        preview = self.preview()
        if preview:
            lines += [[(DIM, part)] for part in wrap(preview, width, 4)]
        body = skill_md_body(str(s.get("skill_md") or "")).splitlines()
        if body:   # the start of what the members read; /skills show has all of it
            for line in body[:self.SKILL_MD_LINES]:
                lines += [[(TEXT, part)] for part in wrap(line.rstrip(), width, 1)] if line.strip() else [[(TEXT, "")]]
            if len(body) > self.SKILL_MD_LINES:
                lines.append([(DIM, t(lang, "skill_md_more", name=self.name))])
        lines += [[(DIM, part)] for part in wrap(t(lang, "skill_uses"), width, 2)]
        return lines

    def preview(self) -> str:
        """What its packages install (from the snapshot's deps states)."""
        return skill_states_preview(RenderContext(lang=self.lang), self.skill.get("deps"))


class SessionPicker(Dialog):
    """↑↓ and Enter pick a session (its id), Delete picks it for deletion ((DELETE, id)), Esc cancels.
    `focus`: the session selected at the start (default: the current one); `title`: the picker's title."""

    kind = "sessions"

    def __init__(self, lang: str, sessions: Sequence[dict], current: str | None = None, unicode: bool = True,
                 *, focus: str | None = None, title: str | None = None):
        options = []
        index = 0
        focus = focus or current
        dot = " · " if unicode else " - "
        for i, s in enumerate(sessions):
            sid = str(s.get("id") or "")
            mark = ("▶ " if unicode else "> ") if current and sid == current else "  "
            hint = [sid, session_status(lang, s)]
            goal = (s.get("goal") or {}).get("title") if isinstance(s.get("goal"), dict) else None
            if goal:
                hint.append(" ".join(str(goal).split())[:80])
            options.append(Option(mark + " ".join(str(s.get("title") or sid).split()), sid, dot.join(hint)))
            if focus and sid == focus:
                index = i
        super().__init__(lang, title or t(lang, "pick_session"), options, index=index,
                         footer=t(lang, "pick_session_footer"), numbered=False, rows=10)

    def remove(self) -> None:
        option = self.select.selected
        if option is not None:
            self._resolve((DELETE, option.value))

    def pick_number(self, number: int) -> bool:
        return False


def session_status(lang: str, s: dict) -> str:
    """Status label of a session summary (as the sessions table shows it)."""
    goal = s.get("goal") if isinstance(s.get("goal"), dict) else {}
    if s.get("config_error") or s.get("error"):
        return t(lang, "sess_error")
    if goal.get("status") == "done":
        return t(lang, "sess_done")
    if s.get("status") == "running":
        if s.get("paused"):
            return t(lang, "sess_paused")
        return t(lang, "sess_wrap_up") if s.get("wrap_up") else t(lang, "sess_running")
    return t(lang, "sess_stopped")


def border_chars(unicode: bool) -> dict[str, str]:
    """Characters of the rounded input box."""
    if unicode:
        return {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"}
    return {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|"}


__all__ = [
    "Option", "SelectList", "Dialog", "ConfirmDialog", "BusyDialog", "MCPApprovalDialog", "SessionPicker",
    "APPROVE", "REJECT", "DELETE", "box", "fit", "wrap", "frag_len", "session_status", "border_chars",
]
