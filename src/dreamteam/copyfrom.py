"""Copy a folder or a zip into a new session's folder before its first start (docs/SPEC.md §17.2 "Copying files
into a new session"), and keep the zips the sessions page uploads for it.

A folder is copied with treecopy.copy_tree: `.git`, `node_modules`, `.venv`, `venv` and `__pycache__` (any letter
case, at any depth) and links are left out, as are `.DS_Store` and `Thumbs.db` files (without a word), read-only
files become writable and the setuid / setgid bits are dropped; a file deleted while the copy runs is not there any
more (reported, no refusal). A zip is unpacked with skills.unpack_zip under the same exclusions; a zip whose
members all sit in one top folder gives that folder's contents. The source is only read. `[workspace] copy_max_mb`
and `copy_max_files` bound both (and the upload).

The sessions page uploads a zip with POST /api/uploads (the raw body, not base64 JSON): `Uploads` keeps it as
`<home>/.dreamteam/uploads/<token>.zip` with its `<token>.json` for UPLOAD_TTL seconds; the create call that names
the token claims it first (renames it to `<token>.claimed-<pid>-<run>-<hex8>.zip`) and removes it whatever happens.
Leftovers (expired uploads, the claims and staging folders of processes that no longer run, and those of this one
no longer in use: their removal failed) are removed at each create that copies and by the hub's hourly sweep.

The HTTP API v1 (docs/SPEC.md §21.3) has uploads of its own, in another root (`<home>/.dreamteam/uploads/api`):
any file (`<token>.bin`, its json naming the caller), checked without claiming (`peek_many`), claimed all or none
(`claim_many`) and given back after a refused create (`unclaim`, `settle`); `copy_sources` copies several of them
into one staging folder, a server folder of `[api] folders` among them (§21.10: a Source with a `root`, read below
it by its names without ever following a link, nofollow.walk_inside, never with copy_tree). `check_upload_name` and
`check_rel` are v1's name and path rules (§21.3, §21.8).
Both stores are the hub's alone (0o700 on POSIX), and an upload is read only as the file the hub wrote: never
through a link, never a file with another name (`_open_upload`), since in the Docker sandbox the hub reads with more
rights than the agents.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import errno
import json
import logging
import os
import re
import secrets
import stat
import sys
import threading
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, AsyncIterator, BinaryIO, Callable, Iterable, Iterator

from . import hubauth, netoff, nofollow, treecopy
from .config import CONFIG_FILE
from .console import unquote
from .skills import (ZIP_MAX_ENTRIES, SkillError, ZipRules, _portable, folded_names_text, printable_name,
                     unpack_zip)
from .treecopy import TreeLimit, copy_tree, is_within, scan_tree, writable_bits
from .workspace import _WIN_BAD_CHARS, _WIN_RESERVED, long_path, pid_alive, rmtree

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger(__name__)

COPY_EXCLUDE_NAMES = treecopy.COPY_EXCLUDE_NAMES
COPY_EXCLUDE_PATHS = treecopy.EXPORT_EXCLUDE_PATHS      # a DreamTeam workspace's skills mirror and stand-in folder
COPY_SKIP_FILES = treecopy.COPY_SKIP_FILES              # .DS_Store, Thumbs.db: left out of a folder and of a zip
COPYING_PREFIX = ".copying-"     # sessions/.copying-<pid>-<run>-<hex8>: a copy being staged (never a session id)
COPYING_NAME = re.compile(r"\.copying-(\d+)-([0-9a-f]{8})-([0-9a-f]{8})\Z")
# this process's run: with its pid it names the staging folders and claims it makes. A pid alone is not enough:
# the hub of the Docker sandbox has the same pid at every start, so a killed container's leftovers would look live
RUN = secrets.token_hex(4)
UPLOADS_DIR = "uploads"          # <home>/.dreamteam/uploads
API_UPLOADS_DIR = "api"          # <home>/.dreamteam/uploads/api: the uploads of the HTTP API v1, apart from the board's
UPLOAD_PATH = "/api/uploads"
API_UPLOAD_PATH = "/api/v1/uploads"
UPLOAD_TTL = 3600                # seconds an upload waits for its session
UPLOAD_MAX_PENDING = 8           # uploads waiting at once (the board's; v1 passes [api] uploads_pending)
UPLOAD_CALLERS = ("owner", "api")   # who made a v1 upload: the hub token's owner or the API token (web.API_CALLER)
UPLOAD_TOKEN = re.compile(r"[A-Za-z0-9_-]{24}\Z")   # secrets.token_urlsafe(18)
# an upload waiting (or arriving): the board's <token>.zip, v1's <token>.bin, their <token>.json, a <token>.part
PENDING_NAME = re.compile(r"([A-Za-z0-9_-]{24})\.(zip|bin|json|part)\Z")
# claimed by a create: <token>.claimed-<pid>-<run>-<hex8>.zip (the board's) or .bin (v1's)
CLAIMED_NAME = re.compile(r"([A-Za-z0-9_-]{24})\.claimed-(\d+)-([0-9a-f]{8})-([0-9a-f]{8})\.(zip|bin)\Z")
NAME_MAX = 200                   # characters kept of an upload's file name (v1: the most it may have)
NAME_BYTES_MAX = 255             # v1: the most an upload's file name may have in UTF-8 bytes, and in UTF-16 units
# names the folder of a new session keeps for itself (docs/SPEC.md §21.3): the members' folders, the session's own
# .gitignore, DreamTeam's data folder name, the MCP servers the agents build. A v1 upload never takes one, and the
# top level of what v1 unpacks or copies leaves them out (casefolded)
SESSION_NAMES = frozenset({".scratch", ".gitignore", ".dreamteam", "mcp"})
_COPY_EXCLUDE_FOLDED = frozenset(n.casefold() for n in COPY_EXCLUDE_NAMES)
# what a v1 name or path cannot hold (docs/SPEC.md §21.3): controls (C0, C1), format characters (bidi overrides,
# zero-width ones), surrogates, private use, the line and paragraph separators; spaces of any kind (U+3000, U+00A0)
# and unassigned code points are fine, so a name is taken or refused alike whatever Unicode version Python has
_UNPRINTABLE = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})
_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
SHOWN_MAX = 20                   # left-out paths listed in a result
SOURCE_SHOWN_MAX = 300           # characters of copied_from.source shown by summary() and session_line
CHUNK = 1 << 20
_ERRORS_SHOWN = 5
_LEFT_OUT = ".git, node_modules, .venv, venv and __pycache__ do not count"
_SYSTEM_ROOTS = ("/proc", "/sys", "/dev")
_MOUNT_PARENTS = ("/mnt", "/media", "/run/media", "/Volumes")   # where whole disks are mounted (POSIX)
_MOUNTINFO = "/proc/self/mountinfo"   # Linux: what is mounted where (_mount_root)


class CopyError(ValueError):
    """A copy that cannot be made; `status` is the HTTP status. `code`: the HTTP API v1's error code where the
    status alone does not say it (docs/SPEC.md §21.1: `bad_name`, `file_conflict`, `too_many_uploads`, and for the
    server folders of §21.10 `unknown_folder`, `forbidden_folder`, `not_found`); None otherwise. The internal routes
    do not show it."""

    def __init__(self, message: str, status: int = 400, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class CopyCancelled(Exception):
    """The create call was cancelled while the copy was made."""


@dataclass(frozen=True)
class CopyLimits:
    """[workspace] copy_max_mb / copy_max_files: the largest copy into a new session (and uploaded zip)."""

    max_mb: int
    max_files: int

    @property
    def max_bytes(self) -> int:
        return self.max_mb * 2 ** 20

    @classmethod
    def from_config(cls, config: "Config") -> "CopyLimits":
        return cls(max_mb=config.workspace.copy_max_mb, max_files=config.workspace.copy_max_files)


@dataclass
class Source:
    """What is copied into a new session."""

    # "folder" | "zip" | "upload" (a zip uploaded: the board's, or v1's with unpack) | "file" (v1's, kept as one file)
    kind: str
    path: Path          # resolved folder or zip file (an upload: its claimed file)
    shown: str          # what copied_from records: str(path), "upload: <file name>", or "<root>/<path>"
    token: str | None = None
    filename: str | None = None   # a v1 upload: its file name (check_upload_name), the name of a "file" copied
    # a server folder of the HTTP API v1 (docs/SPEC.md §21.10, kind "folder" or "zip"): its root, resolved, and the
    # names below it as the root's folders spell them; the copy reads from the root by these names, never following
    # a link (nofollow), never by `path`
    root: Path | None = None
    parts: tuple[str, ...] = ()


# the names of the staging folders and claims this process works with now (staging_name, Uploads.claim_path until
# done_with): one of this run that is not here is left over, its removal having failed (a file still open on Windows)
_IN_USE: set[str] = set()
_IN_USE_LOCK = threading.Lock()


def _hold(name: str) -> str:
    with _IN_USE_LOCK:
        _IN_USE.add(name)
    return name


def done_with(path: Path | str) -> None:
    """The staging folder or claim `path` (staging_name, Uploads.claim_path) is no longer used: if it is still
    there, the next clean removes it."""
    with _IN_USE_LOCK:
        _IN_USE.discard(Path(path).name)


def staging_name() -> str:
    """A fresh name for a staging folder of this process (sessions/<name>; COPYING_NAME), in use until done_with."""
    return _hold(f"{COPYING_PREFIX}{os.getpid()}-{RUN}-{secrets.token_hex(4)}")


def left_over(pid: int, run: str, name: str) -> bool:
    """Whether the staging folder or claim `name`, named with `pid` and `run`, is left over by a process that no
    longer runs or by this one (and may be removed): of this pid, an earlier run (an earlier process with the same
    pid made it) or this run's when no longer in use (its removal failed); else not the pid of another live
    process on this computer."""
    if pid == os.getpid():
        if run != RUN:
            return True
        with _IN_USE_LOCK:
            return name not in _IN_USE
    return not pid_alive(pid)


def printable_line(value: Any, limit: int | None = None) -> str:
    """`value` as one line of printable characters (control characters and line breaks become spaces, runs of
    whitespace one space), cut to `limit` characters: a text from a file or a client shown to the user."""
    text = str(value or "").encode("utf-8", "replace").decode("utf-8")
    line = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    return line[:limit] if limit is not None else line


# ---- names and paths of the HTTP API v1 -------------------------------------------------------------------------
def _bad_name(text: str) -> CopyError:
    return CopyError(text, 400, code="bad_name")


def _shown(text: str) -> str:
    return printable_name(text if len(text) <= 120 else text[:120] + "...")


def _unprintable(text: str) -> bool:
    """Whether `text` holds a character a v1 name cannot hold (_UNPRINTABLE's categories)."""
    return any(unicodedata.category(ch) in _UNPRINTABLE for ch in text)


def check_upload_name(name: Any) -> str:
    """The file name a v1 upload gives (docs/SPEC.md §21.3 "The name"), returned as it is: it becomes the file's
    name in the session's folder, so a bad one is refused, never cleaned up (CopyError 400 `bad_name`). Refused:
    none, empty, '.' or '..'; a '/' or '\\'; a NUL or any other character that does not print (_UNPRINTABLE: C0
    and C1 controls, format characters, U+2028, U+2029; spaces such as U+3000 are fine); over NAME_MAX characters,
    or over NAME_BYTES_MAX bytes in UTF-8 or units in UTF-16; a name Windows cannot hold, on every OS (skills'
    portable rule: < > : " | ? *, a trailing dot or space, reserved device names); the form of a Windows short
    name (check_rel never serves one); case-folded, a name a copy leaves out (COPY_EXCLUDE_NAMES: .git,
    node_modules, ...) or one the session keeps for itself (SESSION_NAMES)."""
    if not isinstance(name, str) or not name:
        raise _bad_name("Give the file's name: ?filename=<name>.")
    shown = _shown(name)
    if name in (".", ".."):
        raise _bad_name(f"'{name}' is not a file name.")
    if "/" in name or "\\" in name:
        raise _bad_name(f"'{shown}': a file name cannot hold '/' or '\\'. An upload is one file at the top of the "
                        "session's folder; send folders as a zip with unpack=true.")
    if _unprintable(name):
        raise _bad_name(f"'{shown}': a file name cannot hold control characters or other characters that do not "
                        "print.")
    if (len(name) > NAME_MAX or len(name.encode("utf-8")) > NAME_BYTES_MAX
            or len(name.encode("utf-16-le")) // 2 > NAME_BYTES_MAX):
        raise _bad_name(f"'{shown}': the file name is too long (at most {NAME_MAX} characters and "
                        f"{NAME_BYTES_MAX} bytes in UTF-8).")
    try:
        _portable(name, name, "an upload")
    except SkillError as exc:
        raise _bad_name(str(exc)) from None
    if _short_name(name):
        raise _bad_name(f"'{shown}' has the form of a Windows short name (PROGRA~1), which can name another file; "
                        "give the file another name.")
    folded = name.casefold()
    if folded in _COPY_EXCLUDE_FOLDED:
        raise _bad_name(f"'{shown}': a copy into a session leaves out {', '.join(sorted(COPY_EXCLUDE_NAMES))}; "
                        "give the file another name.")
    if folded in SESSION_NAMES:
        raise _bad_name(f"'{shown}': the session's folder keeps {', '.join(sorted(SESSION_NAMES))} for itself; "
                        "give the file another name.")
    return name


def _short_name(part: str) -> bool:
    """Whether `part` has the form of a Windows 8.3 short name with a '~<digit>' tail (PROGRA~1, GIT~1,
    ABCDEF~1.TXT), which Windows may resolve to another file's long name (GIT~1 to .git)."""
    base, _, ext = part.partition(".")
    return 0 < len(base) <= 8 and len(ext) <= 3 and "." not in ext and re.search(r"~\d", base) is not None


def check_rel(path: Any, *, scratch: bool = False, portable: bool = False) -> tuple[str, ...]:
    """A path relative to a session's workspace (or to a root of [api] folders), with '/' (docs/SPEC.md §21.8): its
    names, () for "" or None (the folder itself); CopyError 400 `bad_name` otherwise. Refused everywhere: an
    absolute path, an empty name, '.' or '..', a NUL or a backslash, a name that case-folds to `.git`, and a name of
    the form of a Windows short name ('~<digit>': GIT~1 is .git, SCRATC~1 is .scratch), which NTFS resolves for a
    Linux hub too (WSL's /mnt/c, a Docker Desktop bind mount of a Windows folder). The other names Windows reads as
    others (< > : " | ? * and control characters: `.git::$INDEX_ALLOCATION` names .git; reserved device names; a
    trailing dot or space) are refused on a Windows hub only, where they would reach another file: on Linux and
    macOS the agents may make such names. `.scratch` (the members' folders, at the top) only with `scratch`;
    `.scratch/_skills` never. `portable`: a path a client makes up (a server folder's, §21.10): the Windows rules
    on every OS, no character that does not print (_UNPRINTABLE), and no `.scratch` at any depth, whatever
    `scratch` says."""
    if path is None or path == "":
        return ()
    if not isinstance(path, str):
        raise _bad_name("A path must be a text, relative to the folder, with '/'.")
    shown = _shown(path)
    if path.startswith("/"):
        raise _bad_name(f"Invalid path '{shown}': give it relative to the folder, with '/'.")
    if "\x00" in path:
        raise _bad_name(f"Invalid path '{shown}': it holds a NUL character.")
    if "\\" in path:
        raise _bad_name(f"Invalid path '{shown}': use '/' between names, never '\\'.")
    windows = portable or os.name == "nt"
    parts = tuple(path.split("/"))
    for part in parts:
        if part in ("", ".", ".."):
            raise _bad_name(f"Invalid path '{shown}': it holds an empty name, '.' or '..'.")
        if part.casefold() == ".git":
            raise _bad_name(f"Invalid path '{shown}': nothing in .git is reachable.")
        if _short_name(part):
            raise _bad_name(f"Invalid path '{shown}': '{_shown(part)}' has the form of a Windows short name "
                            "(PROGRA~1), which can name another file.")
        if portable and _unprintable(part):
            raise _bad_name(f"Invalid path '{shown}': names cannot hold characters that do not print.")
        if not windows:
            continue
        if _WIN_BAD_CHARS.search(part) or "\x7f" in part:
            raise _bad_name(f"Invalid path '{shown}': names cannot hold < > : \" | ? * or control characters "
                            "(Windows reads them as other names).")
        if part != part.rstrip(". "):
            raise _bad_name(f"Invalid path '{shown}': '{_shown(part)}' ends with a dot or a space, which Windows "
                            "drops from names.")
        if _WIN_RESERVED.fullmatch(part):
            raise _bad_name(f"Invalid path '{shown}': '{_shown(part)}' is a reserved device name on Windows.")
    folded = [part.casefold() for part in parts]
    if portable:
        if ".scratch" in folded:
            raise _bad_name(f"Invalid path '{shown}': a .scratch folder (the members' own) cannot be named.")
    elif folded[0] == ".scratch" and (not scratch or folded[1:2] == ["_skills"]):
        raise _bad_name(f"Invalid path '{shown}': .scratch/_skills is never served." if folded[1:2] == ["_skills"]
                        else f"Invalid path '{shown}': the members' .scratch folders need scratch=true.")
    return parts


# ---- the source -------------------------------------------------------------------------------------------------
def _within(path: Path, parent: Path) -> bool:
    return is_within(path, parent)


def _same(a: Path, b: Path) -> bool:
    return is_within(a, b) and is_within(b, a)


def hub_in_container(config: "Config") -> bool:
    """Whether this hub runs in a Docker container of the DreamTeam image (`dreamteam sandbox`, docker compose or a
    plain docker run), which sees only the folders mounted into it: a path of the computer is not there. The
    image's DREAMTEAM_IMAGE, not any container: DreamTeam run directly in a devcontainer sees its folders."""
    return config.skills_host_library_dir is not None or os.environ.get(netoff.IMAGE_ENV) == "1"


def _sandbox_text(home: Path, text: str) -> str:
    return (f"The hub runs in the Docker sandbox and sees only its home ({home}): {text} is not there. Upload the "
            "folder as a .zip in the New session dialog of the sessions page instead.")


def _own_folders(config: "Config") -> list[Path]:
    """DreamTeam's own folders outside the home: the MCP and skills libraries, the skills' environments and the
    hub tokens of the clients on this computer (resolved; one that cannot be resolved is skipped)."""
    folders = []
    for get in (lambda: config.library_dir, lambda: config.skills_library_dir, lambda: config.skill_envs_dir,
                hubauth.tokens_dir):
        try:
            folders.append(Path(get()).resolve())
        except (OSError, ValueError, RuntimeError):
            continue
    return folders


def _other_home(path: Path) -> Path | None:
    """The DreamTeam home (a folder with sessions/ and dreamteam.toml, as cli.is_home) that `path` is or lies in:
    another home than this hub's (its dreamteam.toml can hold API keys); None when there is none."""
    for folder in (path, *path.parents):
        try:
            if (folder / "sessions").is_dir() and (folder / CONFIG_FILE).is_file():
                return folder
        except OSError:
            continue
    return None


def _mount_root(path: Path) -> str | None:
    """Linux: the folder of its file system that is mounted at `path` (/proc/self/mountinfo's root field: "/" for a
    whole file system, the folder for a bind mount of one, e.g. docker's `-v ./app:/mnt/app`); None when `path` is
    no mount point there or mountinfo cannot be read (other OSes)."""
    try:
        with open(_MOUNTINFO, encoding="utf-8", errors="surrogateescape") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    unescape = lambda text: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)  # noqa: E731
    root = None
    for line in lines:
        fields = line.split(" ")
        if len(fields) > 4 and unescape(fields[4]) == str(path):
            root = unescape(fields[3])   # (the last mount at a point is the one seen)
    return root


def _disk_mount(path: Path) -> bool:
    """Whether `path` is a mount point where whole disks are mounted (POSIX): /mnt/<x> (WSL's drives too),
    /media/[<user>/]<x>, /run/media/<user>/<x>, /Volumes/<x>. A folder mounted elsewhere (a devcontainer's
    /workspaces/<x>) is not, nor on Linux a bind mount of a folder (docker's `-v ./my-app:/mnt/app`)."""
    if not any(Path(parent) in (path.parent, path.parent.parent) for parent in _MOUNT_PARENTS):
        return False
    try:
        if not os.path.ismount(path):
            return False
    except (OSError, ValueError):
        return False
    root = _mount_root(path)
    return root is None or root == "/"


def resolve_source(text: Any, *, home: Path, config: "Config", sandbox: bool, within: Path | None = None) -> Source:
    """The folder or .zip file `text` names, checked: CopyError (400) for a path that is not absolute, missing, a
    file that is not a .zip, a drive or file system root, the user's home folder or one containing it, inside or
    containing the DreamTeam home or one of DreamTeam's own folders, or a system folder (/proc, /sys, /dev).
    `sandbox`: the hub runs in a Docker container (hub_in_container: a missing or Windows path suggests the zip
    upload). `within`: a root of the HTTP API v1's server folders (resolved, docs/SPEC.md §21.10): the path, once
    resolved here, must still lie inside it, by text and by file identity (CopyError 403 `forbidden_folder`: a link
    swapped in on the way since the caller resolved it). Blocking."""
    # (one pair of quotes around the path is dropped: Explorer's "Copy as path" adds them)
    if not isinstance(text, str) or not unquote(text):
        raise CopyError("'copy_from' must be an absolute path of a folder or a .zip file.")
    text = unquote(text)
    try:
        given = Path(text).expanduser()
    except RuntimeError:
        given = Path(text)
    if not given.is_absolute():
        if sandbox and PureWindowsPath(text).is_absolute():
            raise CopyError(_sandbox_text(home, text))
        raise CopyError(f"The path to copy from must be absolute, got '{text}' (clients resolve relative paths "
                        "against their own current directory).")
    try:
        path = given.resolve()
        exists = os.path.lexists(path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CopyError(f"Cannot use {given}: {exc}") from None
    if within is not None and not _within(path, within):   # (named as given: never where it leads)
        raise CopyError(f"{given} does not lie inside its folder root any more: a link or a junction on the way leads "
                        "out of it.", 403, code="forbidden_folder")
    if not exists:
        if sandbox:
            raise CopyError(_sandbox_text(home, text))
        raise CopyError(f"Nothing to copy at {path}: no such folder or .zip file.")
    if path.is_file():
        if path.suffix.casefold() != ".zip":
            raise CopyError(f"{path} is a file but not a .zip archive: give a folder or a .zip file.")
        kind = "zip"
    elif path.is_dir():
        kind = "folder"
    else:
        raise CopyError(f"{path} is neither a folder nor a .zip file.")
    if kind == "folder":
        if path.parent == path or (os.name != "nt" and _disk_mount(path)):
            raise CopyError(f"{path} is the root of a drive or of the file system; give the folder to copy.")
        try:
            user_home: Path | None = Path.home().resolve()
        except (OSError, RuntimeError, ValueError):
            user_home = None
        if user_home is not None and _same(path, user_home):
            raise CopyError(f"{path} is your home folder itself; give the folder to copy, e.g. "
                            f"{path / 'my-project'}.")
        if user_home is not None and _within(user_home, path):
            raise CopyError(f"{path} contains your home folder {user_home}; give the folder to copy.")
    try:
        home = Path(home).resolve()
    except (OSError, RuntimeError, ValueError):
        home = Path(home)
    if _within(path, home) or _within(home, path):
        text_home = (f"{path} is inside the DreamTeam home {home} or contains it: DreamTeam's own files are never "
                     "copied into a session.")
        if sandbox:
            text_home += " In the Docker sandbox, upload the folder as a .zip instead."
        raise CopyError(text_home)
    for folder in _own_folders(config):
        if _within(path, folder) or _within(folder, path):
            raise CopyError(f"{path} is DreamTeam's own folder {folder}, or inside it or contains it: DreamTeam's "
                            "own files are never copied into a session.")
    other = _other_home(path)
    if other is not None:
        raise CopyError(f"{path} is a DreamTeam home ({other}) or inside one: its settings (API keys too) and "
                        "sessions are never copied into a session.")
    if os.name != "nt":
        for root in _SYSTEM_ROOTS:
            if _within(path, Path(root)):
                raise CopyError(f"{path} is a system folder ({root}) and is never copied into a session.")
    return Source(kind, path, str(path))


def check_target(source: Source, target: Path) -> None:
    """CopyError when the folder the session will work in (`target`, resolved) and the source overlap. The default
    workspace needs no call (it is inside the home, which resolve_source refuses)."""
    if source.kind == "folder" and _within(target, source.path):
        raise CopyError(f"The folder the session works in ({target}) is inside {source.path}: the copy would copy "
                        "itself. Choose a folder outside it.")
    if _within(source.path, target):
        raise CopyError(f"{source.path} is inside the folder the session works in ({target}); choose a folder "
                        "outside it, or copy from elsewhere.")


# ---- the copy ---------------------------------------------------------------------------------------------------
def zip_rules(limits: CopyLimits) -> ZipRules:
    """The rules a zip is unpacked by into a new session (skills.unpack_zip)."""
    entries = max(ZIP_MAX_ENTRIES, 2 * limits.max_files)
    return ZipRules(
        max_bytes=limits.max_bytes,
        too_big=f"The zip unpacks to more than {limits.max_mb} MB ([workspace] copy_max_mb).",
        max_entries=entries,
        too_many_entries=(f"The zip has more than {entries} entries (twice [workspace] copy_max_files; folders and "
                          "left-out ones count too): zip the folder without node_modules, .venv and the like."),
        max_files=limits.max_files,
        too_many_files=f"The zip holds more than {limits.max_files} files ([workspace] copy_max_files).",
        skip_files=COPY_SKIP_FILES,
        exclude_names=frozenset(n.casefold() for n in COPY_EXCLUDE_NAMES),
        exclude_paths=frozenset(COPY_EXCLUDE_PATHS),
        links="skip",
        portable=os.name == "nt",   # Windows cannot store the names _portable refuses
        # the default file systems of Windows and macOS ignore case: README and readme cannot both be written
        fold_collisions=os.name == "nt" or sys.platform == "darwin",
        names_in="the zip",
        folders=True,
        unwrap=True,
        redecode_names=True,
        max_ratio=None,   # sparse, zero-filled or database files compress far better; max_bytes caps a bomb
        declared_first=False,
    )


def _remove_staging(staging: Path) -> None:
    with contextlib.suppress(OSError):
        if os.path.lexists(staging):
            rmtree(staging)


def copy_into(source: Source, staging: Path, limits: CopyLimits,
              check: Callable[[], None] | None = None) -> dict[str, Any]:
    """Copy `source` into the new folder `staging` (it must not exist yet). Returns the result (see the module's
    SPEC section: source, kind, files, bytes, links, excluded, special_count, skipped_count, top); CopyError for a
    refusal, with nothing left behind (staging removed), as for anything `check` raises. Blocking."""
    if os.path.lexists(staging):   # never someone else's folder: only what this call made is removed
        raise CopyError(f"Cannot copy into {staging}: it exists already.", 500)
    try:
        if source.kind == "folder":
            return _copy_folder(source, staging, limits, check)
        return _copy_zip(source, staging, limits, check)
    except BaseException:
        _remove_staging(staging)
        raise


def _copy_folder(source: Source, staging: Path, limits: CopyLimits,
                 check: Callable[[], None] | None) -> dict[str, Any]:
    src = source.path
    kw: dict[str, Any] = {"exclude_names": COPY_EXCLUDE_NAMES, "exclude_paths": COPY_EXCLUDE_PATHS,
                          "skip_files": COPY_SKIP_FILES, "fold_names": True, "max_files": limits.max_files,
                          "max_bytes": limits.max_bytes, "check": check}
    before = _identity(src)
    try:
        scan_tree(src, hash=False, **kw)   # a huge folder is refused before a byte is written
        result = copy_tree(src, staging, modes="writable", **kw)
    except TreeLimit as exc:
        if exc.what == "files":
            raise CopyError(f"{src} holds more than {limits.max_files} files to copy ([workspace] copy_max_files; "
                            f"{_LEFT_OUT}). Nothing was copied.", 413) from None
        raise CopyError(f"{src} holds more than {limits.max_mb} MB to copy ([workspace] copy_max_mb; "
                        f"{_LEFT_OUT}). Nothing was copied.", 413) from None
    except FileExistsError as exc:   # staging is new: the file system folded two names of the source into one
        rel = _staged(staging, exc.filename)
        raise CopyError(folded_names_text(str(src), rel) if rel else str(exc)) from None
    except (NotADirectoryError, ValueError) as exc:
        raise CopyError(str(exc)) from None
    except OSError as exc:
        raise CopyError(f"Cannot copy into the session's folder: {exc.strerror or exc} "
                        f"({getattr(exc, 'filename', '') or ''})", 500) from None
    # a file deleted while the copy ran (an editor's temporary file, build output) is simply not there any more; a
    # folder that went, or the source itself moved or renamed meanwhile, would leave a session with a part of it
    vanished = set(result.vanished)
    errors = [(rel, reason) for rel, reason in result.errors if rel not in vanished]
    gone = [rel for rel, reason in errors if reason == treecopy.GONE_DIR]
    if _moved(before, _identity(src)) or gone:
        shown = f" ({_first(gone, len(gone))})" if gone else ""
        raise CopyError(f"{src} was moved, renamed or deleted while it was copied{shown}. Nothing was copied: "
                        "copy again once it stays in place.")
    if errors:
        shown = "; ".join(f"{rel} ({reason})" for rel, reason in errors[:_ERRORS_SHOWN])
        more = "; ..." if len(errors) > _ERRORS_SHOWN else ""
        shown = printable_name(shown)
        raise CopyError(f"Cannot read {len(errors)} file(s) in {src}: {shown}{more}. Nothing was copied: "
                        "check the permissions, or close the program that holds them.")
    return _result(source, result.files, result.bytes, result.links, result.excluded,
                   special=len(result.special), skipped=result.skipped, top=None, vanished=result.vanished)


def _identity(path: Path) -> os.stat_result | None:
    """The lstat of the source folder `path` (None: it cannot be read)."""
    try:
        return os.lstat(long_path(path))
    except OSError:
        return None


def _moved(before: os.stat_result | None, now: os.stat_result | None) -> bool:
    """The source folder is gone, no folder, or another one than before the copy (a file system without file ids
    tells by the path only)."""
    if now is None or not stat.S_ISDIR(now.st_mode):
        return True
    return bool(before is not None and before.st_ino and now.st_ino
                and (before.st_dev, before.st_ino) != (now.st_dev, now.st_ino))


def _staged(staging: Path, filename: Any) -> str | None:
    """`filename` (a path in `staging`, maybe in the extended-length form of Windows) relative to it, with '/';
    None when it is not in it."""
    name = str(filename or "")
    if name.startswith("\\\\?\\UNC\\"):
        name = "\\\\" + name[8:]
    elif name.startswith("\\\\?\\"):
        name = name[4:]
    try:
        rel = Path(name).relative_to(staging).as_posix()
    except ValueError:
        return None
    return rel if rel != "." else None


def _result(source: Source, files: int, size: int, links: list[str], excluded: list[str], *, special: int,
            skipped: int, top: str | None, vanished: list[str] | None = None) -> dict[str, Any]:
    """copy_into's result; the names from the source are shown as printable text (a name from a zip cannot drive
    the terminal or the page that shows it)."""
    vanished = vanished or []
    return {"source": source.shown, "kind": source.kind, "files": files, "bytes": size,
            "links": [printable_name(n) for n in links[:SHOWN_MAX]], "links_count": len(links),
            "excluded": [printable_name(n) for n in excluded[:SHOWN_MAX]], "excluded_count": len(excluded),
            "special_count": special, "skipped_count": skipped,
            "vanished": [printable_name(n) for n in vanished[:SHOWN_MAX]], "vanished_count": len(vanished),
            "top": printable_name(top) if top is not None else None}


def _copy_zip(source: Source, staging: Path, limits: CopyLimits,
              check: Callable[[], None] | None, rules: ZipRules | None = None) -> dict[str, Any]:
    """Unpack the zip `source` into the new folder `staging` by `rules` (default: zip_rules(limits)). An upload (a
    source with a token) is read from the file _open_upload opens, never through a link; a server folder's (a source
    with a root) from the file _open_in_root opens below its root."""
    with contextlib.ExitStack() as stack:
        zipped: BinaryIO | Path
        if source.token is not None or source.root is not None:
            zipped = stack.enter_context(_open_upload(source) if source.token is not None else _open_in_root(source))
            size = os.fstat(zipped.fileno()).st_size
        else:
            try:
                size = os.path.getsize(source.path)
            except FileNotFoundError:
                raise CopyError(f"Cannot read {source.shown}: no such file.") from None
            except OSError as exc:
                raise CopyError(f"Cannot read {source.shown}: {exc.strerror or exc}") from None
            zipped = source.path
        if size > limits.max_bytes:
            raise CopyError(f"The zip is larger than {limits.max_mb} MB ([workspace] copy_max_mb).", 413)
        os.mkdir(staging)
        try:
            result = unpack_zip(zipped, staging, rules or zip_rules(limits), check=check)
        except SkillError as exc:
            raise CopyError(str(exc), exc.status) from None
    return _result(source, result.files, result.bytes, result.links, result.excluded,
                   special=0, skipped=len(result.skipped), top=result.top)


def copy_sources(sources: list[Source], staging: Path, limits: CopyLimits,
                 check: Callable[[], None] | None = None) -> dict[str, Any]:
    """Copy the files of one v1 create (docs/SPEC.md §21.3 "Life") into the new folder `staging` (it must not exist
    yet): each source into a folder of its own beside `staging` (staging_name, so a left-over one is cleaned like a
    staging folder), then their top-level entries moved into `staging`. A zip (kind "zip", or "upload": a zip
    uploaded with unpack) is unpacked by the zip rules of a copy, with the names the session keeps for itself
    (SESSION_NAMES) also left out at its top level after the unwrap; a v1 upload of kind "file" becomes one file of
    its name. A server folder of the HTTP API v1 (docs/SPEC.md §21.10: kind "folder" or "zip" with a `root`) is read
    below its root without following a link (nofollow): a folder by _copy_server_folder, a zip unpacked as an
    upload's. One budget for all: `limits` caps the sum of their files and bytes (413). A top-level name that two
    sources share is 409 `file_conflict`: compared in NFC and case-folded on Windows and macOS, exactly elsewhere,
    and a name the file system folds onto another is one too. A folder without a root is not taken here (never
    with copy_tree: ValueError). Returns copy_into's result for all of them ("source": the shown names joined,
    "kind": "sources", "top": None) and "sources": [{shown, files, bytes}]. CopyError for a refusal, with nothing
    left behind, as for anything `check` raises. Blocking."""
    for source in sources:   # (before anything is made)
        if source.kind not in ("zip", "upload", "file", "folder") or (source.kind == "folder" and source.root is None):
            raise ValueError(f"copy_sources cannot copy a source of kind {source.kind!r}"
                             + (" without a root" if source.kind == "folder" else ""))
    if os.path.lexists(staging):   # never someone else's folder: only what this call made is removed
        raise CopyError(f"Cannot copy into {staging}: it exists already.", 500)
    os.mkdir(staging)
    parts: list[Path] = []
    try:
        results: list[dict[str, Any]] = []
        files = size = 0
        many = len(sources) > 1
        for source in sources:
            part = staging.parent / staging_name()
            if os.path.lexists(part):
                done_with(part)
                raise CopyError(f"Cannot copy into {part}: it exists already.", 500)
            parts.append(part)
            if source.kind == "file":
                got = _copy_file(source, part, limits, check, files=files, size=size, many=many)
            elif source.kind == "folder":
                got = _copy_server_folder(source, part, limits, check, files=files, size=size, many=many)
            else:
                got = _copy_zip(source, part, limits, check, rules=_left_rules(limits, files, size, many))
            files += got["files"]
            size += got["bytes"]
            results.append(got)
        _merge(list(zip(parts, sources)), staging)
        return _sources_result(sources, results)
    except BaseException:
        _remove_staging(staging)
        raise
    finally:
        for part in parts:   # (empty after the merge)
            try:
                _remove_staging(part)
            finally:
                done_with(part)


def _sum_texts(limits: CopyLimits) -> tuple[str, str]:
    return (f"The files to copy come to more than {limits.max_mb} MB in all ([workspace] copy_max_mb).",
            f"The files to copy are more than {limits.max_files} in all ([workspace] copy_max_files).")


def _left_rules(limits: CopyLimits, files: int, size: int, many: bool) -> ZipRules:
    """The zip rules of a copy for one of several sources, with what the sources before it left of the budget."""
    rules = dataclasses.replace(zip_rules(limits), exclude_top=SESSION_NAMES)
    if not many:
        return rules
    too_big, too_many = _sum_texts(limits)
    return dataclasses.replace(rules, max_bytes=max(0, limits.max_bytes - size), too_big=too_big,
                               max_files=max(0, limits.max_files - files), too_many_files=too_many)


def _copy_file(source: Source, part: Path, limits: CopyLimits, check: Callable[[], None] | None, *,
               files: int, size: int, many: bool) -> dict[str, Any]:
    """Copy a v1 upload of kind "file" into the new folder `part` as one file of its name (O_EXCL, 0o644), within
    what `files` and `size` (the sources before it) left of the budget. Read from the file _open_upload opens,
    never through a link."""
    name = check_upload_name(source.filename)
    too_big, too_many = _sum_texts(limits) if many else (
        f"The file is larger than {limits.max_mb} MB ([workspace] copy_max_mb).",
        f"The files to copy are more than {limits.max_files} ([workspace] copy_max_files).")
    written = 0
    with _open_upload(source) as fin:
        if files + 1 > limits.max_files:
            raise CopyError(too_many, 413)
        if size + os.fstat(fin.fileno()).st_size > limits.max_bytes:
            raise CopyError(too_big, 413)
        try:
            os.mkdir(long_path(part))
            fd_out = os.open(long_path(part / name), _FILE_FLAGS, 0o644)
            with os.fdopen(fd_out, "wb") as fout:
                while True:
                    if check is not None:
                        check()
                    chunk = fin.read(CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if size + written > limits.max_bytes:
                        raise CopyError(too_big, 413)
                    fout.write(chunk)
        except OSError as exc:
            raise CopyError(f"Cannot copy {source.shown} into the session's folder: {exc.strerror or exc}",
                            500) from None
    return _result(source, 1, written, [], [], special=0, skipped=0, top=None)


# ---- a server folder of the HTTP API v1 (docs/SPEC.md §21.10), read below its root without following a link ------
def _below(source: Source, rel: str) -> str:
    """A path nofollow names (relative to the root, '/') as `<root>/<path>` for a text: below the source, its shown
    name and what lies below it; on the way to it, the root's name and the path."""
    start = "/".join(source.parts)
    if not start or rel == start or rel.startswith(start + "/"):
        inner = rel[len(start):].lstrip("/")
        return f"{source.shown}/{inner}" if inner else source.shown
    return f"{source.shown.split('/', 1)[0]}/{rel}" if rel else source.shown.split("/", 1)[0]


def _refused(source: Source, exc: nofollow.Refused) -> CopyError:
    """A link met on the way (or swapped in), or a path that now leads out of the root: 403 `forbidden_folder`; a
    file with another name (a hard link) or a special file in the place of the .zip: the same, it is never read."""
    where = _below(source, exc.rel)
    named = f"{printable_name(where)}: " if where != source.shown else ""
    return CopyError(f"Cannot copy {source.shown}: {named}{exc.strerror}. Nothing was copied.", 403,
                     code="forbidden_folder")


def _open_in_root(source: Source) -> BinaryIO:
    """The .zip of a server folder, opened below its root by its names without following a link
    (nofollow.open_inside): never a link, a file with another name or a special file (403 `forbidden_folder`); 404
    `not_found` when it is gone, 400 when it is a folder now."""
    try:
        fd = nofollow.open_inside(source.root, source.parts)
    except nofollow.Refused as exc:
        raise _refused(source, exc) from None
    except FileNotFoundError:
        raise CopyError(f"Nothing to copy at {source.shown} any more: no such file.", 404, code="not_found") from None
    except (IsADirectoryError, NotADirectoryError):
        raise CopyError(f"{source.shown} is no .zip file any more.") from None
    except OSError as exc:
        raise CopyError(f"Cannot read {source.shown}: {exc.strerror or exc}", 500) from None
    try:
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _server_skip(parts: tuple[str, ...]) -> bool:
    """What a copy of a server folder leaves out unopened (nofollow.walk_inside's `skip`): COPY_EXCLUDE_NAMES at any
    depth (case-folded), the names the session keeps for itself (SESSION_NAMES) at the top, COPY_EXCLUDE_PATHS."""
    folded = parts[-1].casefold()
    return (folded in _COPY_EXCLUDE_FOLDED or (len(parts) == 1 and folded in SESSION_NAMES)
            or "/".join(parts) in COPY_EXCLUDE_PATHS)


def _server_entries(source: Source, *, open_files: bool) -> Iterator[nofollow.Entry]:
    """nofollow.walk_inside of the source below its root, its refusals as CopyError: a link met or swapped in, or a
    path out of the root, 403 `forbidden_folder` (the walk ends there); the folder gone 404 `not_found`. The other
    troubles of single entries come as ERROR entries."""
    walk = nofollow.walk_inside(source.root, source.parts, skip=_server_skip, open_files=open_files)
    try:
        while True:
            try:
                entry = next(walk)
            except StopIteration:
                return
            except nofollow.Refused as exc:
                raise _refused(source, exc) from None
            except FileNotFoundError:   # (the start folder: an entry's own errors are ERROR entries)
                raise CopyError(f"Nothing to copy at {source.shown} any more: no such folder.", 404,
                                code="not_found") from None
            except NotADirectoryError:
                raise CopyError(f"{source.shown} is no folder any more.") from None
            except OSError as exc:
                raise CopyError(f"Cannot read {source.shown}: {exc.strerror or exc}", 500) from None
            yield entry
    finally:
        walk.close()


def _copy_server_folder(source: Source, part: Path, limits: CopyLimits, check: Callable[[], None] | None, *,
                        files: int, size: int, many: bool) -> dict[str, Any]:
    """Copy the server folder `source` (docs/SPEC.md §21.10 `copy`) into the new folder `part`, within what `files`
    and `size` (the sources before it) left of the budget. Every folder and file is opened below the root by its
    names without following a link (nofollow.walk_inside), never by a path: a link met is left out (listed), one
    swapped in meanwhile, or a path that now leads out of the root, ends the copy (403 `forbidden_folder`). Left
    out: COPY_EXCLUDE_NAMES at any depth and the session's own names at the top (_server_skip), files with more
    than one name (st_nlink > 1: another name may lie outside the root), counted as excluded; links and special
    files, listed and counted as copy_tree does; COPY_SKIP_FILES without a word. A first walk only measures (413
    before a byte is written); the second copies, each file read through the walk's open file before the walk goes
    on, its bytes counted again. Files get the modes of copy_tree's "writable" and their modification time. A file
    or folder gone meanwhile is reported (vanished); one that cannot be read, or whose name another file took
    meanwhile, refuses the copy ("Cannot read ..."), as for a folder of copy_from."""
    shown = source.shown
    too_big, too_many = _sum_texts(limits) if many else (
        f"{shown} holds more than {limits.max_mb} MB to copy ([workspace] copy_max_mb; {_LEFT_OUT}). Nothing was "
        "copied.",
        f"{shown} holds more than {limits.max_files} files to copy ([workspace] copy_max_files; {_LEFT_OUT}). "
        "Nothing was copied.")
    count = total = 0
    with contextlib.closing(_server_entries(source, open_files=False)) as entries:   # measured before any write
        for entry in entries:
            if check is not None:
                check()
            if entry.kind == nofollow.FILE and entry.name not in COPY_SKIP_FILES:
                count += 1
                total += entry.size
                if files + count > limits.max_files:
                    raise CopyError(too_many, 413)
                if size + total > limits.max_bytes:
                    raise CopyError(too_big, 413)
    try:
        os.mkdir(long_path(part))
    except OSError as exc:
        raise CopyError(f"Cannot copy into the session's folder: {exc.strerror or exc}", 500) from None
    no_posix = treecopy.without_posix_bits(source.path)   # (the folder's own file system: a mount below the root)
    copied = written = special = skipped = 0
    links: list[str] = []
    excluded: list[str] = []
    vanished: list[str] = []
    errors: list[tuple[str, str]] = []
    with contextlib.closing(_server_entries(source, open_files=True)) as entries:
        for entry in entries:
            if check is not None:
                check()
            rel = entry.rel
            if entry.kind == nofollow.DIR:
                _make(part, entry, shown, folder=True)
            elif entry.kind == nofollow.FILE:
                if entry.name in COPY_SKIP_FILES:
                    skipped += 1
                    continue
                if files + copied + 1 > limits.max_files:
                    raise CopyError(too_many, 413)
                got = _copy_entry(part, entry, shown, lambda n: size + written + n > limits.max_bytes, too_big,
                                  no_posix, check)
                if isinstance(got, OSError):
                    errors.append((rel, got.strerror or str(got)))
                    continue
                copied += 1
                written += got
            elif entry.kind == nofollow.LINK:
                links.append(rel)
            elif entry.kind == nofollow.SPECIAL:
                special += 1
            elif entry.kind in (nofollow.HARD_LINK, nofollow.EXCLUDED):
                excluded.append(rel)
            else:   # ERROR: gone meanwhile (a file or a folder, also one renamed or moved on Windows), else unreadable
                error = entry.error
                if isinstance(error, FileNotFoundError):
                    vanished.append(rel)
                else:
                    errors.append((rel, (error.strerror if error is not None else None) or str(error)))
    if errors:
        listed = printable_name("; ".join(f"{rel} ({reason})" for rel, reason in errors[:_ERRORS_SHOWN]))
        more = "; ..." if len(errors) > _ERRORS_SHOWN else ""
        raise CopyError(f"Cannot read {len(errors)} file(s) in {shown}: {listed}{more}. Nothing was copied: check "
                        "the permissions, or close the program that holds them.")
    return _result(source, copied, written, links, excluded, special=special, skipped=skipped, top=None,
                   vanished=vanished)


def _make(part: Path, entry: nofollow.Entry, shown: str, *, folder: bool) -> int | None:
    """Make the folder of `entry` in `part` (None), or open its new file (O_EXCL: the descriptor); two names of the
    source that the file system written to takes for one (letter case, Unicode form) are a CopyError, as
    copy_from's."""
    target = long_path(part.joinpath(*entry.parts))
    try:
        if folder:
            os.mkdir(target)
            return None
        return os.open(target, _FILE_FLAGS, 0o600)
    except FileExistsError:
        raise CopyError(folded_names_text(shown, entry.rel)) from None
    except OSError as exc:
        raise CopyError(f"Cannot copy into the session's folder: {exc.strerror or exc}", 500) from None


def _copy_entry(part: Path, entry: nofollow.Entry, shown: str, over: Callable[[int], bool], too_big: str,
                no_posix: bool, check: Callable[[], None] | None) -> int | OSError:
    """Copy the walk's open FILE `entry` into `part` before the walk goes on: its bytes written (`over(n)` says when
    n more bytes pass the budget: 413 `too_big`), or the OSError of a read that failed (the partial file removed)."""
    fd_out = _make(part, entry, shown, folder=False)
    target = long_path(part.joinpath(*entry.parts))
    reader = entry.reader()
    got, head, failed = 0, b"", None
    with os.fdopen(fd_out, "wb") as fout:
        while True:
            if check is not None:
                check()
            try:
                chunk = reader.read(CHUNK)
            except OSError as exc:
                failed = exc
                break
            if not chunk:
                break
            if not got:
                head = chunk[:4]
            got += len(chunk)
            if over(got):
                raise CopyError(too_big, 413)
            try:
                fout.write(chunk)
            except OSError as exc:
                raise CopyError(f"Cannot copy {shown} into the session's folder: {exc.strerror or exc}",
                                500) from None
    if failed is not None:
        with contextlib.suppress(OSError):
            os.unlink(target)
        return failed
    try:
        os.utime(target, ns=(entry.mtime_ns, entry.mtime_ns))
        os.chmod(target, writable_bits(entry.mode, head, no_posix))
    except OSError as exc:
        raise CopyError(f"Cannot copy {shown} into the session's folder: {exc.strerror or exc}", 500) from None
    return got


def _name_key(name: str) -> str:
    """A top-level name as two sources clash on it: NFC and case-folded where the default file systems ignore case
    and Unicode form (Windows, macOS), as zip_rules' fold_collisions; exact elsewhere."""
    if os.name == "nt" or sys.platform == "darwin":
        return unicodedata.normalize("NFC", name).casefold()
    return name


def _clash(first: tuple[str, str], second: tuple[str, str]) -> CopyError:
    (a, a_from), (b, b_from) = first, second
    why = " (names that differ only in letter case or Unicode form are one name here)" if a != b else ""
    return CopyError(f"'{printable_name(a)}' ({printable_name(a_from)}) and '{printable_name(b)}' "
                     f"({printable_name(b_from)}) would both be at the top of the session's folder{why}. Rename one, "
                     "or put them in folders.", 409, code="file_conflict")


def _merge(parts: list[tuple[Path, Source]], staging: Path) -> None:
    """Move the top-level entries of every part into `staging`; CopyError 409 `file_conflict` for a name two parts
    share, found before anything is moved, or one the file system folds onto a name moved before."""
    seen: dict[str, tuple[str, str]] = {}
    moves: list[tuple[Path, str, str]] = []
    for part, source in parts:
        for name in sorted(os.listdir(long_path(part))):
            key = _name_key(name)
            if key in seen:
                raise _clash(seen[key], (name, source.shown))
            seen[key] = (name, source.shown)
            moves.append((part, name, source.shown))
    moved: list[tuple[str, str]] = []
    for part, name, shown in moves:
        target = long_path(staging / name)
        # (os.rename on POSIX replaces a file, or an empty folder, that is there: look first)
        if os.path.lexists(target):
            raise _clash(_folded_onto(staging, name, moved), (name, shown))
        try:
            os.rename(long_path(part / name), target)
        except OSError as exc:
            if os.path.lexists(target):
                raise _clash(_folded_onto(staging, name, moved), (name, shown)) from None
            raise CopyError(f"Cannot copy into the session's folder: {exc.strerror or exc}", 500) from None
        moved.append((name, shown))


def _folded_onto(staging: Path, name: str, moved: list[tuple[str, str]]) -> tuple[str, str]:
    """The entry moved before that the file system takes `name` for (by file identity), for the clash's text."""
    for other, shown in moved:
        with contextlib.suppress(OSError, ValueError):
            if os.path.samefile(long_path(staging / other), long_path(staging / name)):
                return other, shown
    return moved[-1] if moved else (name, "?")


def _sources_result(sources: list[Source], results: list[dict[str, Any]]) -> dict[str, Any]:
    def joined(key: str) -> list[str]:
        return [name for r in results for name in r[key]][:SHOWN_MAX]

    def total(key: str) -> int:
        return sum(int(r[key]) for r in results)

    return {"source": ", ".join(s.shown for s in sources), "kind": "sources", "files": total("files"),
            "bytes": total("bytes"), "links": joined("links"), "links_count": total("links_count"),
            "excluded": joined("excluded"), "excluded_count": total("excluded_count"),
            "special_count": total("special_count"), "skipped_count": total("skipped_count"),
            "vanished": joined("vanished"), "vanished_count": total("vanished_count"), "top": None,
            "sources": [{"shown": s.shown, "files": r["files"], "bytes": r["bytes"]}
                        for s, r in zip(sources, results)]}


def size_text(n: int) -> str:
    """A size for people: "N bytes", "N.N KB" or "N.N MB"."""
    n = int(n or 0)
    if n < 1024:
        return f"{n} bytes"
    if n < 2 ** 20:
        return f"{n / 1024:.1f} KB"
    return f"{n / 2 ** 20:.1f} MB"


def _first(items: list[str], count: int) -> str:
    return ", ".join(printable_name(n) for n in items[:3]) + (", ..." if count > 3 else "")


def copy_text(result: dict[str, Any]) -> str:
    """copy_into's result for the CLI, the MCP and the console: what was copied, and (a second line) what was left
    out. Names are shown as printable text."""
    top = f" (the contents of {printable_name(result['top'])}/ in the zip)" if result.get("top") else ""
    text = (f"Copied {result.get('files', 0)} files ({size_text(result.get('bytes', 0))}) from "
            f"{printable_name(result.get('source'))}{top} into the session's folder.")
    left = []
    if result.get("links_count"):
        left.append(f"{result['links_count']} link(s) ({_first(result.get('links') or [], result['links_count'])})")
    if result.get("excluded_count"):
        left.append(f"{result['excluded_count']} folder(s) by name "
                    f"({_first(result.get('excluded') or [], result['excluded_count'])})")
    if result.get("special_count"):
        left.append(f"{result['special_count']} special file(s)")
    if result.get("vanished_count"):
        left.append(f"{result['vanished_count']} file(s) deleted while they were copied "
                    f"({_first(result.get('vanished') or [], result['vanished_count'])})")
    if left:
        text += "\nLeft out: " + "; ".join(left) + "."
    return text


# ---- uploads ----------------------------------------------------------------------------------------------------
def _upload_name(filename: Any) -> str:
    """An uploaded file's name as shown: its last part, one printable line of at most NAME_MAX characters."""
    base = PurePosixPath(str(filename or "").replace("\\", "/")).name
    return printable_line(base, NAME_MAX) or "upload.zip"


def _read_meta(path: Path) -> dict[str, Any] | None:
    """An upload's json (None: missing, a link where the OS can tell, or not a json object)."""
    try:
        with os.fdopen(os.open(path, _READ_FLAGS), "rb") as f:
            data = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _stamp(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _gone(token: Any, what: str = "zip") -> CopyError:
    again = "the file" if what == "file" else "the .zip"
    return CopyError(f"No upload {token}: uploads wait an hour for their session and are used once. Upload "
                     f"{again} again.", 404)


def _upload_what(source: Source) -> str:
    """What an upload source was uploaded as: "file" for v1's (it has a file name), "zip" for the board's."""
    return "file" if source.filename is not None else "zip"


def _lone_file(st: os.stat_result) -> bool:
    """A regular file with no other name: what the uploads store holds as the hub wrote it. A link, a folder, a
    special file or a file with more hard links (another name of it may lie anywhere) is not."""
    return stat.S_ISREG(st.st_mode) and st.st_nlink == 1


def _open_upload(source: Source) -> BinaryIO:
    """Open the upload `source` (a claim, or a v1 "file") for reading, never through a link: its name must be a
    _lone_file by lstat, it is opened with O_NOFOLLOW where the OS has it, and the opened file must be the one lstat
    saw (st_dev, st_ino) and a _lone_file by fstat, so a link or another file swapped in meanwhile is caught on
    Windows too. In the Docker sandbox the hub reads with more rights than the agents: a link to a file of the hub's
    (/proc/<pid>/environ) put in the store would otherwise be copied into a session's folder. CopyError 404 (the
    upload is gone: Upload ... again) for anything else, 500 when it cannot be read; with no token, 400."""
    refusal = (_gone(source.token, _upload_what(source)) if source.token is not None
               else CopyError(f"Cannot read {source.shown}: it is no regular file with one name."))
    path = long_path(source.path)
    try:
        before = os.lstat(path)
        if not _lone_file(before):
            raise refusal
        fd = os.open(path, _READ_FLAGS)
    except FileNotFoundError:   # (another create took it: see Uploads.claim)
        raise refusal from None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):   # a link swapped in (O_NOFOLLOW: ELOOP; EMLINK on BSD)
            raise refusal from None
        raise CopyError(f"Cannot read {source.shown}: {exc.strerror or exc}", 500) from None
    try:
        try:
            st = os.fstat(fd)
        except OSError as exc:
            raise CopyError(f"Cannot read {source.shown}: {exc.strerror or exc}", 500) from None
        same = not (before.st_ino and st.st_ino) or (before.st_dev, before.st_ino) == (st.st_dev, st.st_ino)
        if not (_lone_file(st) and same):
            raise refusal
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


class Uploads:
    """Zips the sessions page uploads for a new session (POST /api/uploads): <root>/<token>.zip and its
    <token>.json ({"filename", "bytes", "at"}), kept UPLOAD_TTL seconds; a create claims one
    (<token>.claimed-<pid>-<run>-<hex8>.zip) and removes it whatever happens.
    The uploads of the HTTP API v1 (POST /api/v1/uploads, docs/SPEC.md §21.3) live in a store of their own (another
    root): any file, <root>/<token>.bin and its <token>.json ({"filename", "bytes", "at", "unpack", "caller"}),
    received with kind="file"; a create checks them (peek_many), claims them all (claim_many: .claimed-...bin) and
    settles every claim when it ends (settle: released when used, else unclaimed, usable again)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        # claims are made one at a time: a rename on Windows is not exclusive (two can both succeed, the second
        # moving the first one's claimed file), and one hub owns a home
        self._claiming = threading.Lock()

    def _make_root(self) -> None:
        """Make the store's folder (and the board's uploads folder it lies in, for v1's) the hub's alone, 0o700 on
        POSIX: in the Docker sandbox the hub runs with the agents' group and umask 002, so a folder of the default
        mode would let the agents rename the store, put a link or another file in place of an upload or rewrite
        its json. (_open_upload never follows a link whatever the modes.)"""
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            return
        for folder in (self.root, self.root.parent) if self.root.parent.name == UPLOADS_DIR else (self.root,):
            with contextlib.suppress(OSError):
                os.chmod(folder, 0o700)

    def _pending(self) -> int:
        self._make_root()
        self.clean()
        count = 0
        with os.scandir(self.root) as it:
            for entry in it:
                m = PENDING_NAME.match(entry.name)
                if m and m.group(2) in ("zip", "bin", "part"):
                    count += 1
        return count

    def _finish(self, token: str, part: Path, name: str, total: int, ext: str = "zip",
                extra: dict[str, Any] | None = None) -> None:
        meta = self.root / f"{token}.json"
        data = json.dumps({"filename": name, "bytes": total, "at": time.time(), **(extra or {})}, ensure_ascii=False)
        with os.fdopen(os.open(meta, _FILE_FLAGS, 0o600), "wb") as f:   # (new, the hub's alone, as the .part)
            f.write(data.encode("utf-8"))
        try:
            os.replace(part, self.root / f"{token}.{ext}")
        except BaseException:
            with contextlib.suppress(OSError):
                meta.unlink()
            raise

    async def receive(self, chunks: AsyncIterator[bytes], *, filename: Any, length: str | None,
                      limits: CopyLimits, kind: str = "zip", unpack: bool = False,
                      max_pending: int = UPLOAD_MAX_PENDING, caller: str | None = None) -> dict[str, Any]:
        """Store an uploaded zip streamed as `chunks` (at most `limits.max_bytes`; `length`: the Content-Length
        header, checked before reading). Returns {"upload": token, "filename", "bytes", "expires_in"}; CopyError
        413 (too large), 429 `too_many_uploads` (`max_pending` uploads wait), 400 (empty, not a zip). Whatever
        goes wrong (an exception from `chunks` too, which goes on to the caller), the partial file is removed.
        `kind="file"` (the HTTP API v1, docs/SPEC.md §21.3): any file, kept as <token>.bin under the name
        `filename`, which check_upload_name refuses (400 `bad_name`, before anything is read) rather than cleans
        up; a zip only with `unpack` (unpacked by the create); its json records `unpack` and `caller` (who
        uploaded it: UPLOAD_CALLERS). Returns {"upload", "filename", "bytes", "unpack", "expires_in"}. `unpack`
        is a bool (the route parses the query's text strictly), else ValueError."""
        if kind not in ("zip", "file"):
            raise ValueError(f"unknown upload kind {kind!r}")
        if type(unpack) is not bool:
            raise ValueError(f"unpack must be a bool, not {unpack!r}")
        if kind == "file":
            if caller not in UPLOAD_CALLERS:
                raise ValueError(f"unknown upload caller {caller!r}")
            name = check_upload_name(filename)
        elif unpack or caller is not None:
            raise ValueError("a zip upload of the sessions page has no unpack and no caller")
        else:
            name = _upload_name(filename)
        too_big = f"The file is larger than {limits.max_mb} MB ([workspace] copy_max_mb)."
        if isinstance(length, str) and length.isascii() and length.isdigit() and int(length) > limits.max_bytes:
            raise CopyError(too_big, 413)
        pending = await asyncio.to_thread(self._pending)
        if pending >= max_pending:
            raise CopyError(f"{pending} uploads already wait for a session; each is removed when a session uses it "
                            "or an hour after its upload. Try again later.", 429, code="too_many_uploads")
        token = secrets.token_urlsafe(18)
        part = self.root / f"{token}.part"
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        f = os.fdopen(fd, "wb")
        total = 0
        done = False
        try:
            buf = bytearray()
            async for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if total > limits.max_bytes:
                    raise CopyError(too_big, 413)
                buf += chunk
                if len(buf) >= CHUNK:
                    data = bytes(buf)
                    buf.clear()
                    await asyncio.to_thread(f.write, data)
            if buf:
                await asyncio.to_thread(f.write, bytes(buf))
            await asyncio.to_thread(f.close)
            if total == 0:
                raise CopyError("The upload is empty.")
            if kind == "zip" or unpack:
                if not await asyncio.to_thread(zipfile.is_zipfile, part):
                    raise CopyError("The file is not a zip archive." if kind == "zip" else
                                    "The file is not a zip archive: send it without unpack=true to keep it as one "
                                    "file.")
            if kind == "file":
                await asyncio.to_thread(self._finish, token, part, name, total, "bin",
                                        {"unpack": bool(unpack), "caller": caller})
            else:
                await asyncio.to_thread(self._finish, token, part, name, total)
            done = True
        finally:
            if not done:
                with contextlib.suppress(OSError, ValueError):
                    f.close()
                with contextlib.suppress(OSError):
                    os.unlink(part)
        if kind == "file":
            return {"upload": token, "filename": name, "bytes": total, "unpack": bool(unpack),
                    "expires_in": UPLOAD_TTL}
        return {"upload": token, "filename": name, "bytes": total, "expires_in": UPLOAD_TTL}

    @staticmethod
    def _checked(token: Any, refusal: str = "'copy_from_upload' must be a token from POST /api/uploads.") -> str:
        if not isinstance(token, str) or not UPLOAD_TOKEN.fullmatch(token):
            raise CopyError(refusal)
        return token

    def peek(self, token: Any) -> dict[str, Any]:
        """The upload `token` names ({"filename", "bytes", "at"}): CopyError 400 for a malformed token (before any
        path is built), 404 for one that is unknown, used, older than UPLOAD_TTL or no _lone_file. Blocking."""
        token = self._checked(token)
        gone = _gone(token)
        try:
            st = os.lstat(self.root / f"{token}.zip")
        except OSError:
            raise gone from None
        if not _lone_file(st):   # (a link or another name of a file is no upload: see _open_upload)
            raise gone
        meta = _read_meta(self.root / f"{token}.json") or {}
        at = _stamp(meta.get("at"))
        at = st.st_mtime if at is None else at
        if time.time() - at > UPLOAD_TTL:
            raise gone
        size = meta.get("bytes")
        return {"filename": _upload_name(meta.get("filename")),
                "bytes": size if isinstance(size, int) and not isinstance(size, bool) else st.st_size, "at": at}

    def claim_path(self, token: str, ext: str = "zip") -> Path:
        """A fresh name for the claim of `token` (nothing is renamed): the caller knows it before `claim` runs. In
        use until `release` (or `unclaim`). `ext`: "zip" (the board's), "bin" (v1's)."""
        token = self._checked(token)
        if ext not in ("zip", "bin"):
            raise ValueError(f"unknown upload extension {ext!r}")
        return self.root / _hold(f"{token}.claimed-{os.getpid()}-{RUN}-{secrets.token_hex(4)}.{ext}")

    def claim(self, token: Any, claimed: Path) -> Source:
        """Take the upload for one create: renamed to `claimed` (claim_path). CopyError as peek's, 404 when another
        create took it first. Blocking."""
        with self._claiming:
            meta = self.peek(token)
            try:
                os.rename(self.root / f"{token}.zip", claimed)
            except FileNotFoundError:
                raise _gone(token) from None
            except OSError as exc:
                raise CopyError(f"Cannot use the upload {token}: {exc.strerror or exc}", 500) from None
        return Source("upload", Path(claimed), f"upload: {meta['filename']}", token)

    def release(self, token: str, claimed: Path) -> None:
        """Remove a claimed upload and its json (best effort: `clean` gets what is still open on Windows, the claim
        being no longer in use). Nothing when `claimed` does not exist (the claim failed or never ran: another create
        may hold the upload)."""
        try:
            if not os.path.lexists(claimed):
                return
            with contextlib.suppress(OSError):
                os.unlink(claimed)
            with contextlib.suppress(OSError):
                os.unlink(self.root / f"{token}.json")
        finally:
            done_with(claimed)

    # ---- the uploads of the HTTP API v1 (docs/SPEC.md §21.3) ----
    def peek_many(self, tokens: Any, *, caller: str) -> list[dict[str, Any]]:
        """The v1 uploads `tokens` names (a list, none twice), each {"token", "filename", "bytes", "at", "unpack"},
        without claiming them: CopyError 400 for a malformed token or one given twice (before any path is built),
        404 for one that is unknown, used, older than UPLOAD_TTL, no _lone_file (a link) or uploaded by the other
        kind of caller (`caller`: an API client never uses the owner's uploads, nor the reverse). Blocking."""
        if not isinstance(tokens, list):
            raise CopyError("'files' must be a list of upload tokens.")
        refusal = f"'files' must hold tokens from POST {API_UPLOAD_PATH}."
        checked = [self._checked(token, refusal) for token in tokens]
        if len(set(checked)) != len(checked):
            raise CopyError("'files' names an upload twice.")
        return [self._peek_file(token, caller) for token in checked]

    def _peek_file(self, token: str, caller: str) -> dict[str, Any]:
        gone = _gone(token, "file")
        try:
            st = os.lstat(self.root / f"{token}.bin")
        except OSError:
            raise gone from None
        meta = _read_meta(self.root / f"{token}.json")
        # (a link or another name of a file is no upload: see _open_upload; a json that does not name this caller,
        # or that cannot be read, cannot say whose the upload is)
        if not _lone_file(st) or meta is None or caller not in UPLOAD_CALLERS or meta.get("caller") != caller:
            raise gone
        at = _stamp(meta.get("at"))
        at = st.st_mtime if at is None else at
        if time.time() - at > UPLOAD_TTL:
            raise gone
        filename = meta.get("filename")
        try:
            check_upload_name(filename)
        except CopyError:
            raise gone from None
        size = meta.get("bytes")
        return {"token": token, "filename": filename,
                "bytes": size if isinstance(size, int) and not isinstance(size, bool) else st.st_size, "at": at,
                "unpack": meta.get("unpack") is True}

    def claim_many(self, tokens: Any, *, caller: str) -> list[Source]:
        """Take the v1 uploads `tokens` names for one create, all or none, under the lock claims are made under:
        checked again (peek_many's CopyError), then each renamed to a claim (claim_path, .bin). 404 when another
        create took one first, 500 when a rename fails; whatever fails, the ones already taken are given back
        (unclaim). Each Source is of kind "upload" (a zip to unpack) or "file", with its token and file name. The
        create settles every claim when it ends, whatever happens (settle). Blocking."""
        taken: list[Source] = []
        with self._claiming:
            try:
                for meta in self.peek_many(tokens, caller=caller):
                    token = meta["token"]
                    claimed = self.claim_path(token, "bin")
                    try:
                        os.rename(self.root / f"{token}.bin", claimed)
                    except FileNotFoundError:
                        done_with(claimed)
                        raise _gone(token, "file") from None
                    except OSError as exc:
                        done_with(claimed)
                        raise CopyError(f"Cannot use the upload {token}: {exc.strerror or exc}", 500) from None
                    except BaseException:
                        done_with(claimed)
                        raise
                    taken.append(Source("upload" if meta["unpack"] else "file", claimed,
                                        f"upload: {meta['filename']}", token, filename=meta["filename"]))
            except BaseException:
                for source in reversed(taken):
                    self.unclaim(source)
                raise
        return taken

    def unclaim(self, source: Source) -> bool:
        """Give back a claimed v1 upload (the create failed: a 4xx, a 5xx, a cancel): the claim is renamed back to
        <token>.bin (or .zip), so the token works again until its hour is over (the clock is the json's `at`,
        unchanged). False, logged, when that fails or `source` is no claim of this store: the next clean removes
        what is left. Never raises for a file system error, and the claim is no longer in use afterwards
        (done_with): call it in a `finally`."""
        claimed = Path(source.path)
        try:
            m = CLAIMED_NAME.fullmatch(claimed.name)
            if m is None or claimed.parent != self.root or m.group(1) != source.token:
                return False
            try:
                os.rename(claimed, self.root / f"{m.group(1)}.{m.group(5)}")
            except OSError as exc:
                log.warning("could not give back the upload %s: %s", m.group(1), exc)
                return False
            return True
        finally:
            done_with(claimed)

    def settle(self, sources: Iterable[Source], *, used: bool) -> None:
        """Settle every claim of one create when it ends, whatever happened (synchronous: call it in a `finally`):
        `used` (the session was created) removes them (release), otherwise each is given back (unclaim). Sources
        that are no uploads are passed over. Never raises for a file system error."""
        for source in sources:
            if source.token is None or source.kind not in ("upload", "file"):
                continue
            try:
                if used:
                    self.release(source.token, source.path)
                else:
                    self.unclaim(source)
            except Exception:   # (the others are settled all the same)
                log.exception("could not settle the upload %s", source.token)
                done_with(source.path)

    def clean(self) -> None:
        """Remove (best effort) the uploads older than UPLOAD_TTL (by their json's "at", else the file's time) and
        the claims left over (left_over: by processes that no longer run, or this run's no longer in use); a claim
        a create of this run uses or one of another live process is never touched, and names of neither kind are
        left alone. Blocking."""
        now = time.time()
        try:
            entries = list(os.scandir(self.root))
        except OSError:
            return
        ages: dict[str, float | None] = {}
        for entry in entries:
            pending = PENDING_NAME.match(entry.name)
            claimed = CLAIMED_NAME.match(entry.name)
            try:
                if pending:
                    token = pending.group(1)
                    if token not in ages:
                        ages[token] = _stamp((_read_meta(self.root / f"{token}.json") or {}).get("at"))
                    at = ages[token]
                    if at is None:
                        at = entry.stat(follow_symlinks=False).st_mtime
                    if now - at > UPLOAD_TTL:
                        os.unlink(entry.path)
                elif claimed:
                    if left_over(int(claimed.group(2)), claimed.group(3), entry.name):
                        os.unlink(entry.path)
                        with contextlib.suppress(OSError):
                            os.unlink(self.root / f"{claimed.group(1)}.json")
            except OSError:
                continue
