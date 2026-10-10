"""Input helpers of the terminal UI (docs/TUI.md §4.1-4.2): suggestions for `/` commands, `@` members and
`#` tasks, parsing of what the user typed, the addressee rules, and the per-home preference and history
files. Nothing here touches the terminal or the hub."""

from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from ..console import match_addressee

ALL = "all"
ALL_ALIASES = (ALL, "все", "everyone")
MAX_ROWS = 8                   # suggestion rows shown under the status line
MAX_SUGGESTIONS = 50
PREFS_FILE = "tui.json"         # <home>/.dreamteam/tui.json: {"lang": "ru"}
HISTORY_FILE = "tui_history"    # <home>/.dreamteam/tui_history (prompt_toolkit FileHistory)
EXPLICIT_ADDRESSEE_SECONDS = 60.0

_MENTION = re.compile(r"(?:^|(?<=\s))@([^@\n]{0,60})$")
_TASK_REF = re.compile(r"(?:^|(?<=\s))#(\w{0,40})$")


@dataclass(frozen=True)
class Suggestion:
    kind: str                # "command" | "member" | "task"
    label: str               # "/board", "@Анна Смирнова", "#3 Title"
    description: str         # dim text after the label
    start: int               # text[start:end] is replaced by `insert`
    end: int
    insert: str
    runnable: bool = False   # a command that needs no argument: Enter runs it

    def apply(self, text: str) -> tuple[str, int]:
        """The text with this suggestion applied and the new cursor position."""
        new = text[: self.start] + self.insert + text[self.end:]
        return new, self.start + len(self.insert)


@dataclass(frozen=True)
class CommandInfo:
    """What the suggestions need to know about a slash command."""
    name: str
    description: str
    aliases: tuple[str, ...] = ()
    args: str = ""
    required: bool = False   # Enter on the suggestion completes "/name " instead of running it


# ---- suggestions ----------------------------------------------------------------------------------
def suggest(text: str, cursor: int | None = None, *, commands: Sequence[CommandInfo] = (),
            members: Iterable[str] = (), tasks: Iterable[dict] = (),
            everyone: str = "", status: Callable[[str], str] | None = None) -> list[Suggestion]:
    """Suggestions for the word being typed at `cursor`: commands after a leading `/`, members (plus
    `all`) after `@`, tasks after `#`. `everyone` describes `all` (e.g. «все»); `status` turns a task
    status into its label (e.g. «к работе»)."""
    cursor = len(text) if cursor is None else max(0, min(cursor, len(text)))
    before = text[:cursor]
    if before.startswith("/") and not any(ch.isspace() for ch in before):
        return _command_suggestions(text, before[1:], commands)
    mention = _MENTION.search(before)
    if mention is not None:
        return _member_suggestions(text, cursor, mention, members, everyone)
    ref = _TASK_REF.search(before)
    if ref is not None:
        return _task_suggestions(text, cursor, ref, tasks, status)
    return []


def _token_end(text: str, start: int) -> int:
    match = re.compile(r"\S*").match(text, start)
    return match.end() if match else start


def _command_suggestions(text: str, query: str, commands: Sequence[CommandInfo]) -> list[Suggestion]:
    query = query.casefold()
    ranked: list[tuple[int, int, Suggestion]] = []
    end = _token_end(text, 0)
    for order, cmd in enumerate(commands):
        names = (cmd.name, *cmd.aliases)
        rank, label = None, cmd.name
        for name in names:
            if name.casefold().startswith(query):
                rank, label = 0 if name == cmd.name else 1, name
                break
        if rank is None:
            if query and any(query in name.casefold() for name in names):
                rank = 2
            else:
                continue
        insert = "/" + label + (" " if cmd.required else "")
        display = "/" + label + (f" {cmd.args}" if cmd.args else "")
        ranked.append((rank, order, Suggestion("command", display, cmd.description, 0, end, insert,
                                               runnable=not cmd.required)))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [s for _, _, s in ranked][:MAX_SUGGESTIONS]


def _member_suggestions(text: str, cursor: int, match: re.Match, members: Iterable[str],
                        everyone: str) -> list[Suggestion]:
    query = " ".join(match.group(1).split()).casefold()
    names = [*dict.fromkeys(n for n in members if n and n != ALL), ALL]
    if match.group(1)[-1:].isspace() and any(" ".join(n.split()).casefold() == query for n in names):
        return []  # "@Анна Смирнова " is complete: the message follows
    start = match.start(0)
    out: list[tuple[int, Suggestion]] = []
    for name in names:
        folded = name.casefold()
        words = folded.split()
        if not query or folded.startswith(query):
            rank = 0
        elif " " not in query and any(w.startswith(query) for w in words[1:]):
            rank = 1
        else:
            continue
        desc = everyone if name == ALL else ""
        out.append((rank, Suggestion("member", "@" + name, desc, start, cursor, "@" + name + " ")))
    out.sort(key=lambda item: item[0])
    return [s for _, s in out][:MAX_SUGGESTIONS]


