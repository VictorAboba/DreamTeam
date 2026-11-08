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
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable

from . import hubauth, netoff, treecopy
from .config import CONFIG_FILE
from .console import unquote
from .skills import ZIP_MAX_ENTRIES, SkillError, ZipRules, folded_names_text, printable_name, unpack_zip
from .treecopy import TreeLimit, copy_tree, is_within, scan_tree
from .workspace import long_path, pid_alive, rmtree

if TYPE_CHECKING:
    from .config import Config

COPY_EXCLUDE_NAMES = treecopy.COPY_EXCLUDE_NAMES
COPY_EXCLUDE_PATHS = treecopy.EXPORT_EXCLUDE_PATHS      # a DreamTeam workspace's skills mirror and stand-in folder
COPY_SKIP_FILES = treecopy.COPY_SKIP_FILES              # .DS_Store, Thumbs.db: left out of a folder and of a zip
COPYING_PREFIX = ".copying-"     # sessions/.copying-<pid>-<run>-<hex8>: a copy being staged (never a session id)
COPYING_NAME = re.compile(r"\.copying-(\d+)-([0-9a-f]{8})-([0-9a-f]{8})\Z")
# this process's run: with its pid it names the staging folders and claims it makes. A pid alone is not enough:
# the hub of the Docker sandbox has the same pid at every start, so a killed container's leftovers would look live
RUN = secrets.token_hex(4)
UPLOADS_DIR = "uploads"          # <home>/.dreamteam/uploads
UPLOAD_PATH = "/api/uploads"
UPLOAD_TTL = 3600                # seconds an upload waits for its session
UPLOAD_MAX_PENDING = 8           # uploads waiting at once
UPLOAD_TOKEN = re.compile(r"[A-Za-z0-9_-]{24}\Z")   # secrets.token_urlsafe(18)
PENDING_NAME = re.compile(r"([A-Za-z0-9_-]{24})\.(zip|json|part)\Z")          # an upload waiting (or arriving)
# claimed by a create: <token>.claimed-<pid>-<run>-<hex8>.zip
CLAIMED_NAME = re.compile(r"([A-Za-z0-9_-]{24})\.claimed-(\d+)-([0-9a-f]{8})-([0-9a-f]{8})\.zip\Z")
NAME_MAX = 200                   # characters kept of an upload's file name
SHOWN_MAX = 20                   # left-out paths listed in a result
SOURCE_SHOWN_MAX = 300           # characters of copied_from.source shown by summary() and session_line
CHUNK = 1 << 20
_ERRORS_SHOWN = 5
_LEFT_OUT = ".git, node_modules, .venv, venv and __pycache__ do not count"
_SYSTEM_ROOTS = ("/proc", "/sys", "/dev")
_MOUNT_PARENTS = ("/mnt", "/media", "/run/media", "/Volumes")   # where whole disks are mounted (POSIX)
_MOUNTINFO = "/proc/self/mountinfo"   # Linux: what is mounted where (_mount_root)


