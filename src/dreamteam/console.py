"""Terminal chat with the swarm, talking to a running hub over its REST API (stdlib only)."""

from __future__ import annotations

import json
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
  /show N                  — задача N с обсуждением
  /comment N текст         — написать в обсуждение задачи N
  /hire Роль | инструкции  — нанять агента
  /pause  /resume          — пауза / продолжить
  /budget 10               — лимит бюджета в $;  /max 6 — максимум агентов
  /approve имя  /reject имя [комментарий] — одобрить или отклонить MCP-сервер
  /trace [Имя | N]         — трейсы агентов: последние, агента или трейс N по шагам
  /span N [ctx]            — шаг трейса полностью (ctx — сообщения, которые видела модель)
  /session                 — текущая сессия (другая: dreamteam console --session ID)
  /help  /quit""",
    "en": """Commands:
  @First Last text         — message an agent (or /msg First Last: text)
  /chat First Last         — plain text then goes to this agent (/chat alone to leave)
  /all text                — message every agent
  /goal Title | description — set the global goal
  /task Title | goal       — create a task
  /board  /agents  /status — board, team, status
  /show N                  — task N with its discussion
  /comment N text          — post to the discussion of task N
  /hire Role | instructions — hire an agent
  /pause  /resume          — pause / resume
  /budget 10               — budget limit in $;  /max 6 — max agents
  /approve name  /reject name [comment] — approve or reject an MCP server
  /trace [Name | N]        — agent traces: recent ones, an agent's, or trace N step by step
  /span N [ctx]            — one trace step in full (ctx — the messages the model saw)
  /session                 — the current session (another: dreamteam console --session ID)
  /help  /quit""",
}

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


def _print(text: str) -> None:
    sys.stdout.write("\r" + text + "\n")
    sys.stdout.flush()


class Console:
    def __init__(self, base_url: str, lang: str = "ru", stop: threading.Event | None = None):
        self.hub = Hub(base_url)
        self.lang = lang if lang in HELP else "en"
        self.chat_with: str | None = None
        self.since_message = 0
        self.since_note = 0
        self.stop = stop or threading.Event()

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
        if not line:
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
            return hub.act("hire_agent", role=role, instructions=instructions or role)
        if cmd == "/pause":
            return hub.act("swarm_control", action="pause")
        if cmd == "/resume":
            return hub.act("swarm_control", action="resume")
        if cmd == "/budget" and arg:
            return hub.act("swarm_control", max_cost_usd=float(arg.replace("$", "")))
        if cmd == "/max" and arg:
            return hub.act("swarm_control", max_agents=int(arg))
        if cmd in ("/approve", "/reject") and arg:
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
        return _color("err", "Unknown command. /help")

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
                    line = input(prompt)
                except EOFError:
                    break
                if self.stop.is_set() or not self.handle(line):
                    break
        except KeyboardInterrupt:
            pass
        finally:
            self.stop.set()


def run_console(base_url: str, lang: str = "ru", stop: threading.Event | None = None) -> None:
    """Interactive console; returns on /quit, EOF or Ctrl+C, or after `stop` is set (checked between lines)."""
    Console(base_url, lang, stop).run()
