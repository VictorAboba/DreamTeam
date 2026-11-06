"""Copying a folder or a zip into a new session (copyfrom.py; docs/SPEC.md §17.2 "Copying files into a new
session"): the source checks, the copy and its limits, the result texts, the uploads store and the settings."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import os
import secrets
import stat
import sys
import time
import tomllib
import zipfile
from pathlib import Path

import pytest

from dreamteam import copyfrom, hubauth
from dreamteam import treecopy as tc
from dreamteam.config import Config, WorkspaceSettings, render_template
from dreamteam.copyfrom import (UPLOAD_TOKEN, CopyCancelled, CopyError, CopyLimits, Source, Uploads, check_target,
                                copy_into, copy_text, resolve_source, size_text)
from dreamteam.settings import public_view
from dreamteam.treecopy import scan_tree
from dreamteam.workspace import pid_alive

LIMITS = CopyLimits(max_mb=64, max_files=1000)


def _write(path: Path, data: bytes | str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def _files(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _project(root: Path) -> Path:
    _write(root / "a.txt", "alpha\r\n")
    _write(root / "src" / "main.py", "print('hi')\n")
    _write(root / ".gitignore", "*.log\n")
    _write(root / ".git" / "HEAD", "ref: refs/heads/main\n")
    _write(root / "node_modules" / "pkg" / "index.js", "module\n")
    _write(root / "web" / "node_modules" / "x.js", "nested\n")
    _write(root / ".venv" / "pyvenv.cfg", "home\n")
    _write(root / "venv" / "lib.py", "x\n")
    _write(root / "src" / "__pycache__" / "main.pyc", b"\0pyc")
    return root


def _zip(members: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _config(tmp_path: Path) -> Config:
    return Config.from_dict(tmp_path / "cfg", {})


def _resolve(text, tmp_path: Path, *, sandbox: bool = False, home: Path | None = None):
    return resolve_source(text, home=home or tmp_path / "home", config=_config(tmp_path), sandbox=sandbox)


def _dir_link(target: Path, link: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        import _winapi

        try:
            _winapi.CreateJunction(str(target), str(link))
            return True
        except OSError:
            pass
    return False


def _dead_pid() -> int:
    pid = 2 ** 22 + 12345
    assert not pid_alive(pid)
    return pid


# ---- the source -------------------------------------------------------------------------------------------------
def test_resolve_source_refusals(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "sessions").mkdir(parents=True)
    _write(tmp_path / "notes.txt", "x")
    _write(home / "in.zip", _zip({"a": "1"}))
    cases = [
        (5, "must be an absolute path"),
        ("  ", "must be an absolute path"),
        ("relative/dir", "must be absolute"),
        (str(tmp_path / "missing"), "Nothing to copy"),
        (str(tmp_path / "notes.txt"), "not a .zip archive"),
        (tmp_path.anchor, "root of a drive"),
        (str(home), "DreamTeam home"),
        (str(home / "sessions"), "DreamTeam home"),
        (str(home / "in.zip"), "DreamTeam home"),
        (str(tmp_path), "DreamTeam home"),   # contains it
    ]
    for text, words in cases:
        with pytest.raises(CopyError, match=words) as err:
            _resolve(text, tmp_path)
        assert err.value.status == 400, text

    user = tmp_path / "users" / "me"
    user.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
    for text, words in ((user, "your home folder itself"), (tmp_path / "users", "contains your home folder")):
        with pytest.raises(CopyError, match=words):
            _resolve(str(text), tmp_path)
    (user / "proj").mkdir()
    assert _resolve(str(user / "proj"), tmp_path).kind == "folder"   # a folder inside the user's home is fine


def test_dreamteams_own_folders_are_refused(tmp_path, monkeypatch):
    # the hub tokens in a ~/.dreamteam of their own: that folder (it contains them) is refused too
    monkeypatch.setenv(hubauth.TOKENS_DIR_ENV, str(tmp_path / "dot-dreamteam" / "hub-tokens"))
    config = _config(tmp_path)
    folders = [config.library_dir, config.skills_library_dir / "a-skill", config.skill_envs_dir,
               hubauth.tokens_dir(), tmp_path / "dot-dreamteam"]
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
        with pytest.raises(CopyError, match="DreamTeam's own") as err:
            resolve_source(str(folder), home=tmp_path / "home", config=config, sandbox=False)
        assert err.value.status == 400


@pytest.mark.skipif(os.name == "nt", reason="POSIX system folders")
def test_system_folders_are_refused(tmp_path):
    for root in ("/proc", "/proc/self", "/dev", "/sys"):
        if not os.path.isdir(root):
            continue
        with pytest.raises(CopyError, match="system folder"):
            _resolve(root, tmp_path)


def test_resolve_source_accepts_a_folder_and_a_zip(tmp_path):
    src = _project(tmp_path / "src")
    zip_path = _write(tmp_path / "W.ZIP", _zip({"a": "1"}))
    folder = _resolve(f"  {src}  ", tmp_path)
    assert (folder.kind, folder.path, folder.shown) == ("folder", src.resolve(), str(src.resolve()))
    found = _resolve(str(zip_path), tmp_path)
    assert (found.kind, found.path) == ("zip", zip_path.resolve())
    # one pair of quotes around the path (Explorer's "Copy as path") is dropped
    for quoted in (f'"{src}"', f"  '{src}' ", f'"{zip_path}"'):
        assert _resolve(quoted, tmp_path).path in (src.resolve(), zip_path.resolve()), quoted
    for empty in ('""', "' '"):
        with pytest.raises(CopyError, match="must be an absolute path"):
            _resolve(empty, tmp_path)


def test_disk_mount_points_are_refused(tmp_path, monkeypatch):
    """A whole disk mounted in /mnt, /media, /run/media or /Volumes counts as a drive's root; a folder mounted
    elsewhere (a devcontainer's /workspaces/x) does not."""
    from pathlib import PurePosixPath

    mounts = {"/mnt/c", "/media/usb", "/media/me/usb", "/run/media/me/usb", "/Volumes/Backup", "/workspaces/x"}
    monkeypatch.setattr(copyfrom.os.path, "ismount", lambda p: Path(p).as_posix() in mounts)
    roots: dict[str, str] = {}   # Linux's mountinfo root field ("/": a whole file system); none: not known
    monkeypatch.setattr(copyfrom, "_mount_root", lambda p: roots.get(Path(p).as_posix()))
    for path in ("/mnt/c", "/media/usb", "/media/me/usb", "/run/media/me/usb", "/Volumes/Backup"):
        assert copyfrom._disk_mount(Path(PurePosixPath(path))), path
    for path in ("/workspaces/x", "/mnt/c/proj", "/home/me/proj"):
        assert not copyfrom._disk_mount(Path(PurePosixPath(path))), path
    # a bind mount of one folder (docker compose `- ./my-app:/mnt/app:ro`) is no disk; a whole one is
    roots.update({"/mnt/c": "/", "/media/usb": "/home/me/my-app"})
    assert copyfrom._disk_mount(Path(PurePosixPath("/mnt/c")))
    assert not copyfrom._disk_mount(Path(PurePosixPath("/media/usb")))
    if os.name == "nt":   # (drive roots are refused by the parent check)
        return
    disk = tmp_path / "disks" / "usb"
    disk.mkdir(parents=True)
    disk = disk.resolve()
    monkeypatch.setattr(copyfrom, "_MOUNT_PARENTS", (str(disk.parent),))
    monkeypatch.setattr(copyfrom.os.path, "ismount", lambda p: str(p) == str(disk))
    with pytest.raises(CopyError, match="root of a drive"):
        _resolve(str(disk), tmp_path)
    monkeypatch.setattr(copyfrom.os.path, "ismount", lambda p: False)
    assert _resolve(str(disk), tmp_path).kind == "folder"


def test_mount_root_reads_mountinfo(tmp_path, monkeypatch):
    info = tmp_path / "mountinfo"
    info.write_text("22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
                    "40 22 0:50 / /mnt/c rw - 9p drvfs rw\n"
                    "41 22 8:1 /home/me/my\\040app /mnt/app ro - ext4 /dev/sda1 rw\n", encoding="utf-8")
    monkeypatch.setattr(copyfrom, "_MOUNTINFO", str(info))
    from pathlib import PurePosixPath

    assert copyfrom._mount_root(PurePosixPath("/mnt/c")) == "/"
    assert copyfrom._mount_root(PurePosixPath("/mnt/app")) == "/home/me/my app"
    assert copyfrom._mount_root(PurePosixPath("/mnt/d")) is None
    monkeypatch.setattr(copyfrom, "_MOUNTINFO", str(tmp_path / "missing"))   # not Linux: the plain rule stays
    assert copyfrom._mount_root(PurePosixPath("/mnt/c")) is None


def test_the_sandbox_suggests_the_zip(tmp_path):
    with pytest.raises(CopyError) as err:
        _resolve(str(tmp_path / "missing"), tmp_path, sandbox=True)
    assert "Docker sandbox" in str(err.value) and ".zip" in str(err.value)
    home = tmp_path / "home"
    (home / "x").mkdir(parents=True)
    with pytest.raises(CopyError, match="upload the folder as a .zip"):
        _resolve(str(home / "x"), tmp_path, sandbox=True)
    if os.name == "nt":
        return   # a Windows path is absolute here: only "missing" applies
    with pytest.raises(CopyError) as err:
        _resolve("C:\\work\\p", tmp_path, sandbox=True)
    assert "Docker sandbox" in str(err.value) and ".zip" in str(err.value)


def test_check_target(tmp_path):
    src = _project(tmp_path / "src")
    source = Source("folder", src.resolve(), str(src))
    with pytest.raises(CopyError, match="would copy itself"):
        check_target(source, (src / "inner").resolve())
    with pytest.raises(CopyError, match="is inside the folder the session works in"):
        check_target(source, tmp_path.resolve())
    check_target(source, (tmp_path / "elsewhere").resolve())
    zipped = Source("zip", _write(tmp_path / "z" / "w.zip", "x").resolve(), "w.zip")
    with pytest.raises(CopyError, match="is inside the folder the session works in"):
        check_target(zipped, (tmp_path / "z").resolve())
    check_target(zipped, (tmp_path / "z" / "new").resolve())   # unpacking next to the zip is fine


# ---- the copy ---------------------------------------------------------------------------------------------------
def test_copy_into_a_folder_leaves_the_source_unchanged(tmp_path):
    src = _project(tmp_path / "src")
    ro = src / "src" / "main.py"
    os.chmod(ro, stat.S_IREAD)
    before = scan_tree(src, exclude_names=(), exclude_paths=())
    mtimes = {p: p.stat().st_mtime_ns for p in src.rglob("*")}
    try:
        result = copy_into(Source("folder", src, str(src)), tmp_path / "stage", LIMITS)
        assert _files(tmp_path / "stage") == {".gitignore": b"*.log\n", "a.txt": b"alpha\r\n",
                                              "src/main.py": b"print('hi')\n"}
        assert os.access(tmp_path / "stage" / "src" / "main.py", os.W_OK)   # the agents can edit the copy
        assert result["source"] == str(src) and result["kind"] == "folder"
        assert (result["files"], result["bytes"]) == (3, len(b"*.log\nalpha\r\nprint('hi')\n"))
        assert sorted(result["excluded"]) == [".git", ".venv", "node_modules", "src/__pycache__", "venv",
                                              "web/node_modules"]
        assert result["excluded_count"] == 6 and result["links_count"] == 0 and result["top"] is None
        after = scan_tree(src, exclude_names=(), exclude_paths=())
        assert after.digest == before.digest
        assert {p: p.stat().st_mtime_ns for p in src.rglob("*")} == mtimes
    finally:
        os.chmod(ro, stat.S_IREAD | stat.S_IWRITE)


def test_copy_into_leaves_out_links(tmp_path):
    outside = _write(tmp_path / "outside" / "secret.txt", "secret").parent
    src = tmp_path / "src"
    _write(src / "kept.txt", "kept")
    if not _dir_link(outside, src / "link"):
        pytest.skip("this account can make no links")
    result = copy_into(Source("folder", src, str(src)), tmp_path / "stage", LIMITS)
    assert _files(tmp_path / "stage") == {"kept.txt": b"kept"}
    assert result["links"] == ["link"] and result["links_count"] == 1
    assert "1 link(s) (link)" in copy_text(result)


def test_an_unreadable_file_refuses_the_copy(tmp_path, monkeypatch):
    src = tmp_path / "src"
    _write(src / "ok.txt", "ok")
    _write(src / "held.txt", "held")
    real_open = os.open

    def flaky_open(path, flags, *args, **kwargs):
        if str(path).endswith("held.txt") and not flags & os.O_WRONLY:
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(tc.os, "open", flaky_open)
    with pytest.raises(CopyError, match=r"Cannot read 1 file\(s\) in .*held.txt \(Permission denied\)") as err:
        copy_into(Source("folder", src, str(src)), tmp_path / "stage", LIMITS)
    assert err.value.status == 400 and "Nothing was copied" in str(err.value)
    assert not (tmp_path / "stage").exists()


def test_a_file_deleted_during_the_copy_is_reported_not_refused(tmp_path):
    src = tmp_path / "src"
    for name in ("a.txt", "b.txt", "c.txt"):
        _write(src / name, name)
    def check():   # an editor's temporary file goes while the copy runs (the count before it saw it)
        if (tmp_path / "stage").is_dir() and (src / "c.txt").exists():
            (src / "c.txt").unlink()

    result = copy_into(Source("folder", src, str(src)), tmp_path / "stage", LIMITS, check)
    assert _files(tmp_path / "stage") == {"a.txt": b"a.txt", "b.txt": b"b.txt"}
    assert (result["files"], result["vanished"], result["vanished_count"]) == (2, ["c.txt"], 1)
    assert copy_text(result).splitlines()[1] == "Left out: 1 file(s) deleted while they were copied (c.txt)."


def _rename_at(src: Path, stage: Path, nth: int):
    """A `check` that renames `src` at the nth call of the copy proper (the count before it ran untouched)."""
    calls = []

    def check():
        if stage.is_dir():
            calls.append(1)
            if len(calls) == nth:
                src.rename(src.with_name("moved"))
    return check


def test_a_source_moved_while_its_folders_are_copied_is_refused(tmp_path):
    src = tmp_path / "proj"
    for i in range(10):
        _write(src / f"d{i}" / "f.txt", "x")
    stage = tmp_path / "stage"
    with pytest.raises(CopyError, match="was moved, renamed or deleted while it was copied") as err:
        copy_into(Source("folder", src, str(src)), stage, LIMITS, _rename_at(src, stage, 3))
    assert "Nothing was copied" in str(err.value) and not stage.exists()


def test_a_source_moved_after_its_last_folder_was_listed_is_refused(tmp_path):
    """Only the files of the root are left once the root is renamed: each is gone, and the root is checked too."""
    src = tmp_path / "proj"
    for name in ("a.txt", "b.txt", "c.txt"):
        _write(src / name, name)
    stage = tmp_path / "stage"
    with pytest.raises(CopyError, match="was moved, renamed or deleted"):
        copy_into(Source("folder", src, str(src)), stage, LIMITS, _rename_at(src, stage, 3))
    assert not stage.exists()


def test_a_folder_deleted_during_the_copy_is_refused(tmp_path):
    src = tmp_path / "src"
    for rel in ("a.txt", "b/x.txt", "c/y.txt"):
        _write(src / rel, rel)
    stage = tmp_path / "stage"

    def check():   # a checkout in the source removes a folder before the walk reaches it
        if stage.is_dir() and (src / "c").exists():
            (src / "c" / "y.txt").unlink()
            (src / "c").rmdir()

    with pytest.raises(CopyError, match=r"while it was copied \(c\)"):
        copy_into(Source("folder", src, str(src)), stage, LIMITS, check)
    assert not stage.exists()


def test_os_litter_is_left_out_of_a_folder_as_of_a_zip(tmp_path):
    src = tmp_path / "src"
    for rel in ("a.txt", ".DS_Store", "web/.DS_Store", "web/Thumbs.db"):
        _write(src / rel, "x")
    result = copy_into(Source("folder", src, str(src)), tmp_path / "stage", LIMITS)
    assert _files(tmp_path / "stage") == {"a.txt": b"x"} and (tmp_path / "stage" / "web").is_dir()
    assert (result["files"], result["skipped_count"], result["excluded_count"]) == (1, 3, 0)
    zipped = _write(tmp_path / "w.zip", _zip({"a.txt": "x", ".DS_Store": "x", "web/Thumbs.db": "x"}))
    result = copy_into(Source("zip", zipped, str(zipped)), tmp_path / "zstage", LIMITS)
    assert _files(tmp_path / "zstage") == {"a.txt": b"x"} and result["skipped_count"] == 2


def test_names_the_session_folder_folds_are_refused_with_their_relative_path(tmp_path, monkeypatch):
    """A case-sensitive source written onto a file system that ignores case (a Linux hub's Docker Desktop bind
    mount, a case-sensitive APFS volume copied on macOS): a 400 that names the path in the source, not staging."""
    src = tmp_path / "src"
    _write(src / "README", "upper")
    _write(src / "docs" / "readme", "lower")
    real_open = os.open

    def folding_open(path, flags, *args, **kwargs):
        if flags & os.O_WRONLY and str(path).endswith("readme"):
            raise FileExistsError(17, "File exists", str(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(tc.os, "open", folding_open)
    with pytest.raises(CopyError, match="cannot tell apart") as err:
        copy_into(Source("folder", src, str(src)), tmp_path / "stage", LIMITS)
    assert err.value.status == 400 and "'docs/readme'" in str(err.value)
    assert str(tmp_path / "stage") not in str(err.value) and str(src) in str(err.value)
    assert not (tmp_path / "stage").exists()


def test_limits_refuse_the_copy_and_leave_nothing(tmp_path):
    src = tmp_path / "src"
    for name in ("a.txt", "b.txt"):
        _write(src / name, "x" * 600_000)
    for i in range(5):
        _write(src / "node_modules" / f"{i}.js", "x")   # never counted
    source = Source("folder", src, str(src))
    with pytest.raises(CopyError, match=r"more than 1 files to copy \(\[workspace\] copy_max_files; \.git") as err:
        copy_into(source, tmp_path / "stage", CopyLimits(max_mb=64, max_files=1))
    assert err.value.status == 413 and not (tmp_path / "stage").exists()
    with pytest.raises(CopyError, match=r"more than 1 MB to copy \(\[workspace\] copy_max_mb;") as err:
        copy_into(source, tmp_path / "stage", CopyLimits(max_mb=1, max_files=10))
    assert err.value.status == 413 and not (tmp_path / "stage").exists()
    assert copy_into(source, tmp_path / "stage", CopyLimits(max_mb=64, max_files=2))["files"] == 2   # exactly

    zipped = _write(tmp_path / "w.zip", _zip({"a": "1", "b": "2"}))
    with pytest.raises(CopyError, match=r"copy_max_files") as err:
        copy_into(Source("zip", zipped, str(zipped)), tmp_path / "zstage", CopyLimits(max_mb=1, max_files=1))
    assert err.value.status == 413 and not (tmp_path / "zstage").exists()
    big = _write(tmp_path / "big.zip", b"x" * (2 * 2 ** 20))   # random bytes may hold a zip's end record
    with pytest.raises(CopyError, match=r"larger than 1 MB \(\[workspace\] copy_max_mb\)") as err:
        copy_into(Source("zip", big, str(big)), tmp_path / "zstage", CopyLimits(max_mb=1, max_files=10))
    assert err.value.status == 413 and not (tmp_path / "zstage").exists()
    with pytest.raises(CopyError, match="not a zip"):
        copy_into(Source("zip", big, str(big)), tmp_path / "zstage", LIMITS)
    assert not (tmp_path / "zstage").exists()
    taken = _write(tmp_path / "taken" / "mine.txt", "mine").parent   # a staging that exists is never used
    with pytest.raises(CopyError, match="exists already") as err:
        copy_into(source, taken, LIMITS)
    assert err.value.status == 500 and (taken / "mine.txt").read_text(encoding="utf-8") == "mine"


def test_a_cancel_removes_the_staging(tmp_path):
    src = tmp_path / "src"
    for name in ("a.txt", "b.txt", "c.txt"):
        _write(src / name, name)
    stage = tmp_path / "stage"
    seen = []

    def check():
        if stage.is_dir() and any(stage.iterdir()):
            seen.append(sorted(p.name for p in stage.iterdir()))
            raise CopyCancelled()

    with pytest.raises(CopyCancelled):
        copy_into(Source("folder", src, str(src)), stage, LIMITS, check)
    assert seen and not stage.exists()
    zipped = _write(tmp_path / "w.zip", _zip({"a": "1", "b": "2"}))
    with pytest.raises(CopyCancelled):
        copy_into(Source("zip", zipped, str(zipped)), stage, LIMITS, check)
    assert not stage.exists()


def test_copy_into_a_zip_unwraps_its_top_folder(tmp_path):
    zipped = _write(tmp_path / "w.zip", _zip({"mywork/": "", "mywork/a.txt": "A", "mywork/.git/HEAD": "ref",
                                              "mywork/.DS_Store": "x"}))
    result = copy_into(Source("zip", zipped, str(zipped)), tmp_path / "stage", LIMITS)
    assert _files(tmp_path / "stage") == {"a.txt": b"A"}
    assert result["top"] == "mywork" and result["excluded"] == ["mywork/.git"] and result["skipped_count"] == 1
    assert result["special_count"] == 0 and result["kind"] == "zip"


def test_copy_text_and_size_text():
    assert [size_text(n) for n in (0, 1023, 1024, 1536, 2 ** 20, 5 * 2 ** 20 + 2 ** 19)] == \
        ["0 bytes", "1023 bytes", "1.0 KB", "1.5 KB", "1.0 MB", "5.5 MB"]
    base = {"source": "/w/p", "kind": "folder", "files": 3, "bytes": 2048, "links": [], "links_count": 0,
            "excluded": [], "excluded_count": 0, "special_count": 0, "skipped_count": 4, "top": None}
    assert copy_text(base) == "Copied 3 files (2.0 KB) from /w/p into the session's folder."
    zipped = {**base, "source": "upload: w.zip", "kind": "upload", "top": "mywork"}
    assert copy_text(zipped) == ("Copied 3 files (2.0 KB) from upload: w.zip (the contents of mywork/ in the zip) "
                                 "into the session's folder.")
    left = {**base, "links": ["l1", "l2", "l3", "l4"], "links_count": 4, "excluded": [".git"], "excluded_count": 1,
            "special_count": 2}
    assert copy_text(left).splitlines()[1] == ("Left out: 4 link(s) (l1, l2, l3, ...); 1 folder(s) by name (.git); "
                                               "2 special file(s).")


# ---- uploads ----------------------------------------------------------------------------------------------------
async def _chunks(*parts: bytes):
    for part in parts:
        await asyncio.sleep(0)
        yield part


def _names(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


async def test_receive_stores_a_zip(tmp_path):
    uploads = Uploads(tmp_path / "uploads")
    data = _zip({"a.txt": "A"})
    got = await uploads.receive(_chunks(data[:10], data[10:]), filename="w.zip", length=str(len(data)),
                                limits=LIMITS)
    token = got["upload"]
    assert UPLOAD_TOKEN.fullmatch(token) and got == {"upload": token, "filename": "w.zip", "bytes": len(data),
                                                     "expires_in": copyfrom.UPLOAD_TTL}
    assert _names(uploads.root) == [f"{token}.json", f"{token}.zip"]
    assert (uploads.root / f"{token}.zip").read_bytes() == data
    meta = uploads.peek(token)
    assert meta["filename"] == "w.zip" and meta["bytes"] == len(data)


async def test_receive_refusals_leave_nothing(tmp_path):
    uploads = Uploads(tmp_path / "uploads")
    small = CopyLimits(max_mb=1, max_files=10)
    with pytest.raises(CopyError, match=r"larger than 1 MB \(\[workspace\] copy_max_mb\)") as err:
        await uploads.receive(_chunks(b"x"), filename="w.zip", length=str(2 * 2 ** 20), limits=small)
    assert err.value.status == 413 and _names(uploads.root) == []
    with pytest.raises(CopyError) as err:
        await uploads.receive(_chunks(b"x" * 700_000, b"x" * 700_000), filename="w.zip", length=None, limits=small)
    assert err.value.status == 413 and _names(uploads.root) == []
    with pytest.raises(CopyError, match="not a zip") as err:
        await uploads.receive(_chunks(b"plain text"), filename="w.zip", length=None, limits=small)
    assert err.value.status == 400 and _names(uploads.root) == []
    with pytest.raises(CopyError, match="empty") as err:
        await uploads.receive(_chunks(), filename="w.zip", length="0", limits=small)
    assert err.value.status == 400 and _names(uploads.root) == []

    class Gone(Exception):
        pass

    async def broken():
        yield b"PK"
        raise Gone()

    with pytest.raises(Gone):   # a disconnect: the caller gets it, the partial file is gone
        await uploads.receive(broken(), filename="w.zip", length=None, limits=small)
    assert _names(uploads.root) == []


async def test_the_ninth_pending_upload_is_refused(tmp_path):
    uploads = Uploads(tmp_path / "uploads")
    data = _zip({"a": "1"})
    for _ in range(copyfrom.UPLOAD_MAX_PENDING):
        await uploads.receive(_chunks(data), filename="w.zip", length=None, limits=LIMITS)
    with pytest.raises(CopyError, match="uploads already wait") as err:
        await uploads.receive(_chunks(data), filename="w.zip", length=None, limits=LIMITS)
    assert err.value.status == 429


async def test_pending_uploads_count_zips_and_parts_not_claims(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    for _ in range(7):
        _write(root / f"{secrets.token_urlsafe(18)}.zip", "x")
    for _ in range(3):
        _write(root / f"{secrets.token_urlsafe(18)}.claimed-{os.getpid()}-{copyfrom.RUN}-0123abcd.zip", "x")
    uploads = Uploads(root)
    data = _zip({"a": "1"})
    await uploads.receive(_chunks(data), filename="w.zip", length=None, limits=LIMITS)   # 7 + 0 waiting: fine
    (root / f"{secrets.token_urlsafe(18)}.part").write_bytes(b"x")
    with pytest.raises(CopyError) as err:   # 8 zips and a part
        await uploads.receive(_chunks(data), filename="w.zip", length=None, limits=LIMITS)
    assert err.value.status == 429


def test_peek_refuses_bad_and_unknown_tokens(tmp_path):
    uploads = Uploads(tmp_path / "uploads")
    for bad in ("../x", "a" * 23, "a" * 25, "a" * 23 + "/", 5, None, ["a" * 24]):
        with pytest.raises(CopyError, match="must be a token") as err:
            uploads.peek(bad)
        assert err.value.status == 400
    with pytest.raises(CopyError, match="Upload the .zip again") as err:
        uploads.peek("a" * 24)
    assert err.value.status == 404


async def test_uploads_expire(tmp_path):
    uploads = Uploads(tmp_path / "uploads")
    token = (await uploads.receive(_chunks(_zip({"a": "1"})), filename="w.zip", length=None, limits=LIMITS))["upload"]
    meta_path = uploads.root / f"{token}.json"
    old = time.time() - copyfrom.UPLOAD_TTL - 1
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta_path.write_text(json.dumps({**meta, "at": old}), encoding="utf-8")
    for path in uploads.root.iterdir():
        os.utime(path, (old, old))
    with pytest.raises(CopyError) as err:
        uploads.peek(token)
    assert err.value.status == 404
    uploads.clean()
    assert _names(uploads.root) == []


async def test_upload_file_names_are_one_short_line(tmp_path):
    uploads = Uploads(tmp_path / "uploads")
    data = _zip({"a": "1"})
    for given, stored in (("a\nb.zip", "a b.zip"), ("x\x00y.zip", "x y.zip"), ("..\\..\\evil.zip", "evil.zip"),
                          ("../../evil.zip", "evil.zip"), ("", "upload.zip"), (None, "upload.zip"),
                          ("n" * 500 + ".zip", "n" * 200)):
        got = await uploads.receive(_chunks(data), filename=given, length=None, limits=LIMITS)
        assert got["filename"] == stored and uploads.peek(got["upload"])["filename"] == stored
        uploads.release(got["upload"], uploads.root / f"{got['upload']}.zip")   # (not counted any more)


async def test_claim_and_release(tmp_path):
    uploads = Uploads(tmp_path / "uploads")
    token = (await uploads.receive(_chunks(_zip({"a": "1"})), filename="w.zip", length=None, limits=LIMITS))["upload"]
    winner, loser = uploads.claim_path(token), uploads.claim_path(token)
    assert winner != loser and copyfrom.CLAIMED_NAME.fullmatch(winner.name)
    source = uploads.claim(token, winner)
    assert (source.kind, source.path, source.shown, source.token) == ("upload", winner, "upload: w.zip", token)
    assert winner.is_file() and not (uploads.root / f"{token}.zip").exists()
    with pytest.raises(CopyError) as err:
        uploads.claim(token, loser)
    assert err.value.status == 404
    uploads.release(token, loser)   # the loser never renamed anything: the winner's files stay
    assert _names(uploads.root) == sorted([winner.name, f"{token}.json"])
    uploads.release(token, winner)
    assert _names(uploads.root) == []


async def test_concurrent_claims_have_one_winner(tmp_path):
    """A rename on Windows is not exclusive (two can both succeed, the second moving the first one's file): the
    claims are made one at a time, so the loser gets the 404 and the winner keeps its file."""
    import threading

    uploads = Uploads(tmp_path / "uploads")
    data = _zip({"a": "1"})
    for _ in range(50):
        token = (await uploads.receive(_chunks(data), filename="w.zip", length=None, limits=LIMITS))["upload"]
        paths = [uploads.claim_path(token), uploads.claim_path(token)]
        barrier = threading.Barrier(2)
        outcomes: list = [None, None]

        def claim(i: int) -> None:
            barrier.wait()
            try:
                outcomes[i] = uploads.claim(token, paths[i])
            except CopyError as exc:
                outcomes[i] = exc

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        won = [i for i, o in enumerate(outcomes) if isinstance(o, Source)]
        lost = [o for o in outcomes if isinstance(o, CopyError)]
        assert len(won) == 1 and len(lost) == 1 and lost[0].status == 404, outcomes
        assert paths[won[0]].is_file() and not paths[1 - won[0]].exists()
        uploads.release(token, paths[won[0]])


def test_a_vanished_upload_is_a_404(tmp_path):
    source = Source("upload", tmp_path / "gone.zip", "upload: w.zip", "A" * 24)
    with pytest.raises(CopyError, match="Upload the .zip again") as err:
        copy_into(source, tmp_path / "staging", LIMITS)
    assert err.value.status == 404 and not (tmp_path / "staging").exists()


def test_another_dreamteam_home_is_never_copied(tmp_path):
    other = tmp_path / "other-home"
    (other / "sessions" / "001-x" / "workspace").mkdir(parents=True)
    _write(other / "dreamteam.toml", "[models.default]\napi_key = 'sk-secret'\n")
    for path in (other, other / "sessions", other / "sessions" / "001-x" / "workspace"):
        with pytest.raises(CopyError, match="is a DreamTeam home") as err:
            _resolve(str(path), tmp_path)
        assert err.value.status == 400 and str(other.resolve()) in str(err.value)
    plain = _project(tmp_path / "project")   # a project with a sessions/ folder but no dreamteam.toml is fine
    (plain / "sessions").mkdir()
    assert _resolve(str(plain), tmp_path).kind == "folder"


def test_names_from_a_zip_cannot_drive_the_terminal(tmp_path):
    """Control characters of member names (OSC 52 sets the clipboard, CSI clears the screen) never reach the
    result, its text or a refusal: they show as '?'."""
    path = tmp_path / "evil.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("t/a.txt", "A")
        zf.writestr("readme.txt", "R")
        link = zipfile.ZipInfo("t/l\x1b[31m")
        link.external_attr = 0o120777 << 16
        zf.writestr(link, "a.txt")
        zf.writestr("node_modules\x1b]0;title\x07/x.js", "x")   # (not excluded: the name differs)
    path.write_bytes(buf.getvalue())
    if os.name == "nt":   # Windows refuses the name with the control characters, without them in the message
        with pytest.raises(CopyError) as err:
            copy_into(Source("zip", path, str(path)), tmp_path / "s1", LIMITS)
        assert "\x1b" not in str(err.value) and "\x07" not in str(err.value) and "?" in str(err.value)
    else:
        result = copy_into(Source("zip", path, str(path)), tmp_path / "s1", LIMITS)
        assert result["links"] == ["t/l?[31m"]
        text = copy_text(result)
        assert "\x1b" not in text and "t/l?[31m" in text
    text = copy_text({"source": "s\x1b[2J", "files": 1, "bytes": 1, "links": ["t/l\x1b[31m"], "links_count": 1,
                      "excluded": ["x\x07"], "excluded_count": 1, "top": "a\x1b]52;c;SGVsbG8=\x07"})
    assert "\x1b" not in text and "\x07" not in text


def test_a_zip_copy_leaves_out_the_skills_mirror(tmp_path):
    """The folder copy's path exclusions (a DreamTeam workspace's .scratch/_skills and .unavailable-workspace)
    hold inside a zip too, after the unwrap."""
    path = tmp_path / "ws.zip"
    path.write_bytes(_zip({"mywork/a.txt": "A", "mywork/.scratch/_skills/s/SKILL.md": "skill",
                           "mywork/.scratch/anna/notes.md": "n", "mywork/.unavailable-workspace/x": "x"}))
    result = copy_into(Source("zip", path, str(path)), tmp_path / "s", LIMITS)
    assert _files(tmp_path / "s") == {"a.txt": b"A", ".scratch/anna/notes.md": b"n"}
    assert result["top"] == "mywork" and result["excluded_count"] == 2
    assert sorted(result["excluded"]) == ["mywork/.scratch/_skills", "mywork/.unavailable-workspace"]


def test_clean_keeps_live_claims_and_other_names(tmp_path):
    root = tmp_path / "uploads"
    root.mkdir()
    old = time.time() - copyfrom.UPLOAD_TTL - 1
    live, dead, earlier, orphan = (secrets.token_urlsafe(18) for _ in range(4))
    parent = os.getppid()
    assert pid_alive(parent)
    other_run = "0" * 8 if copyfrom.RUN != "0" * 8 else "1" * 8
    uploads = Uploads(root)
    own_claim = uploads.claim_path(secrets.token_urlsafe(18))   # a create of this run uses it
    own = own_claim.name.split(".", 1)[0]
    files = {
        "live": _write(root / f"{live}.claimed-{parent}-{other_run}-0123abcd.zip", "x"),
        "own": _write(own_claim, "x"),
        "own_json": _write(root / f"{own}.json", json.dumps({"filename": "o.zip", "bytes": 1, "at": old})),
        # this run's, no longer in use: its removal failed (a file still open on Windows)
        "orphan": _write(root / f"{orphan}.claimed-{os.getpid()}-{copyfrom.RUN}-0123abcd.zip", "x"),
        "dead": _write(root / f"{dead}.claimed-{_dead_pid()}-{copyfrom.RUN}-0123abcd.zip", "x"),
        "dead_json": _write(root / f"{dead}.json", json.dumps({"filename": "d.zip", "bytes": 1, "at": time.time()})),
        # this pid, an earlier run: the Docker sandbox's hub has the same pid at every container start
        "earlier": _write(root / f"{earlier}.claimed-{os.getpid()}-{other_run}-0123abcd.zip", "x"),
        "notes": _write(root / "notes.txt", "mine"),
        "short": _write(root / "abc.zip", "not a token"),
        "fresh": _write(root / f"{secrets.token_urlsafe(18)}.zip", "x"),
    }
    for key in ("live", "own", "notes", "short"):
        os.utime(files[key], (old, old))
    try:
        uploads.clean()
        left = {key for key, path in files.items() if path.exists()}
        assert left == {"live", "own", "notes", "short", "fresh"}   # (the own claim's json may go: it was read)
        assert copyfrom.left_over(os.getpid(), other_run, own_claim.name)
        assert not copyfrom.left_over(os.getpid(), copyfrom.RUN, own_claim.name)
        # a release whose unlink fails (Windows: the file still open) leaves the claim to the next clean
        def busy(*args, **kwargs):
            raise PermissionError(13, "busy")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(copyfrom.os, "unlink", busy)
            uploads.release(own, own_claim)
        assert own_claim.exists()
        uploads.clean()
        assert not own_claim.exists()
    finally:
        copyfrom.done_with(own_claim)


# ---- settings ---------------------------------------------------------------------------------------------------
def test_workspace_copy_settings_and_template(tmp_path):
    defaults = WorkspaceSettings()
    assert (defaults.copy_max_mb, defaults.copy_max_files) == (1024, 100000)
    raw = tomllib.loads(render_template("X", "ru", "anthropic"))
    assert raw["workspace"] == dataclasses.asdict(defaults)
    for key in ("copy_max_mb", "copy_max_files"):
        for value in (0, "x", True, -1, 1.5):
            with pytest.raises(ValueError, match=rf"^\[workspace\] {key} must be"):
                Config.from_dict(tmp_path, {"workspace": {key: value}})
        assert getattr(Config.from_dict(tmp_path, {"workspace": {key: 7}}).workspace, key) == 7
        assert key in public_view({})["workspace"]
    limits = CopyLimits.from_config(Config.from_dict(tmp_path, {"workspace": {"copy_max_mb": 3,
                                                                             "copy_max_files": 9}}))
    assert (limits.max_mb, limits.max_files, limits.max_bytes) == (3, 9, 3 * 2 ** 20)
