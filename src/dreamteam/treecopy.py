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
makes them up) and sets the recorded ones (`apply_modes`) instead, never writable by the group or others;
"writable" copies them as "keep" does, but the owner can always read and write, the group and others never write,
and the setuid, setgid and sticky bits are dropped; a file reported as 0o777, or any file of a source on a Linux
file system without POSIX bits (`fs_type`: WSL's drvfs, Docker Desktop's 9p, FAT, exFAT, NTFS, SMB/CIFS), becomes
0o644, or 0o755 when it starts with `#!` or is an ELF or Mach-O program.

Export uses the defaults below (EXPORT_EXCLUDE_NAMES, EXPORT_EXCLUDE_PATHS): environments and caches that are
rebuilt on the other computer, the agents' skills mirror and the stand-in folder of an unavailable workspace; `.git`
and the members' `.scratch/<member>` folders are kept. Paths use the extended-length form on Windows
(workspace.long_path), so deep git objects pass the 260-character limit.

A copy into a new session (copyfrom.py) uses COPY_EXCLUDE_NAMES ignoring case, COPY_SKIP_FILES, limits and
modes='writable'. On POSIX the walk lists each folder through a descriptor opened without following a link and
checked against its parent's listing: a folder swapped for a link meanwhile is never walked.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Collection, Iterable

from .workspace import is_link, long_path

# copy_tree(progress=...): called with (bytes, files) as the copy goes
CopyProgress = Callable[[int, int], None]

