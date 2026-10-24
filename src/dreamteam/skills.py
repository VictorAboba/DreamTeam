"""Skills: folders with a SKILL.md (Anthropic Agent Skills), their sources and the shared library (docs/SPEC.md §19).

A skill is a folder whose root holds `SKILL.md` (YAML front matter with `name` and `description`) plus any other
files. This module validates such folders (`check_skill_dir`), copies them safely (`copy_skill`), keeps the shared
library (`Library`, `~/.dreamteam/skills`), fetches skills from a folder, a zip, a Git URL or Claude Code's own
skill folders (`stage_source` / `import_source`) and resolves the text references every surface uses (`parse_ref`,
`resolve_ref`). Nothing from a skill is ever run, imported or rendered: it is data until an agent reads it.

Everything here is synchronous file work; async callers run it in `asyncio.to_thread`.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import functools
import hashlib
import io
import json
import logging
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlsplit

from .workspace import _TRANSLIT, _WIN_BAD_CHARS, _WIN_RESERVED, NO_EXPAND, git_env, is_link, long_path, rmtree

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger(__name__)

# Anthropic's rule for a skill name; "anthropic" and "claude" are not refused (that rule is the API upload's only)
NAME_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
NAME_MAX = 64
DESCRIPTION_MAX = 1024
SKILL_MD = "SKILL.md"
SKILL_MD_MAX = 100_000           # bytes
META_FILE = ".dreamteam.json"   # DreamTeam's metadata inside a stored skill (the folder stays a valid skill)
FRONTMATTER_LINES = 200          # the front matter must close within this many lines
MAX_DEPTH = 8                    # folder levels inside one skill
FIND_DEPTH = 4                   # how deep find_skills looks for SKILL.md files in a source
ZIP_MAX_ENTRIES = 5000
ZIP_MAX_RATIO = 200              # uncompressed / compressed size of one member; more looks like a zip bomb
GIT_TIMEOUT = 120.0
GIT_PARTIAL = (2, 35)            # `sparse-checkout set --no-cone` (older git: a plain shallow clone)
STALE_SECONDS = 3600             # leftover .t-* / .o-* folders older than this are removed
RENAME_TRIES = 5                 # a rename refused by a scanner or an indexer holding a file is tried again
RENAME_PAUSE = 0.2
SHOW_FILES_MAX = 500
COPY_CHUNK = 1 << 16
# library entries record where they came from
ORIGINS = ("folder", "zip", "git", "agent", "claude-user", "claude-project", "host-library")
# the prefixes of a skill reference (`claude-user:pdf`); `host-library` exists only in the Docker sandbox
REF_SOURCES = ("library", "claude-user", "claude-project", "host-library")
# skipped silently in a skill folder: version control, caches, OS litter, and our own metadata (never taken from
# a source)
SKIP_NAMES = frozenset(n.casefold() for n in NO_EXPAND | {".DS_Store", "__MACOSX", "Thumbs.db", META_FILE})
_TAG_LIKE = re.compile(r"<\s*[A-Za-z/!?]")
_REF_RE = re.compile(r"(?:(library|claude-user|claude-project|host-library):)?([a-z0-9]+(?:-[a-z0-9]+)*)")
_WINDOWS = sys.platform == "win32"

NAME_RULE = ("lowercase letters a-z, digits and single hyphens between them (e.g. 'csv-cleanup'), "
             f"at most {NAME_MAX} characters")


class SkillError(ValueError):
    """A skill operation that cannot be done; `status` is the matching HTTP status, `extra` goes into the JSON
    answer (`exists`: the names that clash)."""

    def __init__(self, message: str, status: int = 400, **extra: Any):
        super().__init__(message)
        self.status = status
        self.extra = extra

    def to_json(self) -> dict[str, Any]:
        return {"error": str(self), **self.extra}


@dataclass(frozen=True)
class Limits:
    max_mb: int = 10           # one skill, all its files
    max_files: int = 200       # one skill
    max_archive_mb: int = 50   # one zip or one Git checkout

    @classmethod
    def from_config(cls, config: "Config") -> "Limits":
        s = config.skills
        return cls(max_mb=s.max_mb, max_files=s.max_files, max_archive_mb=s.max_archive_mb)

    @property
    def max_bytes(self) -> int:
        return self.max_mb * 2 ** 20

    @property
    def archive_bytes(self) -> int:
        return self.max_archive_mb * 2 ** 20

    @property
    def zip_b64_max(self) -> int:
        """The longest base64 text of an uploaded zip (checked before it is decoded)."""
        return math.ceil(self.archive_bytes * 4 / 3) + 1024


@dataclass
class SkillInfo:
    """A checked skill folder."""

    name: str
    description: str
    path: Path
    files: int
    size: int
    digest: str                 # "" when the files were not hashed (a stat-only check)
    manual: bool = False        # disable-model-invocation: only when the user asks for it
    manifest: list[list] = field(default_factory=list)   # [[posix rel, size, mtime_ns]]: "changed since"
    renamed_from: str | None = None                      # the name as written, when it was slugged
    skipped: list[dict] = field(default_factory=list)    # [{path, error}]: links and special files left out
    file_list: list[tuple[str, int]] = field(default_factory=list, repr=False)
    _sources: list[tuple[str, str]] = field(default_factory=list, repr=False)   # (rel, absolute path to read)

    def public(self) -> dict[str, Any]:
        out = {"name": self.name, "description": self.description, "files": self.files, "size": self.size,
               "digest": self.digest, "manual": self.manual}
        if self.renamed_from:
            out["renamed_from"] = self.renamed_from
        return out


# ---- names ---------------------------------------------------------------------------------------------------
def check_name(name: Any) -> str:
    """`name` if it is a valid skill name, else SkillError 400. Every function that takes a name calls it before
    any path is built: this is what keeps '..', '.', 'a/b', '..\\x' and decoded '%5C' out of every file operation."""
    if not isinstance(name, str) or len(name) > NAME_MAX or not NAME_RE.fullmatch(name):
        shown = repr(name)[:80] if isinstance(name, str) else f"a {type(name).__name__}"
        raise SkillError(f"Invalid skill name {shown}: a skill name is {NAME_RULE}.")
    return name


def child(root: Path, name: str) -> Path:
    """`root / name` for a checked name, asserted to be a direct child of `root` (a link there that resolves
    elsewhere is refused too)."""
    path = Path(root) / check_name(name)
    try:
        if path.resolve().parent != Path(root).resolve():
            raise SkillError(f"Invalid skill name '{name}': its folder resolves outside {root}.")
    except (OSError, RuntimeError) as exc:
        raise SkillError(f"Cannot use the skill folder {path}: {exc}") from None
    return path


def slug(text: str) -> str:
    """A skill name made from any text ("My Skill" -> "my-skill", "Отчёт" -> "otchet"); "" when nothing is left."""
    text = "".join(_TRANSLIT.get(ch, ch) for ch in (text or "").lower())
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:NAME_MAX].strip("-")


def for_agents(text: str) -> str:
    """A skill's text (name, description) as it may appear in a text built for agents: no '<' / '>' (a description
    can never open or close a tag of the prompt)."""
    return (text or "").replace("<", "\u2039").replace(">", "\u203a")


def looks_like_ref(text: Any) -> bool:
    """`text` is a skill reference (`x`, `library:x`, `claude-user:x`, ...) rather than a source (a path, a URL)."""
    return isinstance(text, str) and bool(_REF_RE.fullmatch(text.strip())) and len(text.strip()) <= NAME_MAX + 20


def parse_ref(text: Any) -> tuple[str | None, str]:
    """(source or None, name) of a skill reference. A Git scp URL (`git@host:x`, it has an '@') and a Windows path
    (`C:\\x`, a one-letter drive) are not references: SkillError 400."""
    if not isinstance(text, str):
        raise SkillError("A skill reference must be a string.")
    value = text.strip()
    match = _REF_RE.fullmatch(value)
    if match:
        return match.group(1), check_name(match.group(2))
    prefix, colon, rest = value.partition(":")
    if colon and prefix in REF_SOURCES:
        check_name(rest)
    if colon and re.fullmatch(r"[a-z][a-z0-9-]+", prefix) and not re.search(r"[@/\\]", value):
        raise SkillError(f"Unknown skill source '{prefix}:'. Use library:NAME, claude-user:NAME or "
                         "claude-project:NAME (or just NAME).")
    check_name(value)
    raise SkillError(f"'{value[:80]}' is not a skill reference.")  # (check_name raised already)


# ---- front matter ----------------------------------------------------------------------------------------------
_KEY_LINE = re.compile(r"([A-Za-z0-9_-]+)[ \t]*:(?:[ \t]+(.*?))?[ \t]*\Z")
_BLOCK = re.compile(r"([|>])([-+]?)[1-9]?([-+]?)[ \t]*(?:#.*)?\Z")
_NESTED = re.compile(r"(?:-(?:[ \t]|\Z)|[A-Za-z0-9_\"'-][^:]*:(?:[ \t]|\Z))")
NO_FRONTMATTER = "SKILL.md must start with YAML front matter between '---' lines (name, description)"


def parse_frontmatter(text: str) -> dict[str, str]:
    """The top-level `key: value` pairs of SKILL.md's YAML front matter (a stdlib subset of YAML: plain, 'single'
    and "double" quoted values, block scalars | and >; nested maps and lists are skipped)."""
    lines = text.lstrip("\ufeff").splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines) or lines[i].rstrip() != "---":
        raise SkillError(NO_FRONTMATTER)
    start = i + 1
    end = next((j for j in range(start, min(len(lines), start + FRONTMATTER_LINES))
                if lines[j].rstrip() in ("---", "...")), None)
    if end is None:
        raise SkillError(f"SKILL.md's front matter has no closing '---' line within its first {FRONTMATTER_LINES} "
                         "lines")
    body = lines[start:end]
    out: dict[str, str] = {}
    k = 0
    while k < len(body):
        line = body[k]
        k += 1
        if not line.strip() or line[:1] in (" ", "\t") or line.lstrip().startswith("#"):
            continue
        match = _KEY_LINE.match(line)
        if not match:
            continue
        block: list[str] = []   # the lines indented below the key
        while k < len(body) and (not body[k].strip() or body[k][:1] in (" ", "\t")):
            block.append(body[k])
            k += 1
        while block and not block[-1].strip():
            block.pop()
        out[match.group(1)] = _scalar(match.group(1), match.group(2) or "", block)
    return out


def _scalar(key: str, value: str, block: list[str]) -> str:
    if _BLOCK.fullmatch(value):
        indent = min((len(b) - len(b.lstrip(" \t")) for b in block if b.strip()), default=0)
        rows = [b[indent:].rstrip() if b.strip() else "" for b in block]
        if value.startswith("|"):
            return "\n".join(rows)
        paragraphs, current = [], []
        for row in rows:  # ">": lines fold into spaces, an empty line is a line break
            if row:
                current.append(row)
            else:
                paragraphs.append(" ".join(current))
                current = []
        paragraphs.append(" ".join(current))
        return "\n".join(paragraphs).strip("\n")
    if value[:1] in ("'", '"'):
        text = " ".join([value] + [b.strip() for b in block])  # a quoted value may go on below
        return _quoted(key, text)
    value = re.sub(r"(?:^|[ \t]+)#.*\Z", "", value)
    if block:
        first = next(b for b in block if b.strip()).strip()
        if not value and _NESTED.match(first):  # a nested map or list: skipped
            return ""
        return " ".join([value] + [b.strip() for b in block if b.strip()]).strip()
    return value


def _quoted(key: str, text: str) -> str:
    quote, out, i = text[0], [], 1
    escapes = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "/": "/", "r": "\r", "0": "\0", " ": " "}
    while i < len(text):
        ch = text[i]
        if quote == "'" and ch == "'":
            if text[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out)
        if quote == '"' and ch == "\\" and i + 1 < len(text):
            out.append(escapes.get(text[i + 1], "\\" + text[i + 1]))
            i += 2
            continue
        if quote == '"' and ch == '"':
            return "".join(out)
        out.append(ch)
        i += 1
    raise SkillError(f"SKILL.md's front matter: the value of '{key}' has no closing {quote} quote")


# ---- validating a folder ---------------------------------------------------------------------------------------
def _portable(part: str, rel: str) -> None:
    """A path part every OS can store (the Windows rules on every OS, so that a skill copies to Windows too)."""
    if _WIN_BAD_CHARS.search(part) or "\\" in part or "\x7f" in part:
        raise SkillError(f"'{rel}': file names in a skill cannot contain < > : \" | ? * \\ or control characters "
                         "(Windows cannot store them).")
    if part != part.rstrip(". "):
        raise SkillError(f"'{rel}': '{part}' ends with a dot or a space, which Windows drops from file names.")
    if _WIN_RESERVED.fullmatch(part):
        raise SkillError(f"'{rel}': '{part}' is a reserved device name on Windows (CON, PRN, AUX, NUL, COM1-9, "
                         "LPT1-9).")


def _fold(rel: str) -> str:
    return unicodedata.normalize("NFC", rel).casefold()


@dataclass
class _Walk:
    files: list[tuple[str, int, int, str, int]] = field(default_factory=list)   # (rel, size, mtime_ns, abs, mode)
    skipped: list[dict] = field(default_factory=list)
    size: int = 0


def _walk(root: Path, limits: Limits, skill_md: str | None = None) -> _Walk:
    """Every regular file below `root` (links and special files skipped and reported, the skip-list left out),
    with the portable-name, collision and size rules applied. `skill_md`: the root's SKILL.md as it is spelled
    there (counted as "SKILL.md")."""
    out = _Walk()
    seen: dict[str, str] = {}

    def visit(folder: str, parts: tuple[str, ...]) -> None:
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            raise SkillError(f"Cannot read {'/'.join(parts) or 'the skill folder'}: {exc.strerror or exc}") from None
        for entry in entries:
            name = entry.name
            if name.casefold() in SKIP_NAMES:
                continue
            if not parts and skill_md is not None and name == skill_md:
                name = SKILL_MD
            rel = "/".join(parts + (name,))
            if is_link(entry):
                out.skipped.append({"path": rel, "error": "a link (not copied)"})
                continue
            _portable(name, rel)
            key = _fold(rel)
            if key in seen:
                raise SkillError(f"'{seen[key]}' and '{rel}' differ only in letter case or Unicode form: one would "
                                 "overwrite the other on Windows and macOS. Rename one of them.")
            seen[key] = rel
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise SkillError(f"Cannot read '{rel}': {exc.strerror or exc}") from None
            if stat.S_ISDIR(st.st_mode):
                if len(parts) + 1 > MAX_DEPTH:
                    raise SkillError(f"'{rel}' is nested more than {MAX_DEPTH} folders deep.", 413)
                visit(entry.path, parts + (name,))
            elif stat.S_ISREG(st.st_mode):
                out.files.append((rel, st.st_size, st.st_mtime_ns, entry.path, st.st_mode))
                out.size += st.st_size
                if len(out.files) > limits.max_files:
                    raise SkillError(f"The skill has more than {limits.max_files} files ([skills] max_files).", 413)
                if out.size > limits.max_bytes:
                    raise SkillError(f"The skill's files are larger than {limits.max_mb} MB ([skills] max_mb).", 413)
            else:
                out.skipped.append({"path": rel, "error": "not a regular file (not copied)"})

    visit(long_path(root), ())
    return out


class _NoSkillMd(SkillError):
    """The folder has no SKILL.md at all (not a skill folder)."""


def _find_skill_md(root: Path) -> str:
    """How SKILL.md is spelled in `root`: exactly, or (only on a case-insensitive file system, where both name the
    same file) in another case. `_NoSkillMd` when there is none."""
    try:
        with os.scandir(long_path(root)) as it:
            names = [e.name for e in it]
    except OSError as exc:
        raise SkillError(f"Cannot read the folder {root}: {exc.strerror or exc}") from None
    if SKILL_MD in names:
        return SKILL_MD
    other = next((n for n in names if n.casefold() == SKILL_MD.casefold()), None)
    if other is not None and os.path.lexists(long_path(Path(root) / SKILL_MD)):
        return other
    if other is not None:
        raise SkillError(f"The skill file must be named exactly SKILL.md (found {other}).")
    raise _NoSkillMd(f"No SKILL.md in {root}.")


def _read_skill_md(path: Path) -> str:
    try:
        st = os.lstat(long_path(path))
    except OSError as exc:
        raise SkillError(f"Cannot read SKILL.md: {exc.strerror or exc}") from None
    if not stat.S_ISREG(st.st_mode) or is_link(path):
        raise SkillError("SKILL.md must be a regular file (not a link).")
    if st.st_size > SKILL_MD_MAX:
        raise SkillError(f"SKILL.md is larger than {SKILL_MD_MAX // 1000} KB; move details into other files of the "
                         "skill.", 413)
    try:
        with open(long_path(path), "rb") as f:
            data = f.read(SKILL_MD_MAX + 1)
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise SkillError("SKILL.md is not UTF-8 text.") from None
    except OSError as exc:
        raise SkillError(f"Cannot read SKILL.md: {exc.strerror or exc}") from None


def _file_digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(COPY_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_digest(pairs: list[tuple[str, str]]) -> str:
    """The digest of a skill: sha256 over its sorted "rel\\0sha256(bytes)" lines (without .dreamteam.json)."""
    return hashlib.sha256("\n".join(f"{rel}\0{h}" for rel, h in sorted(pairs)).encode("utf-8")).hexdigest()


def check_skill_dir(folder: Path, limits: Limits, *, strict: bool = False, digest: bool = True,
                    fallback: str | None = None) -> SkillInfo:
    """Validate a skill folder (docs/SPEC.md §19). `strict` (agent proposals): the name and description must follow
    the rules as written; otherwise (imports, Claude Code) a name is slugged and the description kept. `digest`:
    hash the files (False: a stat-only check, `digest` ""). `fallback`: the name when SKILL.md has none, instead of
    the folder's (a zip's or a clone's root is a staging folder: its name means nothing)."""
    folder = Path(folder)
    if is_link(folder) or not folder.is_dir():
        raise SkillError(f"{folder} is not a folder.")
    spelled = _find_skill_md(folder)
    meta = parse_frontmatter(_read_skill_md(folder / spelled))
    written = " ".join((meta.get("name") or "").split()) or fallback or folder.name
    renamed_from = None
    if not (len(written) <= NAME_MAX and NAME_RE.fullmatch(written)):
        if strict:
            raise SkillError(f"The skill name '{written[:80]}' is not valid: a skill name is {NAME_RULE}. Fix the "
                             "name line of SKILL.md's front matter.")
        name = slug(written)
        if not name:
            raise SkillError(f"The skill name '{written[:80]}' has no letters or digits to make a name of.")
        renamed_from = written
    else:
        name = written
    description = " ".join((meta.get("description") or "").split())
    if not description:
        raise SkillError(f"Skill '{name}': SKILL.md's front matter needs a description line (what the skill is "
                         "for and when to use it).")
    if len(description) > DESCRIPTION_MAX:
        raise SkillError(f"Skill '{name}': the description is longer than {DESCRIPTION_MAX} characters.")
    if strict and _TAG_LIKE.search(description):
        raise SkillError(f"Skill '{name}': the description cannot contain tag-like text such as '<tag>'; write "
                         "it as plain words.")
    manual = (meta.get("disable-model-invocation") or "").strip().lower() == "true"
    walk = _walk(folder, limits, spelled)
    info = SkillInfo(name=name, description=description, path=folder, files=len(walk.files), size=walk.size,
                     digest="", manual=manual, renamed_from=renamed_from, skipped=walk.skipped,
                     manifest=[[rel, size, mtime] for rel, size, mtime, _p, _m in walk.files],
                     file_list=[(rel, size) for rel, size, _t, _p, _m in walk.files],
                     _sources=[(rel, p) for rel, _s, _t, p, _m in walk.files])
    if digest:
        try:
            info.digest = _tree_digest([(rel, _file_digest(p)) for rel, p in info._sources])
        except OSError as exc:
            raise SkillError(f"Cannot read the skill's files: {exc.strerror or exc}") from None
    return info


