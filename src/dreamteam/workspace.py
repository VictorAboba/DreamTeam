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
from pathlib import Path, PurePath
from typing import Any

from .models import USER, ToolError

log = logging.getLogger(__name__)

DEFAULT_GITIGNORE = "node_modules/\n.venv/\n__pycache__/\n*.pyc\n.DS_Store\n.scratch/\n"
SCRATCH_DIR = ".scratch"   # the agents' temporary files: never committed
SCRATCH_GITIGNORE = ("# DreamTeam: temporary files of the agents. Git ignores this folder, this file included;\n"
                     "# nothing in it is ever committed.\n*\n")
# inherited variables that would point an agent's git at another repository or index
GIT_LOCATION_VARS = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                               "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_PREFIX"})
BINARY_PROBE_BYTES = 8192
LIST_MAX_ENTRIES = 500
TREE_MAX_ENTRIES = 5000
MAX_LOCK_MINUTES = 24 * 60
GIT_TIMEOUT = 120.0
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
    run_git(root, "add", "-A", "--ignore-errors", git=git, adopted=adopted)
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


class Workspace:
    def __init__(self, swarm: Any):
        self.swarm = swarm
        self.root: Path = Path(swarm.config.workspace_dir)
        self._git_exe: str | None = shutil.which("git")
        self._git_lock = asyncio.Lock()
        self._io_lock = asyncio.Lock()  # read-modify-write of files by concurrent agents
        self._running_git: set[asyncio.Future] = set()  # git commands in threads (they outlive a cancelled caller)

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
    async def write(self, actor: str, path: str, content: str) -> str:
        p = self.resolve(path)
        rel = self.rel(p)
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

    @staticmethod
    def _is_link(entry: os.DirEntry) -> bool:
        """Symlinks and Windows junctions: listed but never followed (they may point outside)."""
        try:
            if entry.is_symlink():
                return True
            st = entry.stat(follow_symlinks=False)
            if not getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                return False
            tag = getattr(st, "st_reparse_tag", None)
            # junctions and other name surrogates; OneDrive/cloud placeholders are regular entries
            return tag is None or bool(tag & _REPARSE_TAG_NAME_SURROGATE)
        except OSError:
            return False

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
    async def autocommit(self, actor: str, message: str) -> str | None:
        if not self.git_enabled:
            return None
        async with self._git_lock:
            return await self._commit_locked(actor, message, None)

    async def _commit_locked(self, actor: str, message: str, rel: str | None) -> str | None:
        """Commit everything except `.scratch/` (rel None) or just `rel` as `actor`. Caller holds the git lock.
        Nothing under `.scratch/` is ever committed."""
        if not self.git_enabled:
            return None
        if rel is not None and in_scratch(rel):
            self.ensure_scratch()  # the file may be the marker itself: put it back at once
            return None  # a temporary file of an agent (write_file / edit_file into .scratch/)
        try:
            if rel:
                spec = ("--", rel)
                status = await self._git(actor, "status", "--porcelain", *spec)
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
                status = await self._git(actor, "status", "--porcelain", "-z")
                if status.returncode != 0:
                    log.warning("git status failed: %s", self._output(status))
                    return None
                entries = porcelain_entries(status.stdout)
                outside = [path for _xy, path in entries if not in_scratch(path)]
                if any(in_scratch(path) and xy[0] not in " ?!" for xy, path in entries):
                    # a manual `git add -f`: put the index back to HEAD for .scratch/ (never deletes a tracked file)
                    await self._git(actor, "reset", "-q", "--", SCRATCH_DIR)
                if not outside:
                    return None
                # the exclude pathspec needs pathspec magic (literal=False); both pathspecs are fixed strings
                add = await self._git(actor, "add", "-A", "--ignore-errors", "--", ".", f":(exclude){SCRATCH_DIR}",
                                      literal=False)
                if add.returncode != 0:
                    log.warning("git add reported errors (committing what it could add): %s", self._output(add))
                # no pathspec: the index is committed (a merge an agent left in progress concludes as before)
                commit = await self._git(actor, "commit", "-q", "--no-verify", "-m", message or "update")
            if commit.returncode != 0:
                log.info("git commit skipped: %s", self._output(commit))
                return None
            head = await self._git(actor, "rev-parse", "HEAD")
            return head.stdout.decode("ascii", "replace").strip() or None
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("git autocommit failed: %s", exc)
            return None

    def _git_argv(self, actor: str | None, args: tuple[str, ...], literal: bool = True) -> list[str]:
        # an adopted folder: the user's line-ending settings apply to the agents' commits as to the user's own
        return git_argv(self._git_exe or "git", self._real_root(), actor, args, adopted=self.adopted, literal=literal)

    async def _git(self, actor: str | None, *args: str, literal: bool = True) -> subprocess.CompletedProcess:
        argv = self._git_argv(actor, args, literal)
        kwargs: dict[str, Any] = {}
        if _WINDOWS:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        run = functools.partial(subprocess.run, argv, cwd=str(self.root), env=git_env(), stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=GIT_TIMEOUT, **kwargs)
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
