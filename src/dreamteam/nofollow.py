"""Open, list and walk inside a root folder without ever following a link (docs/SPEC.md §21.8, §21.10).

A path below the root is taken one name at a time. Each name must be one the folder holds, spelled exactly as its
listing has it (a letter-case alias, another Unicode form or a Windows short name such as GIT~1 names nothing:
FileNotFoundError), and none may be a link: a symlink or a junction met anywhere is `Refused`, never followed, also
when it was put in place of a folder or a file after it was checked (a swap while the request runs).

- POSIX: the root is opened with O_DIRECTORY | O_NOFOLLOW, and every name from its parent's descriptor (`dir_fd`)
  with O_NOFOLLOW, so no path is ever resolved again from the top: a link swapped in above is never crossed, one in
  the name's own place fails the open (ELOOP, ENOTDIR). A name counts only when it is in its parent's listing.
- Windows (no `dir_fd`): every name is opened by its full path below its parent's final path, with
  FILE_FLAG_OPEN_REPARSE_POINT, so the name itself is never followed. Its lstat just before and the open handle's
  attributes must show no name-surrogate reparse point (a symlink, a junction, a mount point); the handle's
  identity (fstat) must be the one lstat gave; its final path (GetFinalPathNameByHandleW) must be the parent's final
  path and the name, so a link anywhere above, swapped in meanwhile, leads elsewhere: out of the root it is refused;
  still below the root's final path (an alias, a short name, a folder renamed or a file moved inside the root
  meanwhile) the name is not there (FileNotFoundError). A folder is listed through its open handle
  (GetFileInformationByHandleEx), never by its path. Other reparse points (cloud placeholders, deduplicated files)
  are ordinary files and folders, opened again without the flag so their driver serves the data, and checked the
  same way. Handles share reading, writing and deleting: an agent may still rename or delete a file being read.
- A regular file with more than one hard link (st_nlink > 1, read by fstat of the open handle or lstat through the
  parent's descriptor, never DirEntry.stat(), which says 0 on Windows) is never read: another name of it may lie
  outside the root. FIFOs, sockets and devices are never opened.

`open_inside` opens one file (or folder) and gives its descriptor, `list_inside` lists one folder, `walk_inside`
walks a tree in name order and gives each regular file open, as a file object the walk owns. Paths of the computer
never appear in their errors: an error names the path relative to the root, with '/'. Everything here blocks (run
it in a thread). Stdlib only, ctypes on Windows.
"""

from __future__ import annotations

import errno
import io
import os
import stat
import struct
import sys
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Callable, Iterator

__all__ = ["DIR", "FILE", "LINK", "HARD_LINK", "SPECIAL", "EXCLUDED", "ERROR", "REASONS", "MAX_DEPTH", "Entry",
           "Refused", "open_inside", "list_inside", "walk_inside"]

# what an Entry is
DIR = "dir"                # a folder (walk_inside: entered next)
FILE = "file"              # a regular file with one name (walk_inside: open, Entry.fd)
LINK = "link"              # a symlink or a junction: never followed
HARD_LINK = "hard_link"    # a regular file with more than one name: never read
SPECIAL = "special"        # a FIFO, a socket, a device: never opened
EXCLUDED = "excluded"      # walk_inside: left out by `skip`, never opened
ERROR = "error"            # walk_inside: could not be opened or listed (Entry.error; FileNotFoundError: gone)

# Refused.reason -> its text
REASONS = {
    "link": "a link (a symlink or a junction) is never followed",
    "hard_link": "a file with more than one name (a hard link) is never read",
    "special": "not a regular file or a folder",
    "outside": "it leads out of the folder",
    "replaced": "it was replaced while it was opened",
}
# Refused reasons that end a walk; the others ("replaced", "special": a name the listing gave was meanwhile taken
# by another file, folder or a special file, which is not opened) are an ERROR entry of that name
_ENDS_WALK = frozenset({"link", "outside"})