def stat_manifest(folder: Path, limits: Limits) -> list[list] | None:
    """The manifest of a stored skill as it is now (stats only), or None when it no longer passes the walk rules."""
    try:
        spelled = _find_skill_md(folder)
        return [[rel, size, mtime] for rel, size, mtime, _p, _m in _walk(folder, limits, spelled).files]
    except SkillError:
        return None


# ---- safe copy -------------------------------------------------------------------------------------------------
def _token() -> str:
    return secrets.token_hex(3)


def _temp_name(kind: str) -> str:
    """`.t-<hex>-<epoch>` (a copy being built) or `.o-<hex>-<epoch>` (an old copy on its way out). The time is in
    the name because a rename keeps a folder's old mtime: an `.o-` of a skill last changed long ago would look stale
    to clean_stale at once, and be removed before a failed swap could restore it."""
    return f".{kind}-{_token()}-{int(time.time())}"


def _rename(src: Path | str, dst: Path | str) -> None:
    for attempt in range(RENAME_TRIES):
        try:
            os.rename(long_path(src), long_path(dst))
            return
        except PermissionError:  # an antivirus scanner or an indexer holding a file for a moment
            if attempt == RENAME_TRIES - 1:
                raise
            time.sleep(RENAME_PAUSE)


def _remove(path: Path) -> None:
    """Remove a folder, a file or a link (a link is never followed)."""
    if is_link(path):
        # no chmod first: it follows the link and would change the mode of what the link points to
        with contextlib.suppress(FileNotFoundError):
            os.unlink(long_path(path))
    elif os.path.lexists(long_path(path)) and not os.path.isdir(long_path(path)):
        with contextlib.suppress(OSError):
            os.chmod(long_path(path), stat.S_IWRITE)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(long_path(path))
    else:
        rmtree(path)


