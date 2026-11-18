"""nofollow.py: open, list and walk inside a root without following a link (docs/SPEC.md §21.8, §21.10).

Links are junctions on Windows (no privilege needed) and symlinks elsewhere. Only the stdlib and nofollow are
imported, so the POSIX cases run on a Linux file system without the project's environment too."""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import unicodedata
from collections import Counter
from pathlib import Path

import pytest

from dreamteam import nofollow as nf
from dreamteam.nofollow import (DIR, ERROR, EXCLUDED, FILE, HARD_LINK, LINK, SPECIAL, Refused, list_inside,
                                open_inside, walk_inside)

WINDOWS = sys.platform == "win32"
SECRET = b"SECRET outside the root"


def _write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)
    return path


def _dir_link(target: Path, link: Path) -> None:
    """A link to the folder `target` at `link`: a junction on Windows, a symlink elsewhere."""
    if WINDOWS:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def _swap_for_link(path: Path, target: Path) -> None:
    """Put a link to the folder `target` in place of `path` (moved aside)."""
    path.rename(path.with_name(path.name + ".old"))
    _dir_link(target, path)


def _read(fd: int) -> bytes:
    with os.fdopen(fd, "rb") as f:
        return f.read()


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    """root/{a.txt, b/x.txt, c.txt} and outside/secret.txt."""
    root = tmp_path / "root"
    _write(root / "a.txt", "A")
    _write(root / "b" / "x.txt", "X")
    _write(root / "c.txt", "C")
    outside = tmp_path / "outside"
    _write(outside / "secret.txt", SECRET)
    _write(outside / "x.txt", SECRET)
    return root, outside


def _walk(root: Path, parts: tuple[str, ...] = (), **kw) -> list[tuple[str, str, bytes | None]]:
    """(kind, rel, bytes of a FILE) for every entry of the walk."""
    out = []
    for entry in walk_inside(root, parts, **kw):
        data = None
        if entry.kind == FILE:
            with entry.reader() as f:
                data = f.read()
        out.append((entry.kind, entry.rel, data))
    return out


def _no_secret(entries) -> None:
    assert all(data != SECRET for _kind, _rel, data in entries)


def _before_open(monkeypatch, rel: str, action) -> list[int]:
    """Run `action` once, just before nofollow opens `rel` (after its lstat). POSIX opens a name from its parent's
    descriptor (so `rel` is matched by its last name), Windows by its full path. Gives a list that is [1] once done."""
    done: list[int] = []

    def once() -> None:
        if not done:
            done.append(1)
            action()

    if WINDOWS:
        api = nf._FS.api
        real = api.open

        def fake(full, rel_, **kw):
            if rel_ == rel:
                once()
            return real(full, rel_, **kw)
        monkeypatch.setattr(api, "open", fake)
    else:
        real = os.open

        def fake(path, flags, *args, **kw):
            if path == rel.rpartition("/")[2] and "dir_fd" in kw:
                once()
            return real(path, flags, *args, **kw)
        monkeypatch.setattr(os, "open", fake)
    return done


# ---- open_inside ------------------------------------------------------------------------------------------------
def test_a_file_and_a_folder_open_by_their_names(tmp_path):
    root, _ = _tree(tmp_path)
    assert _read(open_inside(root, ("b", "x.txt"))) == b"X"
    assert _read(open_inside(str(root), ["a.txt"])) == b"A"
    for parts in (("b",), ()):
        fd = open_inside(root, parts, want_dir=True)
        try:
            assert stat.S_ISDIR(os.fstat(fd).st_mode)
        finally:
            os.close(fd)


@pytest.mark.parametrize("parts", [("",), (".",), ("..",), ("b", ".."), ("b/x.txt",), ("a\x00",)]
                         + ([("b\\x.txt",), ("a.txt:stream",)] if WINDOWS else []))
def test_a_part_that_is_no_name_inside_the_folder_is_refused(tmp_path, parts):
    root, _ = _tree(tmp_path)
    with pytest.raises(ValueError):
        open_inside(root, parts)
    with pytest.raises(ValueError):
        list_inside(root, parts)
    with pytest.raises(ValueError):
        list(walk_inside(root, parts))


