"""Colours, symbols and prompt_toolkit styles of the terminal UI (the Claude Code look, docs/TUI.md §3).

Colours are mid-luminance hex values chosen to stay readable on dark and on light terminal backgrounds;
secondary text uses the terminal's own "dim" attribute instead of a fixed grey."""

from __future__ import annotations

import codecs
import sys
import zlib
from typing import Any

# ---- colours (rich style strings) ----------------------------------------------------------------
ACCENT = "#d97757"        # Claude orange: welcome box, input border, spinner, commands
OK = "#3a9e4a"            # done tasks, achieved goal, resumed swarm
ERROR = "#e0475b"         # errors, failed tasks
WARN = "#c79200"          # pause, budget, pending approvals
BASH = "#e3489f"          # `!` bash mode
INFO = "#5f87d7"          # links, directories
USER = ACCENT             # the user's own name
USER_ECHO = "#e4e4e4 on #3a3a3a"   # "> text" echo of a sent message: light text on a dark chip (both themes)
CODE_THEME = "ansi_dark"  # pygments-free ANSI theme: follows the terminal palette, works on light and dark

# Agent colours: solarized-like accents, which were designed to read well on both light and dark
# backgrounds. Errors (red) and the accent (orange) are left out so an agent never looks like an error.
AGENT_PALETTE: tuple[str, ...] = (
    "#5f87d7",  # blue
    "#2aa198",  # teal
    "#9c6ade",  # violet
    "#d33682",  # magenta
    "#859900",  # olive
    "#268bd2",  # azure
    "#b58900",  # amber
    "#6c71c4",  # indigo
    "#3a9e6e",  # green
    "#c0607e",  # rose
    "#4f97a3",  # steel cyan
    "#a0794f",  # brown
)


def agent_color(name: str | None) -> str:
    """Stable colour of a member: crc32 of the case-folded name (Python's hash() is randomized per run)."""
    if not name:
        return "default"
    if name == "user":
        return USER
    key = " ".join(str(name).split()).casefold().encode("utf-8", "replace")
    return AGENT_PALETTE[zlib.crc32(key) % len(AGENT_PALETTE)]


def agent_style(name: str | None) -> str:
    """rich style string for a member's bullet and name."""
    return agent_color(name)


# ---- symbols --------------------------------------------------------------------------------------
SYMBOLS: dict[str, str] = {
    "bullet": "⏺",     # activity line
    "cont": "⎿",       # continuation ("  ⎿  result")
    "todo": "☐",
    "doing": "◐",
    "review": "◑",
    "done": "☒",
    "failed": "✗",
    "spark": "✻",      # thinking, welcome
    "mail": "✉",       # agent-to-agent message
    "chat": "💬",      # task discussion
    "bell": "🔔",      # notification to the user
    "warn": "⚠",
    "alarm": "⏰",     # wake-up
    "nudge": "↻",
    "pause": "⏸",
    "play": "▶",
    "plus": "+",
    "minus": "−",
    "arrow": "→",
    "reply": "↳",
    "up": "↑",
    "down": "↓",
    "check": "✓",
    "dot": "·",
    "ellipsis": "…",
    "dash": "—",
    "prompt": ">",
    "edit": "✎",       # file changed / locked
    "trigger": "⚡",
    "folder": "▸",
    "current": "▶",
}

SYMBOLS_ASCII: dict[str, str] = {
    "bullet": "*",
    "cont": "|_",
    "todo": "[ ]",
    "doing": "[~]",
    "review": "[?]",
    "done": "[x]",
    "failed": "x",
    "spark": "*",
    "mail": ">",
    "chat": "#",
    "bell": "!",
    "warn": "!",
    "alarm": "@",
    "nudge": "~",
    "pause": "||",
    "play": ">",
    "plus": "+",
    "minus": "-",
    "arrow": "->",
    "reply": ">",
    "up": "^",
    "down": "v",
    "check": "ok",
    "dot": "-",
    "ellipsis": "...",
    "dash": "-",
    "prompt": ">",
    "edit": "~",
    "trigger": "!",
    "folder": ">",
    "current": ">",
}

SPINNER_FRAMES_UNICODE = ("·", "✢", "✳", "✶", "✻", "✽")
SPINNER_FRAMES_ASCII = ("-", "\\", "|", "/")
# box drawing of rounded panels, plus typography used in the UI texts
_UNICODE_PROBE = "".join(SYMBOLS.values()) + "".join(SPINNER_FRAMES_UNICODE) + "╭╮╰╯─│«»"


def can_encode(encoding: str | None, chars: str = _UNICODE_PROBE) -> bool:
    """True when `encoding` can encode every UI symbol (False for None, cp1251, cp866, ascii...)."""
    if not encoding:
        return False
    try:
        codecs.lookup(encoding)
        chars.encode(encoding)
    except (LookupError, UnicodeError):
        return False
    return True


def unicode_supported(stream: Any = None) -> bool:
    """Whether the output stream (default sys.stdout) can show the unicode symbols."""
    stream = sys.stdout if stream is None else stream
    return can_encode(getattr(stream, "encoding", None))


def pick_symbols(stream: Any = None) -> dict[str, str]:
    return SYMBOLS if unicode_supported(stream) else SYMBOLS_ASCII


def symbols(unicode: bool) -> dict[str, str]:
    return SYMBOLS if unicode else SYMBOLS_ASCII


# ---- prompt_toolkit -------------------------------------------------------------------------------
# Input of prompt_toolkit's Style.from_dict(). Fragments returned by render.spinner_line / status_line use
# these class names ("class:spinner.verb"); the app may use the box/suggestion/dialog ones for its layout.
PT_STYLE: dict[str, str] = {
    "spinner": ACCENT,                       # the animated ✻ frame
    "spinner.verb": f"{ACCENT} bold",        # "Совещаются…"
    "spinner.info": "dim",                   # "(12s · ↑ 4.2k ↓ 310 · $0.12 · esc — пауза)"
    "spinner.agents": "dim italic",          # names of the working agents
    "spinner.paused": f"{WARN} bold",        # "⏸ Рой на паузе"
    "status": "dim",                         # the status line under the input box
    "status.hint": "dim",                    # "? — горячие клавиши" or a transient hint
    "status.mode": f"{WARN}",                # verbosity / input mode
    "status.session": "bold",                # session title
    "status.addressee": ACCENT,              # "→ Анна"
    "status.sep": "dim",                     # " · "
    "status.warn": f"{WARN} bold",           # "нет связи…", budget nearly spent
    "status.error": ERROR,
    "status.ok": OK,
    "box.border": ACCENT,                    # rounded input box
    "box.border.bash": BASH,                 # input starts with "!"
    "box.border.dialog": WARN,               # a dialog is open
    "box.title": f"{ACCENT} bold",
    "prompt": "bold",                        # "> "
    "prompt.bash": f"{BASH} bold",           # "! "
    "placeholder": "dim italic",
    "suggestion": "",
    "suggestion.name": ACCENT,
    "suggestion.desc": "dim",
    "suggestion.selected": "reverse",
    "dialog.title": f"{WARN} bold",
    "dialog.text": "",
    "dialog.dim": "dim",
    "dialog.option": "",
    "dialog.option.selected": f"{ACCENT} bold",
}
