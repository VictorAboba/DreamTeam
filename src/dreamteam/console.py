"""Terminal chat with the swarm, talking to a running hub over its REST API (stdlib only)."""

from __future__ import annotations

import json
import ntpath
import os
import posixpath
import re
import shlex
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Iterable

HELP = {
    "ru": """Команды:
  @Имя Фамилия текст      — написать агенту (или /msg Имя Фамилия: текст)
  /chat Имя Фамилия        — дальше обычный текст уходит этому агенту (/chat без имени — выйти)
  /all текст               — написать всем агентам
  /goal Заголовок | описание — задать глобальную цель
  /task Заголовок | цель   — создать задачу
  /board  /agents  /status — доска, команда, статус
  /jobs [jN | stop jN]     — фоновые процессы агентов: список, вывод одного, остановка
  /show N                  — задача N с обсуждением
  /comment N текст         — написать в обсуждение задачи N
  /hire Роль [| инструкции]  — нанять агента (роль из каталога или новая)
  /pause  /resume          — пауза / продолжить
  /budget 10               — лимит бюджета в $;  /max 6 — максимум агентов
  /approve имя|N [комментарий]  /reject имя|N [что исправить] — MCP-сервер или задача на ревью
  /trace [Имя | N]         — трейсы агентов: последние, агента или трейс N по шагам
  /span N [ctx]            — шаг трейса полностью (ctx — сообщения, которые видела модель)
  /session                 — текущая сессия (другая: dreamteam console --session ID)
  /cd [путь | --default]   — папка сессии / перенести сессию в другую папку (спросит y/N; --yes — без вопроса)
  /help  /quit""",
    "en": """Commands:
  @First Last text         — message an agent (or /msg First Last: text)
  /chat First Last         — plain text then goes to this agent (/chat alone to leave)
  /all text                — message every agent
  /goal Title | description — set the global goal
  /task Title | goal       — create a task
  /board  /agents  /status — board, team, status
  /jobs [jN | stop jN]     — agents' background jobs: list, one job's output, stop
  /show N                  — task N with its discussion
  /comment N text          — post to the discussion of task N
  /hire Role [| instructions] — hire an agent (a catalog role or a new one)
  /pause  /resume          — pause / resume
  /budget 10               — budget limit in $;  /max 6 — max agents
  /approve name|N [comment]  /reject name|N [what to fix] — an MCP server or a task in review
  /trace [Name | N]        — agent traces: recent ones, an agent's, or trace N step by step
  /span N [ctx]            — one trace step in full (ctx — the messages the model saw)
  /session                 — the current session (another: dreamteam console --session ID)
  /cd [path | --default]   — the session folder / move the session to another folder (asks y/N; --yes: no question)
  /help  /quit""",
}

