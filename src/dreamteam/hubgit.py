"""DreamTeam's own git commands in folders the members can write (docs/SPEC.md §7 "DreamTeam's own git").

A member's `shell` reaches the workspace's `.git`, so what git runs for that repository is the members' to choose:
hooks, an fsmonitor command, filter, textconv and merge drivers, ssh and credential helpers named in `.git/config`
(directly or through an included file), the same in a submodule's own config. The autocommits and the session's
checks of a folder of the user must not run them with DreamTeam's rights and environment. Every such git call gets:

* `environment()`: the hub's environment filtered of secrets as the agents' is, whatever `shell.pass_env` allows
  them, and without inherited GIT_*; every transport refused (GIT_ALLOW_PROTOCOL empty: no fetch, lazy or not,
  so no ssh command, remote or credential helper ever starts), no editor, askpass or LFS download;
* `fixed_args()`: `--no-pager`, hooks from a path that holds none (`hooks_dir`), no fsmonitor, no signing, no
  credential helper;
* `guard()`: for every key that names a command in the repository's own (local, worktree, included) config and in
  its checked-out submodules' (git starts a child git in each), a `-c` that puts back the user's global or system
  value (a git-lfs filter keeps working) or an inert one; every config-based hook disabled; automatic maintenance off
  when `gc.recentObjectsHook` (which `-c` cannot take back) is set there. Reading the config runs nothing; the result
  is cached per repository until the content of one of its config files changes, or where its `.git` leads (its
  index, for the submodules). A filter the repository requires but only its own config defines (git-crypt, git-lfs
  installed with --local) is disarmed too: its files are left out of DreamTeam's commits and checks
  (`Guard.excludes`, `run_unfiltered`) rather than stored unfiltered.

A key added by a member's command that runs at the very moment of DreamTeam's call is not seen in time. With the
network off under the "docker" or "netns" isolation every git DreamTeam runs for the session (the workspace's, from
before its init() on, and sessions.py's checks and commits: workspace.isolated_git) therefore runs like the members'
commands (`run(..., offline=True)`: netoff.wrap): whatever slips through runs with their rights, not root's with the
network. Under "env" and "netns" the members have DreamTeam's uid anyway: what runs in this git gets the filtered
environment, no more of DreamTeam's than the members' commands get."""

from __future__ import annotations

import atexit
import functools
import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import netoff
from .shell import secret_free_env

log = logging.getLogger(__name__)

SCAN_TIMEOUT = 60.0
_WINDOWS = sys.platform == "win32"
GITLINK_MODE = b"\x00\x00\xe0\x00"   # mode 160000 as the index stores it (32 bits, big-endian)
# a filter driver's name that a pathspec's attr: magic can spell
_ATTR_VALUE = re.compile(r"[A-Za-z0-9_.+-]+\Z")
# `git add` takes the attr: magic since 2.44 (RelNotes 2.44.0); ls-files, status and commit since it exists (2.13)
ATTR_IN_ADD = (2, 44)
PATHSPEC_FILE = (2, 25)   # --pathspec-from-file
_TRUE = ("true", "yes", "on", "1")
# set for every call: what git reads from the environment beats any config
ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ALLOW_PROTOCOL": "",       # no transport at all: nothing DreamTeam does here needs one
    "GIT_NO_LAZY_FETCH": "1",       # a partial clone's missing object stays missing (git 2.44+; the above anyway)
    "GIT_EDITOR": ":", "GIT_SEQUENCE_EDITOR": ":",   # ":" is never launched
    "GIT_ASKPASS": "",              # set but empty: core.askPass is not consulted
    "GIT_PAGER": "cat",
    "GIT_LFS_SKIP_SMUDGE": "1",
}

_lock = threading.Lock()
_hooks: list[str] = []
_repos: dict[tuple[str, bool], _Cached] = {}
_links: dict[tuple[str, bool], tuple[Any, list[str]]] = {}
_told: set[tuple[str, str]] = set()   # (folder, filter) whose files DreamTeam does not version: logged once