# never copied by an export: rebuilt on the other computer (environments, caches)
EXPORT_EXCLUDE_NAMES: frozenset[str] = frozenset({"node_modules", ".venv", "venv", "__pycache__"})
# paths relative to a workspace: the agents' skills mirror (Swarm.start rebuilds it) and the stand-in folder of a
# workspace that is not available here (sessions.py)
EXPORT_EXCLUDE_PATHS: frozenset[str] = frozenset({".scratch/_skills", ".unavailable-workspace"})
# copied into a new session (sessions.py, copyfrom.py): the export's names plus .git (DreamTeam makes its own
# repository and first commit), compared ignoring case
COPY_EXCLUDE_NAMES: frozenset[str] = EXPORT_EXCLUDE_NAMES | {".git"}
# files a copy into a new session leaves out without a word (OS litter: Finder's and Explorer's caches)
COPY_SKIP_FILES: frozenset[str] = frozenset({".DS_Store", "Thumbs.db"})
CHUNK = 1 << 20
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
# POSIX: a folder is opened without following a link, checked against what its parent listed and listed from that
# descriptor, so a folder swapped for a link while the walk runs is never walked (elsewhere: checked by its path)
_FD_WALK = os.scandir in os.supports_fd and hasattr(os, "O_DIRECTORY") and hasattr(os, "O_NOFOLLOW")
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_REPLACED_DIR = "the folder was replaced while it was copied"
GONE_DIR = "the folder was moved or deleted while it was copied"   # never `vanished`: a folder is no litter
# what stays executable from a file system without POSIX bits: scripts, ELF and Mach-O programs
_EXEC_MAGIC = (b"#!", b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe")
# Linux file systems that hold no POSIX bits: what they report (0o777; 0o755 for CIFS or a vfat umask; 0o555 for a
# read-only Windows file on WSL's drvfs or Docker Desktop's 9p) says nothing about a file being a program
_NO_POSIX_FS = frozenset({"9p", "drvfs", "vfat", "msdos", "exfat", "cifs", "smb3", "smbfs", "ntfs", "ntfs3",
                          "fuseblk"})


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
    vanished: list[str] = field(default_factory=list)   # the paths in `errors` that were gone (deleted meanwhile)
    skipped: int = 0                                    # files left out by skip_files (OS litter: .DS_Store)
    entries: list[tuple[str, int, str]] = field(default_factory=list, repr=False)   # (path, size, sha256)
    modes: dict[str, int] = field(default_factory=dict, repr=False)   # copy_tree(modes="export"): see mode_record


class TreeLimit(Exception):
    """A copy_tree / scan_tree limit was passed: `what` is "files" or "bytes", `limit` the number given."""

    def __init__(self, what: str, limit: int):
        super().__init__(f"more than {limit} {what}")
        self.what = what
        self.limit = limit


class _Budget:
    """The limits of one walk (max_files, max_bytes: None for none) and the caller's `check`, called before every
    file and after every chunk read. Only regular files that are walked count."""

    def __init__(self, max_files: int | None, max_bytes: int | None, check: Callable[[], None] | None):
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.check = check

    def before_file(self, out: TreeResult, size: int) -> None:
        """Before a file of `size` bytes (as the file system reports it) is counted or read."""
        if self.check is not None:
            self.check()
        if self.max_files is not None and out.files + 1 > self.max_files:
            raise TreeLimit("files", self.max_files)
        if self.max_bytes is not None and out.bytes + size > self.max_bytes:
            raise TreeLimit("bytes", self.max_bytes)

    def reading(self, out: TreeResult, size_so_far: int) -> None:
        """After a chunk of the current file was read (`size_so_far`: its bytes read so far): a file that grows."""
        if self.check is not None:
            self.check()
        if self.max_bytes is not None and out.bytes + size_so_far > self.max_bytes:
            raise TreeLimit("bytes", self.max_bytes)


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


def _failed(out: TreeResult, rel: str, exc: OSError, *, folder: bool = False) -> None:
    """`rel` could not be read: in `errors`, and in `vanished` too when it was a file that was gone (deleted while
    the walk ran). A folder that was gone is only in `errors` (GONE_DIR): a whole subtree, or the whole source moved
    or renamed meanwhile, is no deleted temporary file."""
    if folder and isinstance(exc, FileNotFoundError):
        out.errors.append((rel, GONE_DIR))
        return
    out.errors.append((rel, exc.strerror or str(exc)))
    if isinstance(exc, FileNotFoundError):
        out.vanished.append(rel)


def _listed_dir(entry: os.DirEntry) -> bool:
    """The listing said `entry` is a folder (the cached type; False when it cannot tell)."""
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def fs_type(path: str | os.PathLike, mountinfo: str | None = None) -> str | None:
    """The type of the file system `path` lies on, from /proc/self/mountinfo (or the text `mountinfo`); None where
    it is unknown (no Linux)."""
    if mountinfo is None:
        try:
            with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as f:
                mountinfo = f.read()
        except OSError:
            return None
    real = os.path.realpath(path)
    best, kind = -1, None
    for line in mountinfo.splitlines():
        head, sep, tail = line.partition(" - ")
        fields, rest = head.split(), tail.split()
        if not sep or len(fields) < 5 or not rest:
            continue
        point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])
        if (real == point or real.startswith(point.rstrip("/") + "/")) and len(point) >= best:
            best, kind = len(point), rest[0]   # the last of the longest mount points: what is mounted there now
    return kind


def _changed(now: os.stat_result, seen: os.stat_result) -> bool:
    return bool(now.st_ino and seen.st_ino and (now.st_dev, now.st_ino) != (seen.st_dev, seen.st_ino))