def _copy_file(src: str, dst: str, mode: int) -> str:
    """Copy one checked regular file without following a link swapped in since the check (the opened file must be
    the one `lstat` sees); returns the sha256 of the bytes written."""
    try:
        before = os.lstat(src)
    except OSError as exc:
        raise SkillError(f"A file of the skill disappeared while it was copied: {exc.strerror or exc}", 409) from None
    if not stat.S_ISREG(before.st_mode):
        raise SkillError("The folder changed while it was being copied (a file became a link); try again.", 409)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(src, flags)
    except OSError as exc:
        raise SkillError(f"Cannot read a file of the skill: {exc.strerror or exc}", 409) from None
    h = hashlib.sha256()
    with os.fdopen(fd, "rb") as fin:
        now = os.fstat(fin.fileno())
        if not stat.S_ISREG(now.st_mode) or (before.st_ino and now.st_ino and (before.st_ino, before.st_dev)
                                             != (now.st_ino, now.st_dev)):
            raise SkillError("The folder changed while it was being copied (a file was replaced); try again.", 409)
        out_fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(out_fd, "wb") as fout:
            for chunk in iter(lambda: fin.read(COPY_CHUNK), b""):
                h.update(chunk)
                fout.write(chunk)
    os.chmod(dst, mode)
    return h.hexdigest()


def copy_skill(src: Path, dest_parent: Path, name: str | None, limits: Limits, *, strict: bool = False,
               info: SkillInfo | None = None, readonly: bool = False,
               meta: Callable[[SkillInfo], dict] | None = None) -> SkillInfo:
    """Copy the skill folder `src` to `dest_parent/<name>` (default: its checked name) atomically: built in a
    temporary `.t-*` folder on the same volume, then swapped in by renames (an existing copy is replaced, and
    restored if the swap fails). The copy is validated first (`info`: an existing check of `src`), never follows a
    link, and is refused when the bytes written differ from the checked ones. `readonly`: the files are made
    read-only (the agents' mirror). `meta`: the metadata (`.dreamteam.json`) to write into the copy, made from the
    copy's info. Returns the info of the copy (its path, digest and manifest)."""
    info = info or check_skill_dir(src, limits, strict=strict)
    name = check_name(name or info.name)
    dest_parent = Path(dest_parent)
    dest_parent.mkdir(parents=True, exist_ok=True)
    final = dest_parent / name
    if not is_link(final):  # a link there is replaced like an old copy (renamed away, unlinked), never followed
        final = child(dest_parent, name)
    tmp = dest_parent / _temp_name("t")
    os.mkdir(long_path(tmp))
    try:
        hashes, size = [], 0
        for rel, src_path in info._sources:
            dst = Path(tmp, *rel.split("/"))
            os.makedirs(long_path(dst.parent), exist_ok=True)
            mode = os.lstat(src_path).st_mode & 0o755
            mode = (0o555 if mode & 0o111 else 0o444) if readonly else (mode | 0o600)
            hashes.append((rel, _copy_file(src_path, long_path(dst), mode)))
            size += os.lstat(long_path(dst)).st_size
        if len(hashes) > limits.max_files or size > limits.max_bytes:
            raise SkillError("The folder grew past the skill limits while it was being copied; try again.", 413)
        digest = _tree_digest(hashes)
        if info.digest and digest != info.digest:
            raise SkillError("The folder changed while it was being copied; try again.", 409)
        walk = _walk(tmp, limits, SKILL_MD)
        copied = SkillInfo(name=name, description=info.description, path=final, files=len(hashes), size=size,
                           digest=digest, manual=info.manual, renamed_from=info.renamed_from, skipped=info.skipped,
                           manifest=[[rel, s, t] for rel, s, t, _p, _m in walk.files],
                           file_list=[(rel, s) for rel, s, _t, _p, _m in walk.files],
                           _sources=[(rel, str(final.joinpath(*rel.split("/")))) for rel, *_x in walk.files])
        if meta is not None:
            _write_json(tmp / META_FILE, meta(copied))
        old = None
        if os.path.lexists(long_path(final)):
            old = dest_parent / _temp_name("o")
            _rename(final, old)
        try:
            _rename(tmp, final)
        except BaseException:
            if old is not None:
                with contextlib.suppress(OSError):
                    _rename(old, final)
            raise
    except BaseException:
        with contextlib.suppress(OSError):
            rmtree(tmp)
        raise
    if old is not None:
        with contextlib.suppress(OSError):
            _remove(old)
    return copied


def clean_stale(parent: Path, older_than: float = STALE_SECONDS) -> None:
    """Remove `.t-*` / `.o-*` folders a crashed copy left in `parent` (best effort). Their age is the later of the
    time in their name (`_temp_name`) and their mtime."""
    now = time.time()

    def made(entry: os.DirEntry) -> float:
        when = entry.stat(follow_symlinks=False).st_mtime
        stamp = entry.name.rpartition("-")[2]
        return max(when, int(stamp)) if stamp.isdigit() and len(stamp) <= 12 else when

    try:
        with os.scandir(long_path(parent)) as it:
            stale = [Path(parent, e.name) for e in it if e.name[:3] in (".t-", ".o-") and now - made(e) > older_than]
    except OSError:
        return
    for path in stale:
        with contextlib.suppress(OSError):
            _remove(path)