def test_a_path_text_or_a_relative_root_is_refused(tmp_path):
    root, _ = _tree(tmp_path)
    with pytest.raises(TypeError):
        open_inside(root, "a.txt")
    with pytest.raises(ValueError):
        open_inside(os.path.relpath(root), ("a.txt",))


def test_what_is_missing_or_of_the_other_kind(tmp_path):
    root, _ = _tree(tmp_path)
    with pytest.raises(FileNotFoundError):
        open_inside(root, ("nope.txt",))
    with pytest.raises(FileNotFoundError):
        open_inside(root, ("nope", "x.txt"))
    with pytest.raises(IsADirectoryError):
        open_inside(root, ("b",))
    with pytest.raises(IsADirectoryError):
        open_inside(root, ())
    with pytest.raises(NotADirectoryError):
        open_inside(root, ("a.txt",), want_dir=True)
    with pytest.raises(NotADirectoryError):
        open_inside(root, ("a.txt", "x"))
    with pytest.raises(NotADirectoryError):
        list_inside(root, ("a.txt",))
    with pytest.raises(FileNotFoundError):
        open_inside(tmp_path / "no-root", ("a.txt",))


def test_errors_name_the_path_inside_the_root_never_the_computers(tmp_path):
    root, outside = _tree(tmp_path)
    _dir_link(outside, root / "j")
    for call in (lambda: open_inside(tmp_path / "no-root", ("a.txt",)), lambda: open_inside(root, ("b", "no")),
                 lambda: open_inside(root, ("j", "secret.txt")), lambda: list(walk_inside(root, ("j",)))):
        with pytest.raises(OSError) as info:
            call()
        assert str(tmp_path) not in str(info.value) and tmp_path.name not in str(info.value)
    with pytest.raises(FileNotFoundError) as info:
        open_inside(root, ("b", "no"))
    assert info.value.filename == "b/no"


# ---- links ------------------------------------------------------------------------------------------------------
def test_a_link_to_a_folder_outside_is_never_followed(tmp_path):
    root, outside = _tree(tmp_path)
    _dir_link(outside, root / "j")
    with pytest.raises(Refused) as info:
        open_inside(root, ("j", "secret.txt"))
    assert (info.value.reason, info.value.rel) == ("link", "j")
    with pytest.raises(Refused):
        open_inside(root, ("j",), want_dir=True)
    with pytest.raises(Refused):
        list_inside(root, ("j",))
    with pytest.raises(Refused):
        list(walk_inside(root, ("j",)))
    assert [e.name for e in list_inside(root)] == ["a.txt", "b", "c.txt"]
    entries = _walk(root)
    assert ("link", "j", None) in entries and not any(rel.startswith("j/") for _k, rel, _d in entries)
    _no_secret(entries)


def test_a_link_to_a_file_is_never_followed(tmp_path):
    root, outside = _tree(tmp_path)
    try:
        os.symlink(outside / "secret.txt", root / "s.txt")
    except (OSError, NotImplementedError):
        pytest.skip("this system makes no file symlinks for this user")
    with pytest.raises(Refused) as info:
        open_inside(root, ("s.txt",))
    assert info.value.reason == "link"
    assert "s.txt" not in [e.name for e in list_inside(root)]
    entries = _walk(root)
    assert ("link", "s.txt", None) in entries
    _no_secret(entries)


def test_a_link_that_stays_inside_the_root_is_refused_too(tmp_path):
    root, _ = _tree(tmp_path)
    _dir_link(root / "b", root / "alias")
    with pytest.raises(Refused):
        open_inside(root, ("alias", "x.txt"))
    assert [kind for kind, rel, _d in _walk(root) if rel.startswith("alias")] == [LINK]


def test_a_root_that_is_a_link_is_refused(tmp_path):
    root, _ = _tree(tmp_path)
    _dir_link(root, tmp_path / "root-link")
    with pytest.raises(Refused) as info:
        open_inside(tmp_path / "root-link", ("a.txt",))
    assert (info.value.reason, info.value.rel) == ("link", "")
    with pytest.raises(Refused):
        list_inside(tmp_path / "root-link")
    with pytest.raises(Refused):
        list(walk_inside(tmp_path / "root-link"))