def _walk(src: Path, exclude_names: Iterable[str], exclude_paths: Iterable[str], out: TreeResult, *,
          on_dir=None, on_file=None, fold: bool = False, budget: _Budget | None = None,
          skip_files: Collection[str] = ()) -> None:
    """Visit every entry below `src` in name order (iteratively: deep trees do not hit the recursion limit).
    `fold`: `exclude_names` match ignoring case. `budget`: the limits, checked here for the files counted without
    `on_file` (which checks them itself). `skip_files`: names of regular files left out without a word (counted in
    `skipped`). A folder that is no longer the one its parent listed (a link put in its place) is not walked: an
    error."""
    names = frozenset(n.casefold() for n in exclude_names) if fold else frozenset(exclude_names)
    paths = frozenset(p.strip("/") for p in exclude_paths)
    stack: list[tuple[str, tuple[str, ...], os.stat_result | None]] = [(long_path(src), (), None)]
    while stack:
        folder, parts, seen = stack.pop()
        where = "/".join(parts) or "."
        fd = None
        try:
            if _FD_WALK:
                try:
                    fd = os.open(folder, _DIR_FLAGS)
                except OSError as exc:   # a link (O_NOFOLLOW) or a file now in the folder's place
                    if seen is not None and exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                        raise _Replaced() from None
                    raise
                if seen is not None and _changed(os.fstat(fd), seen):
                    raise _Replaced()
                with os.scandir(fd) as it:
                    entries = sorted(it, key=lambda e: e.name)
            else:
                if seen is not None and is_link(folder):
                    raise _Replaced()
                with os.scandir(folder) as it:
                    entries = sorted(it, key=lambda e: e.name)
        except _Replaced:
            out.errors.append((where, _REPLACED_DIR))
            entries = None
        except OSError as exc:
            _failed(out, where, exc, folder=True)
            entries = None
        if entries is None:
            if fd is not None:
                os.close(fd)
            continue
        subdirs = []
        try:   # (the entries of a descriptor's listing read their stat through it: open until they are done)
            for entry in entries:
                rel = "/".join(parts + (entry.name,))
                path = os.path.join(folder, entry.name)
                if (entry.name.casefold() if fold else entry.name) in names or rel in paths:
                    out.excluded.append(rel)
                    continue
                if is_link(entry):
                    out.links.append(rel)
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    _failed(out, rel, exc, folder=_listed_dir(entry))
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if on_dir is not None:
                        on_dir(rel)
                    subdirs.append((path, parts + (entry.name,), st))
                elif stat.S_ISREG(st.st_mode):
                    if entry.name in skip_files:
                        out.skipped += 1
                    elif on_file is not None:
                        on_file(path, rel, st)
                    else:
                        if budget is not None:
                            budget.before_file(out, st.st_size)
                        out.files += 1
                        out.bytes += st.st_size
                else:
                    out.special.append(rel)
        finally:
            if fd is not None:
                os.close(fd)
        stack.extend(reversed(subdirs))  # popped in name order


class _Replaced(Exception):
    pass


def _hash_file(path: str, on_chunk: Callable[[int], None] | None = None) -> tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    fd = os.open(path, _READ_FLAGS)
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise OSError("not a regular file")
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
            size += len(chunk)
            if on_chunk is not None:
                on_chunk(size)
    return size, h.hexdigest()