# `/cd`: the folder a launcher session works in (docs/SPEC.md §17.2 "Workspace folder")
CD_TEXTS = {
    "ru": {
        "launcher_only": "/cd работает только с сессией лаунчера (dreamteam serve), не с отдельным роем.",
        "remote_relative": "Хаб не на этом компьютере: укажите абсолютный путь к папке.",
        "current": "Папка сессии: {path} ({kind})",
        "kind_default": "своя папка сессии",
        "kind_moved": "своя папка сессии, перенесена сюда",
        "kind_adopted": "папка проекта: агенты коммитят в её текущую ветку",
        "missing": "Папки {path} нет — перенесите сессию: /cd <папка> или /cd --default",
        "hint": "/cd <папка> — перенести сессию в другую папку, /cd --default — вернуть в её собственную.",
        "file": "{path} — это файл, а не папка.",
        "ask": "Перенести сессию «{title}» в {path}? {text} [y/N] ",
        "ask_default": "Вернуть сессию «{title}» в её собственную папку {path}? {text} [y/N] ",
        "text_missing": "Папки нет — она будет создана; файлы и история git переедут.",
        "text_empty": "Папка пустая: файлы и история git переедут.",
        "text_fresh": "Папка пустая: сессия начнёт в ней с чистого листа (новый git-репозиторий); ваша папка {old} "
                      "останется как есть, переедут только MCP-серверы сессии.",
        "text_files": "Папка не пустая: агенты будут работать с её файлами и коммитить в текущую ветку (прежняя "
                      "папка {old} останется как есть).",
        "continue": "Продолжить?",
        "text_back": "В папке остались прежние файлы сессии: агенты вернутся к ним; папка {old} останется как есть.",
        "text_unknown": "Пустая папка: файлы и история git переедут. Папка с файлами: агенты будут работать с ними и "
                        "коммитить в текущую ветку.",
        "text_lost": "Прежней папки {old} нет — переносить нечего: сессия продолжит в новой папке.",
        "text_running": "Сессия запущена: агенты остановятся на время переноса и продолжат работу.",
        "cancelled": "Отменено: папка сессии не изменилась.",
        "unchanged": "Сессия «{title}» уже работает в {path}.",
        "moved": "Сессия «{title}» теперь работает в {path}: файлы перенесены.",
        "now": "Сессия «{title}» теперь работает в {path}.",
        "adopted": "Сессия «{title}» теперь работает с файлами папки {path}.",
        "created": "Сессия «{title}» работает в новой папке {path}.",
        "git_moved": "git: история перенесена вместе с файлами.",
        "git_existing": "git: коммиты агентов пойдут в текущую ветку репозитория этой папки.",
        "git_initialized": "git: в папке создан новый репозиторий (git init).",
        "git_disabled": "git выключен в настройках — коммитов не будет.",
        "git_none": "git не установлен — коммитов не будет.",
        "mcp_moved": "MCP-серверы сессии перенесены: {names}.",
        "old_kept": "Прежняя папка {path} осталась на месте.",
        "old_mcp_committed": "Прежняя папка {path} осталась на месте; MCP-серверы сессии ({paths}) убраны из неё, "
                             "удаление закоммичено там ({commit}).",
        "old_mcp_uncommitted": "Прежняя папка {path} осталась на месте, но MCP-серверы сессии ({paths}) убраны из неё, "
                               "и это удаление там не закоммичено ({error}): закоммитьте его или верните файлы "
                               "(git checkout -- {paths}).",
        "old_missing": "Прежней папки {path} не было — переносить было нечего.",
        "start_error": "Сессия не запустилась снова: {error}",
    },
    "en": {
        "launcher_only": "/cd works with a launcher session (dreamteam serve), not with a single swarm.",
        "remote_relative": "The hub is not on this computer: give the absolute path of the folder.",
        "current": "Session folder: {path} ({kind})",
        "kind_default": "the session's own folder",
        "kind_moved": "the session's own folder, moved here",
        "kind_adopted": "a project folder: the agents commit to its current branch",
        "missing": "The folder {path} does not exist — move the session: /cd <folder> or /cd --default",
        "hint": "/cd <folder> moves the session to another folder, /cd --default back to its own one.",
        "file": "{path} is a file, not a folder.",
        "ask": "Move session \"{title}\" to {path}? {text} [y/N] ",
        "ask_default": "Move session \"{title}\" back to its own folder {path}? {text} [y/N] ",
        "text_missing": "The folder does not exist — it will be created; the files and the git history move there.",
        "text_empty": "The folder is empty: the files and the git history move there.",
        "text_fresh": "The folder is empty: the session starts afresh there (a new git repository); your folder {old} "
                      "stays as it is, only the session's MCP servers move.",
        "text_files": "The folder is not empty: the agents will work with its files and commit to its current branch "
                      "(the previous folder {old} stays as it is).",
        "continue": "Continue?",
        "text_back": "The session's previous files are still there: the agents go back to them; {old} stays as it is.",
        "text_unknown": "An empty folder: the files and the git history move there. A folder with files: the agents "
                        "work with them and commit to its current branch.",
        "text_lost": "The current folder {old} does not exist, so nothing moves: the session carries on in the new "
                     "folder.",
        "text_running": "The session is running: its agents stop during the move and then carry on.",
        "cancelled": "Cancelled: the session folder is unchanged.",
        "unchanged": "Session \"{title}\" already works in {path}.",
        "moved": "Session \"{title}\" now works in {path}: its files moved there.",
        "now": "Session \"{title}\" now works in {path}.",
        "adopted": "Session \"{title}\" now works with the files of {path}.",
        "created": "Session \"{title}\" works in the new folder {path}.",
        "git_moved": "git: the history moved along with the files.",
        "git_existing": "git: the agents commit to the current branch of this folder's repository.",
        "git_initialized": "git: a new repository was created in the folder (git init).",
        "git_disabled": "git is off in the settings: no commits.",
        "git_none": "git is not installed: no commits.",
        "mcp_moved": "The session's MCP servers moved along: {names}.",
        "old_kept": "The previous folder {path} stays where it is.",
        "old_mcp_committed": "The previous folder {path} stays where it is; the session's MCP servers ({paths}) left "
                             "it, and their removal is committed there ({commit}).",
        "old_mcp_uncommitted": "The previous folder {path} stays where it is, but the session's MCP servers ({paths}) "
                               "left it and their removal is not committed there ({error}): commit it, or restore "
                               "them with git checkout -- {paths}.",
        "old_missing": "The previous folder {path} did not exist: nothing was moved.",
        "start_error": "The session did not start again: {error}",
    },
}
JOB_STOP = ("stop", "kill", "стоп")   # `/jobs stop j3`
YES_WORDS = frozenset({"y", "yes", "д", "да"})
NO_WORDS = frozenset({"", "n", "no", "н", "нет"})
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})

