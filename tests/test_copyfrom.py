"""Copying a folder or a zip into a new session (copyfrom.py; docs/SPEC.md §17.2 "Copying files into a new
session"): the source checks, the copy and its limits, the result texts, the uploads store and the settings; the
uploads of the HTTP API v1, its name and path rules and its copy of several sources (§21.3, §21.8)."""

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
from dreamteam.copyfrom import (UPLOAD_TOKEN, CopyCancelled, CopyError, CopyLimits, Source, Uploads, check_rel,
                                check_target, check_upload_name, copy_into, copy_sources, copy_text, resolve_source,
                                size_text)
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
    assert (err.value.status, err.value.code) == (429, "too_many_uploads")


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


# ---- the uploads of the HTTP API v1 (docs/SPEC.md §21.3) --------------------------------------------------------
async def _file_upload(uploads: Uploads, data: bytes = b"hello", *, filename: str = "notes.txt",
                       unpack: bool = False, caller: str = "api", **kwargs) -> dict:
    return await uploads.receive(_chunks(data), filename=filename, length=None, limits=LIMITS, kind="file",
                                 unpack=unpack, caller=caller, **kwargs)


def _claims_held(tokens: list[str]) -> list[str]:
    return [name for name in copyfrom._IN_USE if name.split(".", 1)[0] in tokens]


async def test_a_v1_upload_keeps_any_file_with_its_caller(tmp_path):
    uploads = Uploads(tmp_path / "api")
    got = await _file_upload(uploads, b"%PDF-1.7 x", filename="Отчёт 1.pdf")
    token = got["upload"]
    assert got == {"upload": token, "filename": "Отчёт 1.pdf", "bytes": 10, "unpack": False,
                   "expires_in": copyfrom.UPLOAD_TTL}
    assert _names(uploads.root) == [f"{token}.bin", f"{token}.json"]
    assert (uploads.root / f"{token}.bin").read_bytes() == b"%PDF-1.7 x"
    meta = json.loads((uploads.root / f"{token}.json").read_text(encoding="utf-8"))
    assert list(meta) == ["filename", "bytes", "at", "unpack", "caller"]
    assert (meta["filename"], meta["bytes"], meta["unpack"], meta["caller"]) == ("Отчёт 1.pdf", 10, False, "api")
    assert uploads.peek_many([token], caller="api") == [{"token": token, "filename": "Отчёт 1.pdf", "bytes": 10,
                                                          "at": meta["at"], "unpack": False}]
    # the other kind of caller finds no such upload, and neither does the board's peek (a .zip)
    for peek in (lambda: uploads.peek_many([token], caller="owner"), lambda: uploads.peek(token)):
        with pytest.raises(CopyError) as err:
            peek()
        assert err.value.status == 404
    with pytest.raises(CopyError, match="Upload the file again"):
        uploads.peek_many([token], caller="owner")
    owners = await _file_upload(uploads, filename="mine.txt", caller="owner")
    assert uploads.peek_many([owners["upload"]], caller="owner")[0]["filename"] == "mine.txt"
    with pytest.raises(CopyError) as err:
        uploads.peek_many([owners["upload"]], caller="api")
    assert err.value.status == 404


async def test_a_v1_upload_to_unpack_must_be_a_zip(tmp_path):
    uploads = Uploads(tmp_path / "api")
    with pytest.raises(CopyError, match="not a zip archive: send it without unpack=true") as err:
        await _file_upload(uploads, b"plain text", filename="a.zip", unpack=True)
    assert (err.value.status, err.value.code) == (400, None) and _names(uploads.root) == []
    zipped = await _file_upload(uploads, _zip({"a.txt": "A"}), filename="w.zip", unpack=True)
    assert zipped["unpack"] is True and uploads.peek_many([zipped["upload"]], caller="api")[0]["unpack"] is True
    kept = await _file_upload(uploads, _zip({"a.txt": "A"}), filename="w.zip")   # without unpack: one file
    assert kept["unpack"] is False
    with pytest.raises(CopyError, match="empty") as err:
        await _file_upload(uploads, b"", filename="e.txt")
    assert err.value.status == 400


async def test_a_refused_upload_name_reads_nothing(tmp_path):
    uploads = Uploads(tmp_path / "api")
    read = []

    async def chunks():
        read.append(1)
        yield b"x"

    with pytest.raises(CopyError) as err:
        await uploads.receive(chunks(), filename="CON", length=None, limits=LIMITS, kind="file", caller="api")
    assert (err.value.status, err.value.code) == (400, "bad_name") and read == [] and _names(uploads.root) == []
    for bad in ({"kind": "folder"}, {"kind": "file", "caller": "guest"}, {"kind": "file"},
                {"unpack": True}, {"caller": "api"},
                # (the route parses ?unpack= strictly: bool("false") would be True)
                {"kind": "file", "caller": "api", "unpack": "false"}, {"kind": "file", "caller": "api", "unpack": 1}):
        with pytest.raises(ValueError):
            await uploads.receive(chunks(), filename="a.txt", length=None, limits=LIMITS, **bad)
    assert read == []