def scan_tree(src: str | os.PathLike, *, exclude_names: Iterable[str] = EXPORT_EXCLUDE_NAMES,
              exclude_paths: Iterable[str] = EXPORT_EXCLUDE_PATHS, hash: bool = True, fold_names: bool = False,
              max_files: int | None = None, max_bytes: int | None = None,
              check: Callable[[], None] | None = None, skip_files: Collection[str] = ()) -> TreeResult:
    """Walk `src` as copy_tree would, copying nothing. `hash`: read every file for the digest (False: counts and
    sizes from the file system only, `digest` None). NotADirectoryError when `src` is no folder (or a link).
    `fold_names`, `max_files`, `max_bytes`, `check` and `skip_files` as for copy_tree (TreeLimit when a limit is
    passed)."""
    src = _check_src(Path(src))
    out = TreeResult()
    budget = _Budget(max_files, max_bytes, check)

    def on_file(path: str, rel: str, st: os.stat_result) -> None:
        budget.before_file(out, st.st_size)
        try:
            size, sha = _hash_file(path, lambda so_far: budget.reading(out, so_far))
        except OSError as exc:
            _failed(out, rel, exc)
            return
        out.files += 1
        out.bytes += size
        out.entries.append((rel, size, sha))

    _walk(src, exclude_names, exclude_paths, out, on_file=on_file if hash else None, fold=fold_names,
          budget=budget, skip_files=skip_files)
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
              apply_modes: dict[str, int] | None = None, progress: CopyProgress | None = None,
              fold_names: bool = False, max_files: int | None = None, max_bytes: int | None = None,
              check: Callable[[], None] | None = None, skip_files: Collection[str] = ()) -> TreeResult:
    """Copy the folder `src` into `dst` (created, with its parents; an existing empty folder is used). Files keep
    their bytes, permission bits (`modes`, see the module docstring; `apply_modes`: {relative path: bits} for
    "import") and modification time; links and special files are left out and listed; a file that cannot be read
    is left out and listed in `errors` (a running session may delete one meanwhile: also in `vanished`); a folder
    that is no longer the one its parent listed (a link put in its place) is not copied, an error too.
    `skip_files`: names of regular files left out without a word (counted in `skipped`). `progress(bytes, files)` is
    called after each chunk written (bytes, 0) and after each file copied (0, 1); what it raises stops the copy.
    `fold_names`: `exclude_names` match ignoring case. `max_files` / `max_bytes`: TreeLimit before the file that
    would pass them (sizes as the file system reports them, and again as the bytes are read; only regular files
    count, reaching a limit exactly is allowed). `check()` is called before every file and after every chunk read;
    what it raises stops the copy (a cancellation).
    Raises NotADirectoryError for a bad `src`, FileExistsError when `dst` is a file or a folder that is not empty,
    ValueError when `dst` lies inside `src`, and OSError when writing fails (the partial copy stays: the caller
    removes it, as after a TreeLimit or what `check` or `progress` raised)."""
    if modes not in ("keep", "export", "import", "writable"):
        raise ValueError(f"unknown modes '{modes}'")
    budget = _Budget(max_files, max_bytes, check)
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
    no_posix = modes == "writable" and os.name != "nt" and fs_type(real_src) in _NO_POSIX_FS

    def on_dir(rel: str) -> None:
        os.mkdir(long_path(dst.joinpath(*rel.split("/"))))

    def on_file(path: str, rel: str, st: os.stat_result) -> None:
        budget.before_file(out, st.st_size)
        target = long_path(dst.joinpath(*rel.split("/")))
        try:
            fd_in = os.open(path, _READ_FLAGS)
        except OSError as exc:
            _failed(out, rel, exc)
            return
        with os.fdopen(fd_in, "rb") as fin:
            now = os.fstat(fin.fileno())
            if not stat.S_ISREG(now.st_mode) or (st.st_ino and now.st_ino and (st.st_ino, st.st_dev)
                                                 != (now.st_ino, now.st_dev)):
                out.errors.append((rel, "the file was replaced while it was copied"))
                return
            h = hashlib.sha256()
            size = 0
            head = b""
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
                        if not size:
                            head = chunk[:4]
                        size += len(chunk)
                        budget.reading(out, size)
                        fout.write(chunk)
                        if progress is not None:
                            progress(len(chunk), 0)
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
        elif modes == "writable":
            # the agents can read and edit it (the sandbox hub is root and reads a 000 file); no setuid / setgid /
            # sticky bits, and never writable by the group or others
            bits = stat.S_IMODE(st.st_mode) & 0o777
            if os.name != "nt" and (bits == 0o777 or no_posix):
                # a file system without POSIX bits (WSL's /mnt/c, FAT, exFAT, SMB) makes them up: only scripts and
                # programs stay executable, not every file committed as 100755
                bits = 0o755 if head.startswith(_EXEC_MAGIC) else 0o644
            os.chmod(target, (bits | stat.S_IRUSR | stat.S_IWUSR) & ~0o022)
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
        if progress is not None:
            progress(0, 1)

    _walk(src, exclude_names, exclude_paths, out, on_dir=on_dir, on_file=on_file, fold=fold_names,
          skip_files=skip_files)
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