def test_a_swap_between_the_check_and_the_open_is_refused(tmp_path, monkeypatch):
    """A link put in a folder's place after its lstat, before its open: the open itself never follows it (POSIX:
    O_NOFOLLOW from the parent's descriptor; Windows: the handle of the reparse point shows it)."""
    root, outside = _tree(tmp_path)
    done = _before_open(monkeypatch, "b", lambda: _swap_for_link(root / "b", outside))
    with pytest.raises(Refused) as info:
        open_inside(root, ("b", "x.txt"))
    assert done and (info.value.reason, info.value.rel) == ("link", "b")


def test_a_file_replaced_between_the_check_and_the_open_is_refused(tmp_path, monkeypatch):
    root, _ = _tree(tmp_path)
    other = _write(tmp_path / "other.txt", "other")
    done = _before_open(monkeypatch, "a.txt", lambda: os.replace(other, root / "a.txt"))
    with pytest.raises(Refused) as info:
        open_inside(root, ("a.txt",))
    assert done and info.value.reason == "replaced"


def test_a_file_replaced_mid_walk_is_an_error_entry_and_the_walk_goes_on(tmp_path, monkeypatch):
    """An atomic save (another file renamed over the name) between the lstat and the open: that name is an ERROR
    entry, never read, and the walk goes on (only a link or a path out of the root ends it)."""
    root, _ = _tree(tmp_path)
    _write(root / "d.txt", "D")
    other = _write(tmp_path / "other.txt", "new C")
    done = _before_open(monkeypatch, "c.txt", lambda: os.replace(other, root / "c.txt"))
    got, error = [], None
    for entry in walk_inside(root):
        got.append((entry.kind, entry.rel, entry.reader().read() if entry.kind == FILE else None))
        if entry.kind == ERROR:
            error = entry.error
    assert done and got == [(FILE, "a.txt", b"A"), (DIR, "b", None), (FILE, "b/x.txt", b"X"),
                            (ERROR, "c.txt", None), (FILE, "d.txt", b"D")]
    assert isinstance(error, Refused) and (error.reason, error.rel) == ("replaced", "c.txt")


@pytest.mark.skipif(not WINDOWS, reason="Windows reparse points")
@pytest.mark.parametrize("tag, read", [(0xA000000C, None), (0xA0000003, None), (0x9000601A, b"A")])
def test_the_handle_itself_tells_a_link_from_another_reparse_point(tmp_path, monkeypatch, tag, read):
    """What the open handle says counts, not only lstat: a name surrogate (a symlink, a junction) is refused; another
    reparse point (here a cloud placeholder's tag) is opened again without FILE_FLAG_OPEN_REPARSE_POINT and read."""
    root, _ = _tree(tmp_path)
    api = nf._FS.api
    real = api.attribute_tag
    reopened = []
    real_open = api.open

    def attribute_tag(fd, rel):
        attributes, _tag = real(fd, rel)
        return (attributes | stat.FILE_ATTRIBUTE_REPARSE_POINT, tag) if rel == "a.txt" else (attributes, _tag)

    def open_(full, rel, **kw):
        if rel == "a.txt" and not kw["reparse"]:
            reopened.append(rel)
        return real_open(full, rel, **kw)
    monkeypatch.setattr(api, "attribute_tag", attribute_tag)
    monkeypatch.setattr(api, "open", open_)
    if read is None:
        with pytest.raises(Refused) as info:
            open_inside(root, ("a.txt",))
        assert info.value.reason == "link" and not reopened
    else:
        assert _read(open_inside(root, ("a.txt",))) == read and reopened == ["a.txt"]


# ---- exact names ------------------------------------------------------------------------------------------------
def test_a_name_counts_only_spelled_as_the_folder_holds_it(tmp_path):
    root, _ = _tree(tmp_path)
    nfc = unicodedata.normalize("NFC", "café.txt")
    _write(root / nfc, "cafe")
    assert _read(open_inside(root, (nfc,))) == b"cafe"
    for parts in (("A.TXT",), ("B", "x.txt"), (unicodedata.normalize("NFD", nfc),)):
        with pytest.raises(FileNotFoundError):
            open_inside(root, parts)
    with pytest.raises(FileNotFoundError):
        list_inside(root, ("B",))