async def test_board_uploads_stay_zips_without_a_caller(tmp_path):
    uploads = Uploads(tmp_path / "uploads")
    got = await uploads.receive(_chunks(_zip({"a": "1"})), filename="w.zip", length=None, limits=LIMITS)
    assert list(got) == ["upload", "filename", "bytes", "expires_in"]
    meta = json.loads((uploads.root / f"{got['upload']}.json").read_text(encoding="utf-8"))
    assert list(meta) == ["filename", "bytes", "at"]
    with pytest.raises(CopyError) as err:   # a zip of the board is no v1 upload (and lives in another root)
        uploads.peek_many([got["upload"]], caller="owner")
    assert err.value.status == 404


async def test_v1_uploads_have_their_own_cap(tmp_path):
    uploads = Uploads(tmp_path / "api")
    for _ in range(copyfrom.UPLOAD_MAX_PENDING + 2):   # past the board's 8
        await _file_upload(uploads, max_pending=10)
    with pytest.raises(CopyError, match="uploads already wait") as err:
        await _file_upload(uploads, max_pending=10)
    assert (err.value.status, err.value.code) == (429, "too_many_uploads")
    assert len([n for n in _names(uploads.root) if n.endswith(".bin")]) == 10
    (uploads.root / f"{secrets.token_urlsafe(18)}.part").write_bytes(b"x")   # one arriving counts too
    with pytest.raises(CopyError):
        await _file_upload(uploads, max_pending=11)


async def test_v1_uploads_expire_and_left_over_claims_go(tmp_path):
    uploads = Uploads(tmp_path / "api")
    token = (await _file_upload(uploads))["upload"]
    meta_path = uploads.root / f"{token}.json"
    old = time.time() - copyfrom.UPLOAD_TTL - 1
    meta_path.write_text(json.dumps({**json.loads(meta_path.read_text(encoding="utf-8")), "at": old}),
                         encoding="utf-8")
    for path in uploads.root.iterdir():
        os.utime(path, (old, old))
    with pytest.raises(CopyError) as err:
        uploads.peek_many([token], caller="api")
    assert err.value.status == 404
    dead = secrets.token_urlsafe(18)
    _write(uploads.root / f"{dead}.claimed-{_dead_pid()}-{copyfrom.RUN}-0123abcd.bin", "x")
    _write(uploads.root / f"{dead}.json", json.dumps({"filename": "d.txt", "bytes": 1, "at": time.time(),
                                                      "unpack": False, "caller": "api"}))
    fresh = (await _file_upload(uploads))["upload"]   # (its _pending sweep removes the others)
    assert _names(uploads.root) == [f"{fresh}.bin", f"{fresh}.json"]
    uploads.clean()
    assert _names(uploads.root) == [f"{fresh}.bin", f"{fresh}.json"]


async def test_peek_many_refusals(tmp_path):
    uploads = Uploads(tmp_path / "api")
    token = (await _file_upload(uploads))["upload"]
    assert uploads.peek_many([], caller="api") == []
    for bad in ("x", token, None, [5], ["../x"], ["a" * 23], [token, "a" * 25], ["A" * 24, "bad"]):
        with pytest.raises(CopyError) as err:   # a malformed one is a 400 before any upload is looked at
            uploads.peek_many(bad, caller="api")
        assert err.value.status == 400, bad
    with pytest.raises(CopyError, match="twice") as err:
        uploads.peek_many([token, token], caller="api")
    assert err.value.status == 400
    with pytest.raises(CopyError, match="Upload the file again") as err:
        uploads.peek_many([token, "A" * 24], caller="api")
    assert err.value.status == 404
    # a json that cannot be read, or names no caller, cannot say whose the upload is
    meta_path = uploads.root / f"{token}.json"
    for meta in ("{", json.dumps({"filename": "notes.txt", "bytes": 5, "at": time.time()}),
                 json.dumps({"filename": "../x", "bytes": 5, "at": time.time(), "caller": "api"})):
        meta_path.write_text(meta, encoding="utf-8")
        with pytest.raises(CopyError) as err:
            uploads.peek_many([token], caller="api")
        assert err.value.status == 404


