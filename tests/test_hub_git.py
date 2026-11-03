"""DreamTeam's own git in folders the members can write (docs/SPEC.md §7 "DreamTeam's own git", hubgit.py): what a
member plants in a repository through its shell (hooks, an fsmonitor command, filter, textconv and diff drivers, ssh
and credential helpers, an included config file, config-based hooks, a submodule's own filter) never runs in the
autocommits, the commits of write_file, the session checks of a folder of the user and the initial commit of an
adopted folder; the environment of that git is filtered of secrets like the agents', whatever shell.pass_env lets
through to them; the user's own (global) filters keep working; a filter the repository requires but DreamTeam cannot
run leaves its files out of DreamTeam's commits. Real git throughout; the user's own git configuration is never
touched (HOME points at a folder of the test where a global config is needed)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from dreamteam import hubgit, netoff
from dreamteam import workspace as workspace_mod
from dreamteam.shell import secret_free_env
from dreamteam.workspace import Workspace, commit_paths, init_repo, repo_state, run_git

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

ANNA = "Anna Smith"
FAKE_ANTHROPIC = "sk-ant-api03-FAKE-hubgit-test-0000"
FAKE_OPENAI = "sk-FAKE-openai-hubgit-test-1111"
FAKE_CUSTOM = "FAKE-custom-key-of-a-profile-2222"


@pytest.fixture(autouse=True)
def _fresh_scans():
    hubgit.forget()
    yield
    hubgit.forget()


def git(root: Path, *args: str, check: bool = True) -> str:
    """git as the user (or a member's shell) runs it in the folder: the planting side, nothing hardened."""
    proc = subprocess.run(["git", "-C", str(root), "-c", "safe.directory=*", "-c", "user.name=Owner",
                           "-c", "user.email=owner@example.com", "-c", "commit.gpgsign=false", *args],
                          capture_output=True, env=workspace_mod.git_env(), stdin=subprocess.DEVNULL, timeout=120)
    if check:
        assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return proc.stdout.decode("utf-8", "replace")


def put(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def marker_cmd(marker: Path, tail: str = "cat") -> str:
    """A command git runs through sh (also Git for Windows'): it leaves `marker`, then passes stdin through."""
    return f"echo ran >> '{marker.as_posix()}'; {tail}"


def plant_hooks(root: Path, marker: Path) -> None:
    hooks = root / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("pre-commit", "commit-msg", "post-commit", "reference-transaction", "post-index-change"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\necho {name} >> '{marker.as_posix()}'\nexit 0\n", encoding="utf-8",
                        newline="\n")
        hook.chmod(0o755)


def plant_config(root: Path, marks: Path) -> None:
    """Everything a member can name in .git/config for git to run, each leaving its own marker in `marks`."""
    put(root, ".gitattributes", "*.md filter=probe diff=probe\n*.txt merge=probe\n")
    git(root, "config", "core.fsmonitor", marker_cmd(marks / "fsmonitor", "exit 1"))
    git(root, "config", "filter.probe.clean", marker_cmd(marks / "clean"))
    git(root, "config", "filter.probe.smudge", marker_cmd(marks / "smudge"))
    git(root, "config", "diff.probe.textconv", marker_cmd(marks / "textconv"))
    git(root, "config", "diff.probe.command", marker_cmd(marks / "diffcmd"))
    git(root, "config", "diff.external", marker_cmd(marks / "external"))
    git(root, "config", "merge.probe.driver", marker_cmd(marks / "merge"))
    git(root, "config", "core.sshCommand", marker_cmd(marks / "ssh"))
    git(root, "config", "credential.helper", f"!{marker_cmd(marks / 'credential')}")
    git(root, "config", "core.pager", marker_cmd(marks / "pager"))
    git(root, "config", "core.editor", marker_cmd(marks / "editor"))
    # a config-based hook (git 2.54+; older git ignores it)
    git(root, "config", "hook.probe.command", marker_cmd(marks / "confighook", "true"))
    git(root, "config", "--add", "hook.probe.event", "post-commit")
    git(root, "config", "--add", "hook.probe.event", "reference-transaction")
    # an included file of the member's own: a filter driver defined only there
    included = marks.parent / "included.gitconfig"
    included.write_text(f"[filter \"inc\"]\n\tclean = \"{marker_cmd(marks / 'included')}\"\n", encoding="utf-8")
    git(root, "config", "include.path", included.as_posix())
    with open(root / ".gitattributes", "a", encoding="utf-8") as f:
        f.write("*.inc filter=inc\n")
    plant_hooks(root, marks / "hooks")


def ran(marks: Path) -> list[str]:
    return sorted(p.name for p in marks.iterdir()) if marks.is_dir() else []


def racy(path: Path, text: str, index: Path) -> None:
    """Rewrite `path` with as many bytes and the index's own mtime: git must look at the content (through the
    clean filter) to know whether it changed."""
    path.write_bytes(text.encode("utf-8"))
    now = time.time()
    os.utime(path, (now, now))
    os.utime(index, (now, now))


# ---- the environment and the fixed settings ----------------------------------------------------------------------
def test_the_environment_has_no_secret_and_refuses_every_transport(monkeypatch):
    from dreamteam.config import Config

    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_ANTHROPIC)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI)
    monkeypatch.setenv("DEEPSEEK_LOGIN", FAKE_CUSTOM)   # a name no pattern matches, named by a profile below
    monkeypatch.setenv("MY_OTHER_LOGIN", FAKE_CUSTOM)   # one passed explicitly
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    monkeypatch.setenv("DREAMTEAM_OFFLINE_USER", "1:1")
    monkeypatch.setenv("PLAIN_SETTING", "kept")
    Config.from_dict(Path("p"), {"models": {"default": {"provider": "openai", "model": "m",
                                                      "api_key_env": "$DEEPSEEK_LOGIN"}}})
    env = hubgit.environment(["my_other_login"])
    for value in (FAKE_ANTHROPIC, FAKE_OPENAI, FAKE_CUSTOM, "/elsewhere", "1:1"):
        assert value not in env.values()
    assert env["PLAIN_SETTING"] == "kept"
    assert env["GIT_ALLOW_PROTOCOL"] == "" and env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_EDITOR"] == ":"
    assert not any(k.upper().startswith("GIT_") and k not in hubgit.ENV for k in env)
    # the agents' scrubbing lets shell.pass_env through; DreamTeam's own git never gets those
    assert "OPENAI_API_KEY" not in secret_free_env()