COLORS = {"agent": "\033[36m", "note": "\033[33m", "err": "\033[31m", "dim": "\033[2m", "end": "\033[0m"}

# Text written by agents (LLM output, possibly steered by fetched web pages) must not drive the terminal:
# drop C0/C1 control characters (ESC sequences can retitle the window, rewrite lines, set the clipboard).
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_NAME_END = r"(?=$|[\s,:;.!?])"


def clean(text: Any) -> str:
    return _CONTROL.sub("", str(text).replace("\r\n", "\n"))


def match_addressee(text: str, members: Iterable[str]) -> tuple[str | None, str]:
    """Split '@First Last message' (or 'First Last: message') into (member, message).

    Full names match case-insensitively with any whitespace between their words and must end at a word
    boundary ('@Анна Смирнова,' yes, '@Анна Смирновааа' no); the longest name wins. A first name alone
    works when exactly one member has it."""
    body = text.strip().lstrip("@").lstrip()
    names = sorted(set(members), key=lambda n: (len(n.split()), len(n)), reverse=True)
    for name in names:
        words = name.split()
        if not words:
            continue
        m = re.match(r"\s+".join(map(re.escape, words)) + _NAME_END, body, re.IGNORECASE)
        if m:
            return name, body[m.end():].lstrip(" \t:,;")
    first = re.match(r"([^\s,:;.!?]+)" + _NAME_END, body)
    if first:
        hits = [n for n in names if n.split() and n.split()[0].casefold() == first.group(1).casefold()]
        if len(hits) == 1:
            return hits[0], body[first.end():].lstrip(" \t:,;")
    return None, body


# ---- `/cd`: helpers shared with the TUI (dreamteam.tui) -----------------------------------------------
def is_loopback(url: str) -> bool:
    """The hub at `url` runs on this computer (as far as its address tells)."""
    try:
        return (urllib.parse.urlsplit(url).hostname or "") in _LOOPBACK
    except ValueError:
        return False


def unquote(text: str) -> str:
    """A path typed with quotes around it ("C:\\My Projects\\app") without them."""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1].strip()
    return text


def resolve_folder(text: str, *, local: bool, cwd: str | None = None) -> str | None:
    """A folder typed by the user as the absolute path the hub needs. `~` and relative paths are resolved here,
    against this process's current directory, only when the hub runs on this computer (`local`); for another
    hub they mean nothing (None). An absolute path is passed on as typed (normalized when local)."""
    path = unquote(text)
    if not path:
        return None
    if local:
        # (os.path.join, not isabs: on Windows "\projects\app" is rooted but has no drive; it gets the current
        # one, as `--workspace` does with os.path.abspath)
        return os.path.normpath(os.path.join(cwd or os.getcwd(), os.path.expanduser(path)))
    # the hub's system decides what is absolute: "/srv/app" for Linux, "D:\\app" for Windows
    return path if posixpath.isabs(path) or ntpath.isabs(path) else None


