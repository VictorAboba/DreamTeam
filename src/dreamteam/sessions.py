"""Sessions of a DreamTeam home (docs/SPEC.md §17.2).

A session is one independent swarm: its own goal, board, members, messages, workspace, MCP servers and
traces, stored in `<home>/sessions/<id>/` (`session.json`, `.dreamteam/swarm.db`, `workspace/`). Its
configuration is the home's global settings deep-merged with the session's `overrides`.
A home that was used as a single-swarm project before sessions existed (`<home>/.dreamteam/swarm.db`)
shows up as the session `main` that lives in the home directory itself.

A session can work in another folder than its own `workspace/` (session.json "workspace"): an empty one gets
the session's files moved into it, a folder that already holds files (the user's project) is adopted as it is
(see `SessionManager.set_workspace`).

The manager keeps at most one `Swarm` object per session: *running* sessions are started (agents work);
a stopped session can be *loaded* without starting, so the web UI can show its board and traces.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import errno
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable

from . import skills as skill_lib
from .config import Config
from .db import loads
from .goal_text import shorten_title, split_goal
from .models import USER, Status, ToolError
from .settings import (
    apply_update, deep_merge, ensure_home, profile_renames, public_view, read_raw, settings_path, validate, write_raw,
)
from .skills import Library, Limits, SkillError, SkillInfo, Staged
from .swarm_skills import attach_files, decide_files, read_session_skills, skills_dir, source_spec, stopped_public
from .workspace import (SCRATCH_DIR, SKILLS_MIRROR, commit_paths, init_repo, is_link, looks_like_git_dir, repo_state,
                        rmtree, tracked)

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
WORKSPACE_DIR = "workspace"  # a session's own (default) workspace folder inside its directory
MOVE_JOURNAL = "workspace-move.json"  # in the session's .dreamteam/ while its workspace moves (kept if it failed)
LEGACY_WORKSPACE_TEXT = (
    "The main session lives in the home directory itself, and `dreamteam run` can still open that directory as a "
    "single-swarm project, which would not know about another folder: its workspace cannot be moved. Create a new "
    "session in the other folder instead.")

_TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґ",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
     "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya", "i", "yi", "ye", "g"],
))


class SessionError(ValueError):
    """A session operation that cannot be done; `status` is the matching HTTP status, `extra` goes into the JSON
    answer (`exists`: skill names that clash, `pending_skills`: the proposals a delete would lose)."""

    def __init__(self, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.status = status
        self.extra = extra


def _session_error(exc: SkillError | ToolError) -> SessionError:
    """A skills error (SkillError, or a swarm_skills ToolError with its status) as a SessionError."""
    return SessionError(str(exc), getattr(exc, "status", 400), **(getattr(exc, "extra", None) or {}))


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


# workspace.rmtree: the extended-length form on Windows, since a renamed-away session (.deleted-<id>-<ns>) is longer
# than the original and its deepest files (git objects, read-only there) may pass the 260-character limit
_rmtree = rmtree


# ---- workspace folders ---------------------------------------------------------------------------------
def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _same_path(a: Path, b: Path) -> bool:
    return _path_key(a) == _path_key(b)


def _within(path: Path, parent: Path) -> bool:
    """`path` is `parent` or inside it (both resolved; case-insensitive where the file system is)."""
    p, q = _path_key(path), _path_key(parent)
    return p == q or p.startswith(q if q.endswith(os.sep) else q + os.sep)


def _remove(path: Path) -> None:
    """Delete a file or a directory tree; OSError when something is left."""
    if path.is_dir() and not path.is_symlink():
        _rmtree(path)
    elif os.path.lexists(path):
        try:
            path.unlink()
        except PermissionError:  # a read-only file on Windows
            if is_link(path):  # chmod would follow the link and change the mode of what it points to
                raise
            os.chmod(path, stat.S_IWRITE)
            path.unlink()
    if os.path.lexists(path):
        raise OSError(f"could not remove {path} completely (a file in it is in use?)")


def _drop_mirror(folder: Path) -> None:
    """Remove the agents' skills mirror `<folder>/.scratch/_skills` (Swarm.start rebuilds it from the session's
    copies). A link there is never followed: a junction goes with rmdir, a symlink with unlink."""
    mirror = Path(folder, *SKILLS_MIRROR.split("/"))
    if is_link(mirror):
        try:
            os.rmdir(mirror)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(mirror)
    elif mirror.is_dir():
        _rmtree(mirror)


def _cross_device(exc: OSError) -> bool:
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == 17  # ERROR_NOT_SAME_DEVICE


def _move_path(src: Path, dst: Path, done: list[tuple[Path, Path, bool]] | None = None) -> None:
    """Move one file or directory: a rename on the same volume, else a copy and a delete (a failed copy is
    removed again, so the original stays the only one). A `dst` that exists is never touched (FileExistsError,
    nothing moved). `done` gets (src, dst, copied) as soon as `dst` holds the entry — after the rename, or after
    the copy and before the original is deleted — so an undo puts back only what this move made."""
    if os.path.lexists(dst):  # (os.rename would replace a file there on POSIX; a copy would merge into it)
        raise FileExistsError(errno.EEXIST, "a file or folder of that name is already there", str(dst))
    try:
        os.rename(src, dst)
    except OSError as exc:
        if not _cross_device(exc):
            raise
    else:
        if done is not None:
            done.append((src, dst, False))
        return
    try:
        if src.is_dir() and not src.is_symlink():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst, follow_symlinks=False)
    except BaseException:
        if os.path.lexists(dst):  # the partial copy (dst did not exist before)
            try:
                _remove(dst)
            except OSError:
                log.warning("could not remove the partial copy %s", dst)
        raise
    if done is not None:
        done.append((src, dst, True))
    _remove(src)


def _move_back(moved: Path, original: Path, copied: bool = False) -> None:
    """Undo `_move_path(original, moved)`. `copied`: it was a copy and a delete, and the original may have been
    only partly deleted (the rest is put back from the copy). After a rename, something at `original` is not this
    move's: it is left alone (FileExistsError: the moved entry stays where it is, reported)."""
    if not os.path.lexists(moved):
        return  # it never arrived: the original is intact
    if not os.path.lexists(original):
        _move_path(moved, original)
        return
    if not copied:
        raise FileExistsError(errno.EEXIST, "something else is there now", str(original))
    if moved.is_dir() and not moved.is_symlink():
        shutil.copytree(moved, original, symlinks=True, dirs_exist_ok=True)
    _remove(moved)  # (a file's original is still whole: it is deleted at once or not at all)


def _check_writable(folder: Path, shown: Path) -> None:
    try:
        fd, probe = tempfile.mkstemp(prefix=".dreamteam-probe-", dir=folder)
        os.close(fd)
        os.unlink(probe)
    except OSError as exc:
        raise SessionError(f"Cannot write to {shown}: {exc.strerror or exc}. Choose a folder you can write to.") \
            from None


@dataclass
class _Plan:
    """What changing a session's workspace will do, decided (and checked) before anything changes."""
    target: Path                 # the new folder (resolved)
    old: Path                    # the current folder
    mode: str                    # "moved" | "adopted" | "unchanged"
    default: bool                # the target is the session's own workspace/ folder
    adopted: bool                # stored as "adopted": the folder was the user's (its repo config is never touched)
    git: str                     # "moved" | "existing" | "initialized" | "disabled" | "none"
    move_all: bool = False       # move every entry of `old` (else only the MCP servers' folders)
    mcp: list[str] = field(default_factory=list)  # forged/imported MCP servers whose folders go along
    old_exists: bool = True
    old_is_default: bool = False
    old_adopted: bool = False    # leaving a folder of the user: the skills mirror there goes (docs/SPEC.md §19)
    # leaving a folder of the user: the `mcp/<name>` folders its repository tracks (the agents committed them
    # there), whose removal is committed there after the move so that the user's repository stays clean
    old_tracked: list[str] = field(default_factory=list)

    def result(self, git: str | None = None) -> dict[str, Any]:
        # old_kept: the previous folder still holds its files (an adopt, or the user's folder left as it was)
        kept = str(self.old) if self.mode != "unchanged" and not self.move_all and self.old_exists else None
        return {"mode": self.mode, "from": str(self.old), "to": str(self.target), "git": git or self.git,
                "mcp_moved": list(self.mcp) if self.mode != "unchanged" else [], "old_kept": kept,
                "old_missing": not self.old_exists, "old_changed": None}