# ---- the fixed part --------------------------------------------------------------------------------------------
def hooks_dir() -> str:
    """core.hooksPath. On POSIX os.devnull: a device only root can replace, below which no hook can ever be (a
    folder of DreamTeam's would be the members' to write under "env" and "netns", where they have its uid, between
    the check below and git's start). On Windows, which has no such path, an empty folder of DreamTeam's own outside
    every workspace (the members' commands have DreamTeam's account there anyway): made once per process, and anew
    when anything appeared in it (or it vanished, or is not a folder any more)."""
    if not _WINDOWS:
        return os.devnull
    with _lock:
        if _hooks and _still_empty(_hooks[0]):
            return _hooks[0]
        folder = tempfile.mkdtemp(prefix="dreamteam-nohooks-")
        atexit.register(_remove_empty, folder)
        _hooks[:] = [Path(folder).as_posix()]
        return _hooks[0]


def _remove_empty(folder: str) -> None:
    try:
        os.rmdir(folder)   # only an empty folder: whatever appeared in it is left for a look
    except OSError:
        pass


def _still_empty(folder: str) -> bool:
    try:
        if not stat.S_ISDIR(os.lstat(folder).st_mode):
            return False
        with os.scandir(folder) as it:
            return next(it, None) is None
    except OSError:
        return False


def fixed_args() -> list[str]:
    """The global options of every DreamTeam git call, before the subcommand."""
    return ["--no-pager",
            "-c", f"core.hooksPath={hooks_dir()}",
            "-c", "core.fsmonitor=",       # empty: off for every git (2.35 and older take "false" for a hook path)
            "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false",
            "-c", "credential.helper="]   # empty: resets the list of helpers


def environment(secret_names: Iterable[str] = ()) -> dict[str, str]:
    """The environment of DreamTeam's git: os.environ filtered of secrets (`shell.secret_free_env`, `secret_names` too),
    without any inherited GIT_* (they would redirect git to another repository or index, or override the author),
    plus ENV."""
    env = {k: v for k, v in secret_free_env(secret_names).items() if not k.upper().startswith("GIT_")}
    env.update(ENV)
    return env


def run(argv: list[str], *, cwd: Path | str, env: dict[str, str], timeout: float, input: bytes | None = None,
        offline: bool = False) -> subprocess.CompletedProcess:
    """Run a git command (synchronous: call it from a thread). `offline`: like a member's command in a session
    without network (netoff.wrap and its environment: the offline user's ids, or the session's network namespace)."""
    if offline:
        argv, env = netoff.wrap(argv), netoff.environment(env)
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL} if input is None else {"input": input}
    if _WINDOWS:
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(argv, cwd=str(cwd), env=env, capture_output=True, timeout=timeout, **kwargs)


# ---- what a repository's own config names -----------------------------------------------------------------------
def _split(key: str) -> tuple[str, str, str]:
    """(section, subsection, name) of a config key as `git config --list` prints it; section and name in lower case
    (git compares them so), the subsection as it is (git compares it exactly)."""
    section, _, rest = key.partition(".")
    sub, _, name = rest.rpartition(".")
    return section.lower(), sub, name.lower()


def _norm(key: str) -> str:
    section, sub, name = _split(key)
    return f"{section}.{sub}.{name}" if sub else f"{section}.{name}"


def inert_value(key: str) -> str | None:
    """The value that disarms `key` when it names a command that git (or the filter program it runs) would start in
    the commands DreamTeam runs, else None. Transports, editors and pagers are ENV's and fixed_args' business,
    config-based hooks and gc.recentObjectsHook guard()'s."""
    section, sub, name = _split(key)
    if section == "filter" and sub:
        return {"clean": "", "smudge": "", "process": "", "required": "false"}.get(name)
    if section == "diff":
        return "" if (sub and name in ("textconv", "command")) or (not sub and name == "external") else None
    if section == "merge" and sub and name == "driver":
        return ""
    if section == "core" and not sub and name in ("sshcommand", "alternaterefscommand", "askpass"):
        return ""
    if section == "credential" and name == "helper":
        return ""
    if section == "gpg" and (name == "program" or (sub.lower() == "ssh" and name == "defaultkeycommand")):
        return ""
    if section == "remote" and sub and name in ("uploadpack", "receivepack"):
        return ""
    if section == "trailer" and sub and name in ("cmd", "command"):
        return ""
    if section == "lfs" and sub.lower().startswith("extension.") and name in ("clean", "smudge"):
        return ""   # git-lfs (a filter of the user's) would run these itself: it fails instead
    return None


