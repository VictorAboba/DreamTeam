"""The folders of the hub's computer that an HTTP API v1 client may name (docs/SPEC.md §21.10): the roots of
`[api] folders`, their checks, a create's `folder` resolved, and the git rules of a folder a session works in in place.

A client names a root by its name and a path inside it, never a path of the computer. In the Docker sandbox a root is
where `dreamteam sandbox` mounts it (roots: config.API_FOLDERS_ENV). Where a root lies is checked at each use
(resolve_root), so an unplugged disk never stops the hub and a hand edit of the settings counts at the next request:
- its path must be absolute on this computer's own system (config.native_absolute), checked before anything resolves
  it (403 `forbidden_folder`);
- it is resolved, following links and junctions; a missing root is 404 `not_found`;
- it is refused (403 `forbidden_folder`, root_refusal) where copyfrom.resolve_source refuses a folder to copy: a drive
  or file-system root, the user's home folder or a folder containing it, the DreamTeam home or one of DreamTeam's own
  folders (inside or containing it), another DreamTeam home (or inside one), on POSIX /proc, /sys and /dev; and on
  POSIX /run and /var/run, the running system's sockets (a socket stays reachable through the sandbox's read-only
  mount), but not a removable disk's folder under /run/media.
A request's `path` is a path a client makes up (copyfrom.check_rel(portable=True): 400 `bad_name`); the root joined
with it is resolved and must lie inside the resolved root, by text and by file identity (treecopy.is_within), so a
link or junction under the root that leads out of it is 403 `forbidden_folder` (resolve_request). Its names below the
root are then taken as their folders list them (as_listed: on a file system that ignores letter case or Unicode form,
`Docs` names the folder listed as `docs`, and on POSIX a resolved path keeps the spelling it was given). The create
checks it again after its own resolution (`within=`), and a copy reads below the root without following any link
(copyfrom.copy_sources, nofollow).

Errors are copyfrom.CopyError with their v1 code (`unknown_folder`, `forbidden_folder`, `not_found`, `bad_name`, or
none for a 400 `bad_request`). The texts made here name a folder as `<root>/<path>` and a root by its name; those of
resolve_source that resolve_request passes on (another DreamTeam home under the root...) name the resolved path, which
ApiV1.redact turns into `<root>/<path>` before an API client sees it (docs/SPEC.md §21.1 "Paths in answers"). Stdlib
and DreamTeam's own modules only, never dreamteam.tui. Everything here blocks (paths are resolved, a root may lie on
a network share; git_dir_refusal runs git and walks the repository's folders): run it in a thread."""

from __future__ import annotations

import dataclasses
import logging
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import API_FOLDERS_ENV, ApiFolder, native_absolute
from .copyfrom import (_SYSTEM_ROOTS, CopyError, Source, _disk_mount, _other_home, _own_folders, check_rel,
                       hub_in_container, printable_line, resolve_source)
from .treecopy import is_within
from .workspace import git_output, is_link, long_path, run_git

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger(__name__)

MODES = ("copy", "in_place")            # a create's folder.mode: copied into the session, or worked in in place
FOLDER_FIELDS = ("root", "path", "mode")   # a create's `folder` object (docs/SPEC.md §21.4)
FORBIDDEN = "forbidden_folder"
NAME_SHOWN = 64                         # characters of an unknown root's name quoted in a text
# the paths of a repository DreamTeam's git writes (`git rev-parse --git-path`): each must resolve inside the folder
# (git_dir_refusal)
GIT_WRITES = ("objects", "refs", "logs", "packed-refs", "index", "HEAD", "COMMIT_EDITMSG")
SHOWN_NAME = 120                        # characters of a name inside a repository quoted in a text
# git_dir_refusal's reason when git fails on the folder's repository: followed by git's own output, which v1's
# `changed_files_error` leaves out (docs/SPEC.md §21.7: its texts are never git's output)
GIT_UNREADABLE = "git cannot read its repository"
# POSIX: the folders of the running system's sockets (the Docker daemon's, containerd's, a user's rootless daemon's in
# /run/user/<uid>), never a root nor inside one (root_refusal): a socket stays reachable through a read-only mount of
# the sandbox. Below RUNTIME_DISKS lie removable disks, not sockets.
RUNTIME_FOLDERS = ("/run", "/var/run", "/private/var/run")
RUNTIME_DISKS = "/run/media"
# what each root path (as written) resolved to the last time resolve_root resolved it in this process (at the hub's
# start: log_refusals; then at each use), for the redaction of error texts (api_v1.root_pairs), which never resolves a
# root itself: an error's answer never waits on a root on a share that does not answer. At most RESOLVED_KEPT paths
_RESOLVED: dict[str, Path] = {}
RESOLVED_KEPT = 256