async def test_claim_many_takes_all_or_none_and_settle_gives_back(tmp_path, monkeypatch):
    uploads = Uploads(tmp_path / "api")
    tokens = [(await _file_upload(uploads, f"file {i}".encode(), filename=f"f{i}.txt",
                                  unpack=False))["upload"] for i in range(2)]
    tokens.append((await _file_upload(uploads, _zip({"z.txt": "Z"}), filename="z.zip", unpack=True))["upload"])
    waiting = sorted(f"{t}.{ext}" for t in tokens for ext in ("bin", "json"))
    stamps = {t: uploads.peek_many([t], caller="api")[0]["at"] for t in tokens}
    real_rename = os.rename
    claims = []

    def flaky(src, dst):
        if ".claimed-" in Path(dst).name:
            claims.append(dst)
            if len(claims) == 3:
                raise PermissionError(13, "busy")
        return real_rename(src, dst)

    with monkeypatch.context() as mp:
        mp.setattr(copyfrom.os, "rename", flaky)
        with pytest.raises(CopyError, match="Cannot use the upload") as err:
            uploads.claim_many(tokens, caller="api")
    assert err.value.status == 500 and len(claims) == 3
    assert _names(uploads.root) == waiting and _claims_held(tokens) == []   # the two taken were given back
    with pytest.raises(CopyError) as err:   # all or none: one of another caller takes none
        uploads.claim_many(tokens, caller="owner")
    assert err.value.status == 404 and _names(uploads.root) == waiting

    sources = uploads.claim_many(tokens, caller="api")
    assert [s.token for s in sources] == tokens and [s.kind for s in sources] == ["file", "file", "upload"]
    assert [s.filename for s in sources] == ["f0.txt", "f1.txt", "z.zip"]
    assert [s.shown for s in sources] == ["upload: f0.txt", "upload: f1.txt", "upload: z.zip"]
    assert all(copyfrom.CLAIMED_NAME.fullmatch(s.path.name) and s.path.is_file() for s in sources)
    assert sorted(_claims_held(tokens)) == sorted(s.path.name for s in sources)
    with pytest.raises(CopyError) as err:   # another create finds them taken
        uploads.claim_many([tokens[1]], caller="api")
    assert err.value.status == 404

    uploads.settle(sources, used=False)   # a refused create: every token works again, its hour unchanged
    assert _names(uploads.root) == waiting and _claims_held(tokens) == []
    assert {t: uploads.peek_many([t], caller="api")[0]["at"] for t in tokens} == stamps
    sources = uploads.claim_many(tokens, caller="api")
    uploads.settle(sources, used=True)   # a created session: used up
    assert _names(uploads.root) == [] and _claims_held(tokens) == []


async def test_unclaim_is_safe_to_call_in_a_finally(tmp_path, monkeypatch):
    uploads = Uploads(tmp_path / "api")
    token = (await _file_upload(uploads))["upload"]
    [source] = uploads.claim_many([token], caller="api")
    with monkeypatch.context() as mp:   # the rename back fails (Windows: still open): logged, no exception
        mp.setattr(copyfrom.os, "rename", lambda a, b: (_ for _ in ()).throw(PermissionError(13, "busy")))
        assert uploads.unclaim(source) is False
    assert _claims_held([token]) == [] and source.path.is_file()
    uploads.clean()   # (no longer in use: the sweep removes it)
    assert not source.path.exists()
    stranger = Source("file", tmp_path / "elsewhere.bin", "upload: x", token, filename="x")
    assert uploads.unclaim(stranger) is False   # no claim of this store
    uploads.settle([Source("folder", tmp_path, str(tmp_path))], used=False)   # (not an upload: passed over)


async def test_a_v1_upload_too_large_reads_nothing(tmp_path):
    uploads = Uploads(tmp_path / "api")
    read = []

    async def chunks():
        read.append(1)
        yield b"x"

    over = str(LIMITS.max_bytes + 1)
    with pytest.raises(CopyError, match="larger than 64 MB") as err:   # the Content-Length says so: 413 at once
        await uploads.receive(chunks(), filename="big.bin", length=over, limits=LIMITS, kind="file", caller="api")
    assert err.value.status == 413 and read == [] and _names(uploads.root) == []
    with pytest.raises(CopyError) as err:   # a bad name and a size too large: the name is checked first
        await uploads.receive(chunks(), filename="a:b", length=over, limits=LIMITS, kind="file", caller="api")
    assert (err.value.status, err.value.code) == (400, "bad_name") and read == []
    small = CopyLimits(max_mb=1, max_files=10)   # without a Content-Length: streamed against the limit
    with pytest.raises(CopyError) as err:
        await uploads.receive(_chunks(b"x" * 2 ** 20, b"y"), filename="big.bin", length=None, limits=small,
                              kind="file", caller="api")
    assert err.value.status == 413 and _names(uploads.root) == []