def test_the_fixed_settings_and_a_hooks_path_that_holds_none():
    args = hubgit.fixed_args()
    assert args[0] == "--no-pager"
    pairs = dict(a.split("=", 1) for a in args[1:] if a != "-c")
    assert pairs["core.fsmonitor"] == "" and pairs["commit.gpgSign"] == "false" and pairs["credential.helper"] == ""
    if sys.platform != "win32":
        # a device: nobody (the members under "env" and "netns" have DreamTeam's uid) can put a hook below it
        assert pairs["core.hooksPath"] == os.devnull
        with pytest.raises(OSError):
            (Path(os.devnull) / "pre-commit").write_text("planted", encoding="utf-8")
        return
    folder = Path(pairs["core.hooksPath"])
    assert folder.is_dir() and not any(folder.iterdir())
    assert not str(folder).startswith(str(Path.cwd()))   # outside every workspace
    (folder / "pre-commit").write_text("planted", encoding="utf-8")   # anything in it: a new, empty one
    try:
        again = Path(dict(a.split("=", 1) for a in hubgit.fixed_args()[1:] if a != "-c")["core.hooksPath"])
        assert again != folder and not any(again.iterdir())
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_parse_disarms_the_repositorys_own_commands_and_gives_the_users_back():
    rec = lambda scope, key, value=None: (f"{scope}\0file:{scope}.cfg\0" + (key if value is None else
                                                                             f"{key}\n{value}") + "\0")
    data = "".join([
        rec("system", "filter.lfs.clean", "git-lfs clean -- %f"),
        rec("system", "filter.lfs.process", "git-lfs filter-process"),
        rec("system", "filter.lfs.required", "true"),
        rec("global", "credential.helper", "manager"),
        rec("global", "hook.lint.command", "lint"),
        rec("local", "filter.lfs.clean", "evil"),             # a member's override of the user's driver
        rec("local", "filter.Probe.clean", "evil"),           # a driver of the member's own (case kept)
        rec("local", "filter.Probe.required"),                 # a bare key: true
        rec("local", "filter.weird,name.process", "evil"),
        rec("local", "filter.weird,name.required", "true"),
        rec("local", "diff.x.textconv", "evil"),
        rec("local", "credential.https://example.com.helper", "evil"),
        rec("worktree", "core.sshCommand", "evil"),
        rec("local", "gc.recentObjectsHook", "evil"),
        rec("local", "include.path", "x.cfg"),
        rec("local", "user.name", "someone"),                   # nothing to disarm
        rec("command", "filter.mine.clean", "ours"),
    ]).encode("utf-8")
    scan = hubgit.parse(data, Path("/repo"))
    assert scan.guard == {
        "filter.lfs.clean": "git-lfs clean -- %f",   # the user's value back: git-lfs keeps working
        "filter.Probe.clean": "", "filter.Probe.required": "false",
        "filter.weird,name.process": "", "filter.weird,name.required": "true",   # no pathspec names it: refused
        "diff.x.textconv": "", "credential.https://example.com.helper": "", "core.sshcommand": "",
    }
    assert scan.unfiltered == {"Probe"}   # required, no command of the user's: its files stay out
    assert scan.hooks == {"lint"} and scan.maintenance_off and scan.includes
    # the global config the members can write (the offline user's home in the Docker sandbox): nothing comes back
    scan = hubgit.parse(data + rec("global", "filter.home.clean", "evil").encode(), Path("/repo"), trust_global=False)
    assert scan.guard["filter.lfs.clean"] == "git-lfs clean -- %f"   # the system's still does
    assert scan.guard["filter.home.clean"] == "" and scan.hooks == {"lint"}


def test_in_the_docker_sandbox_the_offline_users_global_config_is_not_the_users(tmp_path, monkeypatch):
    data = b"global\0file:/home/agent/.gitconfig\0filter.home.clean\nevil\0"
    monkeypatch.setattr(hubgit, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, data, b""))
    monkeypatch.setattr(netoff, "isolation", lambda: "docker")
    assert hubgit.scan("git", tmp_path, {}, offline=True).guard == {"filter.home.clean": ""}
    assert hubgit.scan("git", tmp_path, {}).guard == {}   # the hub's own home: the user's
    monkeypatch.setattr(netoff, "isolation", lambda: "netns")
    assert hubgit.scan("git", tmp_path, {}, offline=True).guard == {}


def test_with_excludes_turns_literal_pathspecs_into_magic():
    ex = [":(exclude,attr:filter=crypt)"]
    assert hubgit.with_excludes(("add", "-A", "--", "a[1].md"), ex, True, None) == (
        ["add", "-A", "--", ":(literal)a[1].md", *ex], False, None)
    assert hubgit.with_excludes(("add", "--", ".", ":(exclude)x"), ex, False, None) == (
        ["add", "--", ".", ":(exclude)x", *ex], False, None)
    args, literal, data = hubgit.with_excludes(("add", "--pathspec-from-file=-"), ex, True, b"a\0b\0")
    assert data == b":(literal)a\0:(literal)b\0" + ex[0].encode() + b"\0" and literal is False
    assert hubgit.with_excludes(("commit", "-m", "x"), ex, True, None) == (["commit", "-m", "x"], True, None)
    assert hubgit.with_excludes(("add", "--", "a"), [], True, None) == (["add", "--", "a"], True, None)
    # many excludes (by name, for an old git's add): every pathspec through stdin
    args, literal, data = hubgit.with_excludes(("add", "-A", "--", "a", "b"), [":(exclude,literal)s"], True, None,
                                               stdin=True)
    assert args == ["add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul"] and literal is False
    assert data == b":(literal)a\0:(literal)b\0:(exclude,literal)s\0"


# ---- nothing planted runs ----------------------------------------------------------------------------------------
@needs_git
@pytest.mark.parametrize("old_git", [False, True])
async def test_nothing_a_member_plants_runs_in_dreamteams_commits(fake_swarm, tmp_path, monkeypatch, old_git):
    if old_git:   # a git older than 2.31: the settings go on the command line (-c), not into GIT_CONFIG_COUNT
        monkeypatch.setattr(hubgit, "git_version", lambda git: (2, 30))
    ws = Workspace(fake_swarm)
    fake_swarm.workspace = ws
    await ws.init()
    root = ws.root
    marks = tmp_path / "marks"
    marks.mkdir()
    plant_config(root, marks)
    put(root, "notes.md", "# notes\n")
    put(root, "data.inc", "included\n")
    put(root, "a.txt", "a\n")
    assert await ws.autocommit(ANNA, kind="shell", detail="make")   # status, add, diff, commit
    await ws.write(ANNA, "notes.md", "# notes, more\n")              # a path commit
    await ws.write(ANNA, "fresh.md", "new\n")
    racy(root / "notes.md", "# notes, MORE\n", root / ".git" / "index")
    put(root, "b.txt", "b\n")
    assert await ws.autocommit(ANNA, kind="shell", detail="again", hold=True)
    await ws.commit_held(None, "when the session stops")
    assert ran(marks) == []
    log = git(root, "log", "--name-only", "--format=%s")
    assert "notes.md" in log and "fresh.md" in log and "data.inc" in log and "b.txt" in log
    # the control: the same repository with plain git runs what was planted
    put(root, "c.md", "c\n")
    git(root, "add", "c.md")
    assert "clean" in ran(marks)


@needs_git
async def test_a_submodules_own_filter_does_not_run_either(fake_swarm, tmp_path):
    """git starts a child git in every checked-out submodule (status, add, commit): its own config is disarmed
    too, on the same command line."""
    ws = Workspace(fake_swarm)
    fake_swarm.workspace = ws
    await ws.init()
    root = ws.root
    lib = root / "lib"
    lib.mkdir()
    git(lib, "init", "-q")
    put(lib, ".gitattributes", "*.md filter=probe\n")
    put(lib, "s.md", "s\n")
    git(lib, "add", "-A")
    git(lib, "commit", "-q", "-m", "lib")
    git(root, "add", "lib")   # a gitlink, as a member's `git add lib` makes it
    git(root, "commit", "-q", "-m", "add lib")
    marks = tmp_path / "marks"
    marks.mkdir()
    git(lib, "config", "filter.probe.clean", marker_cmd(marks / "subclean"))
    git(lib, "config", "core.fsmonitor", marker_cmd(marks / "subfsmonitor", "exit 1"))
    racy(lib / "s.md", "S\n", lib / ".git" / "index")
    put(root, "top.txt", "t\n")
    assert await ws.autocommit(ANNA, kind="shell", detail="make")
    racy(lib / "s.md", "s\n", lib / ".git" / "index")
    await ws.write(ANNA, "top2.txt", "t\n")
    assert ran(marks) == []
    racy(lib / "s.md", "Q\n", lib / ".git" / "index")
    git(root, "status", "--porcelain")   # the control: plain git runs the submodule's filter
    assert "subclean" in ran(marks)