def same_folder(a: Any, b: Any, *, local: bool = True) -> bool:
    if not a or not b:
        return False
    try:
        if local:
            return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))
        return os.path.normcase(os.path.normpath(str(a))) == os.path.normcase(os.path.normpath(str(b)))
    except (OSError, ValueError):
        return False


def folder_state(path: str) -> str:
    """"missing", "file", "empty", "files" (something inside) or "unknown" (cannot be read)."""
    try:
        if not os.path.lexists(path):
            return "missing"
        if not os.path.isdir(path):
            return "file"
        with os.scandir(path) as entries:
            return "files" if next(entries, None) is not None else "empty"
    except OSError:
        return "unknown"


def folder_kind(info: dict) -> str:
    """The text key of what a session folder is: its own one, its own files moved elsewhere, or adopted."""
    if info.get("default", True):
        return "kind_default"
    return "kind_adopted" if info.get("adopted") else "kind_moved"


def cd_preview(current: dict, target: str | None, default_folder: str, *, local: bool) -> tuple[str, str | None]:
    """What moving a session from its folder `current` (a summary's "workspace") to `target` (None: its own
    `default_folder`) will do, as far as this client can tell: (state, the key of the text that explains it, None
    when there is nothing to ask). state: "same" (nothing to do), "file" (the hub refuses it), "missing",
    "empty", "files" or "unknown" (a hub on another computer: the folder cannot be looked at from here). The
    hub decides; this only words the question (docs/SPEC.md §17.2)."""
    path = default_folder if target is None else target
    if (target is None and current.get("default")) or same_folder(path, current.get("path"), local=local):
        return "same", None
    state = folder_state(path) if local else "unknown"
    if state == "file":
        return state, None
    if state in ("missing", "empty"):
        if current.get("adopted"):  # the user's files stay where they are: nothing of theirs moves
            return state, "text_fresh"
        if current.get("exists") is False:
            return state, "text_lost"
        return state, "text_missing" if state == "missing" else "text_empty"
    if state == "files":
        return state, "text_back" if target is None else "text_files"
    return state, "text_unknown"


def cd_expect(state: str) -> str | None:
    """The mode the hub must find for a folder whose state (cd_preview) the user was told about: "moved" for an
    empty or missing folder, "adopted" for one with files; None when this client could not look (another computer)."""
    return {"missing": "moved", "empty": "moved", "files": "adopted"}.get(state)


def cd_moves_files(state: str, current: dict) -> bool:
    """Whether the session's own files leave its folder `current` (a summary's "workspace") for a target in `state`
    (cd_preview): a move into an empty or missing folder (or one this client could not look at). An adopt, or a
    move away from a folder of the user or from one that does not exist, leaves the files where they are (only
    the session's MCP servers move): a `!` command running there can go on."""
    if state not in ("missing", "empty", "unknown"):
        return False
    return not current.get("adopted") and current.get("exists") is not False


def cd_result_lines(result: dict, title: str) -> list[tuple[str, dict[str, Any]]]:
    """A workspace change (`POST /api/sessions/{sid}/workspace` "result") as (text key, fields) lines; the
    "start_error" line is an error."""
    mode, to = result.get("mode"), result.get("to")
    if mode == "unchanged":
        return [("unchanged", {"title": title, "path": to})]
    if mode in ("adopted", "created"):
        head = mode
    elif result.get("old_kept") or result.get("old_missing"):
        head = "now"  # nothing of the session's moved: the user's folder stays, or there was nothing
    else:
        head = "moved"
    lines: list[tuple[str, dict[str, Any]]] = [(head, {"title": title, "path": to})]
    if result.get("git") in ("moved", "existing", "initialized", "disabled", "none"):
        lines.append(("git_" + result["git"], {}))
    if result.get("mcp_moved"):
        lines.append(("mcp_moved", {"names": ", ".join(map(str, result["mcp_moved"]))}))
    changed = result.get("old_changed") if isinstance(result.get("old_changed"), dict) else None
    if changed and changed.get("paths"):  # the session's MCP servers left the user's repository
        paths = ", ".join(map(str, changed["paths"]))
        if changed.get("commit"):
            lines.append(("old_mcp_committed", {"path": result.get("old_kept") or result.get("from"), "paths": paths,
                                                "commit": str(changed["commit"])[:8]}))
        else:
            lines.append(("old_mcp_uncommitted", {"path": result.get("old_kept") or result.get("from"),
                                                  "paths": paths, "error": changed.get("error") or "?"}))
    elif result.get("old_kept"):
        lines.append(("old_kept", {"path": result["old_kept"]}))
    if result.get("old_missing") and mode != "created":
        lines.append(("old_missing", {"path": result.get("from")}))
    if result.get("start_error"):
        lines.append(("start_error", {"error": result["start_error"]}))
    return lines


