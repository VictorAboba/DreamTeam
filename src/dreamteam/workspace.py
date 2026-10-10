"""Shared workspace: files visible to every member, advisory locks and git autocommits."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
from pathlib import Path, PurePath
from typing import Any

from .models import USER, ToolError

log = logging.getLogger(__name__)

DEFAULT_GITIGNORE = "node_modules/\n.venv/\n__pycache__/\n*.pyc\n.DS_Store\n"
BINARY_PROBE_BYTES = 8192
LIST_MAX_ENTRIES = 500
TREE_MAX_ENTRIES = 5000
MAX_LOCK_MINUTES = 24 * 60
GIT_TIMEOUT = 120.0
# Directories shown but never expanded in listings (dependency caches, virtualenvs).
NO_EXPAND = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
                       ".pytest_cache", ".ruff_cache", ".tox"})
_WINDOWS = sys.platform == "win32"
# Names Windows (and git for Windows) cannot use for a file or directory, with or without extension.
_WIN_RESERVED = re.compile(r"(con|prn|aux|nul|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³]) *(\..*)?",
                           re.IGNORECASE)
_WIN_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
# A reparse point with this bit set redirects elsewhere (symlink, junction); others (OneDrive
# placeholders, dedup, ...) are ordinary files and directories.
_REPARSE_TAG_NAME_SURROGATE = 0x20000000

_TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
     "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya"],
))


def email_slug(name: str) -> str:
    """ASCII-only local part for a commit e-mail ("Анна Смирнова" -> "anna.smirnova")."""
    text = "".join(_TRANSLIT.get(ch, ch) for ch in (name or "").lower())
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", ".", text).strip(".")
    return slug or "agent"


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _split_lines(text: str) -> list[str]:
    """Split into lines the way editors number them (a trailing newline does not add a line)."""
    if not text:
        return []
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def _norm(rel: str) -> str:
    return rel.casefold() if _WINDOWS else rel


def _covers(lock_path: str, rel: str) -> bool:
    """True if a lock on `lock_path` (file or directory) protects `rel`."""
    lock_path, rel = _norm(lock_path), _norm(rel)
    return rel == lock_path or rel.startswith(lock_path + "/")


def _encode(text: str, what: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ToolError(f"{what} contains invalid Unicode (an unpaired surrogate at character {exc.start}); "
                        "remove or replace that character and retry.") from None


def _file_eol(text: str) -> str | None:
    """The file's line ending if it is consistent ("\\n" or "\\r\\n"); None if mixed or no newlines."""
    lf = text.count("\n")
    if not lf:
        return None
    crlf = text.count("\r\n")
    if crlf == lf:
        return "\r\n"
    return "\n" if crlf == 0 else None


def _check_windows_names(parts: tuple[str, ...], shown: str) -> None:
    for part in parts:
        if _WIN_BAD_CHARS.search(part):
            raise ToolError(f"Invalid path '{shown}': file names cannot contain < > : \" | ? * "
                            "or control characters on Windows.")
        if part != part.rstrip(". "):
            raise ToolError(f"Invalid path '{shown}': '{part}' ends with a dot or a space, which Windows "
                            "drops from file names. Remove the trailing dot/space.")
        if _WIN_RESERVED.fullmatch(part):
            raise ToolError(f"Invalid path '{shown}': '{part}' is a reserved device name on Windows "
                            "(CON, PRN, AUX, NUL, COM1-9, LPT1-9, also with an extension). Choose another name.")