async def test_a_large_upload_is_copied_byte_for_byte(tmp_path):
    uploads = Uploads(tmp_path / "api")
    data = os.urandom(5 * 2 ** 19)   # 2.5 MiB: three reads of copyfrom.CHUNK
    got = await uploads.receive(_chunks(data[:3000], data[3000:2 ** 20 + 7], data[2 ** 20 + 7:]), filename="big.bin",
                                length=str(len(data)), limits=LIMITS, kind="file", caller="api")
    assert (uploads.root / f"{got['upload']}.bin").read_bytes() == data
    sources = uploads.claim_many([got["upload"]], caller="api")
    sessions = _sessions(tmp_path)
    try:
        result = copy_sources(sources, sessions / "stage", LIMITS)
    finally:
        uploads.settle(sources, used=True)
    assert (sessions / "stage" / "big.bin").read_bytes() == data
    assert (result["files"], result["bytes"]) == (1, len(data)) and _names(uploads.root) == []


def _hard_link(target: Path, path: Path) -> None:
    """Put another name of the file `target` where the file `path` is (what an agent that can write into the store
    could do)."""
    os.unlink(path)
    try:
        os.link(target, path)
    except (OSError, NotImplementedError):
        pytest.skip("this file system has no hard links")


def _file_link(target: Path, path: Path) -> None:
    """Put a symbolic link to the file `target` where the file `path` is."""
    os.unlink(path)
    try:
        os.symlink(target, path)
    except (OSError, NotImplementedError):
        pytest.skip("this system cannot make a file link here (Windows without the privilege)")


async def test_another_name_of_a_file_is_no_upload(tmp_path):
    """In the Docker sandbox the hub reads with more rights than the agents: a hard link put in place of an upload
    (another name of a file that may lie anywhere, a file of the hub's) is never read, neither when it is checked
    nor after the claim; the same for an unpacked zip and the board's zips."""
    secret = _write(tmp_path / "secret.txt", "TOP SECRET")
    zipped = _write(tmp_path / "secret.zip", _zip({"s.txt": "TOP SECRET"}))
    uploads = Uploads(tmp_path / "api")
    token = (await _file_upload(uploads))["upload"]
    _hard_link(secret, uploads.root / f"{token}.bin")
    with pytest.raises(CopyError) as err:
        uploads.peek_many([token], caller="api")
    assert err.value.status == 404
    sessions = _sessions(tmp_path)
    for data, filename, unpack, target in ((b"mine", "a.txt", False, secret),
                                           (_zip({"a.txt": "A"}), "w.zip", True, zipped)):
        token = (await _file_upload(uploads, data, filename=filename, unpack=unpack))["upload"]
        [source] = uploads.claim_many([token], caller="api")
        _hard_link(target, source.path)   # (swapped in after the claim)
        with pytest.raises(CopyError, match="Upload the file again") as err:
            copy_sources([source], sessions / "stage", LIMITS)
        assert err.value.status == 404 and _names(sessions) == []
        uploads.settle([source], used=False)
    board = Uploads(tmp_path / "uploads")
    token = (await board.receive(_chunks(_zip({"a": "1"})), filename="w.zip", length=None, limits=LIMITS))["upload"]
    claim = board.claim_path(token)
    source = board.claim(token, claim)
    _hard_link(zipped, claim)
    with pytest.raises(CopyError, match="Upload the .zip again") as err:
        copy_into(source, tmp_path / "staging", LIMITS)
    assert err.value.status == 404 and not (tmp_path / "staging").exists()
    board.release(token, claim)