def roots(config: "Config") -> dict[str, ApiFolder]:
    """The roots of `[api] folders` by name, as this hub uses them (docs/SPEC.md §21.10). In the container of the
    Docker sandbox (copyfrom.hub_in_container), with API_FOLDERS_ENV naming a folder that is absolute there, a root's
    path is `<that folder>/<name>`, where `dreamteam sandbox` mounts it (the settings name the host's folders, maybe
    a Windows path); its `in_place` stays the settings'. Anywhere else the variable changes nothing, so a stray one on
    a hub outside the image never points a root elsewhere."""
    folders = dict(config.api.folders)
    mounted = os.environ.get(API_FOLDERS_ENV, "")
    if not folders or not mounted or not native_absolute(mounted) or not hub_in_container(config):
        return folders
    return {name: dataclasses.replace(folder, path=str(Path(mounted) / name)) for name, folder in folders.items()}


def shown(root: str, path: str = "", sep: str = "/") -> str:
    """A server folder as texts and answers name it: `<root>/<path>`, the root's name alone for the root itself.
    `sep`: the separator below the root (os.sep where a text's absolute path is named so, as the redaction of
    docs/SPEC.md §21.1 "Paths in answers" names it; a request's path is quoted with '/', as the client wrote it)."""
    return f"{root}{sep}{path.replace('/', sep)}" if path else root


def folder_meta(value: Any) -> dict[str, str] | None:
    """A session's `api.folder` ({"root", "path", "mode"}, docs/SPEC.md §21.4) when it is one, else None."""
    if not isinstance(value, dict) or not isinstance(value.get("root"), str) or value.get("mode") not in MODES:
        return None
    path = value.get("path")
    return {"root": value["root"], "path": path if isinstance(path, str) else "", "mode": value["mode"]}


def in_place_folder(value: Any) -> dict[str, str] | None:
    """A session's `api.folder` when the session works in it in place (mode "in_place"), else None."""
    folder = folder_meta(value)
    return folder if folder is not None and folder["mode"] == "in_place" else None


def root_refusal(root_real: Path, *, home: Path, config: "Config") -> str | None:
    """Why the resolved root `root_real` can never be a root of `[api] folders` (the location rules of
    copyfrom.resolve_source, docs/SPEC.md §21.10 "Settings", and on POSIX /run and /var/run, RUNTIME_FOLDERS), as
    words that follow its name ("is ..."), without a path of the computer; None when it can be."""
    if root_real.parent == root_real or (os.name != "nt" and _disk_mount(root_real)):
        return "is the root of a drive or of the file system"
    try:
        user_home: Path | None = Path.home().resolve()
    except (OSError, RuntimeError, ValueError, KeyError):
        user_home = None
    if user_home is not None and (is_within(user_home, root_real)):
        return "is the user's home folder, or contains it"
    try:
        home_real = Path(home).resolve()
    except (OSError, RuntimeError, ValueError):
        home_real = Path(home)
    if is_within(root_real, home_real) or is_within(home_real, root_real):
        return "is the DreamTeam home, or lies inside it or contains it"
    for folder in _own_folders(config):
        if is_within(root_real, folder) or is_within(folder, root_real):
            return ("is one of DreamTeam's own folders (the MCP or skills library, the skill environments, the hub "
                    "tokens), or lies inside one or contains one")
    if _other_home(root_real) is not None:
        return "is a DreamTeam home, or lies inside one (its settings hold API keys)"
    if os.name != "nt" and any(is_within(root_real, Path(system)) for system in _SYSTEM_ROOTS):
        return "lies under /proc, /sys or /dev"
    if (os.name != "nt" and any(is_within(root_real, Path(folder)) for folder in RUNTIME_FOLDERS)
            and not is_within(root_real, Path(RUNTIME_DISKS))):
        return ("lies under /run or /var/run, which hold the running system's sockets (the Docker daemon's among "
                "them)")
    return None