@pytest.mark.skipif(not WINDOWS, reason="Windows short names")
def test_a_windows_short_name_names_nothing(tmp_path):
    import ctypes

    root, _ = _tree(tmp_path)
    _write(root / "Long Folder Name" / "inner.txt", "inner")
    buf = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(root / "Long Folder Name"), buf, 1024)
    short = buf.value.rpartition("\\")[2]
    if not short or short == "Long Folder Name":
        pytest.skip("the volume makes no short names")
    with pytest.raises(FileNotFoundError):
        open_inside(root, (short, "inner.txt"))
    assert _read(open_inside(root, ("Long Folder Name", "inner.txt"))) == b"inner"


# ---- hard links and special files -------------------------------------------------------------------------------
def test_a_file_with_another_name_is_never_read(tmp_path):
    root, outside = _tree(tmp_path)
    try:
        os.link(outside / "secret.txt", root / "innocent.txt")
        os.link(root / "a.txt", root / "a2.txt")
    except (OSError, NotImplementedError):
        pytest.skip("no hard links here")
    for name in ("innocent.txt", "a.txt", "a2.txt"):
        with pytest.raises(Refused) as info:
            open_inside(root, (name,))
        assert info.value.reason == "hard_link"
    assert [e.name for e in list_inside(root)] == ["b", "c.txt"]
    entries = _walk(root)
    assert {rel for kind, rel, _d in entries if kind == HARD_LINK} == {"a.txt", "a2.txt", "innocent.txt"}
    _no_secret(entries)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX")
def test_a_fifo_is_never_opened(tmp_path):
    root, _ = _tree(tmp_path)
    os.mkfifo(root / "pipe")
    with pytest.raises(Refused) as info:
        open_inside(root, ("pipe",))   # (an open for reading would wait for a writer)
    assert info.value.reason == "special"
    assert "pipe" not in [e.name for e in list_inside(root)]
    assert (SPECIAL, "pipe", None) in _walk(root)


# ---- list_inside ------------------------------------------------------------------------------------------------
def test_a_listing_gives_folders_and_files_in_name_order(tmp_path):
    root, outside = _tree(tmp_path)
    _write(root / "b" / "deeper" / "y.txt", "YY")
    _dir_link(outside, root / "b" / "j")
    entries = list_inside(root)
    assert [(e.kind, e.parts) for e in entries] == [(FILE, ("a.txt",)), (DIR, ("b",)), (FILE, ("c.txt",))]
    assert entries[0].size == 1 and entries[0].mtime_ns > 0 and entries[0].fd is None
    assert [(e.kind, e.name) for e in list_inside(root, ("b",))] == [(DIR, "deeper"), (FILE, "x.txt")]
    assert [(e.name, e.size) for e in list_inside(root, ["b", "deeper"])] == [("y.txt", 2)]


def test_a_folder_swapped_after_its_open_is_listed_through_what_was_opened(tmp_path, monkeypatch):
    """The folder is swapped for a link to outside after it was opened, before it is listed: its listing comes
    from the folder opened (POSIX: its descriptor; Windows: its handle), never from outside; on Windows a name below
    it then leads out of the root (Refused), on POSIX it is read from the folder held."""
    real = nf._FS.listing
    roots: list[tuple[Path, Path]] = []

    def listing(folder, rel):
        root, outside = roots[-1]
        if rel == "b" and not (root / "b.old").exists():
            _swap_for_link(root / "b", outside)
        return real(folder, rel)
    monkeypatch.setattr(nf._FS, "listing", listing)

    roots.append(_tree(tmp_path / "1"))
    if WINDOWS:
        with pytest.raises(Refused) as info:
            list_inside(roots[-1][0], ("b",))
        assert (info.value.reason, info.value.rel) == ("outside", "b/x.txt")
    else:
        assert [(e.kind, e.name, e.size) for e in list_inside(roots[-1][0], ("b",))] == [(FILE, "x.txt", 1)]

    roots.append(_tree(tmp_path / "2"))
    walk = walk_inside(roots[-1][0])
    first = next(walk)
    assert (first.kind, first.rel, first.reader().read()) == (FILE, "a.txt", b"A")
    second = next(walk)
    assert (second.kind, second.rel) == (DIR, "b")
    if WINDOWS:
        with pytest.raises(Refused) as info:
            next(walk)
        assert (info.value.reason, info.value.rel) == ("outside", "b/x.txt")
    else:
        rest = [(e.kind, e.rel, e.reader().read() if e.kind == FILE else None) for e in walk]
        assert rest == [(FILE, "b/x.txt", b"X"), (FILE, "c.txt", b"C")]