# walk_inside enters at most this many folder levels below its start folder; a folder deeper is an ERROR entry. A walk
# holds one descriptor per level (and one for its current file), so this bounds what one walk holds however deep an
# agent nests a tree; what walks hold together is bounded by their callers (api_v1.ARCHIVES_AT_ONCE for the archives,
# which a client may keep open while it reads)
MAX_DEPTH = 64

_WINDOWS = sys.platform == "win32"
_WINDOWS_NAME_MAX = 255        # UTF-16 units of a name in a folder (NTFS, ReFS, exFAT); a longer one is EINVAL there
_NAME_SURROGATE = 0x20000000   # IsReparseTagNameSurrogate: the reparse point names another file (link, junction)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
# (O_NONBLOCK: a FIFO swapped in never blocks the open; cleared again once the file is known to be regular)
_FILE_FLAGS = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
               | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0))
# what an open with O_NOFOLLOW (and O_DIRECTORY) gives for a link in the name's place: ELOOP (Linux, macOS), EMLINK
# (FreeBSD), EFTYPE (NetBSD), ENOTDIR (O_DIRECTORY on Linux)
_LINK_ERRNOS = frozenset(n for n in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR, getattr(errno, "EFTYPE", None))
                         if n is not None)


class Refused(OSError):
    """A path refused, never followed nor read: `reason` is a key of REASONS ("link", "hard_link", "special",
    "outside", "replaced"), `rel` the path relative to the root with '/' ("" for the root itself). errno ELOOP."""

    def __init__(self, rel: str, reason: str):
        super().__init__(errno.ELOOP, REASONS[reason], rel or ".")
        self.rel = rel
        self.reason = reason


@dataclass(frozen=True)
class Entry:
    """One entry of list_inside or walk_inside. `parts`: its names relative to the folder listed or walked.
    `size`, `mtime_ns`, `mode`: from the open handle's fstat (walk_inside's DIR and FILE; list_inside's FILE on
    Windows), else from the listing. `error`: ERROR only, the OSError, which names the path relative to the root.
    `file`: walk_inside's FILE only (with open_files), the file open for reading, owned by the walk, which closes
    it when it goes on: read it through `reader()` before the next step. Once it is closed, reading it (or asking
    `fd`) is a ValueError, never another file's bytes; closing it before the walk does is harmless."""

    parts: tuple[str, ...]
    kind: str
    size: int = 0
    mtime_ns: int = 0
    mode: int = 0
    error: OSError | None = None
    file: BinaryIO | None = field(default=None, repr=False, compare=False)

    @property
    def name(self) -> str:
        return self.parts[-1]

    @property
    def rel(self) -> str:
        return "/".join(self.parts)

    @property
    def fd(self) -> int | None:
        """The descriptor of `file` while it is open (None without a file, ValueError once it was closed). Never
        keep it past the walk's next step, never close it: use `reader()`."""
        return None if self.file is None else self.file.fileno()

    def reader(self) -> BinaryIO:
        """The open file of a FILE entry of walk_inside (the same object every time); ValueError when there is none
        or it was closed."""
        if self.file is None or self.file.closed:
            raise ValueError(f"{self.rel} is not open")
        return self.file


@dataclass(frozen=True)
class _Dir:
    """An open folder: its descriptor and, on Windows, its final path (the reference for the names below it) and
    the root's (what lies below it is inside the root)."""

    fd: int
    final: str | None = None
    root_final: str | None = None


@dataclass(frozen=True)
class _Listed:
    """A name as its folder's listing gives it (nlink None: the listing does not say, Windows)."""

    name: str
    kind: str          # DIR | FILE | LINK | SPECIAL | ERROR
    size: int = 0
    mtime_ns: int = 0
    mode: int = 0
    nlink: int | None = None
    error: OSError | None = None


def _check_parts(parts: Any) -> tuple[str, ...]:
    """`parts` as a tuple of names (a path checked by copyfrom.check_rel first); ValueError for anything that is
    not one name inside a folder."""
    if isinstance(parts, (str, bytes)):
        raise TypeError("parts are the names of a path, not a path")
    parts = tuple(parts)
    for name in parts:
        if (not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\x00" in name
                or (_WINDOWS and ("\\" in name or ":" in name))):
            raise ValueError(f"not a name inside a folder: {name!r}")
    return parts


