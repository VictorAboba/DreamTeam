"""The folder a session works in (docs/SPEC.md §17.2 "Workspace folder"): moving a session's files to another
folder, adopting a folder of the user (an existing project), the safety rules, undo and restarts. Scripted
members write files through the real tools; git runs for real."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from dreamteam import sessions as sessions_mod
from dreamteam import settings as st
from dreamteam.config import Config
from dreamteam.docker import outside_home_warning
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.launcher_mcp import workspace_text
from dreamteam.models import USER, AgentKind
from dreamteam.sessions import LEGACY_ID, MOVE_JOURNAL, SessionError, SessionManager, external_workspaces
from dreamteam.shell import run_shell
from dreamteam.workspace import init_repo, repo_state

HAS_GIT = shutil.which("git") is not None
needs_git = pytest.mark.skipif(not HAS_GIT, reason="git is not installed")

CALC = '''
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("calc")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


if __name__ == "__main__":
    mcp.run()
'''


# ---- helpers ----------------------------------------------------------------------------------------
class Writers:
    """Scripted members: each LLM call writes the next queued file through the real write_file tool."""

    def __init__(self) -> None:
        self.queue: list[tuple[str, str]] = []

    def factory(self, profile, agent):
        def handler(system, messages, tools):
            if self.queue:
                path, content = self.queue.pop(0)
                return {"tool_calls": [{"name": "write_file", "arguments": {"path": path, "content": content}}]}
            return "Done."

        return ScriptedLLM(handler, profile)


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 60},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    })
    return home


def _manager(tmp_path: Path, writers: Writers | None = None) -> SessionManager:
    return SessionManager(_home(tmp_path), llm_factory=(writers or Writers()).factory)


def git(root: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8",
                          check=check)
    return proc.stdout


def _user_repo(path: Path, branch: str = "feature") -> Path:
    """A project of the user: a git repository with its own identity, one commit, on branch `branch`."""
    path.mkdir(parents=True)
    git(path, "init", "-q")
    for key, value in (("user.name", "Owner"), ("user.email", "owner@example.com"), ("core.autocrlf", "false")):
        git(path, "config", "--local", key, value)
    (path / "README.md").write_text("# My project\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "Initial")
    git(path, "checkout", "-q", "-b", branch)
    return path


def _commit(root: Path, message: str) -> None:
    git(root, "add", "-A")
    git(root, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", message)


def _halfway_merge(project: Path) -> None:
    """A merge that stopped on a conflict, resolved with "ours": nothing shows as changed, MERGE_HEAD is there."""
    git(project, "checkout", "-q", "-b", "other")
    (project / "README.md").write_text("# theirs\n", encoding="utf-8")
    _commit(project, "Theirs")
    git(project, "checkout", "-q", "feature")
    (project / "README.md").write_text("# ours\n", encoding="utf-8")
    _commit(project, "Ours")
    git(project, "-c", "commit.gpgsign=false", "merge", "-q", "other", check=False)
    git(project, "checkout", "--ours", "--", "README.md")
    git(project, "add", "README.md")
    assert git(project, "status", "--porcelain") == "" and (project / ".git" / "MERGE_HEAD").exists()


def _blob(root: Path, spec: str) -> bytes:
    """A file of a commit as bytes (text mode would turn CRLF into LF)."""
    return subprocess.run(["git", "-C", str(root), "cat-file", "-p", spec], capture_output=True, check=True).stdout


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _fake_forged(swarm, name: str) -> Path:
    """A forged MCP server's record and files (as MCPForge.create leaves them), without starting it."""
    folder = swarm.forge.server_dir(name)
    folder.mkdir(parents=True)
    (folder / "server.py").write_text("# a forged server\n", encoding="utf-8")
    swarm.db.execute(
        "INSERT INTO mcp_servers(name, owner, scope, description, command, cwd, requirements, origin, status, tools,"
        " created_at, updated_at)"
        " VALUES (?, 'user', 'swarm', 'test', '[]', ?, '[]', 'workspace', 'active', '[]', 0, 0)",
        (name, str(folder)))
    return folder


