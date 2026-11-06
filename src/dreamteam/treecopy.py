"""Copy a folder tree byte for byte, or measure one, with a digest that is the same on every OS.

`copy_tree(src, dst)` copies every regular file below `src` into the new folder `dst` (bytes, permission bits and
modification times), never follows a link (symlinks and Windows junctions are left out and listed) and leaves out
what the exclude rules name: folder or file names at any depth (`exclude_names`) and paths relative to `src`
(`exclude_paths`, written with '/'). `scan_tree(src)` walks the same way without copying. Both return a
`TreeResult`: counts, bytes, the tree digest and what was left out.

The tree digest is sha256 over the sorted lines `<relative path>\\0<size>\\0<sha256 of the bytes>` (paths with '/'
separators, NFC-normalized), so a folder copied to another computer has the same digest there. Empty folders are
copied but do not count in the digest. `compare_digest` leaves out the files that change without a change of content
(git's index and logs, lock files, the -wal/-shm/-journal files of databases): it tells whether two copies of a
workspace hold the same work.

Permission bits (`modes`): "keep" copies them as they are; "export" copies them where the destination's file system
can hold them (FAT and exFAT cannot: that is no error) and records the ones that matter (`TreeResult.modes`: the
executable, read-only and owner-restricted files); "import" ignores the bits the source reports (an export medium
makes them up) and sets the recorded ones (`apply_modes`) instead, never writable by the group or others.

Export uses the defaults below (EXPORT_EXCLUDE_NAMES, EXPORT_EXCLUDE_PATHS): environments and caches that are
rebuilt on the other computer, the agents' skills mirror and the stand-in folder of an unavailable workspace; `.git`
and the members' `.scratch/<member>` folders are kept. Paths use the extended-length form on Windows
(workspace.long_path), so deep git objects pass the 260-character limit.
"""

from __future__ import annotations

import hashlib
import os
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Collection, Iterable

from .workspace import is_link, long_path

# never copied by an export: rebuilt on the other computer (environments, caches)
EXPORT_EXCLUDE_NAMES: frozenset[str] = frozenset({"node_modules", ".venv", "venv", "__pycache__"})
# paths relative to a workspace: the agents' skills mirror (Swarm.start rebuilds it) and the stand-in folder of a
# workspace that is not available here (sessions.py)
EXPORT_EXCLUDE_PATHS: frozenset[str] = frozenset({".scratch/_skills", ".unavailable-workspace"})
CHUNK = 1 << 20
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)


@dataclass
class TreeResult:
    """What a walk found (scan_tree) or copied (copy_tree). Relative paths use '/'."""

    files: int = 0
    bytes: int = 0
    digest: str | None = None                                   # None: not hashed (scan_tree(hash=False))
    links: list[str] = field(default_factory=list)              # symlinks and junctions, never followed
    excluded: list[str] = field(default_factory=list)           # left out by exclude_names / exclude_paths
    special: list[str] = field(default_factory=list)            # FIFOs, sockets, devices
    errors: list[tuple[str, str]] = field(default_factory=list)  # (path, reason): could not be read, left out
    entries: list[tuple[str, int, str]] = field(default_factory=list, repr=False)   # (path, size, sha256)
    modes: dict[str, int] = field(default_factory=dict, repr=False)   # copy_tree(modes="export"): see mode_record


def tree_digest(entries: Iterable[tuple[str, int, str]]) -> str:
    """The digest of a tree from its (relative path, size, sha256) entries (see the module docstring)."""
    lines = sorted(f"{_nfc(rel)}\0{int(size)}\0{sha}\n" for rel, size, sha in entries)
    return hashlib.sha256("".join(lines).encode("utf-8", "surrogatepass")).hexdigest()


def _nfc(rel: str) -> str:
    return unicodedata.normalize("NFC", rel)


_GIT_VOLATILE = frozenset({"index", "FETCH_HEAD", "ORIG_HEAD", "gc.pid", "gc.log", "COMMIT_EDITMSG"})


_DB_SIDE_FILES = ("-wal", "-shm", "-journal")


def volatile(rel: str, paths: Collection[str] | None = None) -> bool:
    """A file that changes without a change of the work in it: below a `.git` folder its index (rewritten by
    every `git status` after a copy: it caches inode and change times), logs, lock files and the like; anywhere the
    -wal/-shm/-journal files of a database (`paths`: the tree's files; then only those next to their database,
    so a file named like `notes/dev-journal` still counts)."""
    parts = rel.split("/")
    name = parts[-1]
    suffix = next((s for s in _DB_SIDE_FILES if name.endswith(s)), None)
    if suffix and (paths is None or rel[:-len(suffix)] in paths):
        return True
    lowered = [p.casefold() for p in parts[:-1]]
    if ".git" not in lowered:
        return False
    after = parts[lowered.index(".git") + 1:]
    if "refs" in after[:-1] or (after and after[0] == "objects"):
        return False
    return (name in _GIT_VOLATILE or name.endswith(".lock") or name.startswith(("index.", "fsmonitor--daemon"))
            or "logs" in after[:-1])