def _root_text(root: str | os.PathLike) -> str:
    text = os.fspath(root)
    if not isinstance(text, str) or not os.path.isabs(text):
        raise ValueError("the root must be an absolute path")
    return text


def _join(base: str, parts: tuple[str, ...]) -> str:
    rel = "/".join(parts)
    return f"{base}/{rel}" if base and rel else base or rel


def _named(exc: OSError, rel: str) -> OSError:
    """`exc` again, naming `rel` (relative to the root) instead of a path of the computer (same errno and subclass:
    FileNotFoundError, PermissionError...)."""
    if isinstance(exc, Refused):
        return exc
    return OSError(exc.errno, exc.strerror or str(exc), rel or ".")


def _close(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _reader(fd: int) -> BinaryIO:
    """A file object that owns `fd` (it closes `fd`, also when it cannot be made)."""
    try:
        raw = io.FileIO(fd, "rb", closefd=True)
    except BaseException:
        _close(fd)
        raise
    try:
        return io.BufferedReader(raw)
    except BaseException:
        raw.close()
        raise


def _close_file(file: BinaryIO) -> None:
    try:
        file.close()   # (a no-op when the reader closed it already)
    except OSError:
        pass


def _is_link(st: os.stat_result) -> bool:
    """A symlink, or (Windows) any reparse point that names another file: a junction, a mount point."""
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                and getattr(st, "st_reparse_tag", 0) & _NAME_SURROGATE)


def _expect(st: os.stat_result, rel: str, want_dir: bool) -> None:
    """What lstat says of `rel` is what is asked: no link (Refused), a folder or a regular file (else Refused
    "special"), and the kind asked (IsADirectoryError, NotADirectoryError)."""
    if _is_link(st):
        raise Refused(rel, "link")
    if stat.S_ISDIR(st.st_mode):
        if not want_dir:
            raise IsADirectoryError(errno.EISDIR, "it is a folder", rel or ".")
    elif not stat.S_ISREG(st.st_mode):
        raise Refused(rel, "special")
    elif want_dir:
        raise NotADirectoryError(errno.ENOTDIR, "it is not a folder", rel or ".")


def _same(before: os.stat_result, now: os.stat_result) -> bool:
    """The open handle (`now`) is the entry lstat saw (`before`): the same kind and, where the file system gives
    file ids, the same id."""
    if stat.S_IFMT(before.st_mode) != stat.S_IFMT(now.st_mode):
        return False
    return not (before.st_ino and now.st_ino) or (before.st_dev, before.st_ino) == (now.st_dev, now.st_ino)


def _listed(name: str, st: os.stat_result) -> _Listed:
    if stat.S_ISLNK(st.st_mode):
        kind = LINK
    elif stat.S_ISDIR(st.st_mode):
        kind = DIR
    elif stat.S_ISREG(st.st_mode):
        kind = FILE
    else:
        kind = SPECIAL
    return _Listed(name, kind, st.st_size if kind == FILE else 0, st.st_mtime_ns, st.st_mode, st.st_nlink)