@needs_git
async def test_the_session_checks_and_commits_in_a_folder_of_the_user(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    git(folder, "init", "-q")
    put(folder, "README.md", "mine\n")
    put(folder, "mcp/srv/server.py", "x\n")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "mine")
    marks = tmp_path / "marks"
    marks.mkdir()
    plant_config(folder, marks)
    git(folder, "add", ".gitattributes")
    git(folder, "-c", "core.hooksPath=/nonexistent", "-c", "core.fsmonitor=false", "commit", "-q", "--no-verify",
        "-m", "attributes", check=False)
    for f in marks.iterdir():
        f.unlink()
    racy(folder / "README.md", "MINE\n", folder / ".git" / "index")
    state = repo_state(folder)
    assert state["error"] is None and state["toplevel"] is not None and state["changes"]
    assert workspace_mod.tracked(folder, ["mcp/srv"]) == ["mcp/srv"]
    shutil.rmtree(folder / "mcp")
    made, error = commit_paths(folder, ["mcp/srv"], "Move the session's MCP servers")
    assert error is None and made
    assert ran(marks) == []
    # the initial commit of an adopted folder that is no repository yet: its attributes name a driver nobody defines
    other = tmp_path / "other"
    other.mkdir()
    put(other, ".gitattributes", "*.md filter=probe\n")
    put(other, "x.md", "x\n")
    assert init_repo(other, adopted=True) is None
    assert ran(marks) == [] and git(other, "show", "HEAD:x.md").strip() == "x"