@dataclass
class Scan:
    """What one repository's config asks of DreamTeam's git (`parse`)."""

    guard: dict[str, str] = field(default_factory=dict)  # normalized key -> the value it gets on the command line
    unfiltered: set[str] = field(default_factory=set)     # disarmed filters the repository requires (module doc)
    hooks: set[str] = field(default_factory=set)          # names of config-based hooks (hook.<name>.*), any scope
    maintenance_off: bool = False                         # gc.recentObjectsHook in the repository's own config
    includes: bool = False                                # its own config includes other files
    files: list[str] = field(default_factory=list)        # the config files git read (absolute)
    git_dir: str | None = None                            # where its own config is (a linked worktree's: common)
    index_dir: str | None = None                          # where its index is, when known (_cached_scan)
    ok: bool = True


def parse(data: bytes, folder: Path | str, *, trust_global: bool = True) -> Scan:
    """A Scan of `git config --list --show-scope --show-origin -z --includes` output: records of scope, origin and
    "key\\nvalue" (a bare "key": true), each ended by NUL. The system and global scopes are the user's: their values
    are what a disarmed key gets back. Every other scope (local, worktree, unknown; an included file counts as its
    includer's) is the repository's; "command" is DreamTeam's own -c. `trust_global` False: the global config is
    the members' too (the offline user's home in the Docker sandbox) and disarmed like the repository's."""
    out = Scan()
    trusted: dict[str, str] = {}
    own: dict[str, str] = {}
    fields = data.split(b"\0")
    for i in range(0, len(fields) - 2, 3):
        scope = fields[i].decode("utf-8", "replace")
        origin = fields[i + 1].decode("utf-8", "replace")
        key, sep, value = fields[i + 2].decode("utf-8", "replace").partition("\n")
        value = value if sep else "true"
        section, sub, name = _split(key)
        if origin.startswith("file:"):
            path = os.path.normpath(os.path.join(str(folder), origin[5:]))
            if path not in out.files:
                out.files.append(path)
            if scope == "local" and out.git_dir is None and os.path.basename(path) == "config":
                out.git_dir = os.path.dirname(path)
        if section == "hook" and sub:
            out.hooks.add(sub)
        if scope == "command":
            continue
        norm = _norm(key)
        if scope == "system" or (scope == "global" and trust_global):
            trusted[norm] = value
        elif section in ("include", "includeif") and name == "path":
            out.includes = True
        elif section == "gc" and not sub and name == "recentobjectshook":
            out.maintenance_off = True
        elif inert_value(key) is not None:
            own[norm] = value
    for norm in own:
        # a credential helper never comes back (nothing here authenticates); anything else: the user's own value
        out.guard[norm] = "" if norm.startswith("credential.") else trusted.get(norm, inert_value(norm))
    for norm in own:
        section, driver, name = _split(norm)
        if section != "filter" or name not in ("clean", "process"):
            continue
        if trusted.get(f"filter.{driver}.clean") or trusted.get(f"filter.{driver}.process"):
            continue   # the user's own command applies
        required = own.get(f"filter.{driver}.required", trusted.get(f"filter.{driver}.required", "false"))
        if required.strip().lower() in _TRUE:
            if _ATTR_VALUE.match(driver):
                out.unfiltered.add(driver)
            else:   # no pathspec can name its files: git refuses them instead
                out.guard[f"filter.{driver}.required"] = "true"
    return out