async def test_a_link_in_the_store_is_no_upload(tmp_path):
    """The same for a symbolic link (to /proc/<hub>/environ, say): refused by lstat, and opened with O_NOFOLLOW."""
    secret = _write(tmp_path / "secret.txt", "TOP SECRET")
    uploads = Uploads(tmp_path / "api")
    token = (await _file_upload(uploads))["upload"]
    _file_link(secret, uploads.root / f"{token}.bin")
    for call in (lambda: uploads.peek_many([token], caller="api"), lambda: uploads.claim_many([token], caller="api")):
        with pytest.raises(CopyError) as err:
            call()
        assert err.value.status == 404
    token = (await _file_upload(uploads))["upload"]
    [source] = uploads.claim_many([token], caller="api")
    _file_link(secret, source.path)
    sessions = _sessions(tmp_path)
    with pytest.raises(CopyError, match="Upload the file again") as err:
        copy_sources([source], sessions / "stage", LIMITS)
    assert err.value.status == 404 and _names(sessions) == []
    uploads.settle([source], used=False)
    if hasattr(os, "O_NOFOLLOW"):   # a json that is a link says nothing of whose the upload is
        token = (await _file_upload(uploads))["upload"]
        meta = uploads.root / f"{token}.json"
        elsewhere = _write(tmp_path / "meta.json", meta.read_bytes())
        _file_link(elsewhere, meta)
        with pytest.raises(CopyError) as err:
            uploads.peek_many([token], caller="api")
        assert err.value.status == 404
    board = Uploads(tmp_path / "uploads")
    token = (await board.receive(_chunks(_zip({"a": "1"})), filename="w.zip", length=None, limits=LIMITS))["upload"]
    _file_link(_write(tmp_path / "secret.zip", _zip({"s.txt": "TOP SECRET"})), board.root / f"{token}.zip")
    with pytest.raises(CopyError) as err:
        board.peek(token)
    assert err.value.status == 404


def test_a_file_swapped_in_after_the_check_is_refused(tmp_path, monkeypatch):
    """Another file can take a claim's name between its lstat and its opening (Windows has no O_NOFOLLOW): the file
    opened must be the one lstat saw."""
    source = _claimed(tmp_path, "a.txt", b"mine")
    other = _write(tmp_path / "other.txt", "TOP SECRET")
    claim = copyfrom.long_path(source.path)
    real_lstat = os.lstat
    monkeypatch.setattr(copyfrom.os, "lstat",
                        lambda p, *a, **k: real_lstat(other) if os.fspath(p) == claim else real_lstat(p, *a, **k))
    sessions = _sessions(tmp_path)
    with pytest.raises(CopyError, match="Upload the file again") as err:
        copy_sources([source], sessions / "stage", LIMITS)
    assert err.value.status == 404 and _names(sessions) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
async def test_the_uploads_store_is_the_hubs_alone(tmp_path):
    """In the Docker sandbox the hub runs with umask 002 and the agents' group: the stores' folders and files are
    still the hub's alone."""
    old = os.umask(0o002)
    try:
        api = Uploads(tmp_path / ".dreamteam" / "uploads" / "api")
        token = (await _file_upload(api))["upload"]
        board = Uploads(tmp_path / "home" / "uploads")
        await board.receive(_chunks(_zip({"a": "1"})), filename="w.zip", length=None, limits=LIMITS)
    finally:
        os.umask(old)

    def mode(path: Path) -> int:
        return stat.S_IMODE(os.stat(path).st_mode)

    assert mode(api.root) == mode(api.root.parent) == mode(board.root) == 0o700
    assert mode(tmp_path / ".dreamteam") == 0o775   # (not the stores': left as it was made)
    assert mode(api.root / f"{token}.json") == mode(api.root / f"{token}.bin") == 0o600
    assert all(mode(p) == 0o600 for p in board.root.iterdir())


# ---- v1 names and paths (docs/SPEC.md §21.3 "The name", §21.8) --------------------------------------------------
@pytest.mark.parametrize("name", [
    None, 5, "", ".", "..", "a/b", "a\\b", "a\x00b", "a\tb", "a\x85b", "a\u2028b", "a\u2029b", "a\x7f",
    "a:b", "what?.md", "a<b", "a>b", 'a"b', "a|b", "a*b", "CON", "con", "aux.txt", "NUL.tar.gz", "COM1", "a.", "a ",
    ".git", ".GIT", "node_modules", "Node_Modules", ".venv", "__pycache__", ".scratch", ".Scratch", ".gitignore",
    ".GITIGNORE", ".dreamteam", "mcp", "MCP", "x" * 201, "я" * 130,
    # format characters (a bidi override, a mark, a BOM), private use, a lone surrogate; Windows short names
    "a\u202eb.txt", "a\u200eb", "\ufeffa", "\ue000.txt", "a\ud800b", "GIT~1", "PROGRA~1.TXT", "abc~2.md",
])
def test_refused_upload_names(name):
    with pytest.raises(CopyError) as err:
        check_upload_name(name)
    assert (err.value.status, err.value.code) == (400, "bad_name")
    assert all(ch.isprintable() for ch in str(err.value))


@pytest.mark.parametrize("name", ["notes.txt", "Отчёт 1.pdf", "a b.txt", "x" * 200, "я" * 127, ".env", "README",
                                  "data.tar.gz", "~$doc.docx", "mcp.txt", "gitignore", " lead.txt",
                                  # the spaces of CJK and macOS names, a combining mark, an emoji newer than the
                                  # Unicode of Python 3.11 (unassigned there), a '~' of no short name
                                  "資料\u3000第1回.pdf", "a\u00a0b.txt", "cafe\u0301.txt",
                                  "\U0001fae8.png", "backup~1-of-the-site.txt", "a~b.txt"])