# ---- POSIX: descriptors all the way down -------------------------------------------------------------------------
class _Posix:
    @staticmethod
    def root(path: str) -> _Dir:
        try:
            return _Dir(os.open(path, _DIR_FLAGS))
        except OSError as exc:
            if exc.errno in _LINK_ERRNOS:
                try:
                    link = stat.S_ISLNK(os.lstat(path).st_mode)
                except OSError:
                    link = False
                if link:
                    raise Refused("", "link") from None
            raise _named(exc, "") from None

    @staticmethod
    def spelled(parent: _Dir, name: str, rel: str) -> None:
        """`name` is in the parent's listing exactly as given (a case-insensitive or normalizing file system opens
        an alias too)."""
        try:
            names = os.listdir(parent.fd)
        except OSError as exc:
            raise _named(exc, rel) from None
        if name not in names:
            raise FileNotFoundError(errno.ENOENT, "no such name in the folder (names are compared exactly)", rel)

    @staticmethod
    def child(parent: _Dir, name: str, rel: str, *, want_dir: bool,
              data: bool = True) -> tuple[int, os.stat_result, str | None]:
        """Open `name` in `parent` without following it: (descriptor, its fstat, None)."""
        try:
            before = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
        except OSError as exc:
            raise _named(exc, rel) from None
        _expect(before, rel, want_dir)
        try:
            fd = os.open(name, _DIR_FLAGS if want_dir else _FILE_FLAGS, dir_fd=parent.fd)
        except OSError as exc:
            if exc.errno in _LINK_ERRNOS:   # a link (or another kind of entry) took the name's place meanwhile
                try:
                    now = os.stat(name, dir_fd=parent.fd, follow_symlinks=False)
                except OSError:
                    now = None
                raise Refused(rel, "link" if now is not None and _is_link(now) else "replaced") from None
            raise _named(exc, rel) from None
        try:
            st = os.fstat(fd)
            if not _same(before, st):
                raise Refused(rel, "replaced")
            if not want_dir:
                os.set_blocking(fd, True)
        except BaseException:
            _close(fd)
            raise
        return fd, st, None

    @staticmethod
    def listing(folder: _Dir, rel: str) -> list[_Listed]:
        """The folder's names in name order, with what lstat (through its descriptor) says of each."""
        out: list[_Listed] = []
        try:
            with os.scandir(folder.fd) as it:
                found = list(it)
        except OSError as exc:
            raise _named(exc, rel) from None
        for entry in found:   # (their stat goes through folder.fd, which stays open)
            try:
                st = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue   # deleted meanwhile
            except OSError as exc:
                out.append(_Listed(entry.name, ERROR, error=_named(exc, _join(rel, (entry.name,)))))
                continue
            out.append(_listed(entry.name, st))
        out.sort(key=lambda item: item.name)
        return out


# ---- Windows: full paths below a final path, every handle checked --------------------------------------------------
_FILE_READ_DATA = 0x1            # FILE_LIST_DIRECTORY for a folder
_FILE_READ_ATTRIBUTES = 0x80
_SYNCHRONIZE = 0x100000
_SHARE_ALL = 0x1 | 0x2 | 0x4     # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
_OPEN_EXISTING = 3
_FLAG_BACKUP_SEMANTICS = 0x02000000   # (needed to open a folder)
_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_TAG_INFO = 9
_FILE_FULL_DIRECTORY_INFO = 14
_FILE_FULL_DIRECTORY_RESTART_INFO = 15
_ERROR_FILE_NOT_FOUND = 2      # (the first query of a folder with no entries at all, not even '.': exFAT, shares)
_ERROR_NO_MORE_FILES = 18
_FILETIME_EPOCH = 116444736000000000   # 1970-01-01 in 100 ns steps since 1601
# FILE_FULL_DIR_INFO: NextEntryOffset, FileIndex, Creation/LastAccess/LastWrite/ChangeTime, EndOfFile,
# AllocationSize, FileAttributes, FileNameLength, EaSize (the reparse tag of a reparse point), then FileName
_FULL_DIR_INFO = struct.Struct("<IIqqqqqqIII")
_LIST_BUFFER = 64 * 1024


def _long(path: str) -> str:
    """`path` in the extended-length form (\\\\?\\C:\\... or \\\\?\\UNC\\server\\...): taken as it is, no 260-character
    limit, no name rewritten (a trailing dot stays)."""
    if path.startswith("\\\\?\\"):
        return path
    full = os.path.abspath(path)
    return "\\\\?\\UNC\\" + full[2:] if full.startswith("\\\\") else "\\\\?\\" + full


def _windows_mode(attributes: int) -> int:
    """st_mode as Python makes it from file attributes (for a folder the listing gives)."""
    bits = 0o555 if attributes & stat.FILE_ATTRIBUTE_READONLY else 0o777
    if attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
        return stat.S_IFDIR | bits
    return stat.S_IFREG | (bits & 0o666)


