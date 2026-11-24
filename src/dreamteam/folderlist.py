"""Plain helpers of the web UI's folder check and folder browser (docs/SPEC.md §17.2 "Choosing a folder from the web
UI"): the text rules of a path (network paths, inside a root by text), the drive letters, the scan of one folder,
the sort, the peek and the marks' budget. Which folder can be chosen is decided by the session manager's rules
(sessions.py, the one module that imports this one); nothing here knows a session."""

from __future__ import annotations

import os
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Any

from .workspace import is_link

FOLDER_SCAN_MAX = 10_000      # entries of a folder read at most (files too): a folder of a million files never stalls
FOLDER_LIST_MAX = 500         # folders shown at most, after the sort
FOLDER_MARKS_BUDGET = 5.0     # seconds of a listing spent on the entries' marks; the rest get none ("partial")
FOLDER_RECENT_MAX = 8         # `recent` roots: the parents of the folders sessions work in
LINK_HOPS_MAX = 32            # links read on one path's way (network_target): a longer chain counts as leading out
# macOS: folders of the user's home that pop a privacy (TCC) prompt for the hub's terminal app when they are read
MACOS_PRIVATE = frozenset({"Desktop", "Documents", "Downloads", "Pictures", "Movies", "Music", "Library"})
_HIDDEN = stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM


def network_path(text: str, *, windows: bool | None = None) -> bool:
    """A network or device path by its text alone, before any file system call: on Windows a path whose drive starts
    with two slashes (`\\\\server\\share\\x`, `//server/share`, `\\\\?\\...`, `\\\\.\\...`); never on POSIX, where
    `//x` is an ordinary path. `windows`: decide as on Windows (True) or POSIX (False); None: this computer."""
    if not (os.name == "nt" if windows is None else windows):
        return False
    drive = PureWindowsPath(text).drive
    return drive.startswith("\\\\") or drive.startswith("//")


def _plain(target: str) -> str:
    """A link's target as os.readlink gives it on Windows, without its `\\\\?\\` prefix (a junction's target reads
    `\\\\?\\C:\\x`, a link to a share's may read `\\\\?\\UNC\\server\\share`)."""
    for prefix in ("\\\\?\\", "\\??\\"):
        if target.startswith(prefix):
            rest = target[len(prefix):]
            if rest[:4].upper() == "UNC\\":
                return "\\\\" + rest[4:]
            if len(rest) >= 2 and rest[1] == ":":
                return rest
    return target


def _hop(link: str) -> str | None:
    """Where the link `link` leads (os.readlink: the link itself is read, never followed), absolute; None when
    `link` is no symlink or junction, is missing or cannot be read."""
    try:
        target = _plain(os.readlink(link))
    except (OSError, ValueError):
        return None
    if not PureWindowsPath(target).drive:   # relative to the link's folder (or to its drive: `\x`)
        target = os.path.join(os.path.dirname(link), target)
    return os.path.normpath(target)


def network_target(path: str, *, link: bool = False) -> str | None:
    """Windows: the network or device path (network_path) that following the links on the way of the absolute
    `path` leads to, found by reading each link (os.readlink: one call per part of each path on the way) and never
    following one, so no share is contacted; None when the way stays on this computer, and always elsewhere. A
    chain of more than LINK_HOPS_MAX links gives its last target (shut too). `link`: `path` is a link whose own
    folder was read already: only its target's way is read."""
    if os.name != "nt":
        return None
    hops = 0
    pending: str | None = path
    if link:
        pending, hops = _hop(path), 1
    while pending is not None:
        if network_path(pending):
            return pending
        drive, rest = os.path.splitdrive(os.path.abspath(pending))
        parts = [part for part in rest.replace("/", "\\").split("\\") if part]
        current, pending = drive + "\\", None
        for index, part in enumerate(parts):
            current = os.path.join(current, part)
            target = _hop(current)
            if target is None:
                continue   # not a link (or missing): its own path stands
            hops += 1
            if hops > LINK_HOPS_MAX:
                return target
            pending = os.path.join(target, *parts[index + 1:])
            break
    return None


def logical_drives() -> list[str]:
    """Windows: the drive letters in use ("C:", ...), from GetLogicalDrives' bitmask: no drive is touched, so an empty
    card reader or a sleeping network drive costs nothing until it is opened. [] elsewhere."""
    if os.name != "nt":
        return []
    import ctypes

    try:
        mask = int(ctypes.windll.kernel32.GetLogicalDrives())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return []
    return [f"{chr(ord('A') + i)}:" for i in range(26) if mask >> i & 1]


