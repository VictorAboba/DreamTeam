"""The shared MCP library on disk, usable without a Swarm (export / import, later the hub's REST).

The library (`config.library_dir`, `~/.dreamteam/library` unless DREAMTEAM_MCP_LIBRARY_DIR or `[mcp] library_dir`
says otherwise) holds one folder per server, in the format `mcp_forge` writes and reads:

    <root>/<name>/server.py    the snapshot's code (at most mcp_forge.MAX_CODE_BYTES bytes)
    <root>/<name>/meta.json    {"name", "description", "owner", "requirements", "tools", "created_at",
                                "updated_at", "project", "swarm", "digest", "approved_digest", ...}

This module lists and reads entries (`list_entries`, `scan`, `read_entry`), adds an entry from a folder in that same
format (`add_entry`: staged in `.t-<token>-<time>` and renamed in, so a reader sees the old entry or the new one,
never half of one) and removes one (`remove_entry`). Links (symlinks, Windows junctions) are never followed: a linked
entry folder or a linked server.py / meta.json is not an entry, and removing a linked entry removes only the link.
The temp folder names are those of the skills library (`skills.clean_stale` removes what a crashed write left).

Everything here is synchronous file work; async callers run it in `asyncio.to_thread`.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

from .mcp_forge import MAX_CODE_BYTES, NAME_RE, MCPForge, snapshot_digest
from .models import ToolError
from .skills import _remove, _rename, _temp_name, clean_stale
from .workspace import is_link, long_path

SERVER_FILE = "server.py"
META_FILE = "meta.json"
ENTRY_FILES = (SERVER_FILE, META_FILE)
MAX_META_BYTES = 4_000_000   # meta.json (its tool list included) of one entry

AddStatus = Literal["added", "rewritten", "unchanged"]


class LibraryError(ValueError):
    """A library operation that cannot be done; `status` is the matching HTTP status, `extra` goes into a JSON
    answer (`exists`: the names that clash), as skills.SkillError does."""

    def __init__(self, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.status = status
        self.extra = extra


@dataclass
class LibraryEntry:
    """One library entry as read from disk. `digest` is computed from the files (mcp_forge.snapshot_digest of
    server.py and the requirements); `stored_digest` and `approved_digest` are what meta.json records."""

    name: str
    path: Path
    meta: dict
    code: bytes
    requirements: list[str]
    digest: str
    stored_digest: str | None
    approved_digest: str | None
    tools: list[dict] = field(default_factory=list)

    @property
    def description(self) -> str:
        return str(self.meta.get("description") or "")

    @property
    def owner(self) -> str | None:
        owner = self.meta.get("owner")
        return str(owner) if owner else None

    @property
    def project(self) -> str | None:
        project = self.meta.get("project")
        return project if isinstance(project, str) and project else None

    @property
    def approved(self) -> bool:
        """The user approved this very code (mcp_forge imports it without asking again)."""
        return self.approved_digest == self.digest

    @property
    def changed_outside(self) -> bool:
        """server.py or the requirements are not what DreamTeam recorded: edited by hand after it was saved, or a
        folder dropped in by hand (no digest recorded at all). Content-based, so it holds on another computer."""
        return self.stored_digest != self.digest

    @property
    def requirements_text(self) -> str:
        """The requirements one per line (empty when there are none)."""
        return "".join(f"{req}\n" for req in self.requirements)


@dataclass
class Scan:
    """What `scan` found: the readable entries (by name) and the folders it passed over, with the reason."""

    entries: list[LibraryEntry] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)


# ---- reading ---------------------------------------------------------------------------------------------------
def _read_regular(path: Path, limit: int) -> bytes:
    """The bytes of the regular file `path` (not a link, at most `limit` bytes). Raises LibraryError otherwise. The
    opened file must be the one `lstat` saw, so a link swapped in after the check is not read."""
    target = long_path(path)
    try:
        before = os.lstat(target)
    except FileNotFoundError:
        raise LibraryError(f"{path.name} is missing.", 404) from None
    except OSError as exc:
        raise LibraryError(f"{path.name} cannot be read: {exc.strerror or exc}.", 409) from None
    if is_link(path) or not stat.S_ISREG(before.st_mode):
        raise LibraryError(f"{path.name} is not a regular file (a link or a folder).")
    if before.st_size > limit:
        raise LibraryError(f"{path.name} is too long ({before.st_size} bytes, max {limit}).", 413)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(target, flags)
    except OSError as exc:
        raise LibraryError(f"{path.name} cannot be read: {exc.strerror or exc}.", 409) from None
    with os.fdopen(fd, "rb") as f:
        now = os.fstat(f.fileno())
        if not stat.S_ISREG(now.st_mode) or (before.st_ino and now.st_ino and (before.st_ino, before.st_dev)
                                             != (now.st_ino, now.st_dev)):
            raise LibraryError(f"{path.name} changed while it was read; try again.", 409)
        data = f.read(limit + 1)
    if len(data) > limit:
        raise LibraryError(f"{path.name} is too long (more than {limit} bytes).", 413)
    return data


def _requirements(meta: dict) -> list[str]:
    """The requirements as mcp_forge reads them from meta.json (the digest covers exactly this list)."""
    reqs = meta.get("requirements") or []
    return [str(r) for r in reqs] if isinstance(reqs, list) else []


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def read_folder(folder: Path, name: str | None = None) -> LibraryEntry:
    """Read an entry folder (one in the library, or a copy of one elsewhere, e.g. an export) without following
    links. `name` defaults to the folder's name and must match mcp_forge.NAME_RE. Raises LibraryError."""
    folder = Path(folder)
    name = folder.name if name is None else name
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise LibraryError(f"Invalid MCP server name '{str(name)[:60]}': use 2-40 characters - lowercase latin "
                           "letters, digits and '_', starting with a letter.")
    if is_link(folder):
        raise LibraryError(f"The folder of MCP server '{name}' is a link.")
    if not os.path.isdir(long_path(folder)):
        raise LibraryError(f"No MCP server '{name}' in {folder.parent}.", 404)
    try:
        code = _read_regular(folder / SERVER_FILE, MAX_CODE_BYTES)
        raw = _read_regular(folder / META_FILE, MAX_META_BYTES)
    except LibraryError as exc:
        raise LibraryError(f"MCP server '{name}': {exc}", exc.status) from None
    try:
        meta = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, RecursionError):
        raise LibraryError(f"MCP server '{name}': {META_FILE} is not valid JSON.") from None
    if not isinstance(meta, dict):
        raise LibraryError(f"MCP server '{name}': {META_FILE} is not a JSON object.")
    requirements = _requirements(meta)
    tools = meta.get("tools") or []
    return LibraryEntry(
        name=name, path=folder, meta=meta, code=code, requirements=requirements,
        digest=snapshot_digest(code, requirements), stored_digest=_str_or_none(meta.get("digest")),
        approved_digest=_str_or_none(meta.get("approved_digest")),
        tools=[t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else [],
    )


def read_entry(root: Path, name: str) -> LibraryEntry | None:
    """The entry `name` of the library `root`, or None when there is none that can be read (a bad name, a link,
    no server.py, a server.py longer than mcp_forge.MAX_CODE_BYTES, a broken meta.json)."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        return None
    try:
        return read_folder(Path(root) / name, name)
    except LibraryError:
        return None


def scan(root: Path) -> Scan:
    """Every entry of the library `root` (sorted by name) and the folders passed over, each with its reason. Names
    that cannot be entries (temp folders `.t-*` / `.o-*`, files) are not reported; a missing root is empty."""
    found = Scan()
    try:
        with os.scandir(long_path(root)) as it:
            names = sorted(e.name for e in it)
    except OSError:
        return found
    for name in names:
        path = Path(root) / name
        if name.startswith("."):
            continue
        if is_link(path):
            if NAME_RE.match(name):
                found.skipped.append((name, "it is a link"))
            continue
        if not os.path.isdir(long_path(path)):
            continue
        if not NAME_RE.match(name):
            found.skipped.append((name, "the folder name is not a valid MCP server name"))
            continue
        try:
            found.entries.append(read_folder(path, name))
        except LibraryError as exc:
            found.skipped.append((name, str(exc).removeprefix(f"MCP server '{name}': ")))
    return found


def list_entries(root: Path) -> list[LibraryEntry]:
    """Every readable entry of the library `root`, sorted by name (see `scan` for the folders passed over)."""
    return scan(root).entries


# ---- writing ---------------------------------------------------------------------------------------------------
def check_requirements(requirements: Any) -> list[str]:
    """`requirements` as a library entry may hold them: a list of pip requirement strings that mcp_forge accepts
    as they are (MCPForge._check_requirements leaves them unchanged). Raises LibraryError."""
    if not isinstance(requirements, list) or not all(isinstance(r, str) for r in requirements):
        raise LibraryError("requirements must be a list of pip requirement strings.")
    try:
        checked = MCPForge._check_requirements(requirements)
    except ToolError as exc:
        raise LibraryError(str(exc)) from None
    if checked != requirements:
        raise LibraryError("requirements must not hold empty, padded or repeated items.")
    return checked


def write_entry_folder(folder: Path, code: bytes, meta: dict) -> None:
    """Write server.py and meta.json into the new folder `folder` (its parent must exist), as mcp_forge writes
    them. The files are created, never opened through an existing link."""
    os.mkdir(long_path(folder))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    text = json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8")
    for filename, data in ((SERVER_FILE, code), (META_FILE, text)):
        fd = os.open(long_path(folder / filename), flags, 0o644)
        with os.fdopen(fd, "wb") as f:
            f.write(data)


def add_entry(root: Path, src: Path, *, name: str | None = None, replace: bool = False,
              meta_updates: dict | None = None, meta_drop: Iterable[str] = ()) -> AddStatus:
    """Add the entry folder `src` (server.py + meta.json, the library's own format) to the library `root` as
    `name` (default: the folder's name). `meta_updates` are merged into its meta.json first (e.g. import's new
    `project`), and the keys `meta_drop` names are left out of it (e.g. an export's `project_session`). Returns:
    - "unchanged": the library already has this server.py and this meta.json (nothing written);
    - "added": there was no entry `name`;
    - "rewritten": another entry `name` (or a link there, removed itself, never followed) was replaced; without
      `replace` that is a LibraryError (409, exists=[name]).
    An approval the library records for this very code (approved_digest = the code's digest) is kept even when
    the new meta.json has none. The new entry is staged in a temp folder of `root` and renamed in; the old one is
    renamed away first and put back when that fails. Raises LibraryError (bad name, links, oversized or broken
    files, invalid requirements) or OSError."""
    new = read_folder(Path(src), name)
    name = new.name
    drop = set(meta_drop)
    meta = {k: v for k, v in {**new.meta, **(meta_updates or {})}.items() if k not in drop}
    try:
        digest = snapshot_digest(new.code, check_requirements(meta.get("requirements") or []))
    except LibraryError as exc:
        raise LibraryError(f"MCP server '{name}': {exc}") from None
    root = Path(root)
    os.makedirs(long_path(root), exist_ok=True)
    clean_stale(root)
    final = root / name
    exists = os.path.lexists(long_path(final))
    old = None if is_link(final) else read_entry(root, name)
    if old is not None and old.approved and old.digest == digest and meta.get("approved_digest") != digest:
        meta["approved_digest"] = digest
    if old is not None and old.code == new.code and old.meta == meta:
        return "unchanged"
    if exists and not replace:
        raise LibraryError(f"The MCP library already has a different server '{name}'.", 409, exists=[name])
    tmp = root / _temp_name("t")
    try:
        write_entry_folder(tmp, new.code, meta)
        trash = None
        if os.path.lexists(long_path(final)):
            trash = root / _temp_name("o")
            _rename(final, trash)
        try:
            _rename(tmp, final)
        except BaseException:
            if trash is not None:
                with contextlib.suppress(OSError):
                    _rename(trash, final)
            raise
    except BaseException:
        with contextlib.suppress(OSError):
            _remove(tmp)
        raise
    if trash is not None:
        with contextlib.suppress(OSError):
            _remove(trash)
    return "rewritten" if exists else "added"


def remove_entry(root: Path, name: str) -> bool:
    """Remove the entry `name` from the library `root`: renamed away first (so it leaves listings at once and a
    folder with a file in use stays whole), then deleted. A link there is removed itself, never what it points to.
    Returns False when there is no entry `name`. Raises LibraryError for a bad name or a folder in use."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise LibraryError(f"Invalid MCP server name '{str(name)[:60]}'.")
    folder = Path(root) / name
    if not os.path.lexists(long_path(folder)):
        return False
    trash = Path(root) / _temp_name("o")
    try:
        _rename(folder, trash)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise LibraryError(f"Cannot remove MCP server '{name}': {exc.strerror or exc} (a file in use?)", 409) from None
    _remove(trash)
    return True


__all__ = [
    "ENTRY_FILES", "LibraryEntry", "LibraryError", "META_FILE", "SERVER_FILE", "Scan", "add_entry",
    "check_requirements", "list_entries", "read_entry", "read_folder", "remove_entry", "scan", "write_entry_folder",
]
