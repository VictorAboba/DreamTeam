"""Sessions of a DreamTeam home (docs/SPEC.md §17.2).

A session is one independent swarm: its own goal, board, members, messages, workspace, MCP servers and
traces, stored in `<home>/sessions/<id>/` (`session.json`, `.dreamteam/swarm.db`, `workspace/`). Its
configuration is the home's global settings deep-merged with the session's `overrides`.
A home that was used as a single-swarm project before sessions existed (`<home>/.dreamteam/swarm.db`)
shows up as the session `main` that lives in the home directory itself.

The manager keeps at most one `Swarm` object per session: *running* sessions are started (agents work);
a stopped session can be *loaded* without starting, so the web UI can show its board and traces.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import sys
import time
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from .config import Config
from .db import loads
from .models import USER
from .settings import (
    apply_update, deep_merge, ensure_home, profile_renames, public_view, read_raw, settings_path, validate, write_raw,
)

if TYPE_CHECKING:
    from .swarm import Swarm

log = logging.getLogger(__name__)

SESSIONS_DIR = "sessions"
SESSION_FILE = "session.json"
LEGACY_ID = "main"
OVERRIDE_SECTIONS = ("swarm", "agents", "shell", "workspace", "mcp", "tracing")
SESSION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")  # \Z: "$" would accept a trailing newline
TITLE_LIMIT = 200
TRASH_PREFIX = ".deleted-"  # a session directory being deleted (never a valid session id)

_TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґ",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
     "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya", "i", "yi", "ye", "g"],
))


class SessionError(ValueError):
    """A session operation that cannot be done; `status` is the matching HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def slugify(title: str, limit: int = 40) -> str:
    text = "".join(_TRANSLIT.get(ch, ch) for ch in (title or "").lower())
    # "Café Größe" -> "cafe grosse": accents dropped after the Cyrillic table (NFKD would split "й")
    text = unicodedata.normalize("NFKD", text.replace("ß", "ss"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:limit].strip("-")


def _text(value: Any) -> str:
    """User text that can be stored as UTF-8 (a lone surrogate from JSON becomes '?')."""
    return str(value or "").encode("utf-8", "replace").decode("utf-8")


def _one_line(value: Any) -> str:
    return " ".join(_text(value).split())


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _rmtree(path: Path) -> None:
    def on_error(func, target, _exc):  # git objects are read-only on Windows
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=on_error)
    else:
        shutil.rmtree(path, onerror=lambda func, target, info: on_error(func, target, info[1]))


