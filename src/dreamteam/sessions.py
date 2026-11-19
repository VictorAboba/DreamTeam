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
import math
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Collection, Iterable, Iterator

from . import __version__, apifolders, hubauth, netoff, nofollow
from . import skills as skill_lib
from .autoreply import SessionAutoReply, follow_journal, journal_head, listed_requests, requests_from
from .config import INT64_LIMIT, Config
from .copyfrom import (_SYSTEM_ROOTS, API_UPLOADS_DIR, COPYING_NAME, SOURCE_SHOWN_MAX, UPLOADS_DIR, CopyCancelled,
                       CopyError, CopyLimits, Source, Uploads, _other_home, check_target, copy_into, copy_sources,
                       done_with, hub_in_container, left_over, printable_line, resolve_source, staging_name)
from .db import Database, loads, made_by_earlier_version, reading_file, remap_sql, sqlite_uri
from .events import event_data, read_page
from .eventtext import without_abs_paths
from .goal_text import shorten_title, split_goal
from .instruct import without_api_sessions
from .models import API_VIA, USER, Status, Task, ToolError
from .settings import (
    apply_update, deep_merge, ensure_home, profile_renames, public_view, read_raw, settings_path, validate, write_raw,
)
from .skills import Library, Limits, SkillError, SkillInfo, Staged
from .swarm_skills import attach_files, decide_files, read_session_skills, skills_dir, source_spec, stopped_public
from .treecopy import is_within
from .workspace import (SCRATCH_DIR, SKILLS_MIRROR, commit_paths, init_repo, is_link, isolated_git, looks_like_git_dir,
                        pid_alive, repo_state, rmtree, tracked)

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
IMPORTING_PREFIX = ".importing-"  # a session being imported, staged before it is renamed in (never a valid session id)
REPLACED_PREFIX = ".replaced-"  # the last copy of a session an import rewrote: .replaced-<id>-<time>/
SWAP_JOURNAL = "swap.json"  # in a .replaced-* backup while an import swaps the session (kept if the process died)
# what an import rewrite moves from the old copy's workspace into the new one: environments an export never carries
KEEP_ON_IMPORT = (".venv", "venv", "node_modules")
_STAMP = re.compile(r"\d{8}-\d{6}(?:-\d+)?")
RENAME_TRIES = 10  # a rename refused while a file inside is held open for a moment (Windows) is tried again
RENAME_PAUSE = 0.2
# session.json replaced while a reader holds it open without sharing delete access (Windows: a thread of the hub's,
# such as the HTTP API v1's, another process, a scanner) is tried again (_write_meta): it blocks the loop at most
# META_REPLACE_TRIES * META_REPLACE_PAUSE seconds, and only while the file is held
META_REPLACE_TRIES = 10
META_REPLACE_PAUSE = 0.02
# A session's identity across homes and computers (session.json "uid", see session_uid): the sequence id is not one,
# two homes both make 017-session. Fixed forever: it derives the uid of sessions made before "uid" was written.
UID_NAMESPACE = uuid.UUID("545ad73f-3f1d-471d-a6b5-de9395f527da")
_UID = re.compile(r"[0-9a-f]{32}")
# the manager's own journal events: a run's start and end (docs/SPEC.md §21.6). They are not the session's activity:
# `last_activity` leaves them out, or a hub shutdown would stamp every running session.
SESSION_EVENTS = ("session.started", "session.stopped")
# why `session.stopped`: a stop by the user (any surface), a goal or budget end (stop_when_done), the hub stopping, a
# stop the hub makes to start the session again (a workspace move), a start that failed
STOP_REASONS = ("user", "done", "budget", "shutdown", "restart", "failed")
# the reasons a caller of SessionManager.stop may give; the others are the manager's own (shutdown(), a workspace
# move, a failed start)
STOP_REQUEST_REASONS = ("user", "done", "budget")
# the HTTP API v1's codes of SessionError (docs/SPEC.md §21.1, §21.10): a folder another session works in (or is being
# created in); a folder the session cannot work in now (a missing workspace folder, a repository not ready for the
# agents' commits, a workspace move that did not finish); a server folder out of its root, or under a root that no
# longer allows it
FOLDER_BUSY = "folder_busy"
FOLDER_NOT_READY = "folder_not_ready"
FORBIDDEN_FOLDER = "forbidden_folder"
# session.json "via" of a session created through the HTTP API v1 (models.API_VIA, which the clients read too), and its
# "api" block (docs/SPEC.md §21.4): what the create gives (API_GIVEN, with their defaults) and what the manager writes
# later
API_ANSWERS = ("auto", "manual")
API_GIVEN = {"answers": "auto", "stop_when_done": True, "client_ref": None, "request_digest": None, "folder": None}
API_LATER = ("base_commit", "started_at", "finished_at", "start_error", "error")
# what an import drops from an "api" block (docs/SPEC.md §21.9): the caller's retry key and its body's digest, the
# errors of runs on the other computer, and the server folder, whose {root, path} names another folder here or none
API_IMPORT_DROPS = ("client_ref", "request_digest", "start_error", "error", "folder")
# a SessionError's status as a v1 code, for a start_error without a code of its own
_STATUS_CODES = {400: "bad_request", 404: "not_found", 409: "conflict", 413: "too_large"}
# SessionManager.journal's 503 when a stopped session's journal file cannot be read now (docs/SPEC.md §21.6 "Polling")
JOURNAL_TEXT = "The session's journal cannot be read now (the hub's log says why): ask again later."
LEGACY_WORKSPACE_TEXT = (
    "The main session lives in the home directory itself, and `dreamteam run` can still open that directory as a "
    "single-swarm project, which would not know about another folder: its workspace cannot be moved. Create a new "
    "session in the other folder instead.")
# a session whose database was made by an earlier DreamTeam (db.made_by_earlier_version): listed, deleted and
# exported, never opened (docs/SPEC.md §17.2 "Sessions of an earlier version")
EARLIER_VERSION_TEXT = (
    "Session {sid} was made by an earlier version of DreamTeam and cannot be opened by this one ({version}). Open it "
    "with the version that made it (export it here, then import it into a home of that version: once this one saved "
    "its settings, that version may not read this home), or delete it.")
MAIN_EARLIER_VERSION_TEXT = (
    "Session main (the swarm in {home}) was made by an earlier version of DreamTeam and cannot be opened by this one "
    "({version}). Open it with the version that made it, or remove {home}/.dreamteam/swarm.db (with its -wal and "
    "-shm files) by hand.")

_TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюяіїєґ",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
     "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya", "i", "yi", "ye", "g"],
))


