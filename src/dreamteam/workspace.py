"""Shared workspace: files visible to every member, advisory locks and git autocommits."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any

from . import netoff
from .models import USER, ToolError

log = logging.getLogger(__name__)

DEFAULT_GITIGNORE = "node_modules/\n.venv/\n__pycache__/\n*.pyc\n.DS_Store\n.scratch/\n"
SCRATCH_DIR = ".scratch"   # the agents' temporary files: never committed
SCRATCH_GITIGNORE = ("# DreamTeam: temporary files of the agents. Git ignores this folder, this file included;\n"
                     "# nothing in it is ever committed.\n*\n")
# `git add` pathspecs that keep SCRATCH_DIR out: the path itself and all below it (without glob magic `*` matches "/").
# "[.]" rather than ".": git add takes an exclude spelled out literally as naming the path, and when the root
# .gitignore ignores .scratch/ (DEFAULT_GITIGNORE does) it reports it as an ignored path and exits 1 every time.
SCRATCH_EXCLUDES = (":(exclude)[.]scratch", ":(exclude)[.]scratch/*")
# The swarm's skills as the agents read them: a read-only copy DreamTeam rebuilds from the session's own copies
# (docs/SPEC.md §19). A member's scratch folder never starts with "_" (email_slug), so it cannot be one.
SKILLS_MIRROR = f"{SCRATCH_DIR}/_skills"
# inherited variables that would point an agent's git at another repository or index
GIT_LOCATION_VARS = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                               "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_PREFIX"})
BINARY_PROBE_BYTES = 8192
LIST_MAX_ENTRIES = 500
TREE_MAX_ENTRIES = 5000
MAX_LOCK_MINUTES = 24 * 60
GIT_TIMEOUT = 120.0
COMMIT_SUBJECT_MAX = 72   # the subject of an autocommit that names what changed: "shell: src/app.py, x.json (+3 more)"
COMMIT_PATH_MAX = 40      # one path in it; a longer one keeps its end: "…/models/user.py"
COMMIT_DETAIL_MAX = 2000  # its body (the command that made the change), cut in the middle beyond this
NESTED_SHOWN = 5          # nested repositories named in the note about what an autocommit left out
NESTED_PATHSPECS_MAX = 100  # untracked folders passed to `git ls-files` by name; more: it lists all untracked files
# pathspecs passed on git's command line up to this many characters in all; more go through stdin
# (--pathspec-from-file, git 2.25+): Windows limits a command line to 32767 characters, Linux and macOS to far more
PATHSPEC_ARGV_MAX = 8000 if sys.platform == "win32" else 100_000
MOVED_CANDIDATES_MAX = 500  # new files compared with the tracked files a call deleted (a rename by `mv`)
# The body of a commit of held new files (docs/SPEC.md §7 "Held new files"): who it is attributed to and why it came
# late. {others}: "" or HELD_OTHERS
HELD_NOTE = ("New files first seen after this command of {owner}{others}, committed {when}: a new file waits until "
             "it outlives one more command, so leftovers that the next command deletes (of a test run, say) never "
             "enter the history.")
HELD_OTHERS = " ({names} also had a command running then; git cannot tell who wrote a file)"
# Directories shown but never expanded in listings (dependency caches, virtualenvs).
NO_EXPAND = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
                       ".pytest_cache", ".ruff_cache", ".tox"})
_WINDOWS = sys.platform == "win32"
# Names Windows (and git for Windows) cannot use for a file or directory, with or without extension.
_WIN_RESERVED = re.compile(r"(con|prn|aux|nul|conin\$|conout\$|com[1-9¹²³]|lpt[1-9¹²³]) *(\..*)?",
                           re.IGNORECASE)
_WIN_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
# A reparse point with this bit set redirects elsewhere (symlink, junction); others (OneDrive
# placeholders, dedup, ...) are ordinary files and directories.
_REPARSE_TAG_NAME_SURROGATE = 0x20000000

_TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
     "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya"],
))


def email_slug(name: str) -> str:
    """ASCII-only local part for a commit e-mail ("Анна Смирнова" -> "anna.smirnova")."""
    text = "".join(_TRANSLIT.get(ch, ch) for ch in (name or "").lower())
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", ".", text).strip(".")
    return slug or "agent"


def git_identity(actor: str | None) -> tuple[str, str]:
    """(name, e-mail) of commits made for `actor`: the autocommits and the agent's own shell."""
    name = actor or "DreamTeam"
    return name, f"{email_slug(name)}@dreamteam.local"


def scratch_rel(actor: str) -> str:
    """The member's own scratch folder, relative to the workspace root: ".scratch/anna.smirnova/"."""
    return f"{SCRATCH_DIR}/{email_slug(actor)}/"


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def _split_lines(text: str) -> list[str]:
    """Split into lines the way editors number them (a trailing newline does not add a line)."""
    if not text:
        return []
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


def _norm(rel: str) -> str:
    return rel.casefold() if _WINDOWS else rel


def _covers(lock_path: str, rel: str) -> bool:
    """True if a lock on `lock_path` (file or directory) protects `rel`."""
    lock_path, rel = _norm(lock_path), _norm(rel)
    return rel == lock_path or rel.startswith(lock_path + "/")


def in_scratch(rel: str) -> bool:
    """`rel` (workspace-relative, "/"-separated) is `.scratch` or inside it."""
    return _covers(SCRATCH_DIR, rel.rstrip("/"))


def scratch_is_local(root: Path) -> bool:
    """`<root>/.scratch` is a plain folder of the workspace (not a file, nor a link or junction that resolves
    elsewhere): only then do DreamTeam's own files (job logs, the skills mirror) go there."""
    folder = Path(root) / SCRATCH_DIR
    try:
        if not folder.is_dir() or folder.is_symlink():
            return False
        return os.path.normcase(str(folder.resolve())) == os.path.normcase(str(Path(root).resolve() / SCRATCH_DIR))
    except OSError:
        return False


def is_link(entry: os.DirEntry | Path | str) -> bool:
    """Symlinks and Windows junctions (a directory entry or a path): listed but never followed (they may point
    outside). Reparse points that do not redirect (OneDrive/cloud placeholders, dedup) are ordinary entries."""
    if not hasattr(entry, "is_symlink"):  # a str (DirEntry and Path have the same two methods)
        entry = Path(entry)
    try:
        if entry.is_symlink():
            return True
        st = entry.stat(follow_symlinks=False)
        if not getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            return False
        tag = getattr(st, "st_reparse_tag", None)
        return tag is None or bool(tag & _REPARSE_TAG_NAME_SURROGATE)
    except OSError:
        return False


def long_path(path: str | os.PathLike) -> str:
    """`path` in the extended-length form on Windows (`\\\\?\\C:\\...`, `\\\\?\\UNC\\server\\...`), so deep files
    (git objects, a skill's nested folders under a long session path) pass the 260-character limit of plain paths;
    the path unchanged elsewhere."""
    target = os.fspath(path)
    if not _WINDOWS or target.startswith("\\\\?\\"):
        return target
    full = os.path.abspath(target)
    return "\\\\?\\UNC\\" + full[2:] if full.startswith("\\\\") else "\\\\?\\" + full


def rmtree(path: Path) -> None:
    """Remove a directory tree (best effort: what cannot be removed stays); read-only files (git objects on Windows,
    the skills mirror) are made writable first."""
    def on_error(func, target, _exc):
        try:
            if not is_link(target):  # chmod follows a link: what it points to (maybe outside) keeps its mode
                os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    if not Path(path).exists():
        return
    target = long_path(path)
    if sys.version_info >= (3, 12):
        shutil.rmtree(target, onexc=on_error)
    else:
        shutil.rmtree(target, onerror=lambda func, target, info: on_error(func, target, info[1]))


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # control characters other than tab and newline


def _one_line(text: str) -> str:
    return " ".join(_CONTROL.sub(" ", text or "").split())


def _short_path(path: str, limit: int = COMMIT_PATH_MAX) -> str:
    """`path` in at most `limit` characters: its last parts behind "…/" ("…/models/user.py"), or the start of a file
    name that alone is too long ("a_very_long_file_na…")."""
    if len(path) <= limit:
        return path
    *folders, shown = path.split("/")
    if len(shown) > limit:
        return shown[:limit - 1] + "…"
    if len(shown) + 2 > limit:
        return shown
    for folder in reversed(folders):  # "…/" + folder + "/" + what is shown so far
        if len(folder) + len(shown) + 3 > limit:
            break
        shown = f"{folder}/{shown}"
    return "…/" + shown


def _describe_paths(paths: list[str], room: int) -> str:
    """As many of `paths` as fit in `room` characters (at least one), then "(+N more)" for the others."""
    names = [_short_path(p) for p in paths]
    count, used = 1, 0
    for i, name in enumerate(names):
        used += len(name) + (2 if i else 0)
        rest = len(names) - i - 1
        if used + (len(f" (+{rest} more)") if rest else 0) > room:
            break
        count = i + 1
    rest = len(names) - count
    return ", ".join(names[:count]) + (f" (+{rest} more)" if rest else "")