@needs_git
async def test_an_adopted_repository_still_commits(make_fake_swarm, tmp_path):
    swarm = make_fake_swarm()
    root = swarm.config.workspace_dir
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    put(root, "README.md", "mine\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "the user's work")
    swarm.config.workspace_adopted = True
    marks = tmp_path / "marks"
    marks.mkdir()
    plant_hooks(root, marks / "hooks")
    git(root, "config", "core.fsmonitor", marker_cmd(marks / "fsmonitor", "exit 1"))
    ws = Workspace(swarm)
    swarm.workspace = ws
    await ws.init()
    await ws.write(ANNA, "notes/new.txt", "n\n")
    put(root, "README.md", "mine, changed\n")
    assert await ws.autocommit(ANNA, kind="shell", detail="edit")
    assert ran(marks) == []
    assert git(root, "log", "--format=%an", "-2").splitlines() == [ANNA, ANNA]
    assert git(root, "status", "--porcelain") == ""


# ---- the user's own filters --------------------------------------------------------------------------------------
@pytest.fixture
def user_home(tmp_path, monkeypatch) -> Path:
    """A HOME of the test's own: its .gitconfig is the global config git reads (the user's real one is untouched)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


@needs_git
async def test_a_global_filter_of_the_user_still_runs_without_secrets(make_fake_swarm, user_home, tmp_path,
                                                                       monkeypatch):
    dump = tmp_path / "filter-env.txt"
    evil = tmp_path / "evil"
    (user_home / ".gitconfig").write_text(
        "[filter \"trusted\"]\n"
        f"\tclean = \"env > '{dump.as_posix()}'; tr a-z A-Z\"\n"   # quoted: ";" starts a comment
        "\tsmudge = cat\n\trequired = true\n", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_ANTHROPIC)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_OPENAI)
    monkeypatch.setenv("DEEPSEEK_KEY", FAKE_CUSTOM)
    swarm = make_fake_swarm({"models": {"default": {"provider": "openai", "model": "m",
                                                    "api_key_env": "DEEPSEEK_KEY"}}})
    ws = Workspace(swarm)
    swarm.workspace = ws
    await ws.init()
    root = ws.root
    put(root, ".gitattributes", "*.dat filter=trusted\n")
    # a member's override of the user's driver gets the user's value back
    git(root, "config", "filter.trusted.clean", marker_cmd(evil))
    await ws.write(ANNA, "x.dat", "abc\n")
    assert not evil.exists()
    assert git(root, "show", "HEAD:x.dat") == "ABC\n"   # the user's filter ran
    seen = dump.read_text(encoding="utf-8", errors="replace")
    for secret in (FAKE_ANTHROPIC, FAKE_OPENAI, FAKE_CUSTOM):
        assert secret not in seen
    assert "GIT_ALLOW_PROTOCOL=" in seen


def old_git(monkeypatch) -> None:
    """git 2.39 (macOS's /usr/bin/git, Debian 12, Ubuntu 24.04), emulated with the git at hand: its `git add`
    rejects the attr pathspec magic, on its command line and in a --pathspec-from-file (status, commit and ls-files
    take it)."""
    real = hubgit.run

    def run(argv, *, input=None, **kw):
        if "add" in argv and (any("attr:" in a for a in argv[argv.index("add"):]) or b"attr:" in (input or b"")):
            return subprocess.CompletedProcess(argv, 128, b"", b"fatal: :(exclude,attr:filter=crypt): pathspec "
                                                           b"magic not supported by this command: 'attr'\n")
        return real(argv, input=input, **kw)

    monkeypatch.setattr(hubgit, "git_version", lambda git: (2, 39))
    monkeypatch.setattr(hubgit, "run", run)


@needs_git
@pytest.mark.parametrize("old", [False, True])
async def test_a_required_filter_only_the_repository_defines_keeps_its_files_out(fake_swarm, tmp_path, monkeypatch,
                                                                                 old):
    """git-crypt (or git-lfs installed with --local): DreamTeam cannot run it, and must not store its files
    unfiltered; the rest is committed as always, and the member's own git (which runs the filter) can commit them.
    With a git older than 2.44 (`old`) the add leaves them out by name."""
    if old:
        old_git(monkeypatch)
    ws = Workspace(fake_swarm)
    fake_swarm.workspace = ws
    await ws.init()
    root = ws.root
    marks = tmp_path / "marks"
    marks.mkdir()
    put(root, ".gitattributes", "secret*.txt filter=crypt\n")
    git(root, "config", "filter.crypt.clean", marker_cmd(marks / "crypt"))
    git(root, "config", "filter.crypt.required", "true")
    put(root, "secret.txt", "password\n")
    put(root, "other.txt", "o\n")
    assert await ws.autocommit(ANNA, kind="shell", detail="make")
    await ws.write(ANNA, "secret.txt", "password 2\n")
    put(root, "more.txt", "m\n")
    put(root, "secret2.txt", "password 3\n")
    put(root, "data/secret3.txt", "password 4\n")
    put(root, "data/plain.txt", "p\n")
    await ws.autocommit(ANNA, kind="shell", detail="held", hold=True)   # new files only: all held
    assert set(ws.held()) == {"more.txt", "data/plain.txt"}   # no commit could ever take the filter's files
    await ws.commit_held(None, "when the session stops")
    assert ran(marks) == [] and not ws.held()
    tracked = git(root, "ls-files").split()
    assert {"other.txt", ".gitattributes", "more.txt", "data/plain.txt"} <= set(tracked)
    assert not {"secret.txt", "secret2.txt", "data/secret3.txt"} & set(tracked)
    assert git(root, "diff", "--cached", "--name-only") == ""


@needs_git
async def test_with_an_old_git_a_file_made_while_the_add_starts_is_put_back(fake_swarm, tmp_path, monkeypatch):
    """git older than 2.44: the files of the filter are listed first and left out by name. One made between the
    listing and the add (another member's command) is staged by that add, unfiltered: it is put back before any
    commit takes it."""
    old_git(monkeypatch)
    ws = Workspace(fake_swarm)
    fake_swarm.workspace = ws
    await ws.init()
    root = ws.root
    put(root, ".gitattributes", "secret*.txt filter=crypt\n")
    git(root, "config", "filter.crypt.clean", "false")
    git(root, "config", "filter.crypt.required", "true")
    put(root, "secret.txt", "password\n")
    put(root, "other.txt", "o\n")
    emulated = hubgit.run
    lists: list[list[str]] = []

    def run(argv, **kw):
        proc = emulated(argv, **kw)
        if "ls-files" in argv and "-s" in argv and "--others" in argv:   # the listing before the add
            lists.append(argv)
            put(root, "secret-late.txt", "late password\n")
        return proc

    monkeypatch.setattr(hubgit, "run", run)
    assert await ws.autocommit(ANNA, kind="shell", detail="make")
    assert lists
    tracked = git(root, "ls-files").split()
    assert "other.txt" in tracked and "secret.txt" not in tracked and "secret-late.txt" not in tracked
    assert git(root, "diff", "--cached", "--name-only") == ""   # nor left staged for the next commit


ROT13 = "tr A-Za-z N-ZA-Mn-za-m"


def crypt_repo(folder: Path) -> Path:
    """A repository of the user after `git-crypt unlock`, as far as git sees it: a transforming filter (rot13 here)
    that only its own config defines, required; secret/ is stored filtered."""
    folder.mkdir(parents=True)
    git(folder, "init", "-q")
    for key, value in (("filter.crypt.clean", ROT13), ("filter.crypt.smudge", ROT13),
                       ("filter.crypt.required", "true")):
        git(folder, "config", key, value)
    put(folder, ".gitattributes", "secret/** filter=crypt\n")
    put(folder, "secret/key.txt", "hello\n")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "mine")
    assert git(folder, "cat-file", "-p", "HEAD:secret/key.txt") == "uryyb\n"
    return folder


def touch(path: Path) -> None:
    """The same content written again with a new mtime (an editor's save, a checkout): only its stat changes."""
    path.write_bytes(path.read_bytes())
    later = time.time() + 5
    os.utime(path, (later, later))


@needs_git
def test_a_touched_file_of_a_required_filter_is_no_change_to_the_session_checks(tmp_path, caplog):
    """repo_state decides whether a folder may be adopted and whether a session may start there. With the filter
    disarmed, a file of it whose stat changed but not its content looks changed (its raw content is compared with
    the filtered one in the index): it is left out of the check, as from DreamTeam's commits. The user's own git,
    which runs the filter, sees no change either; any other change still counts. The user is told once."""
    import logging

    folder = crypt_repo(tmp_path / "project")
    touch(folder / "secret" / "key.txt")
    with caplog.at_level(logging.WARNING, logger="dreamteam.hubgit"):
        state = repo_state(folder)
        assert state["error"] is None and state["changes"] == [], state
        assert git(folder, "status", "--porcelain") == ""   # the control: the user's git, with the filter
        put(folder, "secret/new.txt", "an agent's\n")   # nor what DreamTeam never commits
        put(folder, "plain.txt", "p\n")
        assert repo_state(folder)["changes"] == ["?? plain.txt"]
    told = [r.getMessage() for r in caplog.records if "git filter crypt" in r.getMessage()]
    assert len(told) == 1 and str(folder) in told[0], told


@needs_git
def test_a_path_commit_in_the_users_repository_leaves_out_a_required_filters_files(tmp_path):
    """commit_paths (and init_repo's add) stage pathspecs too: a changed git-crypt-style file below them is not
    committed unfiltered."""
    folder = tmp_path / "project"
    folder.mkdir()
    git(folder, "init", "-q")
    put(folder, ".gitattributes", "*.key filter=crypt\n")
    git(folder, "config", "filter.crypt.clean", "tr a-z A-Z")   # the user's "encryption", run by the user's git
    git(folder, "config", "filter.crypt.required", "true")
    put(folder, "mcp/srv/server.key", "secret\n")
    put(folder, "mcp/srv/server.py", "x\n")
    git(folder, "add", "-A")
    git(folder, "commit", "-q", "-m", "mine")
    marks = tmp_path / "marks"
    marks.mkdir()
    git(folder, "config", "filter.crypt.clean", marker_cmd(marks / "crypt", "tr a-z A-Z"))
    put(folder, "mcp/srv/server.key", "plain secret\n")
    (folder / "mcp" / "srv" / "server.py").unlink()
    made, error = commit_paths(folder, ["mcp/srv"], "Move the session's MCP servers")
    assert error is None and made
    assert ran(marks) == []
    assert git(folder, "show", "HEAD:mcp/srv/server.key") == "SECRET\n"   # as the user's git stored it
    assert git(folder, "ls-files", "mcp").split() == ["mcp/srv/server.key"]   # the removal is committed


# ---- the cache ----------------------------------------------------------------------------------------------------
@needs_git
async def test_the_scan_is_reused_until_a_config_file_changes(fake_swarm, tmp_path, monkeypatch):
    ws = Workspace(fake_swarm)
    fake_swarm.workspace = ws
    await ws.init()
    root = ws.root
    scans: list[Path] = []
    real = hubgit.scan
    monkeypatch.setattr(hubgit, "scan", lambda git, folder, env, **kw: scans.append(folder) or real(git, folder,
                                                                                                    env, **kw))
    for i in range(3):
        await ws.write(ANNA, f"f{i}.txt", "x\n")
    assert len(scans) == 1
    marks = tmp_path / "marks"
    marks.mkdir()
    put(root, ".gitattributes", "*.md filter=late\n")
    git(root, "config", "filter.late.clean", marker_cmd(marks / "late"))   # the config's content changed
    await ws.write(ANNA, "late.md", "x\n")
    assert len(scans) == 2 and ran(marks) == []
    # an included file is never trusted to stay as it was
    included = tmp_path / "inc.gitconfig"
    included.write_text("[core]\n\tquotepath = false\n", encoding="utf-8")
    git(root, "config", "include.path", included.as_posix())
    await ws.write(ANNA, "a1.txt", "x\n")
    count = len(scans)
    included.write_text(f"[filter \"late2\"]\n\tclean = \"{marker_cmd(marks / 'late2')}\"\n", encoding="utf-8")
    put(root, ".gitattributes", "*.md filter=late2\n")
    await ws.write(ANNA, "late2.md", "x\n")
    assert len(scans) > count and ran(marks) == []


def _guard_pairs(guard: hubgit.Guard) -> dict[str, str]:
    env, count = guard.env, int(guard.env.get("GIT_CONFIG_COUNT", 0))
    return {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}


@needs_git
def test_a_commondir_a_member_writes_is_not_hidden_by_the_cache(tmp_path):
    """.git/commondir makes git read another folder's config: the cached scan of the old one must not be reused."""
    exe = shutil.which("git")
    root, other = tmp_path / "r", tmp_path / "other"
    for folder in (root, other):
        folder.mkdir()
        git(folder, "init", "-q")
    git(other, "config", "filter.probe.clean", "evil")
    env = hubgit.environment()
    assert "filter.probe.clean" not in _guard_pairs(hubgit.guard(exe, root, env))
    (root / ".git" / "commondir").write_text((other / ".git").as_posix() + "\n", encoding="utf-8")
    assert "filter.probe.clean" in git(root, "config", "--list")   # the control: git reads the other config now
    assert _guard_pairs(hubgit.guard(exe, root, env))["filter.probe.clean"] == ""


@needs_git
def test_the_cache_watches_the_global_config_too(user_home, tmp_path, monkeypatch):
    """A global config that appears (the members can write it under "netns" and in the Docker sandbox) is read."""
    exe = shutil.which("git")
    root = tmp_path / "r"
    root.mkdir()
    git(root, "init", "-q")
    scans: list[Path] = []
    real = hubgit.scan
    monkeypatch.setattr(hubgit, "scan", lambda git, folder, env, **kw: scans.append(folder) or real(git, folder,
                                                                                                    env, **kw))
    env = hubgit.environment()
    hubgit.guard(exe, root, env)
    hubgit.guard(exe, root, env)
    assert len(scans) == 1
    (user_home / ".gitconfig").write_text("[core]\n\tquotepath = false\n", encoding="utf-8")
    hubgit.guard(exe, root, env)
    assert len(scans) == 2


def _processes(monkeypatch) -> list[list[str]]:
    """Every git process hubgit starts from now on (the guard's own and the commands)."""
    calls: list[list[str]] = []
    real = hubgit.run
    monkeypatch.setattr(hubgit, "run", lambda argv, **kw: calls.append(argv) or real(argv, **kw))
    return calls


def _superproject(tmp_path: Path) -> tuple[Path, Path]:
    """`lib` (a repository) and `top`, with lib checked out twice as the submodules s1 and s2: their `.git` is a
    file naming their git dir in top/.git/modules."""
    lib, top = tmp_path / "lib", tmp_path / "top"
    for folder in (lib, top):
        folder.mkdir()
        git(folder, "init", "-q")
        put(folder, "f.txt", "f\n")
        git(folder, "add", "-A")
        git(folder, "commit", "-q", "-m", "first")
    for name in ("s1", "s2"):
        git(top, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), name)
    git(top, "commit", "-q", "-m", "submodules")
    assert (top / "s1" / ".git").is_file()
    return lib, top


@needs_git
def test_the_scan_is_reused_in_submodules_and_linked_worktrees(tmp_path, monkeypatch):
    """A checked-out submodule's and a linked worktree's `.git` is a file that names the git dir: cached like a
    plain repository's (once warm, the guard starts no git process of its own), and scanned again when the `.git`
    file names another git dir, when that git dir's config changes, and when the common dir's config changes (a
    linked worktree reads it)."""
    exe = shutil.which("git")
    lib, top = _superproject(tmp_path)
    tree = tmp_path / "tree"
    git(lib, "worktree", "add", "-q", str(tree))
    assert (tree / ".git").is_file()
    env = hubgit.environment()
    calls = _processes(monkeypatch)
    for root in (top, tree):
        hubgit.guard(exe, root, env)
        calls.clear()
        hubgit.guard(exe, root, env)
        assert calls == [], (root, calls)
    # a submodule's .git file pointed at another repository's git dir: that config is read
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    git(other, "config", "filter.other.clean", "evil")
    (top / "s1" / ".git").unlink()   # (Git for Windows hides it: no rewrite in place there)
    (top / "s1" / ".git").write_text(f"gitdir: {(other / '.git').as_posix()}\n", encoding="utf-8")
    assert "filter.other.clean" in git(top / "s1", "config", "--list")   # the control: git reads it now
    assert _guard_pairs(hubgit.guard(exe, top, env))["filter.other.clean"] == ""
    # the git dir's own config (top/.git/modules/s2/config) gains a filter
    git(top / "s2", "config", "filter.late.clean", "evil")
    assert _guard_pairs(hubgit.guard(exe, top, env))["filter.late.clean"] == ""
    # the common dir's config (lib/.git/config), which the linked worktree reads, gains one
    calls.clear()
    assert "filter.common.clean" not in _guard_pairs(hubgit.guard(exe, tree, env)) and calls == []
    git(lib, "config", "filter.common.clean", "evil")
    assert _guard_pairs(hubgit.guard(exe, tree, env))["filter.common.clean"] == ""


@needs_git
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX: a `..` after a link leads out of the link's target (on "
                                                    "Windows it is lexical, for git as for DreamTeam)")
def test_a_submodule_folder_swapped_for_a_link_is_no_stale_cache(tmp_path):
    """A member replaces a checked-out submodule's folder with a link to a copy elsewhere: its `.git` file has the
    same bytes, but its relative "gitdir: ../.git/modules/s1" now leads to another git dir, whose config git reads."""
    exe = shutil.which("git")
    _lib, top = _superproject(tmp_path)
    env = hubgit.environment()
    assert "filter.moved.clean" not in _guard_pairs(hubgit.guard(exe, top, env))   # cached now
    elsewhere = tmp_path / "elsewhere"
    shutil.copytree(top / "s1", elsewhere / "s1")
    shutil.copytree(top / ".git" / "modules" / "s1", elsewhere / ".git" / "modules" / "s1")
    git(elsewhere / "s1", "config", "filter.moved.clean", "evil")
    (top / "s1").rename(tmp_path / "s1-before")
    os.symlink(elsewhere / "s1", top / "s1", target_is_directory=True)
    assert (top / "s1" / ".git").read_bytes() == (tmp_path / "s1-before" / ".git").read_bytes()
    assert "filter.moved.clean" in git(top / "s1", "config", "--list")   # the control: git reads that config
    assert _guard_pairs(hubgit.guard(exe, top, env))["filter.moved.clean"] == ""


@needs_git
@pytest.mark.parametrize("config", ["deleted", "emptied"])
def test_a_top_config_a_member_removes_does_not_hide_the_submodules(tmp_path, config):
    """git works without `.git/config`, and with an empty one: then no local config tells where the index is, and the
    gitlinks come from `git ls-files`, so a checked-out submodule's own filter is still disarmed."""
    _lib, top = _superproject(tmp_path)
    marks = tmp_path / "marks"
    marks.mkdir()
    sub = top / "s1"
    git(sub, "config", "filter.probe.clean", marker_cmd(marks / "subclean"))
    put(sub, ".gitattributes", "*.txt filter=probe\n")
    touch(sub / "f.txt")   # (a clone's checkout may have other line endings: the same bytes, git must hash them)
    if config == "deleted":
        (top / ".git" / "config").unlink()
    else:
        (top / ".git" / "config").write_bytes(b"")
    assert git(top, "rev-parse", "--git-dir").strip()   # git still takes the repository
    assert _guard_pairs(hubgit.guard(shutil.which("git"), top, hubgit.environment()))["filter.probe.clean"] == ""
    assert repo_state(top)["error"] is None
    assert ran(marks) == []
    touch(sub / "f.txt")
    git(top, "status", "--porcelain")   # the control: plain git runs the submodule's filter
    assert "subclean" in ran(marks)


@needs_git
async def test_a_nul_in_a_submodules_git_file_neither_breaks_nor_skips_the_guard(fake_swarm, tmp_path):
    """A member's command puts a NUL and more after the path of a submodule's `.git` file: git takes the path up to
    the NUL, Python's calls refuse it. The autocommits (the shell tool awaits one after every command) and the session
    check still work, and that submodule's filter stays disarmed (its folder is scanned every time)."""
    ws = Workspace(fake_swarm)
    fake_swarm.workspace = ws
    await ws.init()
    root = ws.root
    lib = tmp_path / "lib"
    lib.mkdir()
    git(lib, "init", "-q")
    put(lib, ".gitattributes", "*.md filter=probe\n")
    put(lib, "s.md", "s\n")
    git(lib, "add", "-A")
    git(lib, "commit", "-q", "-m", "lib")
    git(root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "sub")
    git(root, "commit", "-q", "-m", "add sub")
    sub = root / "sub"
    marks = tmp_path / "marks"
    marks.mkdir()
    git(sub, "config", "filter.probe.clean", marker_cmd(marks / "subclean"))
    data = (sub / ".git").read_bytes().rstrip(b"\r\n")
    (sub / ".git").unlink()   # (Git for Windows hides it: no rewrite in place there)
    (sub / ".git").write_bytes(data + b"\0x\n")
    touch(sub / "s.md")   # (the clone's checkout may have other line endings: the same bytes, git must hash them)
    put(root, "top.txt", "t\n")
    assert await ws.autocommit(ANNA, kind="shell", detail="make")
    touch(sub / "s.md")
    await ws.write(ANNA, "top2.txt", "t\n")
    touch(sub / "s.md")
    assert repo_state(root)["error"] is None
    assert ran(marks) == []
    touch(sub / "s.md")
    git(root, "status", "--porcelain")   # the control: plain git follows that `.git` file and runs the filter
    assert "subclean" in ran(marks)


@needs_git
@pytest.mark.parametrize("junk", [b"\0x", b"\xff"], ids=["nul", "not-utf8"])
@pytest.mark.parametrize("where", ["git-file", "commondir"])
def test_a_git_file_or_commondir_that_names_no_path_never_breaks_the_guard(tmp_path, where, junk):
    """Bytes no path can hold after the path of a submodule's `.git` file or a linked worktree's `commondir`: a NUL
    (git takes the path up to it) or, on Windows, no UTF-8 (elsewhere just a git dir that does not exist). The guard
    and the session check answer anyway, and where git still finds the repository its filter is disarmed."""
    lib, top = _superproject(tmp_path)
    if where == "git-file":
        folder, file, owner = top, top / "s1" / ".git", top / "s1"
    else:
        folder = tmp_path / "tree"
        git(lib, "worktree", "add", "-q", str(folder))
        file, owner = lib / ".git" / "worktrees" / "tree" / "commondir", folder
    git(owner, "config", "filter.probe.clean", "evil")
    data = file.read_bytes().rstrip(b"\r\n")
    file.unlink()
    file.write_bytes(data + junk + b"\n")
    pairs = _guard_pairs(hubgit.guard(shutil.which("git"), folder, hubgit.environment()))
    repo_state(folder)
    if junk == b"\0x":
        assert "filter.probe.clean" in git(owner, "config", "--list")   # the control: git still reads that config
        assert pairs["filter.probe.clean"] == ""


@needs_git
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX: a FIFO")
@pytest.mark.parametrize("where", ["config.worktree", "commondir", "index"])
def test_a_fifo_a_member_leaves_in_a_git_dir_never_hangs_the_guard(tmp_path, monkeypatch, where):
    """Opening a FIFO waits for a writer (and a device may never end): the guard reads neither, and never reuses a
    scan while one is there. One that git itself opens (a submodule's commondir or index) holds up only DreamTeam's
    git, until its timeout, as in any command."""
    monkeypatch.setattr(hubgit, "SCAN_TIMEOUT", 3.0)
    _lib, top = _superproject(tmp_path)
    exe, env = shutil.which("git"), hubgit.environment()
    hubgit.guard(exe, top, env)   # cached now
    modules = top / ".git" / "modules" / "s1"
    target = {"config.worktree": top / ".git" / "config.worktree", "commondir": modules / "commondir",
              "index": modules / "index"}[where]
    if target.exists():
        target.unlink()
    os.mkfifo(target)
    outcome: list[object] = []

    def call() -> None:
        try:
            outcome.append(hubgit.guard(exe, top, env))
        except subprocess.SubprocessError as exc:   # git's own wait, bounded by SCAN_TIMEOUT
            outcome.append(exc)

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(20)
    hung = worker.is_alive()
    if hung:   # let it go: a writer that opens the FIFO and closes it (end of file)
        os.close(os.open(target, os.O_WRONLY | os.O_NONBLOCK))
        worker.join(10)
    assert not hung, f"hubgit.guard blocked on a FIFO at {where}"
    assert len(outcome) == 1, "the guard failed otherwise"
    if where == "config.worktree":   # git does not read it (no extensions.worktreeConfig): a plain answer
        assert isinstance(outcome[0], hubgit.Guard)
        calls = _processes(monkeypatch)
        hubgit.guard(exe, top, env)
        assert any("config" in argv for argv in calls)   # scanned again: not reused


def _descriptors() -> int:
    """How many descriptors this process holds (/proc/self/fd on Linux, /dev/fd on macOS)."""
    return len(os.listdir("/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"))


@needs_git
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX: a folder opens like a file (Windows refuses the open)")
@pytest.mark.parametrize("where", ["config.worktree", "commondir"])
def test_a_folder_a_member_makes_in_a_git_dir_leaks_no_descriptor(tmp_path, monkeypatch, where):
    """`mkdir .git/config.worktree` (git ignores it) or a folder at a submodule's commondir: every guard(), so every
    DreamTeam git call, left a descriptor open, until the hub could open nothing (256 by default on macOS: a few
    dozen of the members' commands). The guard still disarms the repository's keys, and the scan is not reused
    while the folder is there (a folder is NOT_A_FILE, not a missing file)."""
    _lib, top = _superproject(tmp_path)
    git(top, "config", "filter.probe.clean", "evil")
    exe, env = shutil.which("git"), hubgit.environment()
    hubgit.guard(exe, top, env)   # cached now
    target = {"config.worktree": top / ".git" / "config.worktree",
              "commondir": top / ".git" / "modules" / "s1" / "commondir"}[where]
    target.mkdir()
    before = _descriptors()
    for _ in range(30):
        pairs = _guard_pairs(hubgit.guard(exe, top, env))
        run_git(top, "rev-parse", "HEAD")   # (a guard() of its own)
    assert _descriptors() - before < 10, "a descriptor per call"
    assert pairs["filter.probe.clean"] == ""
    calls = _processes(monkeypatch)
    hubgit.guard(exe, top, env)
    assert any("config" in argv for argv in calls)   # scanned again: not reused


def test_a_folder_or_a_file_that_grows_while_read_is_no_content_to_compare(tmp_path, monkeypatch):
    """_read: a folder at a watched path (POSIX opens it, Windows refuses) is NOT_A_FILE, never None, which stands for
    no file. A file longer when read than fstat said a moment before (it grew meanwhile) is NOT_A_FILE too, never its
    first bytes: those could equal a stamp of the file before the change, and the old scan would be reused. One that
    shrank meanwhile is read as it is now."""
    import types

    folder = tmp_path / "config.worktree"
    folder.mkdir()
    assert hubgit._read(str(folder)) is hubgit.NOT_A_FILE
    path = tmp_path / "config"
    path.write_bytes(b"[core]\n[filter \"x\"]\n\tclean = evil\n")
    now = path.read_bytes()
    real = os.fstat

    def fstat_said(size: int):
        return lambda fd: types.SimpleNamespace(st_mode=real(fd).st_mode, st_size=size)

    with monkeypatch.context() as m:
        m.setattr(hubgit.os, "fstat", fstat_said(7))      # b"[core]\n" when fstat looked: the filter came after
        grown = hubgit._read(str(path))
        m.setattr(hubgit.os, "fstat", fstat_said(1000))   # longer when fstat looked
        shrunk = hubgit._read(str(path))
    assert grown is hubgit.NOT_A_FILE and shrunk == now


def test_reading_a_watched_file_costs_a_buffer_of_its_own_size(tmp_path):
    """_read takes the size fstat gives and one byte more, not its limit: _may_link reads the index, up to INDEX_LIMIT
    (64 MiB), before nearly every git call, and a buffer that large cost Windows some 20 ms at each."""
    import tracemalloc

    index = tmp_path / "index"
    index.write_bytes(b"DIRC" + b"\0" * 2044)
    tracing = tracemalloc.is_tracing()
    if not tracing:
        tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        before, _peak = tracemalloc.get_traced_memory()
        data = hubgit._read(str(index), hubgit.INDEX_LIMIT)
        _now, peak = tracemalloc.get_traced_memory()
    finally:
        if not tracing:
            tracemalloc.stop()
    assert data == index.read_bytes()
    assert peak - before < 1 << 20, f"{peak - before} bytes for a file of 2 KiB"


@needs_git
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX: a writer can set an mtime back, never the ctime")
def test_an_index_rewritten_in_place_with_its_mtime_put_back_is_read_again(tmp_path):
    """The gitlinks' cache: a submodule written into the index in place (same size and inode) with the old mtime
    restored is still found, so its own config is scanned."""
    folder = tmp_path / "r"
    folder.mkdir()
    git(folder, "init", "-q")
    put(folder, "x", "x\n")
    blob = git(folder, "hash-object", "-w", "x").strip()
    index = folder / ".git" / "index"
    git(folder, "update-index", "--add", "--cacheinfo", f"100644,{blob},sub")
    plain = index.read_bytes()
    git(folder, "update-index", "--cacheinfo", f"160000,{'1' * 40},sub")
    linked = index.read_bytes()
    assert len(plain) == len(linked) and hubgit.GITLINK_MODE in linked and hubgit.GITLINK_MODE not in plain
    index.write_bytes(plain)
    env = hubgit.environment()
    dot_git = str(folder / ".git")
    assert hubgit._gitlinks("git", folder, env, dot_git, False) == []
    before = os.stat(index)
    time.sleep(0.05)
    with open(index, "r+b") as f:   # in place: the same inode and size
        f.write(linked)
    os.utime(index, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = os.stat(index)
    assert (after.st_ino, after.st_size, after.st_mtime_ns) == (before.st_ino, before.st_size, before.st_mtime_ns)
    assert hubgit._gitlinks("git", folder, env, dot_git, False) == ["sub"]


@needs_git
def test_run_git_is_hardened_too(tmp_path):
    folder = tmp_path / "r"
    folder.mkdir()
    git(folder, "init", "-q")
    marks = tmp_path / "marks"
    marks.mkdir()
    git(folder, "config", "core.fsmonitor", marker_cmd(marks / "fsmonitor", "exit 1"))
    put(folder, "a.txt", "a\n")
    assert run_git(folder, "status", "--porcelain").returncode == 0
    assert ran(marks) == []
    # no transport: a fetch from a local repository is refused
    proc = run_git(folder, "fetch", "-q", str(folder))
    assert proc.returncode != 0 and b"not allowed" in proc.stderr


# ---- defense in depth: the isolation of sessions without network --------------------------------------------------
def test_dreamteams_git_runs_isolated_only_with_network_off_and_a_real_isolation(tmp_path, monkeypatch):
    from conftest import FakeSwarm

    swarm = FakeSwarm(tmp_path / "p", {"swarm": {"network": "off"}})
    try:
        ws = Workspace(swarm)
        calls: list[tuple[list[str], bool]] = []
        monkeypatch.setattr(hubgit, "run", lambda argv, **kw: calls.append((argv, kw["offline"])) or
                            subprocess.CompletedProcess(argv, 0, b"", b""))
        monkeypatch.setattr(hubgit, "guard", lambda git, root, env, offline=False: hubgit.Guard())
        ws._run_git(None, ("status",), True, None, False)
        ws.git_isolated = True   # Swarm._prepare_offline under "docker" or "netns"
        ws._run_git(None, ("status",), True, None, False)
        swarm.config.swarm.network = "on"
        ws._run_git(None, ("status",), True, None, False)
        assert [offline for _argv, offline in calls] == [False, True, False]
    finally:
        swarm.db.close()


@needs_git
@pytest.mark.parametrize("network", ["off", "on"])
async def test_the_session_checks_and_commits_run_isolated_too_without_network(tmp_path, monkeypatch, network):
    """sessions.py's git in a folder of the user (the check before adopting it, the check at every start, the initial
    commit, the commit that removes the session's MCP servers when it leaves) and the workspace's init() run like
    the members' commands when the session has no network, whatever the repository's config holds by then. Here
    "netns" without a prefix: the git itself runs as usual."""
    from test_session_workspace import _fake_forged, _manager, _user_repo

    netoff.reset()
    netoff._detected.update(kind="netns", prefix=[])
    real_run = hubgit.run
    seen: list[tuple[str, bool]] = []

    def run(argv, *, cwd, offline=False, **kw):
        seen.append((os.path.normcase(os.path.realpath(cwd)), offline))
        return real_run(argv, cwd=cwd, offline=offline, **kw)

    monkeypatch.setattr(hubgit, "run", run)
    project = _user_repo(tmp_path / "project")
    bare = tmp_path / "bare"   # a folder of the user that is no repository yet: initialized when adopted
    put(bare, "notes.md", "mine\n")
    manager = _manager(tmp_path)
    try:
        sid = (await manager.create("Tools", start=False, overrides={"swarm": {"network": network}}))["id"]
        _fake_forged(await manager.load(sid), "tool")
        assert (await manager.set_workspace(sid, str(project)))["mode"] == "adopted"
        swarm = await manager.start(sid)
        assert await swarm.workspace.autocommit(ANNA, "shell: ls")
        await manager.stop(sid)
        result = await manager.set_workspace(sid, str(bare))
        assert result["git"] == "initialized" and result["old_changed"]["commit"], result
        in_project = [offline for folder, offline in seen
                      if folder.startswith(os.path.normcase(os.path.realpath(project)))]
        in_bare = [offline for folder, offline in seen if folder.startswith(os.path.normcase(os.path.realpath(bare)))]
        assert len(in_project) > 5 and in_bare, seen
        assert set(in_project) == set(in_bare) == {network == "off"}, seen
    finally:
        await manager.shutdown()
        netoff.reset()


def test_the_isolation_of_a_session_check_is_decided_by_its_network_and_the_machine(tmp_path, monkeypatch):
    opened: list[Path] = []
    monkeypatch.setattr(netoff, "prepare_workspace", lambda root: opened.append(root) or 0)
    for kind, off, folder, isolated in (("docker", True, tmp_path, True), ("netns", True, tmp_path, True),
                                        ("env", True, tmp_path, False), ("docker", False, tmp_path, False),
                                        ("docker", True, None, True)):
        netoff.reset()
        netoff._detected.update(kind=kind, prefix=[])
        assert workspace_mod.isolated_git(off, folder) is isolated, (kind, off)
    netoff.reset()
    # only in the Docker sandbox, and only where that git writes: opened to the offline user first
    assert opened == [tmp_path]


def _netns_available() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    netoff.reset()
    try:
        return netoff.isolation() == "netns"
    finally:
        netoff.reset()


@needs_git
@pytest.mark.skipif(not _netns_available(), reason="no unprivileged network namespaces (unshare -rn) here")
async def test_in_a_real_network_namespace_whatever_slips_through_runs_there(make_fake_swarm, user_home, tmp_path):
    """A filter of the "global" config (which a member could write under this isolation: it is the user's home)
    runs in the session's namespace, not in DreamTeam's network."""
    netoff.reset()
    try:
        assert netoff.isolation() == "netns"
        seen = tmp_path / "ns.txt"
        (user_home / ".gitconfig").write_text(
            f"[filter \"trusted\"]\n\tclean = \"readlink /proc/self/ns/net > '{seen.as_posix()}'; cat\"\n",
            encoding="utf-8")
        swarm = make_fake_swarm({"swarm": {"network": "off"}})
        ws = Workspace(swarm)
        swarm.workspace = ws
        await ws.init()
        ws.git_isolated = True
        put(ws.root, ".gitattributes", "*.dat filter=trusted\n")
        await ws.write(ANNA, "x.dat", "x\n")
        assert seen.exists() and seen.read_text().strip() != os.readlink("/proc/self/ns/net")
        assert "x.dat" in git(ws.root, "ls-files")
    finally:
        netoff.reset()


DOCKER_IMAGE = f"dreamteam-hubgit-test:pytest-{os.getpid()}"
DOCKER_CHECK = r"""
import asyncio, json, os, subprocess, sys
from pathlib import Path
from dreamteam import netoff
from dreamteam.config import Config
from dreamteam.workspace import Workspace, commit_paths, isolated_git

class Bus:
    def emit(self, *a, **k): pass
class Db:
    def all(self, *a, **k): return []
    def execute(self, *a, **k): return None
class Swarm:
    def __init__(self, config): self.config, self.bus, self.db = config, Bus(), Db()

problem = netoff.setup_firewall(10001, [8765])
os.environ[netoff.OFFLINE_USER_ENV] = "10001:10001"
os.umask(0o002)
os.setgid(10001)   # as sandbox_entry leaves the hub: root with the offline user's group
os.makedirs("/swarm/p", exist_ok=True)
marks = Path("/tmp/marks"); marks.mkdir(mode=0o777); os.chmod(marks, 0o777)
(marks / "probe.py").write_text(
    "import socket, sys\ntry:\n    socket.create_connection((sys.argv[1], 80), timeout=3); print('OPEN')\n"
    "except OSError as e:\n    print('errno', e.errno)\n")
# "anything missed": a filter of the system config (git's settings in the image are the system's)
subprocess.run(["git", "config", "--system", "filter.sys.clean",
                "id -u > /tmp/marks/sys-uid; python3 /tmp/marks/probe.py %s > /tmp/marks/sys-net; cat" % sys.argv[1]],
               check=True)
read = lambda name: (marks / name).read_text().strip() if (marks / name).exists() else None

async def main():
    config = Config.from_dict(Path("/swarm/p"), {"swarm": {"network": "off"}})
    ws = Workspace(Swarm(config))
    await ws.init()
    assert netoff.isolation() == "docker"
    netoff.prepare_workspace(ws.root)
    ws.git_isolated = True
    root = ws.root
    (root / ".gitattributes").write_text("*.md filter=probe\n*.dat filter=sys\n")
    subprocess.run(["git", "-C", str(root), "config", "filter.probe.clean", "id -u > /tmp/marks/local; cat"],
                   check=True)
    await ws.write("Anna", "a.md", "a\n")
    await ws.write("Anna", "b.dat", "b\n")
    files = subprocess.run(["git", "-C", str(root), "ls-files"], capture_output=True, text=True).stdout.split()
    out = {"problem": problem, "local": read("local"), "sys_uid": read("sys-uid"), "sys_net": read("sys-net"),
           "files": files}
    for name in ("sys-uid", "sys-net"):
        (marks / name).unlink()
    # sessions.py's git for such a session (its checks, a path commit): isolated as well
    (root / "b.dat").write_text("b2\n")
    made, error = commit_paths(root, ["b.dat"], "a session's commit", offline=isolated_git(True, root))
    out.update(session_commit=error or bool(made), session_uid=read("sys-uid"), session_net=read("sys-net"))
    for name in ("sys-uid", "sys-net"):
        if (marks / name).exists():
            (marks / name).unlink()
    # a new workspace, isolated from its init() on (Swarm.start: _prepare_offline first): its repository is made
    fresh = Workspace(Swarm(Config.from_dict(Path("/swarm/q"), {"swarm": {"network": "off"}})))
    fresh.git_isolated = True
    await fresh.init()
    # (the image trusts no repository of another uid: the check says which one, as DreamTeam's own git does)
    out["fresh"] = subprocess.run(["git", "-c", f"safe.directory={fresh.root}", "-C", str(fresh.root), "log",
                                   "--format=%s|%an", "-1"],
                                  capture_output=True, text=True).stdout.strip()
    out["fresh_owner"] = (fresh.root / ".git" / "HEAD").stat().st_uid
    ws.git_isolated = False   # the control: DreamTeam's git as the hub itself (root, its network)
    await ws.write("Anna", "c.dat", "c\n")
    out.update(control_uid=read("sys-uid"), control_net=read("sys-net"))
    print(json.dumps(out))

asyncio.run(main())
"""


def _docker_tests() -> bool:
    if os.environ.get("DREAMTEAM_DOCKER_TESTS") != "1" or not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@pytest.mark.skipif(not _docker_tests(), reason="opt-in: DREAMTEAM_DOCKER_TESTS=1 and a Docker daemon (builds an "
                                                "image under a tag of its own)")
def test_in_the_docker_sandbox_without_network_dreamteams_git_runs_as_the_offline_user(tmp_path):
    """The sandbox image under a test tag, the firewall in place: a member's filter in .git/config does not run at
    all, and one DreamTeam cannot know of (here: of the system config) runs as the offline user, without network;
    the control without the isolation runs it as root, unfiltered."""
    import json

    from dreamteam import docker as docker_module

    (tmp_path / "check.py").write_text(DOCKER_CHECK, encoding="utf-8")
    try:
        docker_module.build_image(DOCKER_IMAGE)
        out = subprocess.run(["docker", "run", "--rm", "--cap-add", "NET_ADMIN", "-v", f"{tmp_path}:/check:ro",
                              "--entrypoint", "python", DOCKER_IMAGE, "/check/check.py", "192.0.2.1"],
                             capture_output=True, text=True, timeout=600)
        assert out.returncode == 0, out.stdout + out.stderr
        result = json.loads(out.stdout.strip().splitlines()[-1])
        assert result["problem"] is None, result
        assert result["local"] is None, result                      # disarmed: never ran
        assert result["sys_uid"] == "10001", result                 # what slipped through: the offline user's
        assert result["sys_net"] == "errno 111", result             # ... rejected by the firewall at once
        assert "a.md" in result["files"] and "b.dat" in result["files"], result
        assert result["session_commit"] is True and result["session_uid"] == "10001", result
        assert result["session_net"] == "errno 111", result
        assert result["fresh"] == "Initialize workspace|DreamTeam" and result["fresh_owner"] == 10001, result
        assert result["control_uid"] == "0" and result["control_net"] != "errno 111", result
    finally:
        subprocess.run(["docker", "image", "rm", "-f", DOCKER_IMAGE], capture_output=True, timeout=120)
