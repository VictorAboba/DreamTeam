"""The symbols of the feed lines (docs/TUI.md §3, docs/SPEC.md §21.6): the unicode table and its ASCII fallback, one
key for one meaning. The terminal UI (tui/theme.py re-exports them) and the HTTP API's event texts (eventtext.py)
take them from here.

Stdlib only: hub code imports it, and it never imports dreamteam.tui (whose package imports prompt_toolkit)."""

from __future__ import annotations

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
    "job": "⚙",        # background job of an agent
    "folder": "▸",
    "current": "▶",
    "sub": "●",        # a direct subtask on its parent's board line (its colour = its status)
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
    "job": "&",
    "folder": ">",
    "current": ">",
    "sub": "o",
}


def symbols(unicode: bool) -> dict[str, str]:
    """The unicode table, or the ASCII one for a terminal that cannot show it."""
    return SYMBOLS if unicode else SYMBOLS_ASCII


__all__ = ["SYMBOLS", "SYMBOLS_ASCII", "symbols"]