@pytest.mark.skipif(not WINDOWS, reason="the Windows listing through a handle")
def test_a_long_listing_is_read_in_several_parts(tmp_path, monkeypatch):
    """GetFileInformationByHandleEx gives as many entries as fit its buffer; the listing goes on until no more."""
    root = tmp_path / "root"
    names = sorted(f"{i:03d}-{'n' * 70}.txt" for i in range(40))
    for name in names:
        _write(root / name, "x")
    (root / f"sub-{'d' * 70}").mkdir()
    api = nf._FS.api
    real = api.information_ex
    calls = []

    def counted(handle, kind, buf, size):
        calls.append(kind)
        return real(handle, kind, buf, size)
    monkeypatch.setattr(api, "information_ex", counted)
    monkeypatch.setattr(nf, "_LIST_BUFFER", 512)   # (two such entries per call)
    assert [e.name for e in list_inside(root)] == names + [f"sub-{'d' * 70}"]
    assert calls.count(nf._FILE_FULL_DIRECTORY_INFO) >= 15
    assert [e.rel for e in walk_inside(root, open_files=False)] == names + [f"sub-{'d' * 70}"]


@pytest.mark.skipif(not WINDOWS, reason="the Windows listing through a handle")
def test_a_folder_without_even_dot_entries_lists_as_empty(tmp_path, monkeypatch):
    """The first query of a folder that has no entries at all (exFAT, some network shares: not even '.') fails with
    ERROR_FILE_NOT_FOUND: an empty folder, not a missing one."""
    root, _ = _tree(tmp_path)
    (root / "empty").mkdir()
    api = nf._FS.api
    real = api.information_ex

    def none_at_all(handle, kind, buf, size):
        if kind == nf._FILE_FULL_DIRECTORY_RESTART_INFO:
            api.ctypes.set_last_error(2)
            return 0
        return real(handle, kind, buf, size)
    monkeypatch.setattr(api, "information_ex", none_at_all)
    assert list_inside(root, ("empty",)) == []
    assert list(walk_inside(root, ("empty",))) == []


# ---- walk_inside ------------------------------------------------------------------------------------------------
def test_the_walk_gives_every_entry_in_name_order_with_its_file_open(tmp_path):
    root, _ = _tree(tmp_path)
    _write(root / "b" / "deeper" / "y.txt", "Y")
    _write(root / ".git" / "config", "[core]")
    _write(root / "b" / ".GIT" / "HEAD", "ref")
    _write(root / "b" / "node_modules" / "m.js", "m")
    (root / "empty").mkdir()
    asked = []

    def skip(parts: tuple[str, ...]) -> bool:
        asked.append(parts)
        return parts[-1].casefold() in (".git", "node_modules")

    assert _walk(root, skip=skip) == [
        (EXCLUDED, ".git", None), (FILE, "a.txt", b"A"), (DIR, "b", None), (EXCLUDED, "b/.GIT", None),
        (DIR, "b/deeper", None), (FILE, "b/deeper/y.txt", b"Y"), (EXCLUDED, "b/node_modules", None),
        (FILE, "b/x.txt", b"X"), (FILE, "c.txt", b"C"), (DIR, "empty", None)]
    assert ("b", ".GIT", "HEAD") not in asked and (".git", "config") not in asked
    assert _walk(root, ("b",), skip=skip)[:2] == [(EXCLUDED, ".GIT", None), (DIR, "deeper", None)]