class CopyError(ValueError):
    """A copy that cannot be made; `status` is the HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


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

    kind: str           # "folder" | "zip" | "upload"
    path: Path          # resolved folder or zip file (an upload: its claimed file)
    shown: str          # what copied_from records: str(path), or "upload: <file name>"
    token: str | None = None


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


def resolve_source(text: Any, *, home: Path, config: "Config", sandbox: bool) -> Source:
    """The folder or .zip file `text` names, checked: CopyError (400) for a path that is not absolute, missing, a
    file that is not a .zip, a drive or file system root, the user's home folder or one containing it, inside or
    containing the DreamTeam home or one of DreamTeam's own folders, or a system folder (/proc, /sys, /dev).
    `sandbox`: the hub runs in a Docker container (hub_in_container: a missing or Windows path suggests the zip
    upload). Blocking."""
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
              check: Callable[[], None] | None) -> dict[str, Any]:
    gone = _gone(source.token) if source.kind == "upload" else None
    try:
        size = os.path.getsize(source.path)
    except FileNotFoundError:
        if gone is not None:   # (another create took it: see Uploads.claim)
            raise gone from None
        raise CopyError(f"Cannot read {source.shown}: no such file.") from None
    except OSError as exc:
        raise CopyError(f"Cannot read {source.shown}: {exc.strerror or exc}") from None
    if size > limits.max_bytes:
        raise CopyError(f"The zip is larger than {limits.max_mb} MB ([workspace] copy_max_mb).", 413)
    os.mkdir(staging)
    try:
        result = unpack_zip(source.path, staging, zip_rules(limits), check=check)
    except SkillError as exc:
        if gone is not None and not os.path.lexists(source.path):
            raise gone from None
        raise CopyError(str(exc), exc.status) from None
    return _result(source, result.files, result.bytes, result.links, result.excluded,
                   special=0, skipped=len(result.skipped), top=result.top)


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
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _stamp(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _gone(token: Any) -> CopyError:
    return CopyError(f"No upload {token}: uploads wait an hour for their session and are used once. Upload the "
                     ".zip again.", 404)


class Uploads:
    """Zips the sessions page uploads for a new session (POST /api/uploads): <root>/<token>.zip and its
    <token>.json ({"filename", "bytes", "at"}), kept UPLOAD_TTL seconds; a create claims one
    (<token>.claimed-<pid>-<run>-<hex8>.zip) and removes it whatever happens."""

    def __init__(self, root: Path):
        self.root = Path(root)
        # claims are made one at a time: a rename on Windows is not exclusive (two can both succeed, the second
        # moving the first one's claimed file), and one hub owns a home
        self._claiming = threading.Lock()

    def _pending(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        self.clean()
        count = 0
        with os.scandir(self.root) as it:
            for entry in it:
                m = PENDING_NAME.match(entry.name)
                if m and m.group(2) in ("zip", "part"):
                    count += 1
        return count

    def _finish(self, token: str, part: Path, name: str, total: int) -> None:
        meta = self.root / f"{token}.json"
        meta.write_text(json.dumps({"filename": name, "bytes": total, "at": time.time()}, ensure_ascii=False),
                        encoding="utf-8")
        try:
            os.replace(part, self.root / f"{token}.zip")
        except BaseException:
            with contextlib.suppress(OSError):
                meta.unlink()
            raise

    async def receive(self, chunks: AsyncIterator[bytes], *, filename: Any, length: str | None,
                      limits: CopyLimits) -> dict[str, Any]:
        """Store an uploaded zip streamed as `chunks` (at most `limits.max_bytes`; `length`: the Content-Length
        header, checked before reading). Returns {"upload": token, "filename", "bytes", "expires_in"}; CopyError
        413 (too large), 429 (UPLOAD_MAX_PENDING uploads wait), 400 (empty, not a zip). Whatever goes wrong (an
        exception from `chunks` too, which goes on to the caller), the partial file is removed."""
        name = _upload_name(filename)
        too_big = f"The file is larger than {limits.max_mb} MB ([workspace] copy_max_mb)."
        if isinstance(length, str) and length.isascii() and length.isdigit() and int(length) > limits.max_bytes:
            raise CopyError(too_big, 413)
        pending = await asyncio.to_thread(self._pending)
        if pending >= UPLOAD_MAX_PENDING:
            raise CopyError(f"{pending} uploads already wait for a session; each is removed when a session uses it "
                            "or an hour after its upload. Try again later.", 429)
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
            if not await asyncio.to_thread(zipfile.is_zipfile, part):
                raise CopyError("The file is not a zip archive.")
            await asyncio.to_thread(self._finish, token, part, name, total)
            done = True
        finally:
            if not done:
                with contextlib.suppress(OSError, ValueError):
                    f.close()
                with contextlib.suppress(OSError):
                    os.unlink(part)
        return {"upload": token, "filename": name, "bytes": total, "expires_in": UPLOAD_TTL}

    @staticmethod
    def _checked(token: Any) -> str:
        if not isinstance(token, str) or not UPLOAD_TOKEN.fullmatch(token):
            raise CopyError("'copy_from_upload' must be a token from POST /api/uploads.")
        return token

    def peek(self, token: Any) -> dict[str, Any]:
        """The upload `token` names ({"filename", "bytes", "at"}): CopyError 400 for a malformed token (before any
        path is built), 404 for one that is unknown, used or older than UPLOAD_TTL. Blocking."""
        token = self._checked(token)
        gone = _gone(token)
        try:
            st = os.stat(self.root / f"{token}.zip")
        except OSError:
            raise gone from None
        meta = _read_meta(self.root / f"{token}.json") or {}
        at = _stamp(meta.get("at"))
        at = st.st_mtime if at is None else at
        if time.time() - at > UPLOAD_TTL:
            raise gone
        size = meta.get("bytes")
        return {"filename": _upload_name(meta.get("filename")),
                "bytes": size if isinstance(size, int) and not isinstance(size, bool) else st.st_size, "at": at}

    def claim_path(self, token: str) -> Path:
        """A fresh name for the claim of `token` (nothing is renamed): the caller knows it before `claim` runs. In
        use until `release`."""
        token = self._checked(token)
        return self.root / _hold(f"{token}.claimed-{os.getpid()}-{RUN}-{secrets.token_hex(4)}.zip")

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
