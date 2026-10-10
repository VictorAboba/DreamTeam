"""The agents' scratch folder and git identity (docs/SPEC.md §7 "Scratch folder"): the workspace helpers,
`Workspace.ensure_scratch`, the autocommit that never commits `.scratch/`, listings, and the `shell` tool's
environment."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from dreamteam import workspace as workspace_mod
from dreamteam.config import Config
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import AgentKind
from dreamteam.shell import resolve_shell
from dreamteam.swarm import Swarm
from dreamteam.tools import ToolContext, call_tool
from dreamteam.workspace import (GIT_LOCATION_VARS, SCRATCH_DIR, SCRATCH_GITIGNORE, Workspace, agent_git_env,
                                 git_identity, in_scratch, porcelain_entries, scratch_rel)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

ANNA = "Anna Smith"
MARKER = SCRATCH_GITIGNORE.encode("utf-8")


@pytest.fixture
def ws(fake_swarm) -> Workspace:
    w = Workspace(fake_swarm)
    fake_swarm.workspace = w
    return w


@pytest.fixture
async def repo(ws) -> Workspace:
    """An initialized workspace: a repository DreamTeam created (git on, the fake swarm's default)."""
    await ws.init()
    return ws


def marker(ws: Workspace) -> Path:
    return ws.root / SCRATCH_DIR / ".gitignore"


def put(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    return p


def git(root: Path, *args: str) -> str:
    """git as the user runs it in the folder (their own configuration; a fixed identity for their commits)."""
    proc = subprocess.run(["git", "-C", str(root), "-c", "user.name=Owner", "-c", "user.email=owner@example.com",
                           "-c", "commit.gpgsign=false", "-c", "core.quotepath=false", *args],
                          capture_output=True, env=workspace_mod.git_env())
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return proc.stdout.decode("utf-8")


def commits(root: Path) -> list[tuple[str, str, list[str]]]:
    """[(author, subject, [files])], newest first."""
    result = []
    for chunk in git(root, "log", "--name-only", "--format=@@%an|%s").split("@@")[1:]:
        head, *files = [line for line in chunk.splitlines() if line.strip()]
        author, subject = head.split("|", 1)
        result.append((author, subject, files))
    return result


def user_repo(fake_swarm, files: dict[str, str]) -> Path:
    """The workspace folder as a repository of the user with `files` committed, adopted by the session."""
    root = fake_swarm.config.workspace_dir
    git(root, "init", "-q")
    for rel, text in files.items():
        put(root, rel, text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "the user's work")
    fake_swarm.config.workspace_adopted = True
    return root


def symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symbolic links are not available here: {exc}")


def make_dir_link(link: Path, target: Path) -> None:
    """Directory symlink, or a junction on Windows without symlink privileges (as in tests/test_workspace.py)."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
    if not link.exists():
        pytest.skip("neither symlinks nor junctions are available")


# ------------------------------------------------------------------------------------- helpers
def test_helpers():
    assert scratch_rel("Анна Смирнова") == ".scratch/anna.smirnova/"
    assert scratch_rel(ANNA) == ".scratch/anna.smith/"
    for rel in (".scratch", ".scratch/", ".scratch/a", ".scratch/anna.smith/t.py"):
        assert in_scratch(rel), rel
    for rel in (".scratch2/a", ".scratch-old", "a/.scratch/x", "scratch/a", "."):
        assert not in_scratch(rel), rel
    assert in_scratch(".SCRATCH/a") is workspace_mod._WINDOWS    # paths compare like the file system does
    assert git_identity(None) == ("DreamTeam", "dreamteam@dreamteam.local")
    assert git_identity("Анна Смирнова") == ("Анна Смирнова", "anna.smirnova@dreamteam.local")
    assert porcelain_entries(b"R  new\0old\0?? x/\0") == [("R ", "new"), ("R ", "old"), ("??", "x")]
    assert porcelain_entries(b" M a b.txt\0 R moved\0orig\0A  c\0") == [
        (" M", "a b.txt"), (" R", "moved"), (" R", "orig"), ("A ", "c")]
    assert porcelain_entries(b"") == []


def test_agent_git_env_points_git_at_the_workspace_as_the_agent():
    env = {"PATH": "/bin", "GIT_DIR": "/elsewhere/.git", "git_work_tree": "/elsewhere", "GIT_INDEX_FILE": "i",
           "GIT_SSH_COMMAND": "ssh -i key", "GIT_AUTHOR_NAME": "Owner", "GIT_COMMITTER_EMAIL": "owner@example.com"}
    out = agent_git_env(env, "Анна Смирнова")
    assert not {k.upper() for k in out} & GIT_LOCATION_VARS
    assert out["PATH"] == "/bin" and out["GIT_SSH_COMMAND"] == "ssh -i key"
    assert {k: out[k] for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")} == {
        "GIT_AUTHOR_NAME": "Анна Смирнова", "GIT_AUTHOR_EMAIL": "anna.smirnova@dreamteam.local",
        "GIT_COMMITTER_NAME": "Анна Смирнова", "GIT_COMMITTER_EMAIL": "anna.smirnova@dreamteam.local"}
    assert env["GIT_DIR"] == "/elsewhere/.git"     # the caller's mapping is not changed


# ------------------------------------------------------------------------------ ensure_scratch
def test_ensure_scratch_creates_the_marker_and_the_member_folder(ws):
    ws.ensure_scratch()
    assert marker(ws).read_bytes() == MARKER
    assert [p.name for p in (ws.root / SCRATCH_DIR).iterdir()] == [".gitignore"]
    ws.ensure_scratch(ANNA)
    assert (ws.root / scratch_rel(ANNA)).is_dir()
    (ws.root / scratch_rel(ANNA) / "keep.txt").write_text("mine", encoding="utf-8")
    ws.ensure_scratch(ANNA)                                    # idempotent: nothing is emptied
    assert (ws.root / scratch_rel(ANNA) / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_ensure_scratch_restores_a_deleted_or_changed_marker(ws):
    ws.ensure_scratch()
    marker(ws).unlink()
    ws.ensure_scratch()
    assert marker(ws).read_bytes() == SCRATCH_GITIGNORE.encode("utf-8")
    marker(ws).write_text("!important.txt\n", encoding="utf-8")
    ws.ensure_scratch(ANNA)
    assert marker(ws).read_bytes() == SCRATCH_GITIGNORE.encode("utf-8")


def test_ensure_scratch_leaves_a_scratch_file_alone(ws, caplog):
    (ws.root / SCRATCH_DIR).write_text("not a folder", encoding="utf-8")
    ws.ensure_scratch(ANNA)
    assert (ws.root / SCRATCH_DIR).read_text(encoding="utf-8") == "not a folder"


def test_ensure_scratch_logs_what_it_cannot_do(ws, caplog):
    ws.ensure_scratch()
    (ws.root / scratch_rel(ANNA).rstrip("/")).write_text("a file where the folder goes", encoding="utf-8")
    ws.ensure_scratch(ANNA)                                    # no exception: best effort
    assert "cannot prepare" in caplog.text


def test_ensure_scratch_does_not_follow_links_elsewhere(ws, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    make_dir_link(ws.root / SCRATCH_DIR, outside)
    ws.ensure_scratch(ANNA)
    assert list(outside.iterdir()) == []


def test_ensure_scratch_never_writes_through_a_linked_marker(ws, tmp_path):
    ws.ensure_scratch()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    marker(ws).unlink()
    symlink_or_skip(marker(ws), victim)
    ws.ensure_scratch(ANNA)
    assert victim.read_text(encoding="utf-8") == "keep me"
    assert (ws.root / scratch_rel(ANNA)).is_dir()


@needs_git
def test_the_marker_hides_the_folder_from_git(ws):
    proc = workspace_mod.run_git(ws.root, "init", "-q")
    assert proc.returncode == 0, workspace_mod.git_output(proc)
    ws.ensure_scratch(ANNA)
    (ws.root / scratch_rel(ANNA) / "check.py").write_text("print(1)\n", encoding="utf-8")
    (ws.root / "out.txt").write_text("result\n", encoding="utf-8")
    status = subprocess.run(["git", "-C", str(ws.root), "status", "--porcelain", "--untracked-files=all"],
                            capture_output=True, check=True, env=workspace_mod.git_env())
    assert status.stdout.decode("utf-8").splitlines() == ["?? out.txt"]


# ------------------------------------------------------------------------------------- init
async def test_init_creates_the_scratch_folder_without_git(make_fake_swarm):
    swarm = make_fake_swarm({"workspace": {"git": False}})
    w = Workspace(swarm)
    await w.init()
    assert marker(w).read_bytes() == MARKER
    assert not (w.root / ".git").exists()


async def test_init_of_a_missing_outside_folder_creates_nothing(fake_swarm, tmp_path):
    fake_swarm.config.workspace_path = tmp_path / "unplugged"  # a folder of the user that is gone (/cd)
    w = Workspace(fake_swarm)
    with pytest.raises(FileNotFoundError):
        await w.init()
    assert not (tmp_path / "unplugged").exists()


# ------------------------------------------------------------------------------- autocommit
@needs_git
async def test_scratch_files_are_never_autocommitted(repo):
    put(repo.root, scratch_rel(ANNA) + "t.py", "print(1)\n")
    put(repo.root, "out.txt", "result\n")
    assert await repo.autocommit(ANNA, "shell: run")
    assert commits(repo.root)[0] == (ANNA, "shell: run", ["out.txt"])
    assert git(repo.root, "ls-files", SCRATCH_DIR) == ""


@needs_git
async def test_only_scratch_changes_make_no_commit(repo):
    before = commits(repo.root)
    put(repo.root, ".scratch/x.txt", "tmp")
    assert await repo.autocommit(ANNA, "shell: tmp") is None
    assert commits(repo.root) == before


@needs_git
async def test_autocommit_restores_a_deleted_marker(repo):
    marker(repo).unlink()  # e.g. an agent emptied the folder, dot files included
    put(repo.root, ".scratch/x.txt", "tmp")
    put(repo.root, "a.txt", "a")
    assert await repo.autocommit(ANNA, "shell: clean up")
    assert marker(repo).read_bytes() == MARKER
    assert commits(repo.root)[0] == (ANNA, "shell: clean up", ["a.txt"])
    assert git(repo.root, "status", "--porcelain") == ""


@needs_git
async def test_autocommit_excludes_the_folder_even_without_its_marker(repo, monkeypatch):
    marker(repo).unlink()
    monkeypatch.setattr(repo, "ensure_scratch", lambda actor=None: None)  # the marker cannot be written back
    put(repo.root, ".scratch/x.txt", "tmp")
    put(repo.root, ".scratch2/kept.txt", "an ordinary folder")
    put(repo.root, "a.txt", "a")
    assert await repo.autocommit(ANNA, "shell: run")
    assert commits(repo.root)[0] == (ANNA, "shell: run", [".scratch2/kept.txt", "a.txt"])
    assert git(repo.root, "ls-files", SCRATCH_DIR) == ""
    assert await repo.autocommit(ANNA, "shell: again") is None  # what is left is only .scratch/


@needs_git
async def test_autocommit_unstages_force_added_scratch(repo):
    put(repo.root, ".scratch/x.txt", "tmp")
    git(repo.root, "add", "-f", ".scratch/x.txt")  # an agent's manual `git add -f`
    assert await repo.autocommit(ANNA, "shell: add") is None  # nothing else changed: no commit at all
    assert git(repo.root, "diff", "--cached", "--name-only") == ""
    git(repo.root, "add", "-f", ".scratch/x.txt")
    put(repo.root, "a.txt", "a")
    assert await repo.autocommit(ANNA, "shell: add again")
    assert commits(repo.root)[0] == (ANNA, "shell: add again", ["a.txt"])
    assert git(repo.root, "diff", "--cached", "--name-only") == ""
    assert git(repo.root, "ls-files", SCRATCH_DIR) == ""


@needs_git
async def test_tracked_scratch_file_stays_as_committed(fake_swarm):
    root = user_repo(fake_swarm, {".scratch/keep.txt": "the user's\n", "b.txt": "b\n"})  # the user's own convention
    ws = Workspace(fake_swarm)
    await ws.init()
    put(root, ".scratch/keep.txt", "changed by an agent\n")
    git(root, "add", "-f", ".scratch/keep.txt")
    put(root, "b.txt", "b2\n")
    assert await ws.autocommit(ANNA, "shell: edit")
    assert commits(root)[0] == (ANNA, "shell: edit", ["b.txt"])
    assert git(root, "show", "HEAD:.scratch/keep.txt") == "the user's\n"
    assert git(root, "ls-files", SCRATCH_DIR) == ".scratch/keep.txt\n"


@needs_git
async def test_write_and_edit_into_scratch_do_not_commit(repo, fake_swarm):
    before = commits(repo.root)
    path = scratch_rel(ANNA) + "c.py"
    result = await repo.write(ANNA, path, "print(1)\n")
    assert result == f"Created {path} (1 line, 9 B) (in .scratch/: not committed)."
    result = await repo.edit(ANNA, path, "print(1)", "print(2)")
    assert result == f"Edited {path}: replaced 1 occurrence (in .scratch/: not committed)."
    assert (repo.root / path).read_text(encoding="utf-8") == "print(2)\n"
    assert commits(repo.root) == before
    changed = [e["data"] for e in fake_swarm.bus.recent(types=("file.changed",), limit=10)]
    assert changed == [{"path": path, "op": "create"}, {"path": path, "op": "edit"}]
    assert (await repo.write(ANNA, "a.txt", "a\n")).startswith("Created a.txt (1 line, 2 B), commit ")


@needs_git
async def test_adopted_repo_keeps_its_config_and_gitignore(fake_swarm):
    root = user_repo(fake_swarm, {"README.md": "mine\n"})
    config = (root / ".git" / "config").read_bytes()
    ws = Workspace(fake_swarm)
    await ws.init()
    assert marker(ws).read_bytes() == MARKER
    assert git(root, "status", "--porcelain") == ""  # the marker hides the folder: the repository looks clean
    await ws.write(ANNA, "src/app.py", "print(1)\n")
    put(root, scratch_rel(ANNA) + "check.py", "print(2)\n")
    put(root, "out.txt", "x\n")
    assert await ws.autocommit(ANNA, "shell: run")
    assert (root / ".git" / "config").read_bytes() == config
    assert not (root / ".gitignore").exists()
    assert git(root, "status", "--porcelain") == ""
    assert [c[2] for c in commits(root)[:2]] == [["out.txt"], ["src/app.py"]]


@needs_git
async def test_writing_over_the_marker_puts_it_back(fake_swarm):
    root = user_repo(fake_swarm, {"README.md": "mine\n"})
    ws = Workspace(fake_swarm)
    await ws.init()
    await ws.write(ANNA, ".scratch/notes/todo.txt", "temp\n")
    # an agent "tidies" the folder's .gitignore: without the marker the whole folder would show as untracked
    assert (await ws.write(ANNA, ".scratch/.gitignore", "*.tmp\n")).endswith("(in .scratch/: not committed).")
    assert marker(ws).read_bytes() == MARKER
    await ws.edit(ANNA, ".scratch/.gitignore", "*", "!*")
    assert marker(ws).read_bytes() == MARKER
    assert git(root, "status", "--porcelain") == ""


@needs_git
async def test_scratch_is_left_alone_when_it_is_a_file(repo):
    shutil.rmtree(repo.root / SCRATCH_DIR)
    (repo.root / SCRATCH_DIR).write_text("not a folder", encoding="utf-8")
    put(repo.root, "a.txt", "a")
    assert await repo.autocommit(ANNA, "shell: run")  # its ensure_scratch() leaves the file as it is
    assert (repo.root / SCRATCH_DIR).read_text(encoding="utf-8") == "not a folder"
    assert commits(repo.root)[0] == (ANNA, "shell: run", ["a.txt"])
    assert git(repo.root, "ls-files", SCRATCH_DIR) == ""


# ---------------------------------------------------------------------------------- listings
def test_listings_collapse_the_scratch_folder(ws):
    ws.ensure_scratch(ANNA)
    for name in ("t.py", "out.txt"):
        put(ws.root, scratch_rel(ANNA) + name, "x")
    put(ws.root, "src/app.py", "print(1)\n")
    deep = ws.list(".", depth=5).splitlines()
    assert "  .scratch/ (2 entries)" in deep  # the marker and anna.smith/
    assert "    app.py (9 B)" in deep and not any("t.py" in line for line in deep)
    assert ws.list(scratch_rel(ANNA)).splitlines() == [".scratch/anna.smith/", "  out.txt (1 B)", "  t.py (1 B)"]
    inside = ws.list(SCRATCH_DIR).splitlines()  # listed by its path, the folder itself expands
    assert inside[:4] == [".scratch/", "  anna.smith/", "    out.txt (1 B)", "    t.py (1 B)"]
    assert inside[4].startswith("  .gitignore (")
    assert scratch_rel(ANNA) + "t.py" in {e["path"] for e in ws.tree()}  # the board's file tree shows everything


# ------------------------------------------------------------------------------ shell tool
def swarm_config(tmp_path: Path, git_on: bool) -> Config:
    return Config.from_dict(tmp_path / "project", {
        "swarm": {"name": "Scratch swarm", "language": "en", "start_agents": 1, "max_agents": 2, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0},
        "workspace": {"git": git_on},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    })


@pytest.fixture
async def make_swarm(tmp_path):
    started: list[Swarm] = []

    async def factory(git_on: bool = False) -> Swarm:
        swarm = Swarm(config=swarm_config(tmp_path, git_on),
                      llm_factory=lambda profile, agent: ScriptedLLM(lambda *a: "ok", profile))
        await swarm.start(run_agents=False)
        started.append(swarm)
        return swarm

    yield factory
    for swarm in started:
        await swarm.stop()
        swarm.close()


def agent_of(swarm: Swarm) -> str:
    return next(m["name"] for m in swarm.members() if m["kind"] == AgentKind.LLM)


async def shell(swarm: Swarm, actor: str, command: str) -> str:
    text, is_error = await call_tool(ToolContext(swarm, actor), "shell", {"command": command})
    assert not is_error and text.startswith("exit code: 0"), text
    return text


def python(swarm: Swarm, script: str) -> str:
    """A command line that runs `script` with this interpreter in the agents' shell."""
    program = f"'{sys.executable}' {script}"
    return f"& {program}" if resolve_shell(swarm.config.shell.program)[0] in ("powershell", "pwsh") else program


async def test_shell_env_has_the_agent_git_identity_and_no_git_dir(make_swarm, monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/elsewhere/.git")  # inherited by the server: would point git at another repo
    monkeypatch.setenv("GIT_WORK_TREE", "/elsewhere")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Owner")
    swarm = await make_swarm()
    agent = agent_of(swarm)
    keys = ["GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_DIR",
            "GIT_WORK_TREE", "DREAMTEAM_SCRATCH"]
    put(swarm.workspace.root, "env_check.py",
        f"import json, os\nprint(json.dumps({{k: os.environ.get(k) for k in {keys!r}}}))\n")
    seen = json.loads((await shell(swarm, agent, python(swarm, "env_check.py"))).splitlines()[-1])
    name, email = git_identity(agent)
    assert seen.pop("DREAMTEAM_SCRATCH") == str(swarm.workspace.root / scratch_rel(agent))
    assert seen == {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
                    "GIT_COMMITTER_EMAIL": email, "GIT_DIR": None, "GIT_WORK_TREE": None}


async def test_shell_creates_the_agent_scratch_folder(make_swarm):
    swarm = await make_swarm()
    agent = agent_of(swarm)
    folder = swarm.workspace.root / scratch_rel(agent)
    assert marker(swarm.workspace).is_file() and not folder.exists()  # init made the folder, not the members'
    await shell(swarm, agent, "echo hi")
    assert folder.is_dir()


@needs_git
async def test_manual_commit_in_shell_is_attributed_to_the_agent(make_swarm):
    swarm = await make_swarm(git_on=True)
    agent = agent_of(swarm)
    root = swarm.workspace.root
    git(root, "config", "--local", "user.name", "Owner")  # the identity variables beat the repository's config
    git(root, "config", "--local", "user.email", "owner@example.com")
    await shell(swarm, agent, "git commit -q --allow-empty -m manual")
    name, email = git_identity(agent)
    assert git(root, "log", "-1", "--format=%an|%ae|%cn|%ce|%s").strip() == f"{name}|{email}|{name}|{email}|manual"