def scan(git: str, folder: Path, env: dict[str, str], *, offline: bool = False) -> Scan:
    """The config of the repository that contains `folder`, as git itself reads it there (`git config --list`, which
    runs nothing). A git without --show-scope (older than 2.26) lists the local config alone (no value of the user's
    comes back then). ok=False: git could not read it (the command DreamTeam runs next fails the same way).
    `offline` in the Docker sandbox: git reads the offline user's home there, whose global config the members can
    write, so none of its values comes back (parse's trust_global)."""
    trust_global = not (offline and netoff.isolation() == "docker")
    base = [git, *fixed_args(), "-c", "safe.directory=*", "config"]
    proc = run([*base, "--list", "--show-scope", "--show-origin", "-z", "--includes"], cwd=folder, env=env,
               timeout=SCAN_TIMEOUT, offline=offline)
    if proc.returncode == 129:   # an option this git does not know
        proc = run([*base, "--local", "--list", "-z", "--includes"], cwd=folder, env=env, timeout=SCAN_TIMEOUT,
                   offline=offline)
        if proc.returncode == 0:
            data = b"".join(b"local\0file:.git/config\0" + entry + b"\0" for entry in proc.stdout.split(b"\0") if entry)
            result = parse(data, folder, trust_global=trust_global)
            result.includes = True   # which files it read is not known: never reused
            return result
    if proc.returncode != 0:
        log.info("git config --list failed in %s: %s", folder, (proc.stderr or b"").decode("utf-8", "replace")[:300])
        return Scan(ok=False)
    return parse(proc.stdout, folder, trust_global=trust_global)


CONFIG_LIMIT = 1 << 20    # a larger config file is not compared (_cached_scan: never reused then)
INDEX_LIMIT = 64 << 20    # a larger index is not searched for gitlinks (_gitlinks: `git ls-files` then)
NOT_A_FILE = b"\0not a regular file"   # _read of a folder, a FIFO, a device, a file over the limit or still growing
# (O_NONBLOCK: opening a FIFO waits for a writer; O_NOCTTY: a terminal never becomes the hub's; O_BINARY: on Windows a
# text-mode read would stop at a Ctrl-Z, and a change after it would go unseen)
_OPEN = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOCTTY", 0) | getattr(os, "O_BINARY", 0)


def _read(path: str, limit: int = CONFIG_LIMIT) -> bytes | None:
    """The bytes of a file the members may have made: None when there is none (or it cannot be opened), NOT_A_FILE
    when it is no regular file (a folder, which POSIX opens; a FIFO would block the read, a device never end it),
    larger than `limit`, or longer when read than fstat said (it grew meanwhile: its first bytes are no content to
    compare, they could match a stamp of the file before the change). Reads the size fstat gave and one byte more
    (`limit` + 1 would cost a buffer that large at every call: _may_link reads the index, up to INDEX_LIMIT, before
    nearly every git call), and closes the descriptor on every path (a folder a member makes at a watched path, such
    as `.git/config.worktree`, which git ignores, would leak one at every guard() until the hub could open nothing)."""
    try:
        fd = os.open(path, _OPEN)
    except PermissionError:   # (on Windows also for a folder, which os.open refuses there)
        return NOT_A_FILE if os.path.isdir(path) else None
    except (OSError, ValueError):   # (ValueError: a NUL in the path)
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return NOT_A_FILE
        with open(fd, "rb", closefd=False) as f:
            data = f.read(info.st_size + 1)
        return data if len(data) <= info.st_size else NOT_A_FILE
    except OSError:
        return None
    finally:
        os.close(fd)


@dataclass
class _Cached:
    stamp: tuple | None        # the content of the config files when scanned (None: scanned anew every time)
    result: Scan


def _global_files(env: dict[str, str], offline: bool) -> list[str]:
    """The global config files git looks for with `env` (run `offline`: with netoff's environment, the offline user's
    home in the Docker sandbox), whether they exist or not."""
    seen = netoff.environment(env) if offline else env
    home = seen.get("HOME") or (seen.get("USERPROFILE") if _WINDOWS else None)
    xdg = seen.get("XDG_CONFIG_HOME") or (os.path.join(home, ".config") if home else None)
    files = ([os.path.join(home, ".gitconfig")] if home else []) + ([os.path.join(xdg, "git", "config")] if xdg else [])
    return [os.path.normpath(f) for f in files]