def _task_suggestions(text: str, cursor: int, match: re.Match, tasks: Iterable[dict],
                      status: Callable[[str], str] | None = None) -> list[Suggestion]:
    query = match.group(1).casefold()
    start = match.start(0)
    open_rows: list[Suggestion] = []
    closed_rows: list[Suggestion] = []
    for task in tasks:
        if not isinstance(task, dict) or task.get("id") is None:
            continue
        tid = str(task.get("id"))
        title = " ".join(str(task.get("title") or "").split())
        if query and not (tid.startswith(query) or query in title.casefold()):
            continue
        raw = str(task.get("status") or "")
        row = Suggestion("task", f"#{tid} {title}".rstrip(), status(raw) if status and raw else raw, start,
                         cursor, f"#{tid} ")
        (closed_rows if task.get("status") in ("done", "failed", "cancelled") else open_rows).append(row)
    return (open_rows + closed_rows)[:MAX_SUGGESTIONS]


def window(count: int, selected: int, rows: int = MAX_ROWS) -> range:
    """Indexes of the rows to show so that `selected` stays visible."""
    if count <= rows:
        return range(count)
    first = min(max(0, selected - rows + 1), count - rows)
    return range(first, first + rows)


# ---- parsing --------------------------------------------------------------------------------------
def parse_command(text: str) -> tuple[str, str] | None:
    """`/name args` -> ("name", "args"); None when the text is not a slash command."""
    stripped = text.strip()
    match = re.match(r"/(\S*)(?:\s+(.*))?\Z", stripped, re.S)
    if match is None:
        return None
    return match.group(1).casefold(), (match.group(2) or "").strip()


def bash_command(text: str) -> str | None:
    """`!command` -> "command" (bash mode); None for any other input."""
    stripped = text.strip()
    return stripped[1:].strip() if stripped.startswith("!") else None


def is_bash(text: str) -> bool:
    return text.lstrip().startswith("!")


def split_mention(text: str, members: Iterable[str]) -> tuple[str | None, str]:
    """`@Имя Фамилия текст` / `@all текст` -> (member or "all", text); (None, text) when nobody matches.
    A first name alone is enough when it is unique (console.match_addressee)."""
    name, body = match_addressee(text, [*members, *ALL_ALIASES])
    return (ALL if name in ALL_ALIASES else name), body


def resolve_member(name: str, members: Iterable[str]) -> str | None:
    """A member (or "all") named by `name` (full name, or a unique first name; case-insensitive)."""
    name = " ".join(name.strip().lstrip("@").split())
    if not name:
        return None
    if name.casefold() in ALL_ALIASES:
        return ALL
    found, rest = match_addressee(name, list(members))
    return found if found and not rest.strip() else None


def did_you_mean(name: str, names: Iterable[str], limit: int = 3) -> list[str]:
    """Close command names for an unknown `/name` (prefix matches first)."""
    names = list(dict.fromkeys(names))
    name = name.casefold()
    prefix = [n for n in names if name and (n.startswith(name) or name.startswith(n))]
    close = difflib.get_close_matches(name, names, n=limit, cutoff=0.5)
    return list(dict.fromkeys(prefix + close))[:limit]


def number(value: str) -> float | None:
    """A number typed by the user ("5", "$2.50", "2,5"); None if it is not one."""
    text = value.strip().replace("$", "").replace(",", ".")
    try:
        result = float(text)
    except ValueError:
        return None
    return result if result == result and abs(result) != float("inf") else None


# ---- addressee ------------------------------------------------------------------------------------
def members_of(state: dict | None) -> list[str]:
    """Names of the current (not retired) members of a /api/state snapshot."""
    return [a["name"] for a in (state or {}).get("agents") or []
            if isinstance(a, dict) and a.get("name") and a.get("status") != "retired"]


def planning_assignee(state: dict | None) -> str | None:
    members = set(members_of(state))
    for task in (state or {}).get("tasks") or []:
        if isinstance(task, dict) and "planning" in (task.get("tags") or []) and task.get("assignee") in members:
            return task["assignee"]
    return None


def default_addressee(state: dict | None, last_sender: str | None) -> str:
    """The agent who last messaged the user, else the planning task's assignee, else `all`."""
    if last_sender and last_sender in members_of(state):
        return last_sender
    return planning_assignee(state) or ALL


# ---- files of the home ----------------------------------------------------------------------------
def data_dir(home: Path) -> Path:
    return Path(home) / ".dreamteam"


def history_path(home: Path) -> Path:
    path = data_dir(home) / HISTORY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_prefs(home: Path) -> dict[str, Any]:
    try:
        data = json.loads((data_dir(home) / PREFS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_prefs(home: Path, **updates: Any) -> None:
    prefs = {**load_prefs(home), **updates}
    path = data_dir(home) / PREFS_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{PREFS_FILE}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # a preference that cannot be saved must not break the UI


__all__ = [
    "ALL", "ALL_ALIASES", "MAX_ROWS", "Suggestion", "CommandInfo", "suggest", "window", "parse_command", "bash_command",
    "is_bash", "split_mention", "resolve_member", "did_you_mean", "number", "members_of", "planning_assignee",
    "default_addressee", "history_path", "load_prefs", "save_prefs",
]