class SessionError(ValueError):
    """A session operation that cannot be done; `status` is the matching HTTP status, `extra` goes into the JSON
    answer (`exists`: skill names that clash, `pending_skills`: the proposals a delete would lose). `code`: the HTTP
    API v1's error code where the status alone does not say it (docs/SPEC.md §21.1: `folder_busy`,
    `folder_not_ready`, `conflict`, a CopyError's `bad_name` or `file_conflict`); None otherwise. The internal
    routes do not show it. v1 answers a code with that code's own status (§21.1), which may differ from `status`:
    `folder_not_ready` is 409 there, also for the two refusals of a create that are 400 here (a folder inside a
    repository, a repository's own database). `holder`: the session whose folder it is (`folder_busy`), which v1
    names, by its uid, only to a caller who sees that session (docs/SPEC.md §21.10); never in the internal answers
    either."""

    def __init__(self, message: str, status: int = 400, *, code: str | None = None, holder: str | None = None,
                 **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.holder = holder
        self.extra = extra


def _session_error(exc: SkillError | ToolError) -> SessionError:
    """A skills error (SkillError, or a swarm_skills ToolError with its status) as a SessionError."""
    return SessionError(str(exc), getattr(exc, "status", 400), **(getattr(exc, "extra", None) or {}))


def earlier_version_text(sid: str, home: str | os.PathLike | None, version: str | None = None) -> str:
    """The message of a session made by an earlier version (the legacy `main` names the file to remove by hand).
    `home` and `version` may come from a hub's /api/hub; `version` defaults to this one."""
    version = version or __version__
    if sid == LEGACY_ID:
        return MAIN_EARLIER_VERSION_TEXT.format(home=os.fspath(home) if home else "the home", version=version)
    return EARLIER_VERSION_TEXT.format(sid=sid, version=version)


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


def _finite(number: int | float) -> bool:
    """Whether `number` is finite as a float: a whole number too large for one (10**400, which JSON may hold) is not,
    where math.isfinite would raise OverflowError."""
    try:
        return math.isfinite(number)
    except OverflowError:
        return False


def _copied_from(value: Any) -> dict[str, Any] | None:
    """session.json "copied_from" as a summary shows it: exactly {source, files, bytes, at}, the source one
    printable line of at most SOURCE_SHOWN_MAX characters, the numbers >= 0; None when there is none. The file may
    come from an import or a hand edit, so nothing else of it is passed on."""
    if not isinstance(value, dict) or not isinstance(value.get("source"), str):
        return None

    def count(v: Any) -> float:
        n = _number(v)
        return n if math.isfinite(n) and n > 0 else 0.0

    return {"source": printable_line(value["source"], SOURCE_SHOWN_MAX), "files": int(count(value.get("files"))),
            "bytes": int(count(value.get("bytes"))), "at": count(value.get("at"))}


# workspace.rmtree: the extended-length form on Windows, since a renamed-away session (.deleted-<id>-<ns>) is longer
# than the original and its deepest files (git objects, read-only there) may pass the 260-character limit
_rmtree = rmtree


# ---- workspace folders ---------------------------------------------------------------------------------
def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _same_path(a: Path, b: Path) -> bool:
    return _path_key(a) == _path_key(b)


def _fold_key(path: Path) -> str:
    return unicodedata.normalize("NFC", os.path.normpath(str(path))).casefold()


def _overlaps(a: Path, b: Path) -> bool:
    """The resolved folders `a` and `b` are one folder, or one holds the other, ignoring letter case and Unicode
    form (a folder not created yet has no file identity to compare: on the safe side, for a reservation only)."""
    p, q = _fold_key(a), _fold_key(b)
    return p == q or p.startswith(q.rstrip(os.sep) + os.sep) or q.startswith(p.rstrip(os.sep) + os.sep)


def _within(path: Path, parent: Path) -> bool:
    """`path` is `parent` or inside it (both resolved; case-insensitive where the file system is)."""
    p, q = _path_key(path), _path_key(parent)
    return p == q or p.startswith(q if q.endswith(os.sep) else q + os.sep)


def _some_entries(folder: Path, shown: int = 5) -> str:
    """A few entries of a folder that is not empty, as ' (it holds .git, notes.txt; hidden files count)': .git and
    the hidden ones first (a fresh `git init`, .DS_Store: the user may see no files there); '' when it cannot be
    read."""
    try:
        with os.scandir(folder) as it:
            names = [entry.name for entry in it]
    except OSError:
        return ""
    if not names:
        return ""
    names.sort(key=lambda n: (n != ".git", not n.startswith("."), n.casefold()))
    text = ", ".join(printable_line(n, 80) or "?" for n in names[:shown]) + (", ..." if len(names) > shown else "")
    hidden = "; hidden files count" if any(n.startswith(".") for n in names[:shown]) else ""
    return f" (it holds {text}{hidden})"


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


def _check_inside(path: Path, within: Path) -> None:
    """The folder `path` of a plan (resolved when it was planned) still resolves to itself inside the resolved root
    `within` of a server folder (docs/SPEC.md §21.10 "Resolution"): 403 `forbidden_folder` otherwise, a link or a
    junction swapped in on its way since (the text names the planned path, never where it leads). Blocking."""
    try:
        real: Path | None = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        real = None
    if real is None or not (_same_path(real, path) and is_within(real, within)):
        raise _left_root(path)


def _left_root(path: Path) -> SessionError:
    return SessionError(f"{path} does not lie inside its folder root any more: a link or a junction on the way leads "
                        "out of it.", 403, code=FORBIDDEN_FOLDER)


def _identity(st: os.stat_result) -> tuple[int, int]:
    return st.st_dev, st.st_ino


def _same_identity(a: tuple[int, int], b: tuple[int, int]) -> bool:
    """The same file, where the file system gives file ids (st_ino 0 on both sides or either: it does not say)."""
    return not (a[1] and b[1]) or a == b


def _hold(folder: Path, within: Path) -> int:
    """The folder of a server folder's plan held: nofollow.hold_inside from its resolved root `within`, name by name,
    never through a link (on Windows nothing on its way can be renamed while the descriptor is open; POSIX has no such
    hold). A descriptor the caller closes; 403 `forbidden_folder` when the folder is not reached so (a link or a
    junction on the way, the folder renamed or gone). Blocking."""
    try:
        return nofollow.hold_inside(within, folder.relative_to(within).parts)
    except (ValueError, nofollow.Refused, FileNotFoundError, NotADirectoryError):
        raise _left_root(folder) from None


def _check_held(folder: Path, within: Path, held: tuple[int, int]) -> None:
    """The folder of a server folder's plan still resolves to itself inside `within` (_check_inside) and is still the
    folder held (`held`: its identity when _hold opened it), else 403 `forbidden_folder`. Blocking."""
    _check_inside(folder, within)
    try:
        now = _identity(os.stat(folder))
    except OSError:
        raise _left_root(folder) from None
    if not _same_identity(now, held):
        raise _left_root(folder)


def _remove_held(fd: int, folder: Path, names: Iterable[str]) -> list[str]:
    """Remove the entries `names` of the folder held as `fd` (_hold): on POSIX through the descriptor (`dir_fd`), so
    in that very folder wherever it lies now and never through a link; on Windows by path, which the hold keeps naming
    that folder (a link in a name's own place is removed itself, never followed). Returns what could not be removed.
    Blocking."""
    problems = []
    for name in names:
        try:
            if os.name == "nt":
                path = folder / name
                if is_link(path):
                    try:
                        os.unlink(path)
                    except OSError:
                        os.rmdir(path)
                elif os.path.lexists(path):
                    _remove(path)
                continue
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(st.st_mode):
                shutil.rmtree(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)
        except (OSError, NotImplementedError) as exc:
            problems.append(f"{folder / name}: {exc}")
    return problems


def _remove_from_held(folder: Path, within: Path, held: tuple[int, int], names: list[str]) -> list[str]:
    """The undo's removal of what git init made in a server folder (`names` of `folder`): only from the folder held then
    (`held`, its identity), held again (_hold); a folder that is not reached so any more (renamed, moved, a link or a
    junction on its way) keeps them, reported, since its path leads elsewhere now. Returns what could not be removed.
    Blocking."""
    try:
        fd = _hold(folder, within)
    except SessionError:
        fd = None
    except OSError as exc:
        return [f"{folder / name}: not removed: {exc}" for name in names]
    try:
        if fd is not None and _same_identity(_identity(os.fstat(fd)), held):
            return _remove_held(fd, folder, names)
    finally:
        if fd is not None:
            os.close(fd)
    return [f"{folder / name}: not removed: the folder is no longer there inside its folder root (renamed or moved, or "
            "a link or a junction on its way)" for name in names]


def _init_held(plan: "_Plan", within: Path, undo: "_Undo", names: list[str]) -> None:
    """Git init in a server folder in place (docs/SPEC.md §21.10 "Resolution") with the folder held (_hold): on Windows
    nothing on its way can then be renamed until git is done; on POSIX, which has no such hold, every step works in the
    folder held, through its descriptor (init_repo's `held`, isolated_git's: git starts in it, the .gitignore is made
    and the sandbox's offline user given the folder through it), and the folder must still be the one held, inside its
    root, before each of init_repo's steps and after the last (_check_held), so a link swapped in ends it at the next
    step (what git itself does by the path it takes when it starts is §21.10's). What a step that fails leaves of
    `names` (.git, and .gitignore when it was not there) is removed through the held folder (_remove_held), never by a
    path; once it is done, the undo of a later failure removes them from that folder only (`undo.held`). Blocking."""
    fd = _hold(plan.target, within)
    try:
        held = _identity(os.fstat(fd))

        def check() -> None:
            _check_held(plan.target, within, held)

        try:
            check()
            offline = isolated_git(plan.network_off, plan.target, held=fd)
            error = init_repo(plan.target, adopted=plan.adopted, offline=offline, check=check, held=fd)
            if error:
                raise OSError(f"git init in {plan.target} failed: {error}")
            check()
        except BaseException:
            undo.problems += _remove_held(fd, plan.target, names)
            raise
        undo.held = (within, held)
        undo.git_dir = plan.target / ".git"
        undo.gitignore = plan.target / ".gitignore" if ".gitignore" in names else None
    finally:
        os.close(fd)


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
    # the session has no network: DreamTeam's git in both folders runs like its members' commands (isolated_git)
    network_off: bool = False
    # a server folder of the HTTP API v1 (docs/SPEC.md §21.10): its resolved root, which _carry_out finds every level
    # of the target inside again, right before and after it makes one, and before and after git init (_check_inside)
    within: Path | None = None

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
        # a server folder's (docs/SPEC.md §21.10 "Resolution"): its resolved root and the identity of the folder git
        # init ran in, held (_init_held); .git / .gitignore are then removed only from that folder, held again
        self.held: tuple[Path, tuple[int, int]] | None = None
        # a server folder's: the identity of each level made (None: unknown), removed only while its path names it
        self.made: dict[Path, tuple[int, int] | None] = {}
        self.problems: list[str] = []                 # what a step that failed could not remove itself

    def run(self) -> list[str]:
        """Undo in reverse order; returns what could not be undone (empty: everything is back)."""
        problems: list[str] = list(self.problems)
        made = [path for path in (self.git_dir, self.gitignore) if path is not None]
        if self.held is not None and made:
            problems += _remove_from_held(made[0].parent, *self.held, [path.name for path in made])
        for path in made if self.held is None else ():
            if os.path.lexists(path):
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
            if folder in self.made:   # (a server folder's level: only while its path still names the folder made)
                try:
                    st = os.lstat(folder)
                except OSError:
                    continue
                ident = self.made[folder]
                if ident is None or not stat.S_ISDIR(st.st_mode) or not _same_identity(_identity(st), ident):
                    continue
            try:
                folder.rmdir()
            except OSError:
                pass  # not empty (something could not be moved back: listed above) or already gone
        return problems


def _repo_refusal(folder: Path, state: dict[str, Any], *, starting: str | None = None) -> SessionError | None:
    """Why the agents cannot work with git in a folder of the user (`repo_state(folder)`), or None. `starting`: the
    session about to start in a folder it adopted before (else the folder is about to be adopted). Every refusal
    carries the v1 code `folder_not_ready` (docs/SPEC.md §21.10)."""
    where = f"{folder} (the folder session {starting} works in)" if starting else str(folder)
    first = "before starting the session" if starting else "first"

    def refused(text: str, status: int = 409) -> SessionError:
        return SessionError(text, status, code=FOLDER_NOT_READY)

    if state["error"]:
        return refused(f"Git cannot read the repository in {where}: {state['error'][:500]}")
    if state["git_dir"]:
        return refused(f"{where} is a git repository's own database (a .git folder or a bare repository), not a "
                       "folder of files: choose the folder of its work tree.", 409 if starting else 400)
    toplevel = state["toplevel"]
    if toplevel is None:
        return None  # no repository: one is created, as for a new workspace
    try:
        top = Path(toplevel).resolve()
    except (OSError, ValueError, RuntimeError):
        top = Path(toplevel)
    if not _same_path(top, folder):
        if starting:
            return refused(f"{where} is inside the git repository {top}: with git on, DreamTeam would create a "
                           f"repository inside it. Move the session to {top} or to a folder outside it (/cd), or "
                           "turn git off for this session.")
        return refused(f"{folder} is inside the git repository {top}: choose the repository's own folder {top}, or "
                       "a folder outside it.", 400)
    if state["branch"] is None:
        return refused(f"The git repository in {where} is in a detached HEAD state: check out a branch {first} (the "
                       "agents' commits go to the current branch).")
    if state["operation"]:
        return refused(f"A {state['operation']} is in progress in the git repository in {where}: finish or abort it "
                       f"{first} (the agents' next commit would conclude it in their name).")
    if state["changes"]:
        shown = "; ".join(state["changes"][:5]) + ("; ..." if len(state["changes"]) > 5 else "")
        return refused(
            f"The git repository in {where} has uncommitted changes or untracked files ({len(state['changes'])}: "
            f"{shown}). Commit or stash them {first}: the agents commit with `git add -A`, which would mix your "
            "changes into their commits.")
    return None


async def _in_thread(fn: Callable[..., Any], *args: Any, on_cancel: Callable[[], None] | None = None,
                     threads: Callable[..., Awaitable[asyncio.Future[Any]]] | None = None) -> Any:
    """Blocking file work in a thread that a cancellation cannot abandon halfway (a move whose undo would run
    while it still moves files): the caller waits for it to end before the CancelledError goes on. `on_cancel()`
    is called at the first cancellation, before the wait (it tells the work to stop early: a copy). `threads`: where
    the work runs instead of the loop's default executor, `threads(fn, *args)` awaited for a future of its result
    (the HTTP API v1's folder threads, api_v1._Threads.start: a cancel while it waits for a place starts nothing)."""
    work = await threads(fn, *args) if threads is not None else asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        while not work.done():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.wait({work})
        if not work.cancelled():
            work.exception()  # retrieved (the caller undoes the work whatever happened)
        raise


async def _to_end(fn: Callable[..., Any], *args: Any) -> Any:
    """Blocking file work in a thread, run to its end and its result returned whatever happens meanwhile: a
    cancellation (Ctrl+C) waits for it and stays pending for the caller, who sees it in
    `asyncio.current_task().cancelling()` (an import finishes the session it is swapping in, then stops)."""
    work = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    while True:
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            if work.done():
                return work.result()


# ---- import ----------------------------------------------------------------------------------------------
@dataclass
class ImportedWorkspace:
    """Where a session an import brings in works on this computer. `own`: its own `workspace/` folder (the staged
    copy's). `in_home`: `folder`, a folder of the home that the staged `workspace/` becomes. `outside`: `path`, a
    folder outside the home, kept as it is stored (its files are never copied)."""
    kind: str = "own"
    folder: Path | None = None
    path: str | None = None
    adopted: bool = False


def _rename_retry(src: Path, dst: Path, done: list[tuple[Path, Path]] | None = None) -> None:
    """Rename a file or folder within one volume. A refusal while a file inside is held open for a moment (Windows:
    a scanner, an indexer) is tried again, as `delete` does. Something at `dst` is never replaced (FileExistsError,
    nothing renamed). `done` gets (src, dst) once renamed."""
    if os.path.lexists(dst):
        raise FileExistsError(errno.EEXIST, "a file or folder of that name is already there", str(dst))
    for attempt in range(RENAME_TRIES):
        try:
            os.rename(src, dst)
            break
        except (FileNotFoundError, FileExistsError):
            raise
        except OSError as exc:
            if _cross_device(exc) or attempt == RENAME_TRIES - 1:
                raise
            time.sleep(RENAME_PAUSE)
    if done is not None:
        done.append((src, dst))


def _undo_renames(done: list[tuple[Path, Path]], created: list[Path]) -> list[str]:
    """Put back what `_rename_retry` renamed (in reverse order) and remove the folders made for it; returns what
    could not be put back (empty: everything is as it was)."""
    problems = []
    for src, dst in reversed(done):
        try:
            _rename_retry(dst, src)
        except OSError as exc:
            problems.append(f"{dst} (belongs in {src}): {exc.strerror or exc}")
    for folder in reversed(created):
        with contextlib.suppress(OSError):
            folder.rmdir()
    return problems


def _in_use(exc: BaseException) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in (5, 32, 33)


_pid_alive = pid_alive


def _journal_path(path: Path, home: Path) -> str:
    """A path of a swap journal: relative to the home (posix) when it lies inside it, so that the journal still
    holds where the home was moved or mounted under another drive letter; absolute otherwise."""
    try:
        return Path(path).relative_to(home).as_posix()
    except ValueError:
        return str(path)


def _journal_resolve(text: Any, home: Path) -> Path:
    """A path of a swap journal (_journal_path) for the home as it is now. ValueError for a relative path that
    leaves the home."""
    path = Path(str(text))
    if path.is_absolute():
        return path   # (a journal of an older version, or a folder outside the home)
    if not path.parts or ".." in path.parts or path.drive or path.root:
        raise ValueError(f"not a path inside the home: {text}")
    return home / path


def _write_swap_journal(path: Path, sid: str, planned: list[tuple[Path, Path]], done: list[tuple[Path, Path]],
                        created: list[Path], home: Path) -> None:
    """The journal of a session swap (`_swap_replaced`), written atomically: the planned renames, those done, the
    folders made for them (paths inside `home` relative to it) and the process that does it."""
    def rel(p: Path) -> str:
        return _journal_path(p, home)

    data = {"sid": sid, "pid": os.getpid(), "planned": [[rel(a), rel(b)] for a, b in planned],
            "done": [[rel(a), rel(b)] for a, b in done], "created": [rel(p) for p in created]}
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8", "replace"))
    os.replace(tmp, path)


def _backup_uid(backup: Path) -> str | None:
    """The uid of the session copy a `.replaced-*` backup keeps (None when its session.json cannot be read)."""
    try:
        meta = json.loads((backup / "session" / SESSION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return session_uid(meta) if isinstance(meta, dict) else None


def _rebase_job_logs(db: Database, staged_jobs: Path, final_jobs: Path) -> None:
    """Point the absolute job-log paths of an imported database (the data-dir fallback of the computer it came
    from) at the same file of the copy's own jobs folder `final_jobs`, when the copy has it; clear the others."""
    for row in db.all("SELECT id, log FROM jobs WHERE log IS NOT NULL AND log != ''"):
        value = str(row["log"])
        for flavour in (PurePosixPath, PureWindowsPath):
            path = flavour(value)
            if path.is_absolute():
                break
        else:
            continue   # relative to the workspace: found again there
        name = path.name
        found = path.parent.name == "jobs" and name and not is_link(staged_jobs / name) \
            and (staged_jobs / name).is_file()
        db.execute("UPDATE jobs SET log = ? WHERE id = ?", (str(final_jobs / name) if found else None, row["id"]))


def _make_parents(folder: Path, created: list[Path]) -> None:
    missing = []
    parent = folder.parent
    while not os.path.lexists(parent):
        missing.append(parent)
        parent = parent.parent
    for path in reversed(missing):
        path.mkdir()
        created.append(path)


def _home_relative(workspace: Any) -> str | None:
    """The "in_home" entry of a stored workspace: the folder relative to the home (posix) when it lies inside the
    home, so that it is found again where the home is mounted elsewhere (the Docker sandbox) or was moved."""
    relative = workspace.get("in_home") if isinstance(workspace, dict) else None
    if not isinstance(relative, str) or not relative.strip() or any(ord(c) < 32 for c in relative):
        return None   # (a NUL or another control character is in no folder name)
    text = relative.replace("\\", "/")
    parts = PurePosixPath(text).parts
    if not parts or text.startswith("/") or PurePosixPath(text).is_absolute() or ".." in parts or ":" in parts[0]:
        return None  # only a plain relative path inside the home ('//x' is a root of its own for PurePosixPath)
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


def read_stats(db_path: Path, conn: sqlite3.Connection | None = None, *, clarify: bool = False,
               running: bool = True, requests: bool = False) -> dict[str, Any]:
    """Board/agent/budget figures of a session database (read-only; zeros when it does not exist), and whether an
    earlier version made it (`earlier_version`). A file of its own is read through db.reading_file, which writes
    nothing next to it (a plain read-only open of a WAL database would leave a -wal and a -shm in every stopped
    session at each listing). `requests` True adds "requests": how many requests to the user wait
    (autoreply.requests_from: the same kinds the HTTP API v1 lists, docs/SPEC.md §21.5), `clarify` being the session's
    [swarm] clarify; of a session that does not run (`running` False) only those that hold its work
    (autoreply.listed_requests). Only the HTTP API v1's summary asks for it: those queries take longer as the journal
    grows, and the board's summaries (the sessions page polls them, default_session reads them all) never show it."""
    stats: dict[str, Any] = {
        "goal": None, "tasks": {"total": 0, "done": 0, "open": 0, "by_status": {}},
        "agents": {"total": 0, "working": 0, "llm": 0, "external": 0}, "spent_usd": 0.0,
        "limits": {}, "paused": False, "pause_reason": "", "wrap_up": False, "last_activity": None,
        "jobs": {"running": 0}, "question": None, "earlier_version": False,
    }
    if requests:
        stats["requests"] = 0
    with contextlib.ExitStack() as stack:
        if conn is None:
            if not db_path.exists():
                return stats
            try:
                conn, _immutable = stack.enter_context(reading_file(db_path, timeout=2.0))
            except (sqlite3.Error, OSError):
                return stats
        _read_stats(conn, db_path, stats, clarify, running, requests)
    return stats


def _read_stats(conn: sqlite3.Connection, db_path: Path, stats: dict[str, Any], clarify: bool = False,
                running: bool = True, requests: bool = False) -> None:
    """Fill `stats` (read_stats) from an open connection; what cannot be read keeps its default. `requests`: count the
    requests to the user too (read_stats)."""
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
        # a member's question that holds the pause (Swarm.pending_question): the launcher's card and session_line
        if (stats["paused"] and paused.get("reason") == "question" and paused.get("asker")
                and isinstance(paused.get("message_id"), int)):
            stats["question"] = {"asker": paused["asker"], "message_id": paused["message_id"]}
        wrap_up = kv("wrap_up")  # the user's wrap-up mode (Swarm.wrap_up)
        stats["wrap_up"] = bool(wrap_up.get("on")) if isinstance(wrap_up, dict) else False
        # the newest event of the session's own work: a run's start and end (SESSION_EVENTS) are not activity
        row = conn.execute(f"SELECT ts FROM events WHERE type NOT IN ({', '.join('?' * len(SESSION_EVENTS))}) "
                           "ORDER BY id DESC LIMIT 1", SESSION_EVENTS).fetchone()
        stats["last_activity"] = row[0] if row else None
        try:  # its own try: a database of a version before background jobs has no jobs table
            row = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'running'").fetchone()
            stats["jobs"]["running"] = int(row[0] or 0)
        except sqlite3.Error as exc:
            log.debug("cannot count the jobs of %s: %s", db_path, exc)
        if requests:
            try:  # its own try too: a request it cannot read must not hide the figures above
                stats["requests"] = len(listed_requests(requests_from(conn, clarify=clarify), running=running))
            except (sqlite3.Error, ValueError, TypeError, KeyError, IndexError) as exc:   # (a damaged row too)
                log.debug("cannot count the requests of %s: %s", db_path, exc)
    except sqlite3.Error as exc:  # a database of an older version or locked: show what we have
        log.debug("cannot read stats of %s: %s", db_path, exc)
    try:  # its own try: the tables it reads may be all that is left of a damaged file
        stats["earlier_version"] = made_by_earlier_version(conn)
    except sqlite3.Error as exc:
        log.debug("cannot tell whether an earlier version made %s: %s", db_path, exc)


def read_lists(db_path: Path, conn: sqlite3.Connection | None = None) -> tuple[list[dict[str, Any]],
                                                                               list[dict[str, Any]]]:
    """(tasks, agents) of a session database, read-only, in the shape and order of a loaded swarm's snapshot: every
    task (closed ones too) as `Task.to_dict()`, by priority then id, without the board's computed fields (blocked_by,
    comments, crew_working stay empty); every member (retired ones too) as its `agents` row, in hiring order. For a
    stopped session's result (docs/SPEC.md §21.7) without loading it. A file of its own is read through
    db.reading_file, as read_stats reads it; `conn`: a connection the caller holds (under the Database's lock).
    ([], []) without a database; what cannot be read stays empty (logged)."""
    tasks: list[dict[str, Any]] = []
    agents: list[dict[str, Any]] = []
    with contextlib.ExitStack() as stack:
        if conn is None:
            if not db_path.exists():
                return tasks, agents
            try:
                conn, _immutable = stack.enter_context(reading_file(db_path, timeout=2.0))
            except (sqlite3.Error, OSError):
                return tasks, agents
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row   # (never the connection's own: it may be a loaded swarm's)
        try:
            tasks = [Task.from_row(row).to_dict()
                     for row in cursor.execute("SELECT * FROM tasks ORDER BY priority DESC, id ASC")]
            agents = [dict(row) for row in cursor.execute("SELECT * FROM agents ORDER BY created_at, rowid")]
        except sqlite3.Error as exc:   # a database of an older version, or locked: show what we have
            log.debug("cannot read the tasks and members of %s: %s", db_path, exc)
        finally:
            cursor.close()
    return tasks, agents


def rename_member_profiles(db_path: Path, renames: dict[str, str]) -> int:
    """agents.model of a session database that is not loaded follows profile renames ({old: new}); the number of
    members changed. A missing file or one without an `agents` table is left alone; any error (a database held by
    another process, a damaged file) is logged and gives 0: such a member falls back to the default profile."""
    if not renames or not db_path.exists():
        return 0
    try:  # a plain path (exists() above): a file: URI of a UNC path or a mapped drive has an authority SQLite refuses
        conn = sqlite3.connect(str(db_path), timeout=2.0)
    except (OSError, sqlite3.Error) as exc:
        log.warning("could not rename the model profiles of the members in %s: %s", db_path, exc)
        return 0
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agents'").fetchone() is None:
            return 0
        with conn:  # one statement: in a swap or a chain a name is both an old and a new one
            return conn.execute(*remap_sql("agents", "model", renames)).rowcount
    except sqlite3.Error as exc:
        log.warning("could not rename the model profiles of the members in %s: %s", db_path, exc)
        return 0
    finally:
        conn.close()


def pending_goal(meta: dict[str, Any]) -> dict[str, str] | None:
    """The goal a session was created with and that its first start has not set yet ({title, description}), split
    as set_goal will split it (a session.json written before the split may hold a multi-line title)."""
    pending = meta.get("pending_goal")
    if not isinstance(pending, dict):
        return None
    title, description = split_goal(str(pending.get("title") or ""), str(pending.get("description") or ""))
    return {"title": title, "description": description} if title else None


def new_uid() -> str:
    """A fresh session uid: 32 lowercase hex characters."""
    return uuid.uuid4().hex


def session_uid(meta: dict[str, Any]) -> str | None:
    """The session's identity across homes and computers: session.json "uid" when it holds 32 hex characters
    (lowercased), else a uid derived from "created_at" (uuid5 of its float value), so a session made before "uid"
    existed, or a folder copied by hand, has the same identity in every home it is in. Nothing is written. None
    without a usable "created_at" (missing, not a finite number) and for the legacy main session, whose created_at
    may come from the file system and is not stable: such a session is never matched with another."""
    if not isinstance(meta, dict) or meta.get("legacy"):
        return None
    stored = meta.get("uid")
    if isinstance(stored, str) and _UID.fullmatch(stored.lower()):
        return stored.lower()
    created = meta.get("created_at")
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        return None
    try:
        value = float(created)
    except OverflowError:  # an int too large for a float
        return None
    if not math.isfinite(value):
        return None
    return uuid.uuid5(UID_NAMESPACE, "created_at:" + repr(value)).hex


def api_meta(meta: Any, *, create: bool = False) -> dict[str, Any] | None:
    """The "api" block of the session.json `meta` of a session created through the HTTP API v1 ("via": "api",
    docs/SPEC.md §21.4), None for any other session. A v1 session whose block is not an object (a hand edit) gets an
    empty one; `create`: put into `meta`, so that a change to it is written with it."""
    if not isinstance(meta, dict) or meta.get("via") != API_VIA or meta.get("legacy"):
        return None
    block = meta.get("api")
    if not isinstance(block, dict):
        block = {}
        if create:
            meta["api"] = block
    return block


def api_answers(api: dict[str, Any]) -> Any:
    """The `answers` of a v1 session's "api" block (api_meta), API_GIVEN's default ("auto") when the block has none (a
    hand edit), as `stop_when_done` falls back to its own: the hold, the automatic answers and the summary agree."""
    return api.get("answers", API_GIVEN["answers"])


def _new_api_block(api: Any) -> dict[str, Any]:
    """The "api" block a create writes (docs/SPEC.md §21.4): what the caller gives (the keys of API_GIVEN, their
    defaults for the others) and the keys the manager fills later (API_LATER), null. SessionError 400 for another key
    or a value of the wrong kind (v1 checks the request before; this guards the session.json it writes)."""
    if not isinstance(api, dict):
        raise SessionError("'api' must be an object.")
    unknown = sorted(str(key) for key in api if key not in API_GIVEN)
    if unknown:
        raise SessionError(f"Unknown keys in 'api': {', '.join(unknown)}.")
    block = {**API_GIVEN, **copy.deepcopy(api)}
    if block["answers"] not in API_ANSWERS:
        raise SessionError(f"'answers' must be one of {', '.join(API_ANSWERS)}.")
    if not isinstance(block["stop_when_done"], bool):
        raise SessionError("'stop_when_done' must be true or false.")
    for key in ("client_ref", "request_digest"):
        if block[key] is not None and not isinstance(block[key], str):
            raise SessionError(f"'{key}' must be a string.")
    if block["folder"] is not None and not isinstance(block["folder"], dict):
        raise SessionError("'folder' must be an object.")
    block.update(dict.fromkeys(API_LATER))
    return block


def _error_code(exc: BaseException) -> str:
    """The v1 code of a start's failure (api.start_error.code): the error's own code (SessionError, CopyError), else
    by its status, else "internal" (docs/SPEC.md §21.1)."""
    code = exc.code if isinstance(exc, (SessionError, CopyError)) else None
    if isinstance(code, str) and code:
        return code
    if isinstance(exc, SessionError):
        if exc.extra.get("earlier_version"):
            return "earlier_version"
        return _STATUS_CODES.get(exc.status, "internal")
    return "internal"


class _SessionLock(asyncio.Lock):
    """A session's lock that calls `on_release` each time it is released: the end of an operation that held it (a
    start, a stop, a workspace move...) wakes the session's waiters (SessionManager.watch), since a held lock keeps
    the session from being final (is_final) and its release changes no state a waiter could see otherwise."""

    def __init__(self, on_release: Callable[[], None]) -> None:
        super().__init__()
        self._on_release = on_release

    def release(self) -> None:
        super().release()
        try:
            self._on_release()
        except Exception:  # a waiter must never break the operation that held the lock
            log.exception("could not wake the waiters of a session")


@dataclass
class _RunWatch:
    """What runs beside one run of a v1 session (docs/SPEC.md §21.4, §21.5), tied to its Swarm object: the hub's
    automatic answers (`answers: "auto"`) and the stop_when_done watcher."""
    swarm: "Swarm"
    auto_reply: SessionAutoReply | None = None
    watcher: asyncio.Task | None = None

    async def stop(self) -> None:
        """Cancel both and wait for them to end; never raises (but a cancel of the caller)."""
        if self.watcher is not None:
            self.watcher.cancel()
            await asyncio.wait({self.watcher})
            if not self.watcher.cancelled() and self.watcher.exception() is not None:
                log.error("the stop_when_done watcher of session %s ended with an error", self.swarm.session_id,
                          exc_info=self.watcher.exception())
        if self.auto_reply is not None:
            try:
                await self.auto_reply.stop()
            except Exception:
                log.exception("could not stop the automatic answers of session %s", self.swarm.session_id)


class SessionManager:
    # what _public_text writes for a path in another session's folder (whose name is that session's title)
    OTHER_SESSION = "(a folder of another session)"

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
        # sessions _unload took out of `swarms` whose database is not closed yet: readers in threads use it (reading)
        self._unloading: dict[str, "Swarm"] = {}
        self.running: set[str] = set()
        self.hub_url: str | None = None        # base URL of the web server (no trailing /), set by launcher.serve
        self._locks: dict[str, asyncio.Lock] = {}  # kept after a delete: a waiter may still hold the old lock
        # one workspace change at a time in the whole home (taken inside a session's lock, from the final check of
        # the new folder until session.json names it): two sessions never take the same folder
        self._claims = asyncio.Lock()
        # the folders new sessions are being created in with `workspace` (resolved): a second create, or a workspace
        # change, into the same folder (inside it, containing it) is refused at once instead of copying everything
        # first and failing at the claim
        self._creating_in: set[Path] = set()
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        # the waiters of each session (watch): woken by a journaled event of the session, a change (_changed) and the
        # end of an operation that held its lock (_SessionLock); each with the loop that waits on it
        self._wakers: dict[str, dict[asyncio.Event, asyncio.AbstractEventLoop]] = {}
        # the sessions resume() is about to start (docs/SPEC.md §21.7 `final`): filled before the launcher takes its
        # first request (plan_resume), emptied one by one as resume() handles them
        self.resume_pending: set[str] = set()
        self._resume_planned: list[str] | None = None
        # the sessions a create or a v1 `continue` is starting (mark_starting .. unmark_starting; is_final reads it),
        # and the marks of each: one per start on its way, so a start ends its own mark only
        self.starting: set[str] = set()
        self._marks: dict[str, set[object]] = {}
        # the hub's own readers of a session's files while they are sent (the HTTP API v1's downloads and archives,
        # each with cut(); add_reader .. drop_reader): delete() cuts them before it renames the session's folder away
        # (Windows renames no folder that holds an open file), and none is added while it runs (`_deleting`)
        self._readers: dict[str, set[Any]] = {}
        self._deleting: set[str] = set()
        # what runs beside a started v1 session, tied to its Swarm (the hub's automatic answers, the stop_when_done
        # watcher; docs/SPEC.md §21.4, §21.5): started by _start_locked, stopped by _unload before the swarm stops
        self._run_watch: dict[str, _RunWatch] = {}
        # the stops stop_when_done scheduled (detached: _unload cancels the watcher that scheduled them); none is
        # scheduled while shutdown() runs (`_closing`): the resume check at the next start handles those jobs
        self._stopping: set[asyncio.Task] = set()
        self._closing = False
        # uid -> sid of every session, the sids of the v1 sessions and client_ref -> sid of those (sid_of_uid,
        # api_sids, sid_of_client_ref), with the sessions folder's mtime and the session folders they were read from;
        # None: read again at the next use
        self._index: tuple[int | None, frozenset[str], dict[str, str], list[str], dict[str, str]] | None = None
        # zips the sessions page uploads for a new session's copy (POST /api/uploads, copy_from_upload)
        self.uploads = Uploads(self.home / ".dreamteam" / UPLOADS_DIR)
        # the files of the HTTP API v1 (POST /api/v1/uploads, docs/SPEC.md §21.3): a root of their own, so a token
        # of one store is never usable in the other
        self.api_uploads = Uploads(self.home / ".dreamteam" / UPLOADS_DIR / API_UPLOADS_DIR)

    # ---- change notifications ------------------------------------------------------------------
    def listen(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(listener)

    def unlisten(self, listener: Callable[[dict[str, Any]], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _changed(self, sid: str | None, change: str) -> None:
        if change in ("created", "deleted"):
            self._index = None   # a session came or went (sid_of_uid, api_sids)
        if sid is not None:
            self._poke(sid)
        event = {"type": "sessions.changed", "session": sid, "change": change, "ts": time.time()}
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                log.exception("session listener failed")

    def _lock(self, sid: str) -> asyncio.Lock:
        lock = self._locks.get(sid)
        if lock is None:
            lock = self._locks[sid] = _SessionLock(lambda: self._poke(sid))
        return lock

    # ---- waiting for a session's changes (docs/SPEC.md §21.6) -----------------------------------------
    @contextlib.contextmanager
    def watch(self, sid: str) -> Iterator[asyncio.Event]:
        """A waiter for any change of session `sid`: an asyncio.Event set by a new journaled event of the session
        (one bus listener per Swarm, installed by _load_locked), a change the manager makes (_changed: created,
        started, stopped, updated, deleted), the end of an operation that held the session's lock, and resume()
        handling it. It is keyed by session, never by a bus: it outlives an unload, which replaces the Swarm and its
        bus. Clear the event before each read, then wait on it:

            with manager.watch(sid) as changed:
                while ...:
                    changed.clear()
                    ... read ...
                    await asyncio.wait_for(changed.wait(), timeout)

        Call it on the event loop that waits (a poke from another thread is handed to that loop)."""
        event = asyncio.Event()
        waiters = self._wakers.setdefault(sid, {})
        waiters[event] = asyncio.get_running_loop()
        try:
            yield event
        finally:
            waiters = self._wakers.get(sid)
            if waiters is not None:
                waiters.pop(event, None)
                if not waiters:
                    self._wakers.pop(sid, None)

    def _poke(self, sid: str) -> None:
        """Wake every waiter of session `sid` (watch). It only sets events: no database work, no await, no emit."""
        waiters = self._wakers.get(sid)
        if not waiters:
            return
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        for event, loop in list(waiters.items()):
            if loop is current:
                event.set()
            elif not loop.is_closed():
                with contextlib.suppress(RuntimeError):   # its loop closed meanwhile
                    loop.call_soon_threadsafe(event.set)

    def is_final(self, sid: str) -> bool:
        """The session is stopped and will not come back by itself (docs/SPEC.md §21.7): it is not running, not being
        started (`starting`: a create or a `continue` in flight; `resume_pending`: resume() is about to start it), and
        no operation holds its lock (_unload takes it out of `running` before it journals `session.stopped`). The
        autostart mark does not count: resume() skips finished and earlier-version sessions but keeps their mark."""
        if sid in self.running or sid in self.starting or sid in self.resume_pending:
            return False
        lock = self._locks.get(sid)
        return lock is None or not lock.locked()

    def mark_starting(self, sid: str, mark: object | None = None) -> object:
        """A start of `sid` is on its way (a create with start=True, a v1 create or `continue` that starts it in the
        background): the session is `starting` (docs/SPEC.md §21.7), so never seen final, until that start ends.
        Returns the mark (`mark`, else a new object): `start(sid, marked=mark)` starts the session only while the mark
        is there and takes it away when it ends; unmark_starting(sid, mark) takes it away without a start. A delete
        meanwhile takes every mark of the session away, a stop the marks placed before it was asked (_stop), and those
        starts then start nothing. Each start has its own mark: one that ends never takes away another's (a second
        caller's, or a new session's that got the folder name of a deleted one)."""
        mark = object() if mark is None else mark
        self._marks.setdefault(sid, set()).add(mark)
        self.starting.add(sid)
        self._poke(sid)
        return mark

    def unmark_starting(self, sid: str, mark: object) -> None:
        """The start `mark` announced (mark_starting) is over, whatever its outcome; nothing when the mark is gone."""
        marks = self._marks.get(sid)
        if marks is None or mark not in marks:
            return
        marks.discard(mark)
        if not marks:
            del self._marks[sid]
            self.starting.discard(sid)
        self._poke(sid)

    def _marked(self, sid: str, mark: object) -> bool:
        return mark in self._marks.get(sid, ())

    def marked_session(self, mark: object) -> str | None:
        """The session the start `mark` is on now (mark_starting), else None (not placed yet, or taken away)."""
        return next((sid for sid, marks in self._marks.items() if mark in marks), None)

    def _drop_marks(self, sid: str, marks: Iterable[object] | None = None) -> None:
        """A delete (`marks` None: every mark) or a stop (`marks`: those placed before it was asked): those starts on
        their way start nothing any more (start(..., marked=...)); the session leaves `starting` with its last mark."""
        held = self._marks.get(sid)
        if held is not None and marks is not None:
            held.difference_update(marks)
            if held:
                return
        self._marks.pop(sid, None)
        self.starting.discard(sid)

    def add_reader(self, sid: str, reader: Any) -> None:
        """`reader` (cut(): it stops and closes what it holds in the session's folder, at once or when its running
        step ends) reads the session's files until drop_reader: a delete of the session cuts it first. SessionError
        409 while a delete of the session runs. On the loop."""
        if sid in self._deleting:
            raise SessionError(f"Session {sid} is being deleted.", 409)
        self._readers.setdefault(sid, set()).add(reader)

    def drop_reader(self, sid: str, reader: Any) -> None:
        """`reader` holds nothing of the session's any more (add_reader)."""
        readers = self._readers.get(sid)
        if readers is not None:
            readers.discard(reader)
            if not readers:
                del self._readers[sid]

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
        A session whose model profile was renamed follows the rename, and so do the members hired on it (agents.model
        of the loaded and the stopped sessions); one whose profile was deleted falls back to the default profile
        (instead of blocking the save)."""
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
            # a renamed profile follows (also in a swap, where its old name is still a profile)
            if isinstance(profile, str) and (profile in renames or profile not in new_config.models):
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
        if renames:  # members hired on a renamed profile keep it (agents.model), before the reconfigure below
            for sid in self.ids():
                swarm = self.swarms.get(sid)
                if swarm is None:  # off the loop (a busy file waits up to its timeout); no load in between
                    async with self._lock(sid):
                        swarm = self.swarms.get(sid)
                        if swarm is None:
                            # a session made by an earlier version keeps its database as it is (§17.2)
                            if not await asyncio.to_thread(self.earlier_version, sid):
                                await asyncio.to_thread(rename_member_profiles, self._data_dir(sid) / "swarm.db",
                                                        renames)
                            continue
                rename = getattr(swarm, "rename_profiles", None)
                try:
                    if rename is not None:
                        rename(renames)
                except Exception:  # the settings are saved; such a member falls back to the default profile
                    log.exception("could not rename the model profiles of the members of session %s", sid)
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

    def _check(self, sid: str, uid: str | None = None) -> None:
        """404 for a session that is not there; with `uid`, also when the folder name `sid` holds another session by
        now: a delete gives its folder number to the next session with the same title slug (_new_id), so a caller
        that named the session by its uid (the HTTP API v1) checks it again after waiting for the session's lock."""
        if not self.exists(sid) or (uid is not None and not self._has_uid(sid, uid)):
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
        """session.json written whole: a temporary file, then put in its place (os.replace). A replace refused with
        PermissionError (Windows: a reader holds the file open for a moment, such as a thread of the hub's that reads
        it) is tried again, META_REPLACE_TRIES times META_REPLACE_PAUSE apart; then the error is raised."""
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
            for attempt in range(META_REPLACE_TRIES):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:
                    if attempt == META_REPLACE_TRIES - 1:
                        raise
                    time.sleep(META_REPLACE_PAUSE)
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
            f"the files together there, then delete {path}.", 409, code=FOLDER_NOT_READY)

    def _check_workspace_ready(self, sid: str, meta: dict[str, Any]) -> None:
        """Before a start: a folder outside the session is never created silently (a deleted project, an unplugged
        drive, a host path inside the Docker sandbox): the agents would work in an empty stand-in. A session that
        works in a server folder of the HTTP API v1 in place (`api.folder`, docs/SPEC.md §21.10 "Starts") is checked
        again too, whoever starts it (_check_server_folder). Blocking (a folder may lie on a share that does not
        answer): _start_locked runs it in a thread, with the settings read again just before."""
        self._check_journal(sid, meta)
        info = self.workspace_of(sid, meta)
        if not (info["default"] or info["exists"]):
            what = "is not a folder" if Path(info["path"]).exists() else "does not exist"
            raise SessionError(
                f"The workspace folder of session {sid} {what}: {info['path']}. Bring it back (e.g. reconnect the "
                f"drive), or move the session to another folder: /cd <folder> (/cd --default for its own folder) in "
                f"`dreamteam` or `dreamteam console --session {sid}`, session_cd in MCP.", 409, code=FOLDER_NOT_READY)
        self._check_server_folder(sid, meta)

    def _check_server_folder(self, sid: str, meta: dict[str, Any], *,
                             refusal: str = "it does not start") -> tuple[Path, Path] | None:
        """Before a start of a session that works in a server folder in place (api.folder mode "in_place",
        docs/SPEC.md §21.10 "Starts"; nothing for any other session): its root must still be listed in `[api]
        folders` with `in_place = true` (the settings as _start_locked read them again: a hand edit counts at once)
        and must not be refused (apifolders.resolve_root; a missing root is 409 `folder_not_ready`), and the stored
        folder must still resolve to itself inside that root, by text and by file identity: a component swapped for a
        link after the create, or a root moved elsewhere, refuses the start (403 `forbidden_folder`). `refusal` ends
        the texts of its own refusals (what does not happen: v1 reading the files passes its own). The git rules of
        the folder are checked apart (_check_server_git). (The root resolved, the folder resolved: on Windows its names
        as the folders on the way spell them, on POSIX as session.json spells them; v1 reads the files below the root
        by the names as their folders list them, apifolders.as_listed, §21.8), None for any other session. Blocking:
        in a thread."""
        folder = apifolders.in_place_folder((api_meta(meta) or {}).get("folder")) if sid != LEGACY_ID else None
        if folder is None:
            return None
        where = apifolders.shown(folder["root"], folder["path"])
        config = self.config   # (never _refresh_settings here: it writes the manager's settings, off the loop)
        root = apifolders.roots(config).get(folder["root"])
        if root is None or not root.in_place:
            raise SessionError(
                f"Session {sid} works in {where}, under the folder root '{folder['root']}', which [api] folders no "
                f"longer lists with in_place = true: {refusal}.", 403, code=FORBIDDEN_FOLDER)
        try:
            root_real = apifolders.resolve_root(root, home=self.home, config=config)
        except CopyError as exc:
            missing = exc.code == "not_found"
            raise SessionError(str(exc), 409 if missing else 403,
                               code=FOLDER_NOT_READY if missing else FORBIDDEN_FOLDER) from None
        stored = Path(self.workspace_of(sid, meta)["path"])
        try:
            real = stored.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise SessionError(f"The folder {where} of session {sid} cannot be read: {refusal}.", 409,
                               code=FOLDER_NOT_READY) from None
        if not (_same_path(real, stored) and is_within(real, root_real)):
            raise SessionError(
                f"The folder {where} of session {sid} no longer leads to itself inside its root (a link or a junction "
                f"on the way, or the root moved): {refusal}.", 403, code=FORBIDDEN_FOLDER)
        return root_real, real

    def _check_server_git(self, sid: str, folder: Path, network_off: bool = False) -> None:
        """Before a start of a session that works in a server folder in place, with git on (docs/SPEC.md §21.10
        `in_place`): DreamTeam's git must write inside the folder only (apifolders.git_dir_refusal: a `.git` that is
        a link or a gitfile, a link or a hard-linked file in its repository's folders, a git dir, common dir or path
        git writes outside it): 409 `folder_not_ready`. Blocking (git): run it in a thread."""
        git_exe = shutil.which("git")
        if git_exe is None:
            return
        problem = apifolders.git_dir_refusal(folder, git_exe, offline=isolated_git(network_off))
        if problem is not None:
            raise SessionError(f"The folder session {sid} works in ({folder}) cannot take DreamTeam's commits: "
                               f"{problem}.", 409, code=FOLDER_NOT_READY)

    def _check_adopted_repo(self, sid: str, folder: Path, mcp: list[str], network_off: bool = False) -> None:
        """Before a start in a folder the session adopted, with git on: the same rules as when it was adopted, since
        the user may have worked in it meanwhile (or git was off then). Its repository must be clean (the agents
        commit with `git add -A`; the session's own `mcp/<name>` folders are left out: they are the agents' work, and
        so is `.scratch/`, which no commit takes and whose marker the start writes again if an agent removed it),
        on a branch, with no merge or rebase halfway, and not inside another repository (a folder that is in no
        repository gets one at the start, as when it was adopted). `network_off`: the session's setting (its git
        then runs isolated, isolated_git). Blocking (git): run it in a thread."""
        git_exe = shutil.which("git")
        if git_exe is None:
            return
        state = repo_state(folder, git_exe, exclude=[*(f"mcp/{name}" for name in mcp), SCRATCH_DIR],
                           offline=isolated_git(network_off))
        refusal = _repo_refusal(folder, state, starting=sid)
        if refusal is not None:
            raise refusal

    def _forged_servers(self, sid: str) -> list[str]:
        """MCP servers whose files live in the session's workspace (forged there or imported from the library)."""
        db = self.dir_of(sid) / ".dreamteam" / "swarm.db"
        if not db.exists():
            return []
        try:
            conn = sqlite3.connect(sqlite_uri(db, "mode=ro"), uri=True, timeout=5.0)
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

    def _target_refusal(self, target: Path, sid: str | None) -> str | None:
        """Why the folder `target` (resolved) can never be the workspace of session `sid` (None: a session about to
        be created; an id that does not exist yet is fine): a drive root, the user's home or a folder containing it,
        the DreamTeam home or a folder containing it, DreamTeam's own data folder, the sessions folder (except the
        session's own default folder), a git database, another DreamTeam home or a folder inside one, the hub tokens'
        folder (inside or containing it), on POSIX /proc, /sys and /dev, the MCP and skills libraries and the skill
        environments. None when it can be."""
        try:
            home = self.home.resolve()
            sessions = self.sessions_dir.resolve()
            user_home = Path.home().resolve()
            default_real = self.default_workspace(sid).resolve() if sid is not None else None
            session_dir = self.dir_of(sid).resolve() if sid is not None else None
        except (OSError, ValueError, RuntimeError) as exc:
            return f"Cannot use the folder {target}: {exc}"
        # compared as text and by file identity: a file system that ignores case where normcase does not (macOS)
        def within(path: Path, parent: Path) -> bool:
            return _within(path, parent) or is_within(path, parent)

        def same(a: Path, b: Path) -> bool:
            return _same_path(a, b) or (is_within(a, b) and is_within(b, a))

        is_default = default_real is not None and same(target, default_real)
        if target.parent == target:
            return f"{target} is the root of a drive or of the file system; choose a folder inside it."
        if same(target, user_home):
            return f"{target} is your home folder itself; choose a folder inside it, e.g. {target / 'my-project'}."
        if within(user_home, target):
            return f"{target} contains your home folder {user_home}; choose a folder inside it."
        if same(target, home) or within(home, target):
            return f"{target} is the DreamTeam home or contains it; choose a folder outside {home}."
        if within(target, home / ".dreamteam"):
            return f"{target} is inside DreamTeam's own data folder {home / '.dreamteam'}."
        if within(target, sessions) and not is_default:
            return (f"{target} is inside the DreamTeam sessions folder {sessions}; choose a folder outside it (or "
                    "--default for the session's own folder).")
        if session_dir is not None and within(target, session_dir / ".dreamteam"):
            return f"{target} is inside the session's data folder."
        if any(part.casefold() == ".git" for part in target.parts) or looks_like_git_dir(target):
            return (f"{target} is a git repository's own database (a .git folder or a bare repository), not a folder "
                    "of files: choose the folder of its work tree.")
        # (this home and its sessions folder: above; the session's own folder is its own whatever its agents built
        # there, even a home's layout: /cd --default always leads back)
        other = _other_home(target) if not is_default else None
        if other is not None and not same(other, home):
            return (f"{target} is a DreamTeam home ({other}) or inside one: its settings (API keys too) and sessions "
                    "are never a session's folder; choose another folder.")
        with contextlib.suppress(OSError, ValueError, RuntimeError):
            tokens = hubauth.tokens_dir().resolve()
            if within(target, tokens) or within(tokens, target):
                return (f"{target} is the folder of DreamTeam's hub tokens {tokens}, or inside it or contains it; "
                        "choose another folder.")
        if os.name != "nt":
            for system in _SYSTEM_ROOTS:
                if _within(target, Path(system)):
                    return f"{target} is a system folder ({system}); choose another folder."
        libraries = [self.config.library_dir]
        if sid is not None and os.path.lexists(self.dir_of(sid) / SESSION_FILE):
            with contextlib.suppress(ValueError, SessionError):
                libraries.append(self._config_from(self.raw, sid).library_dir)
        for library in libraries:
            try:
                library = library.resolve()
            except (OSError, ValueError, RuntimeError):
                continue
            if _within(target, library) or _within(library, target):
                return (f"{target} is DreamTeam's shared MCP library {library}, or inside it or contains it (every "
                        "session imports its servers from there); choose another folder.")
        try:  # the skills library: agents would write into the skills every session attaches from
            skills_library = self.config.skills_library_dir.resolve()
        except (OSError, ValueError, RuntimeError):
            skills_library = None
        if skills_library is not None and (_within(target, skills_library) or _within(skills_library, target)):
            return (f"{target} is DreamTeam's shared skills library {skills_library}, or inside it or contains it "
                    "(sessions attach their skills from there); choose another folder.")
        try:  # the skills' environments: agents' files and autocommit's `git add -A` would reach every session's
            envs = self.config.skill_envs_dir.resolve()
        except (OSError, ValueError, RuntimeError):
            envs = None
        if envs is not None and (_within(target, envs) or _within(envs, target)):
            return (f"{target} is DreamTeam's skill environments folder {envs}, or inside it or contains it (every "
                    "session runs its skills' packages from there); choose another folder.")
        return None

    def _check_reserved(self, target: Path) -> None:
        """409 when a session is being created right now (create with `workspace`) in the resolved folder `target`,
        inside it or in a folder containing it: two sessions cannot share files, and the second would learn it only
        after a copy of its own."""
        for folder in tuple(self._creating_in):
            if _overlaps(target, folder):
                how = "" if _fold_key(target) == _fold_key(folder) else f" ({target} is inside it or contains it)"
                raise SessionError(f"Another session is being created in {folder} right now{how}; when it is done, "
                                   "that folder is taken.", 409, code=FOLDER_BUSY)

    def _plan(self, sid: str | None, path: Any, *, git_on: bool, mcp: list[str] | None = None,
              network_off: bool = False, copying: bool = False, within: Path | None = None) -> _Plan:
        """Check a new workspace folder for a session (sid None: a session about to be created) and decide what
        to do; SessionError (400 bad target, 409 conflict) before anything changes. `network_off`: the session's
        setting, for its git checks here and later (_Plan.network_off; the first check runs while the session's
        members may still run commands). `copying`: files are copied into the new session (create's copy_from),
        so a folder with files is refused (409). `within`: the resolved root of a server folder of the HTTP API v1
        (docs/SPEC.md §21.10): the folder, once resolved here, must lie inside it by text and by file identity (403
        `forbidden_folder`: a link swapped in on the way since the request was resolved), and with git on its
        repository must keep DreamTeam's git inside it (apifolders.git_dir_refusal: 409 `folder_not_ready`). A
        folder another session works in, inside it or containing it, is compared by text and by file identity
        (treecopy.is_within: another letter case or Unicode form of a name is the same folder where the file system
        says so). Blocking (git): run it in a thread."""
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
            old_real = old.resolve() if old is not None else None
            default_real = default.resolve() if default is not None else None
            session_dir = self.dir_of(sid).resolve() if sid is not None else None
        except (OSError, ValueError, RuntimeError) as exc:
            raise SessionError(f"Cannot use the folder {path}: {exc}") from None
        if within is not None and not is_within(target, within):   # (named as asked: never where it leads)
            asked = os.fspath(path).strip() if path is not None else target
            raise SessionError(f"{asked} does not lie inside its folder root any more: a link or a junction on the "
                               "way leads out of it.", 403, code=FORBIDDEN_FOLDER)
        is_default = default_real is not None and _same_path(target, default_real)
        refusal = self._target_refusal(target, sid)
        if refusal is not None:
            raise SessionError(refusal)
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
            # by file identity too: a file system that ignores case or Unicode form where normcase does not (APFS,
            # a casefold mount) takes jobs/Acme for jobs/acme
            inside, holds = is_within(target, folder), is_within(folder, target)
            if inside or holds:
                how = "is the folder" if inside and holds else ("is inside the folder" if inside else
                                                                "contains the folder")
                raise SessionError(f"{target} {how} of session {other} ({folder}); two sessions cannot share files.",
                                   409, code=FOLDER_BUSY, holder=other)
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
                     old_is_default=old_stored is None and old is not None, old_adopted=old_adopted,
                     network_off=network_off, within=within)
        carried = [name for name in (mcp or []) if old_exists and (old_real / "mcp" / name).is_dir()]
        if old_adopted and carried and git_exe:  # leaving the user's folder: did the agents commit the servers there?
            plan.old_tracked = tracked(old_real, [f"mcp/{name}" for name in carried], git_exe,
                                       offline=isolated_git(network_off))
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
        if copying and not empty:
            raise SessionError(f"{target} already has files{_some_entries(target)}: a copy goes into a new or empty "
                               f"folder. Choose an empty folder, or leave out the copy to work with the files in "
                               f"{target} itself.", 409)
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
            if within is not None:   # a server folder: DreamTeam's git stays inside it (checked before git reads it)
                problem = apifolders.git_dir_refusal(target, git_exe, offline=isolated_git(network_off))
                if problem is not None:
                    raise SessionError(f"The repository in {target} cannot take DreamTeam's commits: {problem}.",
                                       409, code=FOLDER_NOT_READY)
            # the session's own servers (absent there: checked above) are left out, as at every start: a folder it
            # worked in before may track them, and their move brings them back (else it could never return there)
            state = repo_state(target, git_exe, exclude=[f"mcp/{name}" for name in carried],
                               offline=isolated_git(network_off))
            refusal = _repo_refusal(target, state)
            if refusal is not None:
                raise refusal
            plan.git = "initialized" if state["toplevel"] is None else "existing"
        return plan

    def _carry_out(self, sid: str | None, plan: _Plan, undo: _Undo) -> str:
        """Do what the plan says (blocking: run it in a thread). Returns the git outcome; on an exception `undo`
        knows how to put everything back. A server folder's plan (`within`, docs/SPEC.md §21.10) finds the deepest
        level that is there and each level it makes right after making it inside its root again (_check_inside, 403
        `forbidden_folder`): the levels are made by path, so a link swapped in on the way since the last _plan ends
        the change, and the undo removes each level it made while its path still names it (its identity). Git init
        there runs in the folder held (_init_held), and what it made is removed only from that folder."""
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
        if plan.within is not None:
            _check_inside(folder, plan.within)
        for folder in reversed(missing):
            folder.mkdir()
            undo.created.append(folder)
            if plan.within is not None:
                try:
                    undo.made[folder] = _identity(os.lstat(folder))
                except OSError:
                    undo.made[folder] = None
                _check_inside(folder, plan.within)
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
            names = [".git"] if os.path.lexists(plan.target / ".gitignore") else [".git", ".gitignore"]
            if plan.within is not None:
                _init_held(plan, plan.within, undo, names)
                return plan.git
            undo.gitignore = plan.target / ".gitignore" if ".gitignore" in names else None
            undo.git_dir = plan.target / ".git"
            error = init_repo(plan.target, adopted=plan.adopted, offline=isolated_git(plan.network_off, plan.target))
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

    def validated_overrides(self, overrides: Any) -> dict[str, Any]:
        """The overrides a new session would be created with: cleaned (clean_overrides) and checked against the
        global settings (SessionError 400 "Invalid session settings: ..." otherwise); create's own check, for callers
        that refuse a request before anything else (the HTTP API v1, docs/SPEC.md §21.4 "Order")."""
        clean = self.clean_overrides(overrides)
        try:
            validate(self.sessions_dir / "new", self._merged(self.raw, clean))
        except ValueError as exc:
            raise SessionError(f"Invalid session settings: {exc}") from None
        return clean

    # ---- summaries ----------------------------------------------------------------------------
    def summary(self, sid: str, *, workspace: bool = True, requests: bool = False) -> dict[str, Any]:
        """The session as the board and the clients show it. `workspace`: False leaves its "workspace" None, which
        otherwise looks at the folder the session works in (workspace_of: it may lie on a share that does not answer),
        for a caller that never shows it, on the event loop (the HTTP API v1's summary, docs/SPEC.md §21.7).
        `requests`: True adds "requests", how many requests to the user wait (read_stats), for the HTTP API v1's
        summary only; list() and default_session() leave it out, so they never pay for its queries."""
        self._check(sid)
        meta = self.meta(sid)
        try:
            cfg = self._config_from(self.raw, sid, meta.get("overrides") or {})
            config_error = None
        except ValueError as exc:
            cfg, config_error = self.config, str(exc)
        swarm = self.swarms.get(sid)
        db_path = cfg.data_dir / "swarm.db" if config_error is None else self.dir_of(sid) / ".dreamteam" / "swarm.db"
        clarify = bool(cfg.swarm.clarify)   # (a clarify planner that waits for the user is a request: read_stats)
        running = sid in self.running        # (a session that does not run counts only the requests that block it)
        if swarm is not None:
            with swarm.db._lock:
                stats = read_stats(db_path, swarm.db.conn, clarify=clarify, running=running, requests=requests)
        else:
            stats = read_stats(db_path, clarify=clarify, running=running, requests=requests)
        limits = stats.pop("limits") or {}
        pending = pending_goal(meta)
        if stats["goal"] is None and pending:
            stats["goal"] = {**pending, "status": "pending", "summary": ""}
        profile = cfg.agents.default_model
        model = cfg.models.get(profile)
        attached = self._attached_skills(sid)
        api = api_meta(meta)
        # a session of the HTTP API v1 says so (the sessions page marks it, `dreamteam -p` sends no automatic replies
        # of its own to an `auto` one); other sessions have neither key
        made_by_api = {} if api is None else {"via": API_VIA, "answers": api_answers(api)}
        return {
            "id": sid, "uid": session_uid(meta), "title": meta.get("title") or sid, "legacy": meta.get("legacy", False),
            "dir": str(self.dir_of(sid)), "created_at": meta.get("created_at"),
            "status": "running" if sid in self.running else "stopped",
            "loaded": swarm is not None, "autostart": bool(meta.get("autostart")),
            "language": cfg.swarm.language, "model_profile": profile, "model": model.model if model else None,
            "network": cfg.swarm.network,   # "off": the agents' commands have no internet (docs/SPEC.md §7.2)
            "clarify": cfg.swarm.clarify,   # the planner asks the user first (docs/SPEC.md §3 "Clarify mode")
            "max_cost_usd": _number(limits.get("max_cost_usd", cfg.swarm.max_cost_usd)),
            "max_agents": int(_number(limits.get("max_agents", cfg.swarm.max_agents))),
            "overrides": meta.get("overrides") or {}, "config_error": config_error,
            "url": f"/s/{sid}/", "workspace": self.workspace_of(sid, meta) if workspace else None,
            "copied_from": _copied_from(meta.get("copied_from")),   # files copied in at create (copyfrom.py)
            "skills": [s.name for s in attached],
            "skills_pending": [s.name for s in attached if s.library == "pending"],
            **stats,
            **made_by_api,
        }

    def list(self) -> list[dict[str, Any]]:
        items = []
        for sid in self.ids():
            try:
                items.append(self.summary(sid))
            except SessionError as exc:
                if exc.status != 404:  # 404: deleted meanwhile
                    items.append({"id": sid, "uid": None, "title": sid, "status": "stopped", "error": str(exc)})
            except Exception as exc:  # one broken session must not break the sessions page
                log.exception("cannot summarize session %s", sid)
                items.append({"id": sid, "uid": None, "title": sid, "status": "stopped",
                              "error": f"{type(exc).__name__}: {exc}"})
        items.sort(key=lambda s: (s.get("status") != "running",
                                  -_number(s.get("last_activity") or s.get("created_at"))))
        return items

    def default_session(self) -> str | None:
        """The session MCP calls without `session` go to (and `serve`'s console): the only running one, else the most
        recently active running one, else the only session this version opens (one made by an earlier version is
        never it). Never one created through the HTTP API v1 (instruct.without_api_sessions, as `-c` and the CLI's
        other unnamed picks): an API client's job would otherwise take the owner's calls; `session=` reaches it."""
        running = [s for s in without_api_sessions(self.list()) if s.get("status") == "running"]
        if running:
            return running[0]["id"]
        api = set(self.api_sids())
        ids = [sid for sid in self.ids() if sid not in api and not self.earlier_version(sid)]
        return ids[0] if len(ids) == 1 else None

    # ---- the HTTP API v1's lookups (docs/SPEC.md §21.1 "Scope", §21.2 `{id}`) ----------------------------------
    def _sessions_stamp(self) -> int | None:
        try:
            return self.sessions_dir.stat().st_mtime_ns
        except OSError:
            return None

    def _uid_index(self, *, recount: bool = False) -> tuple[dict[str, str], list[str], dict[str, str]]:
        """(uid -> sid, the sids of the v1 sessions, client_ref -> sid of those: the first by name when two share
        one), read from every session.json once and kept until a session comes or goes: here (_changed "created" /
        "deleted", an import's rewrite) or in another process (the sessions folder's mtime). `recount`: also compare
        the session folders (ids(): no session.json is read) with those it was read from, for a uid it does not know
        and for api_sids(): another process makes a session's folder before it writes its session.json, which
        changes no mtime of the sessions folder. A uid two sessions share (a folder copied by hand) names neither: a
        v1 id must never reach another job. The legacy main session has no uid; a damaged session.json is left
        out."""
        stamp = self._sessions_stamp()
        index = self._index
        ids: frozenset[str] | None = None
        if index is not None and index[0] == stamp:
            if not recount:
                return index[2], index[3], index[4]
            ids = frozenset(self.ids()) - {LEGACY_ID}
            if ids == index[1]:
                return index[2], index[3], index[4]
        if ids is None:
            ids = frozenset(self.ids()) - {LEGACY_ID}
        owners: dict[str, list[str]] = {}
        made_by_api: list[str] = []
        refs: dict[str, str] = {}
        for sid in sorted(ids):
            try:
                meta = self.meta(sid)
            except SessionError:
                continue
            uid = session_uid(meta)
            if uid is not None:
                owners.setdefault(uid, []).append(sid)
            api = api_meta(meta)
            if api is not None:
                made_by_api.append(sid)
                if isinstance(api.get("client_ref"), str):
                    refs.setdefault(api["client_ref"], sid)
        by_uid = {}
        for uid, sids in owners.items():
            if len(sids) == 1:
                by_uid[uid] = sids[0]
            else:
                log.warning("sessions %s share the uid %s: the HTTP API reaches neither", ", ".join(sids), uid)
        self._index = (stamp, ids, by_uid, made_by_api, refs)
        return by_uid, made_by_api, refs

    def _has_uid(self, sid: str, uid: str) -> bool:
        try:
            return self.exists(sid) and session_uid(self.meta(sid)) == uid
        except SessionError:
            return False

    def sid_of_uid(self, uid: Any) -> str | None:
        """The folder name of the session whose uid is `uid` (32 lowercase hex characters, session_uid), else None:
        an unknown or malformed uid, a deleted session (its folder number may belong to a new one by now), the
        legacy main session (no uid). A cached answer is checked against the session.json it names; a uid the index
        does not know is looked up again when the session folders changed since it was read (_uid_index's
        `recount`)."""
        if not isinstance(uid, str) or not _UID.fullmatch(uid):
            return None
        sid = self._uid_index()[0].get(uid)
        if sid is not None and self._has_uid(sid, uid):
            return sid
        if sid is None:   # a session.json written since (another process): read again when the folders differ
            sid = self._uid_index(recount=True)[0].get(uid)
        else:   # changed behind the index: read it again once
            self._index = None
            sid = self._uid_index()[0].get(uid)
        return sid if sid is not None and self._has_uid(sid, uid) else None

    def api_sids(self) -> list[str]:
        """The folder names of the sessions created through the HTTP API v1 (session.json "via": "api"), by name,
        read from session.json alone (no summary): the sessions an API-token caller may see. The index is read again
        when the session folders changed since it was read. One deleted meanwhile by another process may still be
        named; its summary then says it is unknown (404)."""
        return list(self._uid_index(recount=True)[1])

    def sid_of_client_ref(self, ref: Any) -> str | None:
        """The folder name of the v1 session created with client_ref `ref` (docs/SPEC.md §21.4), else None, from the
        index (_uid_index, read again when the session folders changed since: no session.json read per session at
        each create), its answer checked against the session.json it names and read again once when that changed
        behind it. A client_ref never changes after the create."""
        if not isinstance(ref, str):
            return None
        sid = self._uid_index(recount=True)[2].get(ref)
        if sid is None or self._has_client_ref(sid, ref):
            return sid
        self._index = None
        sid = self._uid_index()[2].get(ref)
        return sid if sid is not None and self._has_client_ref(sid, ref) else None

    def _has_client_ref(self, sid: str, ref: str) -> bool:
        try:
            api = api_meta(self.meta(sid)) if self.exists(sid) else None
        except SessionError:
            return False
        return api is not None and api.get("client_ref") == ref

    def earlier_version(self, sid: str) -> bool:
        """The session is not loaded and its database was made by an earlier version of DreamTeam (§17.2): this one
        does not open it. False for a loaded session (it passed the check) and when its database cannot be read."""
        if sid in self.swarms:
            return False
        try:
            return made_by_earlier_version(self._data_dir(sid) / "swarm.db")
        except (sqlite3.Error, OSError) as exc:
            log.debug("cannot tell whether an earlier version made session %s: %s", sid, exc)
            return False

    def earlier_version_text(self, sid: str) -> str:
        """The message of a session made by an earlier version (module earlier_version_text)."""
        return earlier_version_text(sid, self.home)

    def _refuse_earlier(self, sid: str) -> None:
        """409 with the message when the session was made by an earlier version: it is not opened or changed."""
        if self.earlier_version(sid):
            raise SessionError(self.earlier_version_text(sid), 409, earlier_version=True)

    def journal(self, sid: str, after: int, limit: int, types: tuple[str, ...] | None = None, *,
                recipient: str | None = None) -> tuple[list[dict[str, Any]], int, bool]:
        """One page of the session's journal (events.read_page: (events, last, more); docs/SPEC.md §21.6 "Polling").
        A loaded session's, or one being unloaded until its database is closed (reading), through its own connection,
        under its Database's lock (never the session's lock, which a start, a stop or a folder move holds for long);
        any other's from its file, read-only, opened and closed for this read (db.reading_file), never through load(),
        which would build and keep a whole Swarm; from the file also when an unload closed the connection meanwhile
        (sqlite3.ProgrammingError). The rows' data is decoded after the Database's lock is released (the event loop
        takes it too). A session without a database has no events (head 0). A file that cannot be read is SessionError
        503 (JOURNAL_TEXT, the cause logged), never an empty page, which would say "nothing more" to a client that has
        missed events. Blocking: call it in a thread."""
        swarm = self.reading(sid)
        page = None
        if swarm is not None:
            try:
                with swarm.db._lock:
                    page = read_page(swarm.db.conn, after, limit, types, recipient=recipient, decode=False)
            except sqlite3.ProgrammingError as exc:   # (unloaded meanwhile: its connection is closed)
                log.debug("session %s was unloaded while its journal was read (%s): read from its file", sid, exc)
        if page is None:
            path = self._data_dir(sid) / "swarm.db"
            if not path.is_file():
                return [], 0, False
            try:
                with reading_file(path, timeout=2.0) as (conn, _immutable):
                    page = read_page(conn, after, limit, types, recipient=recipient, decode=False)
            except (sqlite3.Error, OSError) as exc:
                log.info("cannot read the journal of session %s: %s", sid, exc)
                raise SessionError(JOURNAL_TEXT, 503) from None
        events, last, more = page
        for event in events:
            event["data"] = event_data(event["data"])
        return events, last, more

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
        `skill_md` and `deps` of that session copy, and `install` (the switches its preview line is worded by).
        Refused (409) for a session made by an earlier version."""
        self._check(sid)
        self._refuse_earlier(sid)
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
        SessionError with the status (404 unknown, 409 not pending, a library clash or a session made by an earlier
        version)."""
        self._check(sid)
        self._refuse_earlier(sid)
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
            # a session of an earlier version has no Skills tab here (never opened): its export keeps the proposals
            first = ("export the session first (the export keeps its proposals)" if self.earlier_version(sid) else
                     "decide them in the session's Skills tab first")
            raise SessionError(f"The library already has other skills named {', '.join(clashes)}: {first}, or "
                               f"delete session {sid} with its proposals discarded.",
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
                     via: str = "home", auto_reply: bool = False, copy_from: str | os.PathLike | None = None,
                     copy_from_upload: str | None = None, api: dict[str, Any] | None = None,
                     sources: list[Source] | None = None, mark: object | None = None,
                     within: Path | None = None,
                     folder_threads: Callable[..., Awaitable[asyncio.Future[Any]]] | None = None) -> dict[str, Any]:
        """Create a session (and start it with the goal unless start=False). Returns its summary.
        `workspace`: an absolute folder to work in instead of the session's own one (the rules of set_workspace:
        an empty or missing folder is used as a new workspace, one with files is adopted); the summary then has
        "workspace_result" (set_workspace's result, mode "created" for a new folder).
        `skills`: skills attached before the first start, references (`x`, `claude-user:x`, `claude-project:x` from
        the `workspace` folder) or sources (absolute folder or .zip paths, Git URLs: imported into the library, `by`
        the user, `via` the surface); all of them are resolved and staged before anything is created.
        `auto_reply`: a `dreamteam -p` client with automatic replies asks (models.AUTO_REPLY_HEADER): see
        _start_locked.
        `copy_from`: an absolute folder or .zip path on this computer, copied into the folder the session works in
        before anything else is created (docs/SPEC.md §17.2 "Copying files into a new session"); `copy_from_upload`:
        the token of a zip uploaded with POST /api/uploads instead, claimed before anything else: a create that
        names it uses it up, whether it succeeds or not. Not both. With `workspace`, that folder must be
        empty or missing. The summary then has "copy_result" (copyfrom.copy_into's result).
        `sources`: the HTTP API v1's files instead (docs/SPEC.md §21.4), already claimed by the caller, which also
        settles the claims (copyfrom.Uploads.settle): copied together by copyfrom.copy_sources (its result is the
        "copy_result"); not with copy_from or copy_from_upload. A CopyError keeps its code (`bad_name`,
        `file_conflict`) in the SessionError. `folder_threads`: with `sources`, where their copy runs instead of the
        loop's default executor (_in_thread's `threads`): v1 passes its folder threads when a server folder is among
        them (docs/SPEC.md §21.1 "Threads"), so a share that stops answering in the middle of the copy holds none of
        the threads the rest of the hub shares, nor its exit.
        `api`: the session is the HTTP API v1's: session.json gets "via": "api" and the "api" block (the keys of
        API_GIVEN: answers, stop_when_done, client_ref, request_digest, folder; the others null) in its first write,
        before any start (see _start_locked for what every start does with it).
        With `start`, the session is `starting` (mark_starting) from its first write of session.json (which lists it)
        until its start ends. `mark`: a start mark of the caller's (any object): the new session is `starting` with it
        from its first write of session.json, and with start=False the caller starts it later (in the background:
        v1's create, docs/SPEC.md §21.4) with `start(sid, marked=mark)`, so nobody sees it final in between; a create
        that fails after the mark takes it away. The session's lock is held from that first write until the session
        is made (its skills committed, its folder set), so a stop, a start, a folder change or a delete asked
        meanwhile waits for the create (a stop or a delete then takes the mark away: the start starts nothing).
        `within`: with `workspace`, the resolved root of the HTTP API v1's server folder it lies in (docs/SPEC.md
        §21.10 `in_place`): every check of the folder on the create's way (_plan, the last one under the home's
        workspace claims just before the missing folders are made) finds it inside that root again, by text and by
        file identity, or refuses the create (403 `forbidden_folder`)."""
        # the goal split as set_goal splits it: its first line also names the session (cut at a word)
        goal_title, goal_description = split_goal(_text(goal), _text(description))
        title = (_one_line(title) or shorten_title(goal_title, 60) or "Session")[:TITLE_LIMIT].strip()
        self._refresh_settings()
        if sources is not None and (copy_from is not None or copy_from_upload is not None):
            # before an upload is claimed: nothing is used up for a call that can never work
            raise SessionError("Give sources, copy_from or copy_from_upload, not more than one.")
        api_block = _new_api_block(api) if api is not None else None
        sources = list(sources) if sources else None
        copying = copy_from is not None or copy_from_upload is not None or sources is not None
        source: Source | None = None
        prepared: list[_SkillItem] = []
        staging: Path | None = None
        copied: dict[str, Any] | None = None
        claimed: Path | None = None
        reserved: Path | None = None
        try:
            if copy_from_upload is not None:
                # claimed first: a create that names an upload uses it up, whatever refuses the create later (the
                # sessions page uploads again for its next try)
                try:
                    claimed = self.uploads.claim_path(copy_from_upload)   # known before the claim runs
                    source = await _in_thread(self.uploads.claim, copy_from_upload, claimed)
                except CopyError as exc:
                    if copy_from is None:   # (with both, "not both" is the answer)
                        raise SessionError(str(exc), exc.status) from None
            if copy_from is not None and copy_from_upload is not None:
                raise SessionError("Give copy_from or copy_from_upload, not both.")
            overrides = self.validated_overrides(overrides)
            if copy_from is not None:   # the source is refused early, before any work
                text = os.fspath(copy_from) if isinstance(copy_from, os.PathLike) else copy_from
                try:
                    source = await asyncio.to_thread(resolve_source, text, home=self.home, config=self.config,
                                                     sandbox=hub_in_container(self.config))
                except CopyError as exc:
                    raise SessionError(str(exc), exc.status) from None
            if workspace is not None:  # checked before anything is created
                new = validate(self.sessions_dir / "new", self._merged(self.raw, overrides))
                plan = await asyncio.to_thread(self._plan, None, workspace, git_on=new.workspace.git,
                                               network_off=netoff.is_off(new), copying=copying, within=within)
                if source is not None and copy_from is not None:
                    try:
                        check_target(source, plan.target)
                    except CopyError as exc:
                        raise SessionError(str(exc), exc.status) from None
                self._check_reserved(plan.target)   # (checked and added with no await between)
                self._creating_in.add(plan.target)
                reserved = plan.target
            if skills:
                if isinstance(skills, list) and len(skills) > self.config.skills.max_attached:
                    raise SessionError(f"At most {self.config.skills.max_attached} skills can be attached to a "
                                       "session ([skills] max_attached).")
                prepared = await asyncio.to_thread(self._prepare_skills, self.config, skills,
                                                   Path(workspace) if workspace is not None else None)
            if copying:
                try:
                    if source is None and sources is None:   # (resolved or claimed above)
                        raise SessionError("Nothing to copy.", 500)
                    self.sessions_dir.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(self.clean_copy_leftovers)
                    staging = self.sessions_dir / staging_name()
                    cancel = threading.Event()

                    def check() -> None:
                        if cancel.is_set():
                            raise CopyCancelled()

                    limits = CopyLimits.from_config(self.config)
                    if sources is not None:
                        copied = await _in_thread(copy_sources, sources, staging, limits, check, on_cancel=cancel.set,
                                                  threads=folder_threads)
                    else:
                        copied = await _in_thread(copy_into, source, staging, limits, check, on_cancel=cancel.set)
                except CopyError as exc:
                    raise SessionError(str(exc), exc.status, code=exc.code) from None
            summary = await self._create(title, goal_title, goal_description, overrides, start, actor, workspace,
                                         prepared, via, auto_reply=auto_reply,
                                         copied=(staging, copied) if staging is not None and copied is not None
                                         else None, api=api_block, mark=mark, within=within)
        finally:
            if reserved is not None:
                self._creating_in.discard(reserved)
            _discard(prepared)   # (whatever a failure left staged)
            if claimed is not None and copy_from_upload is not None:
                # used once, whatever happened (nothing when the claim never renamed it); synchronous: no await
                # after a success, so a cancellation cannot turn a created session into an error
                self.uploads.release(copy_from_upload, claimed)
            if staging is not None:
                try:
                    if os.path.lexists(staging):   # not moved in: a failure before or in _create
                        await _in_thread(_rmtree, staging)
                finally:
                    done_with(staging)   # (what a failed removal left, the next clean removes)
        return summary

    def clean_copy_leftovers(self) -> None:
        """Remove what copies into new sessions left behind: expired uploads and the claims and staging folders of
        processes that no longer run (the launcher calls it at its start and hourly, create before a copy). Best
        effort; blocking."""
        self.uploads.clean()
        self.api_uploads.clean()
        self._clean_copies()

    def _clean_copies(self) -> None:
        """Remove the `.copying-<pid>-<run>-<hex>` staging folders left over (copyfrom.left_over: by processes that
        no longer run, killed while they copied files into a new session, or in the Docker sandbox an earlier
        container's hub with the same pid; or this run's whose removal failed); those concurrent creates of this run
        use and those of other live processes are never touched. Best effort."""
        try:
            entries = list(self.sessions_dir.iterdir())
        except OSError:
            return
        for entry in entries:
            m = COPYING_NAME.match(entry.name)
            if m is None or not left_over(int(m.group(1)), m.group(2), entry.name):
                continue
            try:
                _remove(entry)
            except OSError as exc:
                log.warning("could not remove the stale copy %s: %s", entry, exc)

    async def _create(self, title: str, goal_title: str, goal_description: str, overrides: dict[str, Any],
                      start: bool, actor: str, workspace: str | os.PathLike | None, prepared: list[_SkillItem],
                      via: str, *, auto_reply: bool = False,
                      copied: tuple[Path, dict[str, Any]] | None = None,
                      api: dict[str, Any] | None = None, mark: object | None = None,
                      within: Path | None = None) -> dict[str, Any]:
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
        if copied is not None:
            # the staged copy moves in (one volume: sessions/); _in_thread: the rename always ends before this goes on
            try:
                await _in_thread(_rename_retry, copied[0], path / WORKSPACE_DIR)
            except BaseException as exc:
                await asyncio.to_thread(_rmtree, path)   # nothing half-created stays behind (the copy too, if moved)
                if isinstance(exc, OSError):
                    raise SessionError(f"Could not move the copied files into {path / WORKSPACE_DIR}: {exc}",
                                       500) from None
                raise
        else:
            (path / WORKSPACE_DIR).mkdir(exist_ok=True)
        meta: dict[str, Any] = {"title": title, "uid": new_uid(), "created_at": time.time(), "overrides": overrides,
                                "autostart": False}
        if goal_title:  # set at the first start, when the members exist
            meta["pending_goal"] = {"title": goal_title, "description": goal_description, "actor": actor}
        if copied is not None:   # for the user (the session card, the TUI); the agents are never told the source
            r = copied[1]
            meta["copied_from"] = {"source": r["source"], "files": r["files"], "bytes": r["bytes"], "at": time.time()}
        if api is not None:   # in this first write: any later one could race a start (docs/SPEC.md §21.4)
            meta["via"] = API_VIA
            meta["api"] = copy.deepcopy(api)
        # `starting` from the first write of session.json, which lists the session, until its first start: it is never
        # seen final before; and the session's lock is held until it is made (its skills, its folder), so a stop, a
        # start, a folder change or a delete asked meanwhile waits for it and never meets it half made
        token = self.mark_starting(sid, mark) if start or mark is not None else None
        result = None
        try:
            async with self._lock(sid):
                self._write_meta(sid, meta)
                try:
                    if prepared:  # before the folder: a failure leaves nothing in it; the members find them at the
                        # first start
                        await asyncio.to_thread(self._commit_skills, self.config, prepared, self._data_dir(sid),
                                                via=via)
                    if workspace is not None:
                        # with a copy, the folder must still be new or empty: the copied files move there
                        result = await self._set_workspace_locked(
                            sid, workspace, creating=True, expect="moved" if copied is not None else None,
                            within=within)
                except BaseException:
                    await asyncio.to_thread(_rmtree, path)  # nothing half-created stays behind
                    raise
            if result is not None:
                if result["mode"] == "moved":
                    result["mode"] = "created"
                elif result["old_kept"]:  # adopted: the session's own folder was never used
                    with contextlib.suppress(OSError):
                        self.default_workspace(sid).rmdir()
                    result["old_kept"] = None
            self._changed(sid, "created")
            if start:
                await self.start(sid, auto_reply=auto_reply, marked=token)
            summary = self.summary(sid)
        except BaseException:
            if token is not None:   # (a start that ended took it away already)
                self.unmark_starting(sid, token)
            raise
        if result is not None:
            summary["workspace_result"] = result
        if copied is not None:
            summary["copy_result"] = copied[1]
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
        from .swarm import EarlierVersionError, Swarm

        try:
            swarm = Swarm(config=cfg, llm_factory=self.llm_factory)
        except EarlierVersionError:  # refused before anything was written (§17.2)
            raise SessionError(self.earlier_version_text(sid), 409, earlier_version=True) from None
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
        # every journaled event of this Swarm wakes the session's waiters (watch); live-only ones (id None) do not
        swarm.bus.listen(lambda event: self._poke(sid) if event.get("id") is not None else None)
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

    async def start(self, sid: str, *, skills: list[str] | None = None, via: str = "home",
                    auto_reply: bool = False, marked: object | None = None,
                    then: Callable[["Swarm"], None] | None = None) -> "Swarm | None":
        """Start the session (a no-op when it runs). `skills`: references or sources, as `create` takes them,
        attached first: live to a running session, written into a stopped one before it starts (`via`: the surface,
        recorded in the library for a source). SessionError before anything changes when one of them cannot be.
        `auto_reply`: as `create` takes it (see _start_locked).
        `marked`: the mark of the start mark_starting announced. It starts nothing (None) when a stop or a delete
        took the mark away meanwhile, which is checked again under the session's lock (as resume() does): a delete, or
        a stop asked after the mark was placed, that held the lock while this call waited is never undone (a stop
        asked before leaves the mark alone: _stop). This mark (and no other) goes when this call ends,
        whatever its outcome.
        `then`: called with the running Swarm once the start succeeded (also when the session ran already), still
        under the session's lock, so nothing that waits for the lock (a stop, the stop of stop_when_done) comes
        between the start and it: the HTTP API v1's `continue` delivers its text there (docs/SPEC.md §21.2). It is
        synchronous and should not raise; an error it raises is logged, and the start stands."""
        try:
            if marked is not None and not self._marked(sid, marked):
                return None
            self._check(sid)
            async with self._lock(sid):
                if marked is not None and (not self._marked(sid, marked) or not self.exists(sid)):
                    return None
                written: list[str] = []
                if skills:
                    written = await self._skills_at_start(sid, self._load_locked(sid), skills, via)
                swarm = await self._start_locked(sid, auto_reply=auto_reply)
                if written:
                    swarm.skills.attached_again(written)
                if then is not None:
                    try:
                        then(swarm)
                    except Exception:
                        log.exception("session %s: the step after its start failed", sid)
            self._changed(sid, "started")
            return swarm
        finally:
            if marked is not None:
                self.unmark_starting(sid, marked)

    def _api_write(self, sid: str, **changes: Any) -> None:
        """Set keys of the "api" block of a v1 session's session.json (read-modify-write; the caller holds the
        session's lock); nothing for any other session. An error is logged, never raised: the operation that
        records it goes on (a session.json that cannot be read: its summary says why)."""
        try:
            meta = self.meta(sid)
        except SessionError:
            return
        block = api_meta(meta, create=True)
        if block is None:
            return
        block.update(changes)
        try:
            self._write_meta(sid, meta)
        except Exception:
            log.exception("could not record %s in session %s", ", ".join(changes), sid)

    def _public_text(self, sid: str | None, text: str, *, named: Iterable[tuple[Path, str]] = ()) -> str:
        """An error of the session's start for a reader outside the hub (api.start_error, the error of
        `session.stopped`): eventtext.without_abs_paths with the folders of this computer such an error names, which
        it names unquoted, spaces included ("does not exist: <path>. Bring it back"), and without_abs_paths alone
        would cut at their first space. Each is replaced by a name: the user's home by "~", also at the start of a
        path below it (without_abs_paths then names that path by its last component); the session's workspace, the
        two folders of a workspace move that did not finish, the session's own folder and the home by their last
        component, also at the start of a path below them (what lies there stays, relative); every folder above
        those only where the text names that very folder (a path below one may be anyone's). A path in another
        session's folder (below the home's `sessions`, the session's own aside) is OTHER_SESSION whole: a session's
        folder name is its title. Each also resolved. Best effort: what cannot be read is left out. `sid` None: an
        error of no session (the external API's), with the user's home and the home alone. `named`: (absolute folder,
        name) pairs of the caller's, taken before all of these and named the same way (the external API's roots of
        `[api] folders` by their names, docs/SPEC.md §21.1 "Paths in answers"); they are not resolved here. A session
        that works in a server folder in place (api.folder) has its folder named `<root>/<path>` (§21.10 "Answers"),
        before the generic names."""
        kept: dict[str, tuple[Path, str]] = {}
        exact: dict[str, tuple[Path, str]] = {}
        hidden: dict[str, tuple[Path, str]] = {}

        def add(into: dict[str, tuple[Path, str]], path: Path, name: str) -> None:
            key = os.path.normcase(str(path))
            if path.is_absolute() and path.name and key not in kept:
                into.setdefault(key, (path, name))

        def variants(path: Path) -> list[Path]:
            found = [path]
            with contextlib.suppress(OSError, RuntimeError):
                found.append(path.resolve())
            return found

        for path, name in named:
            add(kept, Path(path), name)
        if sid is not None:   # a server folder the session works in in place: `<root>/<path>` (docs/SPEC.md §21.10)
            with contextlib.suppress(SessionError, OSError, ValueError, TypeError):
                folder = apifolders.in_place_folder((api_meta(self.meta(sid)) or {}).get("folder"))
                if folder is not None:
                    for path in variants(Path(self.workspace_of(sid)["path"])):   # (below the root: the hub's
                        add(kept, path, apifolders.shown(folder["root"], folder["path"], os.sep))   # separator)
        with contextlib.suppress(RuntimeError, KeyError, OSError):
            for path in variants(Path.home()):
                add(kept, path, "~")
        bases = [self.home] if sid is None else [self.dir_of(sid), self.home]
        if sid is not None:
            with contextlib.suppress(SessionError, OSError, ValueError, TypeError):
                bases.insert(0, Path(self.workspace_of(sid)["path"]))
            with contextlib.suppress(OSError, ValueError):
                moving = json.loads(self._journal(sid).read_text(encoding="utf-8"))
                if isinstance(moving, dict):
                    bases += [Path(moving[key]) for key in ("from", "to") if isinstance(moving.get(key), str)]
        found = [path for base in bases for path in variants(base)]
        for path in found:
            add(kept, path, path.name)
        for path in found:
            for folder in path.parents:
                add(exact, folder, folder.name)
        for path in variants(self.sessions_dir):   # (the session's own folder is longer: it is named first)
            add(exact, path, path.name)
            add(hidden, path, self.OTHER_SESSION)
        return without_abs_paths(text, list(kept.values()), exact=list(exact.values()), hidden=list(hidden.values()))

    def _public_text_in_home(self, sid: str | None) -> bool:
        """Whether _public_text(sid, ...) looks at no folder outside the home (the user's home aside): no session, one
        whose session.json cannot be read, or one that works in its own folder or another folder of the home
        ("in_home"), with no workspace move left halfway (its journal names folders anywhere). Otherwise it resolves
        the session's folder outside the home (an in-place server folder of the HTTP API v1, a folder the owner chose),
        maybe on a share that does not answer. Blocking: the journal's presence and session.json, both in the home."""
        if sid is None:
            return True
        if os.path.lexists(self._journal(sid)):
            return False
        try:
            workspace = self.meta(sid).get("workspace")
        except SessionError:
            return True
        return not isinstance(workspace, dict) or _home_relative(workspace) is not None

    def _start_failed(self, sid: str, exc: BaseException) -> None:
        """A v1 session's start failed: `api.start_error` {code, text} (the text through _public_text, as in the
        journal) and `api.finished_at` (docs/SPEC.md §21.4)."""
        text = str(exc) if isinstance(exc, SessionError) else f"{type(exc).__name__}: {exc}"
        self._api_write(sid, start_error={"code": _error_code(exc), "text": self._public_text(sid, text)},
                        finished_at=time.time())

    async def _start_locked(self, sid: str, auto_reply: bool = False, *, resumed: bool = False) -> "Swarm":
        """Start the session (if it is not running). The caller holds the session's lock. `auto_reply`: a
        `dreamteam -p` client with automatic replies asks for it, so the swarm's auto-reply lease holds before its
        start (also of a running session): no question pauses it, and its goal gets the classic planning task
        (Swarm.clarifying; docs/SPEC.md §3 "Clarify mode"). `resumed`: resume() starts it as the hub starts.
        The run is journaled (docs/SPEC.md §21.6): `session.started` right before `swarm.start()`, so this run's
        start events (agent.hired, goal.set...) follow it; a failed `swarm.start()` journals `session.stopped`
        {reason: "failed", error} before its unload (the session is not in `running` yet: _unload would not), the
        error with every absolute path named by its last component, the session's folders whole (_public_text; the
        journal goes to v1 clients and exports; the log has it whole); a start cancelled meanwhile (the hub stops)
        journals {reason: "shutdown"} (and a v1 session records `api.finished_at`, no start_error). A start refused
        by the checks before `swarm.start()` journals nothing: the release of the lock wakes the waiters.
        A session of the HTTP API v1 (api_meta, docs/SPEC.md §21.4), on every start path: with `answers: "auto"` the
        swarm's auto-reply lease is held for its whole run (Swarm.hold_auto_reply) before `swarm.start()`, which may
        set a goal from the settings; its first start records `api.started_at` and `api.base_commit` (HEAD at the end
        of Workspace.init: the first commit of its files, no agent's); a start that succeeds clears
        `api.start_error` and `api.finished_at`; one that fails (the session cannot be loaded, the checks before
        `swarm.start()` refuse it, or `swarm.start()` raises) writes `api.start_error` {code, text} and
        `api.finished_at` before it raises, and before `session.stopped` is journaled. Once it runs (its pending goal
        set), the hub's automatic answers start with `answers: "auto"` (autoreply.SessionAutoReply), and the
        stop_when_done watcher with `stop_when_done` (_watch_end, from the journal's head before `session.started`:
        this run's events only, but on a resume the state it was stopped in), until _unload stops them. Its last write
        of session.json (the resume mark, and the `api` block above) is logged when it fails, never raised: the
        session runs by then."""
        # loaded under the lock: a swarm fetched before waiting could have been unloaded meanwhile
        try:
            swarm = self._load_locked(sid)
        except SessionError as exc:   # (a session made by an earlier version is never written to; a gone one is gone)
            if exc.status != 404 and not exc.extra.get("earlier_version"):
                self._start_failed(sid, exc)
            raise
        api = api_meta(self.meta(sid)) if sid != LEGACY_ID else None
        if auto_reply:
            swarm.auto_reply_seen()
        if api is not None and api_answers(api) == "auto":
            swarm.hold_auto_reply()
        if sid not in self.running:
            first = api is not None and api.get("started_at") is None
            try:
                if sid != LEGACY_ID:
                    server_folder = apifolders.in_place_folder((api or {}).get("folder")) is not None
                    if server_folder:   # `[api] folders` as the owner left it (_check_server_folder), read on the loop
                        self._refresh_settings()
                    # in a thread: a folder on a share that does not answer never stalls the hub
                    await asyncio.to_thread(self._check_workspace_ready, sid, self.meta(sid))
                    cfg = swarm.config  # what this start will use (the loaded swarm got any new settings)
                    if cfg.workspace.git and server_folder:
                        await asyncio.to_thread(self._check_server_git, sid, cfg.workspace_dir, netoff.is_off(cfg))
                    if cfg.workspace_adopted and cfg.workspace.git:
                        servers = [r["name"] for r in
                                   swarm.db.all("SELECT name FROM mcp_servers WHERE origin != 'config'")]
                        await asyncio.to_thread(self._check_adopted_repo, sid, cfg.workspace_dir, servers,
                                                netoff.is_off(cfg))
            except Exception as exc:
                if api is not None:
                    self._start_failed(sid, exc)
                raise
            swarm.workspace.record_head = first   # (base_commit: HEAD once Workspace.init made the first commit)
            try:
                head = journal_head(swarm)   # (stop_when_done counts this run's events: those after it)
                swarm.bus.emit("session.started", None)
                await swarm.start()
            except Exception as exc:
                log.exception("session %s failed to start", sid)
                try:
                    await swarm.stop()  # whatever started before the failure
                except Exception:
                    log.exception("cleanup after the failed start of %s", sid)
                error = str(exc) if isinstance(exc, SessionError) else f"{type(exc).__name__}: {exc}"
                if api is not None:   # (before the journal: a reader of session.stopped finds the error recorded)
                    self._start_failed(sid, exc)
                self._journal_stopped(sid, swarm, "failed", error=self._public_text(sid, error))
                await self._unload(sid)
                raise
            except asyncio.CancelledError:  # the hub stops while the swarm starts (Ctrl+C during resume()): the run
                # ends here, and shutdown()'s unload of a session that is not running journals nothing (nor records
                # finished_at; no start_error: the start did not fail)
                if api is not None:
                    self._api_write(sid, finished_at=time.time())
                self._journal_stopped(sid, swarm, "shutdown")
                raise
            self.running.add(sid)
            meta = self.meta(sid)
            meta["autostart"] = True
            pending = meta.get("pending_goal")
            if not isinstance(pending, dict) or not str(pending.get("title") or "").strip():
                meta.pop("pending_goal", None)
            elif not swarm.goal():
                if auto_reply:  # again: swarm.start() (workspace, MCP servers, skills) may outlast the grace
                    swarm.auto_reply_seen()
                try:  # the goal first, then forget it: a failure keeps it for the next start
                    swarm.set_goal(pending.get("actor") or USER, str(pending["title"]),
                                   str(pending.get("description") or ""))
                    meta.pop("pending_goal", None)
                except Exception:
                    log.exception("could not set the goal of session %s", sid)
            else:
                meta.pop("pending_goal", None)
            if api is not None:
                self._start_run_watch(sid, swarm, api, after=head, resumed=resumed)
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
            block = api_meta(meta, create=True)
            if block is not None:   # (one write with the rest: the session's lock is held)
                if first:
                    block["started_at"] = time.time()
                    block["base_commit"] = swarm.workspace.head_at_init
                block["start_error"] = None
                block["finished_at"] = None   # running: no longer final
            try:   # (the swarm runs: a write that fails is no failed start, which would leave it running unseen)
                self._write_meta(sid, meta)
            except OSError:
                log.exception("session %s runs, but its session.json could not be written: its resume mark and "
                              "the record of this start are missing", sid)
            swarm.session_pending_goal = pending_goal(meta)
        return swarm

    async def stop(self, sid: str, *, remember: bool = False, reason: str = "user", uid: str | None = None) -> None:
        """Stop the session's agents and unload it. remember=True keeps it marked for resume() (used at
        server shutdown); an explicit stop clears the mark, and resume() no longer starts it. A session made by an
        earlier version is refused (409): it is never running, and its mark stays. `reason`: the `session.stopped`
        reason journaled when it was running (STOP_REQUEST_REASONS: "user" for a stop from any surface; "done" and
        "budget" for stop_when_done); another one is a ValueError (the others are the manager's own). A start announced
        before this call (mark_starting, resume_pending) that has not taken the lock yet then starts nothing; one
        announced after it (a v1 `continue` sent after the stop) keeps its mark and starts the session again once
        this stop is over: the requests keep their order, whenever the stop takes the lock. `uid`: the session's uid
        as the caller resolved it, checked again once the lock is taken (_check: 404 when the folder name holds another
        session by then, which is left alone)."""
        if reason not in STOP_REQUEST_REASONS:
            raise ValueError(f"unknown stop reason {reason!r}")
        await self._stop(sid, remember=remember, reason=reason, uid=uid)

    async def _stop(self, sid: str, *, remember: bool = False, reason: str = "user",
                    still: Callable[[], bool] | None = None, uid: str | None = None) -> bool:
        """stop(), and, with `still`, only when `still()` holds under the session's lock (stop_when_done: the session
        still runs the same swarm, and its goal is still completed or its budget pause still holds); False when it did
        nothing then. Such an automatic stop ends the run it watched and nothing more: a start announced meanwhile
        (mark_starting, resume_pending; e.g. a `continue` waiting for the lock) keeps its mark and starts the session
        again."""
        self._check(sid)
        self._refuse_earlier(sid)
        earlier = set(self._marks.get(sid, ()))   # (read before any await: the starts this stop was asked after)
        async with self._lock(sid):
            if uid is not None:
                self._check(sid, uid)   # (another session under the name by now: nothing of it is touched)
            if still is not None and not still():
                return False
            try:
                await self._unload(sid, reason)
            finally:
                if not remember:
                    if still is None:
                        self.resume_pending.discard(sid)
                        self._drop_marks(sid, earlier)
                    if self.exists(sid):
                        meta = self.meta(sid)
                        meta["autostart"] = False
                        self._write_meta(sid, meta)
            self._check(sid)  # deleted while this call waited for the lock
        self._changed(sid, "stopped")
        return True

    # ---- what runs beside a v1 session (docs/SPEC.md §21.4 stop_when_done, §21.5 answers: "auto") -------------------
    def _start_run_watch(self, sid: str, swarm: "Swarm", api: dict[str, Any], *, after: int, resumed: bool) -> None:
        """A v1 session just started (_start_locked, the session's lock held): the hub's automatic answers when its
        `answers` is "auto", and the stop_when_done watcher when its `stop_when_done` is true (the default), which
        reads the journal from `after` (the head before `session.started`). An error is logged: the session runs."""
        watch = _RunWatch(swarm)
        if api_answers(api) == "auto":
            try:   # (from `after` too: a member's messages while swarm.start() ran are answered each)
                watch.auto_reply = SessionAutoReply(swarm)
                watch.auto_reply.start(after=after)
            except Exception:
                log.exception("could not start the automatic answers of session %s", sid)
        if api.get("stop_when_done", API_GIVEN["stop_when_done"]) is True:
            watch.watcher = asyncio.create_task(self._watch_end(sid, swarm, after, resumed),
                                                name=f"stop-when-done-{sid}")
        if watch.auto_reply is not None or watch.watcher is not None:
            self._run_watch[sid] = watch

    @staticmethod
    def _ended(swarm: "Swarm") -> list[str]:
        """Why a stop_when_done session's job is over now: "done" (its goal is completed), "budget" (the budget paused
        it); [] while it is not."""
        reasons = []
        if (swarm.goal() or {}).get("status") == "done":
            reasons.append("done")
        if swarm.paused and swarm.pause_reason == "budget":
            reasons.append("budget")
        return reasons

    async def _watch_end(self, sid: str, swarm: "Swarm", after: int, resumed: bool) -> None:
        """stop_when_done (docs/SPEC.md §21.4): the session is stopped when its goal is completed (`goal.completed`:
        "done") or the budget pauses it (`swarm.paused` {budget}: "budget"), read from the journal by id after `after`,
        woken by the bus (autoreply.follow_journal: never the bus's queue, which drops events when it is full). On a
        resume as the hub starts, a session that is already so is stopped at once: the hub may have stopped between
        that event and its stop. It never awaits a stop (_unload cancels this task): _stop_soon schedules it, and it
        goes on watching in case that stop finds the job not over after all (a goal reopened meanwhile)."""
        if resumed:
            for reason in self._ended(swarm)[:1]:
                self._stop_soon(sid, swarm, reason)
        async with contextlib.aclosing(follow_journal(swarm, after, ("goal.completed", "swarm.paused"))) as events:
            async for event in events:
                if event["type"] == "goal.completed":
                    self._stop_soon(sid, swarm, "done")
                elif event["data"].get("reason") == "budget":
                    self._stop_soon(sid, swarm, "budget")

    def _stop_soon(self, sid: str, swarm: "Swarm", reason: str) -> None:
        """Schedule the stop of a stop_when_done session's run (a task the manager keeps; shutdown() waits for it).
        Nothing while shutdown() runs: such a task could outlive it, and the next start's resume check stops the
        session instead (_watch_end)."""
        if self._closing:
            log.info("session %s: its stop (%s) is left to the next start (the hub stops)", sid, reason)
            return
        task = asyncio.create_task(self._stop_ended(sid, swarm, reason), name=f"stop-{reason}-{sid}")
        self._stopping.add(task)
        task.add_done_callback(self._stopping.discard)

    async def _stop_ended(self, sid: str, swarm: "Swarm", reason: str) -> None:
        """Stop the run of `swarm` with `reason` ("done" | "budget"), if, under the session's lock, the session still
        runs that swarm and the reason still holds (a stop, a continue that reopened the goal or a raised budget may
        have come first). An error is logged."""
        def still() -> bool:
            return self.swarms.get(sid) is swarm and sid in self.running and reason in self._ended(swarm)

        try:
            if await self._stop(sid, reason=reason, still=still):
                log.info("session %s stopped: %s (stop_when_done)", sid, reason)
        except SessionError as exc:   # deleted meanwhile
            log.info("session %s was not stopped (%s): %s", sid, reason, exc)
        except Exception:
            log.exception("could not stop session %s (%s)", sid, reason)

    async def _unload(self, sid: str, reason: str | None = None) -> None:
        """Stop (when it runs) and close the session's Swarm. The caller holds the session's lock. `reason`: a running
        session journals `session.stopped` {reason} after its agents stopped and before its database closes (an emit
        after the close raises); None journals nothing (nor does a session that is not running). A running v1
        session records `api.finished_at` first (docs/SPEC.md §21.4), whatever the reason. What runs beside a v1
        session's run (_RunWatch: the automatic answers, the stop_when_done watcher) stops first, before its agents.
        Until its database is closed the Swarm stays in `_unloading` (put there before it leaves `swarms`), so a reader
        in a thread (reading: the journal, v1's reads) goes on through its connection, which the close waits for, and
        never opens the file next to an open writer: a reader of the file at that moment would keep the close from
        folding the -wal file back into it."""
        swarm = self.swarms.get(sid)
        if swarm is not None:
            self._unloading[sid] = swarm
        self.swarms.pop(sid, None)
        was_running = sid in self.running
        self.running.discard(sid)
        watch = self._run_watch.pop(sid, None)
        if swarm is None:
            return
        try:
            if watch is not None:
                await watch.stop()
            if was_running:
                await swarm.stop()
        finally:
            try:
                if was_running:
                    self._api_write(sid, finished_at=time.time())
                    if reason is not None:
                        self._journal_stopped(sid, swarm, reason)
            finally:
                try:
                    swarm.close()
                finally:
                    if self._unloading.get(sid) is swarm:
                        del self._unloading[sid]

    def reading(self, sid: str) -> "Swarm | None":
        """The Swarm whose connection a reader of session `sid` in a thread uses (docs/SPEC.md §21.6): the loaded one,
        or one _unload is stopping whose database is not closed yet; None when the session's file is read (no writer
        is open). Under its Database's lock; a sqlite3.ProgrammingError means it closed meanwhile: read the file."""
        swarm = self.swarms.get(sid)
        return swarm if swarm is not None else self._unloading.get(sid)

    @staticmethod
    def _journal_stopped(sid: str, swarm: "Swarm", reason: str, **data: Any) -> None:
        """Journal `session.stopped` {reason, **data} into the session's journal; an error is logged, never raised (the
        stop goes on)."""
        try:
            swarm.bus.emit("session.stopped", None, reason=reason, **data)
        except Exception:
            log.exception("could not journal the stop of session %s", sid)

    async def unload_idle(self, sid: str) -> None:
        """Drop a loaded but not running session (frees its database connection)."""
        if sid in self.swarms and sid not in self.running:
            async with self._lock(sid):
                if sid not in self.running:
                    await self._unload(sid)

    async def control(self, sid: str, *, max_cost_usd: float | None = None, max_agents: int | None = None,
                      wrap_up: bool | None = None, uid: str | None = None) -> dict[str, Any]:
        """What `swarm_control` does with the limits and wrap-up mode, as the user (the HTTP API v1's PATCH,
        docs/SPEC.md §21.2), under the session's lock: `Swarm.set_limits` (a `max_cost_usd` that covers what was
        spent lifts a budget pause), then `wrap_up` / `end_wrap_up`. They write the session database's kv `limits`
        and `wrap_up`, which the swarm reads at every use: a stopped session is loaded to write them (one that never
        started gets its database now) and unloaded again afterwards, unless something else had it loaded (on Windows
        an open database blocks a delete or a folder move). None leaves a value as it is; nothing given changes
        nothing. Returns the summary.
        SessionError: 400 for a value of the wrong kind or one the swarm refuses (`max_agents` below 1, a negative
        cost: nothing is written), and for a number a float cannot hold (a whole number of 400 digits) or a
        `max_agents` of more than 64 bits, as `[swarm] max_agents` (config); 409 code `conflict` for `wrap_up: true`
        while the session's first start has its goal still to set (set_goal ends wrap-up mode); 409 for a session
        made by an earlier version; 404, also when `uid` (the session's uid as the caller resolved it) is given and
        the folder name holds another session once the lock is taken (_check)."""
        for name, value in (("max_cost_usd", max_cost_usd), ("max_agents", max_agents)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                      or not _finite(value)):
                raise SessionError(f"'{name}' must be a number.")
        if max_agents is not None and not isinstance(max_agents, int):
            if not float(max_agents).is_integer():
                raise SessionError("'max_agents' must be a whole number.")
            max_agents = int(max_agents)
        if max_agents is not None and max_agents >= INT64_LIMIT:
            raise SessionError("'max_agents' must be a 64-bit integer.")
        if wrap_up is not None and not isinstance(wrap_up, bool):
            raise SessionError("'wrap_up' must be true or false.")
        self._check(sid)
        self._refuse_earlier(sid)
        if max_cost_usd is None and max_agents is None and wrap_up is None:
            return self.summary(sid)
        async with self._lock(sid):
            self._check(sid, uid)  # deleted while this call waited for the lock (and its name taken, with `uid`)
            loaded = sid in self.swarms
            if wrap_up is True and pending_goal(self.meta(sid)) is not None:
                swarm = self.swarms.get(sid)   # (refused before a database is made for it)
                goal = swarm.goal() if swarm is not None else read_stats(self._data_dir(sid) / "swarm.db")["goal"]
                if not goal:
                    raise SessionError(f"Session {sid} has not started yet: its first start sets its goal, which "
                                       "ends wrap-up mode. Start it first.", 409, code="conflict")
            swarm = self._load_locked(sid)
            try:
                try:
                    if max_cost_usd is not None or max_agents is not None:
                        swarm.set_limits(USER, max_cost_usd, max_agents)
                    if wrap_up is True:
                        swarm.wrap_up(USER)
                    elif wrap_up is False:
                        swarm.end_wrap_up(USER)
                except ToolError as exc:
                    raise SessionError(str(exc), 400) from None
            finally:
                if not loaded and sid not in self.running:
                    await self._unload(sid)
        self._changed(sid, "updated")
        return self.summary(sid)

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
        Nothing is written when a value is invalid, nor for a session made by an earlier version (409)."""
        self._check(sid)
        self._refuse_earlier(sid)
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
        "commit", "error"}, "restarted": bool, "start_error": text|None}. A session made by an earlier version is
        refused (409) with its message."""
        self._check(sid)
        self._refuse_earlier(sid)
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

    async def _set_workspace(self, sid: str, path: Any, *, expect: str | None = None) -> dict[str, Any]:
        """set_workspace's change under the session's lock (_set_workspace_locked); a session that ran again
        afterwards is announced "started" once the lock is released."""
        async with self._lock(sid):
            result = await self._set_workspace_locked(sid, path, expect=expect)
        if result["restarted"]:
            self._changed(sid, "started")
        return result

    async def _set_workspace_locked(self, sid: str, path: Any, *, creating: bool = False,
                                    expect: str | None = None, within: Path | None = None) -> dict[str, Any]:
        """The folder session `sid` works in changed to `path` (set_workspace's rules); the caller holds the session's
        lock (_set_workspace; _create, from its first write of session.json). `creating`: the create's own folder
        (no change announced, the reservation checked by the create). Returns the result, "restarted" True when a
        running session ran again afterwards (the caller announces it)."""
        notify = not creating
        self._check(sid)  # deleted while the caller waited for the lock
        meta = self.meta(sid)
        self._check_journal(sid, meta)
        try:
            cfg = self.config_for(sid)
        except SessionError:
            raise
        except ValueError as exc:
            raise SessionError(f"Session {sid} has invalid settings: {exc}") from None
        git_on, network_off = cfg.workspace.git, netoff.is_off(cfg)
        first = await asyncio.to_thread(self._plan, sid, path, git_on=git_on, mcp=self._forged_servers(sid),
                                        network_off=network_off, within=within)
        if first.mode == "unchanged":
            return {**first.result(), "restarted": False, "start_error": None}
        if not creating:   # (a create checked its own folder against the others before it copied)
            self._check_reserved(first.target)
        self._check_expected(sid, first, expect)
        was_loaded, was_running = sid in self.swarms, sid in self.running
        try:  # agents, MCP server processes and triggers stop: nothing may write into the folder now (a running
            # session starts again afterwards, also after a failure: its stop is a restart)
            await self._unload(sid, "restart")
        except Exception:  # unloaded and closed anyway (_unload closes in `finally`)
            log.exception("error stopping session %s before moving its workspace", sid)
        if was_loaded and notify:
            self._changed(sid, "stopped")
        try:
            async with self._claims:  # no other session takes a folder until this one's session.json names it
                plan, git, old_changed = await self._change_folder(sid, path, git_on, first, expect, creating,
                                                                   network_off, within)
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
        return result

    async def _change_folder(self, sid: str, path: Any, git_on: bool, first: _Plan, expect: str | None,
                             creating: bool, network_off: bool = False,
                             within: Path | None = None) -> tuple[_Plan, str, dict[str, Any] | None]:
        """The change itself, with the session unloaded and the home's workspace claims held: the final check, the
        moves, session.json. Returns (plan, git outcome, what changed in the user's folder it left); raises with
        everything put back (or, when that failed too, an error that says where the files are). `within`: the root
        of a create's server folder (_plan). A move by the owner (not `creating`) of a session that works in a server
        folder in place drops its `api.folder` (docs/SPEC.md §21.10 "Starts"): it then works in a folder the owner
        chose, and the checks of that folder no longer apply."""
        # checked again with nothing running: an agent may have forged a server, the folder may have changed, and
        # another session may have taken the folder meanwhile (it is in its session.json now); for a server folder,
        # the last containment check before the missing folders are made (a link swapped in meanwhile)
        plan = await asyncio.to_thread(self._plan, sid, path, git_on=git_on, mcp=self._forged_servers(sid),
                                       network_off=network_off, within=within)
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
                block = api_meta(meta)
                if block is not None and apifolders.in_place_folder(block.get("folder")) is not None:
                    block["folder"] = None
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
                        lambda: commit_paths(plan.old, plan.old_tracked, message,
                                             offline=isolated_git(plan.network_off, plan.old)))
                except (OSError, subprocess.SubprocessError) as exc:
                    old_changed["error"] = str(exc)
            if old_changed["error"]:
                log.warning("session %s: the removal of %s in %s is not committed: %s", sid, plan.old_tracked,
                            plan.old, old_changed["error"])
        return plan, git, old_changed

    def _move_error(self, sid: str, plan: _Plan, exc: Exception, problems: list[str], *,
                    creating: bool = False) -> SessionError:
        """The error of a workspace change that failed; the code of a SessionError that caused it (a folder another
        session took meanwhile: `folder_busy`) is kept, unless files could not be put back (500)."""
        code = exc.code if isinstance(exc, SessionError) else None
        holder = exc.holder if isinstance(exc, SessionError) else None
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
                                exc.status if isinstance(exc, SessionError) else 500, code=code, holder=holder)
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
            + f". Nothing was changed: the session still works in {plan.old}.", status, code=code,
            holder=holder)

    async def delete(self, sid: str, *, skills: str | None = None, via: str = "home",
                     uid: str | None = None) -> str | None:
        """Stop the session and delete its directory (board, messages, workspace, traces). The directory is
        first renamed away, so a session whose files are in use stays intact (409, the system's reason without its
        paths) instead of being half-deleted; the hub's own readers of its files (add_reader: the HTTP API v1's
        downloads and archives) are cut first, and none starts while the delete runs. A workspace folder outside the
        directory (set_workspace) is never deleted: it is the
        user's folder (the skills mirror in it goes). Returns that folder (read under the session's lock, so a
        workspace change that ran just before is taken into account), else None.
        Skills members proposed that wait for the user's library decision are not lost silently: 409
        (`pending_skills`) unless `skills` is "save" (each saved to the library first, `via`; a clash refuses the
        delete) or "discard". `uid`: the session's uid as the caller resolved it, checked again once the lock is taken
        (_check: 404 when the folder name holds another session by then, which is never deleted)."""
        self._check(sid)
        if sid == LEGACY_ID:
            if self.earlier_version(sid):  # its own message names the file to remove by hand
                raise SessionError(self.earlier_version_text(sid), 409, earlier_version=True)
            raise SessionError("The main session lives in the home directory itself and cannot be deleted "
                               "from the UI; delete its .dreamteam folder by hand.", 409)
        if skills not in (None, "save", "discard"):
            raise SessionError("skills must be \"save\" or \"discard\".")
        async with self._lock(sid):
            self._check(sid, uid)  # a concurrent delete finished first (and, with `uid`, another session took the name)
            pending = [s.name for s in self._attached_skills(sid) if s.library == "pending"]
            if pending and skills is None:
                raise SessionError(f"Session {sid} has skills waiting for your library decision: "
                                   f"{', '.join(pending)}. Save them to the library first, or delete them with the "
                                   "session.", 409, pending_skills=pending)
            if pending and skills == "save":
                await self._save_pending(sid, pending, via)
            self.resume_pending.discard(sid)
            self._drop_marks(sid)   # (a start on its way starts nothing: start(..., marked=...))
            self._deleting.add(sid)   # (no reader of its files starts while this runs: add_reader)
            try:
                # the hub's own readers of its files (the HTTP API v1's downloads and archives) close what they hold
                # before the rename, at once or when their running step ends (the retries below wait for that)
                for reader in list(self._readers.get(sid, ())):
                    reader.cut()
                try:  # a user's stop: its journal goes away with the session, unless the rename below fails (files
                    # in use, 409) and the session stays, stopped
                    await self._unload(sid, "user")
                except Exception:  # unloaded and closed anyway (_unload closes in `finally`)
                    log.exception("error stopping session %s before deleting it", sid)
                try:
                    workspace = self.workspace_of(sid)
                    kept = None if workspace["default"] else workspace["path"]
                except SessionError:  # a damaged session.json: it can still be deleted
                    workspace, kept = {}, None
                if workspace.get("adopted") and workspace.get("exists"):
                    # the user's folder keeps no copy of the session's skills (Swarm.stop removes it too: this covers
                    # a crashed or never-stopped one)
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
                            try:  # it stays, stopped as by the user (session.stopped above): a hub start does not
                                # resume it
                                meta = self.meta(sid)
                                meta["autostart"] = False
                                self._write_meta(sid, meta)
                            except Exception:
                                log.exception("could not clear the resume mark of session %s", sid)
                            log.info("could not delete session %s: %s", sid, exc)
                            raise SessionError(f"Could not delete session {sid}: its files are in use "
                                               f"({exc.strerror or exc}). Close the programs that use them and try "
                                               "again.", 409) from None
                        await asyncio.sleep(0.2)
            finally:
                self._deleting.discard(sid)
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

    # ---- import (transfer.apply_import) ---------------------------------------------------------------
    def staging_path(self, sid: str) -> Path:
        """A new (not yet created) folder in sessions/ to stage an imported session in before it is renamed in."""
        return self.sessions_dir / f"{IMPORTING_PREFIX}{sid}-{os.getpid()}-{uuid.uuid4().hex[:8]}"

    def clean_staging(self) -> list[str]:
        """Remove the staged copies (`.importing-*`) an interrupted import left in sessions/; returns those that
        could not be removed completely. Called at the start of an import, when no other one runs on this home."""
        left = []
        if not self.sessions_dir.is_dir():
            return left
        for path in sorted(self.sessions_dir.iterdir()):
            if not path.name.startswith(IMPORTING_PREFIX):
                continue
            parts = path.name.rsplit("-", 2)   # .importing-<id>-<pid>-<token>: another live import's copy stays
            if len(parts) == 3 and parts[1].isdigit() and int(parts[1]) != os.getpid() and _pid_alive(int(parts[1])):
                continue
            try:
                _remove(path)
            except OSError:
                left.append(str(path))
        return left

    def backups(self, sid: str) -> list[Path]:
        """The copies of session `sid` imports replaced (`.replaced-<id>-<time>/`), oldest first."""
        prefix = f"{REPLACED_PREFIX}{sid}-"
        if not self.sessions_dir.is_dir():
            return []
        return sorted(p for p in self.sessions_dir.iterdir()
                      if p.name.startswith(prefix) and _STAMP.fullmatch(p.name[len(prefix):]) and p.is_dir())

    @classmethod
    def import_overrides(cls, overrides: Any, models: Collection[str]) -> tuple[dict[str, Any], str | None]:
        """The overrides an imported session is written with (the import plan compares the same): cleaned as
        update() cleans them, an `agents.default_model` naming a profile not in `models` dropped (as update_settings
        does; its name is returned, else None). Raises SessionError for overrides that are not sections."""
        overrides = cls.clean_overrides(overrides or {})
        agents = overrides.get("agents")
        dropped = agents.get("default_model") if isinstance(agents, dict) else None
        if isinstance(dropped, str) and dropped not in models:
            del agents["default_model"]
            if not agents:
                del overrides["agents"]
            return overrides, dropped
        return overrides, None

    def _import_meta(self, sid: str, meta: dict[str, Any], workspace: ImportedWorkspace) -> tuple[dict, str | None]:
        """The session.json an imported session gets as `sid`: its overrides checked against this home's settings
        (an `agents.default_model` naming a profile this home does not have is dropped, as update_settings does; the
        name is returned), never started by itself (autostart false), its workspace entry built for this home. A
        session of the HTTP API v1 keeps "via" and its "api" block without what belongs to the other computer
        (API_IMPORT_DROPS, set to null; docs/SPEC.md §21.9)."""
        self._refresh_settings()
        data = {k: copy.deepcopy(v) for k, v in meta.items() if k not in ("id", "legacy", "workspace_notice")}
        if not isinstance(data.get("uid"), str) or not _UID.fullmatch(data["uid"]):
            raise SessionError(f"Session {sid} has no valid uid to be imported with.")
        block = api_meta(data)
        if block is not None and isinstance(data.get("api"), dict):
            block.update(dict.fromkeys(API_IMPORT_DROPS))
        overrides, dropped = self.import_overrides(data.get("overrides"), self.config.models)
        try:
            self._config_from(self.raw, sid, overrides)
        except ValueError as exc:
            raise SessionError(f"Session {sid} has settings this home cannot use: {exc}") from None
        data["overrides"] = overrides
        data["autostart"] = False
        data.pop("workspace", None)
        if workspace.kind == "in_home":
            if workspace.folder is None:
                raise SessionError(f"Session {sid}: no folder in the home for its workspace.", 500)
            data["workspace"] = self._workspace_meta(Path(os.path.abspath(workspace.folder)), workspace.adopted)
        elif workspace.kind == "outside":
            if not isinstance(workspace.path, str) or not workspace.path.strip():
                raise SessionError(f"Session {sid}: no folder for its workspace.", 500)
            data["workspace"] = {"path": workspace.path, "adopted": workspace.adopted}
        elif workspace.kind != "own":
            raise SessionError(f"Unknown workspace kind '{workspace.kind}'.", 500)
        return data, dropped

    @staticmethod
    def _finish_staged(staged: Path, data: dict[str, Any], final: Path) -> None:
        """Open the staged copy's database once (columns added by this version, WAL mode again; the absolute job
        log paths of the other computer point at the copy's own `.dreamteam/jobs/` in `final`, the folder it is
        renamed to, or are cleared), then write its session.json: last, so a staged copy without one never looks
        like a finished session."""
        db = staged / ".dreamteam" / "swarm.db"
        if db.is_file():
            database = Database(db)
            try:
                _rebase_job_logs(database, staged / ".dreamteam" / "jobs", final / ".dreamteam" / "jobs")
            finally:
                database.close()
        path = staged / SESSION_FILE
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_bytes(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8", "replace"))
            os.replace(tmp, path)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()

    def _import_error(self, sid: str, exc: BaseException, problems: list[str], what: str) -> SessionError:
        if isinstance(exc, SessionError) and not problems:
            return exc
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else (str(exc) or type(exc).__name__)
        if isinstance(exc, OSError) and exc.filename:
            reason += f": {exc.filename}"
        if problems:
            return SessionError(f"Could not {what} session {sid} ({reason}), and some folders could not be put back: "
                                f"{'; '.join(problems[:10])}. Move them back by hand.", 500)
        if _in_use(exc):
            return SessionError(f"Could not {what} session {sid}: its files are in use ({reason}). Close the programs "
                                "that use them and try again. Nothing was changed.", 409)
        return SessionError(f"Could not {what} session {sid} ({reason}). Nothing was changed.", 500)

    def _place_workspace(self, staged: Path, workspace: ImportedWorkspace, done: list, created: list[Path]) -> None:
        """Move the staged `workspace/` to its folder in the home (in_home; the folder must not exist)."""
        if workspace.kind != "in_home" or workspace.folder is None:
            return
        folder = Path(workspace.folder)
        if os.path.lexists(folder):
            raise SessionError(f"{folder} appeared meanwhile; check it and import again.", 409)
        _make_parents(folder, created)
        source = staged / WORKSPACE_DIR
        if os.path.lexists(source):
            _rename_retry(source, folder, done)
        else:
            folder.mkdir()
            created.append(folder)

    async def import_add(self, staged: Path, meta: dict[str, Any], workspace: ImportedWorkspace, *, sid: str,
                         before_swap: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Add the session staged in `staged` (a folder in sessions/: its .dreamteam/ and workspace/, see
        staging_path) under the id `sid`, or a new id (`_new_id` of its title) when `sid` is taken here. `meta`:
        its session.json (with its uid); `workspace`: where it works here. `before_swap(id)` runs right before it
        is renamed in (it may raise to refuse). The session never starts by itself. Returns {"id", "dropped_model"
        (an agents.default_model override this home does not have, removed, or None)}. Raises SessionError with
        nothing left behind but `staged` (the caller removes it)."""
        if not isinstance(sid, str) or not SESSION_ID.match(sid) or sid == LEGACY_ID:
            raise SessionError(f"'{sid}' is not a valid session id.")
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        chosen = sid
        for _attempt in range(100):   # another process may take the id meanwhile
            if os.path.lexists(self.dir_of(chosen)):
                chosen = self._new_id(str(meta.get("title") or ""))
                continue
            async with self._lock(chosen):
                if os.path.lexists(self.dir_of(chosen)):
                    continue
                data, dropped = self._import_meta(chosen, meta, workspace)
                await _in_thread(self._finish_staged, staged, data, self.dir_of(chosen))
                if before_swap is not None:
                    before_swap(chosen)
                if await _to_end(self._swap_added, staged, chosen, workspace):
                    self._changed(chosen, "created")
                    return {"id": chosen, "dropped_model": dropped}
        raise SessionError("Could not choose a session id.", 500)

    def _swap_added(self, staged: Path, sid: str, workspace: ImportedWorkspace) -> bool:
        """Rename the staged copy in (False: the id was taken meanwhile, nothing changed)."""
        if os.path.lexists(self.dir_of(sid)):
            return False
        done: list[tuple[Path, Path]] = []
        created: list[Path] = []
        try:
            self._place_workspace(staged, workspace, done, created)
            _rename_retry(staged, self.dir_of(sid), done)
        except BaseException as exc:
            problems = _undo_renames(done, created)
            raise self._import_error(sid, exc, problems, "import") from None
        return True

    async def import_rewrite(self, sid: str, staged: Path, meta: dict[str, Any], workspace: ImportedWorkspace, *,
                             expect: dict[str, Any] | None = None,
                             before_swap: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Replace the session `sid` with the copy staged in `staged` (see import_add), under the session's lock.
        `expect`: its session.json as the import plan read it; a session changed since then is refused (409). The
        old folder and its workspace folder inside the home go into the backup `sessions/.replaced-<id>-<time>/`
        (`session/`, `workspace/`), then the staged copy is renamed in; a failure puts everything back (renames are
        tried again while a file is held open for a moment; files in use: 409). Afterwards the old copy's
        environments (KEEP_ON_IMPORT folders) move into the new workspace, and older backups of this session are
        removed: only the last one is kept. A running session is refused; a loaded one is unloaded first. Returns
        {"id", "backup", "kept": [moved environments], "removed_backups", "warnings", "dropped_model"}."""
        self._check(sid)
        if sid == LEGACY_ID:
            raise SessionError("The main session cannot be replaced by an import.", 409)
        async with self._lock(sid):
            self._check(sid)
            current = self.meta(sid)
            if expect is not None and {k: v for k, v in current.items() if k not in ("id", "legacy")} != \
                    {k: v for k, v in expect.items() if k not in ("id", "legacy")}:
                raise SessionError(f"Session {sid} changed after the import was planned: run the import again.", 409)
            self._check_journal(sid, current)   # files of an unfinished workspace move: a person puts them together
            if sid in self.running:
                raise SessionError(f"Session {sid} is running here: stop it first, then import again.", 409)
            if sid in self.swarms:
                await self._unload(sid)
            data, dropped = self._import_meta(sid, meta, workspace)
            await _in_thread(self._finish_staged, staged, data, self.dir_of(sid))
            old_folder, old_stored, _adopted = self._workspace_entry(sid, current)
            relative = _home_relative(current.get("workspace"))
            old_in_home = old_folder if relative is not None else None
            if before_swap is not None:
                before_swap(sid)
            backup = await _to_end(self._swap_replaced, sid, staged, workspace, old_in_home)
            old_workspace = (backup / WORKSPACE_DIR if old_in_home is not None
                             else backup / "session" / WORKSPACE_DIR if old_stored is None else None)
            new_workspace = (Path(workspace.folder) if workspace.kind == "in_home" and workspace.folder is not None
                             else self.default_workspace(sid) if workspace.kind == "own" else None)
            kept, warnings = await _to_end(self._carry_environments, old_workspace, new_workspace)
            removed, left = await _to_end(self._drop_old_backups, sid, backup)
            warnings += [f"the older backup {path} could not be removed completely: delete it by hand"
                         for path in left]
        self._index = None   # another session.json: its uid and "via" may differ (sid_of_uid, api_sids)
        self._changed(sid, "updated")
        return {"id": sid, "backup": str(backup), "kept": kept, "removed_backups": removed, "warnings": warnings,
                "dropped_model": dropped}

    async def import_settings(self, sid: str, meta: dict[str, Any], *,
                              expect: dict[str, Any] | None = None) -> dict[str, Any]:
        """Write the session.json an import brings for session `sid` whose history and files are the same here
        (only its title, its settings or its pending goal differ): no folder is swapped and no backup is made. The
        session keeps its workspace entry; the checks are those of import_rewrite. Returns {"id",
        "dropped_model"}."""
        self._check(sid)
        if sid == LEGACY_ID:
            raise SessionError("The main session cannot be replaced by an import.", 409)
        async with self._lock(sid):
            self._check(sid)
            current = self.meta(sid)
            if expect is not None and {k: v for k, v in current.items() if k not in ("id", "legacy")} != \
                    {k: v for k, v in expect.items() if k not in ("id", "legacy")}:
                raise SessionError(f"Session {sid} changed after the import was planned: run the import again.", 409)
            self._check_journal(sid, current)
            if sid in self.running:
                raise SessionError(f"Session {sid} is running here: stop it first, then import again.", 409)
            if sid in self.swarms:
                await self._unload(sid)   # a loaded swarm keeps the settings it was loaded with
            data, dropped = self._import_meta(sid, meta, ImportedWorkspace("own"))
            if "workspace" in current:
                data["workspace"] = copy.deepcopy(current["workspace"])
            self._write_meta(sid, data)
        self._index = None   # another session.json: its uid and "via" may differ (sid_of_uid, api_sids)
        self._changed(sid, "updated")
        return {"id": sid, "dropped_model": dropped}

    def _swap_replaced(self, sid: str, staged: Path, workspace: ImportedWorkspace, old_in_home: Path | None) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = self.sessions_dir / f"{REPLACED_PREFIX}{sid}-{stamp}"
        number = 1
        while os.path.lexists(backup):
            number += 1
            backup = self.sessions_dir / f"{REPLACED_PREFIX}{sid}-{stamp}-{number}"
        done: list[tuple[Path, Path]] = []
        created: list[Path] = []
        planned: list[tuple[Path, Path]] = [(self.dir_of(sid), backup / "session")]
        if old_in_home is not None and os.path.lexists(old_in_home) and not is_link(old_in_home):
            planned.append((old_in_home, backup / WORKSPACE_DIR))
        if workspace.kind == "in_home" and workspace.folder is not None and os.path.lexists(staged / WORKSPACE_DIR):
            planned.append((staged / WORKSPACE_DIR, Path(workspace.folder)))
        planned.append((staged, self.dir_of(sid)))
        journal = backup / SWAP_JOURNAL
        try:
            backup.mkdir()
            created.append(backup)
            # the journal names every rename in order: a process that dies halfway (power loss) is undone by the
            # next import (recover_swaps); it goes once the swap is complete
            _write_swap_journal(journal, sid, planned, done, created, self.home)
            _rename_retry(self.dir_of(sid), backup / "session", done)
            _write_swap_journal(journal, sid, planned, done, created, self.home)
            if old_in_home is not None and os.path.lexists(old_in_home) and not is_link(old_in_home):
                _rename_retry(old_in_home, backup / WORKSPACE_DIR, done)
                _write_swap_journal(journal, sid, planned, done, created, self.home)
            self._place_workspace(staged, workspace, done, created)
            _write_swap_journal(journal, sid, planned, done, created, self.home)
            _rename_retry(staged, self.dir_of(sid), done)
        except BaseException as exc:
            problems = _undo_renames(done, [])
            if not problems:
                with contextlib.suppress(OSError):
                    journal.unlink()
            for folder in reversed(created):
                with contextlib.suppress(OSError):
                    folder.rmdir()
            raise self._import_error(sid, exc, problems, "replace") from None
        with contextlib.suppress(OSError):
            journal.unlink()
        return backup

    def recover_swaps(self) -> list[str]:
        """Finish or undo the session swaps of an import that stopped halfway (the process died between the renames
        of `_swap_replaced`: the session folder may be in its backup). A swap whose renames all happened is kept
        (only its journal goes); otherwise every rename it made is put back, newest first. Swaps of a live process
        are left alone. Returns what was done, and what could not be put back, as messages. Called at the start
        of an import, when no other one runs on this home."""
        messages: list[str] = []
        if not self.sessions_dir.is_dir():
            return messages
        for backup in sorted(self.sessions_dir.iterdir()):
            journal = backup / SWAP_JOURNAL
            if not backup.name.startswith(REPLACED_PREFIX) or not journal.is_file():
                continue
            try:
                info = json.loads(journal.read_text(encoding="utf-8"))
                sid = str(info["sid"])
                planned = [(_journal_resolve(a, self.home), _journal_resolve(b, self.home)) for a, b in info["planned"]]
                done = [(_journal_resolve(a, self.home), _journal_resolve(b, self.home)) for a, b in info["done"]]
                created = [_journal_resolve(p, self.home) for p in info.get("created") or []]
                pid = int(info.get("pid") or 0)
            except (OSError, ValueError, KeyError, TypeError, RecursionError):
                messages.append(f"{journal} cannot be read: check {backup} by hand (it may hold a session's files)")
                continue
            if pid and pid != os.getpid() and _pid_alive(pid):
                messages.append(f"the import of session {sid} by process {pid} has not finished ({backup}): it is "
                                "left as it is while that process runs")
                continue   # an import of another process is swapping it right now
            if len(done) < len(planned):   # a rename may have happened just before the process died
                src, dst = planned[len(done)]
                if os.path.lexists(dst) and not os.path.lexists(src):
                    done.append((src, dst))
            if len(done) >= len(planned):
                if planned and not os.path.lexists(planned[-1][1]):   # the session folder is not where it should be
                    messages.append(f"the import of session {sid} that stopped halfway cannot be checked: "
                                    f"{planned[-1][1]} does not exist (was the home moved?); the session's files may "
                                    f"be in {backup}. Move them back by hand.")
                    continue
                with contextlib.suppress(OSError):
                    journal.unlink()
                messages.append(f"the import of session {sid} that stopped halfway had finished; its previous copy is "
                                f"in {backup}")
                continue
            problems = []
            for src, dst in reversed(done):
                if not os.path.lexists(dst):
                    if not os.path.lexists(src):   # neither is there: the journal names other places
                        problems.append(f"{dst} (belongs in {src}): neither is there (was the home moved?); the "
                                        f"files may be in {backup}")
                    continue
                if os.path.lexists(src):   # taken meanwhile: the copy stays where it is, and the journal too
                    problems.append(f"{dst} (belongs in {src}, which exists now)")
                    continue
                try:
                    _rename_retry(dst, src)
                except OSError as exc:
                    problems.append(f"{dst} (belongs in {src}): {exc.strerror or exc}")
            if problems:
                messages.append(f"the import of session {sid} that stopped halfway could not be undone completely: "
                                f"{'; '.join(problems)}. Move them back by hand.")
                continue
            with contextlib.suppress(OSError):
                journal.unlink()
            for folder in reversed(created):
                with contextlib.suppress(OSError):
                    folder.rmdir()
            messages.append(f"the import of session {sid} that stopped halfway was undone: the session is as it was "
                            "before it")
        return messages

    def interrupted_swaps(self) -> list[str]:
        """The backups that hold the journal of a session swap that stopped halfway (see recover_swaps)."""
        if not self.sessions_dir.is_dir():
            return []
        return [str(p) for p in sorted(self.sessions_dir.iterdir())
                if p.name.startswith(REPLACED_PREFIX) and (p / SWAP_JOURNAL).is_file()]

    @staticmethod
    def _carry_environments(old: Path | None, new: Path | None) -> tuple[list[str], list[str]]:
        """Move the KEEP_ON_IMPORT folders of the old workspace `old` to the same place in `new` where that place is
        free (an export never carries them; the path stays the same, so they keep working). Returns (the moved
        relative paths, warnings)."""
        moved: list[str] = []
        warnings: list[str] = []
        if old is None or new is None or is_link(old) or not old.is_dir() or not new.is_dir():
            return moved, warnings
        for top, dirs, _files in os.walk(old):
            keep = sorted(d for d in dirs if d in KEEP_ON_IMPORT)
            dirs[:] = [d for d in dirs if d not in KEEP_ON_IMPORT and d != ".git" and not is_link(Path(top, d))]
            for name in keep:
                src = Path(top, name)
                rel = src.relative_to(old)
                dst = new / rel
                if is_link(src) or not dst.parent.is_dir() or os.path.lexists(dst):
                    continue
                try:
                    _rename_retry(src, dst)
                    moved.append(rel.as_posix())
                except OSError as exc:
                    warnings.append(f"{rel.as_posix()} stays in the backup {src} ({exc.strerror or exc}): move it by "
                                    "hand or create it again")
        return moved, warnings

    def _drop_old_backups(self, sid: str, keep: Path) -> tuple[list[str], list[str]]:
        """Remove the older backups of session `sid`: those of the same session (its uid; a deleted session that had
        the same id keeps its backup) whose swap finished (no swap journal)."""
        removed, left = [], []
        uid = _backup_uid(keep)
        for path in self.backups(sid):
            if _same_path(path, keep) or (path / SWAP_JOURNAL).exists():
                continue
            if uid is not None and _backup_uid(path) not in (uid, None):
                continue
            try:
                _remove(path)
                removed.append(str(path))
            except OSError:
                left.append(str(path))
        return removed, left

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

    def _resume_plan(self) -> list[str]:
        """The sessions resume() will try: those marked for it (autostart), read from their session.json alone. It is
        cheap and logs nothing, so the launcher can make it before its port opens; whether one is finished or made by
        an earlier version is resume()'s to tell (and log) at its turn. A session.json that cannot be read is tried
        too: resume() logs why it does not start that session."""
        plan = []
        for sid in self.ids():
            try:
                marked = bool(self.meta(sid).get("autostart"))
            except Exception:  # e.g. a damaged session.json: resume() says what to do
                marked = True
            if marked:
                plan.append(sid)
        return plan

    def plan_resume(self) -> list[str]:
        """Choose the sessions the next resume() tries and put them into `resume_pending` now: the launcher calls it
        before it takes its first request, so a session about to be resumed is never seen final (is_final) by a
        client that comes first. cancel_resume() drops the plan when resume() is not called after all."""
        self._resume_planned = self._resume_plan()
        self.resume_pending.update(self._resume_planned)
        return list(self._resume_planned)

    def cancel_resume(self) -> None:
        """Forget a plan_resume() whose resume() will not run (the launcher lost the port, or stopped first)."""
        planned, self._resume_planned = self._resume_planned or [], None
        for sid in planned:
            self._resume_done(sid)

    def _resume_done(self, sid: str) -> None:
        if sid in self.resume_pending:
            self.resume_pending.discard(sid)
            self._poke(sid)

    async def resume(self) -> list[str]:
        """Start the sessions that were running when the server last stopped (the plan of plan_resume(), else chosen
        now), except finished ones (finished()): they stay stopped until started by hand; and except sessions made by
        an earlier version, which keep their mark (one warning each). Each one is in `resume_pending` until it is
        handled. One stopped by the user (out of `resume_pending`, its mark cleared) or deleted meanwhile is skipped,
        and that is checked again under the session's lock: a stop that held it while this call waited is never
        undone."""
        sids, self._resume_planned = self._resume_planned, None
        if sids is None:
            sids = self._resume_plan()
            self.resume_pending.update(sids)
        started = []

        def still_wanted(sid: str) -> bool:
            return sid in self.resume_pending and bool(self.meta(sid).get("autostart"))

        try:
            for sid in sids:
                try:
                    if not still_wanted(sid):
                        continue
                    if self.earlier_version(sid):
                        log.warning("session %s was made by an earlier version of DreamTeam: not resumed", sid)
                        continue
                    if self.finished(sid):
                        log.info("session %s is finished (goal achieved, no open task): not resumed", sid)
                        continue
                    async with self._lock(sid):
                        if not still_wanted(sid):  # a stop or a delete held the lock while this call waited
                            continue
                        await self._start_locked(sid, resumed=True)
                    self._changed(sid, "started")
                    started.append(sid)
                except SessionError as exc:  # e.g. its workspace folder is missing: the message says what to do
                    log.warning("could not resume session %s: %s", sid, exc)
                except Exception:
                    log.exception("could not resume session %s", sid)
                finally:
                    self._resume_done(sid)
        finally:  # (cancelled: the rest is not about to start any more)
            for sid in sids:
                self._resume_done(sid)
        return started

    async def shutdown(self) -> None:
        """Stop every loaded session, keeping the running ones marked for resume() (they journal `session.stopped`
        {reason: "shutdown"}). The stops stop_when_done scheduled end first: such a job is over, so it is not resumed
        at the next start. None is scheduled from here on (`_closing`): a goal completed or a budget pause the watcher
        has not acted on yet is left to the resume check of the next start (_watch_end)."""
        self._closing = True
        try:
            # (only the stops not done yet are awaited: a task that has just ended leaves _stopping one loop step later,
            # through its done-callback, and on Python 3.12+ a gather of done tasks completes without yielding to the
            # loop, so waiting for those would never let that callback run)
            while self._stopping:
                self._stopping.difference_update([task for task in self._stopping if task.done()])
                if self._stopping:
                    await asyncio.wait(set(self._stopping))
            # also sessions in the middle of an operation (a start, a workspace move that starts them again)
            busy = [sid for sid, lock in self._locks.items() if lock.locked() and sid not in self.swarms]
            for sid in list(self.swarms) + busy:
                try:
                    async with self._lock(sid):
                        await self._unload(sid, "shutdown")
                except Exception:
                    log.exception("error stopping session %s", sid)
        finally:
            self._closing = False

    def copy_overrides(self, sid: str) -> dict[str, Any]:
        return copy.deepcopy(self.meta(sid).get("overrides") or {})