def _is_link(info: os.stat_result) -> bool:
    # a symlink, or on Windows any reparse point (a junction too)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _locate(folder: Path) -> tuple[list[str], str, str | None] | None:
    """Where git finds the repository whose `.git` is in `folder`, as git resolves it: (the files a member may change
    or create that decide which config git reads: the `.git` file itself, `<git dir>/config`, `config.worktree` and
    `commondir`, `<common dir>/config`; the git dir, where the index is; the common dir its `commondir` names, as a
    linked worktree has, or None). The git dir is the `.git` folder or what a `.git` file's "gitdir: <path>" names
    (relative to `folder`: a checked-out submodule, a linked worktree). Real paths, as git takes them: a link a member
    swaps further up changes them. None (no cache then): no `.git` here, a `.git` that is a link, or one git would
    not take, or that names no path (_named)."""
    dot_git = os.path.join(folder, ".git")
    try:
        info = os.lstat(dot_git)
    except (OSError, ValueError):
        return None
    watch: list[str] = []
    if _is_link(info):
        return None
    if stat.S_ISDIR(info.st_mode):
        git_dir = _named(str(folder), b".git")
    elif stat.S_ISREG(info.st_mode):
        data = _read(dot_git) or b""
        git_dir = _named(str(folder), data[8:].rstrip(b"\r\n")) if data.startswith(b"gitdir: ") else None
        watch.append(dot_git)
    else:
        return None
    if git_dir is None:
        return None
    common = None
    data = _read(os.path.join(git_dir, "commondir"))
    if data is not None:
        common = _named(git_dir, data.rstrip(b"\r\n"))
        if common is None:
            return None
    watch += [os.path.join(git_dir, name) for name in ("config", "config.worktree", "commondir")]
    if common is not None:
        watch.append(os.path.join(common, "config"))
    return watch, git_dir, common


def _named(base: str, target: bytes) -> str | None:
    """The real path that a `.git` file's "gitdir: <path>" or a `commondir` names (relative to `base`); None when
    the bytes are no path here: empty, with a NUL (git takes the path up to it, Python's calls refuse it; NOT_A_FILE
    has one), on Windows no UTF-8. A member writes them: the folder is then scanned every time, the guard never
    fails on them."""
    if not target or b"\0" in target:
        return None
    try:
        return os.path.realpath(os.path.join(base, os.fsdecode(target)))
    except (OSError, ValueError):   # (a UnicodeDecodeError is a ValueError)
        return None


def _same_dir(path: str | None, real: str) -> bool:
    try:
        return path is not None and os.path.normcase(os.path.realpath(path)) == os.path.normcase(real)
    except (OSError, ValueError):
        return False


def _cached_scan(git: str, folder: Path, env: dict[str, str], offline: bool) -> Scan:
    """scan(), reused while the content of every config file it read is the same, and of those a member may create
    or change to make git read another config (_locate: the `.git` file of a submodule or a linked worktree, the git
    dir's config, config.worktree and commondir, the common dir's config; their real paths, so a `.git` file pointing
    elsewhere or a link swapped further up is a change) and the global ones (the members can write them under "netns"
    and in the Docker sandbox): a few small reads. Never reused when the config includes other files (one that does
    not exist yet would not be watched), when there is no `.git` of its own in `folder` or it is a link, when one of
    those files is no regular file, too large or growing (_read), nor when git read the config of another repository
    than the one _locate found. Scan.index_dir: where git keeps the index then."""
    key = (os.path.normcase(str(folder)), offline)
    where = _locate(folder)
    watch = [*(where[0] if where is not None else ()), *_global_files(env, offline)]
    with _lock:
        entry = _repos.get(key)
    if entry is not None and entry.stamp is not None:
        files = list(dict.fromkeys([*watch, *entry.result.files]))
        if entry.stamp == tuple((f, _read(f)) for f in files):
            return entry.result
    before = {f: _read(f) for f in watch}   # read first: a change made while git reads shows at the next call
    result = scan(git, folder, env, offline=offline)
    stamp = None
    # git's own config (a linked worktree's: the common dir's) must be the one of the repository found here
    if result.ok and where is not None and _same_dir(result.git_dir, where[2] or where[1]):
        result.index_dir = where[1]
        if not result.includes:
            files = list(dict.fromkeys([*watch, *result.files]))
            stamp = tuple((f, before[f] if f in before else _read(f)) for f in files)
            if any(data is NOT_A_FILE for _f, data in stamp):
                stamp = None   # a content that cannot be compared: scanned every time while it is there
    with _lock:
        _repos[key] = _Cached(stamp, result)
    return result