def test_accepted_upload_names(name):
    assert check_upload_name(name) == name


def test_check_rel_everywhere():
    assert check_rel("") == () and check_rel(None) == ()
    assert check_rel("a/b c/d.txt") == ("a", "b c", "d.txt")
    assert check_rel("sub/.scratch/x") == ("sub", ".scratch", "x")   # only the top one holds the members' folders
    assert check_rel("backup~1-of-the-site.txt/a~b") == ("backup~1-of-the-site.txt", "a~b")   # no short names
    for bad in ("/etc/passwd", "a//b", "a/", "./a", "a/./b", "a/../b", "..", "a\\b", "a\x00b", ".git",
                ".GIT/config", "x/.Git/hooks/pre-commit", "sub/.git", ".scratch", ".scratch/anna/n.md",
                ".SCRATCH/x", ".scratch/_skills", ".scratch/_skills/s/SKILL.md", 5, b"a"):
        with pytest.raises(CopyError) as err:
            check_rel(bad)
        assert (err.value.status, err.value.code) == (400, "bad_name"), bad
    assert check_rel(".scratch", scratch=True) == (".scratch",)
    assert check_rel(".scratch/anna/n.md", scratch=True) == (".scratch", "anna", "n.md")
    for bad in (".scratch/_skills", ".scratch/_SKILLS/x", "a/../b", ".git", ".scratch/_SKILL~1/s/SKILL.md"):
        with pytest.raises(CopyError, match="Invalid path"):
            check_rel(bad, scratch=True)


def test_short_names_are_refused_on_every_os():
    """NTFS resolves 8.3 short names for a Linux hub too (WSL's /mnt/c, a Docker Desktop bind mount of a Windows
    folder): GIT~1 reaches .git and SCRATC~1/_skills the skills mirror, so their form is refused everywhere."""
    for path in ("GIT~1/config", "GIT~1", "SCRATC~1/_skills/s/SKILL.md", "sub/GIT~2/HEAD", "PROGRA~1",
                 "ABCDEF~1.TXT", "v1~2.md"):
        for scratch in (False, True):
            with pytest.raises(CopyError, match="short name") as err:
                check_rel(path, scratch=scratch)
            assert (err.value.status, err.value.code) == (400, "bad_name"), path


# names Windows reads as other names: refused on a Windows hub, listed and served on Linux and macOS
NTFS_PATHS = ["run-10:00.log", ".git::$INDEX_ALLOCATION/config", "what?.md", "a<b", 'a"b', "a|b", "a*b",
              "x\x01y", "CON", "aux.txt", "nul.txt/x", "COM1", "a.", "a ", "dir./x"]


def test_ntfs_names_are_refused_on_a_windows_hub_only():
    for path in NTFS_PATHS:
        if os.name == "nt":
            with pytest.raises(CopyError) as err:
                check_rel(path)
            assert err.value.code == "bad_name", path
        else:
            assert check_rel(path) == tuple(path.split("/")), path
        with pytest.raises(CopyError) as err:   # a path a client makes up: the Windows rules on every OS
            check_rel(path, portable=True)
        assert err.value.code == "bad_name", path


def test_portable_paths():
    assert check_rel("acme/site", portable=True) == ("acme", "site")
    assert check_rel("", portable=True) == ()
    spaced = ("資料\u3000", "a\u00a0b")   # spaces of any kind
    assert check_rel("/".join(spaced), portable=True) == spaced
    for bad in ("acme/.scratch", "acme/.Scratch/anna", ".scratch", "a\u2028b", "a\x85b/x", "a/../b", ".git",
                "a\u202eb/x", "GIT~1"):
        for scratch in (False, True):
            with pytest.raises(CopyError) as err:
                check_rel(bad, portable=True, scratch=scratch)
            assert err.value.code == "bad_name", bad


# ---- several sources in one staging folder (copy_sources) -------------------------------------------------------
def _claimed(tmp_path: Path, name: str, data: bytes, kind: str = "file") -> Source:
    """A Source as claim_many makes it (a claimed v1 upload), without the store."""
    path = _write(tmp_path / "claims" / secrets.token_hex(6), data)
    return Source(kind, path, f"upload: {name}", "A" * 24, filename=name)


def _sessions(tmp_path: Path) -> Path:
    folder = tmp_path / "sessions"
    folder.mkdir(exist_ok=True)
    return folder