class _WindowsApi:
    """kernel32 through ctypes, with the prototypes used here (built on first use, on Windows only)."""

    def __init__(self) -> None:
        import ctypes
        import msvcrt
        from ctypes import wintypes as wt

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        def proto(name: str, restype: Any, *argtypes: Any) -> Any:
            fn = getattr(k32, name)
            fn.restype, fn.argtypes = restype, argtypes
            return fn

        self.ctypes, self.msvcrt, self.wt = ctypes, msvcrt, wt
        self.create_file = proto("CreateFileW", wt.HANDLE, wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                 wt.DWORD, wt.DWORD, wt.HANDLE)
        self.final_path_name = proto("GetFinalPathNameByHandleW", wt.DWORD, wt.HANDLE, wt.LPWSTR, wt.DWORD,
                                     wt.DWORD)
        self.information_ex = proto("GetFileInformationByHandleEx", wt.BOOL, wt.HANDLE, ctypes.c_int,
                                    ctypes.c_void_p, wt.DWORD)
        self.close_handle = proto("CloseHandle", wt.BOOL, wt.HANDLE)
        self.invalid = ctypes.c_void_p(-1).value

    def error(self, rel: str, code: int | None = None) -> OSError:
        """The last error of this thread as an OSError naming `rel` (its subclass by the Windows error)."""
        code = self.ctypes.get_last_error() if code is None else code
        return OSError(None, self.ctypes.FormatError(code).strip(), rel or ".", code)

    def open(self, full: str, rel: str, *, folder: bool, data: bool, reparse: bool) -> int:
        """A handle of `full` (`reparse`: the reparse point itself, never followed), as a C runtime descriptor."""
        access = (_FILE_READ_DATA if data else 0) | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE
        flags = (_FLAG_BACKUP_SEMANTICS if folder else 0) | (_FLAG_OPEN_REPARSE_POINT if reparse else 0)
        handle = self.create_file(full, access, _SHARE_ALL, None, _OPEN_EXISTING, flags, None)
        if handle is None or handle == self.invalid:
            raise self.error(rel)
        try:
            return self.msvcrt.open_osfhandle(handle, os.O_RDONLY)
        except BaseException:
            self.close_handle(handle)
            raise

    def attribute_tag(self, fd: int, rel: str) -> tuple[int, int]:
        """(file attributes, reparse tag) of the open handle."""
        info = (self.wt.DWORD * 2)()
        if not self.information_ex(self.msvcrt.get_osfhandle(fd), _FILE_ATTRIBUTE_TAG_INFO, info,
                                   self.ctypes.sizeof(info)):
            raise self.error(rel)
        return info[0], info[1]

    def final_path(self, fd: int, rel: str) -> str:
        """The open handle's final path (normalized: long names as the folder holds them, \\\\?\\ form)."""
        handle = self.msvcrt.get_osfhandle(fd)
        size = 1024
        while True:
            buf = self.ctypes.create_unicode_buffer(size)
            n = self.final_path_name(handle, buf, size, 0)
            if not n:
                raise self.error(rel)
            if n < size:
                return buf[:n]
            size = n + 1

    def list_dir(self, fd: int, rel: str) -> list[tuple[str, int, int, int, int]]:
        """The entries of the open folder: (name, attributes, reparse tag, size, modification time in ns)."""
        handle = self.msvcrt.get_osfhandle(fd)
        buf = self.ctypes.create_string_buffer(_LIST_BUFFER)
        kind = _FILE_FULL_DIRECTORY_RESTART_INFO
        out = []
        while True:
            if not self.information_ex(handle, kind, buf, _LIST_BUFFER):
                code = self.ctypes.get_last_error()
                if code == _ERROR_NO_MORE_FILES or (code == _ERROR_FILE_NOT_FOUND
                                                    and kind == _FILE_FULL_DIRECTORY_RESTART_INFO):
                    return out
                raise self.error(rel, code)
            kind = _FILE_FULL_DIRECTORY_INFO
            raw = buf.raw
            offset = 0
            while True:
                (next_offset, _index, _created, _accessed, written, _changed, size, _allocated, attributes,
                 name_length, tag) = _FULL_DIR_INFO.unpack_from(raw, offset)
                start = offset + _FULL_DIR_INFO.size
                name = raw[start:start + name_length].decode("utf-16-le", "surrogatepass")
                if name not in (".", ".."):
                    out.append((name, attributes, tag, size, (written - _FILETIME_EPOCH) * 100))
                if not next_offset:
                    break
                offset += next_offset