def _index_stamp(paths: list[str]) -> tuple:
    out = []
    for path in paths:
        try:
            st = os.stat(path)
            # the change time too on POSIX: a writer can put the mtime back after rewriting the index in place, never
            # the ctime (on Windows st_ctime is the creation time, and the members have DreamTeam's account there)
            out.append((path, st.st_mtime_ns, st.st_size, st.st_ino, None if _WINDOWS else st.st_ctime_ns))
        except OSError:
            out.append((path, None))
    return tuple(out)


def _may_link(index: str) -> bool:
    data = _read(index, INDEX_LIMIT)   # None: no index, no gitlink
    return data is NOT_A_FILE or (data is not None and GITLINK_MODE in data)


def _gitlinks(git: str, folder: Path, env: dict[str, str], index_dir: str | None, offline: bool) -> list[str]:
    """The submodules (gitlinks) in the index of the repository at `folder`, relative to it. With the index's folder
    known (Scan.index_dir) its files' bytes first (no gitlink there: none at all, the usual case), cached by their
    stat; else, or when one is there or a file cannot be searched (_read: no regular file, over INDEX_LIMIT),
    `git ls-files -s` (it fails outside a repository: none)."""
    index_files: list[str] | None = None
    if index_dir is not None:
        try:
            shared = [os.path.join(index_dir, name) for name in os.listdir(index_dir)
                      if name.startswith("sharedindex.")]
        except OSError:
            shared = []
        index_files = [os.path.join(index_dir, "index"), *shared]
    key = (os.path.normcase(str(folder)), offline)
    stamp = _index_stamp(index_files) if index_files is not None else None
    with _lock:
        cached = _links.get(key)
    if stamp is not None and cached is not None and cached[0] == stamp:
        return cached[1]
    links: list[str] = []
    if index_files is None or any(_may_link(f) for f in index_files):
        proc = run([git, *fixed_args(), "-c", "safe.directory=*", "ls-files", "-s", "-z"], cwd=folder, env=env,
                   timeout=SCAN_TIMEOUT, offline=offline)
        if proc.returncode == 0:
            for entry in proc.stdout.split(b"\0"):
                if entry.startswith(b"160000 ") and b"\t" in entry:
                    links.append(entry.split(b"\t", 1)[1].decode("utf-8", "surrogateescape"))
    with _lock:
        _links[key] = (stamp, links)
    return links


@dataclass
class Guard:
    """guard(): the settings as `env` (GIT_CONFIG_COUNT and its pairs: a name may hold "=", which `-c` would split
    at) or, for a git older than 2.31, as `args` before the subcommand; `excludes`: pathspecs (with magic) for
    whatever DreamTeam stages, commits or checks (run_unfiltered), of the disarmed required filters `filters`."""

    env: dict[str, str] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    filters: list[str] = field(default_factory=list)