class Hub:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def get(self, path: str, **params: Any) -> Any:
        query = ("?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})) if params else ""
        with urllib.request.urlopen(self.base + path + query, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def post(self, path: str, body: dict[str, Any]) -> Any:
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=660) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def act(self, tool: str, **args: Any) -> str:
        result = self.post(f"/api/act/{tool}", {k: v for k, v in args.items() if v is not None})
        return clean(result.get("result", ""))


def _color(kind: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{COLORS[kind]}{text}{COLORS['end']}"


def _unquote(text: str) -> str:
    """`text` without one pair of quotes around all of it ("'needs tests'" -> "needs tests")."""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1].strip()
    return text


def _print(text: str) -> None:
    sys.stdout.write("\r" + text + "\n")
    sys.stdout.flush()


def _http_error(exc: urllib.error.HTTPError) -> str:
    """The hub's {"error": ...} text of a refused request (else the start of the body)."""
    raw = exc.read().decode("utf-8", "replace")
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("error"), str) and data["error"]:
        return clean(data["error"])
    return f"HTTP {exc.code}: {clean(raw[:300])}"


_SESSION_BASE = re.compile(r"^(?P<hub>.+)/s/(?P<sid>[^/?#]+)$")
_CD_FLAGS = ("--yes", "-y")