class _Windows:
    def __init__(self) -> None:
        self._api: _WindowsApi | None = None

    @property
    def api(self) -> _WindowsApi:
        if self._api is None:
            self._api = _WindowsApi()
        return self._api

    def _open(self, full: str, rel: str, *, want_dir: bool, data: bool) -> tuple[int, os.stat_result, str]:
        """Open `full` without following it: lstat first (no link, the kind asked), then a handle of the entry
        itself, which must show no link either, be what lstat saw, and gives its final path."""
        try:
            before = os.lstat(full)
        except OSError as exc:
            raise _named(exc, rel) from None
        _expect(before, rel, want_dir)
        api = self.api
        fd = api.open(full, rel, folder=want_dir, data=data, reparse=True)
        try:
            attributes, tag = api.attribute_tag(fd, rel)
            if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                if tag & _NAME_SURROGATE:
                    raise Refused(rel, "link")
                # a placeholder or a deduplicated file: its driver gives the data only to an ordinary open, whose
                # handle must still be what lstat saw (and, for a name below a folder, end at its final path)
                checked, fd = fd, -1
                _close(checked)
                fd = api.open(full, rel, folder=want_dir, data=data, reparse=False)
            st = os.fstat(fd)
            if not _same(before, st):
                raise Refused(rel, "replaced")
            return fd, st, api.final_path(fd, rel)
        except BaseException:
            if fd >= 0:
                _close(fd)
            raise

    def root(self, path: str) -> _Dir:
        fd, _st, final = self._open(_long(path), "", want_dir=True, data=True)
        return _Dir(fd, final, final)

    @staticmethod
    def spelled(parent: _Dir, name: str, rel: str) -> None:
        """A name longer than a Windows folder can hold (_WINDOWS_NAME_MAX UTF-16 units) is in none: FileNotFoundError,
        as on POSIX, whose listing never holds it, where Windows itself would answer EINVAL. (Any other spelling is
        checked by the final path of what is opened: see child.)"""
        if len(name.encode("utf-16-le")) // 2 > _WINDOWS_NAME_MAX:
            raise FileNotFoundError(errno.ENOENT, "no such name in the folder (longer than a name can be)", rel)

    def child(self, parent: _Dir, name: str, rel: str, *, want_dir: bool,
              data: bool = True) -> tuple[int, os.stat_result, str]:
        """Open `name` below the parent's final path without following it: (descriptor, fstat, final path). The
        final path must be the parent's and `name`. One that lies out of the root (a link above, swapped in after
        the parent was checked) is Refused "outside". One still below the root's final path is not this name
        (FileNotFoundError): another spelling of it (letter case, a short name), or it or a folder above it renamed
        or moved inside the root meanwhile; a final path has no link in it, so the file is inside, but not read."""
        assert parent.final is not None and parent.root_final is not None
        head = parent.final.rstrip("\\")
        expected = f"{head}\\{name}"
        fd, st, final = self._open(expected, rel, want_dir=want_dir, data=data)
        if final != expected:
            _close(fd)
            if final.startswith(parent.root_final.rstrip("\\") + "\\"):
                raise FileNotFoundError(errno.ENOENT, "no such name in the folder (names are compared exactly; or "
                                        "it was renamed or moved meanwhile)", rel)
            raise Refused(rel, "outside")
        return fd, st, final

    def listing(self, folder: _Dir, rel: str) -> list[_Listed]:
        """The open folder's names in name order, read through its handle."""
        out = []
        for name, attributes, tag, size, mtime_ns in self.api.list_dir(folder.fd, rel):
            if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT and tag & _NAME_SURROGATE:
                kind = LINK
            elif attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
                kind = DIR
            else:
                kind = FILE
            out.append(_Listed(name, kind, size if kind == FILE else 0, mtime_ns, _windows_mode(attributes)))
        out.sort(key=lambda item: item.name)
        return out