def test_a_walk_entry_has_the_files_size_time_and_mode(tmp_path):
    root, _ = _tree(tmp_path)
    os.utime(root / "c.txt", ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    by_rel = {e.rel: (e.kind, e.size, e.mtime_ns, e.mode) for e in walk_inside(root)}
    assert by_rel["c.txt"][:3] == (FILE, 1, 1_600_000_000_000_000_000)
    assert stat.S_ISREG(by_rel["c.txt"][3]) and stat.S_ISDIR(by_rel["b"][3])


def test_a_walk_that_only_measures_gives_no_file_open(tmp_path):
    root, outside = _tree(tmp_path)
    os.link(root / "c.txt", root / "c2.txt")
    _dir_link(outside, root / "j")
    entries = [(e.kind, e.rel, e.size, e.fd) for e in walk_inside(root, open_files=False)]
    assert entries == [(FILE, "a.txt", 1, None), (DIR, "b", 0, None), (FILE, "b/x.txt", 1, None),
                       (HARD_LINK, "c.txt", 1, None), (HARD_LINK, "c2.txt", 1, None), (LINK, "j", 0, None)]


def test_a_folder_swapped_for_a_link_mid_walk_ends_the_walk(tmp_path):
    root, outside = _tree(tmp_path)
    walk = walk_inside(root)
    first = next(walk)
    assert first.rel == "a.txt"   # the root's listing is read: b is a folder in it
    _swap_for_link(root / "b", outside)
    with pytest.raises(Refused) as info:
        next(walk)
    assert (info.value.reason, info.value.rel) == ("link", "b")


def test_a_file_swapped_for_a_link_mid_walk_ends_the_walk(tmp_path):
    root, outside = _tree(tmp_path)
    walk = walk_inside(root, skip=lambda parts: parts == ("b",))
    assert [next(walk).rel, next(walk).rel] == ["a.txt", "b"]
    _swap_for_link(root / "c.txt", outside)   # (a junction on Windows: a file symlink needs a privilege)
    with pytest.raises(Refused) as info:
        next(walk)
    assert (info.value.reason, info.value.rel) == ("link", "c.txt")


def test_a_folder_above_swapped_mid_walk_never_leads_outside(tmp_path):
    """The folder being walked is moved away and a link put in its place: POSIX goes on in the folder it holds
    open (its own files, wherever it now lies); Windows sees the names below it lead elsewhere and ends the walk."""
    root, outside = _tree(tmp_path)
    walk = walk_inside(root)
    seen = [next(walk), next(walk)]
    assert [(e.kind, e.rel) for e in seen] == [(FILE, "a.txt"), (DIR, "b")]
    _swap_for_link(root / "b", outside)
    if WINDOWS:
        with pytest.raises(Refused) as info:
            next(walk)
        assert (info.value.reason, info.value.rel) == ("outside", "b/x.txt")
        return
    rest = []
    for entry in walk:
        rest.append((entry.kind, entry.rel, entry.reader().read() if entry.kind == FILE else None))
    assert rest == [(FILE, "b/x.txt", b"X"), (FILE, "c.txt", b"C")]


def _rest(walk) -> tuple[list[tuple[str, str, bytes | None]], dict[str, OSError]]:
    """(kind, rel, bytes of a FILE) of what is left of a walk, and the errors of its ERROR entries by rel."""
    rest, errors = [], {}
    for entry in walk:
        rest.append((entry.kind, entry.rel, entry.reader().read() if entry.kind == FILE else None))
        if entry.kind == ERROR:
            errors[entry.rel] = entry.error
    return rest, errors


def test_a_folder_renamed_inside_the_root_mid_walk_never_ends_the_walk(tmp_path):
    """A letter-case rename of the folder being walked (an agent's `git mv b B`) stays inside the root: POSIX reads
    on in the folder it holds; on Windows the names below it now have another final path inside the root, so they
    are gone (ERROR, FileNotFoundError), not Refused."""
    root, _ = _tree(tmp_path)
    walk = walk_inside(root)
    first = next(walk)
    assert (first.kind, first.rel, first.reader().read()) == (FILE, "a.txt", b"A")
    second = next(walk)
    assert (second.kind, second.rel) == (DIR, "b")
    (root / "b").rename(root / "b-tmp")
    (root / "b-tmp").rename(root / "B")
    rest, errors = _rest(walk)
    if WINDOWS:
        assert rest == [(ERROR, "b/x.txt", None), (FILE, "c.txt", b"C")]
        assert isinstance(errors["b/x.txt"], FileNotFoundError) and errors["b/x.txt"].filename == "b/x.txt"
    else:
        assert rest == [(FILE, "b/x.txt", b"X"), (FILE, "c.txt", b"C")]


@pytest.mark.skipif(not WINDOWS, reason="the Windows final path")
def test_a_file_moved_inside_the_root_while_it_is_opened_is_gone_not_refused(tmp_path, monkeypatch):
    """Moved to another folder of the root between its open and its final path: its final path is still below the
    root's, so it is not read under the old name (FileNotFoundError) and the walk goes on."""
    root, _ = _tree(tmp_path)
    (root / "d").mkdir()
    api = nf._FS.api
    real = api.final_path

    def moved(fd, rel):
        if rel == "b/x.txt" and (root / "b" / "x.txt").exists():
            os.replace(root / "b" / "x.txt", root / "d" / "x.txt")
        return real(fd, rel)
    monkeypatch.setattr(api, "final_path", moved)
    rest, errors = _rest(walk_inside(root))
    assert rest == [(FILE, "a.txt", b"A"), (DIR, "b", None), (ERROR, "b/x.txt", None), (FILE, "c.txt", b"C"),
                    (DIR, "d", None), (FILE, "d/x.txt", b"X")]
    assert isinstance(errors["b/x.txt"], FileNotFoundError)
    os.replace(root / "d" / "x.txt", root / "b" / "x.txt")
    with pytest.raises(FileNotFoundError):
        open_inside(root, ("b", "x.txt"))


def test_a_walk_enters_at_most_max_depth_folders(tmp_path, monkeypatch):
    """One descriptor is held per level: a folder deeper than MAX_DEPTH below the start folder is an ERROR entry,
    never entered."""
    root, _ = _tree(tmp_path)
    _write(root / "b" / "c" / "d" / "deep.txt", "D")
    monkeypatch.setattr(nf, "MAX_DEPTH", 2)
    rest, errors = _rest(walk_inside(root))
    assert [(kind, rel) for kind, rel, _data in rest] == [
        (FILE, "a.txt"), (DIR, "b"), (DIR, "b/c"), (ERROR, "b/c/d"), (FILE, "b/x.txt"), (FILE, "c.txt")]
    assert errors["b/c/d"].filename == "b/c/d" and "deep" in errors["b/c/d"].strerror
    assert [(kind, rel) for kind, rel, _data in _rest(walk_inside(root, ("b",)))[0]] == [
        (DIR, "c"), (DIR, "c/d"), (FILE, "c/d/deep.txt"), (FILE, "x.txt")]


def test_what_is_gone_or_unreadable_meanwhile_is_an_error_entry(tmp_path):
    root, _ = _tree(tmp_path)
    walk = walk_inside(root)
    assert next(walk).rel == "a.txt"
    (root / "c.txt").unlink()
    rest = list(walk)
    assert [(e.kind, e.rel) for e in rest] == [(DIR, "b"), (FILE, "b/x.txt"), (ERROR, "c.txt")]
    assert isinstance(rest[-1].error, FileNotFoundError) and rest[-1].error.filename == "c.txt"


@pytest.mark.skipif(WINDOWS or not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="POSIX permissions, which root ignores")
def test_a_folder_that_cannot_be_listed_is_an_error_entry(tmp_path):
    root, _ = _tree(tmp_path)
    os.chmod(root / "b", 0)
    try:
        entries = list(walk_inside(root))
    finally:
        os.chmod(root / "b", 0o755)
    assert [(e.kind, e.rel) for e in entries] == [(FILE, "a.txt"), (ERROR, "b"), (FILE, "c.txt")]
    assert isinstance(entries[1].error, PermissionError)


def _tracked(monkeypatch):
    """The descriptors nofollow opens (roots and names), and a function giving the ones closed: by nofollow itself
    or through the file object a walk made of one."""
    opened: list[int] = []
    closed: list[int] = []
    files: list[tuple[int, object]] = []
    fs = nf._FS
    real_root, real_child, real_close, real_reader = fs.root, fs.child, nf._close, nf._reader

    def root(path):
        folder = real_root(path)
        opened.append(folder.fd)
        return folder

    def child(*args, **kw):
        got = real_child(*args, **kw)
        opened.append(got[0])
        return got

    def close(fd):
        closed.append(fd)
        real_close(fd)

    def reader(fd):
        file = real_reader(fd)
        files.append((fd, file))
        return file

    monkeypatch.setattr(fs, "root", root)
    monkeypatch.setattr(fs, "child", child)
    monkeypatch.setattr(nf, "_close", close)
    monkeypatch.setattr(nf, "_reader", reader)
    return opened, lambda: Counter(closed) + Counter(fd for fd, file in files if file.closed)


@pytest.mark.parametrize("taken", [1, 3, 5, None])
def test_the_walk_closes_everything_it_opened_also_when_left_early(tmp_path, monkeypatch, taken):
    root, _ = _tree(tmp_path)
    _write(root / "b" / "deeper" / "y.txt", "Y")
    opened, closed = _tracked(monkeypatch)
    walk = walk_inside(root)
    if taken is None:
        list(walk)
    else:
        last = [next(walk) for _ in range(taken)][-1]
        if last.kind == FILE:
            os.fstat(last.fd)
            with last.reader() as f:   # (closing the walk's file before the walk does is harmless)
                f.read()
        walk.close()
    assert opened and Counter(opened) == closed()


def test_a_file_entry_cannot_be_read_once_the_walk_went_on(tmp_path):
    """The walk owns each file: once it went on, the entry's file is closed, and reading it is a ValueError, never
    the bytes of whatever file the hub opened since under the same descriptor number."""
    root, outside = _tree(tmp_path)
    walk = walk_inside(root)
    first = next(walk)
    assert first.kind == FILE and os.fstat(first.fd).st_size == 1 and first.reader() is first.reader()
    next(walk)
    other = os.open(outside / "secret.txt", os.O_RDONLY | getattr(os, "O_BINARY", 0))   # (likely the same number)
    try:
        for stale in (lambda: first.reader(), lambda: first.fd, lambda: first.file.read()):
            with pytest.raises(ValueError):
                stale()
    finally:
        os.close(other)
    walk.close()

    collected = list(walk_inside(root))   # (collected first, read later: a mistake that must read nothing)
    other = os.open(outside / "secret.txt", os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        for entry in (e for e in collected if e.kind == FILE):
            with pytest.raises(ValueError):
                entry.reader()
    finally:
        os.close(other)
    with pytest.raises(ValueError):
        next(e for e in list_inside(root) if e.kind == FILE).reader()


def test_a_file_closed_by_its_reader_never_closes_another_later(tmp_path):
    """A consumer that closes the walk's file itself (`with entry.reader() as f`): the walk's own close is then a
    no-op, so a descriptor the hub opened meanwhile under the same number stays open."""
    root, outside = _tree(tmp_path)
    walk = walk_inside(root)
    first = next(walk)
    with first.reader() as f:
        assert f.read() == b"A"
    other = os.open(outside / "secret.txt", os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        next(walk)
        assert os.read(other, 100) == SECRET
    finally:
        os.close(other)
    walk.close()


def test_the_module_needs_no_other_dreamteam_module():
    """Stdlib only (ctypes on Windows): it loads no other module of DreamTeam (a fresh, isolated interpreter with
    this run's sources first on its path)."""
    src = str(Path(nf.__file__).resolve().parent.parent)
    code = (
        "import sys\n"
        f"sys.path.insert(0, {src!r})\n"
        "import dreamteam.nofollow\n"
        f"assert dreamteam.nofollow.__file__.startswith({src!r}), dreamteam.nofollow.__file__\n"
        "loaded = sorted(m for m in sys.modules if m == 'dreamteam' or m.startswith('dreamteam.'))\n"
        "assert loaded == ['dreamteam', 'dreamteam.nofollow'], loaded\n"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_a_file_open_inside_can_still_be_renamed_and_deleted(tmp_path):
    """Windows: the handles share deleting, so an agent's rename or delete is never blocked by a download."""
    root, _ = _tree(tmp_path)
    fd = open_inside(root, ("b", "x.txt"))
    try:
        os.replace(root / "b" / "x.txt", root / "b" / "moved.txt")
        os.remove(root / "b" / "moved.txt")
        assert os.read(fd, 10) == b"X"
    finally:
        os.close(fd)
    walk = walk_inside(root)
    assert [(e.kind, e.rel) for e in (next(walk), next(walk))] == [(FILE, "a.txt"), (DIR, "b")]
    os.rename(root / "b", root / "b2")   # the folder the walk holds open
    walk.close()
