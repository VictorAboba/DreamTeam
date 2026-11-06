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
from dreamteam.workspace import long_path


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
    assert result.files == 1


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
