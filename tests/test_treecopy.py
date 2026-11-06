"""The shared tree copy (dreamteam.treecopy): byte-for-byte copies, excludes, links and the tree digest."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from dreamteam import treecopy as tc
from dreamteam.treecopy import EXPORT_EXCLUDE_NAMES, EXPORT_EXCLUDE_PATHS, copy_tree, scan_tree, tree_digest
from dreamteam.workspace import is_link, long_path


def _write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def _tree(root: Path) -> Path:
    _write(root / "a.txt", "alpha\r\n")                    # CRLF stays as it is
    _write(root / "src" / "main.py", "print('hi')\n")
    _write(root / "bin.dat", bytes(range(256)) * 10)
    _write(root / "Привет.md", "unicode name\n")
    _write(root / ".git" / "HEAD", "ref: refs/heads/main\n")
    _write(root / ".git" / "objects" / "ab" / "cdef", b"\x78\x9c packed")
    _write(root / ".scratch" / "alice" / "notes.md", "member scratch\n")
    _write(root / ".scratch" / "_skills" / "x" / "SKILL.md", "mirror\n")
    _write(root / "node_modules" / "pkg" / "index.js", "module\n")
    _write(root / "deep" / "node_modules" / "x.js", "nested module\n")
    _write(root / ".venv" / "pyvenv.cfg", "home = x\n")
    _write(root / "venv" / "lib.py", "x\n")
    _write(root / "src" / "__pycache__" / "main.cpython-311.pyc", b"\x00pyc")
    _write(root / ".unavailable-workspace" / "x", "stand-in\n")
    (root / "empty").mkdir()
    return root


def _files(root: Path) -> dict[str, bytes]:
    out = {}
    for path in root.rglob("*"):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


def test_copy_tree_copies_bytes_and_applies_the_export_excludes(tmp_path):
    src = _tree(tmp_path / "src")
    result = copy_tree(src, tmp_path / "out" / "copy")
    copied = _files(tmp_path / "out" / "copy")
    assert copied == {
        "a.txt": b"alpha\r\n", "src/main.py": b"print('hi')\n", "bin.dat": bytes(range(256)) * 10,
        "Привет.md": b"unicode name\n", ".git/HEAD": b"ref: refs/heads/main\n",
        ".git/objects/ab/cdef": b"\x78\x9c packed", ".scratch/alice/notes.md": b"member scratch\n",
    }
    assert (tmp_path / "out" / "copy" / "empty").is_dir()
    assert result.files == 7 and result.bytes == sum(len(v) for v in copied.values())
    assert sorted(result.excluded) == sorted([".scratch/_skills", ".unavailable-workspace", ".venv",
                                              "deep/node_modules", "node_modules", "src/__pycache__", "venv"])
    assert result.links == [] and result.errors == [] and result.special == []
    # the digest is the scan's digest of the source, and the copy's own
    assert result.digest == scan_tree(src).digest == scan_tree(tmp_path / "out" / "copy").digest
    assert EXPORT_EXCLUDE_NAMES == {"node_modules", ".venv", "venv", "__pycache__"}
    assert EXPORT_EXCLUDE_PATHS == {".scratch/_skills", ".unavailable-workspace"}


def test_tree_digest_is_stable_and_content_based(tmp_path):
    entries = [("b/c.txt", 3, "aa"), ("a.txt", 1, "bb")]
    assert tree_digest(entries) == tree_digest(list(reversed(entries)))
    expected = hashlib.sha256("a.txt\x001\x00bb\nb/c.txt\x003\x00aa\n".encode()).hexdigest()
    assert tree_digest(entries) == expected
    assert tree_digest([]) == hashlib.sha256(b"").hexdigest()
    # NFC and NFD spellings of one name give one digest (macOS may hand out either)
    assert tree_digest([("é.txt", 1, "x")]) == tree_digest([("é.txt", 1, "x")])
    src = _tree(tmp_path / "one")
    before = scan_tree(src).digest
    (src / "a.txt").write_bytes(b"alpha\n")
    assert scan_tree(src).digest != before
    (src / "a.txt").write_bytes(b"alpha\r\n")
    assert scan_tree(src).digest == before
    (src / "node_modules" / "pkg" / "index.js").write_text("changed")   # excluded: not in the digest
    assert scan_tree(src).digest == before
    os.utime(src / "a.txt", (1, 1))                                     # a time is no content
    assert scan_tree(src).digest == before


def test_scan_tree_without_hashing_counts_and_copies_nothing(tmp_path):
    src = _tree(tmp_path / "src")
    quick = scan_tree(src, hash=False)
    full = scan_tree(src)
    assert quick.digest is None and full.digest is not None
    assert (quick.files, quick.bytes) == (full.files, full.bytes) == (7, full.bytes)
    assert sorted(os.listdir(tmp_path)) == ["src"]
    nothing = scan_tree(src, exclude_names=(), exclude_paths=())
    assert nothing.files == 14 and nothing.excluded == []


def test_copy_tree_keeps_modes_and_times_and_read_only_files(tmp_path):
    src = tmp_path / "src"
    script = _write(src / "run.sh", "#!/bin/sh\necho hi\n")
    obj = _write(src / ".git" / "objects" / "12" / "3456", b"object")
    os.chmod(script, 0o755)
    os.chmod(obj, 0o444)
    os.utime(script, ns=(1_600_000_000_000_000_000, 1_600_000_000_123_456_789))
    out = tmp_path / "out"
    copy_tree(src, out)
    assert os.stat(out / "run.sh").st_mtime_ns // 1_000_000 == 1_600_000_000_123
    assert not os.access(out / ".git" / "objects" / "12" / "3456", os.W_OK)
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(out / "run.sh").st_mode) == 0o755
    os.chmod(obj, 0o644)
    os.chmod(out / ".git" / "objects" / "12" / "3456", 0o644)


def _symlink(target: Path, link: Path, directory: bool) -> bool:
    try:
        os.symlink(target, link, target_is_directory=directory)
        return True
    except (OSError, NotImplementedError):
        return False


def _junction(target: Path, link: Path) -> bool:
    if sys.platform != "win32":
        return False
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    return proc.returncode == 0


def test_links_are_never_followed(tmp_path):
    outside = tmp_path / "outside"
    _write(outside / "secret.txt", "outside the tree\n")
    src = tmp_path / "src"
    _write(src / "kept.txt", "kept\n")
    made = []
    if _symlink(outside, src / "dir-link", True):
        made.append("dir-link")
    if _symlink(outside / "secret.txt", src / "file-link.txt", False):
        made.append("file-link.txt")
    if _junction(outside, src / "junction"):
        made.append("junction")
    if not made:
        pytest.skip("this OS lets this account make no links")
    result = copy_tree(src, tmp_path / "out")
    assert sorted(result.links) == sorted(made)
    assert _files(tmp_path / "out") == {"kept.txt": b"kept\n"}
    assert scan_tree(src).links == sorted(made) and scan_tree(src).files == 1


def test_copy_tree_refuses_bad_destinations_and_sources(tmp_path):
    src = _tree(tmp_path / "src")
    with pytest.raises(ValueError):
        copy_tree(src, src / "inside")
    with pytest.raises(ValueError):
        copy_tree(src, src)
    busy = _write(tmp_path / "busy" / "x.txt", "x").parent
    with pytest.raises(FileExistsError):
        copy_tree(src, busy)
    with pytest.raises(FileExistsError):
        copy_tree(src, busy / "x.txt")
    with pytest.raises(NotADirectoryError):
        copy_tree(tmp_path / "missing", tmp_path / "out")
    with pytest.raises(NotADirectoryError):
        scan_tree(busy / "x.txt")
    (tmp_path / "empty").mkdir()
    assert copy_tree(src, tmp_path / "empty").files == 7   # an existing empty folder is used


def test_a_file_that_cannot_be_read_is_listed_and_left_out(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _write(src / "ok.txt", "ok\n")
    _write(src / "gone.txt", "gone\n")
    import dreamteam.treecopy as tc

    real_open = os.open

    def flaky_open(path, flags, *args):
        if str(path).endswith("gone.txt") and not flags & os.O_WRONLY:
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *args)

    monkeypatch.setattr(tc.os, "open", flaky_open)
    result = copy_tree(src, tmp_path / "out")
    assert result.errors == [("gone.txt", "Permission denied")]
    assert _files(tmp_path / "out") == {"ok.txt": b"ok\n"}
    assert result.files == 1 and result.vanished == []


def test_files_deleted_during_the_walk_are_vanished_folders_only_errors(tmp_path):
    src = tmp_path / "src"
    for rel in ("a.txt", "b/x.txt", "c/y.txt", "d.txt"):
        _write(src / rel, rel)
    calls = []

    def check():   # after the root was listed (before a.txt): d.txt and the folder c go
        calls.append(1)
        if len(calls) == 1:
            (src / "d.txt").unlink()
            (src / "c" / "y.txt").unlink()
            (src / "c").rmdir()

    result = copy_tree(src, tmp_path / "out", check=check)
    assert _files(tmp_path / "out") == {"a.txt": b"a.txt", "b/x.txt": b"b/x.txt"}
    assert result.vanished == ["d.txt"] and sorted(r for r, _ in result.errors) == ["c", "d.txt"]
    assert ("c", tc.GONE_DIR) in result.errors   # a whole subtree gone is no deleted temporary file


def test_skip_files_are_left_out_without_a_word(tmp_path):
    src = tmp_path / "src"
    for rel in ("a.txt", ".DS_Store", "sub/.DS_Store", "sub/Thumbs.db", "sub/b.txt"):
        _write(src / rel, "x")
    for walk in (lambda: copy_tree(src, tmp_path / "out", skip_files=tc.COPY_SKIP_FILES),
                 lambda: scan_tree(src, skip_files=tc.COPY_SKIP_FILES)):
        result = walk()
        assert (result.files, result.skipped, result.excluded) == (2, 3, [])
    assert _files(tmp_path / "out") == {"a.txt": b"x", "sub/b.txt": b"x"}
    assert copy_tree(src, tmp_path / "all").files == 5   # (only when asked)


@pytest.mark.skipif(not tc._FD_WALK, reason="the walk through folder descriptors is POSIX")
def test_a_folder_swapped_for_a_link_during_the_walk_is_not_walked(tmp_path):
    secret = _write(tmp_path / "secret" / "id_rsa", "PRIVATE KEY").parent
    src = tmp_path / "src"
    for rel in ("a.txt", "b/x.txt", "c.txt"):
        _write(src / rel, rel)
    calls = []

    def check():   # at c.txt (a.txt took two calls): b's entry was read, b becomes a link before it is listed
        calls.append(1)
        if len(calls) == 3:
            (src / "b").rename(tmp_path / "b-old")
            os.symlink(secret, src / "b", target_is_directory=True)

    result = copy_tree(src, tmp_path / "out", check=check)
    assert not (tmp_path / "out" / "b" / "id_rsa").exists()
    assert ("b", tc._REPLACED_DIR) in result.errors and result.files == 2
    # a folder replaced by another folder (not the one the parent listed) is not walked either
    os.unlink(src / "b")
    (src / "b").mkdir()
    calls.clear()

    def swap() -> None:
        calls.append(1)
        if len(calls) == 3:
            os.rename(src / "b", tmp_path / "b-other")
            os.rename(secret, src / "b")

    result = scan_tree(src, hash=True, check=swap)
    assert ("b", tc._REPLACED_DIR) in result.errors and result.files == 2


def test_a_folder_swapped_for_a_link_is_not_walked_by_the_walk_by_path(tmp_path, monkeypatch):
    """The walk without folder descriptors (Windows; forced here elsewhere): a folder that is a link (a junction on
    Windows, which needs no privilege) by the time it is listed is refused before it is listed."""
    monkeypatch.setattr(tc, "_FD_WALK", False)
    secret = _write(tmp_path / "secret" / "id_rsa", "PRIVATE KEY").parent
    src = tmp_path / "src"
    for rel in ("a.txt", "b/x.txt", "c.txt"):
        _write(src / rel, rel)
    calls = []

    def check():   # at c.txt (a.txt took two calls): b's entry was read, b becomes a link before it is listed
        calls.append(1)
        if len(calls) == 3:
            (src / "b").rename(tmp_path / "b-old")
            if sys.platform == "win32":
                import _winapi
                _winapi.CreateJunction(str(secret), str(src / "b"))
            else:
                os.symlink(secret, src / "b", target_is_directory=True)

    result = copy_tree(src, tmp_path / "out", check=check)
    assert len(calls) >= 3 and is_link(src / "b")
    assert not (tmp_path / "out" / "b" / "id_rsa").exists()
    assert ("b", tc._REPLACED_DIR) in result.errors and result.files == 2


def test_deep_paths_beyond_260_characters(tmp_path):
    src = tmp_path / "src"
    deep = src.joinpath(*(["d" * 40] * 8))
    os.makedirs(long_path(deep))
    with open(long_path(deep / "file.txt"), "wb") as f:
        f.write(b"deep")
    result = copy_tree(src, tmp_path / "out")
    assert result.files == 1 and result.errors == []
    with open(long_path(tmp_path.joinpath("out", *(["d" * 40] * 8), "file.txt")), "rb") as f:
        assert f.read() == b"deep"
    assert scan_tree(tmp_path / "out").digest == result.digest


def test_an_import_takes_no_permission_bits_from_the_export_medium(tmp_path):
    src = tmp_path / "medium"
    src.mkdir()
    (src / "notes.txt").write_bytes(b"notes\n")
    (src / "run.sh").write_bytes(b"#!/bin/sh\n")
    os.chmod(src / "notes.txt", stat.S_IREAD)   # a read-only medium reports every file read-only
    try:
        result = copy_tree(src, tmp_path / "out", modes="import", apply_modes={"run.sh": 0o755, "bad": 0o4777})
    finally:
        os.chmod(src / "notes.txt", stat.S_IREAD | stat.S_IWRITE)
    with open(tmp_path / "out" / "notes.txt", "ab") as f:   # writable: the medium's bits are not taken
        f.write(b"more\n")
    assert result.files == 2
    if os.name != "nt":
        assert os.stat(tmp_path / "out" / "run.sh").st_mode & 0o777 == 0o755


def test_an_export_records_the_bits_and_survives_a_file_system_without_them(tmp_path, monkeypatch):
    src = tmp_path / "ws"
    src.mkdir()
    (src / "object").write_bytes(b"git object\n")
    (src / "plain.txt").write_bytes(b"plain\n")
    os.chmod(src / "object", stat.S_IREAD)
    if os.name != "nt":
        (src / "tool").write_bytes(b"#!/bin/sh\n")
        os.chmod(src / "tool", 0o755)

    def refusing(path, mode, *args, **kw):   # FAT / exFAT on Linux: EPERM
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(tc.os, "chmod", refusing)
    try:
        result = copy_tree(src, tmp_path / "out", modes="export")
    finally:
        monkeypatch.undo()
        os.chmod(src / "object", stat.S_IREAD | stat.S_IWRITE)
    assert result.files == (3 if os.name != "nt" else 2)
    # S_IREAD: read-only on Windows (recorded as 0o444), owner read only (0o400) on POSIX
    assert result.modes["object"] == (0o444 if os.name == "nt" else 0o400) and "plain.txt" not in result.modes
    if os.name != "nt":
        assert result.modes["tool"] == 0o755


def test_volatile_files_do_not_count_in_the_comparison_digest():
    from dreamteam.treecopy import compare_digest, volatile

    for rel in (".git/index", ".git/logs/HEAD", ".git/index.lock", "sub/.git/FETCH_HEAD", "data/app.db-wal",
                ".git/modules/x/index"):
        assert volatile(rel), rel
    for rel in (".git/HEAD", ".git/refs/heads/index", ".git/objects/ab/cdef", "index", "logs/app.log"):
        assert not volatile(rel), rel
    base = [("a.txt", 1, "x"), (".git/HEAD", 2, "y")]
    assert compare_digest(base + [(".git/index", 3, "z")]) == compare_digest(base)
    assert compare_digest(base + [(".git/refs/heads/main", 3, "z")]) != compare_digest(base)
    # a database's side files only next to the database: a file merely named like one is work
    assert volatile("data/app.db-wal", {"data/app.db", "data/app.db-wal"})
    assert not volatile("notes/dev-journal", {"notes/dev-journal"})
    assert compare_digest(base + [("app.db", 4, "d"), ("app.db-wal", 5, "w")]) == compare_digest(
        base + [("app.db", 4, "d")])
    assert compare_digest(base + [("notes/dev-journal", 5, "j")]) != compare_digest(base)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_owner_only_files_stay_owner_only_and_nothing_becomes_group_writable(tmp_path):
    src = tmp_path / "src"
    for name, mode in (("deploy_key", 0o600), ("plain.txt", 0o644), ("shared.sh", 0o775), ("notes", 0o640)):
        os.chmod(_write(src / name, name), mode)
    exported = copy_tree(src, tmp_path / "out", modes="export")
    assert exported.modes == {"deploy_key": 0o600, "shared.sh": 0o775, "notes": 0o640}
    copy_tree(tmp_path / "out", tmp_path / "in", modes="import", apply_modes=exported.modes)
    modes = {name: stat.S_IMODE(os.stat(tmp_path / "in" / name).st_mode) for name in ("deploy_key", "shared.sh",
                                                                                       "notes")}
    assert modes == {"deploy_key": 0o600, "shared.sh": 0o755, "notes": 0o640}


def test_a_failed_write_names_the_file_and_its_size(tmp_path, monkeypatch):
    import errno
    import io

    _write(tmp_path / "src" / "big.bin", b"x" * 1000)
    real = tc.os.fdopen

    def fdopen(fd, mode="r", *args, **kw):
        handle = real(fd, mode, *args, **kw)
        if "w" not in mode:
            return handle

        class Full(io.RawIOBase):
            def write(self, data):
                raise OSError(errno.ENOSPC, "No space left on device")   # as a write reports it: no file name

            def close(self):
                handle.close()
                super().close()

        return Full()

    monkeypatch.setattr(tc.os, "fdopen", fdopen)
    with pytest.raises(OSError) as err:
        copy_tree(tmp_path / "src", tmp_path / "out")
    assert err.value.errno == errno.ENOSPC and err.value.filename.endswith("big.bin")
    assert err.value.src_size == 1000


def test_is_within_sees_another_spelling_of_the_same_folder(tmp_path, monkeypatch):
    from dreamteam.treecopy import is_within

    (tmp_path / "Parent" / "child").mkdir(parents=True)
    assert is_within(tmp_path / "Parent" / "child" / "new", tmp_path / "Parent")
    assert not is_within(tmp_path / "Other", tmp_path / "Parent")
    if Path(str(tmp_path / "Parent").swapcase()).exists():   # a file system that ignores case
        monkeypatch.setattr(os.path, "normcase", lambda s: s)
        assert is_within(Path(str(tmp_path / "Parent").swapcase()) / "child", tmp_path / "Parent")


def test_is_within_compares_as_text_where_the_file_system_has_no_file_ids(tmp_path, monkeypatch):
    from dreamteam.treecopy import is_within

    (tmp_path / "Parent").mkdir()
    (tmp_path / "Other").mkdir()
    real = os.stat

    def no_ids(path, *args, **kw):   # every file id 0, as some network file systems report
        s = real(path, *args, **kw)
        return os.stat_result((s.st_mode, 0, s.st_dev, s.st_nlink, s.st_uid, s.st_gid, s.st_size, int(s.st_atime),
                               int(s.st_mtime), int(s.st_ctime)))

    monkeypatch.setattr(os, "stat", no_ids)
    assert not is_within(tmp_path / "Other", tmp_path / "Parent")
    assert is_within(tmp_path / "Parent" / "x", tmp_path / "Parent")


# ---- a copy into a new session (copyfrom.py) ------------------------------------------------------------
def test_copy_exclude_names_fold_case(tmp_path):
    src = tmp_path / "src"
    _write(src / "kept.txt", "kept\n")
    _write(src / ".GIT" / "HEAD", "ref\n")
    _write(src / "Node_Modules" / "x.js", "module\n")
    _write(src / "x" / ".venv" / "pyvenv.cfg", "home\n")
    _write(src / "sub" / ".git", "gitdir: ../.git/modules/sub\n")   # a submodule's .git file
    _write(src / "sub" / "code.py", "x = 1\n")
    result = copy_tree(src, tmp_path / "out", exclude_names=tc.COPY_EXCLUDE_NAMES, fold_names=True)
    assert _files(tmp_path / "out") == {"kept.txt": b"kept\n", "sub/code.py": b"x = 1\n"}
    assert sorted(result.excluded) == [".GIT", "Node_Modules", "sub/.git", "x/.venv"]
    assert (tmp_path / "out" / "x").is_dir()
    assert ".git" in tc.COPY_EXCLUDE_NAMES and ".git" not in EXPORT_EXCLUDE_NAMES
    # the defaults (an export) are unchanged: exact names, .git kept
    default = copy_tree(src, tmp_path / "export")
    assert "Node_Modules/x.js" in _files(tmp_path / "export") and ".GIT/HEAD" in _files(tmp_path / "export")
    assert default.excluded == ["x/.venv"]


def test_limits_files_and_bytes(tmp_path):
    src = tmp_path / "src"
    for name in ("a.txt", "b.txt", "c.txt"):
        _write(src / name, "12345")
    for i in range(20):
        _write(src / "node_modules" / f"m{i}.js", "x" * 100)
    names = tc.COPY_EXCLUDE_NAMES
    with pytest.raises(tc.TreeLimit) as err:
        copy_tree(src, tmp_path / "o1", exclude_names=names, max_files=2)
    assert (err.value.what, err.value.limit) == ("files", 2)
    assert copy_tree(src, tmp_path / "o2", exclude_names=names, max_files=3, max_bytes=15).files == 3   # exactly
    with pytest.raises(tc.TreeLimit) as err:
        copy_tree(src, tmp_path / "o3", exclude_names=names, max_bytes=14)
    assert (err.value.what, err.value.limit) == ("bytes", 14)
    for kw in ({"max_files": 2}, {"max_bytes": 14}):
        for hashed in (False, True):
            with pytest.raises(tc.TreeLimit) as err:
                scan_tree(src, exclude_names=names, hash=hashed, **kw)
            assert err.value.what == ("files" if "max_files" in kw else "bytes")
    found = scan_tree(src, exclude_names=names, hash=False, max_files=3, max_bytes=15)
    assert (found.files, found.bytes) == (3, 15)


def test_a_file_that_grows_while_it_is_read_passes_the_byte_limit(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _write(src / "log.txt", "x" * 10)
    real_scandir = os.scandir

    class Grown:   # the directory entry reports 10 bytes; 100 are read
        def __init__(self, entry):
            self._entry = entry

        def __getattr__(self, name):
            return getattr(self._entry, name)

        def stat(self, **kw):
            st = self._entry.stat(**kw)
            _write(src / self._entry.name, "x" * 100)   # (a listing through a descriptor has no full path)
            return st

    class Scan:
        def __init__(self, path):
            self._it = real_scandir(path)

        def __enter__(self):
            return [Grown(e) for e in self._it]

        def __exit__(self, *exc):
            self._it.close()

    monkeypatch.setattr(tc.os, "scandir", Scan)
    with pytest.raises(tc.TreeLimit) as err:
        copy_tree(src, tmp_path / "out", max_bytes=50)
    assert err.value.what == "bytes"


def test_check_stops_the_copy(tmp_path):
    src = tmp_path / "src"
    for name in ("a.txt", "b.txt", "c.txt"):
        _write(src / name, name)
    calls = []

    class Stop(Exception):
        pass

    def check():   # before a.txt, after its chunk, before b.txt: stop there
        calls.append(1)
        if len(calls) == 3:
            raise Stop()

    def stop():
        raise Stop()

    with pytest.raises(Stop):
        copy_tree(src, tmp_path / "out", check=check)
    assert (tmp_path / "out").is_dir() and _files(tmp_path / "out") == {"a.txt": b"a.txt"}   # the caller removes it
    with pytest.raises(Stop):
        scan_tree(src, hash=False, check=stop)
    with pytest.raises(Stop):
        scan_tree(src, check=stop)


def test_modes_writable(tmp_path):
    src = tmp_path / "src"
    ro = _write(src / "ro.txt", "read only\n")
    run = _write(src / "run.sh", "#!/bin/sh\n")
    os.chmod(ro, stat.S_IREAD)
    os.chmod(run, 0o755)
    special = {}
    # WSL shows the files of /mnt/c as 0o777: only scripts and programs stay executable
    for name, data in (("wide.sh", "#!/bin/sh\n"), ("wide.txt", "notes\n"), ("wide.bin", b"\x7fELF\x02"),
                       ("wide.empty", b"")):
        os.chmod(_write(src / name, data), 0o777)
    write_only = None
    if os.name != "nt" and os.geteuid() == 0:   # only root reads a write-only file (the sandbox hub)
        write_only = _write(src / "write_only.txt", "w\n")
        os.chmod(write_only, 0o200)
    if os.name != "nt":
        for name, bits in (("suid", 0o4755), ("sgid", 0o2755)):
            path = _write(src / name, "x\n")
            try:
                os.chmod(path, bits)
            except OSError:
                continue
            if stat.S_IMODE(os.stat(path).st_mode) == bits:   # some systems drop the bit for a user
                special[name] = path
    try:
        with pytest.raises(ValueError):
            copy_tree(src, tmp_path / "bad", modes="other")
        copy_tree(src, tmp_path / "out", modes="writable")
        assert os.access(tmp_path / "out" / "ro.txt", os.W_OK)
        assert (tmp_path / "out" / "ro.txt").read_text(encoding="utf-8") == "read only\n"
        if os.name != "nt":
            assert stat.S_IMODE(os.stat(tmp_path / "out" / "ro.txt").st_mode) == 0o600
            assert stat.S_IMODE(os.stat(tmp_path / "out" / "run.sh").st_mode) == 0o755
            wide = {name: stat.S_IMODE(os.stat(tmp_path / "out" / name).st_mode)
                    for name in ("wide.sh", "wide.txt", "wide.bin", "wide.empty")}
            assert wide == {"wide.sh": 0o755, "wide.txt": 0o644, "wide.bin": 0o755, "wide.empty": 0o644}
            if write_only is not None:
                assert stat.S_IMODE(os.stat(tmp_path / "out" / "write_only.txt").st_mode) == 0o600
            for name in special:
                mode = stat.S_IMODE(os.stat(tmp_path / "out" / name).st_mode)
                assert mode & 0o7000 == 0 and mode == 0o755, (name, oct(mode))
    finally:
        os.chmod(ro, stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_modes_writable_ignores_the_bits_a_file_system_without_them_reports(tmp_path, monkeypatch):
    """A read-only Windows file on drvfs / 9p shows 0o555, every file of a CIFS share 0o755: only programs stay
    executable there; on a file system with POSIX bits a 0o755 file stays executable."""
    src = tmp_path / "src"
    files = {"ro.txt": ("notes\n", 0o555), "share.txt": ("notes\n", 0o755), "run.sh": ("#!/bin/sh\n", 0o555),
             "macho": (b"\xcf\xfa\xed\xfe\x07", 0o755)}
    for name, (data, bits) in files.items():
        os.chmod(_write(src / name, data), bits)
    copy_tree(src, tmp_path / "posix", modes="writable")
    assert stat.S_IMODE(os.stat(tmp_path / "posix" / "share.txt").st_mode) == 0o755
    monkeypatch.setattr(tc, "fs_type", lambda path: "cifs")
    copy_tree(src, tmp_path / "out", modes="writable")
    got = {name: stat.S_IMODE(os.stat(tmp_path / "out" / name).st_mode) for name in files}
    assert got == {"ro.txt": 0o644, "share.txt": 0o644, "run.sh": 0o755, "macho": 0o755}


@pytest.mark.skipif(os.name == "nt", reason="POSIX paths (Linux's mountinfo)")
def test_fs_type_reads_the_longest_mount_point():
    info = ("22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
            "40 22 0:50 / /mnt/c rw,noatime - 9p drvfs rw\n"
            "41 22 0:51 / /mnt/my\\040nas rw - cifs //nas/share rw\n"
            "42 22 0:52 / /mnt/cx rw - vfat /dev/sdb1 rw\n")
    assert tc.fs_type("/mnt/c/Users/me/proj", info) == "9p"
    assert tc.fs_type("/mnt/my nas/proj", info) == "cifs"
    assert tc.fs_type("/mnt/cxy", info) == "ext4"
    assert tc.fs_type("/home/me", info) == "ext4"