class Workspace:
    def __init__(self, swarm: Any):
        self.swarm = swarm
        self.root: Path = Path(swarm.config.workspace_dir)
        self._git_exe: str | None = shutil.which("git")
        self._git_lock = asyncio.Lock()
        self._io_lock = asyncio.Lock()  # read-modify-write of files by concurrent agents

    # --- setup -----------------------------------------------------------------------------
    @property
    def git_enabled(self) -> bool:
        return bool(self.swarm.config.workspace.git and self._git_exe and (self.root / ".git").exists())

    async def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.swarm.config.workspace.git:
            return
        if not self._git_exe:
            log.warning("git not found: the workspace will not be versioned")
            return
        async with self._git_lock:
            try:
                if not (self.root / ".git").exists():
                    await self._init_repo()
                await self._configure_repo()
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("git init failed: %s", exc)

    async def _configure_repo(self) -> None:
        """Repo-local settings so agents can also run git themselves from the shell tool."""
        if not (self.root / ".git").exists():
            return
        for key, value in (("core.autocrlf", "false"), ("user.name", "DreamTeam"),
                           ("user.email", "swarm@dreamteam.local"), ("commit.gpgsign", "false")):
            await self._git(None, "config", "--local", key, value)

    async def _init_repo(self) -> None:
        proc = await self._git(None, "-c", "init.defaultBranch=main", "init", "-q")
        if proc.returncode != 0:
            log.warning("git init failed: %s", self._output(proc))
            return
        gitignore = self.root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_bytes(DEFAULT_GITIGNORE.encode("utf-8"))
        await self._git(None, "add", "-A", "--ignore-errors")
        proc = await self._git(None, "commit", "-q", "--no-verify", "--allow-empty", "-m", "Initialize workspace")
        if proc.returncode != 0:
            log.warning("initial commit failed: %s", self._output(proc))

    # --- paths -----------------------------------------------------------------------------
    def _real_root(self) -> Path:
        return self.root.resolve()

    def resolve(self, path: str) -> Path:
        """Map a caller path (relative to the root, `/` or `\\`) to a real path inside the workspace."""
        raw = str(path if path is not None else "").strip().replace("\\", "/")
        shown = raw or "."
        outside = ToolError(f"Path '{shown}' is outside the workspace. "
                            "Use a path relative to the workspace root, e.g. 'docs/notes.md'.")
        try:
            root = self._real_root()
            given = Path(raw or ".")
            if given.anchor:
                # Never touch a network path (\\host\share, \\?\..., \\.\...) that is not the workspace:
                # resolving it would contact the host (NTLM credential leak) and can stall for seconds.
                if _WINDOWS and given.drive.startswith("\\\\") and not self._lexically_inside(given, root):
                    raise outside
                candidate = given
            else:
                candidate = root / given
            real = candidate.resolve()
        except (OSError, ValueError, RuntimeError) as exc:
            raise ToolError(f"Invalid path '{shown}': {exc}. Use a path relative to the workspace root.") from exc
        if real != root and not real.is_relative_to(root):
            raise outside
        parts = real.relative_to(root).parts
        if any(part.casefold() == ".git" for part in parts):
            raise ToolError(f"Path '{shown}' is inside .git, which is managed automatically. "
                            "Git history is committed for you; choose another path.")
        if _WINDOWS:
            _check_windows_names(parts, shown)
        return real

    def _lexically_inside(self, given: Path, real_root: Path) -> bool:
        norm = PurePath(os.path.normpath(given))
        return any(norm == base or norm.is_relative_to(base) for base in (real_root, self.root))

    def rel(self, p: Path) -> str:
        """Workspace-relative posix path ("." for the root)."""
        for base, target in ((self.root, Path(p)), (self._real_root(), Path(p).resolve())):
            try:
                rel = target.relative_to(base).as_posix()
            except ValueError:
                continue
            return rel or "."
        return Path(p).as_posix()

    def mtime(self, path: str) -> float | None:
        try:
            return self.resolve(path).stat().st_mtime
        except (ToolError, OSError, ValueError):
            return None

    def exists(self, path: str) -> bool:
        try:
            return self.resolve(path).exists()
        except (ToolError, OSError, ValueError):
            return False

    # --- reading -----------------------------------------------------------------------------
    def _existing_file(self, path: str) -> tuple[Path, str]:
        p = self.resolve(path)
        rel = self.rel(p)
        if not p.exists():
            raise ToolError(f"File not found: '{rel}'. Use list_files to see what exists.")
        if p.is_dir():
            raise ToolError(f"'{rel}' is a directory. Use list_files to see its contents.")
        return p, rel

    def _read_bytes(self, p: Path, rel: str) -> bytes:
        try:
            return p.read_bytes()
        except OSError as exc:
            raise ToolError(f"Cannot read '{rel}': {exc.strerror or exc}.") from exc

    def read(self, path: str, offset: int = 1, limit: int = 400) -> str:
        p, rel = self._existing_file(path)
        data = self._read_bytes(p, rel)
        if b"\x00" in data[:BINARY_PROBE_BYTES]:
            raise ToolError(f"'{rel}' is a binary file ({format_size(len(data))}); it cannot be read as text.")
        lines = _split_lines(data.decode("utf-8", errors="replace").removeprefix("\ufeff"))
        total = len(lines)
        if total == 0:
            return f"[{rel}: empty file, 0 lines]"
        offset = max(1, int(offset or 1))
        limit = max(1, int(limit or 400))
        if offset > total:
            raise ToolError(f"offset {offset} is past the end of '{rel}' ({total} lines). "
                            f"Use an offset between 1 and {total}.")
        end = min(total, offset + limit - 1)
        max_chars = self._max_chars()
        # reserve room for the longest possible header and note so the result stays within max_chars
        reserve = len(self._header(rel, total, total, total)) + len(self._note(max_chars, total, total - 1, "line")) + 2
        body, end, cut = self._fit(lines, offset, end, max(80, max_chars - reserve))
        parts = [self._header(rel, offset, end, total), body]
        if cut or end < total:
            parts.append(self._note(max_chars, total, end, cut))
        return "\n".join(parts)

    @staticmethod
    def _header(rel: str, first: int, last: int, total: int) -> str:
        return f"[{rel}: lines {first}-{last} of {total}]"

    @staticmethod
    def _note(max_chars: int, total: int, end: int, cut: str) -> str:
        more = f"continue with offset={end + 1}" if end < total else "end of file"
        if not cut:
            return f"[{total - end} more lines; {more}]"
        line_cut = f"; line {end} was cut" if cut == "line" else ""
        return f"[output capped at {max_chars} characters{line_cut}; {more}]"

    def _max_chars(self) -> int:
        return max(200, int(self.swarm.config.agents.tool_output_chars))

    @staticmethod
    def _fit(lines: list[str], offset: int, end: int, budget: int) -> tuple[str, int, str]:
        """Join lines offset..end within `budget` characters. Returns (body, last line shown, cut kind)."""
        out: list[str] = []
        used = 0
        for number in range(offset, end + 1):
            line = lines[number - 1]
            cost = len(line) + (1 if out else 0)
            if used + cost <= budget:
                out.append(line)
                used += cost
                continue
            if not out:  # a single huge line: show its head
                return line[:budget], number, "line"
            return "\n".join(out), number - 1, "chars"
        return "\n".join(out), end, ""

    def read_raw(self, path: str, max_bytes: int = 2_000_000) -> tuple[str, bool]:
        p, rel = self._existing_file(path)
        try:
            size = p.stat().st_size
            with p.open("rb") as f:
                data = f.read(max(0, max_bytes))
        except OSError as exc:
            raise ToolError(f"Cannot read '{rel}': {exc.strerror or exc}.") from exc
        if b"\x00" in data[:BINARY_PROBE_BYTES]:
            return f"[binary file, {format_size(size)}]", False
        return data.decode("utf-8", errors="replace"), size > len(data)

    # --- writing ------------------------------------------------------------------------------
    async def write(self, actor: str, path: str, content: str) -> str:
        p = self.resolve(path)
        rel = self.rel(p)
        if p.is_dir():
            raise ToolError(f"'{rel}' is a directory; give a file path.")
        data = _encode(content or "", "content")
        async with self._io_lock:
            self.check_writable(actor, rel)
            existed = p.exists()
            if existed and self._read_bytes(p, rel) == data:
                return f"{rel} unchanged (same content)."
            commit = await self._store_and_commit(actor, p, rel, data, "write" if existed else "create")
        verb = "Updated" if existed else "Created"
        n = len(_split_lines(content or ""))
        return (f"{verb} {rel} ({n} line{'s' if n != 1 else ''}, {format_size(len(data))})"
                + self._commit_note(commit))

    async def edit(self, actor: str, path: str, old: str, new: str, replace_all: bool = False) -> str:
        if not old:
            raise ToolError("old_text must not be empty. Use write_file to create or overwrite a whole file.")
        new = new or ""
        _encode(new, "new_text")
        async with self._io_lock:
            p, rel = self._existing_file(path)
            self.check_writable(actor, rel)
            updated, replaced = self._apply_edit(rel, self._read_bytes(p, rel), old, new, replace_all)
            if updated is None:
                return f"{rel} unchanged (old_text equals new_text)."
            commit = await self._store_and_commit(actor, p, rel, updated.encode("utf-8"), "edit")
        return (f"Edited {rel}: replaced {replaced} occurrence{'s' if replaced != 1 else ''}"
                + self._commit_note(commit))

    def _apply_edit(self, rel: str, raw: bytes, old: str, new: str, replace_all: bool) -> tuple[str | None, int]:
        """Return (new text or None if nothing changes, occurrences replaced); ToolError on a bad match."""
        if b"\x00" in raw[:BINARY_PROBE_BYTES]:
            raise ToolError(f"'{rel}' is a binary file; it cannot be edited as text.")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(f"'{rel}' is not valid UTF-8; use write_file to replace it.") from exc
        old, new, count = self._match(text, old, new)
        if count == 0:
            raise ToolError(f"old_text was not found in '{rel}'." + self._near_hint(text, old)
                            + " Read the file (read_file) and copy the exact text, including whitespace.")
        if count > 1 and not replace_all:
            raise ToolError(f"old_text occurs {count} times in '{rel}'. Include more surrounding lines "
                            "to make it unique, or pass replace_all=true to replace every occurrence.")
        eol = _file_eol(text)
        if eol and "\n" in new:  # keep the file's line endings (read_file shows lines without CR)
            new = new.replace("\r\n", "\n")
            if eol == "\r\n":
                new = new.replace("\n", "\r\n")
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        return (None if updated == text else updated), (count if replace_all else 1)

    @staticmethod
    def _match(text: str, old: str, new: str) -> tuple[str, str, int]:
        """Count `old` in `text`; if absent, retry with the file's line endings (LF vs CRLF)."""
        count = text.count(old)
        if count or "\n" not in old:
            return old, new, count

        def lf(s: str) -> str:
            return s.replace("\r\n", "\n")

        variants = [(lf(old), lf(new))]
        if "\r\n" in text:
            variants.append((lf(old).replace("\n", "\r\n"), lf(new).replace("\n", "\r\n")))
        for old_v, new_v in variants:
            found = text.count(old_v)
            if found:
                return old_v, new_v, found
        return old, new, 0

    @staticmethod
    def _near_hint(text: str, old: str) -> str:
        first = next((ln.strip() for ln in old.splitlines() if ln.strip()), "")
        if not first:
            return ""
        for number, line in enumerate(_split_lines(text), 1):
            if first in line.strip():
                return f" A similar line exists at line {number} (check whitespace and the following lines)."
        return ""

    async def _store(self, p: Path, rel: str, data: bytes) -> None:
        def _write() -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            raise ToolError(f"Cannot write '{rel}': {exc.strerror or exc}.") from exc

    async def _store_and_commit(self, actor: str, p: Path, rel: str, data: bytes, op: str) -> str | None:
        """Write the file, emit file.changed and commit only this path as `actor`.

        The git lock is held from the write to the commit so that no other commit (e.g. a shell
        autocommit doing `add -A`) picks the change up under another member's name."""
        async with self._git_lock:
            await self._store(p, rel, data)
            self.swarm.bus.emit("file.changed", actor, path=rel, op=op)
            return await self._commit_locked(actor, f"{op} {rel}", rel)

    @staticmethod
    def _commit_note(commit: str | None) -> str:
        return f", commit {commit[:8]}." if commit else "."

    # --- listing ------------------------------------------------------------------------------
    def _scan(self, directory: Path) -> list[os.DirEntry]:
        try:
            with os.scandir(directory) as it:
                entries = [e for e in it if e.name.casefold() != ".git"]
        except OSError:
            return []
        return sorted(entries, key=lambda e: (not self._is_dir(e), e.name.casefold()))

    @staticmethod
    def _is_dir(entry: os.DirEntry) -> bool:
        try:
            return entry.is_dir(follow_symlinks=False)
        except OSError:
            return False

    @staticmethod
    def _is_link(entry: os.DirEntry) -> bool:
        """Symlinks and Windows junctions: listed but never followed (they may point outside)."""
        try:
            if entry.is_symlink():
                return True
            st = entry.stat(follow_symlinks=False)
            if not getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                return False
            tag = getattr(st, "st_reparse_tag", None)
            # junctions and other name surrogates; OneDrive/cloud placeholders are regular entries
            return tag is None or bool(tag & _REPARSE_TAG_NAME_SURROGATE)
        except OSError:
            return False

    @staticmethod
    def _stat(entry: os.DirEntry) -> tuple[int, float]:
        try:
            st = entry.stat(follow_symlinks=False)
            return st.st_size, st.st_mtime
        except OSError:
            return 0, 0.0

    def list(self, path: str = ".", depth: int = 2) -> str:
        p = self.resolve(path)
        rel = self.rel(p)
        if not p.exists():
            raise ToolError(f"Path not found: '{rel}'. Use list_files('.') to see the workspace root.")
        if not p.is_dir():
            return f"{rel} ({format_size(p.stat().st_size)})"
        depth = max(1, min(int(depth or 2), 20))
        lines = ["./" if rel == "." else f"{rel}/"]
        entries = self._scan(p)
        if not entries:
            lines.append("  (empty)")
        elif self._list_into(entries, 1, depth, lines):
            lines.append(f"[listing truncated after {LIST_MAX_ENTRIES} entries; "
                         "list a subdirectory or use a smaller depth]")
        return "\n".join(lines)

    def _list_into(self, entries: list[os.DirEntry], level: int, depth: int, lines: list[str]) -> bool:
        """Append indented entries; returns True when the entry cap was hit."""
        indent = "  " * level
        for entry in entries:
            if len(lines) - 1 >= LIST_MAX_ENTRIES:
                return True
            if self._is_link(entry):
                lines.append(f"{indent}{entry.name} (link)")
            elif not self._is_dir(entry):
                lines.append(f"{indent}{entry.name} ({format_size(self._stat(entry)[0])})")
            else:
                children = self._scan(Path(entry.path))
                if not children:
                    lines.append(f"{indent}{entry.name}/ (empty)")
                elif level >= depth or entry.name in NO_EXPAND:
                    n = len(children)
                    lines.append(f"{indent}{entry.name}/ ({n} entr{'y' if n == 1 else 'ies'})")
                else:
                    lines.append(f"{indent}{entry.name}/")
                    if self._list_into(children, level + 1, depth, lines):
                        return True
        return False

    def tree(self, path: str = ".", depth: int = 4) -> list[dict]:
        p = self.resolve(path)
        rel = self.rel(p)
        if not p.exists():
            raise ToolError(f"Path not found: '{rel}'.")
        if not p.is_dir():
            st = p.stat()
            return [{"path": rel, "type": "file", "size": st.st_size, "mtime": st.st_mtime}]
        out: list[dict] = []
        self._tree_into(p, rel, 1, max(1, min(int(depth or 4), 20)), out)
        return out

    def _tree_into(self, directory: Path, base: str, level: int, depth: int, out: list[dict]) -> None:
        for entry in self._scan(directory):
            if len(out) >= TREE_MAX_ENTRIES:
                return
            size, mtime = self._stat(entry)
            is_dir = self._is_dir(entry)
            rel = entry.name if base == "." else f"{base}/{entry.name}"
            out.append({"path": rel, "type": "dir" if is_dir else "file",
                        "size": 0 if is_dir else size, "mtime": mtime})
            if is_dir and level < depth and entry.name not in NO_EXPAND and not self._is_link(entry):
                self._tree_into(Path(entry.path), rel, level + 1, depth, out)

    # --- locks ------------------------------------------------------------------------------
    def _lock_rows(self) -> list[dict]:
        rows = self.swarm.db.all("SELECT path, owner, acquired_at, expires_at FROM file_locks ORDER BY path")
        return [dict(r) for r in rows]

    def _lock_rel(self, path: str) -> str:
        rel = self.rel(self.resolve(path))
        if rel == ".":
            raise ToolError("Cannot lock the whole workspace; lock a file or a directory.")
        return rel

    @staticmethod
    def _describe(lock: dict) -> str:
        left = max(0, round((lock["expires_at"] - time.time()) / 60))
        return f"locked by {lock['owner']} until {_clock(lock['expires_at'])} ({left} min left)"

    def lock(self, actor: str, path: str, minutes: float | None = None) -> str:
        rel = self._lock_rel(path)
        if minutes is None:
            minutes = self.swarm.config.workspace.lock_ttl_minutes
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            raise ToolError("minutes must be a positive number (lock duration).") from None
        if not minutes > 0:  # also rejects NaN
            raise ToolError("minutes must be a positive number (lock duration).")
        minutes = min(minutes, MAX_LOCK_MINUTES)
        self.expire_locks()
        mine: dict | None = None
        for lock in self._lock_rows():
            if lock["owner"] == actor:
                if _norm(lock["path"]) == _norm(rel):
                    mine = lock
                continue
            if _covers(lock["path"], rel) or _covers(rel, lock["path"]):
                where = "" if _norm(lock["path"]) == _norm(rel) else f" (lock on '{lock['path']}')"
                raise ToolError(f"'{rel}' is {self._describe(lock)}{where}. "
                                "Message them or wait until it is unlocked.")
        now = time.time()
        expires = now + minutes * 60
        if mine:
            self.swarm.db.execute("UPDATE file_locks SET expires_at = ? WHERE path = ?", (expires, mine["path"]))
            rel = mine["path"]
        else:
            self.swarm.db.execute(
                "INSERT OR REPLACE INTO file_locks(path, owner, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
                (rel, actor, now, expires))
        self.swarm.bus.emit("file.locked", actor, path=rel, owner=actor, expires_at=expires,
                            refreshed=bool(mine))
        verb = "Lock on" if mine else "Locked"
        tail = " refreshed" if mine else ""
        return (f"{verb} {rel}{tail} until {_clock(expires)} ({minutes:g} min). "
                "Unlock it when you are done.")

    def unlock(self, actor: str, path: str, force: bool = False) -> str:
        rel = self.rel(self.resolve(path))
        self.expire_locks()
        lock = next((lk for lk in self._lock_rows() if _norm(lk["path"]) == _norm(rel)), None)
        if lock is None:
            return f"{rel} is not locked."
        forced = lock["owner"] != actor
        if forced:
            if force and actor != USER:
                raise ToolError("Only the user can force-unlock a file locked by someone else.")
            if not force:
                hint = " Pass force=true to unlock it anyway." if actor == USER else \
                    " Ask them to unlock it, or wait until the lock expires."
                raise ToolError(f"'{rel}' is {self._describe(lock)}, not by you.{hint}")
        self.swarm.db.execute("DELETE FROM file_locks WHERE path = ?", (lock["path"],))
        self.swarm.bus.emit("file.unlocked", actor, path=lock["path"], owner=lock["owner"], forced=forced)
        suffix = f" (was locked by {lock['owner']})" if forced else ""
        return f"Unlocked {lock['path']}{suffix}."

    def locks(self) -> list[dict]:
        self.expire_locks()
        return self._lock_rows()

    def check_writable(self, actor: str, path: str) -> None:
        rel = self.rel(self.resolve(path))
        self.expire_locks()
        for lock in self._lock_rows():
            if lock["owner"] == actor:
                continue
            if _covers(lock["path"], rel):
                where = "" if _norm(lock["path"]) == _norm(rel) else f" (lock on '{lock['path']}')"
                raise ToolError(f"'{rel}' is {self._describe(lock)}{where}. "
                                "Ask them to unlock it, or wait until the lock expires.")

    def release_all(self, actor: str) -> int:
        rows = self.swarm.db.all("SELECT path FROM file_locks WHERE owner = ?", (actor,))
        for row in rows:
            self.swarm.db.execute("DELETE FROM file_locks WHERE path = ?", (row["path"],))
            self.swarm.bus.emit("file.unlocked", actor, path=row["path"], owner=actor, forced=False)
        return len(rows)

    def expire_locks(self) -> int:
        rows = self.swarm.db.all("SELECT path, owner FROM file_locks WHERE expires_at <= ?", (time.time(),))
        for row in rows:
            self.swarm.db.execute("DELETE FROM file_locks WHERE path = ?", (row["path"],))
            self.swarm.bus.emit("file.unlocked", None, path=row["path"], owner=row["owner"], expired=True)
        return len(rows)

    # --- git --------------------------------------------------------------------------------
    async def autocommit(self, actor: str, message: str) -> str | None:
        if not self.git_enabled:
            return None
        async with self._git_lock:
            return await self._commit_locked(actor, message, None)

    async def _commit_locked(self, actor: str, message: str, rel: str | None) -> str | None:
        """Commit everything (rel None) or just `rel` as `actor`. Caller holds the git lock."""
        if not self.git_enabled:
            return None
        spec = ("--", rel) if rel else ()
        try:
            status = await self._git(actor, "status", "--porcelain", *spec)
            if status.returncode != 0:
                log.warning("git status failed: %s", self._output(status))
                return None
            if not status.stdout.strip():
                return None
            # --ignore-errors: one file git cannot index must not stop the rest from being versioned
            add = await self._git(actor, "add", "-A", "--ignore-errors", *spec)
            if add.returncode != 0:
                log.warning("git add reported errors (committing what it could add): %s", self._output(add))
            commit = await self._git(actor, "commit", "-q", "--no-verify", "-m", message or "update", *spec)
            if commit.returncode != 0:
                log.info("git commit skipped: %s", self._output(commit))
                return None
            head = await self._git(actor, "rev-parse", "HEAD")
            return head.stdout.decode("ascii", "replace").strip() or None
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("git autocommit failed: %s", exc)
            return None

    def _git_argv(self, actor: str | None, args: tuple[str, ...]) -> list[str]:
        name = actor or "DreamTeam"
        return [
            self._git_exe or "git",
            "--literal-pathspecs",  # file names like "a[1].txt" are paths, not globs
            "-c", f"user.name={name}",
            "-c", f"user.email={email_slug(name)}@dreamteam.local",
            "-c", "core.autocrlf=false",
            "-c", "commit.gpgsign=false",
            "-c", "core.quotepath=false",
            "-c", f"safe.directory={self._real_root().as_posix()}",
            *args,
        ]

    async def _git(self, actor: str | None, *args: str) -> subprocess.CompletedProcess:
        argv = self._git_argv(actor, args)
        # Inherited GIT_* variables would redirect git elsewhere (GIT_DIR, GIT_INDEX_FILE, GIT_WORK_TREE)
        # or override the author (GIT_AUTHOR_NAME/EMAIL beat -c user.name), so drop them all.
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        env["GIT_TERMINAL_PROMPT"] = "0"
        kwargs: dict[str, Any] = {}
        if _WINDOWS:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return await asyncio.to_thread(
            subprocess.run, argv, cwd=str(self.root), env=env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=GIT_TIMEOUT, **kwargs,
        )

    @staticmethod
    def _output(proc: subprocess.CompletedProcess) -> str:
        return ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace").strip()