def test_copy_sources_merges_files_and_unpacked_zips(tmp_path):
    sessions = _sessions(tmp_path)
    data = _zip({"a.csv": "1"})
    sources = [_claimed(tmp_path, "brief.pdf", b"%PDF"),
               _claimed(tmp_path, "site.zip", _zip({"site/index.html": "<h1>", "site/css/a.css": "x"}), "upload"),
               _claimed(tmp_path, "data.zip", data)]   # a zip without unpack: one file
    before = set(copyfrom._IN_USE)
    result = copy_sources(sources, sessions / "stage", LIMITS)
    assert _files(sessions / "stage") == {"brief.pdf": b"%PDF", "index.html": b"<h1>", "css/a.css": b"x",
                                          "data.zip": data}
    assert (result["files"], result["bytes"]) == (4, 4 + 4 + 1 + len(data))
    assert result["sources"] == [{"shown": "upload: brief.pdf", "files": 1, "bytes": 4},
                                 {"shown": "upload: site.zip", "files": 2, "bytes": 5},
                                 {"shown": "upload: data.zip", "files": 1, "bytes": len(data)}]
    assert result["source"] == "upload: brief.pdf, upload: site.zip, upload: data.zip"
    assert result["kind"] == "sources" and result["top"] is None and result["excluded_count"] == 0
    assert copy_text(result).startswith("Copied 4 files")
    assert _names(sessions) == ["stage"] and set(copyfrom._IN_USE) == before   # no part left, none held
    assert all(s.path.is_file() for s in sources)   # the claims are the store's to settle
    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(sessions / "stage" / "brief.pdf").st_mode)
        assert mode & 0o600 == 0o600 and not mode & 0o022 and not mode & 0o111


def test_copy_sources_leaves_out_the_sessions_own_names_at_the_top(tmp_path):
    """A '*' .gitignore would hide every output from the autocommits, and .scratch would write into the members'
    folders: at the top level of an unpacked zip they are left out and counted (below it they stay)."""
    sessions = _sessions(tmp_path)
    members = {".gitignore": "*", ".scratch/anna/n.md": "n", "mcp/srv/server.py": "x", ".DreamTeam/x": "x",
               "MCP.txt": "keep", "sub/.gitignore": "keep", "sub/mcp/y": "keep", "a.txt": "A"}
    source = _claimed(tmp_path, "w.zip", _zip(members), "upload")
    result = copy_sources([source], sessions / "stage", LIMITS)
    assert _files(sessions / "stage") == {"MCP.txt": b"keep", "sub/.gitignore": b"keep", "sub/mcp/y": b"keep",
                                          "a.txt": b"A"}
    assert sorted(result["excluded"]) == [".DreamTeam", ".gitignore", ".scratch", "mcp"]
    assert result["excluded_count"] == 4
    # after the unwrap of one top folder, as with the skills mirror
    wrapped = _claimed(tmp_path, "m.zip", _zip({"mywork/.gitignore": "*", "mywork/a.txt": "A"}), "upload")
    result = copy_sources([wrapped], sessions / "stage2", LIMITS)
    assert _files(sessions / "stage2") == {"a.txt": b"A"} and result["excluded"] == ["mywork/.gitignore"]
    # the board's copy of a zip is unchanged: it keeps them
    board = copy_into(Source("zip", source.path, "w.zip"), tmp_path / "board", LIMITS)
    assert ".gitignore" in _files(tmp_path / "board") and board["excluded_count"] == 0


def test_copy_sources_refuses_a_shared_top_name_and_leaves_nothing(tmp_path):
    sessions = _sessions(tmp_path)
    before = set(copyfrom._IN_USE)
    a = _claimed(tmp_path, "a.zip", _zip({"docs/x.md": "1", "README.md": "a"}), "upload")
    b = _claimed(tmp_path, "b.zip", _zip({"docs/y.md": "2", "other.txt": "o"}), "upload")
    readme = _claimed(tmp_path, "README.md", b"r")
    for pair in ((a, b), (a, readme)):
        with pytest.raises(CopyError, match="would both be at the top") as err:
            copy_sources(list(pair), sessions / "stage", LIMITS)
        assert (err.value.status, err.value.code) == (409, "file_conflict")
        assert "upload: a.zip" in str(err.value)
        assert _names(sessions) == [] and set(copyfrom._IN_USE) == before
    upper, lower = _claimed(tmp_path, "README", b"u"), _claimed(tmp_path, "readme", b"l")
    if os.name == "nt" or sys.platform == "darwin":
        with pytest.raises(CopyError, match="letter case") as err:
            copy_sources([upper, lower], sessions / "stage", LIMITS)
        assert err.value.code == "file_conflict" and _names(sessions) == []
    else:
        copy_sources([upper, lower], sessions / "stage", LIMITS)
        assert _files(sessions / "stage") == {"README": b"u", "readme": b"l"}


