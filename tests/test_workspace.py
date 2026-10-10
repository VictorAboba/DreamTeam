from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dreamteam import workspace as workspace_mod
from dreamteam.models import USER, ToolError
from dreamteam.workspace import Workspace, email_slug, format_size

HAS_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not HAS_GIT, reason="git is not installed")

ANNA = "Anna Smith"
BORIS = "Boris King"
LIST_CAP = workspace_mod.LIST_MAX_ENTRIES


@pytest.fixture
async def ws(fake_swarm) -> Workspace:
    w = Workspace(fake_swarm)
    fake_swarm.workspace = w
    await w.init()
    return w


def git_log(root: Path, fmt: str = "%an|%ae|%s") -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "log", f"--format={fmt}"], capture_output=True, check=True)
    return out.stdout.decode("utf-8").splitlines()


def file_events(swarm, kind: str | None = None) -> list[dict]:
    events = swarm.bus.recent(types=("file.*",), limit=1000)
    return [e for e in events if kind is None or e["type"] == kind]


def put(ws: Workspace, rel: str, data: bytes | str) -> Path:
    p = ws.root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return p


# --- helpers ---------------------------------------------------------------------------------------

def test_email_slug():
    assert email_slug("John Smith") == "john.smith"
    assert email_slug("Анна Смирнова") == "anna.smirnova"
    assert email_slug("Фёдор Журавлёв") == "fedor.zhuravlev"
    assert email_slug("José O'Neil") == "jose.o.neil"
    assert email_slug("!!!") == "agent"
    assert email_slug("") == "agent"
    assert email_slug("中文") == "agent"
    assert email_slug(USER) == "user"


def test_format_size():
    assert format_size(10) == "10 B"
    assert format_size(2048) == "2.0 KB"
    assert format_size(3 * 1024 * 1024) == "3.0 MB"


# --- init / git -------------------------------------------------------------------------------------

@needs_git
async def test_init_creates_repo_gitignore_and_initial_commit(ws):
    assert (ws.root / ".git").is_dir()
    ignore = (ws.root / ".gitignore").read_text(encoding="utf-8")
    for entry in ("node_modules/", ".venv/", "__pycache__/", "*.pyc", ".DS_Store"):
        assert entry in ignore.splitlines()
    assert git_log(ws.root, "%s") == ["Initialize workspace"]
    assert ws.git_enabled
    status = subprocess.run(["git", "-C", str(ws.root), "status", "--porcelain"], capture_output=True)
    assert status.stdout.strip() == b""


@needs_git
async def test_init_is_idempotent_and_keeps_existing_files(fake_swarm):
    root = fake_swarm.config.workspace_dir
    (root / "existing.txt").write_text("hello", encoding="utf-8")
    (root / ".gitignore").write_text("custom/\n", encoding="utf-8")
    w = Workspace(fake_swarm)
    await w.init()
    await w.init()
    assert (root / ".gitignore").read_text(encoding="utf-8") == "custom/\n"
    assert git_log(root, "%s") == ["Initialize workspace"]
    tracked = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True).stdout.decode()
    assert "existing.txt" in tracked


async def test_init_creates_missing_root(make_fake_swarm):
    swarm = make_fake_swarm({"workspace": {"git": False}})
    shutil.rmtree(swarm.config.workspace_dir)
    w = Workspace(swarm)
    await w.init()
    assert w.root.is_dir()


async def test_git_disabled(make_fake_swarm):
    swarm = make_fake_swarm({"workspace": {"git": False}})
    w = Workspace(swarm)
    await w.init()
    assert not (w.root / ".git").exists()
    assert not w.git_enabled
    result = await w.write(ANNA, "a.txt", "x\n")
    assert "Created a.txt" in result and "commit" not in result
    assert await w.autocommit(ANNA, "manual") is None


async def test_git_missing(fake_swarm, monkeypatch):
    monkeypatch.setattr(workspace_mod.shutil, "which", lambda name: None)
    w = Workspace(fake_swarm)
    await w.init()
    assert not (w.root / ".git").exists()
    assert not w.git_enabled
    await w.write(ANNA, "notes.md", "hi")
    assert (w.root / "notes.md").read_text(encoding="utf-8") == "hi"
    assert await w.autocommit(ANNA, "x") is None


@needs_git
async def test_autocommit_only_when_dirty(ws):
    assert await ws.autocommit(ANNA, "nothing") is None
    put(ws, "made_by_shell.txt", "output")
    commit = await ws.autocommit(BORIS, "shell: build")
    assert commit and len(commit) == 40
    assert git_log(ws.root)[0] == f"{BORIS}|boris.king@dreamteam.local|shell: build"
    assert await ws.autocommit(BORIS, "again") is None