async def until(predicate, timeout: float = 30.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached in time")


async def agent_writes(manager: SessionManager, writers: Writers, sid: str, path: str, content: str = "x\n") -> str:
    """Wake the session's member, which writes `path` with write_file; wait for the file and its commit."""
    swarm = manager.swarms[sid]
    member = next(m["name"] for m in swarm.members() if m["kind"] == "llm")
    writers.queue.append((path, content))
    swarm.notify(member, "n_resumed")
    root = swarm.workspace.root
    await until(lambda: (root / path).exists() and not writers.queue)
    if swarm.workspace.git_enabled:
        await until(lambda: f"create {path}" in git(root, "log", "--format=%s"))
    await until(lambda: (swarm.member(member) or {}).get("status") == "idle")
    return member


def _notes(swarm, member: str) -> list[str]:
    return [r["kind"] for r in swarm.db.all("SELECT kind FROM notifications WHERE recipient = ?", (member,))]


def _link(link: Path, target: Path) -> None:
    if sys.platform == "win32":  # a junction needs no privileges (a symlink does)
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


# ---- move -------------------------------------------------------------------------------------------
@needs_git
async def test_move_to_a_new_folder_takes_the_files_history_and_mcp_servers(tmp_path):
    writers = Writers()
    manager = _manager(tmp_path, writers)
    events: list[dict] = []
    manager.listen(events.append)
    try:
        sid = (await manager.create("Move me"))["id"]
        swarm = manager.swarms[sid]
        default = manager.default_workspace(sid)
        agent = await agent_writes(manager, writers, sid, "before.txt", "made before\n")
        report = await swarm.forge.create(USER, "calc", "Adds numbers", CALC, save_to_library=False)
        assert swarm.forge.get("calc")["status"] == "active", report

        target = tmp_path / "elsewhere" / "project"  # missing, and so is its parent
        events.clear()
        result = await manager.set_workspace(sid, str(target))
        real = target.resolve()
        assert result["mode"] == "moved" and result["git"] == "moved" and result["mcp_moved"] == ["calc"]
        assert result["to"] == str(real) and result["old_kept"] is None and not result["old_missing"]
        assert result["restarted"] is True and result["start_error"] is None
        assert [e["change"] for e in events] == ["stopped", "updated", "started"]
        assert not default.exists()  # the session's own folder went away (it is recreated on demand)
        assert (real / "before.txt").read_text(encoding="utf-8") == "made before\n"
        assert (real / "mcp" / "calc" / "server.py").exists() and (real / ".git").is_dir()
        assert "create before.txt" in git(real, "log", "--format=%s")
        assert manager.summary(sid)["workspace"] == {"path": str(real), "default": False, "adopted": False,
                                                     "exists": True}
        assert manager.meta(sid)["workspace"] == {"path": str(real), "adopted": False}
        assert not (manager.dir_of(sid) / ".dreamteam" / MOVE_JOURNAL).exists()

        swarm = manager.swarms[sid]  # started again: a new swarm object that works in the new folder
        assert sid in manager.running and swarm.workspace.root == real
        assert swarm.snapshot()["swarm"]["workspace"] == {"path": str(real), "default": False, "adopted": False,
                                                           "exists": True}
        assert "n_workspace_moved" in _notes(swarm, agent)
        await agent_writes(manager, writers, sid, "after.txt")
        assert (real / "after.txt").exists() and not default.exists()
        log = git(real, "log", "--format=%an|%s")
        assert f"{agent}|create after.txt" in log and f"{agent}|create before.txt" in log
        text, is_error = await swarm.forge.call(USER, "calc", "add", {"a": 2, "b": 3})
        assert not is_error and "5" in text, text
        row = swarm.db.one("SELECT cwd FROM mcp_servers WHERE name = 'calc'")
        assert row["cwd"] == str(real / "mcp" / "calc")

        # back to its own folder: everything returns; the other folder is left empty
        result = await manager.set_workspace(sid, None)
        assert result["mode"] == "moved" and result["git"] == "moved" and result["old_kept"] is None
        assert (default / "after.txt").exists() and (default / "mcp" / "calc" / "server.py").exists()
        assert real.is_dir() and not any(real.iterdir())
        assert "workspace" not in manager.meta(sid) and manager.summary(sid)["workspace"]["default"] is True
        assert manager.swarms[sid].workspace.root.resolve() == default.resolve()
    finally:
        await manager.shutdown()


# ---- adopt ------------------------------------------------------------------------------------------
@needs_git
async def test_adopting_a_clean_repository_commits_to_its_current_branch(tmp_path):
    project = _user_repo(tmp_path / "project")
    config_before = git(project, "config", "--local", "--list")
    writers = Writers()
    manager = _manager(tmp_path, writers)
    try:
        sid = (await manager.create("Adopt"))["id"]
        default = manager.default_workspace(sid)
        await agent_writes(manager, writers, sid, "notes.txt")
        _fake_forged(manager.swarms[sid], "tool")

        result = await manager.set_workspace(sid, str(project))
        assert result["mode"] == "adopted" and result["git"] == "existing" and result["mcp_moved"] == ["tool"]
        assert result["old_kept"] == str(default.resolve()) and result["restarted"]
        assert (default / "notes.txt").exists()  # the session's old folder stays as it was
        assert not (default / "mcp" / "tool").exists() and (project / "mcp" / "tool" / "server.py").exists()
        info = manager.summary(sid)["workspace"]
        assert info["adopted"] is True and info["default"] is False and info["path"] == str(project.resolve())

        agent = await agent_writes(manager, writers, sid, "agent.txt", "from the agent\n")
        assert git(project, "rev-parse", "--abbrev-ref", "HEAD").strip() == "feature"
        log = git(project, "log", "--format=%an|%s", "feature").splitlines()
        assert log[0] == f"{agent}|create agent.txt" and log[-1] == "Owner|Initial"
        assert git(project, "config", "--local", "--list") == config_before  # the user's repo config is untouched
        assert not (project / ".gitignore").exists()
        assert "n_workspace_changed" in _notes(manager.swarms[sid], agent)

        # deleting the session never deletes the user's folder
        await manager.delete(sid)
        assert (project / "agent.txt").exists() and (project / "README.md").exists() and (project / ".git").is_dir()
    finally:
        await manager.shutdown()


@needs_git
async def test_a_dirty_or_odd_repository_is_refused_and_nothing_changes(tmp_path):
    project = _user_repo(tmp_path / "project")
    manager = _manager(tmp_path)
    try:
        sid = (await manager.create("Careful"))["id"]
        swarm = manager.swarms[sid]
        default = manager.default_workspace(sid)
        _fake_forged(swarm, "tool")

        async def refused(path: Path, status: int, words: str) -> None:
            with pytest.raises(SessionError) as err:
                await manager.set_workspace(sid, str(path))
            assert err.value.status == status and words in str(err.value), str(err.value)
            # nothing changed: not even stopped
            assert manager.swarms[sid] is swarm and sid in manager.running
            assert "workspace" not in manager.meta(sid) and (default / "mcp" / "tool").exists()
            assert not (project / "mcp").exists()

        (project / "README.md").write_text("changed by the user\n", encoding="utf-8")
        await refused(project, 409, "uncommitted")
        git(project, "checkout", "--", "README.md")
        (project / "new.txt").write_text("not added yet", encoding="utf-8")
        await refused(project, 409, "new.txt")
        (project / "new.txt").unlink()
        git(project, "checkout", "-q", "--detach")
        await refused(project, 409, "detached HEAD")
        git(project, "checkout", "-q", "feature")
        (project / "src").mkdir()
        (project / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        await refused(project / "src", 400, "inside the git repository")
        (project / "mcp" / "tool").mkdir(parents=True)  # the folder already has a server of that name
        (project / "mcp" / "tool" / "x.txt").write_text("mine", encoding="utf-8")
        git(project, "add", "-A")
        git(project, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "Mine")
        with pytest.raises(SessionError) as err:
            await manager.set_workspace(sid, str(project))
        assert err.value.status == 409 and "mcp/tool" in str(err.value)
        assert (default / "mcp" / "tool" / "server.py").exists() and manager.swarms[sid] is swarm
    finally:
        await manager.shutdown()


@needs_git
async def test_adopting_a_folder_without_git_initializes_a_repository(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    writers = Writers()
    manager = _manager(tmp_path, writers)
    try:
        sid = (await manager.create("Plain", start=False))["id"]
        result = await manager.set_workspace(sid, str(folder))  # a stopped session is just updated
        assert result["mode"] == "adopted" and result["git"] == "initialized" and result["restarted"] is False
        assert "data.csv" in git(folder, "ls-files") and (folder / ".gitignore").exists()
        assert git(folder, "log", "--format=%s").strip() == "Initialize workspace"
        await manager.start(sid)
        agent = await agent_writes(manager, writers, sid, "result.txt")
        assert f"{agent}|create result.txt" in git(folder, "log", "--format=%an|%s")
        # an adopted folder's repository gets no repo-local identity (the user's own commits stay theirs)
        assert git(folder, "config", "--local", "user.name", check=False) == ""

        # with git off in the session's settings, a folder is adopted without a repository
        other = (await manager.create("No git", start=False, overrides={"workspace": {"git": False}}))["id"]
        bare = tmp_path / "plain2"
        bare.mkdir()
        (bare / "a.txt").write_text("a", encoding="utf-8")
        result = await manager.set_workspace(other, str(bare))
        assert result["git"] == "disabled" and not (bare / ".git").exists()
    finally:
        await manager.shutdown()


# ---- refused targets ----------------------------------------------------------------------------------
async def test_refused_targets(tmp_path, monkeypatch):
    home = _home(tmp_path)
    manager = SessionManager(home, llm_factory=Writers().factory)
    sid = (await manager.create("Picky", start=False))["id"]
    other = (await manager.create("Other", start=False))["id"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "file.txt").write_text("x", encoding="utf-8")
    cases = [
        ("relative/folder", "absolute path"),
        (tmp_path.anchor, "root"),
        (str(Path.home()), "home folder"),
        (str(home), "DreamTeam home"),
        (str(tmp_path), "DreamTeam home"),  # contains the home
        (str(home / ".dreamteam" / "x"), "data folder"),
        (str(home / "sessions"), "sessions folder"),
        (str(home / "sessions" / other / "workspace"), "sessions folder"),
        (str(home / "sessions" / sid), "sessions folder"),
        (str(home / "sessions" / sid / ".dreamteam" / "w"), "sessions folder"),
        (str(home / "sessions" / sid / "workspace" / "sub"), "sessions folder"),
        (str(elsewhere / "file.txt"), "is a file"),
        (str(elsewhere / "file.txt" / "sub"), "cannot be created"),
        ("   ", "Give a folder"),
    ]
    if Path.home().parent.parent != Path.home().parent:  # e.g. C:\Users or /home (not the root itself)
        cases.append((str(Path.home().parent), "contains your home folder"))
    for path, words in cases:
        with pytest.raises(SessionError) as err:
            await manager.set_workspace(sid, path)
        assert err.value.status == 400 and words in str(err.value), (path, str(err.value))
    with pytest.raises(SessionError) as err:
        await manager.set_workspace("nope", str(elsewhere / "x"))
    assert err.value.status == 404
    link = tmp_path / "link-to-home"  # links are resolved: this is the home
    _link(link, home)
    with pytest.raises(SessionError) as err:
        await manager.set_workspace(sid, str(link / "sessions" / "x"))
    assert err.value.status == 400 and "sessions folder" in str(err.value)
    assert "workspace" not in manager.meta(sid) and manager.default_workspace(sid).is_dir()

    # the current folder: nothing to do
    result = await manager.set_workspace(sid, str(manager.default_workspace(sid)))
    assert result["mode"] == "unchanged" and result["restarted"] is False

    # a folder outside: inside it, containing it, or used by another session
    ext = tmp_path / "ext" / "work"
    assert (await manager.set_workspace(sid, str(ext)))["mode"] == "moved"
    for path, words in ((ext / "sub", "inside the session's current folder"),
                        (tmp_path / "ext", "contains the session's current folder")):
        with pytest.raises(SessionError) as err:
            await manager.set_workspace(sid, str(path))
        assert err.value.status == 400 and words in str(err.value), str(err.value)
    for path in (ext, ext / "deeper", tmp_path / "ext"):
        with pytest.raises(SessionError) as err:
            await manager.set_workspace(other, str(path))
        assert err.value.status == 409 and sid in str(err.value), str(err.value)
    assert (await manager.set_workspace(sid, str(ext)))["mode"] == "unchanged"

    def no_write(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(sessions_mod.tempfile, "mkstemp", no_write)
    with pytest.raises(SessionError) as err:
        await manager.set_workspace(other, str(tmp_path / "locked" / "w"))
    assert err.value.status == 400 and "Cannot write" in str(err.value)
    assert not (tmp_path / "locked").exists() and "workspace" not in manager.meta(other)


async def test_the_legacy_main_session_cannot_move(tmp_path):
    from dreamteam.db import Database

    home = _home(tmp_path)
    Database(home / ".dreamteam" / "swarm.db").close()
    manager = SessionManager(home, llm_factory=Writers().factory)
    with pytest.raises(SessionError) as err:
        await manager.set_workspace(LEGACY_ID, str(tmp_path / "x"))
    assert err.value.status == 409 and "dreamteam run" in str(err.value)
    summary = manager.summary(LEGACY_ID)
    assert summary["workspace"]["default"] and summary["workspace"]["path"] == str(home.resolve() / "workspace")
    # nor can another session take the main session's folder
    sid = (await manager.create("Next", start=False))["id"]
    with pytest.raises(SessionError) as err:
        await manager.set_workspace(sid, str(home / "workspace"))
    assert err.value.status == 409 and LEGACY_ID in str(err.value)


# ---- failures, missing folders, races ----------------------------------------------------------------
@needs_git
async def test_a_move_that_fails_midway_is_undone_and_the_session_restarted(tmp_path, monkeypatch):
    writers = Writers()
    manager = _manager(tmp_path, writers)
    try:
        sid = (await manager.create("Fragile"))["id"]
        default = manager.default_workspace(sid)
        await agent_writes(manager, writers, sid, "a.txt")
        for name in ("b.txt", "c.txt", "d.txt"):
            (default / name).write_text(name, encoding="utf-8")
        before = sorted(p.name for p in default.iterdir())
        head = git(default, "rev-parse", "HEAD")
        swarm = manager.swarms[sid]
        real_move = sessions_mod._move_path
        calls: list[str] = []

        def flaky(src: Path, dst: Path, done=None) -> None:
            calls.append(src.name)
            if len(calls) == 3:  # two entries moved already
                raise OSError(28, "No space left on device")
            real_move(src, dst, done)

        monkeypatch.setattr(sessions_mod, "_move_path", flaky)
        target = tmp_path / "new" / "place"
        with pytest.raises(SessionError) as err:
            await manager.set_workspace(sid, str(target))
        assert err.value.status == 500 and "Nothing was changed" in str(err.value), str(err.value)
        assert str(default.resolve()) in str(err.value)
        assert sorted(p.name for p in default.iterdir()) == before and git(default, "rev-parse", "HEAD") == head
        assert not (tmp_path / "new").exists()  # the folders the move created are gone too
        assert "workspace" not in manager.meta(sid)
        assert not (manager.dir_of(sid) / ".dreamteam" / MOVE_JOURNAL).exists()
        assert sid in manager.running and manager.swarms[sid] is not swarm  # running again, in its old folder
        await agent_writes(manager, writers, sid, "still.txt")
        assert (default / "still.txt").exists()

        # a file in use: 409, nothing changed
        calls.clear()

        def in_use(src: Path, dst: Path, done=None) -> None:
            calls.append(src.name)
            if len(calls) == 2:
                raise PermissionError(13, "The process cannot access the file", str(src))
            real_move(src, dst, done)

        monkeypatch.setattr(sessions_mod, "_move_path", in_use)
        with pytest.raises(SessionError) as err:
            await manager.set_workspace(sid, str(target))
        assert err.value.status == 409 and "in use" in str(err.value)
        assert sorted(p.name for p in default.iterdir()) == sorted(before + ["still.txt"])
    finally:
        await manager.shutdown()


@needs_git
async def test_when_the_undo_fails_too_the_error_and_the_next_start_say_where_the_files_are(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    try:
        sid = (await manager.create("Stuck"))["id"]
        default = manager.default_workspace(sid)
        for name in ("a.txt", "b.txt", "c.txt"):
            (default / name).write_text(name, encoding="utf-8")
        real_move, real_back = sessions_mod._move_path, sessions_mod._move_back
        calls: list[str] = []

        def flaky(src: Path, dst: Path, done=None) -> None:
            calls.append(src.name)
            if src.name == "b.txt":  # after a.txt (and .gitignore, .scratch) moved: entries move in name order
                raise OSError(5, "I/O error")
            real_move(src, dst, done)

        def stuck_back(moved: Path, original: Path, copied: bool = False) -> None:
            if moved.name == "a.txt":
                raise OSError(5, "I/O error")
            real_back(moved, original, copied)

        monkeypatch.setattr(sessions_mod, "_move_path", flaky)
        monkeypatch.setattr(sessions_mod, "_move_back", stuck_back)
        target = tmp_path / "half"
        with pytest.raises(SessionError) as err:
            await manager.set_workspace(sid, str(target))
        text = str(err.value)
        assert err.value.status == 500 and str(target.resolve() / "a.txt") in text and "by hand" in text, text
        journal = manager.dir_of(sid) / ".dreamteam" / MOVE_JOURNAL
        assert journal.exists() and (target / "a.txt").exists() and not (default / "a.txt").exists()
        assert sid not in manager.running  # it cannot start with its files in two places
        with pytest.raises(SessionError) as err:
            await manager.start(sid)
        assert err.value.status == 409 and str(target.resolve()) in str(err.value) and str(journal) in str(err.value)
        with pytest.raises(SessionError) as err:
            await manager.set_workspace(sid, str(tmp_path / "third"))
        assert err.value.status == 409
        monkeypatch.undo()
        os.replace(target / "a.txt", default / "a.txt")  # the user puts the files together and deletes the journal
        journal.unlink()
        await manager.start(sid)
        assert sid in manager.running and (default / "a.txt").exists()
    finally:
        await manager.shutdown()


@needs_git
async def test_a_missing_folder_is_a_clear_error_at_start_and_cd_fixes_it(tmp_path):
    manager = _manager(tmp_path)
    try:
        sid = (await manager.create("Usb", start=False))["id"]
        ext = tmp_path / "usb" / "work"
        await manager.set_workspace(sid, str(ext))
        sessions_mod._rmtree(tmp_path / "usb")  # the drive is unplugged
        assert manager.summary(sid)["workspace"]["exists"] is False
        with pytest.raises(SessionError) as err:
            await manager.start(sid)
        assert err.value.status == 409 and str(ext.resolve()) in str(err.value) and "/cd" in str(err.value)
        assert not ext.exists() and sid not in manager.running  # never created silently
        await manager.load(sid)  # the board can still be viewed
        result = await manager.set_workspace(sid, None)
        assert result["mode"] == "moved" and result["old_missing"] is True and result["old_kept"] is None
        swarm = await manager.start(sid)
        assert swarm.workspace.root.resolve() == manager.default_workspace(sid).resolve()
        assert (swarm.workspace.root / ".git").is_dir()
    finally:
        await manager.shutdown()


async def test_an_external_workspace_is_never_created_by_the_swarm(tmp_path):
    from dreamteam.swarm import Swarm

    missing = tmp_path / "gone" / "work"
    cfg = Config.from_dict(tmp_path / "project", {"swarm": {"start_agents": 0}, "workspace": {"git": False}})
    cfg.workspace_path = missing
    swarm = Swarm(config=cfg, llm_factory=Writers().factory)
    try:
        with pytest.raises(FileNotFoundError):
            await swarm.start()
        assert not missing.exists() and swarm.config.workspace_dir == missing
    finally:
        await swarm.stop()
        swarm.close()


@needs_git
async def test_concurrent_stop_and_cd_end_consistently(tmp_path):
    manager = _manager(tmp_path)
    try:
        sid = (await manager.create("Race"))["id"]
        target = tmp_path / "race-target"
        results = await asyncio.gather(manager.set_workspace(sid, str(target)), manager.stop(sid),
                                       return_exceptions=True)
        assert not any(isinstance(r, BaseException) for r in results), results
        assert sid not in manager.running and manager.summary(sid)["workspace"]["path"] == str(target.resolve())
        swarm = await manager.start(sid)
        assert swarm.workspace.root == target.resolve()
        # an update of the overrides keeps the loaded swarm in its folder
        manager.update(sid, overrides={"swarm": {"max_cost_usd": 3.0}})
        assert swarm.config.workspace_dir == target.resolve() and swarm.config.swarm.max_cost_usd == 3.0
        swarm.reconfigure(Config.from_dict(manager.dir_of(sid), {}))
        assert swarm.config.workspace_dir == target.resolve()
    finally:
        await manager.shutdown()


# ---- create, persistence, sandbox -----------------------------------------------------------------------
@needs_git
async def test_create_a_session_in_a_folder(tmp_path):
    manager = _manager(tmp_path)
    try:
        target = tmp_path / "fresh"
        summary = await manager.create("Fresh", workspace=str(target), start=False)
        result = summary["workspace_result"]
        assert result["mode"] == "created" and result["git"] == "initialized" and result["old_kept"] is None
        assert summary["workspace"] == {"path": str(target.resolve()), "default": False, "adopted": False,
                                        "exists": True}
        assert not manager.default_workspace(summary["id"]).exists() and (target / ".git").is_dir()

        project = _user_repo(tmp_path / "proj")
        (project / "x.txt").write_text("untracked", encoding="utf-8")
        for bad, status in ((str(project), 409), ("relative", 400), (str(target), 409)):
            with pytest.raises(SessionError) as err:
                await manager.create("Bad", workspace=bad)
            assert err.value.status == status, str(err.value)
        assert manager.ids() == [summary["id"]]  # nothing half-created
        (project / "x.txt").unlink()
        adopted = await manager.create("Proj", "Improve it", workspace=str(project))
        result = adopted["workspace_result"]
        assert result["mode"] == "adopted" and result["git"] == "existing" and result["old_kept"] is None
        assert adopted["status"] == "running" and adopted["workspace"]["adopted"] is True
        assert not manager.default_workspace(adopted["id"]).exists()
        assert manager.swarms[adopted["id"]].workspace.root == project.resolve()
    finally:
        await manager.shutdown()


async def test_session_json_workspace_and_the_sandbox_warning(tmp_path):
    manager = _manager(tmp_path)
    inside = (await manager.create("Inside", start=False))["id"]
    outside = (await manager.create("Outside", start=False))["id"]
    target = tmp_path / "out"
    await manager.set_workspace(outside, str(target))
    in_home = manager.home / "projects" / "p"  # a folder inside the home (not in sessions/) is allowed
    await manager.set_workspace(inside, str(in_home))
    assert manager.meta(inside)["workspace"] == {"path": str(in_home.resolve()), "adopted": False,
                                                 "in_home": "projects/p"}
    assert "in_home" not in manager.meta(outside)["workspace"]
    assert external_workspaces(manager.home) == [(outside, str(target.resolve()))]
    warning = "\n".join(outside_home_warning(manager.home))
    assert outside in warning and str(target.resolve()) in warning and inside not in warning

    # the home where the Docker sandbox mounts it (or moved by the user): a folder inside it is found there
    await manager.shutdown()
    mounted = tmp_path / "mounted-home"
    shutil.copytree(manager.home, mounted)
    there = SessionManager(mounted, llm_factory=Writers().factory)
    try:
        assert there.summary(inside)["workspace"] == {"path": str(mounted / "projects" / "p"), "default": False,
                                                      "adopted": False, "exists": True}
        assert there.config_for(inside).workspace_dir == mounted / "projects" / "p"
        swarm = await there.start(inside)
        assert swarm.workspace.root == mounted / "projects" / "p"
    finally:
        await there.shutdown()
    cfg = manager.config_for(outside)
    assert cfg.workspace_path == target.resolve() and cfg.workspace_dir == target.resolve()
    assert cfg.data_dir == manager.dir_of(outside) / ".dreamteam" and cfg.workspace_adopted is False
    # a stored path that is not absolute here (a host path read inside the container) is just unavailable
    path = manager.dir_of(outside) / "session.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["workspace"]["path"] = "relative/on/this/system"
    path.write_text(json.dumps(meta), encoding="utf-8")
    info = manager.summary(outside)["workspace"]
    assert info == {"path": "relative/on/this/system", "default": False, "adopted": False, "exists": False}
    with pytest.raises(SessionError) as err:
        await manager.start(outside)
    assert err.value.status == 409 and "relative/on/this/system" in str(err.value)
    # a damaged value
    meta["workspace"] = "somewhere"
    path.write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(SessionError) as err:
        manager.summary(outside)
    assert err.value.status == 500
    assert any(s["id"] == outside and "error" in s for s in manager.list())
    await manager.delete(outside)  # a damaged session can still be deleted; its folder stays
    assert target.is_dir()
    await manager.shutdown()


# ---- git probe and stopped commands --------------------------------------------------------------------
@needs_git
def test_repo_state(tmp_path):
    project = _user_repo(tmp_path / "project")
    state = repo_state(project)
    assert Path(state["toplevel"]).resolve() == project.resolve() and state["branch"] == "feature"
    assert state["changes"] == [] and state["error"] is None and state["operation"] is None and not state["git_dir"]
    (project / "new.txt").write_text("x", encoding="utf-8")
    assert repo_state(project)["changes"] == ["?? new.txt"]
    # the user's display settings cannot hide from the check what the agents' `git add -A` would commit
    git(project, "config", "--local", "status.showUntrackedFiles", "no")
    assert git(project, "status", "--porcelain") == "" and repo_state(project)["changes"] == ["?? new.txt"]
    (project / "mcp" / "tool").mkdir(parents=True)  # left out: e.g. the session's own MCP servers
    (project / "mcp" / "tool" / "server.py").write_text("x", encoding="utf-8")
    assert repo_state(project, exclude=["mcp/tool"])["changes"] == ["?? new.txt"]
    shutil.rmtree(project / "mcp")
    (project / "new.txt").unlink()
    (project / "sub").mkdir()
    assert Path(repo_state(project / "sub")["toplevel"]).resolve() == project.resolve()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert repo_state(plain) == {"toplevel": None, "git_dir": False, "branch": None, "operation": None, "changes": [],
                                 "error": None}
    # a repository's own database is not a folder of files
    git(tmp_path, "init", "-q", "--bare", "bare.git")
    assert repo_state(tmp_path / "bare.git")["git_dir"] is True and repo_state(project / ".git")["git_dir"] is True
    # a merge stopped halfway: the next commit would conclude it
    _halfway_merge(project)
    state = repo_state(project)
    assert state["changes"] == [] and state["operation"] == "merge"


async def test_a_stopped_agent_command_is_killed(tmp_path):
    started, late = tmp_path / "started.txt", tmp_path / "late.txt"
    (tmp_path / "sleeper.py").write_text(
        f"import os, pathlib, time\npathlib.Path({str(started)!r}).write_text(str(os.getpid()))\ntime.sleep(4)\n"
        f"pathlib.Path({str(late)!r}).write_text('late')\n", encoding="utf-8")
    env = {**os.environ, "PATH": os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", ""),
           "PYEXE": sys.executable}
    program, command = ("cmd", "python sleeper.py") if sys.platform == "win32" else ("sh", '"$PYEXE" sleeper.py')
    task = asyncio.create_task(run_shell(command, cwd=tmp_path, timeout=60, program=program, env=env))
    await until(lambda: started.exists() and started.read_text(encoding="utf-8").strip(), 60)  # the child runs
    pid = int(started.read_text(encoding="utf-8"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await until(lambda: not _alive(pid), 10)  # killed, not left running in the background
    assert not late.exists()  # the command did not go on writing after its agent stopped


@needs_git
async def test_the_target_is_checked_again_once_the_session_stopped(tmp_path):
    """Between the first check and the stop an agent may forge a server whose name the adopted folder already has:
    the second check (with nothing running) refuses, and the session runs again in its old folder."""
    project = _user_repo(tmp_path / "project")
    (project / "mcp" / "late").mkdir(parents=True)
    (project / "mcp" / "late" / "server.py").write_text("# the user's own\n", encoding="utf-8")
    git(project, "add", "-A")
    git(project, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "Own server")
    manager = _manager(tmp_path)
    try:
        sid = (await manager.create("Late"))["id"]
        swarm = manager.swarms[sid]
        events: list[str] = []
        manager.listen(lambda e: events.append(e["change"]))
        real = manager._forged_servers

        def forged(session: str) -> list[str]:
            folder = swarm.forge.server_dir("late")
            if session == sid and "stopped" in events and not folder.exists():
                folder.mkdir(parents=True)  # what an agent forged just before the stop
                (folder / "server.py").write_text("# forged\n", encoding="utf-8")
                conn = sqlite3.connect(manager.dir_of(sid) / ".dreamteam" / "swarm.db")
                with conn:
                    conn.execute("INSERT INTO mcp_servers(name, owner, origin, status) VALUES "
                                 "('late', 'user', 'workspace', 'active')")
                conn.close()
            return real(session)

        manager._forged_servers = forged
        with pytest.raises(SessionError) as err:
            await manager.set_workspace(sid, str(project))
        assert err.value.status == 409 and "mcp/late" in str(err.value)
        assert events == ["stopped", "started"] and sid in manager.running and manager.swarms[sid] is not swarm
        assert "workspace" not in manager.meta(sid)
        assert (manager.default_workspace(sid) / "mcp" / "late" / "server.py").exists()
    finally:
        await manager.shutdown()


# ---- two sessions, foreign files, undo ----------------------------------------------------------------------
@needs_git
async def test_two_sessions_moving_into_one_folder_at_once_cannot_share_it(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    a = (await manager.create("A", start=False))["id"]
    b = (await manager.create("B", start=False))["id"]
    heads = {}
    for sid in (a, b):
        folder = manager.default_workspace(sid)
        (folder / "README.md").write_text(f"from {sid}\n", encoding="utf-8")
        (folder / f"only-{sid}.txt").write_text(sid, encoding="utf-8")
        assert init_repo(folder) is None  # each with its own history
        heads[sid] = git(folder, "rev-parse", "HEAD")
    real_move = sessions_mod._move_path

    def slow(src: Path, dst: Path, done=None) -> None:  # a big move: the other session's checks run meanwhile
        time.sleep(0.05)
        real_move(src, dst, done)

    monkeypatch.setattr(sessions_mod, "_move_path", slow)
    target = tmp_path / "shared"
    results = await asyncio.gather(manager.set_workspace(a, str(target)), manager.set_workspace(b, str(target)),
                                   return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["SessionError", "dict"], results
    winner, loser = (a, b) if isinstance(results[0], dict) else (b, a)
    won, error = (results[0], results[1]) if winner == a else (results[1], results[0])
    assert won["mode"] == "moved" and won["git"] == "moved"
    assert error.status == 409 and f"of session {winner}" in str(error) and "cannot share" in str(error), str(error)
    real = target.resolve()
    assert sorted(p.name for p in real.iterdir()) == sorted([".git", ".gitignore", "README.md", f"only-{winner}.txt"])
    assert (real / "README.md").read_text(encoding="utf-8") == f"from {winner}\n"
    assert git(real, "rev-parse", "HEAD") == heads[winner]
    own = manager.default_workspace(loser)  # the other session's files are where they were, as they were
    assert sorted(p.name for p in own.iterdir()) == sorted([".git", ".gitignore", "README.md", f"only-{loser}.txt"])
    assert (own / "README.md").read_text(encoding="utf-8") == f"from {loser}\n"
    assert git(own, "rev-parse", "HEAD") == heads[loser]
    assert "workspace" not in manager.meta(loser) and manager.meta(winner)["workspace"]["path"] == str(real)
    assert not (manager.dir_of(loser) / ".dreamteam" / MOVE_JOURNAL).exists()

    # two sessions created in one new folder at once: one of them is, and its repository is whole
    fresh = tmp_path / "fresh"
    results = await asyncio.gather(manager.create("C", start=False, workspace=str(fresh)),
                                   manager.create("D", start=False, workspace=str(fresh)), return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["SessionError", "dict"], results
    created = next(r for r in results if isinstance(r, dict))
    error = next(r for r in results if not isinstance(r, dict))
    assert error.status == 409 and MOVE_JOURNAL not in str(error), str(error)
    assert sorted(manager.ids()) == sorted([a, b, created["id"]])
    assert git(fresh, "log", "--format=%s").strip() == "Initialize workspace"
    git(fresh, "fsck", "--no-progress")  # (check=True: a repository the loser's undo damaged fails here)
    assert (fresh / ".git" / "description").exists() and (fresh / ".git" / "hooks").is_dir()
    await manager.shutdown()


@needs_git
async def test_a_file_that_appears_in_the_target_is_never_overwritten_or_deleted(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    sid = (await manager.create("Notes", start=False))["id"]
    default = manager.default_workspace(sid)
    for name, text in (("a.txt", "a"), ("notes.txt", "SESSION NOTES"), ("z.txt", "z")):
        (default / name).write_text(text, encoding="utf-8")
    target = tmp_path / "target"
    real_carry_out = SessionManager._carry_out

    def user_saves_meanwhile(self, session, plan, undo):  # e.g. a sync tool, right after the checks
        target.mkdir(exist_ok=True)
        (target / "notes.txt").write_text("USER FILE", encoding="utf-8")
        return real_carry_out(self, session, plan, undo)

    monkeypatch.setattr(SessionManager, "_carry_out", user_saves_meanwhile)
    with pytest.raises(SessionError) as err:
        await manager.set_workspace(sid, str(target))
    text = str(err.value)
    assert err.value.status == 409 and "Nothing was changed" in text and "notes.txt" in text, text
    assert sorted(p.name for p in target.iterdir()) == ["notes.txt"]
    assert (target / "notes.txt").read_text(encoding="utf-8") == "USER FILE"
    assert sorted(p.name for p in default.iterdir()) == ["a.txt", "notes.txt", "z.txt"]
    assert (default / "notes.txt").read_text(encoding="utf-8") == "SESSION NOTES"
    assert "workspace" not in manager.meta(sid) and not (manager.dir_of(sid) / ".dreamteam" / MOVE_JOURNAL).exists()


def test_moves_never_replace_or_remove_what_they_did_not_make(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    (src / "f.txt").write_text("mine", encoding="utf-8")
    taken = tmp_path / "taken"
    taken.mkdir()
    (taken / "keep.txt").write_text("theirs", encoding="utf-8")
    real_rename = os.rename

    def cross_device(a, b):  # another volume: a copy and a delete
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(sessions_mod.os, "rename", cross_device)
    done: list = []
    with pytest.raises(FileExistsError):
        sessions_mod._move_path(src, taken, done)
    assert sorted(p.name for p in taken.iterdir()) == ["keep.txt"] and (src / "f.txt").exists() and done == []
    copied = tmp_path / "copied"
    sessions_mod._move_path(src, copied, done)  # recorded once the copy is complete
    assert done == [(src, copied, True)] and not src.exists() and (copied / "f.txt").exists()
    monkeypatch.setattr(sessions_mod.os, "rename", real_rename)
    # after a rename, something new at the original place is not the move's: the undo leaves both alone
    (tmp_path / "x.txt").write_text("moved", encoding="utf-8")
    sessions_mod._move_path(tmp_path / "x.txt", tmp_path / "y.txt", done)
    assert done[-1] == (tmp_path / "x.txt", tmp_path / "y.txt", False)
    (tmp_path / "x.txt").write_text("new", encoding="utf-8")
    with pytest.raises(FileExistsError):
        sessions_mod._move_back(tmp_path / "y.txt", tmp_path / "x.txt", False)
    assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "new"
    assert (tmp_path / "y.txt").read_text(encoding="utf-8") == "moved"


@needs_git
async def test_the_mode_the_user_agreed_to_is_checked(tmp_path):
    manager = _manager(tmp_path)
    try:
        sid = (await manager.create("Agreed"))["id"]
        swarm = manager.swarms[sid]
        full = tmp_path / "full"
        full.mkdir()
        (full / "x.txt").write_text("x", encoding="utf-8")
        with pytest.raises(SessionError) as err:
            await manager.set_workspace(sid, str(full), expect="moved")
        assert err.value.status == 409 and "now holds files" in str(err.value)
        assert manager.swarms[sid] is swarm and sid in manager.running  # refused before the stop
        with pytest.raises(SessionError) as err:
            await manager.set_workspace(sid, str(tmp_path / "x"), expect="sideways")
        assert err.value.status == 400
        # files that appear while the session stops: the second check finds an adopt, not the move the first found
        empty = tmp_path / "empty"
        empty.mkdir()
        events: list[str] = []
        manager.listen(lambda e: events.append(e["change"]))
        real = manager._forged_servers

        def meanwhile(session: str) -> list[str]:
            if "stopped" in events and not (empty / "late.txt").exists():
                (empty / "late.txt").write_text("the user's", encoding="utf-8")
            return real(session)

        manager._forged_servers = meanwhile
        with pytest.raises(SessionError) as err:
            await manager.set_workspace(sid, str(empty))
        assert err.value.status == 409 and "now holds files" in str(err.value)
        assert sorted(p.name for p in empty.iterdir()) == ["late.txt"] and sid in manager.running
        assert "workspace" not in manager.meta(sid)
    finally:
        await manager.shutdown()


@needs_git
async def test_a_create_that_fails_in_its_folder_says_what_is_left_there(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    target = tmp_path / "proj"

    def failing_init(root, git=None, *, adopted=False):
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text("x", encoding="utf-8")
        return "fatal: something went wrong"

    real_remove = sessions_mod._remove

    def stuck(path: Path) -> None:
        if path.name == ".git":
            raise OSError(f"could not remove {path} completely (a file in it is in use?)")
        real_remove(path)

    monkeypatch.setattr(sessions_mod, "init_repo", failing_init)
    monkeypatch.setattr(sessions_mod, "_remove", stuck)
    with pytest.raises(SessionError) as err:
        await manager.create("Broken", workspace=str(target))
    text = str(err.value)
    assert "the session was not created" in text and str(target.resolve() / ".git") in text and "by hand" in text
    assert MOVE_JOURNAL not in text and "sessions" not in text, text  # no advice about a session that is gone
    assert manager.ids() == []
    monkeypatch.setattr(sessions_mod, "_remove", real_remove)
    monkeypatch.setattr(sessions_mod, "init_repo", lambda root, git=None, adopted=False: "fatal: no")
    sessions_mod._rmtree(target)
    with pytest.raises(SessionError) as err:
        await manager.create("Broken", workspace=str(target))
    assert "the session was not created and the folder is as it was" in str(err.value) and not target.exists()
    assert manager.ids() == []


# ---- git rules of an adopted folder ----------------------------------------------------------------------------
@needs_git
async def test_git_internals_a_halfway_merge_and_the_mcp_library_are_refused(tmp_path):
    project = _user_repo(tmp_path / "project")
    git(tmp_path, "init", "-q", "--bare", "bare.git")
    library = tmp_path / "library"
    (library / "calc").mkdir(parents=True)
    manager = _manager(tmp_path)
    sid = (await manager.create("Careful", start=False))["id"]
    nogit = (await manager.create("No git", start=False, overrides={"workspace": {"git": False}}))["id"]
    for session in (sid, nogit):  # whether git is on or off
        for path in (tmp_path / "bare.git", project / ".git", project / ".git" / "refs"):
            with pytest.raises(SessionError) as err:
                await manager.set_workspace(session, str(path))
            assert err.value.status == 400 and "own database" in str(err.value), (path, str(err.value))
        for path in (library, library / "calc"):
            with pytest.raises(SessionError) as err:
                await manager.set_workspace(session, str(path))
            assert err.value.status == 400 and "MCP library" in str(err.value), (path, str(err.value))
    assert not (tmp_path / "bare.git" / ".git").exists() and not (library / ".git").exists()
    _halfway_merge(project)
    with pytest.raises(SessionError) as err:
        await manager.set_workspace(sid, str(project))
    assert err.value.status == 409 and "merge is in progress" in str(err.value), str(err.value)
    assert (project / ".git" / "MERGE_HEAD").exists() and git(project, "status", "--porcelain") == ""
    assert "workspace" not in manager.meta(sid) and "workspace" not in manager.meta(nogit)


@needs_git
async def test_an_adopted_folder_is_checked_again_at_every_start(tmp_path):
    project = _user_repo(tmp_path / "project")
    writers = Writers()
    manager = _manager(tmp_path, writers)
    again = None
    try:
        sid = (await manager.create("Adopter", start=False, workspace=str(project)))["id"]
        swarm = await manager.start(sid)
        _fake_forged(swarm, "tool")  # its own MCP server in the folder, not committed yet: no reason to refuse
        await manager.stop(sid)
        await manager.start(sid)
        await manager.stop(sid)
        head = git(project, "rev-parse", "HEAD")
        # the user works in the project while the session is stopped
        (project / "README.md").write_text("# My project, edited\n", encoding="utf-8")
        with pytest.raises(SessionError) as err:
            await manager.start(sid)
        assert err.value.status == 409 and "uncommitted" in str(err.value) and "README.md" in str(err.value)
        assert "mcp" not in str(err.value) and sid not in manager.running
        git(project, "checkout", "--", "README.md")
        git(project, "checkout", "-q", "--detach")
        with pytest.raises(SessionError) as err:
            await manager.start(sid)
        assert err.value.status == 409 and "detached HEAD" in str(err.value)
        git(project, "checkout", "-q", "feature")
        await manager.start(sid)
        await manager.shutdown()  # the session stays marked for resume
        (project / "wip.txt").write_text("mine", encoding="utf-8")
        again = SessionManager(manager.home, llm_factory=writers.factory)
        assert await again.resume() == [] and sid not in again.running  # (logged: the same clear error)
        assert git(project, "rev-parse", "HEAD") == head

        # adopted while git was off: turning git on applies at the next start, which checks the folder
        dirty = _user_repo(tmp_path / "dirty")
        (dirty / "README.md").write_text("changed\n", encoding="utf-8")
        (dirty / "wip.txt").write_text("mine", encoding="utf-8")
        created = await again.create("No git", overrides={"workspace": {"git": False}}, workspace=str(dirty))
        other = created["id"]
        assert created["workspace_result"]["git"] == "disabled" and other in again.running
        running = again.swarms[other]
        again.update(other, overrides={})  # git on (the global settings)
        assert running.config.workspace.git is False and not running.workspace.git_enabled  # not under its agents
        await again.stop(other)
        with pytest.raises(SessionError) as err:
            await again.start(other)
        assert err.value.status == 409 and "uncommitted" in str(err.value) and "wip.txt" in str(err.value)
        assert git(dirty, "log", "--format=%an|%s").strip() == "Owner|Initial"

        # a folder inside a repository, adopted while git was off: no repository is created inside it
        outer = _user_repo(tmp_path / "outer")
        (outer / "docs").mkdir()
        (outer / "docs" / "a.md").write_text("a\n", encoding="utf-8")
        _commit(outer, "Docs")
        nested = (await again.create("Nested", start=False, overrides={"workspace": {"git": False}},
                                     workspace=str(outer / "docs")))["id"]
        again.update(nested, overrides={})
        with pytest.raises(SessionError) as err:
            await again.start(nested)
        assert err.value.status == 409 and "inside the git repository" in str(err.value), str(err.value)
        assert not (outer / "docs" / ".git").exists() and git(outer, "status", "--porcelain") == ""
    finally:
        await manager.shutdown()
        if again is not None:
            await again.shutdown()


@needs_git
async def test_the_scratch_folder_does_not_block_the_next_start(tmp_path):
    project = _user_repo(tmp_path / "project")
    manager = _manager(tmp_path)
    try:
        sid = (await manager.create("Adopter", start=False, workspace=str(project)))["id"]
        swarm = await manager.start(sid)
        await swarm.workspace.write(swarm.members()[0]["name"], ".scratch/notes/todo.txt", "temp\n")
        await manager.stop(sid)
        # an agent's last shell command removed the marker: `.scratch/` is the agents' folder, not the user's work
        (project / ".scratch" / ".gitignore").unlink()
        assert git(project, "status", "--porcelain") == "?? .scratch/\n"
        await manager.start(sid)
        assert git(project, "status", "--porcelain") == ""  # the start wrote the marker again
        assert (project / ".scratch" / "notes" / "todo.txt").read_text(encoding="utf-8") == "temp\n"
    finally:
        await manager.shutdown()


@needs_git
async def test_an_adopted_repository_keeps_the_users_line_endings(tmp_path):
    origin = _user_repo(tmp_path / "origin")
    git(origin, "config", "--local", "core.autocrlf", "true")  # as Git for Windows' system config has it
    (origin / "app.py").write_bytes(b"a = 1\nb = 2\nc = 3\n")
    (origin / "other.py").write_bytes(b"x = 1\ny = 2\n")
    _commit(origin, "Code")
    for name in ("app.py", "other.py", "README.md"):
        (origin / name).unlink()
    git(origin, "checkout", "--", ".")  # checked out again: CRLF in the folder, LF in the repository
    assert (origin / "app.py").read_bytes() == b"a = 1\r\nb = 2\r\nc = 3\r\n"
    project = tmp_path / "project"
    shutil.copytree(origin, project)  # a copy (or a re-save): the index's file stamps are stale
    assert repo_state(project)["changes"] == []  # clean, as the user's own `git status` says
    assert git(project, "status", "--porcelain") == ""
    manager = _manager(tmp_path)
    try:
        sid = (await manager.create("Lines", start=False, workspace=str(project)))["id"]
        swarm = await manager.start(sid)
        await swarm.workspace.edit("Ava", "app.py", "b = 2", "b = 22")
        assert _blob(project, "HEAD:app.py") == b"a = 1\nb = 22\nc = 3\n"  # the user's LF, one line changed
        assert git(project, "diff", "--numstat", "HEAD~1", "HEAD").split() == ["1", "1", "app.py"]
        os.utime(project / "other.py")  # touched, not changed
        (project / "note.txt").write_text("hi\n", encoding="utf-8")
        assert await swarm.workspace.autocommit("Ava", "shell: echo hi > note.txt")
        assert git(project, "diff", "--name-only", "HEAD~1", "HEAD").split() == ["note.txt"]
        assert git(project, "status", "--porcelain") == ""
        assert git(project, "config", "--local", "core.autocrlf").strip() == "true"  # never touched
    finally:
        await manager.shutdown()


@needs_git
async def test_leaving_an_adopted_project_takes_the_mcp_servers_and_leaves_it_clean(tmp_path):
    project = _user_repo(tmp_path / "project")
    manager = _manager(tmp_path)
    try:
        sid = (await manager.create("Tools", start=False))["id"]
        _fake_forged(await manager.load(sid), "tool")
        result = await manager.set_workspace(sid, str(project))
        assert result["mode"] == "adopted" and result["mcp_moved"] == ["tool"] and result["old_changed"] is None
        swarm = await manager.start(sid)
        assert await swarm.workspace.autocommit("Ava", "shell: ls")  # the agents' commit takes it into the branch
        assert "mcp/tool/server.py" in git(project, "ls-files")
        await manager.stop(sid)

        result = await manager.set_workspace(sid, None)  # back to its own folder: the server goes along
        assert result["mcp_moved"] == ["tool"] and result["old_kept"] == str(project.resolve())
        changed = result["old_changed"]
        assert changed["paths"] == ["mcp/tool"] and changed["commit"] and changed["error"] is None, changed
        assert git(project, "status", "--porcelain") == ""  # the user's repository is clean
        assert git(project, "rev-parse", "HEAD").strip() == changed["commit"]
        assert git(project, "log", "-1", "--format=%an|%s").startswith("DreamTeam|Remove mcp/tool")
        assert git(project, "branch", "--show-current").strip() == "feature"
        assert (manager.default_workspace(sid) / "mcp" / "tool" / "server.py").exists()
        assert "their removal is committed there" in workspace_text(result)

        # and it can come back
        result = await manager.set_workspace(sid, str(project))
        assert result["mode"] == "adopted" and result["mcp_moved"] == ["tool"], result
        swarm = await manager.start(sid)
        await swarm.workspace.autocommit("Ava", "shell: ls")
        await manager.stop(sid)
        # leaving while the user's repository is on a detached HEAD: no commit there, and the result says so
        git(project, "checkout", "-q", "--detach")
        result = await manager.set_workspace(sid, None)
        changed = result["old_changed"]
        assert changed["commit"] is None and "detached HEAD" in changed["error"], changed
        assert git(project, "status", "--porcelain").strip() == "D mcp/tool/server.py"
        assert "not committed there" in workspace_text(result)
    finally:
        await manager.shutdown()


@needs_git
async def test_a_session_can_return_to_a_folder_whose_repository_tracks_its_mcp_servers(tmp_path):
    project = _user_repo(tmp_path / "project")
    manager = _manager(tmp_path)
    try:
        sid = (await manager.create("Return", start=False))["id"]
        _fake_forged(await manager.load(sid), "tool")
        own = tmp_path / "own"
        assert (await manager.set_workspace(sid, str(own)))["mode"] == "moved"
        await manager.start(sid)
        assert "mcp/tool/server.py" in git(own, "ls-files")  # the server is tracked in the session's repository
        assert (await manager.set_workspace(sid, str(project)))["mcp_moved"] == ["tool"]
        assert git(own, "status", "--porcelain").strip() == "D mcp/tool/server.py"  # left without the server

        # going back: its absence is not "uncommitted changes" (the move brings the server back)
        result = await manager.set_workspace(sid, str(own))
        assert result["mode"] == "adopted" and result["mcp_moved"] == ["tool"] and result["restarted"], result
        assert git(own, "status", "--porcelain") == "" and (own / "mcp" / "tool" / "server.py").exists()
        assert manager.swarms[sid].workspace.root.resolve() == own.resolve()

        # any other change there is still refused
        assert (await manager.set_workspace(sid, str(project)))["mode"] == "adopted"
        (own / "notes.txt").write_text("the user's own notes\n", encoding="utf-8")
        with pytest.raises(SessionError) as err:
            await manager.set_workspace(sid, str(own))
        assert err.value.status == 409 and "notes.txt" in str(err.value) and "mcp/tool" not in str(err.value)
        assert manager.swarms[sid].workspace.root.resolve() == project.resolve()
    finally:
        await manager.shutdown()


# ---- delete, sandbox, external members -------------------------------------------------------------------------
@needs_git
async def test_delete_names_the_folder_it_kept_as_it_was_when_it_deleted(tmp_path):
    manager = _manager(tmp_path)
    away = (await manager.create("Away", start=False))["id"]
    target = tmp_path / "away"
    await manager.set_workspace(away, str(target))
    (target / "data.txt").write_text("x", encoding="utf-8")
    # a /cd back runs first (it has the session's lock): the files go back into the session, which is deleted
    moved, kept = await asyncio.gather(manager.set_workspace(away, None), manager.delete(away))
    assert moved["mode"] == "moved" and kept is None and not any(target.iterdir())
    here = (await manager.create("Here", start=False))["id"]
    other = tmp_path / "other"
    moved, kept = await asyncio.gather(manager.set_workspace(here, str(other)), manager.delete(here))
    assert kept == str(other.resolve()) and (other / ".git").is_dir() and manager.ids() == []
    await manager.shutdown()


async def test_external_members_hear_of_the_new_folder_too(tmp_path):
    manager = _manager(tmp_path)
    try:
        sid = (await manager.create("Mixed"))["id"]
        swarm = manager.swarms[sid]
        ext = await swarm.hire(USER, "Reviewer", "", kind=AgentKind.EXTERNAL)
        llm = next(m["name"] for m in swarm.members() if m["kind"] == "llm")
        target = tmp_path / "elsewhere"
        result = await manager.set_workspace(sid, str(target))
        swarm = manager.swarms[sid]
        assert result["restarted"] and "n_workspace_moved" in _notes(swarm, llm)
        assert "n_workspace_moved" in _notes(swarm, ext) and swarm.has_pending(ext)  # its wait_for_events returns
        text = swarm.render_wake(ext, incoming=True)
        assert text and str(target.resolve()) in text
    finally:
        await manager.shutdown()