def read_stats(db_path: Path, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Board/agent/budget figures of a session database (read-only; zeros when it does not exist)."""
    stats: dict[str, Any] = {
        "goal": None, "tasks": {"total": 0, "done": 0, "open": 0, "by_status": {}},
        "agents": {"total": 0, "working": 0, "llm": 0, "external": 0}, "spent_usd": 0.0,
        "limits": {}, "paused": False, "pause_reason": "", "last_activity": None,
    }
    own = conn is None
    if own:
        if not db_path.exists():
            return stats
        try:
            conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
        except sqlite3.Error:
            return stats
    try:
        def kv(key: str) -> Any:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            return loads(row[0]) if row else None

        goal = kv("goal")
        if isinstance(goal, dict):
            stats["goal"] = {"title": goal.get("title", ""), "description": goal.get("description", ""),
                             "status": goal.get("status", "active"), "summary": goal.get("summary", "")}
        by_status = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")}
        total = sum(by_status.values())
        done = by_status.get("done", 0)
        closed = done + by_status.get("cancelled", 0)
        stats["tasks"] = {"total": total, "done": done, "open": total - closed, "by_status": by_status}
        for kind, status, count in conn.execute(
                "SELECT kind, status, COUNT(*) FROM agents WHERE status != 'retired' GROUP BY kind, status"):
            stats["agents"]["total"] += count
            stats["agents"][kind] = stats["agents"].get(kind, 0) + count
            if status == "working":
                stats["agents"]["working"] += count
        stats["spent_usd"] = float(conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM agents").fetchone()[0] or 0)
        limits = kv("limits")
        stats["limits"] = limits if isinstance(limits, dict) else {}
        paused = kv("paused")
        paused = paused if isinstance(paused, dict) else {}
        stats["paused"] = bool(paused.get("paused"))
        stats["pause_reason"] = paused.get("reason", "") or ""
        row = conn.execute("SELECT ts FROM events ORDER BY id DESC LIMIT 1").fetchone()
        stats["last_activity"] = row[0] if row else None
    except sqlite3.Error as exc:  # a database of an older version or locked: show what we have
        log.debug("cannot read stats of %s: %s", db_path, exc)
    finally:
        if own:
            conn.close()
    return stats


class SessionManager:
    def __init__(self, home: str | Path, *, llm_factory: Callable | None = None,
                 language: str = "ru", provider: str = "anthropic"):
        self.home = ensure_home(home, language, provider)
        self.llm_factory = llm_factory
        try:
            self.raw: dict[str, Any] = read_raw(self.home)
            self.config: Config = validate(self.home, self.raw)
        except ValueError as exc:
            raise ValueError(f"Invalid settings in {settings_path(self.home)}: {exc}") from None
        self._stamp = self._settings_stamp()
        self.swarms: dict[str, "Swarm"] = {}   # loaded sessions (running or not)
        self.running: set[str] = set()
        self.hub_url: str | None = None        # base URL of the web server (no trailing /), set by launcher.serve
        self._locks: dict[str, asyncio.Lock] = {}  # kept after a delete: a waiter may still hold the old lock
        self._listeners: list[Callable[[dict[str, Any]], None]] = []

    # ---- change notifications ------------------------------------------------------------------
    def listen(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(listener)

    def unlisten(self, listener: Callable[[dict[str, Any]], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _changed(self, sid: str | None, change: str) -> None:
        event = {"type": "sessions.changed", "session": sid, "change": change, "ts": time.time()}
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                log.exception("session listener failed")

    def _lock(self, sid: str) -> asyncio.Lock:
        return self._locks.setdefault(sid, asyncio.Lock())

    # ---- global settings ----------------------------------------------------------------------
    def _settings_stamp(self) -> tuple[int, int] | None:
        try:
            st = settings_path(self.home).stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    def _refresh_settings(self) -> None:
        """Pick up manual edits of dreamteam.toml made while the server runs, so that a UI save does not
        bring back old values (e.g. an API key changed by hand). An invalid file is ignored (logged)
        until it is fixed: the last valid settings stay in use."""
        stamp = self._settings_stamp()
        if stamp is None or stamp == self._stamp:
            return
        self._stamp = stamp
        try:
            raw = read_raw(self.home)
            config = validate(self.home, raw)
        except (OSError, ValueError) as exc:
            log.warning("ignoring the edited %s: %s", settings_path(self.home), exc)
            return
        self.raw, self.config = raw, config

    def settings_view(self) -> dict[str, Any]:
        self._refresh_settings()
        return public_view(self.raw)

    async def update_settings(self, update: dict[str, Any]) -> dict[str, Any]:
        """Apply a settings update from the UI (settings.apply_update semantics), save it and reconfigure
        the loaded sessions. Returns {"settings": public view, "applied_to": [session ids]}.
        A session whose model profile was renamed follows the rename; one whose profile was deleted falls
        back to the default profile (instead of blocking the save)."""
        if not isinstance(update, dict):
            raise SessionError("The settings must be an object.")
        self._refresh_settings()
        new_raw = apply_update(self.raw, update)
        new_config = validate(self.home, new_raw)
        renames = profile_renames(update)
        fixed: dict[str, dict[str, Any]] = {}
        for sid in self.ids():  # every session must still produce a valid config
            if sid == LEGACY_ID:
                continue
            try:
                meta = self.meta(sid)
            except SessionError:
                continue  # a damaged session shows its error in the list; it must not block the settings
            overrides = original = meta.get("overrides") or {}
            if not isinstance(overrides, dict):
                continue
            agents = overrides.get("agents")
            profile = agents.get("default_model") if isinstance(agents, dict) else None
            if isinstance(profile, str) and profile not in new_config.models:
                overrides = copy.deepcopy(overrides)
                if profile in renames:
                    overrides["agents"]["default_model"] = renames[profile]
                else:
                    del overrides["agents"]["default_model"]
                    if not overrides["agents"]:
                        del overrides["agents"]
                meta["overrides"] = overrides
                fixed[sid] = meta
            try:
                self._config_from(new_raw, sid, overrides)
            except ValueError as exc:
                try:
                    self._config_from(self.raw, sid, original)
                except ValueError:
                    continue  # already invalid before this update: its summary shows config_error
                raise SessionError(f"Session {sid}: {exc}") from None
        try:
            write_raw(self.home, new_raw)
        except OSError as exc:
            raise SessionError(f"Could not save {settings_path(self.home)}: {exc}", 500) from None
        self.raw, self.config = new_raw, new_config
        self._stamp = self._settings_stamp()
        for sid, meta in fixed.items():
            try:
                self._write_meta(sid, meta)
            except OSError:
                log.exception("could not update the overrides of session %s", sid)
        applied = []
        for sid, swarm in list(self.swarms.items()):
            reconfigure = getattr(swarm, "reconfigure", None)
            if reconfigure is None:
                continue
            try:
                result = reconfigure(self.config_for(sid))
                if asyncio.iscoroutine(result):
                    await result
                applied.append(sid)
            except Exception:  # the settings are saved; one session must not fail the whole update
                log.exception("could not apply the new settings to session %s", sid)
        for sid in fixed:
            self._changed(sid, "updated")
        self._changed(None, "settings")
        return {"settings": self.settings_view(), "applied_to": applied}

    @staticmethod
    def _reconfigure_soon(swarm: "Swarm", config: Config) -> None:
        """Hot-apply a config to a loaded swarm from synchronous code (a coroutine result is scheduled)."""
        reconfigure = getattr(swarm, "reconfigure", None)
        if reconfigure is not None:
            result = reconfigure(config)
            if asyncio.iscoroutine(result):
                asyncio.ensure_future(result)

    # ---- registry -----------------------------------------------------------------------------
    @property
    def sessions_dir(self) -> Path:
        return self.home / SESSIONS_DIR

    def _legacy_exists(self) -> bool:
        return (self.home / ".dreamteam" / "swarm.db").exists()

    def ids(self) -> list[str]:
        found = []
        if self.sessions_dir.is_dir():
            for path in self.sessions_dir.iterdir():
                # sessions/main would clash with the legacy session that lives in the home itself
                if (path.name != LEGACY_ID and SESSION_ID.match(path.name) and path.is_dir()
                        and (path / SESSION_FILE).exists()):
                    found.append(path.name)
        if self._legacy_exists() or LEGACY_ID in self.swarms:
            found.append(LEGACY_ID)
        return sorted(set(found))

    def exists(self, sid: str) -> bool:
        if not isinstance(sid, str) or not SESSION_ID.match(sid):
            return False
        if sid == LEGACY_ID:
            return self._legacy_exists() or LEGACY_ID in self.swarms
        path = self.sessions_dir / sid
        return path.is_dir() and (path / SESSION_FILE).exists()

    def _check(self, sid: str) -> None:
        if not self.exists(sid):
            raise SessionError(f"Unknown session '{sid}'.", 404)

    def dir_of(self, sid: str) -> Path:
        return self.home if sid == LEGACY_ID else self.sessions_dir / sid

    def meta(self, sid: str) -> dict[str, Any]:
        if sid == LEGACY_ID:
            path = self.home / ".dreamteam" / SESSION_FILE
            stored: Any = {}
            if path.exists():
                try:
                    stored = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    stored = {}
            stored = stored if isinstance(stored, dict) else {}
            db = self.home / ".dreamteam" / "swarm.db"
            created = stored.get("created_at") or (db.stat().st_ctime if db.exists() else time.time())
            return {"id": LEGACY_ID, "title": stored.get("title") or self.config.swarm.name or "main",
                    "created_at": created, "overrides": {}, "autostart": bool(stored.get("autostart", False)),
                    "legacy": True}
        if not SESSION_ID.match(sid or ""):
            raise SessionError(f"Unknown session '{sid}'.", 404)
        path = self.dir_of(sid) / SESSION_FILE
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SessionError(f"Unknown session '{sid}'.", 404) from None
        except (OSError, ValueError) as exc:
            raise SessionError(f"Session '{sid}' is damaged: {exc}", 500) from None
        if not isinstance(meta, dict):
            raise SessionError(f"Session '{sid}' is damaged: {SESSION_FILE} is not a JSON object.", 500)
        meta.setdefault("overrides", {})
        meta.setdefault("autostart", False)
        meta["id"] = sid
        meta["legacy"] = False
        return meta

    def _write_meta(self, sid: str, meta: dict[str, Any]) -> None:
        if sid == LEGACY_ID:
            path = self.home / ".dreamteam" / SESSION_FILE
            data = {k: meta[k] for k in ("title", "created_at", "autostart") if k in meta}
        else:
            path = self.dir_of(sid) / SESSION_FILE
            data = {k: v for k, v in meta.items() if k not in ("id", "legacy")}
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(data, ensure_ascii=False, indent=2)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_bytes(text.encode("utf-8", "replace"))
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _merged(self, raw: dict[str, Any], overrides: Any) -> dict[str, Any]:
        """The raw settings of a (non-legacy) session: the global settings without a goal (a hand-written
        `swarm.goal` of the home would become every new session's goal) plus its overrides."""
        if not isinstance(overrides, dict):
            raise ValueError("The session overrides must be an object")
        base = raw
        swarm = raw.get("swarm")
        if isinstance(swarm, dict) and ("goal" in swarm or "goal_description" in swarm):
            base = {**raw, "swarm": {k: v for k, v in swarm.items() if k not in ("goal", "goal_description")}}
        return deep_merge(base, overrides)

    def _config_from(self, raw: dict[str, Any], sid: str, overrides: dict[str, Any] | None = None) -> Config:
        if sid == LEGACY_ID:
            return validate(self.home, raw)
        if overrides is None:
            overrides = self.meta(sid).get("overrides") or {}
        return validate(self.dir_of(sid), self._merged(raw, overrides))

    def config_for(self, sid: str) -> Config:
        """The effective configuration of a session (global settings + its overrides)."""
        return self._config_from(self.raw, sid)

    def config_view(self, sid: str) -> dict[str, Any]:
        """settings.public_view of a session's effective settings (no API keys); the global settings when its
        overrides are invalid now (its summary carries config_error), so the UI can still show the session."""
        self._refresh_settings()
        if sid != LEGACY_ID:
            try:
                return public_view(self._merged(self.raw, self.meta(sid).get("overrides") or {}))
            except SessionError:
                raise  # unknown or damaged session
            except ValueError as exc:
                log.debug("session %s: invalid overrides: %s", sid, exc)
        return public_view(self.raw)

    @staticmethod
    def clean_overrides(overrides: dict[str, Any] | None) -> dict[str, Any]:
        if overrides is None:
            return {}
        if not isinstance(overrides, dict):
            raise SessionError("Overrides must be an object of sections.")
        result: dict[str, Any] = {}
        for section, values in overrides.items():
            if section not in OVERRIDE_SECTIONS:
                raise SessionError(f"Sessions cannot override [{section}] (allowed: {', '.join(OVERRIDE_SECTIONS)}).")
            if not isinstance(values, dict):
                raise SessionError(f"Override [{section}] must be an object.")
            clean = {k: copy.deepcopy(v) for k, v in values.items() if v is not None and v != ""}
            if section == "swarm":
                clean.pop("goal", None)
                clean.pop("goal_description", None)
            if clean:
                result[section] = clean
        return result

    def _validated_overrides(self, overrides: Any) -> dict[str, Any]:
        clean = self.clean_overrides(overrides)
        try:
            validate(self.sessions_dir / "new", self._merged(self.raw, clean))
        except ValueError as exc:
            raise SessionError(f"Invalid session settings: {exc}") from None
        return clean

    # ---- summaries ----------------------------------------------------------------------------
    def summary(self, sid: str) -> dict[str, Any]:
        self._check(sid)
        meta = self.meta(sid)
        try:
            cfg = self._config_from(self.raw, sid, meta.get("overrides") or {})
            config_error = None
        except ValueError as exc:
            cfg, config_error = self.config, str(exc)
        swarm = self.swarms.get(sid)
        db_path = cfg.data_dir / "swarm.db" if config_error is None else self.dir_of(sid) / ".dreamteam" / "swarm.db"
        if swarm is not None:
            with swarm.db._lock:
                stats = read_stats(db_path, swarm.db.conn)
        else:
            stats = read_stats(db_path)
        limits = stats.pop("limits") or {}
        pending = meta.get("pending_goal")
        if stats["goal"] is None and isinstance(pending, dict):
            stats["goal"] = {"title": pending.get("title", ""), "description": pending.get("description", ""),
                             "status": "pending", "summary": ""}
        profile = cfg.agents.default_model
        model = cfg.models.get(profile)
        return {
            "id": sid, "title": meta.get("title") or sid, "legacy": meta.get("legacy", False),
            "dir": str(self.dir_of(sid)), "created_at": meta.get("created_at"),
            "status": "running" if sid in self.running else "stopped",
            "loaded": swarm is not None, "autostart": bool(meta.get("autostart")),
            "language": cfg.swarm.language, "model_profile": profile, "model": model.model if model else None,
            "max_cost_usd": _number(limits.get("max_cost_usd", cfg.swarm.max_cost_usd)),
            "max_agents": int(_number(limits.get("max_agents", cfg.swarm.max_agents))),
            "overrides": meta.get("overrides") or {}, "config_error": config_error,
            "url": f"/s/{sid}/", **stats,
        }

    def list(self) -> list[dict[str, Any]]:
        items = []
        for sid in self.ids():
            try:
                items.append(self.summary(sid))
            except SessionError as exc:
                if exc.status != 404:  # 404: deleted meanwhile
                    items.append({"id": sid, "title": sid, "status": "stopped", "error": str(exc)})
            except Exception as exc:  # one broken session must not break the sessions page
                log.exception("cannot summarize session %s", sid)
                items.append({"id": sid, "title": sid, "status": "stopped", "error": f"{type(exc).__name__}: {exc}"})
        items.sort(key=lambda s: (s.get("status") != "running",
                                  -_number(s.get("last_activity") or s.get("created_at"))))
        return items

    def default_session(self) -> str | None:
        """The session MCP calls without `session` go to: the only running one, else the most recently
        active running one, else the only session."""
        running = [s for s in self.list() if s.get("status") == "running"]
        if running:
            return running[0]["id"]
        ids = self.ids()
        return ids[0] if len(ids) == 1 else None

    # ---- lifecycle ----------------------------------------------------------------------------
    def _new_id(self, title: str) -> str:
        numbers = [int(m.group(1)) for p in (self.sessions_dir.iterdir() if self.sessions_dir.is_dir() else [])
                   if (m := re.match(r"^(\d+)", p.name))]
        number = max(numbers, default=0) + 1
        slug = slugify(title)
        sid = f"{number:03d}-{slug}" if slug else f"{number:03d}"
        return sid[:64].rstrip("-")

    async def create(self, title: str, goal: str = "", description: str = "", *,
                     overrides: dict[str, Any] | None = None, start: bool = True, actor: str = USER) -> dict[str, Any]:
        """Create a session (and start it with the goal unless start=False). Returns its summary."""
        title = (_one_line(title) or _one_line(goal)[:60] or "Session")[:TITLE_LIMIT].strip()
        goal, description = _text(goal).strip(), _text(description).strip()
        self._refresh_settings()
        overrides = self._validated_overrides(overrides)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        for _attempt in range(100):  # another process (a second launcher, the CLI) may take the same id
            sid = self._new_id(title)
            path = self.dir_of(sid)
            try:
                path.mkdir(exist_ok=False)
                break
            except FileExistsError:
                continue
        else:
            raise SessionError("Could not choose a session id.", 500)
        (path / "workspace").mkdir(exist_ok=True)
        meta: dict[str, Any] = {"title": title, "created_at": time.time(), "overrides": overrides,
                                "autostart": False}
        if goal:  # set at the first start, when the members exist
            meta["pending_goal"] = {"title": goal, "description": description, "actor": actor}
        self._write_meta(sid, meta)
        self._changed(sid, "created")
        if start:
            await self.start(sid)
        return self.summary(sid)

    def _load_locked(self, sid: str) -> "Swarm":
        """The session's Swarm, constructed if needed. The caller holds the session's lock."""
        swarm = self.swarms.get(sid)
        if swarm is not None:
            return swarm
        self._check(sid)  # deleted while this call waited for the lock: do not recreate its directory
        try:
            cfg = self.config_for(sid)
        except SessionError:
            raise
        except ValueError as exc:
            raise SessionError(f"Session {sid} has invalid settings: {exc}") from None
        from .swarm import Swarm

        try:
            swarm = Swarm(config=cfg, llm_factory=self.llm_factory)
        except Exception as exc:  # e.g. a locked or corrupt database
            log.exception("session %s cannot be opened", sid)
            raise SessionError(f"Session {sid} cannot be opened: {type(exc).__name__}: {exc}", 500) from None
        try:  # activations a crashed process left "running" (start() does it too; this covers viewing only)
            swarm.tracer.recover()
        except Exception:
            log.exception("session %s: could not close its interrupted traces", sid)
        swarm.session_id = sid
        swarm.session_title = self.meta(sid).get("title") or sid
        if self.hub_url:
            swarm.hub_url = f"{self.hub_url}/s/{sid}/mcp"
        self.swarms[sid] = swarm
        return swarm

    async def load(self, sid: str) -> "Swarm":
        """The session's Swarm, constructed (not started) if it is not loaded yet."""
        self._check(sid)
        swarm = self.swarms.get(sid)
        if swarm is not None:
            return swarm
        async with self._lock(sid):
            return self._load_locked(sid)

    async def start(self, sid: str) -> "Swarm":
        self._check(sid)
        async with self._lock(sid):
            # loaded under the lock: a swarm fetched before waiting could have been unloaded meanwhile
            swarm = self._load_locked(sid)
            if sid not in self.running:
                try:
                    await swarm.start()
                except Exception:
                    log.exception("session %s failed to start", sid)
                    try:
                        await swarm.stop()  # whatever started before the failure
                    except Exception:
                        log.exception("cleanup after the failed start of %s", sid)
                    await self._unload(sid)
                    raise
                self.running.add(sid)
                meta = self.meta(sid)
                meta["autostart"] = True
                pending = meta.get("pending_goal")
                if not isinstance(pending, dict) or not str(pending.get("title") or "").strip():
                    meta.pop("pending_goal", None)
                elif not swarm.goal():
                    try:  # the goal first, then forget it: a failure keeps it for the next start
                        swarm.set_goal(pending.get("actor") or USER, str(pending["title"]),
                                       str(pending.get("description") or ""))
                        meta.pop("pending_goal", None)
                    except Exception:
                        log.exception("could not set the goal of session %s", sid)
                else:
                    meta.pop("pending_goal", None)
                self._write_meta(sid, meta)
        self._changed(sid, "started")
        return swarm

    async def stop(self, sid: str, *, remember: bool = False) -> None:
        """Stop the session's agents and unload it. remember=True keeps it marked for resume() (used at
        server shutdown); an explicit stop clears the mark."""
        self._check(sid)
        async with self._lock(sid):
            try:
                await self._unload(sid)
            finally:
                if not remember and self.exists(sid):
                    meta = self.meta(sid)
                    meta["autostart"] = False
                    self._write_meta(sid, meta)
            self._check(sid)  # deleted while this call waited for the lock
        self._changed(sid, "stopped")

    async def _unload(self, sid: str) -> None:
        swarm = self.swarms.pop(sid, None)
        was_running = sid in self.running
        self.running.discard(sid)
        if swarm is None:
            return
        try:
            if was_running:
                await swarm.stop()
        finally:
            swarm.close()

    async def unload_idle(self, sid: str) -> None:
        """Drop a loaded but not running session (frees its database connection)."""
        if sid in self.swarms and sid not in self.running:
            async with self._lock(sid):
                if sid not in self.running:
                    await self._unload(sid)

    def update(self, sid: str, *, title: str | None = None, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """Rename a session or replace its overrides (applied to the loaded swarm at once, like the global
        settings; the language and the start settings matter only at the first start)."""
        self._check(sid)
        meta = self.meta(sid)
        if title is not None:
            if not isinstance(title, str):
                raise SessionError("The title must be a string.")
            title = _one_line(title)[:TITLE_LIMIT].strip()
            if not title:
                raise SessionError("The title cannot be empty.")
            meta["title"] = title
        config = None
        if overrides is not None:
            if sid == LEGACY_ID:
                raise SessionError("The main session uses the global settings directly; edit them instead.")
            clean = self.clean_overrides(overrides)
            try:
                config = self._config_from(self.raw, sid, clean)
            except SessionError:
                raise
            except ValueError as exc:
                raise SessionError(f"Invalid session settings: {exc}") from None
            meta["overrides"] = clean
        self._write_meta(sid, meta)
        swarm = self.swarms.get(sid)
        if swarm is not None:
            if title is not None:
                swarm.session_title = title
            if config is not None:
                try:
                    self._reconfigure_soon(swarm, config)
                except Exception:
                    log.exception("could not apply the new overrides to session %s", sid)
        self._changed(sid, "updated")
        return self.summary(sid)

    async def delete(self, sid: str) -> None:
        """Stop the session and delete its directory (board, messages, workspace, traces). The directory is
        first renamed away, so a session whose files are in use stays intact (409) instead of being
        half-deleted."""
        self._check(sid)
        if sid == LEGACY_ID:
            raise SessionError("The main session lives in the home directory itself and cannot be deleted "
                               "from the UI; delete its .dreamteam folder by hand.", 409)
        async with self._lock(sid):
            self._check(sid)  # a concurrent delete finished first
            try:
                await self._unload(sid)
            except Exception:  # unloaded and closed anyway (_unload closes in `finally`)
                log.exception("error stopping session %s before deleting it", sid)
            path = self.dir_of(sid)
            trash = self.sessions_dir / f"{TRASH_PREFIX}{sid}-{time.time_ns()}"
            for attempt in range(10):
                try:
                    os.rename(path, trash)
                    break
                except FileNotFoundError:
                    raise SessionError(f"Unknown session '{sid}'.", 404) from None
                except OSError as exc:  # Windows: a file inside is open (an editor, an antivirus scan...)
                    if attempt == 9:
                        raise SessionError(f"Could not delete session {sid}: its files are in use ({exc}). Close "
                                           "the programs that use them and try again.", 409) from None
                    await asyncio.sleep(0.2)
        self._changed(sid, "deleted")
        await asyncio.to_thread(self._purge_trash)

    def _purge_trash(self) -> None:
        """Remove renamed-away session directories (also ones a previous delete could not finish)."""
        if not self.sessions_dir.is_dir():
            return
        for path in self.sessions_dir.iterdir():
            if path.name.startswith(TRASH_PREFIX) and path.is_dir():
                _rmtree(path)
                if path.exists():
                    log.warning("could not remove %s completely; it is retried at the next delete", path)

    async def resume(self) -> list[str]:
        """Start the sessions that were running when the server last stopped."""
        started = []
        for sid in self.ids():
            try:
                if self.meta(sid).get("autostart"):
                    await self.start(sid)
                    started.append(sid)
            except Exception:
                log.exception("could not resume session %s", sid)
        return started

    async def shutdown(self) -> None:
        """Stop every loaded session, keeping the running ones marked for resume()."""
        for sid in list(self.swarms):
            try:
                async with self._lock(sid):
                    await self._unload(sid)
            except Exception:
                log.exception("error stopping session %s", sid)

    def copy_overrides(self, sid: str) -> dict[str, Any]:
        return copy.deepcopy(self.meta(sid).get("overrides") or {})