@needs_git
async def test_autocommit_survives_git_failure(ws, monkeypatch):
    put(ws, "x.txt", "x")

    async def broken(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(ws, "_git", broken)
    assert await ws.autocommit(ANNA, "x") is None


@needs_git
async def test_concurrent_writes_are_serialized(ws):
    actors = [ANNA if i % 2 else BORIS for i in range(8)]
    results = await asyncio.gather(*(ws.write(actors[i], f"f{i}.txt", f"{i}\n") for i in range(8)))
    assert all("Created" in r and "commit" in r for r in results)
    status = subprocess.run(["git", "-C", str(ws.root), "status", "--porcelain"], capture_output=True)
    assert status.stdout.strip() == b""
    # every write is its own commit, authored by its writer and containing only its file
    by_file = {files[0]: (author, subject) for author, subject, files in commits(ws.root) if files}
    for i in range(8):
        assert by_file[f"f{i}.txt"] == (actors[i], f"create f{i}.txt")
    assert all(len(files) == 1 for _, subject, files in commits(ws.root) if subject.startswith("create"))


def commits(root: Path) -> list[tuple[str, str, list[str]]]:
    """[(author, subject, [files])], newest first."""
    out = subprocess.run(["git", "-C", str(root), "log", "--name-only", "--format=@@%an|%s"],
                         capture_output=True, check=True).stdout.decode("utf-8")
    result = []
    for chunk in out.split("@@")[1:]:
        head, *files = [line for line in chunk.splitlines() if line.strip()]
        author, subject = head.split("|", 1)
        result.append((author, subject, files))
    return result


@needs_git
async def test_write_commits_only_its_own_file(ws):
    put(ws, "build/out.txt", "made by Boris's shell command, not committed yet")
    result = await ws.write(ANNA, "notes.md", "hi\n")
    assert "commit" in result
    assert commits(ws.root)[0] == (ANNA, "create notes.md", ["notes.md"])
    assert await ws.autocommit(BORIS, "shell: make")
    assert commits(ws.root)[0] == (BORIS, "shell: make", ["build/out.txt"])


@needs_git
async def test_write_commit_treats_the_path_literally(ws):
    put(ws, "a1.txt", "pending, not Anna's")
    await ws.write(ANNA, "a[1].txt", "x")
    assert commits(ws.root)[0] == (ANNA, "create a[1].txt", ["a[1].txt"])


@needs_git
async def test_write_is_not_swept_into_a_concurrent_shell_commit(ws):
    put(ws, "shell.txt", "made by a shell command")
    result, shell_commit = await asyncio.gather(ws.write(ANNA, "a.txt", "a\n"),
                                                ws.autocommit(BORIS, "shell: run"))
    assert "commit" in result and shell_commit
    by_subject = {subject: (author, files) for author, subject, files in commits(ws.root)}
    assert by_subject["create a.txt"] == (ANNA, ["a.txt"])
    assert by_subject["shell: run"] == (BORIS, ["shell.txt"])


@needs_git
async def test_inherited_git_env_does_not_hijack_commits(ws, tmp_path, monkeypatch):
    other = tmp_path / "other"
    subprocess.run(["git", "init", "-q", str(other)], check=True, capture_output=True)
    with monkeypatch.context() as m:
        m.setenv("GIT_AUTHOR_NAME", "Intruder")
        m.setenv("GIT_AUTHOR_EMAIL", "intruder@example.com")
        m.setenv("GIT_COMMITTER_NAME", "Intruder")
        m.setenv("GIT_DIR", str(other / ".git"))
        m.setenv("GIT_WORK_TREE", str(other))
        result = await ws.write(ANNA, "a.txt", "x")
        put(ws, "b.txt", "y")
        shell_commit = await ws.autocommit(BORIS, "shell: x")
    assert "commit" in result and shell_commit
    assert git_log(ws.root, "%an|%ae|%cn|%s")[:2] == [
        f"{BORIS}|boris.king@dreamteam.local|{BORIS}|shell: x",
        f"{ANNA}|anna.smith@dreamteam.local|{ANNA}|create a.txt",
    ]
    assert subprocess.run(["git", "-C", str(other), "rev-parse", "HEAD"], capture_output=True).returncode != 0


def make_unindexable(root: Path) -> Path | None:
    """A file `git add` fails on: a reserved device name on Windows, an unreadable file elsewhere."""
    if sys.platform == "win32":
        p = root / "nul.txt"
        try:
            with open("\\\\?\\" + str(p.resolve()), "wb") as f:
                f.write(b"x")
        except OSError:
            return None
    else:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            return None  # root reads everything
        p = root / "secret.txt"
        p.write_bytes(b"x")
        p.chmod(0)
    probe = subprocess.run(["git", "-C", str(root), "add", "--dry-run", "-A"], capture_output=True)
    if probe.returncode == 0:
        return None
    return p


@needs_git
async def test_autocommit_survives_a_file_git_cannot_index(ws):
    bad = make_unindexable(ws.root)
    if bad is None:
        pytest.skip("cannot create a file that git fails to index here")
    put(ws, "good.txt", "fine")
    assert await ws.autocommit(BORIS, "shell: build")
    assert commits(ws.root)[0] == (BORIS, "shell: build", ["good.txt"])
    result = await ws.write(ANNA, "next.txt", "x")
    assert "commit" in result
    assert commits(ws.root)[0] == (ANNA, "create next.txt", ["next.txt"])
    if sys.platform != "win32":
        bad.chmod(0o644)


# --- paths ---------------------------------------------------------------------------------------------

async def test_resolve_and_rel(ws):
    root = ws.root.resolve()
    assert ws.resolve("a/b.txt") == root / "a" / "b.txt"
    assert ws.resolve("a\\b.txt") == root / "a" / "b.txt"
    assert ws.resolve("./a/./b.txt") == root / "a" / "b.txt"
    assert ws.resolve("a/../c.txt") == root / "c.txt"
    assert ws.resolve("") == root
    assert ws.resolve(".") == root
    assert ws.resolve(str(root / "x" / "y.md")) == root / "x" / "y.md"
    assert ws.rel(ws.resolve("a\\b\\c.txt")) == "a/b/c.txt"
    assert ws.rel(root) == "."
    assert ws.rel(ws.root / "d" / "e.txt") == "d/e.txt"


@pytest.mark.parametrize("bad", [
    "../outside.txt", "a/../../outside.txt", "..\\outside.txt", "..",
    ".git", ".git/config", "sub/../.git/HEAD", ".GIT/config", ".git\\hooks\\pre-commit",
])
async def test_resolve_rejects_escapes_and_git(ws, bad):
    with pytest.raises(ToolError):
        ws.resolve(bad)


async def test_resolve_rejects_absolute_outside(ws, tmp_path):
    with pytest.raises(ToolError, match="outside the workspace"):
        ws.resolve(str(tmp_path / "elsewhere.txt"))
    with pytest.raises(ToolError, match="outside"):
        ws.resolve(str(ws.root.parent / "dreamteam.toml"))


async def test_resolve_rejects_symlink_escape(ws, tmp_path):
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "key.txt").write_text("secret", encoding="utf-8")
    make_dir_link(ws.root / "link", outside)
    with pytest.raises(ToolError, match="outside"):
        ws.resolve("link/key.txt")
    with pytest.raises(ToolError):
        ws.read("link/key.txt")
    with pytest.raises(ToolError):
        await ws.write(ANNA, "link/new.txt", "x")
    assert not (outside / "new.txt").exists()
    assert ws.exists("link/key.txt") is False
    listing = ws.list(".", depth=5)
    assert "  link (link)" in listing.splitlines() and "key.txt" not in listing
    paths = [e["path"] for e in ws.tree(".", depth=5)]
    assert "link" in paths and not any(p.startswith("link/") for p in paths)