_FS: Any = _Windows() if _WINDOWS else _Posix()


def _open_folder(root: str | os.PathLike, parts: tuple[str, ...]) -> _Dir:
    """The folder `parts` below `root`, open, reached name by name."""
    fs = _FS
    folder = fs.root(_root_text(root))
    try:
        for i, name in enumerate(parts):
            rel = "/".join(parts[:i + 1])
            fs.spelled(folder, name, rel)
            fd, _st, final = fs.child(folder, name, rel, want_dir=True)
            _close(folder.fd)
            folder = _Dir(fd, final, folder.root_final)
    except BaseException:
        _close(folder.fd)
        raise
    return folder


def open_inside(root: str | os.PathLike, parts: Any = (), *, want_dir: bool = False) -> int:
    """A descriptor of the file `parts` names below the folder `root` (absolute, resolved by the caller), open for
    reading; with `want_dir`, of the folder (the root itself for no parts). The caller closes it.
    Raises ValueError for a part that is no name inside a folder, `Refused` for a link anywhere on the way (or one
    swapped in), a file with another name ("hard_link") or a special file, FileNotFoundError for a name the folder
    does not hold exactly so, IsADirectoryError / NotADirectoryError for the other kind, and OSError when it cannot
    be read. Blocking."""
    parts = _check_parts(parts)
    if not parts:
        if not want_dir:
            raise IsADirectoryError(errno.EISDIR, "it is a folder", ".")
        return _FS.root(_root_text(root)).fd
    folder = _open_folder(root, parts[:-1])
    fs = _FS
    rel = "/".join(parts)
    try:
        fs.spelled(folder, parts[-1], rel)
        fd, st, _final = fs.child(folder, parts[-1], rel, want_dir=want_dir)
    finally:
        _close(folder.fd)
    if not want_dir and st.st_nlink > 1:
        _close(fd)
        raise Refused(rel, "hard_link")
    return fd


def list_inside(root: str | os.PathLike, parts: Any = ()) -> list[Entry]:
    """The folders (DIR) and regular files with one name (FILE) in the folder `parts` below `root`, in name order;
    links, files with more than one name (st_nlink > 1) and special files are left out, as is a name gone
    meanwhile. Entry.parts is the one name. Errors as open_inside's for the folder; `Refused` also when a file's
    path now leads out of the root (Windows: a link swapped in above it). Blocking."""
    parts = _check_parts(parts)
    fs = _FS
    base = "/".join(parts)
    folder = _open_folder(root, parts)
    out: list[Entry] = []
    try:
        for item in fs.listing(folder, base):
            if item.kind == DIR:
                out.append(Entry((item.name,), DIR, 0, item.mtime_ns, item.mode))
            elif item.kind == FILE and item.nlink is not None:   # (POSIX: lstat through the folder's descriptor)
                if item.nlink <= 1:
                    out.append(Entry((item.name,), FILE, item.size, item.mtime_ns, item.mode))
            elif item.kind == FILE:   # (Windows: only a handle tells the number of names; attributes suffice)
                try:
                    fd, st, _final = fs.child(folder, item.name, _join(base, (item.name,)), want_dir=False,
                                              data=False)
                except Refused as exc:
                    if exc.reason == "outside":
                        raise
                    continue   # a link or another file in its place meanwhile: nothing of it is shown
                except OSError:
                    continue   # gone (or moved), or not a file any more
                _close(fd)
                if st.st_nlink <= 1:
                    out.append(Entry((item.name,), FILE, st.st_size, st.st_mtime_ns, st.st_mode))
    finally:
        _close(folder.fd)
    return out