@functools.lru_cache(maxsize=8)
def git_version(git: str) -> tuple[int, int]:
    try:
        out = run([git, "--version"], cwd=tempfile.gettempdir(), env=environment(), timeout=SCAN_TIMEOUT).stdout
    except (OSError, subprocess.SubprocessError):
        return (0, 0)
    match = re.search(rb"(\d+)\.(\d+)", out or b"")
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def guard(git: str, root: Path | str, env: dict[str, str], *, offline: bool = False) -> Guard:
    """What disarms the config of the repository at `root` and of its checked-out submodules, recursively (module
    doc). git hands its -c on to the child git it starts in a submodule, so a submodule's keys go on the same command
    line. Nothing for a repository git cannot read."""
    keys: dict[str, str] = {}
    unfiltered: set[str] = set()
    hooks: set[str] = set()
    maintenance_off = False
    todo, seen = [Path(root)], set()
    while todo:
        folder = todo.pop()
        # a submodule's folder may be a link a member made (back to the top, say): followed only once
        name = os.path.normcase(os.path.realpath(folder) if seen else os.path.abspath(folder))
        if name in seen:
            continue
        seen.add(name)
        result = _cached_scan(git, folder, env, offline)
        if not result.ok:
            continue
        for key, value in result.guard.items():
            keys.setdefault(key, value)   # the top repository's first
        unfiltered |= result.unfiltered
        hooks |= result.hooks
        maintenance_off = maintenance_off or result.maintenance_off
        for path in _gitlinks(git, folder, env, result.index_dir, offline):
            if os.path.lexists(folder / path / ".git"):   # checked out: git starts a child git there
                todo.append(folder / path)
    pairs = list(keys.items())
    for name in sorted(hooks):
        pairs += [(f"hook.{name}.enabled", "false"), (f"hook.{name}.command", "")]
    if maintenance_off:
        pairs += [("maintenance.auto", "false"), ("gc.auto", "0")]
    filters = sorted(unfiltered)
    out = Guard(excludes=[f":(exclude,attr:filter={name})" for name in filters], filters=filters)
    _tell(root, filters)
    if not pairs:
        return out
    if git_version(git) >= (2, 31):
        out.env["GIT_CONFIG_COUNT"] = str(len(pairs))
        for i, (key, value) in enumerate(pairs):
            out.env[f"GIT_CONFIG_KEY_{i}"], out.env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    else:
        for key, value in pairs:
            if "=" in key:
                log.warning("git %s.%s cannot override %s on its command line", *git_version(git), key)
                continue
            out.args += ["-c", f"{key}={value}"]
    return out


def _tell(root: Path | str, filters: list[str]) -> None:
    """The user learns once per folder (and process) which files DreamTeam does not version there."""
    folder = os.path.normcase(os.path.abspath(root))
    with _lock:
        new = [name for name in filters if (folder, name) not in _told]
        _told.update((folder, name) for name in new)
    if new:
        log.warning("DreamTeam leaves the files of the git filter %s out of its commits and checks in %s: the "
                    "repository requires that filter and only its own config defines it (git-crypt, git-lfs "
                    "installed with --local), and DreamTeam's git does not run it. Commit those files with your own "
                    "git.", ", ".join(new), root)


def _encode(spec: str) -> bytes:
    return spec.encode("utf-8", "surrogateescape")   # a path git listed comes back as git gave it


def with_excludes(args: tuple[str, ...] | list[str], excludes: list[str], literal: bool,
                  input: bytes | None, *, stdin: bool = False) -> tuple[list[str], bool, bytes | None]:
    """`args` of a command that stages, commits or checks pathspecs (after "--", or NUL-separated in `input` with
    --pathspec-from-file=-), with `excludes` added: (args, literal, input). Literal pathspecs become `:(literal)...`
    (pathspec magic must be on for the excludes). `stdin`: pathspecs given after "--" move to `input` with the
    excludes (--pathspec-from-file=-, git 2.25+: the excludes may be too many for a command line). A command without
    pathspecs is returned as it is."""
    args = list(args)
    if not excludes:
        return args, literal, input
    if "--pathspec-from-file=-" in args and input is not None:
        specs = [s for s in input.split(b"\0") if s]
        if literal:
            specs = [b":(literal)" + s for s in specs]
        return args, False, b"".join(s + b"\0" for s in [*specs, *(_encode(e) for e in excludes)])
    if "--" not in args:
        return args, literal, input
    cut = args.index("--") + 1
    specs = [f":(literal){s}" for s in args[cut:]] if literal else args[cut:]
    if stdin and input is None:
        data = b"".join(_encode(s) + b"\0" for s in [*specs, *excludes])
        return [*args[:cut - 1], "--pathspec-from-file=-", "--pathspec-file-nul"], False, data
    return [*args[:cut], *specs, *excludes], False, input