def resolve_root(folder: ApiFolder, *, home: Path, config: "Config") -> Path:
    """The root `folder` resolved and checked (docs/SPEC.md §21.10 "Settings", "Resolution"): 403
    `forbidden_folder` for a path that is not absolute on this computer's own system (checked before anything
    resolves it) or a root root_refusal refuses; 404 `not_found` for a root that is not there (or is no folder)."""
    name = folder.name
    if not native_absolute(folder.path):
        raise CopyError(f"The folder root '{name}' is not an absolute path on the hub's computer ([api.folders.{name}] "
                        "path): it cannot be used here.", 403, code=FORBIDDEN)
    try:
        real = Path(folder.path).resolve(strict=True)
    except FileNotFoundError:
        raise CopyError(f"The folder root '{name}' is not there on the hub's computer (an unplugged disk, or a folder "
                        "moved or deleted?).", 404, code="not_found") from None
    except (OSError, RuntimeError, ValueError) as exc:
        why = getattr(exc, "strerror", None) or type(exc).__name__
        raise CopyError(f"The folder root '{name}' cannot be read on the hub's computer ({why}).", 403,
                        code=FORBIDDEN) from None
    if folder.path not in _RESOLVED and len(_RESOLVED) >= RESOLVED_KEPT:
        _RESOLVED.clear()
    _RESOLVED[folder.path] = real
    if not real.is_dir():
        raise CopyError(f"The folder root '{name}' is not a folder on the hub's computer.", 404, code="not_found")
    reason = root_refusal(real, home=home, config=config)
    if reason is not None:
        raise CopyError(f"The folder root '{name}' {reason}: [api] folders cannot grant it.", 403, code=FORBIDDEN)
    return real


def resolved_root(path: str) -> Path | None:
    """What the root path `path` (as written) resolved to the last time resolve_root resolved it in this process, else
    None. Never touches the file system."""
    return _RESOLVED.get(path)


@dataclass(frozen=True)
class FolderRequest:
    """A create's `folder` resolved (resolve_request): the root's name, the path as the request gave it ('/', "" for
    the root itself), the mode, the resolved root, the resolved folder or .zip, its kind ("folder" or "zip"; a folder
    to work in in place may not exist yet) and its names below the resolved root as the root's folders spell them
    (None when it lies inside the root only by file identity)."""

    root: str
    path: str
    mode: str
    root_real: Path
    target: Path
    kind: str
    parts: tuple[str, ...] | None

    @property
    def shown(self) -> str:
        return shown(self.root, self.path)

    def meta(self) -> dict[str, str]:
        """session.json's `api.folder` (docs/SPEC.md §21.4)."""
        return {"root": self.root, "path": self.path, "mode": self.mode}

    def source(self) -> Source:
        """What copyfrom.copy_sources copies for a `copy`: read below the root by its names, never by a path."""
        if self.mode != "copy" or self.parts is None:
            raise ValueError("only a folder to copy, named below its root, is a source")
        return Source(self.kind, self.target, self.shown, root=self.root_real, parts=self.parts)