class Console:
    def __init__(self, base_url: str, lang: str = "ru", stop: threading.Event | None = None,
                 local: bool | None = None):
        self.hub = Hub(base_url)
        self.lang = lang if lang in HELP else "en"
        self.chat_with: str | None = None
        self.since_message = 0
        self.since_note = 0
        self.stop = stop or threading.Event()
        # a launcher session (`{hub}/s/{sid}`): `/cd` talks to the launcher itself
        found = _SESSION_BASE.match(self.hub.base)
        self.launcher: Hub | None = Hub(found.group("hub")) if found else None
        self.session_id = urllib.parse.unquote(found.group("sid")) if found else None
        # the hub runs on this computer: `~` and relative `/cd` paths are resolved here (`local` True: the hub was
        # found through the home's hub.json, or this console runs inside the hub's process; else a loopback address)
        self.local = is_loopback(self.hub.base) if local is None else local
        # (folder or None: default, title, the mode the question announced) awaiting y/N
        self.pending_cd: tuple[str | None, str, str | None] | None = None

    # ---- incoming --------------------------------------------------------------------------
    def _prime(self) -> None:
        data = self.hub.get("/api/inbox")
        msgs = data.get("messages", [])
        for m in msgs[-5:]:
            self._show_message(m)
        self.since_message = max([m["id"] for m in msgs], default=0)
        self.since_note = max([n["id"] for n in data.get("notifications", [])], default=0)

    def _show_message(self, m: dict[str, Any]) -> None:
        stamp = datetime.fromtimestamp(m["ts"]).strftime("%H:%M")
        others = [clean(r) for r in m.get("recipients", []) if r != "user"]
        extra = f" (+{', '.join(others)})" if others else ""
        sender = clean(m["sender"])
        _print(_color("agent", f"[{stamp}] {sender}{extra} → вы:" if self.lang == "ru" else f"[{stamp}] {sender}{extra} → you:")
               + "\n" + clean(m["text"]))

    def _poll(self) -> None:
        while not self.stop.is_set():
            try:
                data = self.hub.get("/api/inbox", since_message=self.since_message, since_notification=self.since_note)
                for m in data.get("messages", []):
                    if self.stop.is_set():
                        return
                    self._show_message(m)
                    self.since_message = max(self.since_message, m["id"])
                for n in data.get("notifications", []):
                    if self.stop.is_set():
                        return
                    _print(_color("note", "• " + clean(n["text"])))
                    self.since_note = max(self.since_note, n["id"])
            except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError):
                pass
            self.stop.wait(1.5)

    # ---- commands --------------------------------------------------------------------------
    def handle(self, line: str) -> bool:
        """Process one input line; returns False to quit."""
        line = line.strip()
        if not line and self.pending_cd is None:  # (an empty answer to the /cd question is "no")
            return True
        if line in ("/quit", "/exit", "/q"):
            return False
        try:
            out = self._dispatch(line)
        except urllib.error.HTTPError as exc:
            out = _color("err", f"HTTP {exc.code}: {clean(exc.read().decode('utf-8', 'replace')[:300])}")
        except (urllib.error.URLError, OSError) as exc:
            out = _color("err", f"Hub is not reachable: {exc}")
        except ValueError as exc:  # "/show abc", "/budget x", unbalanced quotes, a non-JSON reply...
            out = _color("err", (f"Не понял команду ({exc}). /help — список команд." if self.lang == "ru"
                                 else f"Could not parse the command ({exc}). See /help."))
        if out:
            _print(out)
        return True

    def _split(self, text: str) -> tuple[str, str]:
        head, _, tail = text.partition("|")
        return head.strip(), tail.strip()

    def _members(self) -> list[str]:
        state = self.hub.get("/api/state")
        return [a["name"] for a in state.get("agents", []) if a["status"] != "retired"]

    def _parse_addressee(self, text: str) -> tuple[str | None, str]:
        """Split '@First Last message' using the known member names."""
        return match_addressee(text, self._members())

    def _dispatch(self, line: str) -> str:
        hub = self.hub
        if self.pending_cd is not None:
            return self._answer_cd(line)
        if line.startswith("@"):
            name, text = self._parse_addressee(line)
            if not name or not text:
                return _color("err", "Unknown agent or empty text. /agents shows the team.")
            return hub.act("send_message", to=[name], text=text)
        if not line.startswith("/"):
            if self.chat_with:
                return hub.act("send_message", to=[self.chat_with], text=line)
            return _color("dim", "Use @Name text, /chat Name or /help.")
        cmd, _, arg = line.partition(" ")
        arg = arg.strip()
        if cmd == "/help":
            return HELP[self.lang]
        if cmd == "/board":
            return hub.act("board", include_closed=bool(arg))
        if cmd == "/agents":
            return hub.act("agents")
        if cmd == "/status":
            return hub.act("swarm_status")
        if cmd == "/jobs":
            return self._jobs(arg)
        if cmd == "/show" and arg:
            task = hub.act("task_get", task_id=int(arg.lstrip("#")))
            return task + "\n" + hub.act("task_discussion", task_id=int(arg.lstrip("#")))
        if cmd == "/comment":
            num, _, text = arg.partition(" ")
            return hub.act("task_comment", task_id=int(num.lstrip("#")), text=text)
        if cmd == "/goal" and arg:
            title, desc = self._split(arg)
            return hub.act("set_goal", title=title, description=desc)
        if cmd == "/task" and arg:
            title, goal = self._split(arg)
            return hub.act("task_create", title=title, goal=goal or title)
        if cmd == "/msg" and arg:
            name, text = self._parse_addressee(arg)
            if not name and ":" in arg:  # unknown to us: let the hub resolve it (and suggest names)
                name, _, text = arg.partition(":")
                name, text = name.strip(), text.strip()
            if not name or not text:
                return _color("err", "Unknown agent or empty text. /agents shows the team.")
            return hub.act("send_message", to=[name], text=text)
        if cmd == "/all" and arg:
            return hub.act("send_message", to=["all"], text=arg)
        if cmd == "/chat":
            if not arg:
                self.chat_with = None
                return "Chat mode off."
            name, _ = self._parse_addressee(arg)
            if not name:
                return _color("err", "Unknown agent.")
            self.chat_with = name
            return f"Chatting with {name}. Plain text goes to them."
        if cmd == "/hire" and arg:
            role, instructions = self._split(arg)
            # instructions stay a required parameter of the tool: "" is fine for a catalog role
            return hub.act("hire_agent", role=role, instructions=instructions)
        if cmd == "/pause":
            return hub.act("swarm_control", action="pause")
        if cmd == "/resume":
            return hub.act("swarm_control", action="resume")
        if cmd == "/budget" and arg:
            return hub.act("swarm_control", max_cost_usd=float(arg.replace("$", "")))
        if cmd == "/max" and arg:
            return hub.act("swarm_control", max_agents=int(arg))
        if cmd in ("/approve", "/reject") and arg:
            head, _, rest = arg.strip().partition(" ")
            if re.fullmatch(r"#?\d+", head):  # a task in review (MCP server names start with a letter)
                # the comment is free text, taken as typed ("don't", quotes and spacing kept), as in the TUI
                comment = _unquote(rest.strip())
                if cmd == "/reject" and not comment:
                    usage = "/reject N <what to fix>" if self.lang == "en" else "/reject N <что исправить>"
                    return _color("err", usage)
                return hub.act("task_review", task_id=int(head.lstrip("#")), approve=cmd == "/approve",
                               comment=comment)
            parts = shlex.split(arg)
            return hub.act("mcp_approve", name=parts[0], approve=cmd == "/approve", comment=" ".join(parts[1:]))
        if cmd == "/trace":
            if arg.lstrip("#").isdigit():
                return hub.act("agent_traces", trace_id=int(arg.lstrip("#")))
            if arg:
                name, _ = self._parse_addressee(arg)
                return hub.act("agent_traces", agent=name or arg)
            return hub.act("agent_traces")
        if cmd == "/span" and arg:
            num, _, rest = arg.partition(" ")
            if not num.lstrip("#").isdigit():
                return _color("err", "/span N [ctx]")
            if rest.strip().lower() in ("ctx", "context", "контекст"):
                return hub.act("agent_traces", span_id=int(num.lstrip("#")), context=True)
            return hub.act("agent_traces", span_id=int(num.lstrip("#")))
        if cmd == "/session":
            return self._session()
        if cmd == "/cd":
            return self._cd(arg)
        return _color("err", "Unknown command. /help")

    def _jobs(self, arg: str) -> str:
        """`/jobs`: the agents' background jobs; `/jobs jN`: one job's state and the end of its output;
        `/jobs stop jN [jM …]`: stop them (as the user: any member's job)."""
        words = arg.split()
        if not words:
            return self.hub.act("jobs")
        if words[0].casefold() in JOB_STOP:
            if len(words) < 2:
                return _color("err", "/jobs stop jN")
            return "\n".join(self.hub.act("job_stop", job_id=ref) for ref in words[1:])
        return self.hub.act("jobs", job_id=words[0])

    # ---- /cd: the session's folder ---------------------------------------------------------
    def _t(self, key: str, **kw: Any) -> str:
        return CD_TEXTS[self.lang][key].format(**kw)

    def _cd(self, arg: str) -> str:
        """`/cd` shows the session folder; `/cd PATH` (or `--default`) asks y/N, then moves the session there
        or adopts the folder (POST /api/sessions/{sid}/workspace); `--yes` skips the question."""
        if self.launcher is None or not self.session_id:
            return _color("err", self._t("launcher_only"))
        text, yes = arg.strip(), False
        for flag in _CD_FLAGS:  # at either end: the path in between may hold spaces
            if text == flag:
                text, yes = "", True
            elif text.startswith(flag + " "):
                text, yes = text[len(flag):].strip(), True
            elif text.endswith(" " + flag):
                text, yes = text[:-len(flag)].strip(), True
        sid = urllib.parse.quote(self.session_id, safe="")
        summary = self.launcher.get(f"/api/sessions/{sid}").get("session") or {}
        current = summary.get("workspace") if isinstance(summary.get("workspace"), dict) else {}
        title = clean(summary.get("title") or self.session_id)
        if not text:
            lines = [self._t("current", path=clean(current.get("path") or "?"), kind=self._t(folder_kind(current)))]
            if not current.get("default", True) and current.get("exists") is False:
                lines.append(_color("err", self._t("missing", path=clean(current.get("path")))))
            lines.append(_color("dim", self._t("hint")))
            return "\n".join(lines)
        if text == "--default":
            target = None
        else:
            target = resolve_folder(text, local=self.local)
            if target is None:
                return _color("err", self._t("remote_relative"))
        default_folder = os.path.join(str(summary.get("dir") or ""), "workspace")
        shown = clean(default_folder if target is None else target)
        state, key = cd_preview(current, target, default_folder, local=self.local)
        if state == "file":
            return _color("err", self._t("file", path=shown))
        expect = cd_expect(state)
        if state == "same" or yes:
            return self._move(target, title, expect)
        question = self._t(key or "text_unknown", old=clean(current.get("path") or "?"))
        if summary.get("status") == "running":
            question += " " + self._t("text_running")
        if state in ("files", "unknown"):  # adopting the user's folder (maybe): the question comes last
            question += " " + self._t("continue")
        self.pending_cd = (target, title, expect)
        return self._t("ask_default" if target is None else "ask", title=title, path=shown, text=question)

    def _answer_cd(self, line: str) -> str:
        """The line after a `/cd` question: yes moves; no (or Enter) cancels; anything else cancels and is
        handled as usual."""
        target, title, expect = self.pending_cd or (None, "", None)
        self.pending_cd = None
        answer = line.strip().casefold()
        if answer in YES_WORDS:
            return self._move(target, title, expect)
        cancelled = _color("dim", self._t("cancelled"))
        if answer in NO_WORDS:
            return cancelled
        _print(cancelled)
        return self._dispatch(line)

    def _move(self, target: str | None, title: str, expect: str | None = None) -> str:
        sid = urllib.parse.quote(self.session_id or "", safe="")
        body: dict[str, Any] = {"default": True} if target is None else {"path": target}
        if expect:  # what the user agreed to: the hub refuses (409) when the folder changed since
            body["expect"] = expect
        try:
            answer = self.launcher.post(f"/api/sessions/{sid}/workspace", body)  # type: ignore[union-attr]
        except urllib.error.HTTPError as exc:
            return _color("err", _http_error(exc))
        lines = []
        for key, fields in cd_result_lines(answer.get("result") or {}, title):
            text = self._t(key, **{k: clean(v) for k, v in fields.items()})
            lines.append(_color("err", text) if key == "start_error" else text)
        return "\n".join(lines)

    def _session(self) -> str:
        session = (self.hub.get("/api/state").get("swarm") or {}).get("session")
        ru = self.lang == "ru"
        if not session:  # `dreamteam run`: a single swarm, not a launcher session
            return "Это отдельный рой (dreamteam run), не сессия." if ru else "A single swarm (dreamteam run), not a session."
        sid, title, status = (clean(session.get(k) or "") for k in ("id", "title", "status"))
        if ru:
            status = {"running": "работает", "stopped": "остановлена"}.get(status, status)
            return (f"Сессия {sid}: {title} [{status}]\n"
                    "Другая сессия: dreamteam console --session ID (список: dreamteam sessions).")
        return (f"Session {sid}: {title} [{status}]\n"
                "Another session: dreamteam console --session ID (list: dreamteam sessions).")

    # ---- main loop -------------------------------------------------------------------------
    def run(self) -> None:
        for _ in range(40):
            try:
                self._prime()
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.25)
        _print(HELP[self.lang])
        poller = threading.Thread(target=self._poll, daemon=True)
        poller.start()
        try:
            while not self.stop.is_set():
                try:
                    prompt = f"[{self.chat_with}]> " if self.chat_with else "> "
                    if self.pending_cd is not None:  # the answer to a /cd question
                        prompt = "[y/N]> "
                    line = input(prompt)
                except EOFError:
                    break
                if self.stop.is_set() or not self.handle(line):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()


def run_console(base_url: str, lang: str = "ru", stop: threading.Event | None = None,
                local: bool | None = None) -> None:
    """Interactive console; returns on /quit, EOF or Ctrl+C, or after `stop` is set (checked between lines).
    `local`: the hub runs on this computer (None: decided by the address)."""
    Console(base_url, lang, stop, local).run()