def commit_message(kind: str, paths: list[str], detail: str = "") -> str:
    """The message of an autocommit made for `kind` (e.g. "shell", "job j3"): the subject names what is committed
    ("shell: src/app.py, data/lp.json (+3 more)", at most COMMIT_SUBJECT_MAX characters; renamed and deleted files
    too), the body keeps `detail` (e.g. the command that made the change), cut in the middle beyond
    COMMIT_DETAIL_MAX. Without paths (e.g. a merge left in progress being concluded) the subject quotes `detail`."""
    kind = _one_line(kind) or "update"
    body = _CONTROL.sub("", (detail or "").replace("\r\n", "\n").replace("\r", "\n"))
    body = "\n".join(line.rstrip() for line in body.split("\n")).strip("\n")
    if len(body) > COMMIT_DETAIL_MAX:
        head = COMMIT_DETAIL_MAX * 2 // 5
        tail = COMMIT_DETAIL_MAX - head
        body = f"{body[:head]}\n[... {len(body) - head - tail} characters skipped ...]\n{body[-tail:]}"
    names = [_one_line(p) for p in paths if p]
    if names:
        subject = f"{kind}: {_describe_paths(names, COMMIT_SUBJECT_MAX - len(kind) - 2)}"
    else:
        subject = f"{kind}: {_one_line(body)}" if body.strip() else kind
    if len(subject) > COMMIT_SUBJECT_MAX:
        subject = subject[:COMMIT_SUBJECT_MAX - 1] + "…"
    message = f"{subject}\n\n{body}" if body.strip() else subject
    return message.encode("utf-8", "replace").decode("utf-8")  # an unpaired surrogate cannot reach git