def make_dir_link(link: Path, target: Path) -> None:
    """Directory symlink, or a junction on Windows without symlink privileges."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    if not link.exists():
        pytest.skip("neither symlinks nor junctions are available")


async def test_resolve_rejects_nul(ws):
    with pytest.raises(ToolError):
        ws.resolve("a\x00b.txt")


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows path rules")


@windows_only
async def test_resolve_never_touches_network_paths(ws, monkeypatch):
    seen: list[str] = []
    real_realpath = os.path.realpath

    def spy(p, *args, **kwargs):
        seen.append(os.fspath(p))
        return real_realpath(p, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", spy)
    for bad in ("//attacker.invalid/share/x", r"\\attacker.invalid\share\x", "//?/C:/Windows/win.ini",
                "//./C:/Windows/win.ini"):
        with pytest.raises(ToolError, match="outside the workspace"):
            ws.resolve(bad)
        assert ws.exists(bad) is False and ws.mtime(bad) is None
    assert not any(p.startswith(("\\\\", "//")) for p in seen)


@windows_only
@pytest.mark.parametrize("bad", ["nul", "NUL", "con", "aux/x.txt", "nul.txt", "COM1.log", "lpt9.md", "con .txt",
                                 "conin$", "src/prn.py"])
async def test_windows_reserved_names_rejected(ws, bad):
    with pytest.raises(ToolError, match="reserved device name"):
        await ws.write(ANNA, bad, "x")
    assert ws.exists(bad) is False


@windows_only
async def test_windows_name_rules(ws):
    for ok in ("auxiliary.txt", "console.log", "nullable.py", "com10.txt", "con_fig.md"):
        await ws.write(ANNA, ok, "x")
        assert ws.read(ok).endswith("x")
    for bad in ("a?b.txt", "a*b", 'say"hi".txt', "a|b", "a<b>"):
        with pytest.raises(ToolError, match="cannot contain"):
            await ws.write(ANNA, bad, "x")
    with pytest.raises(ToolError, match="trailing dot"):
        await ws.write(ANNA, "dir./x.txt", "x")
    assert not (ws.root / "dir").exists()
    # an existing file reached with Windows' trailing-dot/space aliasing maps to its real name
    put(ws, "notes.md", "x")
    assert ws.rel(ws.resolve("notes.md.")) == "notes.md"


@windows_only
async def test_trailing_dot_cannot_bypass_a_lock(ws):
    ws.lock(ANNA, "plan.md")  # the file does not exist yet
    for alias in ("plan.md.", "plan.md. .", "PLAN.md"):
        with pytest.raises(ToolError):
            await ws.write(BORIS, alias, "mine")
    assert not (ws.root / "plan.md").exists()


async def test_mtime_and_exists_never_raise(ws):
    put(ws, "here.txt", "x")
    assert ws.exists("here.txt") is True
    assert isinstance(ws.mtime("here.txt"), float)
    assert ws.exists("missing.txt") is False
    assert ws.mtime("missing.txt") is None
    for bad in ("../x", ".git/HEAD", "a\x00b", "/definitely/outside/x"):
        assert ws.exists(bad) is False
        assert ws.mtime(bad) is None


# --- read ------------------------------------------------------------------------------------------------

async def test_read_header_and_raw_lines(ws):
    put(ws, "doc.md", "alpha\nbeta\ngamma\n")
    text = ws.read("doc.md")
    assert text == "[doc.md: lines 1-3 of 3]\nalpha\nbeta\ngamma"


async def test_read_offset_limit_and_continuation(ws):
    put(ws, "n.txt", "".join(f"line {i}\n" for i in range(1, 11)))
    text = ws.read("n.txt", offset=3, limit=4)
    lines = text.splitlines()
    assert lines[0] == "[n.txt: lines 3-6 of 10]"
    assert lines[1:5] == ["line 3", "line 4", "line 5", "line 6"]
    assert "offset=7" in lines[-1] and "4 more lines" in lines[-1]
    tail = ws.read("n.txt", offset=9)
    assert tail.splitlines() == ["[n.txt: lines 9-10 of 10]", "line 9", "line 10"]


async def test_read_default_limit_400(ws):
    put(ws, "big.txt", "\n".join(str(i) for i in range(1, 1001)))
    text = ws.read("big.txt")
    assert text.startswith("[big.txt: lines 1-400 of 1000]\n1\n2\n")
    assert "offset=401" in text
    assert "\n400\n" in text and "\n401\n" not in text


async def test_read_bad_offset(ws):
    put(ws, "s.txt", "a\nb\n")
    with pytest.raises(ToolError, match="past the end"):
        ws.read("s.txt", offset=5)
    assert ws.read("s.txt", offset=0).startswith("[s.txt: lines 1-2 of 2]")


async def test_read_missing_dir_binary_empty(ws):
    with pytest.raises(ToolError, match="list_files"):
        ws.read("nope.txt")
    (ws.root / "folder").mkdir()
    with pytest.raises(ToolError, match="directory"):
        ws.read("folder")
    put(ws, "img.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    with pytest.raises(ToolError, match="binary"):
        ws.read("img.png")
    put(ws, "empty.txt", "")
    assert ws.read("empty.txt") == "[empty.txt: empty file, 0 lines]"


async def test_read_crlf_bom_and_unicode(ws):
    put(ws, "win.txt", "\ufeffпервая\r\nвторая\r\n")
    assert ws.read("win.txt") == "[win.txt: lines 1-2 of 2]\nпервая\nвторая"


async def test_read_char_cap(make_fake_swarm):
    swarm = make_fake_swarm({"agents": {"tool_output_chars": 400}, "workspace": {"git": False}})
    w = Workspace(swarm)
    await w.init()
    put(w, "wide.txt", "".join(f"{i:03d} " + "x" * 46 + "\n" for i in range(1, 101)))
    text = w.read("wide.txt")
    assert len(text) <= 400
    header = text.splitlines()[0]
    assert header.startswith("[wide.txt: lines 1-")
    last = int(header.split("-")[1].split()[0])
    assert 1 < last < 100
    assert f"offset={last + 1}" in text.splitlines()[-1]
    nxt = w.read("wide.txt", offset=last + 1)
    assert nxt.splitlines()[1].startswith(f"{last + 1:03d} ")


async def test_read_single_huge_line_is_cut(make_fake_swarm):
    swarm = make_fake_swarm({"agents": {"tool_output_chars": 300}, "workspace": {"git": False}})
    w = Workspace(swarm)
    await w.init()
    put(w, "min.js", "y" * 5000 + "\nsecond\n")
    text = w.read("min.js")
    assert text.startswith("[min.js: lines 1-1 of 2]")
    assert "line 1 was cut" in text and "offset=2" in text
    assert len(text) <= 300


# --- write / edit -----------------------------------------------------------------------------------------

@needs_git
async def test_write_creates_emits_and_commits(ws, fake_swarm):
    result = await ws.write(ANNA, "docs/plan/notes.md", "one\ntwo\n")
    assert result.startswith("Created docs/plan/notes.md (2 lines") and "commit" in result
    assert (ws.root / "docs" / "plan" / "notes.md").read_bytes() == b"one\ntwo\n"
    events = file_events(fake_swarm, "file.changed")
    assert len(events) == 1
    assert events[0]["actor"] == ANNA
    assert events[0]["data"] == {"path": "docs/plan/notes.md", "op": "create"}
    assert git_log(ws.root)[0] == f"{ANNA}|anna.smith@dreamteam.local|create docs/plan/notes.md"

    result = await ws.write(BORIS, "docs\\plan\\notes.md", "three\n")
    assert result.startswith("Updated docs/plan/notes.md (1 line,")
    assert file_events(fake_swarm, "file.changed")[-1]["data"]["op"] == "write"
    assert git_log(ws.root)[0] == f"{BORIS}|boris.king@dreamteam.local|write docs/plan/notes.md"


@needs_git
async def test_write_unicode_names(ws):
    await ws.write("Анна Смирнова", "отчёт.md", "Привет, мир\n")
    assert git_log(ws.root)[0] == "Анна Смирнова|anna.smirnova@dreamteam.local|create отчёт.md"
    assert ws.read("отчёт.md").endswith("Привет, мир")


async def test_write_keeps_bytes_exact(ws):
    await ws.write(ANNA, "lf.txt", "a\nb\n")
    assert (ws.root / "lf.txt").read_bytes() == b"a\nb\n"
    await ws.write(ANNA, "crlf.txt", "a\r\nb\r\n")
    assert (ws.root / "crlf.txt").read_bytes() == b"a\r\nb\r\n"


@needs_git
async def test_write_identical_content_is_noop(ws, fake_swarm):
    await ws.write(ANNA, "same.txt", "x")
    commits = len(git_log(ws.root))
    events = len(file_events(fake_swarm))
    result = await ws.write(ANNA, "same.txt", "x")
    assert "unchanged" in result
    assert len(git_log(ws.root)) == commits
    assert len(file_events(fake_swarm)) == events


async def test_write_rejections(ws):
    (ws.root / "dir").mkdir()
    with pytest.raises(ToolError, match="directory"):
        await ws.write(ANNA, "dir", "x")
    with pytest.raises(ToolError):
        await ws.write(ANNA, ".git/config", "x")
    with pytest.raises(ToolError):
        await ws.write(ANNA, "../escape.txt", "x")
    put(ws, "plain.txt", "x")
    with pytest.raises(ToolError, match="Cannot write"):
        await ws.write(ANNA, "plain.txt/child.txt", "x")


@needs_git
async def test_edit_single_occurrence(ws, fake_swarm):
    put(ws, "code.py", "def f():\n    return 1\n")
    await ws.autocommit(ANNA, "seed")
    result = await ws.edit(BORIS, "code.py", "return 1", "return 2")
    assert result.startswith("Edited code.py: replaced 1 occurrence") and "commit" in result
    assert (ws.root / "code.py").read_text(encoding="utf-8") == "def f():\n    return 2\n"
    event = file_events(fake_swarm, "file.changed")[-1]
    assert event["actor"] == BORIS and event["data"] == {"path": "code.py", "op": "edit"}
    assert git_log(ws.root)[0] == f"{BORIS}|boris.king@dreamteam.local|edit code.py"


async def test_edit_multiple_occurrences(ws):
    put(ws, "dup.txt", "x = 1\ny = 1\nz = 1\n")
    with pytest.raises(ToolError, match="occurs 3 times") as err:
        await ws.edit(ANNA, "dup.txt", "= 1", "= 2")
    assert "replace_all" in str(err.value)
    assert (ws.root / "dup.txt").read_text(encoding="utf-8") == "x = 1\ny = 1\nz = 1\n"
    result = await ws.edit(ANNA, "dup.txt", "= 1", "= 2", replace_all=True)
    assert "replaced 3 occurrences" in result
    assert (ws.root / "dup.txt").read_text(encoding="utf-8") == "x = 2\ny = 2\nz = 2\n"


async def test_edit_errors(ws):
    put(ws, "e.txt", "hello world\n    indented line\n")
    with pytest.raises(ToolError, match="not found") as err:
        await ws.edit(ANNA, "e.txt", "goodbye", "x")
    assert "read_file" in str(err.value)
    with pytest.raises(ToolError, match="line 2"):
        await ws.edit(ANNA, "e.txt", "\tindented line\n  next", "x")
    with pytest.raises(ToolError, match="empty"):
        await ws.edit(ANNA, "e.txt", "", "x")
    with pytest.raises(ToolError, match="list_files"):
        await ws.edit(ANNA, "missing.txt", "a", "b")
    put(ws, "bin.dat", b"ab\x00cd")
    with pytest.raises(ToolError, match="binary"):
        await ws.edit(ANNA, "bin.dat", "ab", "x")
    put(ws, "latin.txt", b"caf\xe9 au lait")
    with pytest.raises(ToolError, match="UTF-8"):
        await ws.edit(ANNA, "latin.txt", "au", "x")


async def test_edit_noop_when_old_equals_new(ws, fake_swarm):
    put(ws, "n.txt", "abc")
    before = len(file_events(fake_swarm))
    assert "unchanged" in await ws.edit(ANNA, "n.txt", "b", "b")
    assert len(file_events(fake_swarm)) == before


async def test_concurrent_edits_are_not_lost(ws):
    put(ws, "shared.txt", "".join(f"slot{i}=old\n" for i in range(10)))
    await asyncio.gather(*(ws.edit(ANNA if i % 2 else BORIS, "shared.txt", f"slot{i}=old", f"slot{i}=new")
                           for i in range(10)))
    text = (ws.root / "shared.txt").read_text(encoding="utf-8")
    assert "old" not in text and text.count("=new") == 10


async def test_edit_matches_across_line_endings(ws):
    put(ws, "crlf.txt", "one\r\ntwo\r\nthree\r\n")
    await ws.edit(ANNA, "crlf.txt", "one\ntwo\n", "uno\ndos\n")
    assert (ws.root / "crlf.txt").read_bytes() == b"uno\r\ndos\r\nthree\r\n"
    put(ws, "lf.txt", "one\ntwo\n")
    await ws.edit(ANNA, "lf.txt", "one\r\ntwo", "1\r\n2")
    assert (ws.root / "lf.txt").read_bytes() == b"1\n2\n"


async def test_edit_keeps_the_file_line_endings_in_new_text(ws):
    # old_text matches as given (one line) but new_text adds lines with the other line ending
    put(ws, "win.txt", "one\r\ntwo\r\n")
    await ws.edit(ANNA, "win.txt", "one", "one\ninserted")
    assert (ws.root / "win.txt").read_bytes() == b"one\r\ninserted\r\ntwo\r\n"
    put(ws, "unix.txt", "a\nb\n")
    await ws.edit(ANNA, "unix.txt", "a", "a\r\nz")
    assert (ws.root / "unix.txt").read_bytes() == b"a\nz\nb\n"
    put(ws, "mixed.txt", "a\r\nb\nc")  # inconsistent file: new_text is inserted as given
    await ws.edit(ANNA, "mixed.txt", "c", "c\nd")
    assert (ws.root / "mixed.txt").read_bytes() == b"a\r\nb\nc\nd"


async def test_write_and_edit_reject_unpaired_surrogates(ws):
    with pytest.raises(ToolError, match="surrogate"):
        await ws.write(ANNA, "s.txt", "broken emoji \ud83d here")
    assert not (ws.root / "s.txt").exists()
    put(ws, "e.txt", "abc")
    with pytest.raises(ToolError, match="new_text.*surrogate"):
        await ws.edit(ANNA, "e.txt", "b", "\udc00")
    assert (ws.root / "e.txt").read_bytes() == b"abc"


# --- locks ---------------------------------------------------------------------------------------------

async def test_lock_default_ttl_and_listing(ws, fake_swarm):
    before = time.time()
    result = ws.lock(ANNA, "report.md")
    assert result.startswith("Locked report.md until")
    locks = ws.locks()
    assert len(locks) == 1
    lock = locks[0]
    assert set(lock) == {"path", "owner", "acquired_at", "expires_at"}
    assert lock["path"] == "report.md" and lock["owner"] == ANNA
    ttl = fake_swarm.config.workspace.lock_ttl_minutes * 60
    assert before + ttl - 5 <= lock["expires_at"] <= time.time() + ttl + 5
    event = file_events(fake_swarm, "file.locked")[-1]
    assert event["actor"] == ANNA and event["data"]["path"] == "report.md"
    assert event["data"]["owner"] == ANNA


async def test_lock_minutes_override_cap_and_validation(ws):
    ws.lock(ANNA, "a.txt", minutes=5)
    assert ws.locks()[0]["expires_at"] == pytest.approx(time.time() + 300, abs=5)
    ws.lock(ANNA, "b.txt", minutes=100_000)
    b = next(lk for lk in ws.locks() if lk["path"] == "b.txt")
    assert b["expires_at"] == pytest.approx(time.time() + 24 * 3600, abs=5)
    for bad in (0, -5, float("nan"), "soon"):
        with pytest.raises(ToolError, match="positive"):
            ws.lock(ANNA, "c.txt", minutes=bad)
    assert len(ws.locks()) == 2
    with pytest.raises(ToolError, match="whole workspace"):
        ws.lock(ANNA, ".")
    with pytest.raises(ToolError):
        ws.lock(ANNA, "../x.txt")


async def test_relock_by_same_actor_refreshes(ws, fake_swarm):
    ws.lock(ANNA, "r.md", minutes=1)
    first = ws.locks()[0]
    result = ws.lock(ANNA, "r.md", minutes=60)
    assert "refreshed" in result
    locks = ws.locks()
    assert len(locks) == 1
    assert locks[0]["acquired_at"] == first["acquired_at"]
    assert locks[0]["expires_at"] > first["expires_at"] + 50 * 60
    assert len(file_events(fake_swarm, "file.locked")) == 2


async def test_lock_conflicts_name_holder(ws):
    ws.lock(ANNA, "shared.md")
    with pytest.raises(ToolError, match=f"locked by {ANNA} until"):
        ws.lock(BORIS, "shared.md")
    with pytest.raises(ToolError, match=ANNA):
        ws.lock(BORIS, ".\\shared.md")
    ws.lock(ANNA, "src")
    with pytest.raises(ToolError, match="lock on 'src'"):
        ws.lock(BORIS, "src/main.py")
    ws.lock(BORIS, "lib/util.py")
    with pytest.raises(ToolError, match=BORIS):
        ws.lock(ANNA, "lib")


async def test_check_writable_and_write_blocked(ws):
    ws.lock(ANNA, "plan.md")
    ws.check_writable(ANNA, "plan.md")
    ws.check_writable(BORIS, "other.md")
    with pytest.raises(ToolError) as err:
        ws.check_writable(BORIS, "plan.md")
    msg = str(err.value)
    assert ANNA in msg and "until" in msg and "min left" in msg
    with pytest.raises(ToolError, match=ANNA):
        await ws.write(BORIS, "plan.md", "mine now")
    assert not (ws.root / "plan.md").exists()
    await ws.write(ANNA, "plan.md", "v1\n")
    with pytest.raises(ToolError, match=ANNA):
        await ws.edit(BORIS, "plan.md", "v1", "v2")
    await ws.edit(ANNA, "plan.md", "v1", "v2")


async def test_directory_lock_covers_descendants(ws):
    ws.lock(ANNA, "src")
    with pytest.raises(ToolError, match="lock on 'src'"):
        await ws.write(BORIS, "src/deep/file.py", "x")
    await ws.write(BORIS, "srcfile.py", "x")  # prefix but not inside
    await ws.write(ANNA, "src/deep/file.py", "x")


async def test_unlock_rules(ws, fake_swarm):
    ws.lock(ANNA, "x.md")
    with pytest.raises(ToolError, match=f"locked by {ANNA}"):
        ws.unlock(BORIS, "x.md")
    with pytest.raises(ToolError, match="Only the user"):
        ws.unlock(BORIS, "x.md", force=True)
    with pytest.raises(ToolError, match="force"):
        ws.unlock(USER, "x.md")
    assert ws.locks()
    assert ws.unlock(ANNA, "x.md") == "Unlocked x.md."
    assert ws.locks() == []
    event = file_events(fake_swarm, "file.unlocked")[-1]
    assert event["actor"] == ANNA and event["data"]["path"] == "x.md" and event["data"]["forced"] is False
    assert "not locked" in ws.unlock(ANNA, "x.md")

    ws.lock(BORIS, "y.md")
    result = ws.unlock(USER, "y.md", force=True)
    assert "was locked by Boris King" in result
    assert ws.locks() == []
    event = file_events(fake_swarm, "file.unlocked")[-1]
    assert event["actor"] == USER and event["data"]["owner"] == BORIS and event["data"]["forced"] is True


async def test_expired_locks_are_ignored_and_cleaned(ws, fake_swarm):
    ws.lock(ANNA, "old.md")
    fake_swarm.db.execute("UPDATE file_locks SET expires_at = ?", (time.time() - 1,))
    ws.check_writable(BORIS, "old.md")
    await ws.write(BORIS, "old.md", "free")
    assert fake_swarm.db.scalar("SELECT COUNT(*) FROM file_locks") == 0
    expired = [e for e in file_events(fake_swarm, "file.unlocked") if e["data"].get("expired")]
    assert len(expired) == 1 and expired[0]["data"]["owner"] == ANNA

    ws.lock(ANNA, "a.md")
    ws.lock(ANNA, "b.md")
    fake_swarm.db.execute("UPDATE file_locks SET expires_at = ? WHERE path = 'a.md'", (time.time() - 1,))
    assert [lk["path"] for lk in ws.locks()] == ["b.md"]
    assert ws.expire_locks() == 0
    fake_swarm.db.execute("UPDATE file_locks SET expires_at = ?", (time.time() - 1,))
    assert ws.expire_locks() == 1
    ws.lock(ANNA, "c.md")
    fake_swarm.db.execute("UPDATE file_locks SET expires_at = ?", (time.time() - 1,))
    ws.lock(BORIS, "c.md")  # an expired lock does not block others
    assert ws.locks()[0]["owner"] == BORIS


async def test_release_all(ws, fake_swarm):
    ws.lock(ANNA, "1.md")
    ws.lock(ANNA, "2.md")
    ws.lock(BORIS, "3.md")
    assert ws.release_all(ANNA) == 2
    assert [lk["path"] for lk in ws.locks()] == ["3.md"]
    assert ws.release_all(ANNA) == 0
    unlocked = file_events(fake_swarm, "file.unlocked")
    assert sorted(e["data"]["path"] for e in unlocked) == ["1.md", "2.md"]


# --- listing -----------------------------------------------------------------------------------------------

async def test_list_tree_text(ws):
    put(ws, "README.md", "x" * 1500)
    put(ws, "src/app.py", "print(1)\n")
    put(ws, "src/pkg/mod.py", "")
    put(ws, "src/pkg/deeper/z.py", "")
    (ws.root / "emptydir").mkdir()
    text = ws.list()
    lines = text.splitlines()
    assert lines[0] == "./"
    assert "  emptydir/ (empty)" in lines
    assert "  src/" in lines
    assert "    app.py (9 B)" in lines
    assert "    pkg/ (2 entries)" in lines  # beyond depth: not expanded, entry count shown
    assert "  README.md (1.5 KB)" in lines
    assert not any(line.strip().startswith(".git/") or line.strip() == ".git" for line in lines)
    assert any(line.startswith("  .gitignore (") for line in lines)
    assert lines.index("  src/") < lines.index("  README.md (1.5 KB)")  # dirs first
    deep = ws.list("src", depth=5)
    assert deep.splitlines()[0] == "src/"
    assert deep.splitlines() == ["src/", "  pkg/", "    deeper/", "      z.py (0 B)", "    mod.py (0 B)",
                                 "  app.py (9 B)"]
    assert ws.list("src/app.py") == "src/app.py (9 B)"
    shallow = ws.list(".", depth=1)
    assert "  src/ (2 entries)" in shallow.splitlines()


async def test_list_empty_missing_and_no_expand(ws):
    (ws.root / "void").mkdir()
    assert ws.list("void") == "void/\n  (empty)"
    with pytest.raises(ToolError, match="not found"):
        ws.list("ghost")
    put(ws, "node_modules/lib/index.js", "x")
    put(ws, "node_modules/other.js", "x")
    text = ws.list(".", depth=5)
    assert "  node_modules/ (2 entries)" in text.splitlines()
    assert "index.js" not in text


def test_only_redirecting_reparse_points_count_as_links():
    """OneDrive/cloud placeholders carry a reparse point too, but they are ordinary files/dirs."""

    class Entry:
        def __init__(self, tag):
            self.tag = tag

        def is_symlink(self):
            return False

        def stat(self, follow_symlinks=True):
            return SimpleNamespace(st_reparse_tag=self.tag, st_file_attributes=(
                stat.FILE_ATTRIBUTE_REPARSE_POINT | stat.FILE_ATTRIBUTE_DIRECTORY))

    assert Workspace._is_link(Entry(0x9000601A)) is False  # IO_REPARSE_TAG_CLOUD_6 (OneDrive)
    assert Workspace._is_link(Entry(stat.IO_REPARSE_TAG_MOUNT_POINT)) is True  # junction
    assert Workspace._is_link(Entry(stat.IO_REPARSE_TAG_SYMLINK)) is True


async def test_list_truncates(ws):
    for i in range(LIST_CAP + 20):
        put(ws, f"many/f{i:04d}.txt", "")
    text = ws.list("many")
    lines = text.splitlines()
    assert "truncated" in lines[-1]
    assert len(lines) == 1 + LIST_CAP + 1



async def test_tree_for_ui(ws):
    put(ws, "a/b/c.txt", "hello")
    put(ws, "top.md", "x")
    entries = ws.tree()
    by_path = {e["path"]: e for e in entries}
    assert set(by_path["a"]) == {"path", "type", "size", "mtime"}
    assert by_path["a"]["type"] == "dir"
    assert by_path["a/b/c.txt"] == {"path": "a/b/c.txt", "type": "file", "size": 5,
                                    "mtime": pytest.approx((ws.root / "a/b/c.txt").stat().st_mtime)}
    assert by_path["top.md"]["type"] == "file"
    assert not any(p == ".git" or p.startswith(".git/") for p in by_path)
    assert [e["path"] for e in ws.tree("a", depth=1)] == ["a/b"]
    assert ws.tree("top.md")[0]["path"] == "top.md"
    with pytest.raises(ToolError):
        ws.tree("missing")
    with pytest.raises(ToolError):
        ws.tree("../")


async def test_read_raw(ws):
    put(ws, "page.html", "<p>привет</p>")
    assert ws.read_raw("page.html") == ("<p>привет</p>", False)
    put(ws, "long.txt", "z" * 100)
    text, truncated = ws.read_raw("long.txt", max_bytes=10)
    assert text == "z" * 10 and truncated
    put(ws, "pic.bin", b"\x00\x01\x02" * 10)
    text, truncated = ws.read_raw("pic.bin")
    assert "binary" in text and not truncated
    with pytest.raises(ToolError, match="not found"):
        ws.read_raw("absent.txt")
    with pytest.raises(ToolError):
        ws.read_raw(".git/HEAD")