def compare_digest(entries: Iterable[tuple[str, int, str]]) -> str:
    """The tree digest without the volatile files (see volatile): equal for two copies of the same work."""
    entries = list(entries)
    paths = {e[0] for e in entries}
    return tree_digest(e for e in entries if not volatile(e[0], paths))


def mode_record(mode: int) -> int | None:
    """The permission bits an export records for a file of mode `mode` (None: a plain default such as 0644): on
    POSIX the bits of an executable, read-only or owner-restricted file (0600, 0640); on Windows only read-only
    (0o444)."""
    bits = stat.S_IMODE(mode) & 0o777
    if os.name == "nt":
        return 0o444 if not bits & stat.S_IWRITE else None
    return None if bits in (0o644, 0o664, 0o666) else bits


def valid_mode(value: object) -> int | None:
    """A recorded mode (mode_record) as an import may set it: an int of permission bits only, else None."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0o777:
        return None
    return value


def _check_src(src: Path) -> Path:
    src = Path(src)
    if is_link(src):
        raise NotADirectoryError(f"{src} is a link, not a folder")
    if not os.path.isdir(long_path(src)):
        raise NotADirectoryError(f"{src} is not a folder")
    return src


def _walk(src: Path, exclude_names: Iterable[str], exclude_paths: Iterable[str], out: TreeResult, *,
          on_dir=None, on_file=None) -> None:
    """Visit every entry below `src` in name order (iteratively: deep trees do not hit the recursion limit)."""
    names = frozenset(exclude_names)
    paths = frozenset(p.strip("/") for p in exclude_paths)
    stack: list[tuple[str, tuple[str, ...]]] = [(long_path(src), ())]
    while stack:
        folder, parts = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            out.errors.append(("/".join(parts) or ".", exc.strerror or str(exc)))
            continue
        subdirs = []
        for entry in entries:
            rel = "/".join(parts + (entry.name,))
            if entry.name in names or rel in paths:
                out.excluded.append(rel)
                continue
            if is_link(entry):
                out.links.append(rel)
                continue
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                out.errors.append((rel, exc.strerror or str(exc)))
                continue
            if stat.S_ISDIR(st.st_mode):
                if on_dir is not None:
                    on_dir(rel)
                subdirs.append((entry.path, parts + (entry.name,)))
            elif stat.S_ISREG(st.st_mode):
                if on_file is not None:
                    on_file(entry.path, rel, st)
                else:
                    out.files += 1
                    out.bytes += st.st_size
            else:
                out.special.append(rel)
        stack.extend(reversed(subdirs))  # popped in name order


def _hash_file(path: str) -> tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    fd = os.open(path, _READ_FLAGS)
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise OSError("not a regular file")
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
            size += len(chunk)
    return size, h.hexdigest()


def scan_tree(src: str | os.PathLike, *, exclude_names: Iterable[str] = EXPORT_EXCLUDE_NAMES,
              exclude_paths: Iterable[str] = EXPORT_EXCLUDE_PATHS, hash: bool = True) -> TreeResult:
    """Walk `src` as copy_tree would, copying nothing. `hash`: read every file for the digest (False: counts and
    sizes from the file system only, `digest` None). NotADirectoryError when `src` is no folder (or a link)."""
    src = _check_src(Path(src))
    out = TreeResult()

    def on_file(path: str, rel: str, st: os.stat_result) -> None:
        try:
            size, sha = _hash_file(path)
        except OSError as exc:
            out.errors.append((rel, exc.strerror or str(exc)))
            return
        out.files += 1
        out.bytes += size
        out.entries.append((rel, size, sha))

    _walk(src, exclude_names, exclude_paths, out, on_file=on_file if hash else None)
    if hash:
        out.digest = tree_digest(out.entries)
    return out


def is_within(path: str | os.PathLike, parent: str | os.PathLike) -> bool:
    """`path` is `parent` or lies inside it. Compared as text (normcase) and, where `parent` exists, by file
    identity of `path` and its existing ancestors: a file system that ignores case (macOS, whose normcase does
    not) or another spelling of the same folder (NFD names, a bind mount) is no way around it."""
    p = os.path.normcase(os.path.abspath(path))
    q = os.path.normcase(os.path.abspath(parent))
    if p == q or p.startswith(q.rstrip(os.sep) + os.sep):
        return True
    try:
        if not os.path.isdir(q):
            return False
        target = os.stat(q)
        if not target.st_ino:   # a file system without file ids (some network or virtual ones): text only
            return False
        here = Path(os.path.abspath(path))
        for candidate in (here, *here.parents):
            if os.path.lexists(candidate):
                try:
                    st = os.stat(candidate)
                except OSError:
                    continue
                if st.st_ino and (st.st_dev, st.st_ino) == (target.st_dev, target.st_ino):
                    return True
    except (OSError, ValueError):
        return False
    return False


_within = is_within


def copy_tree(src: str | os.PathLike, dst: str | os.PathLike, *,
              exclude_names: Iterable[str] = EXPORT_EXCLUDE_NAMES,
              exclude_paths: Iterable[str] = EXPORT_EXCLUDE_PATHS, modes: str = "keep",
              apply_modes: dict[str, int] | None = None) -> TreeResult:
    """Copy the folder `src` into `dst` (created, with its parents; an existing empty folder is used). Files keep
    their bytes, permission bits (`modes`, see the module docstring; `apply_modes`: {relative path: bits} for
    "import") and modification time; links and special files are left out and listed; a file that cannot be read
    is left out and listed in `errors` (a running session may delete one meanwhile).
    Raises NotADirectoryError for a bad `src`, FileExistsError when `dst` is a file or a folder that is not empty,
    ValueError when `dst` lies inside `src`, and OSError when writing fails (the partial copy stays: the caller
    removes it)."""
    if modes not in ("keep", "export", "import"):
        raise ValueError(f"unknown modes '{modes}'")
    recorded = apply_modes or {}
    src = _check_src(Path(src))
    dst = Path(dst)
    try:
        real_src, real_dst = src.resolve(), dst.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"cannot resolve {src} or {dst}: {exc}") from None
    if _within(real_dst, real_src):
        raise ValueError(f"{dst} is inside {src}")
    if os.path.lexists(long_path(dst)):
        if is_link(dst) or not os.path.isdir(long_path(dst)):
            raise FileExistsError(f"{dst} exists and is not a folder")
        with os.scandir(long_path(dst)) as it:
            if any(True for _ in it):
                raise FileExistsError(f"{dst} is not empty")
    else:
        os.makedirs(long_path(dst))
    out = TreeResult()

    def on_dir(rel: str) -> None:
        os.mkdir(long_path(dst.joinpath(*rel.split("/"))))

    def on_file(path: str, rel: str, st: os.stat_result) -> None:
        target = long_path(dst.joinpath(*rel.split("/")))
        try:
            fd_in = os.open(path, _READ_FLAGS)
        except OSError as exc:
            out.errors.append((rel, exc.strerror or str(exc)))
            return
        with os.fdopen(fd_in, "rb") as fin:
            now = os.fstat(fin.fileno())
            if not stat.S_ISREG(now.st_mode) or (st.st_ino and now.st_ino and (st.st_ino, st.st_dev)
                                                 != (now.st_ino, now.st_dev)):
                out.errors.append((rel, "the file was replaced while it was copied"))
                return
            h = hashlib.sha256()
            size = 0
            try:
                fd_out = os.open(target, _WRITE_FLAGS, 0o666 if modes == "import" else 0o600)
                with os.fdopen(fd_out, "wb") as fout:
                    while True:
                        try:
                            chunk = fin.read(CHUNK)
                        except OSError as exc:
                            raise _ReadFailed(exc) from None
                        if not chunk:
                            break
                        h.update(chunk)
                        size += len(chunk)
                        fout.write(chunk)
            except _ReadFailed as failed:
                os.unlink(target)
                out.errors.append((rel, failed.exc.strerror or str(failed.exc)))
                return
            except OSError as exc:   # a write or its flush names no file: the error says which, and how large
                raise _write_failed(exc, target, st.st_size) from exc
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        # the bits last: a read-only file (git objects) stays read-only
        if modes == "keep":
            os.chmod(target, stat.S_IMODE(st.st_mode))
        elif modes == "export":
            record = mode_record(st.st_mode)
            if record is not None:
                out.modes[rel] = record
            try:   # FAT and exFAT hold no POSIX bits: the record carries them
                os.chmod(target, stat.S_IMODE(st.st_mode))
            except OSError:
                pass
        else:
            bits = valid_mode(recorded.get(rel))
            if bits is not None and os.name != "nt":
                os.chmod(target, bits & ~0o022)   # never writable by the group or others of this computer
            elif bits is not None and not bits & 0o200:
                os.chmod(target, stat.S_IREAD)
        out.files += 1
        out.bytes += size
        out.entries.append((rel, size, h.hexdigest()))

    _walk(src, exclude_names, exclude_paths, out, on_dir=on_dir, on_file=on_file)
    out.digest = tree_digest(out.entries)
    return out


def _write_failed(exc: OSError, target: str, src_size: int) -> OSError:
    """The OSError of a failed write (errno, strerror, on Windows winerror; the subclass Python picks for them) with
    the target path as `filename`, which a write or a flush leaves out, and the size of the source file as
    `src_size` (a file system may refuse a file that large)."""
    winerror = getattr(exc, "winerror", None)
    if os.name == "nt" and winerror is not None:
        new = OSError(exc.errno, exc.strerror or str(exc), exc.filename or target, winerror)
    else:
        new = OSError(exc.errno, exc.strerror or str(exc), exc.filename or target)
    new.src_size = src_size   # type: ignore[attr-defined]
    return new


class _ReadFailed(Exception):
    def __init__(self, exc: OSError):
        super().__init__(str(exc))
        self.exc = exc