def resolve_request(root: Any, path: Any = None, mode: Any = "copy", *, config: "Config",
                    home: Path) -> FolderRequest:
    """A create's `folder` (docs/SPEC.md §21.10 "Request", "Resolution"): 400 `unknown_folder` (the root names in the
    text) for a root `[api] folders` does not list; 403 `forbidden_folder` for `in_place` under a root without
    `in_place = true`; 400 `bad_name` for a path a client may not make (check_rel portable); the root resolved
    (resolve_root); the root joined with the path resolved, which must lie inside the resolved root by text and by
    file identity (403 `forbidden_folder`: a link or junction on the way leads out), its names below the root then
    taken as their folders list them (as_listed: the folder an in-place session stores, the names a copy reads). For
    `copy`: a path that names nothing is 404 `not_found`, a file that is not a .zip 400; and resolve_source's rules
    again, after its own resolution, given the root (`within`). For `in_place`: a file is 400 (a missing folder is
    fine: it is made)."""
    known = roots(config)
    if not isinstance(root, str) or root not in known:
        names = ", ".join(sorted(known)) or "none"
        given = printable_line(root if isinstance(root, str) else repr(root), NAME_SHOWN)
        raise CopyError(f"No folder root '{given}' in [api] folders. Roots: {names} (GET /api/v1/options lists them).",
                        400, code="unknown_folder")
    if mode not in MODES:
        raise CopyError(f"'mode' must be {' or '.join(MODES)}.")
    folder = known[root]
    if mode == "in_place" and not folder.in_place:
        raise CopyError(f"The folder root '{root}' takes copies only ([api.folders.{root}] in_place is false): a "
                        "session cannot work in it in place.", 403, code=FORBIDDEN)
    parts = check_rel(path, portable=True)
    rel = "/".join(parts)
    where = shown(root, rel)
    root_real = resolve_root(folder, home=home, config=config)
    try:
        target = root_real.joinpath(*parts).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        why = getattr(exc, "strerror", None) or type(exc).__name__
        raise CopyError(f"Cannot use {where} ({why}).", 403, code=FORBIDDEN) from None
    if not is_within(target, root_real):
        raise CopyError(f"{where} leads out of its root: a link or a junction on the way points elsewhere.", 403,
                        code=FORBIDDEN)
    try:
        below: tuple[str, ...] | None = target.relative_to(root_real).parts
    except ValueError:
        below = None
    if below:   # (another letter case or Unicode form of a name that is there: its folder's own spelling)
        below = as_listed(root_real, below)
        target = root_real.joinpath(*below)
    if mode == "in_place":
        if os.path.lexists(target) and not target.is_dir():
            raise CopyError(f"{where} is a file, not a folder: a session works in a folder.")
        return FolderRequest(root, rel, mode, root_real, target, "folder", below)
    if not os.path.lexists(target):
        raise CopyError(f"Nothing to copy at {where}: no such folder or file.", 404, code="not_found")
    if target.is_dir():
        kind = "folder"
    elif target.is_file():
        if target.suffix.casefold() != ".zip":
            raise CopyError(f"{where} is a file but not a .zip archive: name a folder or a .zip file.")
        kind = "zip"
    else:
        raise CopyError(f"{where} is neither a folder nor a .zip file.")
    if below is None:   # (inside by file identity only: a copy reads below the root by names, which it has not)
        raise CopyError(f"{where} leads out of its root: a link or a junction on the way points elsewhere.", 403,
                        code=FORBIDDEN)
    try:   # the location rules of copy_from for the folder itself (another DreamTeam home under the root...)
        resolve_source(str(target), home=home, config=config, sandbox=False, within=root_real)
    except CopyError as exc:
        if exc.code is not None:
            raise
        raise CopyError(str(exc), 403, code=FORBIDDEN) from None
    return FolderRequest(root, rel, mode, root_real, target, kind, below)


def as_listed(root: Path, parts: tuple[str, ...]) -> tuple[str, ...]:
    """`parts`, names below the folder `root`, each spelled as its folder lists it (docs/SPEC.md §21.8, §21.10). A file
    system that ignores letter case or Unicode form (APFS, NTFS seen from WSL or a Docker Desktop bind mount) opens
    `Docs` for the folder it lists as `docs`, and on POSIX a resolved path keeps the spelling it was given, while
    nofollow takes a name only as its folder lists it. A name the listing does not hold exactly is replaced by the
    listed name of the same entry (lstat: device and inode; with no file ids, the one listed name that differs in
    letter case or Unicode form only). From the first name that is not there (a folder an in-place create makes, or
    one gone meanwhile) on, the names stay as given. Blocking: a listing of each folder on the way."""
    out: list[str] = []
    folder = root
    for i, name in enumerate(parts):
        listed = _listed_name(folder, name)
        if listed is None:
            return (*out, *parts[i:])
        out.append(listed)
        folder = folder / listed
    return tuple(out)