def _read_json(path: Path) -> dict | None:
    try:
        with open(long_path(path), "rb") as f:
            data = json.loads(f.read().decode("utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict) -> None:
    tmp = Path(path).with_name(f"{Path(path).name}.{_token()}.tmp")
    with open(long_path(tmp), "wb") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
    try:
        os.replace(long_path(tmp), long_path(path))
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(long_path(tmp))
        raise


# ---- the library -----------------------------------------------------------------------------------------------
class Library:
    """The shared skills library: one folder per skill with DreamTeam's metadata in `.dreamteam.json`
    (docs/SPEC.md §19). `read_only`: the host's library inside the Docker sandbox (add and remove refuse)."""

    def __init__(self, root: Path, limits: Limits | None = None, *, read_only: bool = False,
                 config: "Config | None" = None):
        self.root = Path(root)
        self.limits = limits or Limits()
        self.read_only = read_only
        self.config = config

    @classmethod
    def from_config(cls, config: "Config") -> "Library":
        return cls(config.skills_library_dir, Limits.from_config(config), config=config)

    @classmethod
    def host(cls, config: "Config") -> "Library | None":
        """Inside the Docker sandbox: the host's library, read-only; None elsewhere."""
        root = config.skills_host_library_dir
        return cls(root, Limits.from_config(config), read_only=True, config=config) if root else None

    def _refuse_write(self) -> None:
        if self.read_only:
            raise SkillError("The host's skills library is read-only inside the sandbox.", 403)

    def _names(self) -> list[str]:
        try:
            with os.scandir(long_path(self.root)) as it:
                names = [e.name for e in it if not e.name.startswith(".")]
        except OSError:
            return []
        return sorted(n for n in names if len(n) <= NAME_MAX and NAME_RE.fullmatch(n))

    def _entry(self, name: str) -> dict | None:
        """The public entry of `name` (None when there is no such folder). A link there (a symlink or a junction,
        made by hand or planted) is an entry with an error, never followed: it cannot break the whole listing and
        `remove` deletes the link only."""
        folder = self.root / check_name(name)
        if is_link(folder):
            return {"name": name, "error": "A link, not a folder: links in the library are not used (deleting it "
                    "removes the link only; import the skill instead).", "origin": "folder"}
        try:
            folder = child(self.root, name)
        except SkillError as exc:
            return {"name": name, "error": str(exc), "origin": "folder"}
        if not os.path.isdir(long_path(folder)):
            return None
        meta = _read_json(folder / META_FILE)
        try:
            if meta is None or not isinstance(meta.get("manifest"), list) or not meta.get("digest"):
                info = check_skill_dir(folder, self.limits)
                now = time.time()
                # not added through DreamTeam (dropped in, or its metadata deleted): flagged as changed outside
                # until the user imports it again, so it never looks like a clean import (docs/SPEC.md §19)
                meta = {"name": name, "description": info.description, "origin": "folder",
                        "source": {"path": str(folder)}, "added_at": now, "updated_at": now, "digest": info.digest,
                        "manifest": info.manifest, "by": "", "via": "outside", "outside": True, "files": info.files,
                        "size": info.size, "manual": info.manual}
                if not self.read_only:  # rebuilt once: from now on a change behind DreamTeam's back is noticed
                    with contextlib.suppress(OSError):
                        _write_json(folder / META_FILE, meta)
                return self._public(name, meta, info, changed=True)
            info = check_skill_dir(folder, self.limits, digest=False)
        except SkillError as exc:
            return {"name": name, "error": str(exc), "origin": (meta or {}).get("origin", "folder")}
        changed = info.manifest != meta.get("manifest") or bool(meta.get("outside"))
        return self._public(name, meta, info, changed=changed)

    @staticmethod
    def _public(name: str, meta: dict, info: SkillInfo, *, changed: bool) -> dict:
        return {"name": name, "description": info.description, "origin": meta.get("origin") or "folder",
                "source": meta.get("source") if isinstance(meta.get("source"), dict) else {},
                "added_at": meta.get("added_at"), "updated_at": meta.get("updated_at"), "files": info.files,
                "size": info.size, "digest": meta.get("digest") or "", "manual": info.manual,
                "by": meta.get("by") or "", "via": meta.get("via") or "", "changed_outside": changed}

    def list(self) -> list[dict]:
        """Every entry, sorted by name; a broken folder is `{"name", "error"}` (shown, not attachable)."""
        if not self.read_only:
            clean_stale(self.root)
        return [e for e in (self._entry(n) for n in self._names()) if e is not None]

    def get(self, name: str) -> dict | None:
        return self._entry(check_name(name))

    def path(self, name: str) -> Path | None:
        """The folder of a valid entry."""
        entry = self.get(name)
        return child(self.root, name) if entry and "error" not in entry else None

    def show(self, name: str) -> dict:
        """The entry plus its files (`files_list`, at most 500) and the text of its SKILL.md."""
        entry = self.get(name)
        if entry is None:
            raise SkillError(f"No skill '{name}' in the library.", 404)
        if is_link(self.root / name):  # never followed, not even to show it
            return {**entry, "files_list": [], "skill_md": "", "deps": {}}
        return {**entry, **show_folder(child(self.root, name), self.limits)}

    def digest_of(self, name: str) -> str | None:
        """The real content digest of an entry (hashed again when its files changed outside DreamTeam); None when
        it is missing or broken."""
        entry = self.get(name)
        if entry is None or "error" in entry:
            return None
        if not entry["changed_outside"] and entry["digest"]:
            return entry["digest"]
        try:
            return check_skill_dir(child(self.root, name), self.limits).digest
        except SkillError:
            return None

    def add(self, folder: Path, origin: str, source: dict, *, replace: bool = False, by: str = "user",
            via: str = "act", strict: bool = False, info: SkillInfo | None = None) -> dict:
        """Copy one checked skill folder into the library under its (slugged) name. The same content again is
        `"unchanged": true` (only `updated_at` and `source` change); other content under that name is a 409 unless
        `replace`. `by` / `via`: who saved it and through which surface (recorded, docs/SPEC.md §19)."""
        self._refuse_write()
        if origin not in ORIGINS:
            raise SkillError(f"Unknown skill origin '{origin}'.")
        info = info or check_skill_dir(folder, self.limits, strict=strict)
        name = check_name(info.name)
        self.root.mkdir(parents=True, exist_ok=True)
        existing = self.get(name)
        now = time.time()
        if existing is not None:
            same = "error" not in existing and self.digest_of(name) == info.digest
            if same:
                target = child(self.root, name)
                meta = _read_json(target / META_FILE) or {}
                manifest = stat_manifest(target, self.limits)
                meta.update(updated_at=now, source=dict(source))
                if existing["changed_outside"]:  # the user vouches for it again: the flag is cleared
                    meta.pop("outside", None)
                    meta.update(manifest=manifest, digest=info.digest, by=by, via=via)
                _write_json(target / META_FILE, meta)
                entry = self.get(name) or existing
                return {**entry, "unchanged": True}
            if not replace:
                raise SkillError(f"The library already has a skill '{name}' (different content). Replace it?", 409,
                                 exists=[name])
        added_at = (existing or {}).get("added_at") or now

        def make_meta(copied: SkillInfo) -> dict:
            return {"name": name, "description": copied.description, "origin": origin, "source": dict(source),
                    "added_at": added_at, "updated_at": now, "digest": copied.digest, "manifest": copied.manifest,
                    "by": by, "via": via, "files": copied.files, "size": copied.size, "manual": copied.manual}

        copy_skill(info.path, self.root, name, self.limits, info=info, meta=make_meta)
        entry = self.get(name)
        if entry is None:
            raise SkillError(f"The skill '{name}' could not be stored in the library.", 500)
        return {**entry, "unchanged": False}

    def remove(self, name: str) -> None:
        """Remove an entry; a link put there is removed itself, never what it points to."""
        self._refuse_write()
        folder = self.root / check_name(name)
        if not is_link(folder):
            folder = child(self.root, name)
        if not os.path.lexists(long_path(folder)):
            raise SkillError(f"No skill '{name}' in the library.", 404)
        trash = self.root / _temp_name("o")
        try:
            _rename(folder, trash)  # gone from listings at once, then removed
        except OSError as exc:
            raise SkillError(f"Cannot remove the skill '{name}': {exc.strerror or exc} (a file in use?)", 409) from None
        _remove(trash)


def show_folder(folder: Path, limits: Limits) -> dict:
    """`files_list` [{path, size}] (at most 500), `skill_md` (its text) and `deps` (deps_preview) of a skill
    folder."""
    try:
        spelled = _find_skill_md(folder)
        walk = _walk(folder, limits, spelled)
        text = _read_skill_md(Path(folder) / spelled)
    except SkillError as exc:
        return {"files_list": [], "skill_md": "", "deps": {}, "error": str(exc)}
    return {"files_list": [{"path": rel, "size": size} for rel, size, *_x in walk.files[:SHOW_FILES_MAX]],
            "skill_md": text, "deps": deps_preview(folder)}


def deps_preview(folder: Path) -> dict:
    """What attaching the skill folder would install, for the previews (skill_deps.detect: files read, no tool
    probed, nothing started): `{"python"?: {"files", "packages", "unsupported"?}, "node"?: {"files", "packages",
    "lock", "unsupported"?, "ignored"?}}`; `{}` without manifests."""
    from . import skill_deps   # (skill_deps imports this module)

    try:
        found = skill_deps.detect(Path(folder))
    except (OSError, ValueError) as exc:   # a preview never fails the listing it is part of
        log.debug("cannot read the dependency manifests of %s: %s", folder, exc)
        return {}
    out: dict[str, dict] = {}
    if found.python is not None:
        py = found.python
        out["python"] = {"files": list(py.files), "packages": list(py.packages),
                         **({"unsupported": py.unsupported} if py.unsupported else {})}
    if found.node is not None:
        node = found.node
        out["node"] = {"files": list(node.files), "packages": list(node.packages), "lock": node.lock,
                       **({"unsupported": node.unsupported} if node.unsupported else {}),
                       **({"ignored": list(node.ignored)} if node.ignored else {})}
    return out


def install_info(config: "Config") -> dict:
    """The `[skills]` dependency switches the previews word their line with (`install` of the skills routes)."""
    s = config.skills
    return {"enabled": s.install, "proposals": s.install_proposals, "scripts": s.install_scripts}


# ---- Claude Code and the host library --------------------------------------------------------------------------
def claude_user_dir() -> Path:
    """Claude Code's own skills folder: `$CLAUDE_CONFIG_DIR/skills`, default `~/.claude/skills`."""
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser() / "skills"


def claude_project_dir(workspace: Path | None) -> Path | None:
    return Path(workspace) / ".claude" / "skills" if workspace else None


def _claude_items(folder: Path | None, origin: str, limits: Limits, lib: Library | None) -> list[dict]:
    if folder is None:
        return []
    try:
        with os.scandir(long_path(folder)) as it:
            entries = sorted((e for e in it if not e.name.startswith(".")), key=lambda e: e.name)
    except OSError:
        return []
    out = []
    for entry in entries:
        if is_link(entry) or not entry.is_dir(follow_symlinks=False):
            continue
        path = Path(folder, entry.name)
        item: dict[str, Any] = {"origin": origin, "path": str(path)}
        if origin == "claude-project":
            item["agent_writable"] = True
        try:
            info = check_skill_dir(path, limits)
        except _NoSkillMd:
            continue
        except SkillError as exc:
            out.append({"name": slug(entry.name) or entry.name, **item, "error": str(exc)})
            continue
        item.update(info.public())
        if lib is not None:
            item["library"] = ("absent" if lib.get(info.name) is None else
                               "same" if lib.digest_of(info.name) == info.digest else "different")
        out.append({"name": info.name, **item})
    return out


def claude_skills(config: "Config", workspace: Path | None = None) -> list[dict]:
    """Claude Code's skills (read-only): `claude-user` from `$CLAUDE_CONFIG_DIR/skills` (default ~/.claude/skills)
    and `claude-project` from `<workspace>/.claude/skills` (agent-writable: attached only when named explicitly).
    `[]` when `[skills] claude` is false. Links are never followed."""
    if not config.skills.claude:
        return []
    limits, lib = Limits.from_config(config), Library.from_config(config)
    return (_claude_items(claude_user_dir(), "claude-user", limits, lib)
            + _claude_items(claude_project_dir(workspace), "claude-project", limits, lib))


def host_skills(config: "Config") -> list[dict]:
    """Inside the Docker sandbox: the host library's entries as the read-only source `host-library`."""
    host = Library.host(config)
    if host is None:
        return []
    return [{**e, "origin": "host-library", "host_origin": e.get("origin"), "read_only": True,
             "path": str(host.root / e["name"])} for e in host.list()]


def resolve_ref(config: "Config", ref: str, *, workspace: Path | None = None) -> dict:
    """The skill a reference names: `{"origin": "library"|"claude-user"|"claude-project"|"host-library", "name",
    "path"}`. A bare name: the library, then ~/.claude/skills (when `[skills] claude`), then the host library (sandbox
    only); `<workspace>/.claude/skills` only when named (`claude-project:x`), since agents can write there.
    SkillError 404 when nothing has it."""
    source, name = parse_ref(ref)
    lib = Library.from_config(config)
    order = [source] if source else ["library", "claude-user", "host-library"]
    for origin in order:
        if origin == "library":
            path = lib.path(name)
        elif origin == "host-library":
            host = Library.host(config)
            path = host.path(name) if host is not None else None
        else:
            folder = claude_user_dir() if origin == "claude-user" else claude_project_dir(workspace)
            items = _claude_items(folder, origin, lib.limits, None) if config.skills.claude else []
            path = next((Path(i["path"]) for i in items if i["name"] == name and "error" not in i), None)
        if path is not None:
            return {"origin": origin, "name": name, "path": path}
    if source == "claude-project":
        raise SkillError(f"No skill '{name}' in the workspace's .claude/skills." if workspace else
                         "claude-project: needs the session's folder (its .claude/skills).", 404)
    if source == "library":
        names = [e["name"] for e in lib.list() if "error" not in e]
        raise SkillError(f"No skill '{name}' in the library. Library: {', '.join(names) or '(empty)'}.", 404)
    if source in ("claude-user", "host-library"):
        where = "~/.claude/skills" if source == "claude-user" else "the host's library"
        off = " (Claude Code skills are off: [skills] claude = false)" if source == "claude-user" and \
            not config.skills.claude else ""
        raise SkillError(f"No skill '{name}' in {where}{off}.", 404)
    names = [e["name"] for e in lib.list() if "error" not in e]
    text = (f"No skill '{name}' in the library or in ~/.claude/skills. Library: "
            f"{', '.join(names) if names else '(empty)'}.")
    project = claude_project_dir(workspace)
    if project is not None and os.path.isdir(long_path(project / name)):
        text += (f" The workspace's .claude/skills has one (agents can write there): attach claude-project:{name} "
                 "to use it.")
    raise SkillError(text, 404)


# ---- zip ------------------------------------------------------------------------------------------------------
def _zip_parts(raw: str) -> list[str]:
    name = raw.replace("\\", "/")
    if not name or name.startswith("/") or re.match(r"[A-Za-z]:", name):
        raise SkillError(f"The zip holds an absolute path ('{raw[:80]}'), which is not allowed.")
    parts = name.rstrip("/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise SkillError(f"The zip holds the path '{raw[:80]}' (with '..', '.' or an empty part), which is not "
                         "allowed.")
    return parts


def extract_zip(data: bytes, dest: Path, limits: Limits) -> list[dict]:
    """Extract a zip into the fresh folder `dest` with every member checked (no absolute paths, '..', drive
    letters, duplicates, case or Unicode collisions, links or encrypted members; sizes streamed against
    `max_archive_mb`, the compression ratio bounded). Returns the members skipped (`__MACOSX/`, `.DS_Store`,
    `.dreamteam.json`)."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError):
        raise SkillError("The file is not a zip archive.") from None
    with archive:
        infos = archive.infolist()
        if len(infos) > ZIP_MAX_ENTRIES:
            raise SkillError(f"The zip has more than {ZIP_MAX_ENTRIES} entries.", 413)
        if sum(i.file_size for i in infos) > limits.archive_bytes:
            raise SkillError(f"The zip unpacks to more than {limits.max_archive_mb} MB ([skills] max_archive_mb).",
                             413)
        skipped: list[dict] = []
        seen: dict[str, tuple[str, bool]] = {}   # folded path -> (as written, is a folder)
        planned: list[tuple[zipfile.ZipInfo, list[str]]] = []
        for info in infos:
            parts = _zip_parts(info.filename)
            rel = "/".join(parts)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise SkillError(f"The zip holds a link ('{rel}'), which is not allowed.")
            if info.is_dir():
                continue
            if any(p == "__MACOSX" for p in parts) or parts[-1] in (".DS_Store", META_FILE):
                skipped.append({"path": rel, "error": "skipped"})
                continue
            if info.flag_bits & 0x1:
                raise SkillError("Encrypted zip entries are not supported.")
            for i, part in enumerate(parts):
                _portable(part, rel)
                sub = "/".join(parts[:i + 1])
                is_folder = i < len(parts) - 1
                key = _fold(sub)
                if key in seen:
                    written, was_folder = seen[key]
                    if not (is_folder and was_folder and written == sub):
                        why = ("differ only in letter case or Unicode form" if written != sub else
                               "are a file and a folder of the same name" if is_folder != was_folder else
                               "are the same file twice")
                        raise SkillError(f"The zip holds '{written}' and '{sub}', which {why}.")
                else:
                    seen[key] = (sub, is_folder)
            planned.append((info, parts))
        total = 0
        os.makedirs(long_path(dest), exist_ok=True)
        for info, parts in planned:
            target = Path(dest, *parts)
            os.makedirs(long_path(target.parent), exist_ok=True)
            written = 0
            try:
                # the declared sizes cannot be trusted: the bytes are counted as they come
                with archive.open(info) as src, open(long_path(target), "xb") as out:
                    for chunk in iter(lambda: src.read(COPY_CHUNK), b""):
                        written += len(chunk)
                        total += len(chunk)
                        if total > limits.archive_bytes:
                            raise SkillError(f"The zip unpacks to more than {limits.max_archive_mb} MB ([skills] "
                                             "max_archive_mb).", 413)
                        # (small files of one repeated byte compress far better: only large members count)
                        if written > 1 << 20 and written > ZIP_MAX_RATIO * max(info.compress_size, 1):
                            raise SkillError(f"'{'/'.join(parts)}' in the zip looks like a zip bomb (compressed "
                                             f"more than {ZIP_MAX_RATIO} times).", 413)
                        out.write(chunk)
            except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError) as exc:
                raise SkillError(f"The zip is damaged or uses an unsupported format ('{'/'.join(parts)}': "
                                 f"{exc}).") from None
            except OSError as exc:
                raise SkillError(f"Cannot extract '{'/'.join(parts)}': {exc.strerror or exc}", 500) from None
            if (info.external_attr >> 16) & 0o111 and not _WINDOWS:
                os.chmod(target, 0o755)
        return skipped


# ---- Git ------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class GitSource:
    url: str = field(repr=False)   # the full URL, credentials included: only ever given to git
    ref: str | None
    subdir: str | None
    shown: str                     # without credentials, query and fragment: the only form stored or shown
    scheme: str = "https"          # https | ssh | scp | file
    secret: str = field(default="", repr=False)   # the URL's user info when it is not shown (redacted from output)


_HOST = re.compile(r"(?:[A-Za-z0-9][A-Za-z0-9.-]*|\[[0-9A-Fa-f:.]+\])\Z")
_GIT_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_SCP = re.compile(r"(?P<user>[^@/\s]+)@(?P<host>\[[^\]/]+\]|[^:/\s\[]+):(?P<path>.+)\Z")
_URL_USERINFO = re.compile(r"(?i)\b((?:https?|ssh|file)://)([^/\s@]+)@")


def _check_ref(ref: str) -> str:
    if (len(ref) > 200 or not _GIT_REF.match(ref) or ".." in ref or "@{" in ref or "//" in ref
            or ref.endswith(("/", ".", ".lock"))):
        raise SkillError(f"'{ref[:80]}' is not a valid Git branch or tag name. A ref with '/' in it: write "
                         "URL#ref:path.")
    return ref


def _check_subdir(subdir: str | None) -> str | None:
    text = (subdir or "").strip()
    if not text:
        return None
    if "\\" in text or ":" in text or PurePosixPath(text).is_absolute():
        raise SkillError(f"The folder '{text[:80]}' in the Git source is not a plain relative path.")
    parts = text.rstrip("/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise SkillError(f"The folder '{subdir[:80]}' in the Git source may not hold '..', '.' or empty parts.")
    return "/".join(parts)


def _netloc(netloc: str, scheme: str) -> tuple[str, str]:
    """(user info that is not shown, netloc shown) of an URL; the host and the user are checked."""
    userinfo, at, hostport = netloc.rpartition("@")
    if hostport.startswith("["):
        host = hostport[:hostport.find("]") + 1] if "]" in hostport else hostport
        port = hostport[len(host):]
    else:
        host, _colon, port = hostport.partition(":")
        port = (":" + port) if _colon else ""
    if port and not re.fullmatch(r":\d{1,5}", port):
        raise SkillError("The Git URL has an invalid port.")
    if host or scheme != "file":
        if not _HOST.match(host):
            raise SkillError(f"'{host[:80]}' is not a valid host name.")
    user = userinfo.split(":", 1)[0] if at else ""
    if user.startswith("-"):
        raise SkillError("The user part of a Git URL cannot start with '-'.")
    # ssh: the bare user ("git@") is no secret; https and file: any user info may be a token
    kept = f"{user}@" if (scheme == "ssh" and user and ":" not in userinfo) else ""
    return ("" if kept or not at else userinfo), kept + host + port


def parse_git_source(text: Any) -> GitSource:
    """A checked Git source: `https://`, `ssh://`, scp-like `user@host:path` or `file://`; `URL#ref:subdir`,
    `URL#ref`, `URL#:subdir`, and GitHub / GitLab `tree` and `blob` URLs. Credentials never reach `shown`."""
    if not isinstance(text, str) or not text.strip():
        raise SkillError("Give a Git URL.")
    value = text.strip()
    if re.search(r"\s", value):
        raise SkillError("A Git URL cannot contain spaces.")
    if value.startswith("-"):
        raise SkillError("A Git URL cannot start with '-'.")
    if re.match(r"[A-Za-z][A-Za-z0-9+.-]*::", value):
        raise SkillError("Git remote helpers (transport::address) are not allowed; use an https://, ssh:// or "
                         "file:// URL.")
    base, _hash, fragment = value.partition("#")
    ref = subdir = None
    if _hash:
        ref, _colon, sub = fragment.partition(":")
        ref = ref or None
        subdir = sub if _colon else None
    lower = base.lower()
    if lower.startswith("http://"):
        raise SkillError("Plain http:// is not allowed for Git; use https://.")
    if lower.startswith(("https://", "ssh://", "file://")):
        parts = urlsplit(base)
        scheme = parts.scheme.lower()
        netloc = parts.netloc
        secret, shown_netloc = _netloc(netloc, scheme)
        path = parts.path
        segments = [s for s in path.split("/") if s]
        tree = None
        if scheme == "https" and parts.hostname and parts.hostname.lower() in ("github.com", "www.github.com") \
                and len(segments) >= 4 and segments[2] in ("tree", "blob"):
            tree = (segments[:2], segments[2], segments[3], segments[4:])
        elif scheme == "https" and "-" in segments:
            i = segments.index("-")
            if i >= 2 and len(segments) > i + 2 and segments[i + 1] in ("tree", "blob"):
                tree = (segments[:i], segments[i + 1], segments[i + 2], segments[i + 3:])
        if tree is not None:
            repo, kind, tree_ref, rest = tree
            if kind == "blob" and rest:
                rest = rest[:-1]   # a file (SKILL.md, or another file of the skill): its folder
            name = "/".join(repo)
            path = "/" + (name if name.endswith(".git") else name + ".git")
            if not _hash:
                ref, subdir = tree_ref, "/".join(rest) or None
        url = f"{scheme}://{netloc}{path}" + (f"?{parts.query}" if parts.query and tree is None else "")
        shown = f"{scheme}://{shown_netloc}{path}"
    else:
        match = _SCP.match(base)
        if not match or "://" in base:
            raise SkillError("Not a Git URL: use https://host/path, ssh://host/path, user@host:path or "
                             "file:///path (#ref:folder picks a branch and a folder).")
        user, host, path = match.group("user"), match.group("host"), match.group("path")
        if user.startswith("-") or not _HOST.match(host):
            raise SkillError("The user or host of the Git URL is not valid.")
        scheme = "scp"
        url = base
        shown = f"{user.split(':', 1)[0]}@{host}:{path}"
        secret = user if ":" in user else ""
    if ref is not None:
        _check_ref(ref)
    return GitSource(url=url, ref=ref, subdir=_check_subdir(subdir), shown=shown, scheme=scheme, secret=secret)


def redact(text: str, src: GitSource) -> str:
    """`text` (git's output) with the full URL, the URL's hidden user info (git also prints it without the
    scheme: "'u:token@host/x' does not appear to be a git repository") and any `//user:secret@` removed: only
    `shown` may be seen."""
    text = (text or "").replace(src.url, src.shown)
    if src.secret:
        kept = src.secret.split(":", 1)[0] + "@" if src.scheme == "scp" else ""
        text = text.replace(src.secret + "@", kept)
        for part in {src.secret, *src.secret.split(":", 1)[1:]}:
            if len(part) >= 4:
                text = text.replace(part, "***")
    return _URL_USERINFO.sub(lambda m: m.group(1) + (m.group(2) + "@" if m.group(1).lower() == "ssh://" and ":"
                                                     not in m.group(2) else ""), text)


@functools.lru_cache(maxsize=8)
def _git_version(git: str) -> tuple[int, int]:
    try:
        out = subprocess.run([git, "--version"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL,
                             **_no_window()).stdout.decode("ascii", "replace")
    except (OSError, subprocess.SubprocessError):
        return (0, 0)
    match = re.search(r"(\d+)\.(\d+)", out)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def _no_window() -> dict[str, Any]:
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if _WINDOWS else {}


def clone_env(git: str, cwd: Path) -> dict[str, str]:
    """The environment of a clone: no inherited GIT_* (git_env) but the user's ssh setup, no prompts at all
    (credential managers, ssh questions: `BatchMode=yes` unless the user has an ssh command of their own), no LFS
    downloads."""
    env = git_env()
    env.update(GCM_INTERACTIVE="never", GIT_LFS_SKIP_SMUDGE="1")
    for name in ("GIT_SSH_COMMAND", "GIT_SSH", "GIT_SSH_VARIANT"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    if not env.get("GIT_SSH_COMMAND") and not env.get("GIT_SSH"):
        try:
            own = subprocess.run([git, "config", "--get", "core.sshCommand"], cwd=str(cwd), env=env,
                                 capture_output=True, timeout=30, stdin=subprocess.DEVNULL, **_no_window())
            has_own = own.returncode == 0 and bool(own.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            has_own = False
        if not has_own:
            env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    return env


def _tree_size(folder: Path) -> int:
    total = 0
    stack = [long_path(folder)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def _run_watched(argv: list[str], *, cwd: Path, env: dict[str, str], watch: Path, cap: int, deadline: float,
                 src: GitSource, timeout: float, err_file: Path) -> None:
    from .shell import kill_tree

    with open(err_file, "wb") as err:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=err, start_new_session=not _WINDOWS, **_no_window())
        try:
            while True:
                try:
                    proc.wait(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if _tree_size(watch) > cap:
                    kill_tree(proc)
                    proc.wait(timeout=30)
                    raise SkillError(f"The repository is larger than max_archive_mb ({cap // 2 ** 20} MB); point "
                                     "the URL at the skill's folder.", 413)
                if time.monotonic() > deadline:
                    kill_tree(proc)
                    proc.wait(timeout=30)
                    raise SkillError(f"git clone timed out after {int(timeout)} s.", 400)
        except BaseException:
            if proc.poll() is None:
                kill_tree(proc)
            raise
    if proc.returncode != 0:
        lines = [ln.strip() for ln in err_file.read_bytes().decode("utf-8", "replace").splitlines() if ln.strip()]
        fatal = [ln for ln in lines if ln.lower().startswith(("fatal:", "error:"))]
        last = fatal[0] if fatal else (lines or [f"git exited with code {proc.returncode}"])[-1]
        raise SkillError(f"Could not clone {src.shown}: {redact(last, src)}", 400)
    if _tree_size(watch) > cap:
        raise SkillError(f"The repository is larger than max_archive_mb ({cap // 2 ** 20} MB); point the URL at "
                         "the skill's folder.", 413)


def clone(src: GitSource, dest: Path, limits: Limits, git: str | None = None,
          timeout: float = GIT_TIMEOUT) -> str:
    """A shallow clone of `src` into `dest` (inside a fresh staging folder, the cwd), only the requested folder
    checked out when git can (partial and sparse); returns the commit. Hardened: an explicit protocol allow-list
    (no remote helpers, also through `insteadOf`), no hooks, no symlinks, no submodules, no LFS, no prompts, a
    size watchdog (`max_archive_mb`) and a timeout. SkillError with credentials redacted."""
    git = git or shutil.which("git")
    if not git:
        raise SkillError("git is not installed: install it, or download the skill and add the folder or zip.", 400)
    staging = Path(dest).parent
    hooks = staging / "nohooks"
    hooks.mkdir(exist_ok=True)
    env = clone_env(git, staging)
    common = ["-c", "credential.interactive=never", "-c", "core.symlinks=false", "-c", "core.longpaths=true",
              "-c", "advice.detachedHead=false", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
              "-c", "protocol.ssh.allow=always",
              *(("-c", "protocol.file.allow=always") if src.scheme == "file" else ()),
              "-c", f"core.hooksPath={hooks.as_posix()}", "-c", "submodule.recurse=false",
              "-c", "fetch.recurseSubmodules=false"]
    branch = [f"--branch={src.ref}"] if src.ref else []
    deadline = time.monotonic() + timeout
    run = functools.partial(_run_watched, cwd=staging, env=env, watch=staging, cap=limits.archive_bytes,
                            deadline=deadline, src=src, timeout=timeout, err_file=staging / "git-stderr.txt")
    if _git_version(git) >= GIT_PARTIAL:
        run([git, *common, "clone", "-q", "--depth", "1", "--single-branch", "--no-tags", "--filter=blob:none",
             "--no-checkout", *branch, "--", src.url, str(dest)])
        pattern = "/" + re.sub(r"([*?\[\]!#])", r"\\\1", src.subdir) + "/" if src.subdir else "/*"
        run([git, *common, "-C", str(dest), "sparse-checkout", "set", "--no-cone", "--", pattern])
        run([git, *common, "-C", str(dest), "checkout", "-q"])
    else:
        run([git, *common, "clone", "-q", "--depth", "1", "--single-branch", "--no-tags", *branch, "--", src.url,
             str(dest)])
    head = subprocess.run([git, *common, "-C", str(dest), "rev-parse", "HEAD"], cwd=str(staging), env=env,
                          capture_output=True, timeout=30, stdin=subprocess.DEVNULL, **_no_window())
    return head.stdout.decode("ascii", "replace").strip()


def _repo_name(src: GitSource) -> str:
    """The name of the folder a source's root stands for: the subdir's last part, else the repository's name
    without `.git` ("https://host/o/pdf-tools.git" -> "pdf-tools")."""
    if src.subdir:
        return src.subdir.rsplit("/", 1)[-1]
    path = re.split(r"[/:]", src.shown.rstrip("/"))[-1]
    return path[:-4] if path.lower().endswith(".git") else path


def _git_folder(dest: Path, subdir: str | None, src: GitSource) -> Path:
    """The skill folder inside a checkout: it must exist, stay inside the clone and contain no link."""
    if not subdir:
        return dest
    target = dest.joinpath(*subdir.split("/"))
    part = dest
    for name in subdir.split("/"):
        part = part / name
        if is_link(part):
            raise SkillError(f"'{subdir}' in {src.shown} is a link, which is not followed.")
    try:
        inside = os.path.normcase(str(target.resolve())).startswith(os.path.normcase(str(dest.resolve())) + os.sep)
    except (OSError, RuntimeError):
        inside = False
    if not inside or not target.is_dir():
        raise SkillError(f"{src.shown} has no folder '{subdir}'" + (f" at {src.ref}." if src.ref else "."))
    return target


# ---- sources: find, stage, commit -------------------------------------------------------------------------------
@dataclass
class _Candidate:
    rel: str
    path: Path
    info: SkillInfo | None = None
    error: SkillError | None = None
    fallback: str | None = None   # the name when SKILL.md has none (check_skill_dir)

    @property
    def name(self) -> str:
        """Its checked name; for a folder that failed the check, the folder's name made a skill name."""
        if self.info is not None:
            return self.info.name
        shown = self.fallback or self.path.name
        return slug(shown) or shown

    def public(self) -> dict:
        if self.info is None:
            return {"name": self.name, "description": "", "path": self.rel, "error": str(self.error)}
        return {**self.info.public(), "path": self.rel, "deps": deps_preview(self.path),
                **({"error": str(self.error)} if self.error else {})}


def find_skills(root: Path, limits: Limits, max_depth: int = FIND_DEPTH, *,
                root_name: str | None = None) -> list[_Candidate]:
    """The skill folders in a source: the root alone when it has a SKILL.md, else every folder with one down to
    `max_depth` levels (links and the skip-list left out, never below a found skill). Each is checked; a second
    candidate with the same name gets the error "duplicate name". `root_name`: the root's name when its SKILL.md
    names none (a zip's file name, a repository's name), instead of the staging folder's."""
    root = Path(root)
    found: list[_Candidate] = []

    def has_skill_md(folder: Path) -> bool:
        try:
            _find_skill_md(folder)
        except _NoSkillMd:
            return False
        except SkillError:  # a misspelled or unreadable one: a candidate that reports why
            pass
        return True

    def visit(folder: Path, parts: tuple[str, ...]) -> None:
        if has_skill_md(folder):
            found.append(_Candidate(rel="/".join(parts) or ".", path=folder, fallback=None if parts else root_name))
            return
        if len(parts) >= max_depth:
            return
        try:
            with os.scandir(long_path(folder)) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            return
        for entry in entries:
            if entry.name.casefold() in SKIP_NAMES or is_link(entry) or not entry.is_dir(follow_symlinks=False):
                continue
            visit(folder / entry.name, parts + (entry.name,))

    visit(root, ())
    names: set[str] = set()
    for cand in found:
        try:
            cand.info = check_skill_dir(cand.path, limits, fallback=cand.fallback)
        except SkillError as exc:
            cand.error = exc
            continue
        if cand.info.name in names:
            cand.error = SkillError(f"Two skills in the source are named '{cand.info.name}' (duplicate name).")
        names.add(cand.info.name)
    return found


class Staged:
    """A source fetched and validated into a staging folder (`library_dir/.t-<token>/`), nothing visible in the
    library yet. `commit` moves the picked skills in; `discard` (also on leaving a `with`) removes the staging
    folder. `need_pick`: several skills and no `pick`; `picked` is empty then."""

    def __init__(self, lib: Library, staging: Path, origin: str, base_source: dict, shown: str):
        self.lib = lib
        self.dir = staging
        self.origin = origin
        self.base_source = base_source
        self.shown = shown
        self.candidates: list[_Candidate] = []
        self.picked: list[_Candidate] = []
        self.skipped: list[dict] = []
        self.need_pick = False

    def __enter__(self) -> "Staged":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.discard()

    def discard(self) -> None:
        if self.dir is not None:
            with contextlib.suppress(OSError):
                rmtree(self.dir)

    def source_of(self, cand: _Candidate) -> dict:
        source = dict(self.base_source)
        if self.origin == "folder":
            source["path"] = str(Path(source["path"]).joinpath(*cand.rel.split("/"))) if cand.rel != "." \
                else source["path"]
        elif cand.rel != ".":
            source["subdir"] = "/".join(p for p in (source.get("subdir"), cand.rel) if p)
        return source

    def conflicts(self) -> list[str]:
        """The picked names the library has with other content (a commit without `replace` would be a 409)."""
        out = []
        for cand in self.picked:
            entry = self.lib.get(cand.info.name)
            if entry is not None and ("error" in entry or self.lib.digest_of(cand.info.name) != cand.info.digest):
                out.append(cand.info.name)
        return out

    def report(self, imported: list[dict] | None = None) -> dict:
        return {"imported": imported or [], "candidates": [c.public() for c in self.candidates],
                "skipped": self.skipped, "need_pick": self.need_pick}

    def commit(self, replace: bool = False, by: str = "user", via: str = "act") -> dict:
        """Copy the picked skills into the library (all clashes are checked before anything is added) and remove
        the staging folder. The import report (docs/SPEC.md §19)."""
        try:
            if self.need_pick:
                return self.report()
            clashes = [] if replace else self.conflicts()
            if clashes:
                raise SkillError(f"The library already has {'a skill' if len(clashes) == 1 else 'skills'} "
                                 f"{', '.join(repr(n) for n in clashes)} (different content). Replace "
                                 f"{'it' if len(clashes) == 1 else 'them'}?", 409, exists=clashes)
            imported = [self.lib.add(c.info.path, self.origin, self.source_of(c), replace=replace, by=by, via=via,
                                     info=c.info) for c in self.picked]
            return self.report(imported)
        finally:
            self.discard()


def _pick(staged: Staged, pick: list[str] | None) -> None:
    valid = [c for c in staged.candidates if c.error is None]
    if pick:
        chosen = []
        for name in pick:
            cand = next((c for c in staged.candidates if c.name == name and c.info is not None), None) or \
                next((c for c in staged.candidates if c.name == name), None)
            if cand is None:
                have = ", ".join(c.info.name for c in valid) or "none"
                raise SkillError(f"No skill '{name}' in {staged.shown}. It has: {have}.")
            if cand.error is not None:
                raise cand.error
            if cand not in chosen:
                chosen.append(cand)
        staged.picked = chosen
    elif len(valid) == 1:
        staged.picked = valid
    elif not valid:
        if len(staged.candidates) == 1:
            raise staged.candidates[0].error
        if not staged.candidates:
            raise SkillError(f"No SKILL.md found in {staged.shown}.")
        raise SkillError(f"No valid skill in {staged.shown}: " + "; ".join(
            f"{c.rel}: {c.error}" for c in staged.candidates))
    else:
        staged.need_pick = True
    for cand in staged.picked:
        staged.skipped += [{"path": s["path"] if cand.rel == "." else f"{cand.rel}/{s['path']}",
                            "error": s["error"]} for s in cand.info.skipped]


def _stage_dir(lib: Library) -> Path:
    lib.root.mkdir(parents=True, exist_ok=True)
    clean_stale(lib.root)
    staging = Path(os.path.abspath(lib.root)) / _temp_name("t")   # (git runs with it as its cwd)
    os.mkdir(long_path(staging))
    return staging


def _zip_bytes(spec: dict, limits: Limits) -> bytes:
    text = spec.get("zip")
    if not isinstance(text, str):
        raise SkillError("'zip' must be the archive as base64 text.")
    if len(text) > limits.zip_b64_max:
        raise SkillError(f"The zip is larger than {limits.max_archive_mb} MB ([skills] max_archive_mb).", 413)
    try:
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise SkillError("'zip' is not valid base64.") from None


def stage_source(lib: Library, spec: dict, *, pick: list[str] | None = None,
                 workspace: Path | None = None) -> Staged:
    """Fetch and validate a source into a staging folder: `{"path": abs folder or .zip}`, `{"zip": base64,
    "filename"}`, `{"git": url}`, `{"claude": name, "origin": "claude-user"|"claude-project"}` (`workspace`: the
    folder whose .claude/skills is the project's) or `{"host": name}` (the sandbox's host library). Nothing is
    visible in the library until `Staged.commit`."""
    if lib.read_only:
        lib._refuse_write()
    limits = lib.limits
    staging = _stage_dir(lib)
    root_name = None   # the name of a root skill whose SKILL.md names none (the root is a staging folder)
    try:
        if "path" in spec:
            text = spec["path"]
            if not isinstance(text, str) or not text.strip():
                raise SkillError("'path' must be a folder or .zip path.")
            given = Path(text.strip()).expanduser()
            if not given.is_absolute():
                raise SkillError(f"The path must be absolute, got '{text.strip()}' (clients resolve relative paths "
                                 "against their own current directory).")
            try:
                path = given.resolve()
            except (OSError, RuntimeError) as exc:
                raise SkillError(f"Cannot use {given}: {exc}") from None
            if path.is_file() and path.suffix.lower() == ".zip":
                size = path.stat().st_size
                if size > limits.archive_bytes:
                    raise SkillError(f"The zip is larger than {limits.max_archive_mb} MB ([skills] max_archive_mb).",
                                     413)
                staged = Staged(lib, staging, "zip", {"filename": path.name}, path.name)
                staged.skipped = extract_zip(path.read_bytes(), staging / "x", limits)
                root, root_name = staging / "x", path.stem
            elif path.is_dir():
                staged = Staged(lib, staging, "folder", {"path": str(path)}, str(path))
                root = path
            else:
                raise SkillError(f"No folder or .zip file at {path}.")
        elif "zip" in spec:
            filename = spec.get("filename") or "skills.zip"
            if not isinstance(filename, str):
                raise SkillError("'filename' must be a string.")
            filename = Path(filename.replace("\\", "/")).name[:200] or "skills.zip"
            staged = Staged(lib, staging, "zip", {"filename": filename}, filename)
            staged.skipped = extract_zip(_zip_bytes(spec, limits), staging / "x", limits)
            root, root_name = staging / "x", Path(filename).stem
        elif "git" in spec:
            src = parse_git_source(spec["git"])
            dest = staging / "r"
            commit = clone(src, dest, limits)
            staged = Staged(lib, staging, "git", {"url": src.shown, "ref": src.ref, "subdir": src.subdir,
                                                  "commit": commit}, src.shown)
            root, root_name = _git_folder(dest, src.subdir, src), _repo_name(src)
        elif "claude" in spec or "host" in spec:
            if "host" in spec:
                name, origin = check_name(spec["host"]), "host-library"
                host = Library.host(lib.config) if lib.config is not None else None
                folder = host.path(name) if host is not None else None
                missing = f"No skill '{name}' in the host's library."
            else:
                name, origin = check_name(spec["claude"]), spec.get("origin") or "claude-user"
                if origin not in ("claude-user", "claude-project"):
                    raise SkillError("'origin' must be claude-user or claude-project.")
                if lib.config is not None and not lib.config.skills.claude:
                    raise SkillError("Claude Code skills are off ([skills] claude = false).")
                where = claude_user_dir() if origin == "claude-user" else claude_project_dir(workspace)
                items = _claude_items(where, origin, limits, None)
                folder = next((Path(i["path"]) for i in items if i["name"] == name and "error" not in i), None)
                bad = next((i for i in items if i["name"] == name and "error" in i), None)
                if bad is not None and folder is None:
                    raise SkillError(f"The Claude Code skill '{name}': {bad['error']}")
                missing = (f"No skill '{name}' in ~/.claude/skills." if origin == "claude-user" else
                           f"No skill '{name}' in the workspace's .claude/skills.")
            if folder is None:
                raise SkillError(missing, 404)
            staged = Staged(lib, staging, origin, {"path": str(folder)}, str(folder))
            root = folder
        else:
            raise SkillError("Give exactly one of path, zip, git, claude.")
        staged.candidates = find_skills(root, limits, root_name=root_name)
        _pick(staged, pick)
        if staged.origin not in ("zip", "git"):
            # freeze what was checked: the commit copies from the staging folder, not from a folder that may change
            for cand in staged.picked:
                copied = copy_skill(cand.path, staging / "s", cand.info.name, limits, info=cand.info)
                copied.skipped = cand.info.skipped
                cand.info = copied
        return staged
    except BaseException as exc:
        with contextlib.suppress(OSError):
            rmtree(staging)
        if isinstance(exc, OSError):
            raise SkillError(f"Cannot read the source: {exc.strerror or exc}", 500) from None
        raise


def import_source(lib: Library, spec: dict, *, pick: list[str] | None = None, replace: bool = False,
                  workspace: Path | None = None, by: str = "user", via: str = "act") -> dict:
    """Stage a source and commit it to the library: the import report `{"imported": [entry + "unchanged"],
    "candidates": [...], "skipped": [{path, error}], "need_pick": bool}` (nothing imported when `need_pick`)."""
    return stage_source(lib, spec, pick=pick, workspace=workspace).commit(replace=replace, by=by, via=via)


SOURCE_KEYS = ("path", "zip", "git", "claude", "host")


def import_request(config: "Config", body: Any, workspace: Path | None = None, *, by: str = "user",
                   via: str = "act") -> dict:
    """An import from a REST or tool body (`{"path"} | {"zip", "filename"} | {"git"} | {"claude", "origin",
    "workspace"?}`, optional `pick`, `replace`) into the configured library; the shape is checked first (400)."""
    if not isinstance(body, dict):
        raise SkillError("The request must be a JSON object.")
    given = [k for k in SOURCE_KEYS if body.get(k) not in (None, "")]
    if len(given) != 1:
        raise SkillError("Give exactly one of path, zip, git, claude.")
    pick = body.get("pick")
    if pick is not None and (not isinstance(pick, list) or not all(isinstance(p, str) for p in pick)):
        raise SkillError("'pick' must be a list of skill names.")
    replace = body.get("replace", False)
    if not isinstance(replace, bool):
        raise SkillError("'replace' must be true or false.")
    key = given[0]
    spec = {key: body[key]}
    if key == "zip":
        spec["filename"] = body.get("filename")
    if key == "claude":
        spec["origin"] = body.get("origin")
        if body.get("workspace") not in (None, ""):
            if not isinstance(body["workspace"], str) or not Path(body["workspace"]).is_absolute():
                raise SkillError("'workspace' must be an absolute folder path.")
            workspace = Path(body["workspace"])
    return import_source(Library.from_config(config), spec, pick=pick or None, replace=replace,
                         workspace=workspace, by=by, via=via)


def body_limit(config: "Config") -> int:
    """The largest import request body: a base64 zip of `max_archive_mb` plus the JSON around it."""
    return Limits.from_config(config).zip_b64_max + 64 * 1024


async def read_import_body(request: Any, config: "Config") -> dict:
    """The JSON body of an import request (a Starlette request), capped by `body_limit` (413 from Content-Length
    or while streaming); 415 for a body that is not JSON, 400 for JSON that is not an object."""
    limit = body_limit(config)
    content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise SkillError("Send the request as application/json.", 415)
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > limit:
        raise SkillError(f"The request is larger than the zip limit ({config.skills.max_archive_mb} MB).", 413)
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            raise SkillError(f"The request is larger than the zip limit ({config.skills.max_archive_mb} MB).", 413)
    try:
        body = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SkillError("The request body is not valid JSON.") from None
    if not isinstance(body, dict):
        raise SkillError("The request must be a JSON object.")
    return body