def text_key(path: str) -> str:
    """`path` normalized by text (`..` folded, the system's letter case), for comparisons without the file system."""
    return os.path.normcase(os.path.abspath(path))


def inside_text(path: str, root: str) -> bool:
    """`path` is `root` or lies inside it, by text alone (os.path.normcase and commonpath, no file system call)."""
    p, r = text_key(path), text_key(root)
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:   # another drive, or a mix of absolute and relative
        return False


def innermost(path: str, roots: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The innermost root (by text) that `path` lies in, else None."""
    inside = [root for root in roots if inside_text(path, root["path"])]
    return max(inside, key=lambda root: len(text_key(root["path"])), default=None)


def sort_key(name: str, hidden: bool) -> tuple[bool, str, str]:
    """Names starting with "." and folders with the hidden or system attribute after the others, each group by the
    name's casefold, then the name."""
    return hidden or name.startswith("."), name.casefold(), name


@dataclass
class Scanned:
    """One folder of a scanned folder: its name, its path (the folder's text joined with the name: never where a link
    leads), whether it is a link (a symlink or a junction: workspace.is_link) and, for a link that leads to a network
    or device path (network_target: never followed), that path."""
    name: str
    path: str
    link: bool
    hidden: bool
    leads: str | None = None


def _hidden(entry: os.DirEntry) -> bool:
    if entry.name.startswith("."):
        return True
    try:
        return bool(getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0) & _HIDDEN)
    except OSError:
        return False


def _link_to_folder(entry: os.DirEntry) -> bool:
    """Windows: the link `entry` itself is a directory link or a junction (its own attributes, never followed)."""
    try:
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_DIRECTORY)


def scan(folder: str, shown: str | None = None) -> tuple[list[Scanned], bool]:
    """The subfolders of `folder` (folders and links to folders; a link to a file and a broken one are left out),
    sorted (sort_key), at most FOLDER_LIST_MAX of the first FOLDER_SCAN_MAX entries read (files included):
    (folders, truncated). Each path is `shown` (default `folder`: the listed folder's text where `folder` is its
    resolved path) joined with the name. A link that leads to a network or device path (network_target) is never
    followed: kept, with `leads`, when the link itself is a directory link. OSError when the folder cannot be read."""
    found: list[Scanned] = []
    read, cut = 0, False
    base = folder if shown is None else shown
    with os.scandir(folder) as entries:
        for entry in entries:
            if read >= FOLDER_SCAN_MAX:
                cut = True
                break
            read += 1
            # (on POSIX a symlink is the one kind of link, and is_symlink reads the entry's type: no call)
            link = entry.is_symlink() or (os.name == "nt" and is_link(entry))
            path = os.path.join(base, entry.name)
            leads = network_target(path, link=True) if link else None   # (a mapped drive's link: read by its letter)
            try:
                if not (_link_to_folder(entry) if leads is not None else entry.is_dir()):   # (is_dir follows a link)
                    continue
            except OSError:
                continue
            found.append(Scanned(entry.name, path, link, _hidden(entry), leads))
    found.sort(key=lambda item: sort_key(item.name, item.hidden))
    if len(found) > FOLDER_LIST_MAX:
        found, cut = found[:FOLDER_LIST_MAX], True
    return found, cut


def peek(folder: str) -> tuple[bool | None, bool]:
    """(the folder holds any entry, its read raised PermissionError): one scandir step; (None, False) when it cannot
    be read for another reason."""
    try:
        with os.scandir(folder) as entries:
            return next(entries, None) is not None, False
    except PermissionError:
        return None, True
    except OSError:
        return None, False


def macos_private(path: str, user_home: str) -> bool:
    """On macOS, one of MACOS_PRIVATE right in the user's home: never peeked (its read would ask for the user's
    permission at every listing)."""
    if sys.platform != "darwin":
        return False
    return os.path.basename(path) in MACOS_PRIVATE and text_key(os.path.dirname(path)) == text_key(user_home)


class Budget:
    """The time a listing may spend on its entries' marks (FOLDER_MARKS_BUDGET, from its start)."""

    def __init__(self, seconds: float = FOLDER_MARKS_BUDGET) -> None:
        self.until = time.monotonic() + seconds

    def left(self) -> bool:
        return time.monotonic() < self.until


__all__ = ["FOLDER_LIST_MAX", "FOLDER_MARKS_BUDGET", "FOLDER_RECENT_MAX", "FOLDER_SCAN_MAX", "LINK_HOPS_MAX",
           "MACOS_PRIVATE", "Budget", "Scanned", "inside_text", "innermost", "logical_drives", "macos_private",
           "network_path", "network_target", "peek", "scan", "sort_key", "text_key"]