def _name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name.casefold())


def _listed_name(folder: Path, name: str) -> str | None:
    """The name of `folder`'s listing that `name` opens (as_listed); None when `name` names nothing there."""
    try:
        names = os.listdir(long_path(folder))
    except OSError:
        return None
    if name in names:
        return name
    try:
        given = os.lstat(long_path(folder / name))
    except OSError:
        return None
    key = _name_key(name)
    alike = [other for other in names if _name_key(other) == key]
    if not given.st_ino:   # (a file system without file ids)
        return alike[0] if len(alike) == 1 else None
    for other in alike + [other for other in names if _name_key(other) != key]:
        try:
            st = os.lstat(long_path(folder / other))
        except OSError:
            continue
        if (st.st_dev, st.st_ino) == (given.st_dev, given.st_ino):
            return other
    return None


def git_dir_refusal(folder: Path, git: str | None = None, *, offline: bool = False) -> str | None:
    """Why DreamTeam's own git (the autocommits, `changed_files`) would write outside the folder `folder` that a
    session works in in place (docs/SPEC.md §21.10 `in_place`, decision D3), as words for a text without a path of
    the computer; None when it would not. Refused: a `.git` that is a link or a file (a gitfile `gitdir: ...`, such
    as a linked worktree's); a link or junction anywhere in a `.git` folder, or a file there outside `objects/` with
    another hard link (_git_tree_refusal, before git reads it); a repository whose git dir or common dir (`git
    rev-parse --absolute-git-dir --git-common-dir`) does not resolve inside the folder (a `.git/commondir` pointing
    elsewhere, or a folder with no `.git` of its own inside another repository), or one of whose paths git writes
    (GIT_WRITES, `--git-path`) does not; and the same walk of a git dir or common dir that lies elsewhere in the
    folder. A folder in no repository is fine (one is made in it). `offline`: run_git's. Blocking (git, a walk)."""
    dot = Path(folder) / ".git"
    try:
        st: os.stat_result | None = os.lstat(long_path(dot))
    except FileNotFoundError:
        st = None
    except OSError as exc:
        return f"its .git cannot be read ({exc.strerror or exc})"
    if st is not None:
        if is_link(dot):
            return "its .git is a link: DreamTeam's git would write where it leads"
        if not stat.S_ISDIR(st.st_mode):
            return ("its .git is a file (a gitfile 'gitdir: ...', such as a linked worktree's), not a folder: "
                    "DreamTeam's git would write into the repository it names")
        problem = _git_tree_refusal(dot, ".git")   # (before git reads anything there)
        if problem is not None:
            return problem
    asked = [arg for name in GIT_WRITES for arg in ("--git-path", name)]
    try:
        proc = run_git(Path(folder), "rev-parse", "--absolute-git-dir", "--git-common-dir", *asked, git=git,
                       safe_directory="*", adopted=True, offline=offline)
    except OSError as exc:
        return f"git cannot be run ({exc.strerror or exc})"
    if proc.returncode != 0:
        return None if st is None else f"{GIT_UNREADABLE}: {git_output(proc)[:300]}"
    lines = [line.strip() for line in proc.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    if len(lines) < 2 + len(GIT_WRITES):
        return "git does not name its repository's folders"
    try:
        real = Path(folder).resolve()
        git_dir = Path(lines[0]).resolve()
        common, *written = ((path if path.is_absolute() else Path(folder) / path).resolve()
                            for path in map(Path, lines[1:2 + len(GIT_WRITES)]))
    except (OSError, RuntimeError, ValueError) as exc:
        return f"its repository's folders cannot be resolved ({getattr(exc, 'strerror', None) or exc})"
    if st is None and not is_within(git_dir, real):
        return "it lies inside another repository, whose database is outside it"
    for what, path in (("git dir", git_dir), ("common dir", common), *zip(GIT_WRITES, written)):
        if not is_within(path, real):
            return f"its repository's {what} lies outside the folder: DreamTeam's git would write there"
    walked = [dot.resolve()] if st is not None else []
    for path in (git_dir, common):   # (elsewhere in the folder: `.git/commondir` naming a folder of its own)
        if not any(is_within(path, done) for done in walked):
            try:
                below = path.relative_to(real).as_posix()
            except ValueError:
                below = path.name
            problem = _git_tree_refusal(path, below)
            if problem is not None:
                return problem
            walked.append(path)
    return None


def _git_tree_refusal(top: Path, shown_top: str) -> str | None:
    """Why the repository folder `top` (a `.git` folder, a git dir or a common dir inside the folder; `shown_top`
    names it in a text, relative to the folder) would make DreamTeam's git write outside the folder (docs/SPEC.md
    §21.10 `in_place`): a link or junction anywhere in it, which git follows (`objects` or `logs/refs/heads` pointing
    elsewhere); a regular file outside `objects/` with another hard link (st_nlink > 1, by lstat: DirEntry.stat() says
    0 on Windows), which git changes in place (a reflog is appended to, COMMIT_EDITMSG rewritten), so its other name,
    maybe outside, would change too; a folder that cannot be read. `hooks/` is not looked at (DreamTeam's git runs no
    hook: hubgit.fixed_args), nor the link count of objects (immutable, never written in place; a local clone shares
    them by hard links). None when nothing there leads out. Blocking."""
    stack: list[tuple[Path, str]] = [(top, "")]
    while stack:
        folder, rel = stack.pop()
        try:
            with os.scandir(long_path(folder)) as listing:
                entries = list(listing)
        except OSError as exc:
            where = f"{shown_top}/{rel}" if rel else shown_top
            return f"its repository's folder {printable_line(where, SHOWN_NAME)} cannot be read ({exc.strerror or exc})"
        for entry in entries:
            name = f"{rel}/{entry.name}" if rel else entry.name
            if not rel and entry.name == "hooks":
                continue
            where = printable_line(f"{shown_top}/{name}", SHOWN_NAME)
            if is_link(entry):
                return (f"its repository holds a link or a junction ({where}): DreamTeam's git would write where it "
                        "leads")
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_dir = False
            if is_dir:
                stack.append((Path(entry.path), name))
                continue
            if name.split("/", 1)[0] == "objects":
                continue
            try:
                info = os.lstat(long_path(entry.path))
            except FileNotFoundError:
                continue   # (gone meanwhile: git's own lock files come and go)
            except OSError as exc:
                return f"its repository's file {where} cannot be read ({exc.strerror or exc})"
            if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
                return (f"its repository's file {where} has another name (a hard link): DreamTeam's git would change "
                        "that file too")
    return None


def log_refusals(config: "Config", home: Path) -> None:
    """Log the roots of `[api] folders` that cannot be used now (docs/SPEC.md §21.10: refused at each use, and logged
    at the hub's start): a refused one as a warning, a missing one (an unplugged disk) as information. Blocking."""
    for name, folder in sorted(roots(config).items()):
        try:
            resolve_root(folder, home=home, config=config)
        except CopyError as exc:
            level = logging.INFO if exc.code == "not_found" else logging.WARNING
            log.log(level, "[api.folders.%s] path = %s: %s", name, folder.path, exc)


__all__ = ["FOLDER_FIELDS", "GIT_UNREADABLE", "GIT_WRITES", "MODES", "ApiFolder", "FolderRequest", "as_listed",
           "folder_meta", "git_dir_refusal", "in_place_folder", "log_refusals", "resolve_request", "resolve_root",
           "resolved_root", "root_refusal", "roots", "shown"]