def test_a_name_the_file_system_folds_is_a_clash(tmp_path, monkeypatch):
    """The comparison is exact on Linux, whose folder may still ignore case (a Docker Desktop bind mount, a
    casefold folder): a name that lands on one moved before is a 409 too, never a replaced file."""
    probe = tmp_path / "Probe"
    probe.touch()
    if not (tmp_path / "probe").exists():
        pytest.skip("this file system tells letter case apart")
    monkeypatch.setattr(copyfrom, "_name_key", lambda name: name)   # the rule of a case-sensitive OS
    sessions = _sessions(tmp_path)
    with pytest.raises(CopyError, match="would both be at the top") as err:
        copy_sources([_claimed(tmp_path, "README", b"u"), _claimed(tmp_path, "readme", b"l")], sessions / "stage",
                     LIMITS)
    assert err.value.code == "file_conflict" and "'README'" in str(err.value) and _names(sessions) == []


def test_copy_sources_has_one_budget(tmp_path):
    sessions = _sessions(tmp_path)
    small = CopyLimits(max_mb=1, max_files=10)
    halves = [_claimed(tmp_path, f"h{i}.bin", b"x" * 600_000) for i in range(2)]
    with pytest.raises(CopyError, match=r"more than 1 MB in all \(\[workspace\] copy_max_mb\)") as err:
        copy_sources(halves, sessions / "stage", small)
    assert err.value.status == 413 and _names(sessions) == []
    big_zip = _claimed(tmp_path, "z.zip", _zip({"a": "x" * 600_000, "b": "y"}), "upload")
    with pytest.raises(CopyError, match="in all") as err:   # the zip unpacks past what the file left
        copy_sources([halves[0], big_zip], sessions / "stage", small)
    assert err.value.status == 413 and _names(sessions) == []
    three = [_claimed(tmp_path, "one.txt", b"1"), _claimed(tmp_path, "z.zip", _zip({"a": "1", "b": "2"}), "upload")]
    with pytest.raises(CopyError, match=r"more than 2 in all \(\[workspace\] copy_max_files\)") as err:
        copy_sources(three, sessions / "stage", CopyLimits(max_mb=64, max_files=2))
    assert err.value.status == 413 and _names(sessions) == []
    assert copy_sources(three, sessions / "stage", CopyLimits(max_mb=64, max_files=3))["files"] == 3   # exactly
    with pytest.raises(CopyError, match=r"The file is larger than 1 MB") as err:   # one source: its own text
        copy_sources([_claimed(tmp_path, "big.bin", b"x" * (2 ** 20 + 1))], sessions / "stage2", small)
    assert err.value.status == 413


def test_copy_sources_refusals_and_a_cancel_leave_nothing(tmp_path):
    sessions = _sessions(tmp_path)
    before = set(copyfrom._IN_USE)
    good = _claimed(tmp_path, "a.txt", b"a")
    with pytest.raises(CopyError, match="not a zip archive") as err:
        copy_sources([good, _claimed(tmp_path, "n.zip", b"plain", "upload")], sessions / "stage", LIMITS)
    assert err.value.status == 400 and _names(sessions) == []
    gone = Source("file", tmp_path / "claims" / "gone", "upload: g.txt", "B" * 24, filename="g.txt")
    with pytest.raises(CopyError, match="Upload the file again") as err:
        copy_sources([good, gone], sessions / "stage", LIMITS)
    assert err.value.status == 404 and _names(sessions) == []
    calls = []

    def check():
        calls.append(1)
        if len(calls) == 3:
            raise CopyCancelled()

    many = [_claimed(tmp_path, f"f{i}.txt", b"x") for i in range(4)]
    with pytest.raises(CopyCancelled):
        copy_sources(many, sessions / "stage", LIMITS, check)
    assert _names(sessions) == [] and set(copyfrom._IN_USE) == before
    taken = _write(sessions / "stage" / "mine.txt", "mine").parent   # a staging that exists is never used
    with pytest.raises(CopyError, match="exists already") as err:
        copy_sources([good], taken, LIMITS)
    assert err.value.status == 500 and (taken / "mine.txt").read_text(encoding="utf-8") == "mine"
    with pytest.raises(ValueError):   # a folder is walked by the server folders' own copy
        copy_sources([Source("folder", tmp_path, str(tmp_path))], sessions / "other", LIMITS)
    assert _names(sessions) == ["stage"]


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
