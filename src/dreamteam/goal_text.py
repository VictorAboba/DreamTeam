"""How a goal typed as one text becomes {title, description} (docs/SPEC.md §3)."""

from __future__ import annotations

import re

GOAL_TITLE_MAX = 200
_HEADING = re.compile(r"#{1,6}\s+")
_SENTENCE_END = re.compile(r"[.!?…](?=\s)")


def _block(text: str) -> str:
    """Newlines normalized (CRLF and CR to LF), trailing whitespace of each line and blank lines at both ends removed;
    inner blank lines and indentation are kept."""
    lines = [line.rstrip() for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _first_line(text: str) -> tuple[str, str]:
    block = _block(text)
    first, _, rest = block.partition("\n")
    head = " ".join(first.split())
    if _HEADING.match(head):          # "# Snake game" pasted from Markdown
        head = _HEADING.sub("", head, count=1)
    return head, _block(rest)


def shorten_title(line: str, limit: int = GOAL_TITLE_MAX) -> str:
    """`line` if it fits; else its sentences that fit (when they make at least a quarter of the limit), else a cut at
    the last space with "…" (at most `limit` characters)."""
    if len(line) <= limit:
        return line
    ends = [m.end() for m in _SENTENCE_END.finditer(line, 0, limit + 1) if m.end() <= limit]
    if ends and ends[-1] >= limit // 4:
        return line[:ends[-1]]
    space = line.rfind(" ", 0, limit)
    cut = space if space >= limit // 4 else limit - 1
    return line[:cut].rstrip(" ,;:-–—") + "…"


def split_goal(title: str, description: str = "") -> tuple[str, str]:
    """(title, description) of a goal. The first non-empty line of `title` is the title; its other lines open the
    description, followed after a blank line by `description`. An empty `title` takes the whole text from
    `description`. A first line over GOAL_TITLE_MAX characters is shortened, and the full line then opens the
    description, so nothing is lost. Idempotent: split_goal(*split_goal(t, d)) == split_goal(t, d)."""
    head, rest = _first_line(title)
    extra = _block(description)
    if not head:
        head, rest = _first_line(extra)
        extra = ""
    if len(head) > GOAL_TITLE_MAX:
        rest = head + ("\n" + rest if rest else "")
        head = shorten_title(head)
    return head, "\n\n".join(part for part in (rest, extra) if part)