@dataclass
class _SkillItem:
    """One skill given to create or start: a reference resolved to its folder, or a source staged (committed to the
    library only once every item is ready)."""
    given: str
    shown: str = ""              # `given` as messages show it: a Git URL without its credentials
    name: str = ""
    folder: Path | None = None
    frm: str = "library"         # the session copy's `from`
    source: dict = field(default_factory=dict)
    info: SkillInfo | None = None
    staged: Staged | None = None


def _discard(items: list[_SkillItem]) -> None:
    for item in items:
        if item.staged is not None:
            item.staged.discard()
            item.staged = None


class _Undo:
    """What a workspace change did so far, to put everything back when a later step fails. Only what the change
    itself made is recorded: an entry that was already in the target is never moved back or deleted."""

    def __init__(self) -> None:
        self.created: list[Path] = []                 # directories made, in creation order
        self.moved: list[tuple[Path, Path, bool]] = []  # (from, to, copied) once `to` holds it, in order
        self.removed_old: Path | None = None          # the emptied default folder that was removed
        self.git_dir: Path | None = None              # a repository created by the change
        self.gitignore: Path | None = None            # a .gitignore created with it

    def run(self) -> list[str]:
        """Undo in reverse order; returns what could not be undone (empty: everything is back)."""
        problems: list[str] = []
        for path in (self.git_dir, self.gitignore):
            if path is not None and os.path.lexists(path):
                try:
                    _remove(path)
                except OSError as exc:
                    problems.append(f"{path}: {exc}")
        if self.removed_old is not None:
            try:
                self.removed_old.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                problems.append(f"{self.removed_old}: {exc}")
        for original, moved, copied in reversed(self.moved):
            try:
                _move_back(moved, original, copied)
            except OSError as exc:
                problems.append(f"{moved} (belongs in {original}): {exc}")
        for folder in reversed(self.created):
            try:
                folder.rmdir()
            except OSError:
                pass  # not empty (something could not be moved back: listed above) or already gone
        return problems


def _repo_refusal(folder: Path, state: dict[str, Any], *, starting: str | None = None) -> SessionError | None:
    """Why the agents cannot work with git in a folder of the user (`repo_state(folder)`), or None. `starting`: the
    session about to start in a folder it adopted before (else the folder is about to be adopted)."""
    where = f"{folder} (the folder session {starting} works in)" if starting else str(folder)
    first = "before starting the session" if starting else "first"
    if state["error"]:
        return SessionError(f"Git cannot read the repository in {where}: {state['error'][:500]}", 409)
    if state["git_dir"]:
        return SessionError(f"{where} is a git repository's own database (a .git folder or a bare repository), not "
                            "a folder of files: choose the folder of its work tree.", 409 if starting else 400)
    toplevel = state["toplevel"]
    if toplevel is None:
        return None  # no repository: one is created, as for a new workspace
    try:
        top = Path(toplevel).resolve()
    except (OSError, ValueError, RuntimeError):
        top = Path(toplevel)
    if not _same_path(top, folder):
        if starting:
            return SessionError(f"{where} is inside the git repository {top}: with git on, DreamTeam would create a "
                                f"repository inside it. Move the session to {top} or to a folder outside it (/cd), "
                                "or turn git off for this session.", 409)
        return SessionError(f"{folder} is inside the git repository {top}: choose the repository's own folder {top}, "
                            "or a folder outside it.")
    if state["branch"] is None:
        return SessionError(f"The git repository in {where} is in a detached HEAD state: check out a branch {first} "
                            "(the agents' commits go to the current branch).", 409)
    if state["operation"]:
        return SessionError(f"A {state['operation']} is in progress in the git repository in {where}: finish or abort "
                            f"it {first} (the agents' next commit would conclude it in their name).", 409)
    if state["changes"]:
        shown = "; ".join(state["changes"][:5]) + ("; ..." if len(state["changes"]) > 5 else "")
        return SessionError(
            f"The git repository in {where} has uncommitted changes or untracked files ({len(state['changes'])}: "
            f"{shown}). Commit or stash them {first}: the agents commit with `git add -A`, which would mix your "
            "changes into their commits.", 409)
    return None


async def _in_thread(fn: Callable[..., Any], *args: Any) -> Any:
    """Blocking file work in a thread that a cancellation cannot abandon halfway (a move whose undo would run
    while it still moves files): the caller waits for it to end before the CancelledError goes on."""
    work = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        while not work.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({work})
        if not work.cancelled():
            work.exception()  # retrieved (the caller undoes the work whatever happened)
        raise


def _home_relative(workspace: Any) -> str | None:
    """The "in_home" entry of a stored workspace: the folder relative to the home (posix) when it lies inside the
    home, so that it is found again where the home is mounted elsewhere (the Docker sandbox) or was moved."""
    relative = workspace.get("in_home") if isinstance(workspace, dict) else None
    if not isinstance(relative, str) or not relative.strip():
        return None
    parts = PurePosixPath(relative.replace("\\", "/")).parts
    if not parts or parts[0] == "/" or ".." in parts or ":" in parts[0]:
        return None  # only a plain relative path inside the home
    return "/".join(parts)


def external_workspaces(home: str | Path) -> list[tuple[str, str]]:
    """(session id, folder) of the sessions of a home that work in a folder outside the home (read from their
    session.json without opening anything; for the Docker sandbox, which mounts only the home: a folder inside
    the home is stored relative to it as well, and is found there)."""
    home = Path(home)
    found = []
    sessions = home / SESSIONS_DIR
    candidates = [(p.name, p / SESSION_FILE) for p in sorted(sessions.iterdir())] if sessions.is_dir() else []
    for sid, path in candidates:
        if sid == LEGACY_ID or not SESSION_ID.match(sid):
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        workspace = meta.get("workspace") if isinstance(meta, dict) else None
        stored = workspace.get("path") if isinstance(workspace, dict) else None
        if not isinstance(stored, str) or not stored.strip() or _home_relative(workspace):
            continue
        found.append((sid, stored))
    return found