def _encode(text: str, what: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ToolError(f"{what} contains invalid Unicode (an unpaired surrogate at character {exc.start}); "
                        "remove or replace that character and retry.") from None


def _file_eol(text: str) -> str | None:
    """The file's line ending if it is consistent ("\\n" or "\\r\\n"); None if mixed or no newlines."""
    lf = text.count("\n")
    if not lf:
        return None
    crlf = text.count("\r\n")
    if crlf == lf:
        return "\r\n"
    return "\n" if crlf == 0 else None


def git_env() -> dict[str, str]:
    """The environment for git: inherited GIT_* variables would redirect it elsewhere (GIT_DIR, GIT_INDEX_FILE,
    GIT_WORK_TREE) or override the author (GIT_AUTHOR_NAME/EMAIL beat -c user.name), so they are all dropped."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def agent_git_env(env: dict[str, str], actor: str) -> dict[str, str]:
    """The environment of a member's shell with git pointed at the workspace and commits attributed to the member:
    GIT_DIR & co. dropped, GIT_AUTHOR_* / GIT_COMMITTER_* set (they beat any user.name/email configuration, so the
    repository config is not touched)."""
    out = {k: v for k, v in env.items() if k.upper() not in GIT_LOCATION_VARS}
    name, email = git_identity(actor)
    out.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
    return out


def porcelain_entries(data: bytes) -> list[tuple[str, str]]:
    """(XY, path) of `git status --porcelain -z` output; a rename or copy yields its new and its old path (with -z
    the old path follows as the next field; paths are never quoted)."""
    fields = data.decode("utf-8", "replace").split("\0")
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(fields):
        field = fields[i]
        i += 1
        if len(field) < 4:
            continue
        xy, path = field[:2], field[3:]
        out.append((xy, path.rstrip("/")))
        if ("R" in xy or "C" in xy) and i < len(fields):  # " R": a rename in the work tree (intent-to-add)
            out.append((xy, fields[i].rstrip("/")))
            i += 1
    return out


def git_argv(git: str, root: Path, actor: str | None, args: tuple[str, ...] | list[str],
             safe_directory: str | None = None, *, adopted: bool = False, literal: bool = True) -> list[str]:
    """git with a per-command identity and settings: nothing is read from or written to the repository's own
    configuration for this (an adopted repository of the user keeps its config untouched). `adopted`: the user's
    repository, whose own line-ending settings apply (no core.autocrlf override: on Windows Git's system config has
    core.autocrlf=true, and overriding it would commit CRLF rewrites of the user's LF files). `literal`: pathspecs
    are plain paths (False: pathspec magic such as `:(exclude)` works)."""
    name, email = git_identity(actor)
    return [
        git,
        *(("--literal-pathspecs",) if literal else ()),  # file names like "a[1].txt" are paths, not globs
        "-c", f"user.name={name}",
        "-c", f"user.email={email}",
        *(() if adopted else ("-c", "core.autocrlf=false")),
        "-c", "commit.gpgsign=false",
        "-c", "core.quotepath=false",
        "-c", f"safe.directory={safe_directory or Path(root).resolve().as_posix()}",
        *args,
    ]


def run_git(root: Path, *args: str, actor: str | None = None, git: str | None = None,
            safe_directory: str | None = None, timeout: float = GIT_TIMEOUT, adopted: bool = False,
            literal: bool = True) -> subprocess.CompletedProcess:
    """Run git in `root` synchronously (call it from a thread); OSError when git cannot be run."""
    kwargs: dict[str, Any] = {}
    if _WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    argv = git_argv(git or shutil.which("git") or "git", root, actor, args, safe_directory, adopted=adopted,
                    literal=literal)
    return subprocess.run(argv, cwd=str(root), env=git_env(), stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=timeout, **kwargs)


def git_output(proc: subprocess.CompletedProcess) -> str:
    return ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace").strip()


def nested_repo_paths(root: Path, listed: bytes) -> list[str]:
    """The nested repositories in `git ls-files --others -z` output: git does not descend into a folder with its
    own .git and names it with a trailing "/" (`git add -A` would commit it as a gitlink, mode 160000, without a
    .gitmodules entry: an empty reference nobody can clone). Paths outside `.scratch/` only."""
    found: list[str] = []
    for name in listed.decode("utf-8", "replace").split("\0"):
        path = name.rstrip("/")
        if name.endswith("/") and path and not in_scratch(path) and os.path.lexists(Path(root) / path / ".git"):
            found.append(path)
    return found


def init_repo(root: Path, git: str | None = None, *, adopted: bool = False) -> str | None:
    """`git init` in `root` with a `.gitignore` (unless one exists) and an initial commit of what is there,
    as for a new workspace (synchronous). `adopted`: a folder of the user (its files are committed with the user's
    own line-ending settings, as every later commit there is). Returns None on success, else git's error text."""
    proc = run_git(root, "-c", "init.defaultBranch=main", "init", "-q", git=git, adopted=adopted)
    if proc.returncode != 0:
        return git_output(proc) or f"git init failed with code {proc.returncode}"
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_bytes(DEFAULT_GITIGNORE.encode("utf-8"))
    # An adopted folder may hold cloned or generated projects: left out like in every autocommit (the next shell
    # call's autocommit tells the agent about them), not committed as gitlinks without a .gitmodules
    listed = run_git(root, "ls-files", "--others", "--exclude-standard", "-z", git=git, adopted=adopted)
    nested = nested_repo_paths(root, listed.stdout) if listed.returncode == 0 else []
    if nested:
        log.info("initial commit of %s leaves out nested repositories: %s", root, ", ".join(nested))
    run_git(root, "add", "-A", "--ignore-errors", "--", ".", *(f":(exclude,literal){path}" for path in nested),
            git=git, adopted=adopted, literal=False)
    proc = run_git(root, "commit", "-q", "--no-verify", "--allow-empty", "-m", "Initialize workspace", git=git,
                   adopted=adopted)
    if proc.returncode != 0:
        return git_output(proc) or f"git commit failed with code {proc.returncode}"
    return None


# A git operation that stopped halfway (a file or folder in the git dir): the next commit would conclude it.
GIT_OPERATIONS = (("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert"),
                  ("rebase-merge", "rebase"), ("rebase-apply", "rebase (or git am)"),
                  ("sequencer", "cherry-pick or revert"), ("BISECT_LOG", "bisect"))


def looks_like_git_dir(folder: Path) -> bool:
    """A repository database itself (a bare repository, or the .git folder of one), not a work tree."""
    try:
        return (folder / "HEAD").is_file() and (folder / "objects").is_dir() and (folder / "refs").is_dir()
    except OSError:
        return False


def repo_state(root: Path, git: str | None = None, exclude: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
    """What an existing folder is to git, before it is adopted as a workspace or before a session starts in a
    folder it adopted (synchronous): {"toplevel": the root of the work tree that contains it (None: not in a
    repository), "git_dir": True when it is a repository database (a bare repository or a .git folder), "branch":
    the current branch (None: detached HEAD), "operation": a merge, rebase, cherry-pick, revert or bisect that
    stopped halfway (None: none), "changes": `git status --porcelain` lines (uncommitted changes and untracked,
    not ignored files; `exclude`: folders relative to `root` left out, e.g. the session's own MCP servers),
    "error": why git could not read a repository there (None: fine)}.
    The user's own git configuration applies (line endings: no core.autocrlf override), except settings that
    would hide changes from this check (status.showUntrackedFiles, ignored submodules)."""
    state: dict[str, Any] = {"toplevel": None, "git_dir": False, "branch": None, "operation": None, "changes": [],
                             "error": None}

    def run(*args: str) -> subprocess.CompletedProcess:
        # safe.directory=*: only reading here, and a repository created by another account must not look like none
        return run_git(root, *args, git=git, safe_directory="*", adopted=True, literal=False)

    probe = run("rev-parse", "--is-inside-git-dir", "--is-bare-repository", "--absolute-git-dir")
    if probe.returncode != 0:
        if (root / ".git").exists():
            state["error"] = git_output(probe) or f"git rev-parse failed with code {probe.returncode}"
        return state
    lines = probe.stdout.decode("utf-8", "replace").splitlines()
    if "true" in lines[:2]:
        state["git_dir"] = True
        return state
    git_dir = Path(lines[2].strip()) if len(lines) > 2 else None
    top = run("rev-parse", "--show-toplevel")
    if top.returncode != 0:
        state["error"] = git_output(top) or f"git rev-parse failed with code {top.returncode}"
        return state
    state["toplevel"] = top.stdout.decode("utf-8", "replace").strip()
    branch = run("symbolic-ref", "-q", "--short", "HEAD")
    if branch.returncode == 0:
        state["branch"] = branch.stdout.decode("utf-8", "replace").strip() or None
    if git_dir is not None:
        state["operation"] = next((what for name, what in GIT_OPERATIONS if os.path.lexists(git_dir / name)), None)
    # explicit flags: the user's status.showUntrackedFiles=no or submodule ignore settings must not hide changes
    # that the agents' `git add -A` would commit
    excluded = [f":(exclude,literal){path}" for path in exclude]
    status = run("status", "--porcelain", "--untracked-files=normal", "--ignore-submodules=none", "--",
                 ":(literal).", *excluded)
    if status.returncode != 0:
        state["error"] = git_output(status) or f"git status failed with code {status.returncode}"
    else:
        state["changes"] = [line for line in status.stdout.decode("utf-8", "replace").splitlines() if line.strip()]
    return state


def tracked(root: Path, paths: list[str], git: str | None = None) -> list[str]:
    """Which of `paths` (folders relative to `root`) hold files the repository there tracks (synchronous; [] when
    git cannot tell)."""
    found = []
    for path in paths:
        proc = run_git(root, "ls-files", "--", path, git=git, safe_directory="*", adopted=True)
        if proc.returncode == 0 and proc.stdout.strip():
            found.append(path)
    return found


def commit_paths(root: Path, paths: list[str], message: str, git: str | None = None) -> tuple[str | None, str | None]:
    """Commit the current state of `paths` only (e.g. their removal) in the user's repository at `root`, as
    DreamTeam, leaving any other change there (staged or not) as it is (synchronous). Refused while the repository
    is on a detached HEAD or in the middle of a merge, rebase... (that commit would be the user's). Returns
    (commit hash, None) or (None, why not)."""
    state = repo_state(root, git)
    if state["error"] or state["toplevel"] is None:
        return None, state["error"] or "not a git repository"
    if state["branch"] is None:
        return None, "the repository is on a detached HEAD"
    if state["operation"]:
        return None, f"a {state['operation']} is in progress there"
    proc = run_git(root, "commit", "-q", "--no-verify", "-m", message, "--", *paths, git=git, adopted=True)
    if proc.returncode != 0:
        return None, git_output(proc) or f"git commit failed with code {proc.returncode}"
    head = run_git(root, "rev-parse", "HEAD", git=git, adopted=True)
    return head.stdout.decode("ascii", "replace").strip() or None, None


def _check_windows_names(parts: tuple[str, ...], shown: str) -> None:
    for part in parts:
        if _WIN_BAD_CHARS.search(part):
            raise ToolError(f"Invalid path '{shown}': file names cannot contain < > : \" | ? * "
                            "or control characters on Windows.")
        if part != part.rstrip(". "):
            raise ToolError(f"Invalid path '{shown}': '{part}' ends with a dot or a space, which Windows "
                            "drops from file names. Remove the trailing dot/space.")
        if _WIN_RESERVED.fullmatch(part):
            raise ToolError(f"Invalid path '{shown}': '{part}' is a reserved device name on Windows "
                            "(CON, PRN, AUX, NUL, COM1-9, LPT1-9, also with an extension). Choose another name.")


@dataclass
class HeldFile:
    """A new untracked file an autocommit held back (Workspace.autocommit, hold=True): `owner` is the member whose
    autocommit first saw it, `kind`/`detail` that commit's (the command), `stamp` its (size, mtime) then. `others`:
    the other members with a command running then or an autocommit waiting for the git lock (git cannot tell who
    wrote a file: any of them may have). `waiting`: those of the suspects (owner and others) whose next command the
    file has not outlived yet. `running`: the commands running when it was first seen (serial -> member; the owner's
    own job too), until each has ended and its commit is made. `after`: member -> the serial of the last command
    started when the member had none of `running` left (first sight, or when its last one ended): only a command
    with a higher serial, i.e. one started after anything of the member that may have written the file, counts as
    its next command."""
    owner: str
    kind: str
    detail: str
    stamp: tuple[int, int] | None
    others: tuple[str, ...] = ()
    waiting: set[str] = field(default_factory=set)
    running: dict[int, str] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)

    @property
    def suspects(self) -> tuple[str, ...]:
        return self.owner, *self.others


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info.st_size, info.st_mtime_ns


def _collapse(paths: list[str], units: dict[str, str]) -> list[str]:
    """`paths` (untracked files) as pathspecs: an untracked folder of `git status` (`units`: file -> its folder, or
    itself) all of whose files are in `paths` is named once instead of file by file."""
    chosen = set(paths)
    members: dict[str, list[str]] = {}
    for path, unit in units.items():
        members.setdefault(unit, []).append(path)
    out: list[str] = []
    for unit, files in members.items():
        if unit not in chosen and all(f in chosen for f in files):
            out.append(unit)
            chosen.difference_update(files)
    return out + sorted(chosen)


def _names(names: tuple[str, ...] | list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _held_note(held: HeldFile, when: str | None) -> str:
    """HELD_NOTE for a commit of files held as `held` was; `when` None: at a later autocommit, once each suspect's
    next command has left them in place."""
    others = HELD_OTHERS.format(names=_names(held.others)) if held.others else ""
    if when is None:
        suspects = held.suspects
        when = (f"after the next command{'' if len(suspects) == 1 else 's'} of {_names(suspects)} left them "
                "in place")
    return HELD_NOTE.format(owner=held.owner, others=others, when=when)


class Workspace:
    def __init__(self, swarm: Any):
        self.swarm = swarm
        self.root: Path = Path(swarm.config.workspace_dir)
        self._git_exe: str | None = shutil.which("git")
        self._git_lock = asyncio.Lock()
        self._io_lock = asyncio.Lock()  # read-modify-write of files by concurrent agents
        self._running_git: set[asyncio.Future] = set()  # git commands in threads (they outlive a cancelled caller)
        # member -> nested repositories (untracked folders with their own .git) already named in its tool results
        self._nested_reported: dict[str, set[str]] = {}
        # new untracked files the shell and job autocommits hold back for one more command (in memory)
        self._held: dict[str, HeldFile] = {}
        self._queued: dict[str, int] = {}  # member -> its whole-tree autocommits waiting for the git lock or in it

    # --- setup -----------------------------------------------------------------------------
    @property
    def git_enabled(self) -> bool:
        return bool(self.swarm.config.workspace.git and self._git_exe and (self.root / ".git").exists())

    @property
    def adopted(self) -> bool:
        """The folder was the user's before the session adopted it (session.json "workspace")."""
        return bool(getattr(self.swarm.config, "workspace_adopted", False))

    async def init(self) -> None:
        if getattr(self.swarm.config, "workspace_path", None) is not None and not self.root.is_dir():
            # a folder outside the session (a project of the user, an unplugged drive, a path of the host inside a
            # Docker sandbox) is never created silently: the agents would work in an empty stand-in
            raise FileNotFoundError(f"The workspace folder {self.root} does not exist.")
        self.root.mkdir(parents=True, exist_ok=True)
        # with or without git, before the initial commit (which ignores it anyway): list_files shows it from the start
        self.ensure_scratch()
        if not self.swarm.config.workspace.git:
            return
        if not self._git_exe:
            log.warning("git not found: the workspace will not be versioned")
            return
        async with self._git_lock:
            try:
                if not (self.root / ".git").exists():
                    await self._init_repo()
                if not self.adopted:  # the user's folder: its repository configuration is never modified
                    await self._configure_repo()
            except (OSError, subprocess.SubprocessError) as exc:
                log.warning("git init failed: %s", exc)

    def ensure_scratch(self, actor: str | None = None) -> None:
        """`.scratch/` with its self-ignoring `.gitignore` (written again when missing or changed) and, for `actor`, the
        member's own folder in it. Best effort and synchronous (a few stats): an OSError is logged. A `.scratch` that
        is not a plain folder of the workspace (a file, or a link or junction that resolves elsewhere) is left alone;
        the autocommit still keeps it out of commits."""
        folder = self.root / SCRATCH_DIR
        try:
            if os.path.lexists(folder):
                # _norm: the folder may be cased differently on a case-insensitive file system
                if not folder.is_dir() or _norm(str(folder.resolve())) != _norm(str(self._real_root() / SCRATCH_DIR)):
                    return
            else:
                folder.mkdir()
            marker = folder / ".gitignore"
            data = SCRATCH_GITIGNORE.encode("utf-8")
            # a link is never written through (it could point anywhere); the autocommit still excludes the folder
            if not marker.is_symlink() and (not marker.is_file() or marker.read_bytes() != data):
                marker.write_bytes(data)
            if actor:
                (self.root / scratch_rel(actor)).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("cannot prepare %s: %s", folder, exc)

    async def _configure_repo(self) -> None:
        """Repo-local settings for a repository DreamTeam created, so that git commands run from the shell behave
        like the autocommits (line endings, no signing)."""
        if not (self.root / ".git").exists():
            return
        for key, value in (("core.autocrlf", "false"), ("user.name", "DreamTeam"),
                           ("user.email", "swarm@dreamteam.local"), ("commit.gpgsign", "false")):
            await self._git(None, "config", "--local", key, value)

    async def _init_repo(self) -> None:
        error = await asyncio.to_thread(functools.partial(init_repo, self.root, self._git_exe, adopted=self.adopted))
        if error:
            log.warning("git init of the workspace failed: %s", error)

    # --- paths -----------------------------------------------------------------------------
    def _real_root(self) -> Path:
        return self.root.resolve()

    def resolve(self, path: str) -> Path:
        """Map a caller path (relative to the root, `/` or `\\`) to a real path inside the workspace."""
        raw = str(path if path is not None else "").strip().replace("\\", "/")
        shown = raw or "."
        outside = ToolError(f"Path '{shown}' is outside the workspace. "
                            "Use a path relative to the workspace root, e.g. 'docs/notes.md'.")
        try:
            root = self._real_root()
            given = Path(raw or ".")
            if given.anchor:
                # Never touch a network path (\\host\share, \\?\..., \\.\...) that is not the workspace:
                # resolving it would contact the host (NTLM credential leak) and can stall for seconds.
                if _WINDOWS and given.drive.startswith("\\\\") and not self._lexically_inside(given, root):
                    raise outside
                candidate = given
            else:
                candidate = root / given
            real = candidate.resolve()
        except (OSError, ValueError, RuntimeError) as exc:
            raise ToolError(f"Invalid path '{shown}': {exc}. Use a path relative to the workspace root.") from exc
        if real != root and not real.is_relative_to(root):
            raise outside
        parts = real.relative_to(root).parts
        if any(part.casefold() == ".git" for part in parts):
            raise ToolError(f"Path '{shown}' is inside .git, which is managed automatically. "
                            "Git history is committed for you; choose another path.")
        if _WINDOWS:
            _check_windows_names(parts, shown)
        return real

    def _lexically_inside(self, given: Path, real_root: Path) -> bool:
        norm = PurePath(os.path.normpath(given))
        return any(norm == base or norm.is_relative_to(base) for base in (real_root, self.root))

    def rel(self, p: Path) -> str:
        """Workspace-relative posix path ("." for the root)."""
        for base, target in ((self.root, Path(p)), (self._real_root(), Path(p).resolve())):
            try:
                rel = target.relative_to(base).as_posix()
            except ValueError:
                continue
            return rel or "."
        return Path(p).as_posix()

    def mtime(self, path: str) -> float | None:
        try:
            return self.resolve(path).stat().st_mtime
        except (ToolError, OSError, ValueError):
            return None

    def exists(self, path: str) -> bool:
        try:
            return self.resolve(path).exists()
        except (ToolError, OSError, ValueError):
            return False

    # --- reading -----------------------------------------------------------------------------
    def _existing_file(self, path: str) -> tuple[Path, str]:
        p = self.resolve(path)
        rel = self.rel(p)
        if not p.exists():
            raise ToolError(f"File not found: '{rel}'. Use list_files to see what exists.")
        if p.is_dir():
            raise ToolError(f"'{rel}' is a directory. Use list_files to see its contents.")
        return p, rel

    def _read_bytes(self, p: Path, rel: str) -> bytes:
        try:
            return p.read_bytes()
        except OSError as exc:
            raise ToolError(f"Cannot read '{rel}': {exc.strerror or exc}.") from exc

    def read(self, path: str, offset: int = 1, limit: int = 400) -> str:
        p, rel = self._existing_file(path)
        data = self._read_bytes(p, rel)
        if b"\x00" in data[:BINARY_PROBE_BYTES]:
            raise ToolError(f"'{rel}' is a binary file ({format_size(len(data))}); it cannot be read as text.")
        lines = _split_lines(data.decode("utf-8", errors="replace").removeprefix("\ufeff"))
        total = len(lines)
        if total == 0:
            return f"[{rel}: empty file, 0 lines]"
        offset = max(1, int(offset or 1))
        limit = max(1, int(limit or 400))
        if offset > total:
            raise ToolError(f"offset {offset} is past the end of '{rel}' ({total} lines). "
                            f"Use an offset between 1 and {total}.")
        end = min(total, offset + limit - 1)
        max_chars = self._max_chars()
        # reserve room for the longest possible header and note so the result stays within max_chars
        reserve = len(self._header(rel, total, total, total)) + len(self._note(max_chars, total, total - 1, "line")) + 2
        body, end, cut = self._fit(lines, offset, end, max(80, max_chars - reserve))
        parts = [self._header(rel, offset, end, total), body]
        if cut or end < total:
            parts.append(self._note(max_chars, total, end, cut))
        return "\n".join(parts)

    @staticmethod
    def _header(rel: str, first: int, last: int, total: int) -> str:
        return f"[{rel}: lines {first}-{last} of {total}]"

    @staticmethod
    def _note(max_chars: int, total: int, end: int, cut: str) -> str:
        more = f"continue with offset={end + 1}" if end < total else "end of file"
        if not cut:
            return f"[{total - end} more lines; {more}]"
        line_cut = f"; line {end} was cut" if cut == "line" else ""
        return f"[output capped at {max_chars} characters{line_cut}; {more}]"

    def _max_chars(self) -> int:
        return max(200, int(self.swarm.config.agents.tool_output_chars))

    @staticmethod
    def _fit(lines: list[str], offset: int, end: int, budget: int) -> tuple[str, int, str]:
        """Join lines offset..end within `budget` characters. Returns (body, last line shown, cut kind)."""
        out: list[str] = []
        used = 0
        for number in range(offset, end + 1):
            line = lines[number - 1]
            cost = len(line) + (1 if out else 0)
            if used + cost <= budget:
                out.append(line)
                used += cost
                continue
            if not out:  # a single huge line: show its head
                return line[:budget], number, "line"
            return "\n".join(out), number - 1, "chars"
        return "\n".join(out), end, ""

    def read_raw(self, path: str, max_bytes: int = 2_000_000) -> tuple[str, bool]:
        p, rel = self._existing_file(path)
        try:
            size = p.stat().st_size
            with p.open("rb") as f:
                data = f.read(max(0, max_bytes))
        except OSError as exc:
            raise ToolError(f"Cannot read '{rel}': {exc.strerror or exc}.") from exc
        if b"\x00" in data[:BINARY_PROBE_BYTES]:
            return f"[binary file, {format_size(size)}]", False
        return data.decode("utf-8", errors="replace"), size > len(data)

    # --- writing ------------------------------------------------------------------------------
    def _refuse_skills_mirror(self, actor: str, rel: str) -> None:
        """write_file / edit_file never change the swarm's skills: the mirror is rebuilt from the session's copies
        (a `shell` edit there is undone by the next check, docs/SPEC.md §19). The way to propose the change is named
        only to a caller whose tool set has skill_propose (an agent of an older revision lacks it)."""
        if not _covers(SKILLS_MIRROR, rel.rstrip("/")):
            return
        text = (f"'{SKILLS_MIRROR}/' holds the swarm's skills and is rebuilt by DreamTeam. To change a skill, copy "
                "its folder (without node_modules) into your scratch folder and edit it there")
        try:
            from .tools import frozen

            member = self.swarm.member(actor)
            proposes = member is not None and "skill_propose" in frozen.member_tool_names(self.swarm, member)
        except Exception:  # noqa: BLE001 - only the wording depends on it (test doubles, a view-only swarm)
            proposes = False
        raise ToolError(text + (", then call skill_propose." if proposes else "."))

    async def write(self, actor: str, path: str, content: str) -> str:
        p = self.resolve(path)
        rel = self.rel(p)
        self._refuse_skills_mirror(actor, rel)
        if p.is_dir():
            raise ToolError(f"'{rel}' is a directory; give a file path.")
        data = _encode(content or "", "content")
        async with self._io_lock:
            self.check_writable(actor, rel)
            existed = p.exists()
            if existed and self._read_bytes(p, rel) == data:
                return f"{rel} unchanged (same content)."
            commit = await self._store_and_commit(actor, p, rel, data, "write" if existed else "create")
        verb = "Updated" if existed else "Created"
        n = len(_split_lines(content or ""))
        return (f"{verb} {rel} ({n} line{'s' if n != 1 else ''}, {format_size(len(data))})"
                + self._commit_note(commit, rel))

    async def edit(self, actor: str, path: str, old: str, new: str, replace_all: bool = False) -> str:
        if not old:
            raise ToolError("old_text must not be empty. Use write_file to create or overwrite a whole file.")
        new = new or ""
        _encode(new, "new_text")
        async with self._io_lock:
            p, rel = self._existing_file(path)
            self._refuse_skills_mirror(actor, rel)
            self.check_writable(actor, rel)
            updated, replaced = self._apply_edit(rel, self._read_bytes(p, rel), old, new, replace_all)
            if updated is None:
                return f"{rel} unchanged (old_text equals new_text)."
            commit = await self._store_and_commit(actor, p, rel, updated.encode("utf-8"), "edit")
        return (f"Edited {rel}: replaced {replaced} occurrence{'s' if replaced != 1 else ''}"
                + self._commit_note(commit, rel))

    def _apply_edit(self, rel: str, raw: bytes, old: str, new: str, replace_all: bool) -> tuple[str | None, int]:
        """Return (new text or None if nothing changes, occurrences replaced); ToolError on a bad match."""
        if b"\x00" in raw[:BINARY_PROBE_BYTES]:
            raise ToolError(f"'{rel}' is a binary file; it cannot be edited as text.")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolError(f"'{rel}' is not valid UTF-8; use write_file to replace it.") from exc
        old, new, count = self._match(text, old, new)
        if count == 0:
            raise ToolError(f"old_text was not found in '{rel}'." + self._near_hint(text, old)
                            + " Read the file (read_file) and copy the exact text, including whitespace.")
        if count > 1 and not replace_all:
            raise ToolError(f"old_text occurs {count} times in '{rel}'. Include more surrounding lines "
                            "to make it unique, or pass replace_all=true to replace every occurrence.")
        eol = _file_eol(text)
        if eol and "\n" in new:  # keep the file's line endings (read_file shows lines without CR)
            new = new.replace("\r\n", "\n")
            if eol == "\r\n":
                new = new.replace("\n", "\r\n")
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        return (None if updated == text else updated), (count if replace_all else 1)

    @staticmethod
    def _match(text: str, old: str, new: str) -> tuple[str, str, int]:
        """Count `old` in `text`; if absent, retry with the file's line endings (LF vs CRLF)."""
        count = text.count(old)
        if count or "\n" not in old:
            return old, new, count

        def lf(s: str) -> str:
            return s.replace("\r\n", "\n")

        variants = [(lf(old), lf(new))]
        if "\r\n" in text:
            variants.append((lf(old).replace("\n", "\r\n"), lf(new).replace("\n", "\r\n")))
        for old_v, new_v in variants:
            found = text.count(old_v)
            if found:
                return old_v, new_v, found
        return old, new, 0

    @staticmethod
    def _near_hint(text: str, old: str) -> str:
        first = next((ln.strip() for ln in old.splitlines() if ln.strip()), "")
        if not first:
            return ""
        for number, line in enumerate(_split_lines(text), 1):
            if first in line.strip():
                return f" A similar line exists at line {number} (check whitespace and the following lines)."
        return ""

    async def _store(self, p: Path, rel: str, data: bytes) -> None:
        def _write() -> None:
            p.parent.mkdir(parents=True, exist_ok=True)
            try:
                p.write_bytes(data)
            except PermissionError:
                # a read-only file, e.g. a copy of an attached skill (the mirror's files are read-only and a copy
                # keeps that): write_file / edit_file is the member's explicit edit, so it is made writable first.
                # Not a file with other hard links (a skill environment's node_modules view shares its files)
                info = p.lstat() if p.exists() else None
                if (info is None or not stat.S_ISREG(info.st_mode) or info.st_nlink > 1
                        or info.st_mode & stat.S_IWUSR):
                    raise
                os.chmod(p, stat.S_IMODE(info.st_mode) | stat.S_IWUSR | stat.S_IRUSR)
                p.write_bytes(data)

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            raise ToolError(f"Cannot write '{rel}': {exc.strerror or exc}.") from exc

    async def _store_and_commit(self, actor: str, p: Path, rel: str, data: bytes, op: str) -> str | None:
        """Write the file, emit file.changed and commit only this path as `actor`.

        The git lock is held from the write to the commit so that no other commit (e.g. a shell
        autocommit doing `add -A`) picks the change up under another member's name."""
        async with self._git_lock:
            await self._store(p, rel, data)
            self.swarm.bus.emit("file.changed", actor, path=rel, op=op)
            return await self._commit_locked(actor, f"{op} {rel}", rel)

    @staticmethod
    def _commit_note(commit: str | None, rel: str = "") -> str:
        if in_scratch(rel):
            return " (in .scratch/: not committed)."
        return f", commit {commit[:8]}." if commit else "."

    # --- listing ------------------------------------------------------------------------------
    def _scan(self, directory: Path) -> list[os.DirEntry]:
        try:
            with os.scandir(directory) as it:
                entries = [e for e in it if e.name.casefold() != ".git"]
        except OSError:
            return []
        return sorted(entries, key=lambda e: (not self._is_dir(e), e.name.casefold()))

    @staticmethod
    def _is_dir(entry: os.DirEntry) -> bool:
        try:
            return entry.is_dir(follow_symlinks=False)
        except OSError:
            return False

    _is_link = staticmethod(is_link)

    @staticmethod
    def _stat(entry: os.DirEntry) -> tuple[int, float]:
        try:
            st = entry.stat(follow_symlinks=False)
            return st.st_size, st.st_mtime
        except OSError:
            return 0, 0.0

    def list(self, path: str = ".", depth: int = 2) -> str:
        p = self.resolve(path)
        rel = self.rel(p)
        if not p.exists():
            raise ToolError(f"Path not found: '{rel}'. Use list_files('.') to see the workspace root.")
        if not p.is_dir():
            return f"{rel} ({format_size(p.stat().st_size)})"
        depth = max(1, min(int(depth or 2), 20))
        lines = ["./" if rel == "." else f"{rel}/"]
        entries = self._scan(p)
        if not entries:
            lines.append("  (empty)")
        elif self._list_into(entries, 1, depth, lines):
            lines.append(f"[listing truncated after {LIST_MAX_ENTRIES} entries; "
                         "list a subdirectory or use a smaller depth]")
        return "\n".join(lines)

    def _list_into(self, entries: list[os.DirEntry], level: int, depth: int, lines: list[str]) -> bool:
        """Append indented entries; returns True when the entry cap was hit."""
        indent = "  " * level
        for entry in entries:
            if len(lines) - 1 >= LIST_MAX_ENTRIES:
                return True
            if self._is_link(entry):
                lines.append(f"{indent}{entry.name} (link)")
            elif not self._is_dir(entry):
                lines.append(f"{indent}{entry.name} ({format_size(self._stat(entry)[0])})")
            else:
                children = self._scan(Path(entry.path))
                if not children:
                    lines.append(f"{indent}{entry.name}/ (empty)")
                # the agents' temporary files would inflate every listing: list_files(".scratch/...") shows them
                elif level >= depth or entry.name in NO_EXPAND or _norm(entry.name) == SCRATCH_DIR:
                    n = len(children)
                    lines.append(f"{indent}{entry.name}/ ({n} entr{'y' if n == 1 else 'ies'})")
                else:
                    lines.append(f"{indent}{entry.name}/")
                    if self._list_into(children, level + 1, depth, lines):
                        return True
        return False

    def tree(self, path: str = ".", depth: int = 4) -> list[dict]:
        p = self.resolve(path)
        rel = self.rel(p)
        if not p.exists():
            raise ToolError(f"Path not found: '{rel}'.")
        if not p.is_dir():
            st = p.stat()
            return [{"path": rel, "type": "file", "size": st.st_size, "mtime": st.st_mtime}]
        out: list[dict] = []
        self._tree_into(p, rel, 1, max(1, min(int(depth or 4), 20)), out)
        return out

    def _tree_into(self, directory: Path, base: str, level: int, depth: int, out: list[dict]) -> None:
        for entry in self._scan(directory):
            if len(out) >= TREE_MAX_ENTRIES:
                return
            size, mtime = self._stat(entry)
            is_dir = self._is_dir(entry)
            rel = entry.name if base == "." else f"{base}/{entry.name}"
            out.append({"path": rel, "type": "dir" if is_dir else "file",
                        "size": 0 if is_dir else size, "mtime": mtime})
            if is_dir and level < depth and entry.name not in NO_EXPAND and not self._is_link(entry):
                self._tree_into(Path(entry.path), rel, level + 1, depth, out)

    # --- locks ------------------------------------------------------------------------------
    def _lock_rows(self) -> list[dict]:
        rows = self.swarm.db.all("SELECT path, owner, acquired_at, expires_at FROM file_locks ORDER BY path")
        return [dict(r) for r in rows]

    def _lock_rel(self, path: str) -> str:
        rel = self.rel(self.resolve(path))
        if rel == ".":
            raise ToolError("Cannot lock the whole workspace; lock a file or a directory.")
        return rel

    @staticmethod
    def _describe(lock: dict) -> str:
        left = max(0, round((lock["expires_at"] - time.time()) / 60))
        return f"locked by {lock['owner']} until {_clock(lock['expires_at'])} ({left} min left)"

    def lock(self, actor: str, path: str, minutes: float | None = None) -> str:
        rel = self._lock_rel(path)
        if minutes is None:
            minutes = self.swarm.config.workspace.lock_ttl_minutes
        try:
            minutes = float(minutes)
        except (TypeError, ValueError):
            raise ToolError("minutes must be a positive number (lock duration).") from None
        if not minutes > 0:  # also rejects NaN
            raise ToolError("minutes must be a positive number (lock duration).")
        minutes = min(minutes, MAX_LOCK_MINUTES)
        self.expire_locks()
        mine: dict | None = None
        for lock in self._lock_rows():
            if lock["owner"] == actor:
                if _norm(lock["path"]) == _norm(rel):
                    mine = lock
                continue
            if _covers(lock["path"], rel) or _covers(rel, lock["path"]):
                where = "" if _norm(lock["path"]) == _norm(rel) else f" (lock on '{lock['path']}')"
                raise ToolError(f"'{rel}' is {self._describe(lock)}{where}. "
                                "Message them or wait until it is unlocked.")
        now = time.time()
        expires = now + minutes * 60
        if mine:
            self.swarm.db.execute("UPDATE file_locks SET expires_at = ? WHERE path = ?", (expires, mine["path"]))
            rel = mine["path"]
        else:
            self.swarm.db.execute(
                "INSERT OR REPLACE INTO file_locks(path, owner, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
                (rel, actor, now, expires))
        self.swarm.bus.emit("file.locked", actor, path=rel, owner=actor, expires_at=expires,
                            refreshed=bool(mine))
        verb = "Lock on" if mine else "Locked"
        tail = " refreshed" if mine else ""
        return (f"{verb} {rel}{tail} until {_clock(expires)} ({minutes:g} min). "
                "Unlock it when you are done.")

    def unlock(self, actor: str, path: str, force: bool = False) -> str:
        rel = self.rel(self.resolve(path))
        self.expire_locks()
        lock = next((lk for lk in self._lock_rows() if _norm(lk["path"]) == _norm(rel)), None)
        if lock is None:
            return f"{rel} is not locked."
        forced = lock["owner"] != actor
        if forced:
            if force and actor != USER:
                raise ToolError("Only the user can force-unlock a file locked by someone else.")
            if not force:
                hint = " Pass force=true to unlock it anyway." if actor == USER else \
                    " Ask them to unlock it, or wait until the lock expires."
                raise ToolError(f"'{rel}' is {self._describe(lock)}, not by you.{hint}")
        self.swarm.db.execute("DELETE FROM file_locks WHERE path = ?", (lock["path"],))
        self.swarm.bus.emit("file.unlocked", actor, path=lock["path"], owner=lock["owner"], forced=forced)
        suffix = f" (was locked by {lock['owner']})" if forced else ""
        return f"Unlocked {lock['path']}{suffix}."

    def locks(self) -> list[dict]:
        self.expire_locks()
        return self._lock_rows()

    def check_writable(self, actor: str, path: str) -> None:
        rel = self.rel(self.resolve(path))
        self.expire_locks()
        for lock in self._lock_rows():
            if lock["owner"] == actor:
                continue
            if _covers(lock["path"], rel):
                where = "" if _norm(lock["path"]) == _norm(rel) else f" (lock on '{lock['path']}')"
                raise ToolError(f"'{rel}' is {self._describe(lock)}{where}. "
                                "Ask them to unlock it, or wait until the lock expires.")

    def release_all(self, actor: str) -> int:
        rows = self.swarm.db.all("SELECT path FROM file_locks WHERE owner = ?", (actor,))
        for row in rows:
            self.swarm.db.execute("DELETE FROM file_locks WHERE path = ?", (row["path"],))
            self.swarm.bus.emit("file.unlocked", actor, path=row["path"], owner=actor, forced=False)
        return len(rows)

    def expire_locks(self) -> int:
        rows = self.swarm.db.all("SELECT path, owner FROM file_locks WHERE expires_at <= ?", (time.time(),))
        for row in rows:
            self.swarm.db.execute("DELETE FROM file_locks WHERE path = ?", (row["path"],))
            self.swarm.bus.emit("file.unlocked", None, path=row["path"], owner=row["owner"], expired=True)
        return len(rows)

    # --- git --------------------------------------------------------------------------------
    async def autocommit(self, actor: str, message: str = "", *, kind: str = "", detail: str = "",
                         skipped: list[str] | None = None, hold: bool = False, ended: int | None = None) -> str | None:
        """Commit every change outside `.scratch/` as `actor`, one commit per call. The message is `message` as
        given or, with `kind` (e.g. "shell", "job j3"), commit_message() of what is committed: the subject names the
        changed files, the body holds `detail` (the command that changed them). Untracked folders with their own
        .git (nested repositories) are left out; `skipped` receives their paths (nested_repos_note).
        `hold` (the shell's and the jobs' commits): new untracked files wait until they outlive one more command
        (_hold_new, docs/SPEC.md §7 "Held new files"); `ended` is the serial of the command whose end this commit
        follows (None: none has ended, e.g. a call whose command goes on as a job). Without `hold` everything is
        committed now, files held for anyone included. Returns the last commit made."""
        if not self.git_enabled:
            return None
        # a member whose command has ended but whose commit waits for the lock may have written what this one sees
        self._queued[actor] = self._queued.get(actor, 0) + 1
        try:
            async with self._git_lock:
                return await self._commit_locked(actor, message, None, kind=kind, detail=detail, skipped=skipped,
                                                 hold=hold, ended=ended)
        finally:
            left = self._queued.pop(actor, 1) - 1
            if left > 0:
                self._queued[actor] = left

    async def commit_held(self, actor: str | None, when: str, *, leaving: bool = False) -> str | None:
        """A flush of the new files held back (docs/SPEC.md §7 "Held new files"): `actor`'s task_complete or the end
        of its turn counts as a command of it that they outlived, once none of its commands that were running when
        they were first seen is left (its own job may still write them); with `leaving` (the member leaves, its
        commands stopped) it is no suspect any more. The files that no longer wait for anyone and are still there
        are committed, each under its owner; `actor` None (the session stops): every held file. `when` completes the
        commit's note (HELD_NOTE: "at the end of Anna's turn"). Never raises; the last commit made or None."""
        if not self.git_enabled or not self._held:
            return None
        if actor is not None and not any(actor in h.suspects or not h.waiting for h in self._held.values()):
            return None  # the usual case: no git call at all
        try:
            async with self._git_lock:
                if actor is not None:
                    running, last = self._commands()
                    for held in self._held.values():
                        self._settle_held(held, running, last, None)
                        if leaving:
                            held.running = {s: m for s, m in held.running.items() if m != actor}
                            held.waiting.discard(actor)
                        elif actor in held.after:  # (none of its commands of then is left)
                            held.waiting.discard(actor)
                paths = [path for path, held in self._held.items() if actor is None or not held.waiting]
                if not paths:
                    return None  # e.g. another member's test run that may have left them still runs
                status = await self._git(actor, "status", "--porcelain", "-z", "--untracked-files=normal")
                if status.returncode != 0:
                    log.warning("git status failed: %s", self._output(status))
                    return None
                units, _nested = await self._untracked(actor, porcelain_entries(status.stdout))
                if units is None:
                    return None
                self._forget_held(units)
                made, _done = await self._commit_held_locked([p for p in paths if p in self._held], units, when)
                return made
        except Exception:  # noqa: BLE001 - a flush must never fail a tool call or the session's stop
            log.exception("commit of held new files failed")
            return None

    def held(self) -> dict[str, HeldFile]:
        """The new files held back now (path -> HeldFile), a copy."""
        return dict(self._held)

    def nested_repos_note(self, actor: str, paths: list[str]) -> str:
        """One line for `actor`'s tool result about nested repositories an autocommit left out ("" when there are
        none): each path is named once per session, so a repository the agent keeps does not repeat at every call."""
        seen = self._nested_reported.setdefault(actor, set())
        new = [path for path in dict.fromkeys(paths) if path not in seen]
        if not new:
            return ""
        seen.update(new)
        shown = ", ".join(new[:NESTED_SHOWN]) + (f" and {len(new) - NESTED_SHOWN} more" if len(new) > NESTED_SHOWN
                                                  else "")
        return (f"Not committed: {shown} — folders with their own .git (nested repositories). Delete their .git or "
                f"move them into your scratch folder {scratch_rel(actor)} if they are temporary.")

    async def _untracked(self, actor: str | None,
                         entries: list[tuple[str, str]]) -> tuple[dict[str, str] | None, list[str]]:
        """(the untracked files outside `.scratch/` that `git add -A` would add, each mapped to its `??` entry of
        `git status`: an untracked folder, or the file itself; None when git could not list them) and the untracked
        folders that git sees as repositories of their own: `git add -A` would commit each as a gitlink (mode 160000)
        without a .gitmodules entry, i.e. an empty reference nobody can clone. A nested repository inside an
        untracked folder shows only as that folder in `git status`, so `git ls-files --others` (which stops at a
        nested repository and names it with a trailing "/") looks inside. Tracked submodules are tracked, so never
        listed. Caller holds the git lock."""
        units: dict[str, str] = {}
        folders: list[str] = []
        for xy, path in entries:
            if xy != "??" or not path or in_scratch(path):
                continue
            if os.path.isdir(self.root / path) and not os.path.islink(self.root / path):
                folders.append(path)
            else:
                units[path] = path
        if not folders:
            return units, []
        # literal pathspecs after "--": a folder named "--help" or "a[1]" is a path, not an option or a glob
        spec = ("--", *(f"{path}/" for path in folders)) if len(folders) <= NESTED_PATHSPECS_MAX else ()
        listed = await self._git(actor, "ls-files", "--others", "--exclude-standard", "-z", *spec)
        if listed.returncode != 0:
            log.info("git ls-files --others failed: %s", self._output(listed))
            return None, []
        nested = nested_repo_paths(self.root, listed.stdout)

        def top(name: str) -> str | None:
            parts = name.split("/")
            return next((unit for unit in ("/".join(parts[:i]) for i in range(1, len(parts))) if unit in tops), None)

        tops = set(folders)
        tops -= {top(path) for path in nested}  # a folder holding a nested repository is never named as a whole
        for name in listed.stdout.decode("utf-8", "replace").split("\0"):
            if name and not name.endswith("/") and not in_scratch(name):  # "/": a nested repository
                units[name] = top(name) or name
        return units, nested

    def _forget_held(self, units: dict[str, str]) -> None:
        """Held files that are no longer untracked (deleted, committed by hand or by write_file, ignored now) are
        forgotten: a held file that vanished never enters the history."""
        for path in [path for path in self._held if path not in units]:
            del self._held[path]

    def _commands(self) -> tuple[dict[int, str], int]:
        """(serial -> member of the commands running now: foreground `shell` calls and jobs; the serial of the last
        command started), from the JobManager."""
        jobs = getattr(self.swarm, "jobs", None)
        return jobs.running_commands() if hasattr(jobs, "running_commands") else ({}, 0)

    def _queued_besides(self, member: str, actor: str | None) -> int:
        """`member`'s whole-tree autocommits waiting for the git lock or in it, besides `actor`'s one under way."""
        return self._queued.get(member, 0) - (1 if member == actor else 0)

    def _settle_held(self, held: HeldFile, running: dict[int, str], last: int, actor: str | None) -> None:
        """Forget the commands of `held.running` that have ended and whose commit is made (the end of a command
        queues its commit at once: a member with one waiting may still have to commit what it did; `actor`: the
        member whose commit is under way, not counted). A member whose last such command ended needs a command
        started after now: `after` = `last`."""
        for serial, member in list(held.running.items()):
            if serial not in running and self._queued_besides(member, actor) <= 0:
                del held.running[serial]
                if member not in held.running.values():
                    held.after[member] = last

    async def _hold_new(self, actor: str, kind: str, detail: str, units: dict[str, str],
                        entries: list[tuple[str, str]], ended: int | None) -> tuple[str | None, list[str]]:
        """The hold step of a shell or job autocommit of `actor` (`ended`: the serial of its command that has ended,
        or None). Held files still as they were when first seen count that command for `actor` (HeldFile: it started
        after anything of `actor` that may have written them), and those no longer waiting for anyone are committed
        now, a commit of their own under their owner. A held file rewritten since (deleted and written again by the
        next run) is held anew, like a new file. A new file is held with `actor` as owner, the commands running now
        and the members with an autocommit queued as suspects too (whoever's command left it). A new file with the
        content of a tracked file the call deleted is not held (_moved: the call's own commit shows the rename).
        Returns (the commit made, if any; the moved files). Caller holds the git lock."""
        # after the file listing: a command that ended meanwhile has its commit queued, one that started is running
        running, last = self._commands()
        others = tuple(sorted(({*running.values()} | {m for m in self._queued if self._queued_besides(m, actor) > 0})
                              - {actor}))
        for path, held in list(self._held.items()):
            if held.stamp != _stamp(self.root / path):
                del self._held[path]  # held anew below
                continue
            self._settle_held(held, running, last, actor)
            if ended is not None and actor in held.waiting and ended > held.after.get(actor, ended):
                held.waiting.discard(actor)
        # (also a file a failed commit kept held)
        ready = [path for path, held in self._held.items() if not held.waiting]
        made, done = await self._commit_held_locked(ready, units, None) if ready else (None, set())
        new = [path for path in units if path not in done and path not in self._held]
        moved = await self._moved(actor, entries, new)
        busy = set(running.values())
        for path in new:
            if path not in moved:
                self._held[path] = HeldFile(actor, kind, detail, _stamp(self.root / path), others, {actor, *others},
                                            dict(running), {m: last for m in (actor, *others) if m not in busy})
        return made, sorted(moved)

    async def _moved(self, actor: str, entries: list[tuple[str, str]], new: list[str]) -> set[str]:
        """The new files of `new` with the content of a tracked file deleted in the same call (`mv a.py b.py`): not
        held, so the call's commit shows the rename. Exact content only, and only up to MOVED_CANDIDATES_MAX new files.
        Caller holds the git lock."""
        deleted = [path for xy, path in entries if "D" in xy and not in_scratch(path) and "\n" not in path]
        new = [path for path in new if "\n" not in path]
        if not deleted or not new or len(new) > MOVED_CANDIDATES_MAX:
            return set()
        # the blob ids of the deleted files in HEAD, through stdin (any number of them; `git ls-tree` would need them
        # on the command line): "<id>" per line, "HEAD:<path> missing" for one HEAD lacks
        tree = await self._git(actor, "cat-file", "--batch-check=%(objectname)",
                               input="".join(f"HEAD:{path}\n" for path in deleted).encode("utf-8", "replace"))
        if tree.returncode != 0:
            log.info("git cat-file failed: %s", self._output(tree))
        blobs = {line for line in tree.stdout.decode("utf-8", "replace").splitlines()
                 if re.fullmatch(r"[0-9a-f]{40,64}", line)} if tree.returncode == 0 else set()
        if not blobs:
            return set()
        # the blob ids these files would get (with the repository's filters and line-ending conversion)
        hashed = await self._git(actor, "hash-object", "--stdin-paths",
                                 input="".join(f"{path}\n" for path in new).encode("utf-8", "replace"))
        if hashed.returncode != 0:
            log.info("git hash-object failed: %s", self._output(hashed))
            return set()
        return {path for path, sha in zip(new, hashed.stdout.decode("ascii", "replace").split()) if sha in blobs}

    async def _commit_held_locked(self, paths: list[str], units: dict[str, str],
                                  when: str | None) -> tuple[str | None, set[str]]:
        """Commit held `paths`, one commit per command that first left them, under its owner (their kind and detail:
        "shell (new files): out/a.txt", the command and HELD_NOTE, _held_note(…, when), in the body). Only those
        paths are committed (`git commit -- paths`); a failed commit unstages them again and keeps them held.
        Returns (the last commit, the paths committed). Caller holds the git lock."""
        groups: dict[tuple[str, str, str, tuple[str, ...]], list[str]] = {}
        for path in paths:
            held = self._held[path]
            if os.path.lexists(self.root / path):  # a command running meanwhile may have deleted it
                groups.setdefault((held.owner, held.kind, held.detail, held.others), []).append(path)
        head: str | None = None
        done: set[str] = set()
        for (owner, kind, detail, _others), files in groups.items():
            note = _held_note(self._held[files[0]], when)
            spec = _collapse(files, units)
            message = commit_message(f"{kind} (new files)", sorted(files),
                                     f"{detail}\n\n{note}" if detail.strip() else note)
            add = await self._git_paths(owner, ("add", "-A", "--ignore-errors"), spec)
            if add.returncode != 0:
                log.warning("git add reported errors (committing what it could add): %s", self._output(add))
            commit = await self._git_paths(owner, ("commit", "-q", "--no-verify", "-m", message), spec)
            if commit.returncode != 0:  # e.g. a merge in progress (no partial commit then): retried next time
                log.info("git commit of held files skipped: %s", self._output(commit))
                await self._git_paths(owner, ("reset", "-q"), spec)  # not left staged for someone else's commit
                continue
            for path in files:
                self._held.pop(path, None)
            done.update(files)
            rev = await self._git(owner, "rev-parse", "HEAD")
            head = rev.stdout.decode("ascii", "replace").strip() or head
        return head, done

    async def _git_paths(self, actor: str | None, args: tuple[str, ...], spec: list[str], *,
                         literal: bool = True) -> subprocess.CompletedProcess:
        """git `args` with the pathspecs `spec`: after "--", or through stdin when they are long (PATHSPEC_ARGV_MAX).
        A git older than 2.25 has no --pathspec-from-file: then the command line after all (the limit is low only
        on Windows, whose git is recent)."""
        if sum(len(item) + 3 for item in spec) <= PATHSPEC_ARGV_MAX:
            return await self._git(actor, *args, "--", *spec, literal=literal)
        data = b"".join(item.encode("utf-8", "replace") + b"\0" for item in spec)
        proc = await self._git(actor, *args, "--pathspec-from-file=-", "--pathspec-file-nul", literal=literal,
                               input=data)
        if proc.returncode == 129 and b"pathspec-from-file" in (proc.stderr or b""):  # "unknown option"
            return await self._git(actor, *args, "--", *spec, literal=literal)
        return proc

    async def _commit_locked(self, actor: str, message: str, rel: str | None, *, kind: str = "",
                             detail: str = "", skipped: list[str] | None = None, hold: bool = False,
                             ended: int | None = None) -> str | None:
        """Commit everything except `.scratch/`, nested repositories and held new files (rel None) or just `rel` as
        `actor`. Caller holds the git lock. Nothing under `.scratch/` is ever committed. `kind`, `detail`, `skipped`,
        `hold`, `ended`: see autocommit (rel None only)."""
        if not self.git_enabled:
            return None
        if rel is not None and in_scratch(rel):
            self.ensure_scratch()  # the file may be the marker itself: put it back at once
            return None  # a temporary file of an agent (write_file / edit_file into .scratch/)
        made: str | None = None  # a commit of held files made before the main one
        try:
            if rel:
                spec = ("--", rel)
                # (a new file must show even with the user's status.showUntrackedFiles=no)
                status = await self._git(actor, "status", "--porcelain", "--untracked-files=normal", *spec)
                if status.returncode != 0:
                    log.warning("git status failed: %s", self._output(status))
                    return None
                if not status.stdout.strip():
                    return None
                # --ignore-errors: one file git cannot index must not stop the rest from being versioned
                add = await self._git(actor, "add", "-A", "--ignore-errors", *spec)
                if add.returncode != 0:
                    log.warning("git add reported errors (committing what it could add): %s", self._output(add))
                commit = await self._git(actor, "commit", "-q", "--no-verify", "-m", message or "update", *spec)
            else:
                self.ensure_scratch()  # an agent may have deleted the marker in its last command
                # --untracked-files=normal: a user's status.showUntrackedFiles=no (or =all) must not hide untracked
                # folders (or split them into files) from the nested-repository check; `git add -A` ignores it
                status = await self._git(actor, "status", "--porcelain", "-z", "--untracked-files=normal")
                if status.returncode != 0:
                    log.warning("git status failed: %s", self._output(status))
                    return None
                entries = porcelain_entries(status.stdout)
                outside = [path for _xy, path in entries if not in_scratch(path)]
                if any(in_scratch(path) and xy[0] not in " ?!" for xy, path in entries):
                    # a manual `git add -f`: put the index back to HEAD for .scratch/ (never deletes a tracked file)
                    await self._git(actor, "reset", "-q", "--", SCRATCH_DIR)
                if not outside:
                    self._held.clear()  # no untracked file is left: every held one vanished
                    return None
                units, nested = await self._untracked(actor, entries)
                if nested:
                    log.info("autocommit for %s leaves out nested repositories: %s", actor, ", ".join(nested))
                    if skipped is not None:
                        skipped.extend(nested)
                # the exclude pathspecs need pathspec magic (literal=False); all the pathspecs are fixed strings, and
                # a nested repository's path is matched literally (`literal` magic: "a[1]" is no glob)
                if units is None or not hold:
                    # without hold, or when git could not list the untracked files: everything is committed now
                    self._held.clear()
                    add = await self._git(actor, "add", "-A", "--ignore-errors", "--", ".", *SCRATCH_EXCLUDES,
                                          *(f":(exclude,literal){path}" for path in nested), literal=False)
                else:
                    self._forget_held(units)
                    made, moved = await self._hold_new(actor, kind or "update", detail or message, units, entries,
                                                       ended)
                    if not moved and all(xy == "??" for xy, path in entries if not in_scratch(path)) \
                            and not self._operation_in_progress():
                        return made  # only new files (all held now): nothing to stage, no commit to try
                    # tracked files only (`-u`), and the files a tracked one was moved to: every other untracked file
                    # is held now (whoever's command left it: this commit must not sweep up another member's), and
                    # one a command still running creates meanwhile waits for the next autocommit
                    add = await self._git(actor, "add", "-u", "--ignore-errors", "--", ".", *SCRATCH_EXCLUDES,
                                          literal=False)
                    if moved:
                        more = await self._git_paths(actor, ("add", "--ignore-errors"), moved)
                        add = add if more.returncode == 0 else more
                if add.returncode != 0:
                    log.warning("git add reported errors (committing what it could add): %s", self._output(add))
                # no pathspec: the index is committed (a merge an agent left in progress concludes as before)
                if kind:
                    message = await self._index_message(actor, kind, detail)
                commit = await self._git(actor, "commit", "-q", "--no-verify", "-m", message or "update")
            if commit.returncode != 0:
                log.info("git commit skipped: %s", self._output(commit))
                return made
            head = await self._git(actor, "rev-parse", "HEAD")
            return head.stdout.decode("ascii", "replace").strip() or None
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("git autocommit failed: %s", exc)
            return made

    def _operation_in_progress(self) -> bool:
        """A merge, rebase, cherry-pick... stopped halfway in the workspace's own .git folder (GIT_OPERATIONS): the
        next index commit concludes it. True also when there is no such folder here (a linked worktree, a
        repository above an adopted folder): not known, so the commit is tried as always."""
        git_dir = self.root / ".git"
        if not git_dir.is_dir():
            return True
        return any(os.path.lexists(git_dir / name) for name, _what in GIT_OPERATIONS)

    async def _index_message(self, actor: str, kind: str, detail: str) -> str:
        """commit_message() for what the index holds after `git add`: the paths staged against HEAD (a rename as
        its old and its new path). Caller holds the git lock."""
        diff = await self._git(actor, "diff", "--cached", "--name-only", "--no-renames", "--no-ext-diff", "-z")
        if diff.returncode != 0:  # e.g. an unusual repository state: the subject quotes the detail instead
            log.info("git diff --cached failed: %s", self._output(diff))
        names = diff.stdout.decode("utf-8", "replace").split("\0") if diff.returncode == 0 else []
        return commit_message(kind, [name for name in names if name], detail)

    def _git_argv(self, actor: str | None, args: tuple[str, ...], literal: bool = True) -> list[str]:
        if netoff.is_off(getattr(self.swarm, "config", None)):
            # without network: hooks and an fsmonitor command are commands the members may have written
            # (`pre-commit install`, `git config core.fsmonitor ...`), and git runs them with DreamTeam's network
            # (docs/SPEC.md §7.2)
            args = ("-c", f"core.hooksPath={os.devnull}", "-c", "core.fsmonitor=false", *args)
        # an adopted folder: the user's line-ending settings apply to the agents' commits as to the user's own
        return git_argv(self._git_exe or "git", self._real_root(), actor, args, adopted=self.adopted, literal=literal)

    async def _git(self, actor: str | None, *args: str, literal: bool = True,
                   input: bytes | None = None) -> subprocess.CompletedProcess:
        argv = self._git_argv(actor, args, literal)
        kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL} if input is None else {"input": input}
        if _WINDOWS:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        run = functools.partial(subprocess.run, argv, cwd=str(self.root), env=git_env(), capture_output=True,
                                timeout=GIT_TIMEOUT, **kwargs)
        future = asyncio.get_running_loop().run_in_executor(None, run)
        self._running_git.add(future)
        future.add_done_callback(self._git_done)
        return await asyncio.shield(future)  # a cancelled caller leaves it tracked until git really ends

    def _git_done(self, future: asyncio.Future) -> None:
        self._running_git.discard(future)
        if not future.cancelled():
            future.exception()  # retrieved here too: its caller may have been cancelled meanwhile

    async def settle(self, timeout: float = GIT_TIMEOUT) -> None:
        """Wait for git commands still running for callers that were cancelled (a stopped agent's commit): when the
        swarm stops, its workspace may be moved or deleted next."""
        pending = {f for f in self._running_git if not f.done()}
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    @staticmethod
    def _output(proc: subprocess.CompletedProcess) -> str:
        return git_output(proc)