def _protected(git: str, root: Path | str, env: dict[str, str], guard: Guard, *, others: bool,
               offline: bool) -> dict[str, bytes | None] | None:
    """The files of `guard.filters` below `root` (`git ls-files -s`, which reads no content): path -> its index
    entries ("<mode> <object> <stage>"), None for an untracked one (`others`); None when git cannot list them (a git
    older than 2.13 has no attr magic at all)."""
    proc = run([git, *fixed_args(), "-c", "safe.directory=*", *guard.args, "ls-files", "-z", "-s", "--cached",
                *(("--others", "--exclude-standard") if others else ()), "--",
                *(f":(attr:filter={name})" for name in guard.filters)],
               cwd=root, env=env, timeout=SCAN_TIMEOUT, offline=offline)
    if proc.returncode != 0:
        log.info("git ls-files cannot list the files of the filters %s in %s: %s", ", ".join(guard.filters), root,
                 (proc.stderr or b"").decode("utf-8", "replace")[:300])
        return None
    found: dict[str, bytes | None] = {}
    for item in proc.stdout.split(b"\0"):
        entry, tab, name = item.partition(b"\t")
        if not item:
            continue
        path = (name if tab else entry).decode("utf-8", "surrogateescape")
        found[path] = (found.get(path) or b"") + entry + b"\n" if tab else None   # (unmerged: several entries)
    return found


def run_unfiltered(git: str, root: Path | str, env: dict[str, str], guard: Guard, args: tuple[str, ...] | list[str],
                   literal: bool, input: bytes | None, argv: Callable[[list[str], bool], list[str]], *,
                   timeout: float, offline: bool = False) -> subprocess.CompletedProcess:
    """Run `args`, a command that stages, commits or checks pathspecs (git add, commit, status), with the files of the
    disarmed required filters left out: `guard.excludes` (with_excludes). `env`: environment() (the guard's own
    settings are added here); `argv(args, literal)` builds the whole command line. A `git add` older than 2.44
    rejects the attr magic (ATTR_IN_ADD): it gets the files those excludes match now by name instead
    (`:(exclude,literal)<path>`, read through stdin from git 2.25 on: they may be many), and since a file made or
    given that filter between the listing and the add would be staged unfiltered, every file of those filters in the
    index after the add that the listing did not name is put back as HEAD has it (`git reset`). Should git be unable
    to list them, the attr excludes stay: that add fails as a whole, and nothing is stored unfiltered. OSError when
    what the add staged cannot be checked or put back (no commit may follow)."""
    full = {**env, **guard.env}
    args = list(args)
    listed = None
    if guard.excludes and args[:1] == ["add"] and git_version(git) < ATTR_IN_ADD:
        listed = _protected(git, root, full, guard, others=True, offline=offline)
    if listed is None:
        args, literal, input = with_excludes(args, guard.excludes, literal, input)
        return run(argv(args, literal), cwd=root, env=full, timeout=timeout, input=input, offline=offline)
    excludes = [f":(exclude,literal){path}" for path in listed]
    args, literal, input = with_excludes(args, excludes, literal, input, stdin=git_version(git) >= PATHSPEC_FILE)
    proc = run(argv(args, literal), cwd=root, env=full, timeout=timeout, input=input, offline=offline)
    after = _protected(git, root, full, guard, others=False, offline=offline)
    if after is None:
        raise OSError(f"git ls-files in {root} cannot tell what git add staged")
    staged = [path for path in after if path not in listed]   # a file it named was left out by that name
    if staged:
        log.warning("git add staged files of the filters %s in %s that appeared meanwhile, unfiltered: put back (%s)",
                    ", ".join(guard.filters), root, ", ".join(staged[:5]) + (" ..." if len(staged) > 5 else ""))
        back, data = ["reset", "-q", "--", *staged], None
        if git_version(git) >= PATHSPEC_FILE:
            back, data = back[:2] + ["--pathspec-from-file=-", "--pathspec-file-nul"], b"".join(
                _encode(path) + b"\0" for path in staged)
        reset = run(argv(back, True), cwd=root, env=full, timeout=timeout, input=data, offline=offline)
        if reset.returncode != 0:
            raise OSError(f"git reset in {root} failed: "
                          f"{(reset.stderr or b'').decode('utf-8', 'replace')[:300]}")
    return proc


def forget() -> None:
    """Drop the cached scans and what was logged (tests)."""
    with _lock:
        _repos.clear()
        _links.clear()
        _told.clear()