def read_stats(db_path: Path, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Board/agent/budget figures of a session database (read-only; zeros when it does not exist)."""
    stats: dict[str, Any] = {
        "goal": None, "tasks": {"total": 0, "done": 0, "open": 0, "by_status": {}},
        "agents": {"total": 0, "working": 0, "llm": 0, "external": 0}, "spent_usd": 0.0,
        "limits": {}, "paused": False, "pause_reason": "", "wrap_up": False, "last_activity": None,
        "jobs": {"running": 0},
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
        wrap_up = kv("wrap_up")  # the user's wrap-up mode (Swarm.wrap_up)
        stats["wrap_up"] = bool(wrap_up.get("on")) if isinstance(wrap_up, dict) else False
        row = conn.execute("SELECT ts FROM events ORDER BY id DESC LIMIT 1").fetchone()
        stats["last_activity"] = row[0] if row else None
        try:  # its own try: a database of a version before background jobs has no jobs table
            row = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'running'").fetchone()
            stats["jobs"]["running"] = int(row[0] or 0)
        except sqlite3.Error as exc:
            log.debug("cannot count the jobs of %s: %s", db_path, exc)
    except sqlite3.Error as exc:  # a database of an older version or locked: show what we have
        log.debug("cannot read stats of %s: %s", db_path, exc)
    finally:
        if own:
            conn.close()
    return stats


def pending_goal(meta: dict[str, Any]) -> dict[str, str] | None:
    """The goal a session was created with and that its first start has not set yet ({title, description}), split
    as set_goal will split it (a session.json written before the split may hold a multi-line title)."""
    pending = meta.get("pending_goal")
    if not isinstance(pending, dict):
        return None
    title, description = split_goal(str(pending.get("title") or ""), str(pending.get("description") or ""))
    return {"title": title, "description": description} if title else None


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
        # one workspace change at a time in the whole home (taken inside a session's lock, from the final check of
        # the new folder until session.json names it): two sessions never take the same folder
        self._claims = asyncio.Lock()
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

    def global_config(self) -> Config:
        """The home's global settings, a hand edit of the file picked up first: the skills library and its limits
        are global ([skills] is no session override)."""
        self._refresh_settings()
        return self.config

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
        workspace = meta.get("workspace")
        if workspace is not None and not (isinstance(workspace, dict) and isinstance(workspace.get("path"), str)
                                          and workspace["path"].strip()):
            raise SessionError(f"Session '{sid}' is damaged: \"workspace\" in {SESSION_FILE} must be "
                               "{\"path\": \"<folder>\", \"adopted\": true|false}.", 500)
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
        """The effective configuration of a session (global settings + its overrides, and its workspace folder)."""
        cfg = self._config_from(self.raw, sid)
        if sid != LEGACY_ID:
            self._apply_workspace(cfg, sid, self.meta(sid))
        return cfg

    # ---- workspace folder -----------------------------------------------------------------------
    def default_workspace(self, sid: str) -> Path:
        """The session's own workspace folder (inside its directory; created on demand)."""
        return self.dir_of(sid) / WORKSPACE_DIR

    def _workspace_entry(self, sid: str, meta: dict[str, Any]) -> tuple[Path, str | None, bool]:
        """(folder, the path as shown or None for the default folder, adopted). A folder inside the home is found
        relative to it ("in_home": the home may be mounted elsewhere, e.g. in the Docker sandbox, or moved). A stored
        path that is not absolute here (a Windows path read inside a Linux container) maps to a folder that does
        not exist in the session."""
        workspace = meta.get("workspace") if sid != LEGACY_ID else None
        if not isinstance(workspace, dict):
            return self.default_workspace(sid), None, False
        relative = _home_relative(workspace)
        if relative is not None:
            folder = self.home / relative
            return folder, str(folder), bool(workspace.get("adopted"))
        stored = str(workspace["path"])
        folder = Path(stored)
        if not folder.is_absolute():
            folder = self.dir_of(sid) / ".unavailable-workspace"
        return folder, stored, bool(workspace.get("adopted"))

    def _workspace_meta(self, target: Path, adopted: bool) -> dict[str, Any]:
        """session.json "workspace" for a folder (resolved): its absolute path, plus the path relative to the home
        when it lies inside the home."""
        entry: dict[str, Any] = {"path": str(target), "adopted": adopted}
        try:
            home = self.home.resolve()
            if _within(target, home):
                entry["in_home"] = PurePosixPath(*Path(os.path.relpath(target, home)).parts).as_posix()
        except (OSError, ValueError):
            pass
        return entry

    def _apply_workspace(self, cfg: Config, sid: str, meta: dict[str, Any]) -> Config:
        folder, stored, adopted = self._workspace_entry(sid, meta)
        cfg.workspace_path = None if stored is None else folder
        cfg.workspace_adopted = adopted
        return cfg

    def workspace_of(self, sid: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """The folder the session works in: {"path", "default" (its own workspace/), "adopted" (a folder that
        held files before, e.g. the user's project), "exists"}."""
        meta = self.meta(sid) if meta is None else meta
        folder, stored, adopted = self._workspace_entry(sid, meta)
        return {"path": str(folder) if stored is None else stored, "default": stored is None, "adopted": adopted,
                "exists": folder.is_dir()}

    def _journal(self, sid: str) -> Path:
        return self.dir_of(sid) / ".dreamteam" / MOVE_JOURNAL

    def _check_journal(self, sid: str, meta: dict[str, Any]) -> None:
        """A workspace move that could not finish (or be undone) leaves its journal: refuse to work until the files
        are put together again."""
        path = self._journal(sid)
        if not path.exists():
            return
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        info = info if isinstance(info, dict) else {}
        uses = self.workspace_of(sid, meta)["path"]
        raise SessionError(
            f"Moving the workspace of session {sid} from {info.get('from', '?')} to {info.get('to', '?')} did not "
            f"finish: some of its files may be in one folder and some in the other. The session uses {uses}: put "
            f"the files together there, then delete {path}.", 409)

    def _check_workspace_ready(self, sid: str, meta: dict[str, Any]) -> None:
        """Before a start: a folder outside the session is never created silently (a deleted project, an unplugged
        drive, a host path inside the Docker sandbox): the agents would work in an empty stand-in."""
        self._check_journal(sid, meta)
        info = self.workspace_of(sid, meta)
        if info["default"] or info["exists"]:
            return
        what = "is not a folder" if Path(info["path"]).exists() else "does not exist"
        raise SessionError(
            f"The workspace folder of session {sid} {what}: {info['path']}. Bring it back (e.g. reconnect the drive), "
            f"or move the session to another folder: /cd <folder> (/cd --default for its own folder) in `dreamteam` "
            f"or `dreamteam console --session {sid}`, session_cd in MCP.", 409)

    def _check_adopted_repo(self, sid: str, folder: Path, mcp: list[str]) -> None:
        """Before a start in a folder the session adopted, with git on: the same rules as when it was adopted, since
        the user may have worked in it meanwhile (or git was off then). Its repository must be clean (the agents
        commit with `git add -A`; the session's own `mcp/<name>` folders are left out: they are the agents' work, and
        so is `.scratch/`, which no commit takes and whose marker the start writes again if an agent removed it),
        on a branch, with no merge or rebase halfway, and not inside another repository (a folder that is in no
        repository gets one at the start, as when it was adopted). Blocking (git): run it in a thread."""
        git_exe = shutil.which("git")
        if git_exe is None:
            return
        state = repo_state(folder, git_exe, exclude=[*(f"mcp/{name}" for name in mcp), SCRATCH_DIR])
        refusal = _repo_refusal(folder, state, starting=sid)
        if refusal is not None:
            raise refusal

    def _forged_servers(self, sid: str) -> list[str]:
        """MCP servers whose files live in the session's workspace (forged there or imported from the library)."""
        db = self.dir_of(sid) / ".dreamteam" / "swarm.db"
        if not db.exists():
            return []
        try:
            conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0)
        except sqlite3.Error:
            return []
        try:
            rows = conn.execute("SELECT name FROM mcp_servers WHERE origin != 'config' AND status != 'removed'"
                                " ORDER BY name").fetchall()
        except sqlite3.Error as exc:
            log.debug("cannot list the MCP servers of %s: %s", sid, exc)
            return []
        finally:
            conn.close()
        return [str(r[0]) for r in rows]

    def _other_workspaces(self, sid: str | None) -> list[tuple[str, Path]]:
        found = []
        for other in self.ids():
            if other == sid:
                continue
            try:
                folder, _stored, _adopted = self._workspace_entry(other, self.meta(other))
                found.append((other, folder.resolve()))
            except (SessionError, OSError, RuntimeError):
                continue  # a damaged session: its own summary shows the error
        return found

    def _plan(self, sid: str | None, path: Any, *, git_on: bool, mcp: list[str] | None = None) -> _Plan:
        """Check a new workspace folder for a session (sid None: a session about to be created) and decide what
        to do; SessionError (400 bad target, 409 conflict) before anything changes. Blocking (git): run it in a
        thread."""
        meta = self.meta(sid) if sid is not None else {}
        if sid is not None:
            old, old_stored, old_adopted = self._workspace_entry(sid, meta)
            default = self.default_workspace(sid)
        else:
            old, old_stored, old_adopted, default = None, None, False, None
        if path is None:
            if default is None:
                raise SessionError("Give the folder for the new session.")
            target = default
        else:
            if not isinstance(path, (str, os.PathLike)):
                raise SessionError("The folder must be a string path.")
            text = os.fspath(path)
            if not isinstance(text, str) or not text.strip():
                raise SessionError("Give a folder path.")
            target = Path(text.strip())
            if not target.is_absolute():
                raise SessionError(f"The folder must be an absolute path, got '{text.strip()}' (clients resolve "
                                   "relative paths against their own current directory).")
        try:
            target = target.resolve()  # symlinks and junctions: the checks below see the real folder
            home = self.home.resolve()
            sessions = self.sessions_dir.resolve()
            user_home = Path.home().resolve()
            old_real = old.resolve() if old is not None else None
            default_real = default.resolve() if default is not None else None
            session_dir = self.dir_of(sid).resolve() if sid is not None else None
        except (OSError, ValueError, RuntimeError) as exc:
            raise SessionError(f"Cannot use the folder {path}: {exc}") from None
        is_default = default_real is not None and _same_path(target, default_real)
        if target.parent == target:
            raise SessionError(f"{target} is the root of a drive or of the file system; choose a folder inside it.")
        if _same_path(target, user_home):
            raise SessionError(f"{target} is your home folder itself; choose a folder inside it, e.g. "
                               f"{target / 'my-project'}.")
        if _within(user_home, target):
            raise SessionError(f"{target} contains your home folder {user_home}; choose a folder inside it.")
        if _same_path(target, home) or _within(home, target):
            raise SessionError(f"{target} is the DreamTeam home or contains it; choose a folder outside {home}.")
        if _within(target, home / ".dreamteam"):
            raise SessionError(f"{target} is inside DreamTeam's own data folder {home / '.dreamteam'}.")
        if _within(target, sessions) and not is_default:
            raise SessionError(f"{target} is inside the DreamTeam sessions folder {sessions}; choose a folder "
                               "outside it (or --default for the session's own folder).")
        if session_dir is not None and _within(target, session_dir / ".dreamteam"):
            raise SessionError(f"{target} is inside the session's data folder.")
        if any(part.casefold() == ".git" for part in target.parts) or looks_like_git_dir(target):
            raise SessionError(f"{target} is a git repository's own database (a .git folder or a bare repository), "
                               "not a folder of files: choose the folder of its work tree.")
        libraries = [self.config.library_dir]
        if sid is not None:
            with contextlib.suppress(ValueError):
                libraries.append(self._config_from(self.raw, sid).library_dir)
        for library in libraries:
            try:
                library = library.resolve()
            except (OSError, ValueError, RuntimeError):
                continue
            if _within(target, library) or _within(library, target):
                raise SessionError(f"{target} is DreamTeam's shared MCP library {library}, or inside it or contains "
                                   "it (every session imports its servers from there); choose another folder.")
        try:  # the skills library: agents would write into the skills every session attaches from
            skills_library = self.config.skills_library_dir.resolve()
        except (OSError, ValueError, RuntimeError):
            skills_library = None
        if skills_library is not None and (_within(target, skills_library) or _within(skills_library, target)):
            raise SessionError(f"{target} is DreamTeam's shared skills library {skills_library}, or inside it or "
                               "contains it (sessions attach their skills from there); choose another folder.")
        try:  # the skills' environments: agents' files and autocommit's `git add -A` would reach every session's
            envs = self.config.skill_envs_dir.resolve()
        except (OSError, ValueError, RuntimeError):
            envs = None
        if envs is not None and (_within(target, envs) or _within(envs, target)):
            raise SessionError(f"{target} is DreamTeam's skill environments folder {envs}, or inside it or contains "
                               "it (every session runs its skills' packages from there); choose another folder.")
        if old_real is not None and _same_path(target, old_real):
            git = "existing" if (target / ".git").exists() else ("disabled" if not git_on else "none")
            return _Plan(target=target, old=old_real, mode="unchanged", default=is_default,
                         adopted=old_adopted, git=git)
        if old_real is not None and _within(target, old_real):
            raise SessionError(f"{target} is inside the session's current folder {old_real}; choose a folder "
                               "outside it.")
        if old_real is not None and (_within(old_real, target) or _within(session_dir, target)):
            raise SessionError(f"{target} contains the session's current folder {old_real}; choose a folder that "
                               "does not.")
        for other, folder in self._other_workspaces(sid):
            if _within(target, folder) or _within(folder, target):
                how = "is the folder" if _same_path(target, folder) else \
                    ("is inside the folder" if _within(target, folder) else "contains the folder")
                raise SessionError(f"{target} {how} of session {other} ({folder}); two sessions cannot share files.",
                                   409)
        if target.exists() and not target.is_dir():
            raise SessionError(f"{target} is a file, not a folder.")
        if target.is_dir():
            try:
                empty = next(target.iterdir(), None) is None
            except OSError as exc:
                raise SessionError(f"Cannot read the folder {target}: {exc.strerror or exc}.") from None
            _check_writable(target, target)
        else:
            empty = True
            parent = target.parent
            while not parent.exists() and parent.parent != parent:
                parent = parent.parent
            if not parent.is_dir():
                why = "is not a folder" if parent.exists() else "does not exist"
                raise SessionError(f"{target} cannot be created: {parent} {why}.")
            _check_writable(parent, target)
        old_exists = old_real is not None and old_real.is_dir()
        git_exe = shutil.which("git")
        plan = _Plan(target=target, old=old_real if old_real is not None else target, mode="moved",
                     default=is_default, adopted=False, git="none", old_exists=old_exists,
                     old_is_default=old_stored is None and old is not None, old_adopted=old_adopted)
        carried = [name for name in (mcp or []) if old_exists and (old_real / "mcp" / name).is_dir()]
        if old_adopted and carried and git_exe:  # leaving the user's folder: did the agents commit the servers there?
            plan.old_tracked = tracked(old_real, [f"mcp/{name}" for name in carried], git_exe)
        if empty and not old_adopted:
            # the session's own files (its default folder, or a folder they were moved to) move along
            plan.move_all = True
            plan.mcp = carried
            if old_exists and (old_real / ".git").exists():
                plan.git = "moved"
            else:
                plan.git = "disabled" if not git_on else ("initialized" if git_exe else "none")
            return plan
        # A folder with files (adopted as it is), or an empty one after the user's own folder: the user's files stay
        # where they are; only the session's MCP servers come along.
        plan.mode = "moved" if empty else "adopted"
        plan.adopted = not empty and not is_default
        plan.mcp = carried
        conflicts = [name for name in carried if os.path.lexists(target / "mcp" / name)]
        if carried and (conflicts or os.path.lexists(target / "mcp") and not (target / "mcp").is_dir()):
            names = ", ".join(f"mcp/{n}" for n in conflicts) or "mcp (a file)"
            raise SessionError(f"{target} already has {names}, where the session's MCP servers would go. Rename or "
                               "remove it there first, or choose another folder.", 409)
        if not git_on:
            plan.git = "disabled"
        elif not git_exe:
            plan.git = "none"
        elif empty or is_default:
            plan.git = "existing" if (target / ".git").exists() else "initialized"
        else:
            # the session's own servers (absent there: checked above) are left out, as at every start: a folder it
            # worked in before may track them, and their move brings them back (else it could never return there)
            state = repo_state(target, git_exe, exclude=[f"mcp/{name}" for name in carried])
            refusal = _repo_refusal(target, state)
            if refusal is not None:
                raise refusal
            plan.git = "initialized" if state["toplevel"] is None else "existing"
        return plan

    def _carry_out(self, sid: str | None, plan: _Plan, undo: _Undo) -> str:
        """Do what the plan says (blocking: run it in a thread). Returns the git outcome; on an exception `undo`
        knows how to put everything back."""
        if sid is not None:
            journal = self._journal(sid)
            journal.parent.mkdir(parents=True, exist_ok=True)
            journal.write_text(json.dumps({"from": str(plan.old), "to": str(plan.target), "mode": plan.mode,
                                           "started_at": time.time()}, ensure_ascii=False), encoding="utf-8")
        missing = []
        folder = plan.target
        while not folder.exists():
            missing.append(folder)
            folder = folder.parent
        for folder in reversed(missing):
            folder.mkdir()
            undo.created.append(folder)
        if plan.old_exists and plan.old_adopted and not plan.move_all:
            # leaving the user's folder: copies of library or Claude Code skills never stay in their project (not
            # undone on a rollback: every start rebuilds the mirror)
            try:
                _drop_mirror(plan.old)
            except OSError as exc:
                log.warning("could not remove the skills mirror in %s: %s", plan.old, exc)
        if plan.move_all:
            if plan.old_exists:
                # the skills mirror first (not undone on a rollback: every start rebuilds it): across volumes a
                # move copies with copytree(symlinks=True), which follows its node_modules junctions into the
                # skills' shared environments
                _drop_mirror(plan.old)
                # .git last: until the end, the old folder stays a complete repository of what has not moved yet
                for entry in sorted(plan.old.iterdir(), key=lambda p: (p.name.casefold() == ".git", p.name)):
                    # (a name that appeared in the target meanwhile: FileExistsError, and it is never touched)
                    _move_path(entry, plan.target / entry.name, undo.moved)
                if plan.old_is_default:  # the session's own folder goes away (it is recreated on demand)
                    try:
                        plan.old.rmdir()
                        undo.removed_old = plan.old
                    except OSError:
                        pass
        elif plan.mcp:
            mcp_dir = plan.target / "mcp"
            if not mcp_dir.exists():
                mcp_dir.mkdir()
                undo.created.append(mcp_dir)
            for name in plan.mcp:
                _move_path(plan.old / "mcp" / name, mcp_dir / name, undo.moved)
        if plan.git == "initialized":
            if os.path.lexists(plan.target / ".git"):  # not ours: never initialized over, nor removed by the undo
                raise SessionError(f"A git repository appeared in {plan.target} meanwhile; check the folder and "
                                   "try again.", 409)
            if not os.path.lexists(plan.target / ".gitignore"):
                undo.gitignore = plan.target / ".gitignore"
            undo.git_dir = plan.target / ".git"
            error = init_repo(plan.target, adopted=plan.adopted)
            if error:
                raise OSError(f"git init in {plan.target} failed: {error}")
        return plan.git

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
        pending = pending_goal(meta)
        if stats["goal"] is None and pending:
            stats["goal"] = {**pending, "status": "pending", "summary": ""}
        profile = cfg.agents.default_model
        model = cfg.models.get(profile)
        attached = self._attached_skills(sid)
        return {
            "id": sid, "title": meta.get("title") or sid, "legacy": meta.get("legacy", False),
            "dir": str(self.dir_of(sid)), "created_at": meta.get("created_at"),
            "status": "running" if sid in self.running else "stopped",
            "loaded": swarm is not None, "autostart": bool(meta.get("autostart")),
            "language": cfg.swarm.language, "model_profile": profile, "model": model.model if model else None,
            "network": cfg.swarm.network,   # "off": the agents' commands have no internet (docs/SPEC.md §7.2)
            "max_cost_usd": _number(limits.get("max_cost_usd", cfg.swarm.max_cost_usd)),
            "max_agents": int(_number(limits.get("max_agents", cfg.swarm.max_agents))),
            "overrides": meta.get("overrides") or {}, "config_error": config_error,
            "url": f"/s/{sid}/", "workspace": self.workspace_of(sid, meta),
            "skills": [s.name for s in attached],
            "skills_pending": [s.name for s in attached if s.library == "pending"],
            **stats,
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

    # ---- skills (docs/SPEC.md §19) -------------------------------------------------------------
    def _data_dir(self, sid: str) -> Path:
        return self.dir_of(sid) / ".dreamteam"   # (Config.data_dir of the session, whatever its overrides)

    def _attached_skills(self, sid: str) -> list:
        """The session's attached skills: from memory when it is loaded, else its metadata files (no Swarm)."""
        swarm = self.swarms.get(sid)
        skills = getattr(swarm, "skills", None)
        if skills is not None:
            return skills.list()
        return read_session_skills(self._data_dir(sid), self.config.skills_library_dir)

    def _prepare_skills(self, config: Config, items: Any, workspace: Path | None) -> list[_SkillItem]:
        """The skills given to create or start, made ready before anything changes (blocking: run it in a thread):
        a reference (`x`, `claude-user:x`, ...) is resolved like a bare attach (`claude-project` only when named,
        against `workspace`) and checked; a source (a folder or .zip path on this machine, a Git URL) is staged, not
        yet committed to the library. SessionError (400 a bad item or a source with several skills, 404 an unknown
        name, 409 a library clash or two different skills of one name); nothing staged is left behind then."""
        if not isinstance(items, list) or not all(isinstance(i, str) and i.strip() for i in items):
            raise SessionError("'skills' must be a list of skill names or sources (folder or .zip paths, Git URLs).")
        lib = Library.from_config(config)
        prepared: list[_SkillItem] = []
        try:
            for text in dict.fromkeys(i.strip() for i in items):
                item = _SkillItem(given=text, shown=text)
                prepared.append(item)
                if skill_lib.looks_like_ref(text):
                    found = skill_lib.resolve_ref(config, text, workspace=workspace)
                    item.name, item.folder, item.frm = found["name"], Path(found["path"]), found["origin"]
                    item.info = skill_lib.check_skill_dir(item.folder, lib.limits)
                    if item.frm == "library":
                        entry = lib.get(item.name) or {}
                        item.source = {"origin": entry.get("origin") or "folder", **(entry.get("source") or {})}
                    else:
                        item.source = {"path": str(item.folder)}
                    continue
                try:
                    spec = source_spec(text)
                except ToolError as exc:
                    raise SessionError(str(exc)) from None
                item.staged = skill_lib.stage_source(lib, spec, workspace=workspace)
                item.shown = item.staged.shown   # (never the typed URL: it may carry a token)
                if item.staged.need_pick:
                    names = ", ".join(c.name for c in item.staged.candidates if c.error is None)
                    raise SessionError(f"{item.staged.shown} holds several skills ({names}): add them in the Skills "
                                       "tab and name them.")
                item.info = item.staged.picked[0].info
                item.name = item.info.name
                if item.staged.conflicts():   # (the commit would be a 409: known now, before anything changes)
                    raise SessionError(f"The library already has a skill '{item.name}' (different content): import "
                                       f"{item.shown} in the Skills tab to replace it.", 409, exists=[item.name])
            unique: dict[str, _SkillItem] = {}
            for item in prepared:
                first = unique.setdefault(item.name, item)
                if first is not item and first.info.digest != item.info.digest:
                    raise SessionError(f"Two of the skills given are named '{item.name}' ({first.shown}, "
                                       f"{item.shown}) and differ.", 409, exists=[item.name])
            _discard([item for item in prepared if unique[item.name] is not item])   # the same skill twice
            return list(unique.values())
        except SkillError as exc:
            _discard(prepared)
            raise _session_error(exc) from None
        except BaseException:
            _discard(prepared)
            raise

    def _commit_skills(self, config: Config, items: list[_SkillItem], data_dir: Path, *,
                       via: str) -> list[str]:
        """Commit the staged sources to the library (`by` the user, `via`), then copy every skill into the session
        whose data dir is `data_dir` (attached_via "create"). Blocking: run it in a thread. The names attached."""
        lib = Library.from_config(config)
        names = []
        try:
            for item in items:
                if item.staged is not None:
                    entry = item.staged.commit(by=USER, via=via)["imported"][0]
                    item.staged = None
                    item.folder, item.frm, item.info = lib.path(entry["name"]), "library", None
                    item.source = {"origin": entry.get("origin") or "folder", **(entry.get("source") or {})}
                skill, _status = attach_files(data_dir, item.folder, item.name, item.frm, item.source, lib.limits,
                                              info=item.info, library_dir=config.skills_library_dir)
                names.append(skill.name)
        except (SkillError, ToolError) as exc:
            raise _session_error(exc) from None
        finally:
            _discard(items)
        return names

    async def _skills_at_start(self, sid: str, swarm: "Swarm", items: Any, via: str) -> list[str]:
        """`start(sid, skills=...)` under the session's lock: a running session attaches them live (the members are
        told); a stopped one gets their copies written before its Swarm starts (the members find them in their
        first wake-up). Every item is ready before anything changes. Returns the names written (stopped)."""
        config = self.global_config()
        if not isinstance(items, list):
            raise SessionError("'skills' must be a list of skill names or sources (folder or .zip paths, Git URLs).")
        prepared = await asyncio.to_thread(self._prepare_skills, config, items, swarm.workspace.root)
        try:
            attached = {s.name: s for s in self._attached_skills(sid)}
            for item in prepared:
                have = attached.get(item.name)
                if have is not None and have.digest != item.info.digest:
                    raise SessionError(f"A skill named '{item.name}' is already attached to session {sid} (different "
                                       "content): replace it in the session's Skills tab.", 409, exists=[item.name])
            new = [item for item in prepared if item.name not in attached]
            if len(attached) + len(new) > config.skills.max_attached:
                raise SessionError(f"Session {sid} would have more than {config.skills.max_attached} skills ([skills] "
                                   "max_attached): detach some first.", 409)
            if sid not in self.running:
                return await asyncio.to_thread(self._commit_skills, config, prepared, swarm.config.data_dir, via=via)
            for item in prepared:   # live, with the usual notifications
                ref = item.given
                if item.staged is not None:
                    entry = (await asyncio.to_thread(item.staged.commit, False, USER, via))["imported"][0]
                    item.staged, ref = None, f"library:{entry['name']}"
                await swarm.skills.attach(USER, ref, via=via)
            return []
        except (SkillError, ToolError) as exc:
            raise _session_error(exc) from None
        finally:
            _discard(prepared)

    async def session_skills(self, sid: str, show: str | None = None) -> dict[str, Any]:
        """`{"attached": [...], "running": bool}` of a session, stopped or running: the snapshot items (docs/SPEC.md
        §14.1) of a loaded swarm, else the same keys from its files (`deps` empty); `show`: also `files_list`,
        `skill_md` and `deps` of that session copy, and `install` (the switches its preview line is worded by)."""
        self._check(sid)
        swarm = self.swarms.get(sid)
        if getattr(swarm, "skills", None) is not None:
            attached = swarm.skills.public()   # on the loop: its deps read live state
        else:
            attached = await asyncio.to_thread(self._stopped_skills, sid)
        out: dict[str, Any] = {"attached": attached, "running": sid in self.running}
        if show is not None:
            try:
                name = skill_lib.check_name(show)
            except SkillError as exc:
                raise _session_error(exc) from None
            if name not in {a["name"] for a in attached}:
                raise SessionError(f"No skill '{name}' is attached to session {sid}.", 404)
            out.update(await asyncio.to_thread(skill_lib.show_folder, skills_dir(self._data_dir(sid)) / name,
                                               Limits.from_config(self.config)))
            # the home's proposal dialog may open before its Skills tab ever loaded the switches
            out["install"] = skill_lib.install_info(self.global_config())
        return out

    def _stopped_skills(self, sid: str) -> list[dict]:
        try:
            config = self.config_for(sid)
        except (ValueError, OSError):   # invalid overrides now: the home's roles, the session's own files
            config = self._apply_workspace(copy.copy(self.config), sid, self.meta(sid))
            config.project_dir = self.dir_of(sid)
        return stopped_public(config)

    async def decide_skill(self, sid: str, name: str, approve: bool, *, comment: str = "", replace: bool = False,
                           via: str = "home") -> str:
        """The user's library decision on a member's proposal (skill_approve), also on a stopped session: there it is
        made on its files (`via` "stopped-session"; the owner is told at the next start). The result text;
        SessionError with the status (404 unknown, 409 not pending or a library clash)."""
        self._check(sid)
        async with self._lock(sid):
            self._check(sid)
            try:
                if sid in self.running:
                    text = await self.swarms[sid].skills.decide(USER, name, approve, comment=comment,
                                                                replace=replace, via=via)
                else:
                    config = self.global_config()
                    _skill, text = await asyncio.to_thread(
                        decide_files, self._data_dir(sid), Library.from_config(config), name, approve, comment=comment,
                        replace=replace, via="stopped-session", project=self._swarm_name(sid), session=sid)
                    loaded = self.swarms.get(sid)   # viewed while stopped: its cache reads the decision too
                    if getattr(loaded, "skills", None) is not None:
                        await asyncio.to_thread(loaded.skills.load)
            except (SkillError, ToolError) as exc:
                raise _session_error(exc) from None
        self._changed(sid, "updated")
        if approve:   # saved to the library: an open Skills tab of the home page reloads it
            self._changed(None, "skills")
        return text

    def _swarm_name(self, sid: str) -> str:
        try:
            return self._config_from(self.raw, sid).swarm.name
        except (ValueError, OSError):   # invalid overrides now: the home's name
            return self.config.swarm.name

    async def _save_pending(self, sid: str, pending: list[str], via: str) -> None:
        """`delete(sid, skills="save")`: every pending proposal saved to the library first; any clash is found before
        one is saved (409 `exists`, nothing saved, nothing deleted)."""
        lib = Library.from_config(self.global_config())
        attached = {s.name: s for s in self._attached_skills(sid)}
        clashes = []
        for name in pending:
            entry = lib.get(name)
            if entry is not None and ("error" in entry or lib.digest_of(name) != attached[name].digest):
                clashes.append(name)
        if clashes:
            raise SessionError(f"The library already has other skills named {', '.join(clashes)}: decide them in the "
                               f"session's Skills tab first, or delete session {sid} with its proposals discarded.",
                               409, exists=clashes, pending_skills=pending)
        try:
            for name in pending:
                if sid in self.running:
                    await self.swarms[sid].skills.decide(USER, name, True, via=via)
                else:   # decided on its files, as decide_skill records it
                    await asyncio.to_thread(decide_files, self._data_dir(sid), lib, name, True, via="stopped-session",
                                            project=self._swarm_name(sid), session=sid)
        except (SkillError, ToolError) as exc:
            raise _session_error(exc) from None
        finally:
            self._changed(None, "skills")   # the library gained them (also when one failed after others)

    # ---- lifecycle ----------------------------------------------------------------------------
    def _new_id(self, title: str) -> str:
        numbers = [int(m.group(1)) for p in (self.sessions_dir.iterdir() if self.sessions_dir.is_dir() else [])
                   if (m := re.match(r"^(\d+)", p.name))]
        number = max(numbers, default=0) + 1
        slug = slugify(title)
        sid = f"{number:03d}-{slug}" if slug else f"{number:03d}"
        return sid[:64].rstrip("-")

    async def create(self, title: str, goal: str = "", description: str = "", *,
                     overrides: dict[str, Any] | None = None, start: bool = True, actor: str = USER,
                     workspace: str | os.PathLike | None = None, skills: list[str] | None = None,
                     via: str = "home") -> dict[str, Any]:
        """Create a session (and start it with the goal unless start=False). Returns its summary.
        `workspace`: an absolute folder to work in instead of the session's own one (the rules of set_workspace:
        an empty or missing folder is used as a new workspace, one with files is adopted); the summary then has
        "workspace_result" (set_workspace's result, mode "created" for a new folder).
        `skills`: skills attached before the first start, references (`x`, `claude-user:x`, `claude-project:x` from
        the `workspace` folder) or sources (absolute folder or .zip paths, Git URLs: imported into the library, `by`
        the user, `via` the surface); all of them are resolved and staged before anything is created."""
        # the goal split as set_goal splits it: its first line also names the session (cut at a word)
        goal_title, goal_description = split_goal(_text(goal), _text(description))
        title = (_one_line(title) or shorten_title(goal_title, 60) or "Session")[:TITLE_LIMIT].strip()
        self._refresh_settings()
        overrides = self._validated_overrides(overrides)
        if workspace is not None:  # checked before anything is created
            git_on = validate(self.sessions_dir / "new", self._merged(self.raw, overrides)).workspace.git
            await asyncio.to_thread(self._plan, None, workspace, git_on=git_on)
        prepared: list[_SkillItem] = []
        if skills:
            if isinstance(skills, list) and len(skills) > self.config.skills.max_attached:
                raise SessionError(f"At most {self.config.skills.max_attached} skills can be attached to a session "
                                   "([skills] max_attached).")
            prepared = await asyncio.to_thread(self._prepare_skills, self.config, skills,
                                               Path(workspace) if workspace is not None else None)
        try:
            summary = await self._create(title, goal_title, goal_description, overrides, start, actor, workspace,
                                         prepared, via)
        finally:
            _discard(prepared)   # (whatever a failure left staged)
        return summary

    async def _create(self, title: str, goal_title: str, goal_description: str, overrides: dict[str, Any],
                      start: bool, actor: str, workspace: str | os.PathLike | None, prepared: list[_SkillItem],
                      via: str) -> dict[str, Any]:
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
        (path / WORKSPACE_DIR).mkdir(exist_ok=True)
        meta: dict[str, Any] = {"title": title, "created_at": time.time(), "overrides": overrides,
                                "autostart": False}
        if goal_title:  # set at the first start, when the members exist
            meta["pending_goal"] = {"title": goal_title, "description": goal_description, "actor": actor}
        self._write_meta(sid, meta)
        if prepared:  # before the folder: a failure leaves nothing in it; the members find them at the first start
            try:
                await asyncio.to_thread(self._commit_skills, self.config, prepared, self._data_dir(sid), via=via)
            except BaseException:
                await asyncio.to_thread(_rmtree, path)  # nothing half-created stays behind
                raise
        result = None
        if workspace is not None:
            try:
                result = await self._set_workspace(sid, workspace, creating=True)
            except BaseException:
                await asyncio.to_thread(_rmtree, path)  # nothing half-created stays behind
                raise
            if result["mode"] == "moved":
                result["mode"] = "created"
            elif result["old_kept"]:  # adopted: the session's own folder was never used
                with contextlib.suppress(OSError):
                    self.default_workspace(sid).rmdir()
                result["old_kept"] = None
        self._changed(sid, "created")
        if start:
            await self.start(sid)
        summary = self.summary(sid)
        if result is not None:
            summary["workspace_result"] = result
        return summary

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
        meta = self.meta(sid)
        swarm.session_id = sid
        swarm.session_title = meta.get("title") or sid
        swarm.session_pending_goal = pending_goal(meta)
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

    async def start(self, sid: str, *, skills: list[str] | None = None, via: str = "home") -> "Swarm":
        """Start the session (a no-op when it runs). `skills`: references or sources, as `create` takes them,
        attached first: live to a running session, written into a stopped one before it starts (`via`: the surface,
        recorded in the library for a source). SessionError before anything changes when one of them cannot be."""
        self._check(sid)
        async with self._lock(sid):
            written: list[str] = []
            if skills:
                written = await self._skills_at_start(sid, self._load_locked(sid), skills, via)
            swarm = await self._start_locked(sid)
            if written:
                swarm.skills.attached_again(written)
        self._changed(sid, "started")
        return swarm

    async def _start_locked(self, sid: str) -> "Swarm":
        """Start the session (if it is not running). The caller holds the session's lock."""
        # loaded under the lock: a swarm fetched before waiting could have been unloaded meanwhile
        swarm = self._load_locked(sid)
        if sid not in self.running:
            if sid != LEGACY_ID:
                self._check_workspace_ready(sid, self.meta(sid))
                cfg = swarm.config  # what this start will use (the loaded swarm got any new settings)
                if cfg.workspace_adopted and cfg.workspace.git:
                    servers = [r["name"] for r in swarm.db.all("SELECT name FROM mcp_servers WHERE origin != 'config'")]
                    await asyncio.to_thread(self._check_adopted_repo, sid, cfg.workspace_dir, servers)
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
            notice = meta.pop("workspace_notice", None)
            if isinstance(notice, dict) and notice.get("key") in ("n_workspace_moved", "n_workspace_changed"):
                try:  # news for the agents' next wake-up, not a reason to wake everybody; an external member (an
                    # MCP client polling wait_for_events) gets it at its next poll
                    for member in swarm.members():
                        if member["kind"] in ("llm", "external"):
                            swarm.notify(member["name"], notice["key"], wake=member["kind"] == "external",
                                         path=str(notice.get("path", "")), old=str(notice.get("old", "")))
                except Exception:
                    log.exception("could not tell the members of session %s about its new folder", sid)
            self._write_meta(sid, meta)
            swarm.session_pending_goal = pending_goal(meta)
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

    def _patched_overrides(self, stored: Any, settings: Any) -> dict[str, Any]:
        """The stored overrides with a partial update applied: {section: {key: value or None}}; None (or "") removes
        the key, so the session inherits the global value again, and a section left empty disappears. The other
        overrides (those given at creation: language, team, model...) stay as they are."""
        if not isinstance(settings, dict):
            raise SessionError("Settings must be an object of sections.")
        if stored is not None and not isinstance(stored, dict):
            raise SessionError("The stored overrides of the session are damaged; replace them with \"overrides\".")
        result = copy.deepcopy(stored or {})
        for section, values in settings.items():
            if section not in OVERRIDE_SECTIONS:
                raise SessionError(f"Sessions cannot override [{section}] (allowed: {', '.join(OVERRIDE_SECTIONS)}).")
            if not isinstance(values, dict):
                raise SessionError(f"Settings [{section}] must be an object.")
            current = result.get(section)
            current = current if isinstance(current, dict) else {}
            for key, value in values.items():
                if value is None:
                    current.pop(key, None)
                else:
                    current[key] = copy.deepcopy(value)
            result[section] = current
        return self.clean_overrides(result)

    def update(self, sid: str, *, title: str | None = None, overrides: dict[str, Any] | None = None,
               settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """Rename a session, replace its overrides (`overrides`) or change some of them (`settings`: {section: {key:
        value or None}}, see _patched_overrides; the settings dialog of the session page). Applied to the loaded
        swarm at once, like the global settings; the language and the start settings matter only at the first start.
        Nothing is written when a value is invalid."""
        self._check(sid)
        if overrides is not None and settings is not None:
            raise SessionError("Pass either \"overrides\" (all of them) or \"settings\" (the keys to change), not both.")
        meta = self.meta(sid)
        if title is not None:
            if not isinstance(title, str):
                raise SessionError("The title must be a string.")
            title = _one_line(title)[:TITLE_LIMIT].strip()
            if not title:
                raise SessionError("The title cannot be empty.")
            meta["title"] = title
        config = None
        if overrides is not None or settings is not None:
            if sid == LEGACY_ID:
                raise SessionError("The main session uses the global settings directly; edit them instead.")
            clean = (self.clean_overrides(overrides) if settings is None
                     else self._patched_overrides(meta.get("overrides"), settings))
            try:
                config = self._apply_workspace(self._config_from(self.raw, sid, clean), sid, meta)
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

    async def set_workspace(self, sid: str, path: str | os.PathLike | None, *,
                            expect: str | None = None) -> dict[str, Any]:
        """Make the session work in another folder (`path` absolute; None: its own default `workspace/`).

        * An empty or missing folder: the session's files (with the git history and the forged MCP servers)
          move there; the old folder is left empty (its own default folder is removed, recreated on demand).
          After a folder the session adopted (the user's), the user's files stay: the new folder starts as a
          fresh workspace and only the session's MCP servers move.
        * A folder with files (e.g. the user's project) is adopted: the agents work with its files and commit to
          its current branch. Refused (409) when its repository has uncommitted changes or untracked files, is in
          a detached HEAD, in the middle of a merge or rebase, or cannot be read; a folder inside a repository but
          not its root, or a repository's own database (a .git folder, a bare repository), is refused (400); a
          folder that is not a repository gets one (git init, .gitignore if missing, an initial commit). Its git
          configuration is never modified. The session's old folder stays as it is; the session's MCP servers
          (`mcp/<name>/`) move along (409 when the folder already has one of them).
        * Refused before anything changes (400): relative paths, a drive or file system root, the user's home and
          the DreamTeam home (or a folder containing one), anything in `sessions/` but the session's own folder,
          its data folder, the shared MCP library, a folder inside the current one or containing it, a file, a
          folder that cannot be written; 409: a folder another session uses (or inside it / containing it). The
          current folder itself is a no-op ("unchanged").
        `expect`: the mode the user agreed to ("moved" or "adopted"); 409 when the folder turns out otherwise (e.g.
        files appeared in a folder that was empty when the user was asked).
        A running session is stopped first and started again afterwards (also after a failure, which undoes
        everything moved so far). Workspace changes of the sessions of a home run one at a time. Returns {"mode":
        "moved"|"adopted"|"unchanged", "from", "to", "git": "moved"|"existing"|"initialized"|"disabled"|"none",
        "mcp_moved": [names], "old_kept": folder|None, "old_missing": bool, "old_changed": None|{"paths",
        "commit", "error"}, "restarted": bool, "start_error": text|None}."""
        self._check(sid)
        if sid == LEGACY_ID:
            raise SessionError(LEGACY_WORKSPACE_TEXT, 409)
        if expect not in (None, "moved", "adopted"):
            raise SessionError("expect must be \"moved\" or \"adopted\".")
        return await self._set_workspace(sid, path, expect=expect)

    @staticmethod
    def _check_expected(sid: str, plan: _Plan, expect: str | None, first: _Plan | None = None) -> None:
        """The folder must still be what the caller (the user) agreed to, and what the first check found."""
        wanted = expect or (first.mode if first is not None else None)
        if wanted is None or plan.mode == wanted:
            return
        if plan.mode == "adopted":
            what = "now holds files: the session would work with them instead of moving its own files there"
        elif plan.mode == "moved":
            what = "is now empty or missing: the session's files would move there instead of adopting it"
        else:
            what = "is now the session's folder already"
        raise SessionError(f"The folder {plan.target} {what}. Nothing was changed; check the folder and try again "
                           f"(session {sid}).", 409)

    async def _set_workspace(self, sid: str, path: Any, *, creating: bool = False,
                             expect: str | None = None) -> dict[str, Any]:
        notify = not creating
        async with self._lock(sid):
            self._check(sid)  # deleted while this call waited for the lock
            meta = self.meta(sid)
            self._check_journal(sid, meta)
            try:
                git_on = self.config_for(sid).workspace.git
            except SessionError:
                raise
            except ValueError as exc:
                raise SessionError(f"Session {sid} has invalid settings: {exc}") from None
            first = await asyncio.to_thread(self._plan, sid, path, git_on=git_on, mcp=self._forged_servers(sid))
            if first.mode == "unchanged":
                return {**first.result(), "restarted": False, "start_error": None}
            self._check_expected(sid, first, expect)
            was_loaded, was_running = sid in self.swarms, sid in self.running
            try:  # agents, MCP server processes and triggers stop: nothing may write into the folder now
                await self._unload(sid)
            except Exception:  # unloaded and closed anyway (_unload closes in `finally`)
                log.exception("error stopping session %s before moving its workspace", sid)
            if was_loaded and notify:
                self._changed(sid, "stopped")
            try:
                async with self._claims:  # no other session takes a folder until this one's session.json names it
                    plan, git, old_changed = await self._change_folder(sid, path, git_on, first, expect, creating)
            except BaseException:
                if was_running:
                    with contextlib.suppress(Exception):
                        await self._start_locked(sid)
                    if notify and sid in self.running:
                        self._changed(sid, "started")
                raise
            log.info("session %s now works in %s (%s, git %s)", sid, plan.target, plan.mode, git)
            if notify:
                self._changed(sid, "updated")
            result = {**plan.result(git), "old_changed": old_changed, "restarted": False, "start_error": None}
            if was_running:
                try:
                    await self._start_locked(sid)
                    result["restarted"] = True
                except Exception as exc:  # the move stands; the session stays stopped and says why
                    log.exception("session %s did not start again after moving its workspace", sid)
                    result["start_error"] = str(exc) if isinstance(exc, SessionError) else \
                        f"{type(exc).__name__}: {exc}"
        if was_running and result["restarted"] and notify:
            self._changed(sid, "started")
        return result

    async def _change_folder(self, sid: str, path: Any, git_on: bool, first: _Plan, expect: str | None,
                             creating: bool) -> tuple[_Plan, str, dict[str, Any] | None]:
        """The change itself, with the session unloaded and the home's workspace claims held: the final check, the
        moves, session.json. Returns (plan, git outcome, what changed in the user's folder it left); raises with
        everything put back (or, when that failed too, an error that says where the files are)."""
        # checked again with nothing running: an agent may have forged a server, the folder may have changed, and
        # another session may have taken the folder meanwhile (it is in its session.json now)
        plan = await asyncio.to_thread(self._plan, sid, path, git_on=git_on, mcp=self._forged_servers(sid))
        if plan.mode == "unchanged":
            raise SessionError(f"The folder of session {sid} changed meanwhile; try again.", 409)
        self._check_expected(sid, plan, expect, first)
        undo = _Undo()
        try:
            git = await _in_thread(self._carry_out, sid, plan, undo)
            meta = self.meta(sid)  # read again: a rename may have happened meanwhile
            if plan.default:
                meta.pop("workspace", None)
            else:
                meta["workspace"] = self._workspace_meta(plan.target, plan.adopted)
            if not creating:  # the members hear of it at their next wake-up (the next start delivers it)
                meta["workspace_notice"] = {"key": "n_workspace_moved" if plan.move_all else "n_workspace_changed",
                                            "path": str(plan.target), "old": str(plan.old)}
            self._write_meta(sid, meta)
        except BaseException as exc:
            problems = await _in_thread(undo.run)
            if not problems:
                with contextlib.suppress(OSError):
                    self._journal(sid).unlink()
            if not isinstance(exc, Exception):
                raise
            raise self._move_error(sid, plan, exc, problems, creating=creating) from None
        with contextlib.suppress(OSError):
            self._journal(sid).unlink()
        old_changed = None
        if plan.old_tracked:  # the agents' commits put the session's MCP servers into the user's repository
            old_changed = {"paths": list(plan.old_tracked), "commit": None, "error": "git is off for this session"}
            if git_on:
                message = (f"Remove {', '.join(plan.old_tracked)}: the MCP servers of DreamTeam session {sid} "
                           "moved with it to another folder")
                try:
                    old_changed["commit"], old_changed["error"] = await asyncio.to_thread(
                        commit_paths, plan.old, plan.old_tracked, message)
                except (OSError, subprocess.SubprocessError) as exc:
                    old_changed["error"] = str(exc)
            if old_changed["error"]:
                log.warning("session %s: the removal of %s in %s is not committed: %s", sid, plan.old_tracked,
                            plan.old, old_changed["error"])
        return plan, git, old_changed

    def _move_error(self, sid: str, plan: _Plan, exc: Exception, problems: list[str], *,
                    creating: bool = False) -> SessionError:
        if isinstance(exc, SessionError):
            reason = str(exc)
        elif isinstance(exc, OSError) and exc.strerror:
            reason = exc.strerror + (f": {exc.filename}" if exc.filename else "")
        else:
            reason = str(exc) or type(exc).__name__
        if creating:  # the new session is removed: only the target can hold leftovers
            if problems:
                return SessionError(
                    f"Could not create the session in {plan.target} ({reason}); the session was not created, and "
                    f"some of what was made in that folder could not be removed: {'; '.join(problems[:10])}. "
                    "Remove it by hand.", 500)
            return SessionError(f"Could not create the session in {plan.target} ({reason}); the session was not "
                                "created and the folder is as it was.",
                                exc.status if isinstance(exc, SessionError) else 500)
        if problems:
            return SessionError(
                f"Could not change the folder of session {sid} to {plan.target} ({reason}), and some files could "
                f"not be put back: {'; '.join(problems[:10])}. The session still uses {plan.old}; move those files "
                f"back there by hand, then delete {self._journal(sid)}.", 500)
        in_use = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (5, 32, 33)
        status = exc.status if isinstance(exc, SessionError) else (409 if in_use or isinstance(exc, FileExistsError)
                                                                   else 500)
        return SessionError(
            f"Could not change the folder of session {sid} to {plan.target} ({reason})"
            + ("; a file is in use: close the programs that use it and try again" if in_use else "")
            + f". Nothing was changed: the session still works in {plan.old}.", status)

    async def delete(self, sid: str, *, skills: str | None = None, via: str = "home") -> str | None:
        """Stop the session and delete its directory (board, messages, workspace, traces). The directory is
        first renamed away, so a session whose files are in use stays intact (409) instead of being
        half-deleted. A workspace folder outside the directory (set_workspace) is never deleted: it is the
        user's folder (the skills mirror in it goes). Returns that folder (read under the session's lock, so a
        workspace change that ran just before is taken into account), else None.
        Skills members proposed that wait for the user's library decision are not lost silently: 409
        (`pending_skills`) unless `skills` is "save" (each saved to the library first, `via`; a clash refuses the
        delete) or "discard"."""
        self._check(sid)
        if sid == LEGACY_ID:
            raise SessionError("The main session lives in the home directory itself and cannot be deleted "
                               "from the UI; delete its .dreamteam folder by hand.", 409)
        if skills not in (None, "save", "discard"):
            raise SessionError("skills must be \"save\" or \"discard\".")
        async with self._lock(sid):
            self._check(sid)  # a concurrent delete finished first
            pending = [s.name for s in self._attached_skills(sid) if s.library == "pending"]
            if pending and skills is None:
                raise SessionError(f"Session {sid} has skills waiting for your library decision: "
                                   f"{', '.join(pending)}. Save them to the library first, or delete them with the "
                                   "session.", 409, pending_skills=pending)
            if pending and skills == "save":
                await self._save_pending(sid, pending, via)
            try:
                await self._unload(sid)
            except Exception:  # unloaded and closed anyway (_unload closes in `finally`)
                log.exception("error stopping session %s before deleting it", sid)
            try:
                workspace = self.workspace_of(sid)
                kept = None if workspace["default"] else workspace["path"]
            except SessionError:  # a damaged session.json: it can still be deleted
                workspace, kept = {}, None
            if workspace.get("adopted") and workspace.get("exists"):
                # the user's folder keeps no copy of the session's skills (Swarm.stop removes it too: this covers a
                # crashed or never-stopped one)
                try:
                    await asyncio.to_thread(_drop_mirror, Path(workspace["path"]))
                except OSError as exc:
                    log.warning("could not remove the skills mirror in %s: %s", workspace["path"], exc)
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
        return kept

    def _purge_trash(self) -> None:
        """Remove renamed-away session directories (also ones a previous delete could not finish)."""
        if not self.sessions_dir.is_dir():
            return
        for path in self.sessions_dir.iterdir():
            if path.name.startswith(TRASH_PREFIX) and path.is_dir():
                _rmtree(path)
                if path.exists():
                    log.warning("could not remove %s completely; it is retried at the next delete", path)

    def finished(self, sid: str) -> bool:
        """The session's work is over: its goal is achieved and no task is open (todo, in_progress, waiting or
        review). Read from its database without loading it; False when that cannot be read."""
        try:
            stats = self.summary(sid)
        except Exception:  # e.g. a damaged session.json: whether it can start is start()'s to say
            log.debug("cannot tell whether session %s is finished", sid, exc_info=True)
            return False
        by_status = (stats.get("tasks") or {}).get("by_status") or {}
        return ((stats.get("goal") or {}).get("status") == "done"
                and not any(by_status.get(status) for status in Status.OPEN))

    async def resume(self) -> list[str]:
        """Start the sessions that were running when the server last stopped, except finished ones (finished()):
        they stay stopped until started by hand."""
        started = []
        for sid in self.ids():
            try:
                if self.meta(sid).get("autostart"):
                    if self.finished(sid):
                        log.info("session %s is finished (goal achieved, no open task): not resumed", sid)
                        continue
                    await self.start(sid)
                    started.append(sid)
            except SessionError as exc:  # e.g. its workspace folder is missing: the message says what to do
                log.warning("could not resume session %s: %s", sid, exc)
            except Exception:
                log.exception("could not resume session %s", sid)
        return started

    async def shutdown(self) -> None:
        """Stop every loaded session, keeping the running ones marked for resume()."""
        # also sessions in the middle of an operation (a start, a workspace move that starts them again)
        busy = [sid for sid, lock in self._locks.items() if lock.locked() and sid not in self.swarms]
        for sid in list(self.swarms) + busy:
            try:
                async with self._lock(sid):
                    await self._unload(sid)
            except Exception:
                log.exception("error stopping session %s", sid)

    def copy_overrides(self, sid: str) -> dict[str, Any]:
        return copy.deepcopy(self.meta(sid).get("overrides") or {})