def walk_inside(root: str | os.PathLike, parts: Any = (), *,
                skip: Callable[[tuple[str, ...]], bool] | None = None, open_files: bool = True) -> Iterator[Entry]:
    """Every entry below the folder `parts` of `root`, depth first in name order (a folder's entry, then what it
    holds). Entry.parts is relative to that folder. `skip(parts)`: True leaves the entry out (EXCLUDED: never
    opened, a folder never entered), asked before anything else. A FILE comes open (Entry.reader(), a file object
    the walk owns) until the walk goes on, when the walk closes it; with `open_files` False it comes without one,
    for a walk that only counts and measures (POSIX: what lstat through its folder's descriptor says; Windows:
    checked through a handle that cannot read data). Links (LINK), files with more than one name (HARD_LINK) and
    special files (SPECIAL) are given but never read (only Windows opens a file to count its names, and closes a
    HARD_LINK unread). What cannot be opened or listed is an ERROR entry (a FileNotFoundError: gone, or on Windows
    renamed or moved inside the root, meanwhile), and so is a name the listing gave that was meanwhile taken by
    another file or folder (Refused "replaced") or a special file (Refused "special"), never opened, and a folder
    more than MAX_DEPTH levels below the start folder, never entered.
    A link swapped in for a folder or a file the listing gave, or a path that now leads out of the root, raises
    `Refused`: the walk ends. Errors of the start folder (as open_inside's) are raised by the first next().
    Every descriptor is closed when the walk ends, also when it is left before. Blocking."""
    parts = _check_parts(parts)
    fs = _FS
    base = "/".join(parts)
    stack: list[tuple[_Dir, tuple[str, ...], Iterator[_Listed]]] = []
    try:
        start = _open_folder(root, parts)
        try:
            listed = fs.listing(start, base)
        except BaseException:
            _close(start.fd)
            raise
        stack.append((start, (), iter(listed)))
        while stack:
            folder, here, items = stack[-1]
            item = next(items, None)
            if item is None:
                stack.pop()
                _close(folder.fd)
                continue
            sub = here + (item.name,)
            rel = _join(base, sub)
            if skip is not None and skip(sub):
                yield Entry(sub, EXCLUDED)
                continue
            if item.kind == ERROR:
                yield Entry(sub, ERROR, error=item.error)
                continue
            if item.kind in (LINK, SPECIAL) or (item.kind == FILE and (item.nlink or 0) > 1):
                yield Entry(sub, HARD_LINK if item.kind == FILE else item.kind, item.size, item.mtime_ns, item.mode)
                continue
            want_dir = item.kind == DIR
            if want_dir and len(sub) > MAX_DEPTH:
                yield Entry(sub, ERROR, error=OSError(errno.ENAMETOOLONG, f"more than {MAX_DEPTH} folders deep", rel))
                continue
            if not (want_dir or open_files) and item.nlink is not None:   # (POSIX: the listing's lstat suffices)
                yield Entry(sub, FILE, item.size, item.mtime_ns, item.mode)
                continue
            try:
                fd, st, final = fs.child(folder, item.name, rel, want_dir=want_dir, data=want_dir or open_files)
            except Refused as exc:
                if exc.reason in _ENDS_WALK:
                    raise
                yield Entry(sub, ERROR, error=exc)   # (closed unread: what took the name's place is never read)
                continue
            except OSError as exc:
                yield Entry(sub, ERROR, error=exc)
                continue
            if want_dir:
                child = _Dir(fd, final, folder.root_final)
                try:
                    listed = fs.listing(child, rel)
                except OSError as exc:
                    _close(fd)
                    yield Entry(sub, ERROR, error=exc)
                    continue
                stack.append((child, sub, iter(listed)))
                yield Entry(sub, DIR, 0, st.st_mtime_ns, st.st_mode)
                continue
            if st.st_nlink > 1 or not open_files:
                _close(fd)
                yield Entry(sub, HARD_LINK if st.st_nlink > 1 else FILE, st.st_size, st.st_mtime_ns, st.st_mode)
                continue
            file = _reader(fd)
            try:
                yield Entry(sub, FILE, st.st_size, st.st_mtime_ns, st.st_mode, file=file)
            finally:
                _close_file(file)
    finally:
        for folder, _here, _items in stack:
            _close(folder.fd)
