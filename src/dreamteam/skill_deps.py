"""Script dependencies of skills (docs/SPEC.md §19 "Script dependencies"): a skill's `requirements.txt`,
`pyproject.toml` or `package.json` is installed into an environment of its own under ~/.dreamteam/skill-envs, one per
skill and dependency set, shared by every session that attaches the same skill with the same manifests.

`detect` reads the manifests (pure: no process). `EnvManager.plan` turns them into specs, probing the interpreter and
the installer once per process. `ensure` builds in daemon threads under OS file locks (one build per env across
processes), `peek` reports a state without blocking, `use` holds the shared in-use locks that keep cleanup and a
reinstall away from an env a running session uses, `view` gives one session its own hardlink copy of a Node env's
`node_modules`, and `cleanup` removes what nobody used for a while. Only the dependencies a skill lists are installed:
never the skill itself, and never from an index the skill names.

Everything here is blocking file and process work, except `ensure` and the subscriptions; async callers use
`procs.daemon_call`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import hashlib
import json
import logging
import os
import platform
import posixpath
import re
import secrets as _secrets
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable, Iterator
from urllib.parse import urlsplit

from . import skills
from .procs import ProcessTree
from .shell import scrubbed_env
from .skills import SkillError
from .workspace import is_link, long_path, rmtree

if TYPE_CHECKING:
    from .config import Config, SkillsSettings

log = logging.getLogger(__name__)

_WINDOWS = sys.platform == "win32"
MANIFEST_MAX = 256 * 1024        # bytes of one requirements.txt / pyproject.toml / package.json
LOCK_MAX = 8 * 2 ** 20           # bytes of an npm lockfile (a large dependency tree makes a large lock)
PY_FILES = ("requirements.txt", "pyproject.toml", "scripts/requirements.txt", "scripts/pyproject.toml")
NODE_FILES = ("package.json", "scripts/package.json")
INCLUDE_DEPTH = 5                # -r / -c nesting
REQUIREMENT_LINES_MAX = 1000
PACKAGES_MAX = 200               # names kept for the previews and the verification
COMMANDS_MAX = 50
KEY_VERSION = 1
DIRNAME_RE = re.compile(r"^[a-z0-9-]{1,24}-(py|js)-[0-9a-f]{12}$")
INSTALL_PARALLEL = 2             # builds at a time per process
RETRY_FAILED_SECONDS = 3600      # a failed build is not retried automatically before this (a reinstall is)
ENV_TOUCH_SECONDS = 3600         # running sessions touch their envs' `used` stamps this often
VERIFY_SECONDS = 60.0            # peek re-checks a ready env (stats) at most this often
VENV_TIMEOUT = 120.0             # the venv command; the install gets [skills] install_timeout
BUILD_WAIT_EXTRA = 120.0         # beyond install_timeout: how long a build waits for another process's build lock
LOCK_POLL = 0.2
USE_WAIT = 10.0                  # how long `use` retries a shared lock an exclusive holder (cleanup) has
LOG_MAX = 2 * 2 ** 20            # bytes of install.log
ERROR_MAX = 300
VIEW_STALE_SECONDS = 86400       # a session's Node view unused this long is removed by cleanup
FAILED_STALE_SECONDS = 86400     # a failed or half-built env older than this is removed by cleanup
TRASH_STALE_SECONDS = 3600
LONG_PATH_LIMIT = 260
# values that would let a log show an installer's credentials (redact)
SECRET_VARS = ("PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_INDEX",
               "npm_config_registry", "NPM_TOKEN")
# inherited variables that would point an installer at another environment or interpreter
_DROPPED_ENV = ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PIP_REQUIRE_VIRTUALENV", "PIP_USER",
                "UV_PYTHON", "CONDA_PREFIX")
_USER_FROM = ("library", "claude-user", "host-library")
_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?")


# ---- detection ---------------------------------------------------------------------------------------------------
class _Refused(Exception):
    """A manifest DreamTeam does not install automatically; the text names the file, the line and the rule
    (English, without parentheses: it is shown inside one)."""


@dataclass(frozen=True)
class PyManifest:
    files: tuple[str, ...]                     # the manifests used, in install order (posix, relative to the skill)
    includes: tuple[str, ...] = ()             # -r / -c targets reached from them
    material: tuple[tuple[str, str], ...] = ()  # (rel, sha256) of everything the key covers
    packages: tuple[str, ...] = ()             # normalised names (PEP 503)
    hashed: bool = False                       # every requirement carries --hash: installers in require-hashes mode
    requires_python: str | None = None
    has_pyproject: bool = False
    unsupported: str | None = None
    args: tuple[str, ...] = ()                 # the files under src/ passed with -r, in order
    generated: tuple[tuple[str, str], ...] = ()  # (name under src/, text): the requirements of a pyproject.toml
    copies: tuple[str, ...] = ()               # skill files copied into src/ (requirement files and includes)


@dataclass(frozen=True)
class NodeManifest:
    dir: str                                   # "" (the root) or "scripts": where package.json is, and node_modules
    files: tuple[str, ...]
    lock: str | None = None                    # "package-lock.json" | "npm-shrinkwrap.json" | None
    material: tuple[tuple[str, str], ...] = ()
    packages: tuple[str, ...] = ()             # names in dependencies (not the optional ones)
    unsupported: str | None = None
    ignored: tuple[str, ...] = ()
    generated: str = ""                        # the package.json the env gets (no scripts, no devDependencies...)


@dataclass(frozen=True)
class Detected:
    python: PyManifest | None
    node: NodeManifest | None


def _sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def _canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _path(root: Path, rel: str) -> Path:
    return Path(root, *rel.split("/"))


def _exists(root: Path, rel: str) -> bool:
    return os.path.lexists(long_path(_path(root, rel)))


def _read(root: Path, rel: str, cap: int = MANIFEST_MAX) -> bytes:
    """A manifest's bytes: a regular file (never a link) of at most `cap` bytes."""
    path = _path(root, rel)
    try:
        st = os.lstat(long_path(path))
    except FileNotFoundError:
        raise _Refused(f"{rel} does not exist") from None
    except OSError as exc:
        raise _Refused(f"{rel} cannot be read: {exc.strerror or exc}") from None
    if is_link(path) or not stat.S_ISREG(st.st_mode):
        raise _Refused(f"{rel} is a link or not a regular file")
    if st.st_size > cap:
        raise _Refused(f"{rel} is larger than {cap // 1024} KiB")
    try:
        with open(long_path(path), "rb") as f:
            return f.read(cap + 1)
    except OSError as exc:
        raise _Refused(f"{rel} cannot be read: {exc.strerror or exc}") from None


def _text(data: bytes, rel: str) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise _Refused(f"{rel} is not UTF-8 text") from None


_COMMENT = re.compile(r"(?:^|\s+)#.*$")
_REQ_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_DIRECT = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*@\s*(\S+)(.*)\Z", re.S)
_LOCAL = re.compile(r"(?:\.|/|~|\\|[A-Za-z]:[\\/])")
_ARCHIVE = re.compile(r"\.(?:whl|tar\.gz|tgz|tar\.bz2|zip)\Z", re.I)
_PINNED = re.compile(r"(?:\[[^\]]*\])?\s*===?\s*[^\s,;]+\s*")
_INDEX_OPTIONS = frozenset({"-i", "--index-url", "--extra-index-url", "-f", "--find-links", "--trusted-host",
                            "--no-index"})
_GLOBAL_OK = frozenset({"--pre", "--prefer-binary", "--require-hashes", "--only-binary"})


def _url(url: str, where: str) -> None:
    lower = url.lower()
    if lower.startswith(("https://", "git+https://")):
        if "@" in urlsplit(url[4:] if lower.startswith("git+") else url).netloc:
            raise _Refused(f"{where}: a URL with credentials is not supported")
        return
    if lower.startswith(("http://", "git+http://")):
        raise _Refused(f"{where}: http URLs are not supported, use https")
    if lower.startswith("file:"):
        raise _Refused(f"{where}: file: URLs are not supported")
    scheme = lower.split(":", 1)[0]
    raise _Refused(f"{where}: {scheme} URLs are not supported; only https and git+https are installed automatically")


def _requirement(text: str, where: str) -> dict:
    """One requirement (a requirements.txt line without its options, or a pyproject dependency): its normalised
    name, whether it is pinned with == (or a direct URL)."""
    if "${" in text:
        raise _Refused(f"{where}: environment variables in requirements are not supported")
    text = text.strip()
    direct = _DIRECT.match(text)
    if direct is not None:
        _url(direct.group(3), where)
        return {"name": _normalise(direct.group(1)), "pinned": True, "direct": True}
    head = text.partition(";")[0].strip()
    if head.lower().startswith("file:"):
        raise _Refused(f"{where}: file: paths are not supported")
    if _LOCAL.match(head) or (_ARCHIVE.search(head) and "://" not in head):
        raise _Refused(f"{where}: a local path is not supported; a skill names packages, not files")
    if "://" in head:
        raise _Refused(f"{where}: write a URL requirement as name @ URL")
    match = _REQ_NAME.match(head)
    if match is None:
        raise _Refused(f"{where}: not a requirement")
    rest = head[match.end():]
    return {"name": _normalise(match.group(0)), "pinned": bool(_PINNED.fullmatch(rest)), "direct": False}


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """(first physical line number, text) of every logical line: a trailing backslash joins, comments go."""
    out: list[tuple[int, str]] = []
    buf, start = "", 0
    for number, raw in enumerate(text.splitlines(), 1):
        if not buf:
            start = number
        if raw.endswith("\\"):
            buf += raw[:-1]
            continue
        out.append((start, _COMMENT.sub("", buf + raw).strip()))
        buf = ""
    if buf:
        out.append((start, _COMMENT.sub("", buf).strip()))
    return out


@dataclass
class _ReqFiles:
    lines: list[dict] = field(default_factory=list)        # {rel, line, name, pinned, direct, hashed, kind}
    includes: list[str] = field(default_factory=list)
    material: list[tuple[str, str]] = field(default_factory=list)
    require_hashes: bool = False


def parse_requirements(root: Path, rel: str, depth: int = 0, out: _ReqFiles | None = None,
                       kind: str = "req") -> _ReqFiles:
    """Read `rel` (a requirements file of the skill at `root`) and its -r / -c includes by the rules of
    docs/SPEC.md §19; raises _Refused with the file, the line and the rule."""
    out = out if out is not None else _ReqFiles()
    if rel in {r for r, _h in out.material}:
        return out   # an include cycle: each file is read once
    data = _read(root, rel)
    out.material.append((rel, _sha(data)))
    folder = posixpath.dirname(rel)
    for number, line in _logical_lines(_text(data, rel)):
        if not line:
            continue
        where = f"{rel} line {number}"
        if "${" in line:
            raise _Refused(f"{where}: environment variables in requirements are not supported")
        if line.startswith("-"):
            tokens = line.split()
            option, _eq, value = tokens[0].partition("=")
            if option[:2] in ("-r", "-c", "-e", "-i", "-f") and len(option) > 2 and not option.startswith("--"):
                option, value = option[:2], option[2:]
            value = value or (tokens[1] if len(tokens) > 1 else "")
            if option in ("-r", "--requirement", "-c", "--constraint"):
                _include(root, folder, value, where, depth, out, "constraint" if option in ("-c", "--constraint")
                         else kind)
            elif option in ("-e", "--editable"):
                raise _Refused(f"{where}: -e, an editable install, is not supported")
            elif option in _INDEX_OPTIONS:
                raise _Refused(f"{where}: {option}: a skill cannot choose the package index")
            elif option in _GLOBAL_OK:
                out.require_hashes = out.require_hashes or option == "--require-hashes"
            else:
                raise _Refused(f"{where}: the option {option} is not supported")
            continue
        tokens = line.split()
        cut = next((i for i, token in enumerate(tokens) if token.startswith("--")), len(tokens))
        hashed = False
        i = cut
        while i < len(tokens):
            option, _eq, value = tokens[i].partition("=")
            if option != "--hash":
                raise _Refused(f"{where}: the option {option} is not supported")
            if not value:
                i += 1
            hashed = True
            i += 1
        requirement = _requirement(" ".join(tokens[:cut]), where)
        out.lines.append({"rel": rel, "line": number, "kind": kind, "hashed": hashed, **requirement})
        if len(out.lines) > REQUIREMENT_LINES_MAX:
            raise _Refused(f"the requirement files have more than {REQUIREMENT_LINES_MAX} lines")
    return out


def _include(root: Path, folder: str, value: str, where: str, depth: int, out: _ReqFiles, kind: str) -> None:
    target = value.strip().replace("\\", "/")
    if not target:
        raise _Refused(f"{where}: an include without a file")
    if "://" in target or target.startswith(("/", "~")) or re.match(r"[A-Za-z]:", target):
        raise _Refused(f"{where}: an include must be a file inside the skill")
    rel = posixpath.normpath(posixpath.join(folder, target))
    if rel == ".." or rel.startswith("../") or rel == ".":
        raise _Refused(f"{where}: an include outside the skill is not supported")
    if depth + 1 > INCLUDE_DEPTH:
        raise _Refused(f"{where}: includes are nested more than {INCLUDE_DEPTH} deep")
    if rel not in out.includes:
        out.includes.append(rel)
    parse_requirements(root, rel, depth + 1, out, kind)


def parse_pyproject(root: Path, rel: str) -> tuple[list[str], str | None, str]:
    """([project].dependencies, [project].requires-python, the canonical JSON the key covers); raises _Refused."""
    try:
        data = tomllib.loads(_text(_read(root, rel), rel))
    except tomllib.TOMLDecodeError:
        raise _Refused(f"{rel} is not valid TOML") from None
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    dynamic = project.get("dynamic") or []
    if isinstance(dynamic, list) and "dependencies" in dynamic:
        raise _Refused(f"{rel}: the dependencies are dynamic")
    deps = project.get("dependencies")
    if deps is None:
        tool = data.get("tool") if isinstance(data.get("tool"), dict) else {}
        poetry = tool.get("poetry") if isinstance(tool.get("poetry"), dict) else {}
        if poetry.get("dependencies") or tool.get("pdm"):
            raise _Refused(f"{rel}: Poetry or PDM dependencies are not supported")
        deps = []
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        raise _Refused(f"{rel}: [project].dependencies must be a list of strings")
    for i, dep in enumerate(deps, 1):
        if dep.strip().startswith("-"):
            raise _Refused(f"{rel} dependency {i}: options are not allowed")
        _requirement(dep, f"{rel} dependency {i}")
    requires = project.get("requires-python")
    requires = requires.strip() if isinstance(requires, str) and requires.strip() else None
    deps = [d.strip() for d in deps if d.strip()]
    return deps, requires, _canonical({"dependencies": deps, "requires-python": requires})


def _detect_python(root: Path) -> PyManifest | None:
    found = [rel for rel in PY_FILES if _exists(root, rel)]
    if not found:
        return None
    files: list[str] = []
    args: list[str] = []
    copies: list[str] = []
    generated: list[tuple[str, str]] = []
    material: list[tuple[str, str]] = []
    includes: list[str] = []
    lines: list[dict] = []
    hash_files: list[str] = []
    pyprojects: list[str] = []
    requires_python = None
    try:
        for rel in found:
            if rel.endswith("pyproject.toml"):
                deps, requires, canonical = parse_pyproject(root, rel)
                if not deps:
                    continue
                pyprojects.append(rel)
                name = f"pyproject-deps-{len(pyprojects)}.txt"
                generated.append((name, "\n".join(deps) + "\n"))
                args.append(name)
                files.append(rel)
                material.append((f"{rel}#deps", _sha(canonical)))
                requires_python = requires_python or requires
                lines += [{"rel": rel, "line": i, "kind": "req", "hashed": False,
                           **_requirement(d, f"{rel} dependency {i}")} for i, d in enumerate(deps, 1)]
                continue
            parsed = parse_requirements(root, rel)
            if not any(line["kind"] == "req" for line in parsed.lines):
                continue
            files.append(rel)
            args.append(rel)
            copies += [rel] + [i for i in parsed.includes if i not in copies]
            includes += [i for i in parsed.includes if i not in includes]
            material += [m for m in parsed.material if m not in material]
            lines += parsed.lines
            if parsed.require_hashes or any(line["hashed"] for line in parsed.lines):
                hash_files.append(rel)
        if not files:
            return None
        hashed = bool(hash_files)
        if hashed:
            first = hash_files[0]
            if pyprojects:
                raise _Refused(f"{first} uses --hash, but {pyprojects[0]} lists unhashed dependencies")
            for line in lines:
                if line["kind"] != "req":
                    continue
                if not line["hashed"]:
                    raise _Refused(f"{first} uses --hash, but {line['rel']} line {line['line']} for {line['name']} "
                                   "has none: pip would refuse the mix")
                if not line["pinned"]:
                    raise _Refused(f"{line['rel']} line {line['line']} for {line['name']} has --hash but is not "
                                   "pinned with ==")
    except _Refused as exc:
        return PyManifest(files=tuple(found), unsupported=str(exc),
                          has_pyproject=any(r.endswith("pyproject.toml") for r in found))
    packages = list(dict.fromkeys(line["name"] for line in lines if line["kind"] == "req"))[:PACKAGES_MAX]
    return PyManifest(files=tuple(files), includes=tuple(includes), material=tuple(material),
                      packages=tuple(packages), hashed=hashed, requires_python=requires_python,
                      has_pyproject=bool(pyprojects), args=tuple(args), generated=tuple(generated),
                      copies=tuple(copies))


_NODE_LOCAL = ("file:", "link:", "workspace:", "portal:", "git+file:")
_NODE_SCHEME = re.compile(r"[a-z][a-z0-9+.-]*:", re.I)
_URL_USERINFO = re.compile(r"://[^/@\s]*@")


def _node_spec(value: Any, where: str) -> None:
    """An allowlist, like _url: semver ranges, tags, `npm:` aliases, https tarballs, git+https and `github:`
    specs; any other scheme (git:, ssh:, git+ssh:, ftp:...) fetches unencrypted or with the hub's own keys."""
    if isinstance(value, dict):   # overrides nest
        for key, inner in value.items():
            _node_spec(inner, f"{where}.{key}")
        return
    if not isinstance(value, str):
        raise _Refused(f"{where} is not a version string")
    spec = value.strip()
    lower = spec.lower()
    if lower.startswith(_NODE_LOCAL) or _LOCAL.match(spec):
        raise _Refused(f"{where} = {spec[:80]}: local paths, file:, link:, workspace: and portal: packages are not "
                       "installed automatically")
    if lower.startswith("npm:"):   # npm:name@spec, npm:@scope/name@spec: the aliased spec is checked too
        at = spec.find("@", 5)
        if at > 0:
            _node_spec(spec[at + 1:], where)
        return
    if lower.startswith(("https://", "git+https://")):
        if _URL_USERINFO.search(spec):
            raise _Refused(f"{where} = {spec[:80]}: a URL with credentials is not supported")
        return
    if lower.startswith("github:") or not _NODE_SCHEME.match(spec):
        return
    if lower.startswith(("http://", "git+http://")):
        raise _Refused(f"{where} = {spec[:80]}: http URLs are not supported, use https")
    scheme = lower.split(":", 1)[0]
    raise _Refused(f"{where} = {spec[:80]}: {scheme} URLs are not supported; only https and git+https are installed "
                   "automatically")


def parse_package_json(data: bytes, rel: str, lock: bool) -> tuple[dict, str, list[str]]:
    """(the dependency fields kept, the generated package.json, the dependency names); raises _Refused."""
    try:
        doc = json.loads(_text(data, rel))
    except ValueError:
        raise _Refused(f"{rel} is not valid JSON") from None
    if not isinstance(doc, dict):
        raise _Refused(f"{rel} is not a JSON object")
    if "workspaces" in doc:
        raise _Refused(f"{rel}: workspaces are not supported")
    fields = ["dependencies", "optionalDependencies", "overrides"]
    if lock:   # npm ci compares these with the lock
        fields += ["devDependencies", "peerDependencies"]
    kept: dict[str, Any] = {}
    for name in fields:
        value = doc.get(name)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise _Refused(f"{rel}: {name} is not an object")
        for package, spec in value.items():
            _node_spec(spec, f"{rel} {name}.{package}")
        if value:
            kept[name] = value
    names = list(kept.get("dependencies", {}))
    if not kept.get("dependencies") and not kept.get("optionalDependencies"):
        return kept, "", []
    if lock:
        out = {k: doc[k] for k in ("name", "version") if isinstance(doc.get(k), str)}
        out.update(kept)
    else:
        out = {"name": "dreamteam-skill-env", "private": True, **kept}
    return kept, _canonical(out), names


def check_lock(lock: Any, rel: str) -> str | None:
    """Why an npm lockfile is not installed automatically, or None: lockfileVersion 2 or 3, no link to a local
    folder, every resolved URL https (npm fetches each package from it)."""
    if not isinstance(lock, dict):
        return f"{rel} is not a JSON object"
    version = lock.get("lockfileVersion")
    if version not in (2, 3):
        if version == 1:
            return f"{rel}: lockfileVersion 1 of npm 6 is not supported; regenerate it with npm 7 or later"
        return f"{rel}: lockfileVersion {version!r} is not supported"
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        return f"{rel} has no packages section"
    for path, entry in packages.items():
        if not isinstance(entry, dict) or not path:
            continue
        if entry.get("link") is True:
            return f"{rel}: {path} is a link to a local folder"
        resolved = entry.get("resolved")
        if resolved is None:
            continue
        if not isinstance(resolved, str) or not resolved.lower().startswith(("https://", "git+https://")):
            return f"{rel}: {path} resolves to {str(resolved)[:80]}; only https is installed automatically"
        if _URL_USERINFO.search(resolved):
            return f"{rel}: {path} resolves to a URL with credentials"
    return None


def _detect_node(root: Path) -> NodeManifest | None:
    present = [rel for rel in NODE_FILES if _exists(root, rel)]
    if not present:
        return None
    rel = present[0]
    ignored = [f"{other} — only one package.json per skill is installed" for other in present[1:]]
    folder = posixpath.dirname(rel)

    def at(name: str) -> str:
        return f"{folder}/{name}" if folder else name

    lock_name = next((n for n in ("npm-shrinkwrap.json", "package-lock.json") if _exists(root, at(n))), None)
    if lock_name is None:
        ignored += [f"{at(n)} — npm does not read it" for n in ("yarn.lock", "pnpm-lock.yaml") if _exists(root, at(n))]
    files = (rel,) + ((at(lock_name),) if lock_name else ())
    try:
        data = _read(root, rel)
        _kept, generated, names = parse_package_json(data, rel, lock_name is not None)
        if not generated:
            return None
        material = [(f"{rel}#generated", _sha(generated))]
        if lock_name:
            lock_rel = at(lock_name)
            lock_bytes = _read(root, lock_rel, LOCK_MAX)
            try:
                lock_doc = json.loads(_text(lock_bytes, lock_rel))
            except ValueError:
                raise _Refused(f"{lock_rel} is not valid JSON") from None
            error = check_lock(lock_doc, lock_rel)
            if error:
                raise _Refused(error)
            material.append((lock_rel, _sha(lock_bytes)))
    except _Refused as exc:
        return NodeManifest(dir=folder, files=files, lock=lock_name, unsupported=str(exc), ignored=tuple(ignored))
    return NodeManifest(dir=folder, files=files, lock=lock_name, material=tuple(material),
                        packages=tuple(names[:PACKAGES_MAX]), ignored=tuple(ignored), generated=generated)


def detect(folder: Path) -> Detected:
    """The dependency manifests of the skill folder `folder` (pure file reading: no process is started)."""
    folder = Path(folder)
    return Detected(python=_detect_python(folder), node=_detect_node(folder))


_SPEC = re.compile(r"\s*(~=|===|==|!=|<=|>=|<|>)\s*([0-9]+(?:\.[0-9]+)*(?:\.\*)?)\s*")


def python_satisfies(spec: str, version: str) -> bool | None:
    """Whether Python `version` ("3.11.15") meets `requires-python` (`>=3.10,<4`, `~=3.11`, `==3.*`); None for a
    specifier this small parser does not know (it is then ignored)."""
    try:
        have = tuple(int(p) for p in version.split(".")[:3])
    except ValueError:
        return None
    for clause in spec.split(","):
        if not clause.strip():
            continue
        m = _SPEC.fullmatch(clause)
        if m is None:
            return None
        op, text = m.groups()
        wild = text.endswith(".*")
        try:
            want = tuple(int(p) for p in text.removesuffix(".*").split("."))
        except ValueError:
            return None
        if wild and op not in ("==", "!="):
            return None
        if wild:
            ok = have[:len(want)] == want
            if not (ok if op == "==" else not ok):
                return False
            continue
        size = max(len(want), len(have))
        a, b = have + (0,) * (size - len(have)), want + (0,) * (size - len(want))
        if op == "~=":
            if len(want) < 2 or not (a >= b and have[:len(want) - 1] == want[:-1]):
                return False
        elif not {"==": a == b, "===": a == b, "!=": a != b, "<=": a <= b, ">=": a >= b, "<": a < b,
                  ">": a > b}[op]:
            return False
    return True


# ---- tools -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, eq=False)
class ToolInfo:
    """A probed interpreter (or node) and its installer."""

    ecosystem: str
    path: str                                   # the interpreter, or the real node binary
    version: str                                # "3.11.15" / "24.16.0"
    key_fields: dict = field(default_factory=dict)
    installer: str = ""                         # "uv" | "pip" | "pip-venv" | "npm" (or a test double's)
    label: str = ""                             # "uv 0.12.23": env.json and the prefix of a failure
    extra: dict = field(default_factory=dict)   # {"uv": path} / {"npm_cli": path}


def os_marker() -> str:
    """`ID-VERSION_ID` of /etc/os-release on Linux ("debian-13"), "" elsewhere: a source build against an old
    system library is never reused after a distribution upgrade."""
    if not sys.platform.startswith("linux"):
        return ""
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            values = dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)
    except OSError:
        return ""
    return "-".join(values.get(k, "").strip('"') for k in ("ID", "VERSION_ID")).strip("-")


def _no_window() -> dict[str, Any]:
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if _WINDOWS else {}


def _run_quiet(argv: list[str], env: dict[str, str], cwd: Path, timeout: float = 30.0) -> str | None:
    try:
        done = subprocess.run(argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=timeout, **_no_window())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", errors="replace").strip()


_PY_PROBE = ("import json,os,platform,sys,sysconfig;"
             "print(json.dumps({'impl':sys.implementation.name,'minor':'%d.%d'%sys.version_info[:2],"
             "'version':platform.python_version(),'platform':sysconfig.get_platform(),"
             "'cache_tag':sys.implementation.cache_tag,'abiflags':getattr(sys,'abiflags',''),"
             "'base':os.path.normcase(getattr(sys,'_base_executable',None) or sys.executable),"
             "'libc':'-'.join(platform.libc_ver())}))")
_NODE_PROBE = ("JSON.stringify([process.execPath,process.versions.node,process.versions.modules,process.platform,"
               "process.arch])")
_PROBES: dict[tuple, tuple[Any, float | None]] = {}   # key -> (value, when a failure expires; None: never)
_PROBES_LOCK = threading.Lock()
PROBE_RETRY_SECONDS = 300.0   # a failed probe (a timeout on a cold start, an antivirus scan) is run again after this


def _cached(kind: str, path: str, compute: Callable[[], Any]) -> Any:
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        return None
    key = (kind, os.path.normcase(path), stamp)
    with _PROBES_LOCK:
        hit = _PROBES.get(key)
        if hit is not None and (hit[1] is None or time.monotonic() < hit[1]):
            return hit[0]
    value = compute()
    failed = value is None or value is False
    with _PROBES_LOCK:
        _PROBES[key] = (value, time.monotonic() + PROBE_RETRY_SECONDS if failed else None)
    return value


def forget_failed_probes() -> None:
    """The user's skill_reinstall: a tool that did not answer is probed again at once."""
    with _PROBES_LOCK:
        for key in [k for k, (_, expires) in _PROBES.items() if expires is not None]:
            del _PROBES[key]


def probe_python(path: str, env: dict[str, str], cwd: Path) -> dict | None:
    """The key fields of the interpreter `path`, evaluated inside it (cached per process by path and mtime)."""
    def compute() -> dict | None:
        out = _run_quiet([path, "-I", "-c", _PY_PROBE], env, cwd)
        try:
            data = json.loads(out or "")
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        data["os"] = os_marker()
        return data
    return _cached("python", path, compute)


def probe_node(path: str, env: dict[str, str], cwd: Path) -> list | None:
    def compute() -> list | None:
        out = _run_quiet([path, "-p", _NODE_PROBE], env, cwd)
        try:
            data = json.loads(out or "")
        except ValueError:
            return None
        return data if isinstance(data, list) and len(data) == 5 else None
    return _cached("node", path, compute)


def _which(name: str, env: dict[str, str]) -> str | None:
    return shutil.which(name, path=env.get("PATH") or env.get("Path") or None)


def _uv(env: dict[str, str]) -> str | None:
    uv = _which("uv", env)
    if uv and _WINDOWS and not uv.lower().endswith(".exe"):   # a .cmd / .bat would go through cmd.exe
        return None
    return uv


def resolve_python(config: "Config", env: dict[str, str], cwd: Path) -> ToolInfo | dict:
    """The interpreter of [skills] python ("": the base interpreter DreamTeam runs on), probed; an `unavailable`
    reason when it is missing or does not answer."""
    value = (config.skills.python or "").strip()
    missing = {"code": "unavailable", "detail": f"Python '{value}' of [skills] python" if value else
               "the Python DreamTeam runs on"}
    if not value:
        path = getattr(sys, "_base_executable", None) or sys.executable
    elif _VERSION.fullmatch(value) and _uv(env):
        path = _run_quiet([_uv(env), "python", "find", value], env, cwd, timeout=60.0) or ""
    elif "/" in value or "\\" in value:
        path = os.path.expanduser(value)
    else:
        path = _which(value, env) or ""
    if not path or not os.path.isfile(path):
        return missing
    data = probe_python(path, env, cwd)
    if data is None:
        return missing
    keys = ("impl", "minor", "platform", "cache_tag", "abiflags", "base", "libc", "os")
    return ToolInfo("python", path, str(data.get("version") or ""), {k: data.get(k) for k in keys})


def pick_installer(py: ToolInfo, env: dict[str, str], cwd: Path) -> ToolInfo | dict:
    """uv when there is one, else the interpreter's pip (>= 22.3, probed with the installers' environment), else
    the pip a venv's ensurepip brings."""
    uv = _uv(env)
    if uv:
        label = _cached("uv", uv, lambda: _run_quiet([uv, "--version"], env, cwd)) or "uv"
        return ToolInfo(py.ecosystem, py.path, py.version, py.key_fields, "uv", " ".join(label.split()[:2]),
                        {"uv": uv})
    pip = _cached("pip", py.path, lambda: _run_quiet([py.path, "-m", "pip", "--version"], env, cwd)) or ""
    m = re.match(r"pip (\d+)\.(\d+)", pip)
    if m and (int(m.group(1)), int(m.group(2))) >= (22, 3):
        return ToolInfo(py.ecosystem, py.path, py.version, py.key_fields, "pip", f"pip {m.group(1)}.{m.group(2)}")
    if _cached("ensurepip", py.path, lambda: _run_quiet([py.path, "-c", "import ensurepip, venv"], env, cwd)
               is not None):
        return ToolInfo(py.ecosystem, py.path, py.version, py.key_fields, "pip-venv", "pip")
    return {"code": "unavailable", "detail": "uv or pip"}


def _npm_cli(exec_path: str, env: dict[str, str]) -> str | None:
    here = Path(exec_path).parent
    for candidate in (here / "node_modules" / "npm" / "bin" / "npm-cli.js",
                      here.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"):
        if candidate.is_file():
            return str(candidate)
    if not _WINDOWS:
        npm = _which("npm", env)
        if npm:
            real = os.path.realpath(npm)
            if real.endswith("npm-cli.js") and os.path.isfile(real):
                return real
    return None


def resolve_node(env: dict[str, str], cwd: Path) -> ToolInfo | dict:
    """The real node binary behind any version-manager shim (process.execPath), and npm-cli.js beside it. On
    Windows only node.exe is looked for: a node.cmd shim would run through cmd.exe."""
    node = _which("node.exe" if _WINDOWS else "node", env)
    if not node:
        return {"code": "unavailable", "detail": "Node.js"}
    data = probe_node(node, env, cwd)
    if data is None:
        return {"code": "unavailable", "detail": "Node.js"}
    exec_path, version, modules, plat, arch = (str(v) for v in data)
    cli = _npm_cli(exec_path, env)
    if cli is None:
        return {"code": "unavailable", "detail": "npm, its npm-cli.js beside node"}
    major = int(version.split(".")[0]) if version.split(".")[0].isdigit() else 0
    return ToolInfo("node", exec_path, version, {"major": major, "abi": modules, "platform": plat, "arch": arch,
                                                 "os": os_marker()}, "npm", "npm", {"npm_cli": cli})


class RealInstallers:
    """The interpreters and commands DreamTeam uses (tests replace the module's INSTALLERS with a double)."""

    def python_tool(self, config: "Config", env: dict[str, str], cwd: Path) -> ToolInfo | dict:
        py = resolve_python(config, env, cwd)
        return py if isinstance(py, dict) else pick_installer(py, env, cwd)

    def node_tool(self, config: "Config", env: dict[str, str], cwd: Path) -> ToolInfo | dict:
        return resolve_node(env, cwd)

    def python_commands(self, spec: "EnvSpec", env_dir: Path) -> list[tuple[list[str], str]]:
        tool, venv = spec.tool, str(env_dir / "venv")
        python = venv_python(env_dir)
        reqs = [a for f in spec.manifest.args for a in ("-r", f"src/{f}")]
        if tool.installer == "uv":
            uv = tool.extra["uv"]
            return [([uv, "venv", "--python", tool.path, venv], "venv"),
                    ([uv, "pip", "install", "--python", python]
                     + (["--require-hashes"] if spec.manifest.hashed else []) + reqs, "install")]
        if tool.installer == "pip":
            return [([tool.path, "-m", "venv", "--without-pip", venv], "venv"),
                    ([tool.path, "-m", "pip", "--python", python, "install", "--disable-pip-version-check",
                      "--no-input"] + reqs, "install")]
        return [([tool.path, "-m", "venv", venv], "venv"),
                ([python, "-m", "pip", "install", "--disable-pip-version-check", "--no-input"] + reqs, "install")]

    def node_commands(self, spec: "EnvSpec", env_dir: Path) -> list[tuple[list[str], str]]:
        tool = spec.tool
        argv = [tool.path, tool.extra["npm_cli"], "ci" if spec.manifest.lock else "install", "--omit=dev",
                "--no-audit", "--no-fund"] + ([] if spec.options.scripts else ["--ignore-scripts"])
        return [(argv, "install")]


INSTALLERS: Any = RealInstallers()


def trust_class(frm: str) -> str:
    """`user` for the user's own sources, `agent` for the agent-writable ones (a member's proposal, the workspace's
    .claude/skills): the two never share an environment, and only `user` installs get [skills] install_env."""
    return "user" if frm in _USER_FROM else "agent"


def installer_env(config: "Config", trust: str) -> dict[str, str]:
    """The installers' environment: the agents' shell environment (scrubbed_env), plus [skills] install_env for the
    user's own skills, without what would point them at another environment."""
    env = scrubbed_env(config)
    if trust == "user":
        for name in config.skills.install_env:
            if name in os.environ:
                env[name] = os.environ[name]
    for name in _DROPPED_ENV:
        env.pop(name, None)
    env.update(PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_INPUT="1", PYTHONNOUSERSITE="1", UV_NO_PROGRESS="1")
    return env


def settings_hash(settings: "SkillsSettings") -> str:
    """The [skills] dependency fields: a plan made under other settings is made again."""
    fields = ("install", "install_proposals", "envs_dir", "python", "install_scripts", "install_timeout",
              "env_retention_days", "install_env")
    return _sha(_canonical({k: getattr(settings, k) for k in fields}))[:16]


# ---- keys and places ---------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class BuildOptions:
    timeout: float = 900.0
    scripts: bool = False
    env: dict = field(default_factory=dict, compare=False)
    secrets: tuple[str, ...] = ()


@dataclass(frozen=True, eq=False)
class EnvSpec:
    skill: str
    ecosystem: str                              # "python" | "node"
    trust: str                                  # "user" | "agent"
    manifest: PyManifest | NodeManifest
    source: Path                                # the folder whose manifests are copied (the session copy)
    material: tuple[tuple[str, str], ...]
    tool: ToolInfo
    installer: str
    key: str
    dirname: str
    options: BuildOptions = field(default_factory=BuildOptions)


def env_key(spec: Any, tool: ToolInfo) -> str:
    """12 hex characters over the skill, the ecosystem, the trust class, the manifests and the tool's identity.
    Not in it: the installer, the npm version, the index configuration and the skill's other files."""
    material = {"v": KEY_VERSION, "eco": spec.ecosystem, "skill": spec.skill, "trust": spec.trust,
                "files": sorted([list(m) for m in spec.material]), "tool": tool.key_fields}
    return _sha(_canonical(material))[:12]


def env_dirname(skill: str, ecosystem: str, key: str) -> str:
    return f"{skill[:24].rstrip('-')}-{'py' if ecosystem == 'python' else 'js'}-{key}"


def owner_id(owner: str) -> str:
    return _sha(owner)[:12]


def venv_python(env_dir: Path) -> str:
    return str(Path(env_dir, "venv", "Scripts", "python.exe") if os.name == "nt" else Path(env_dir, "venv", "bin",
                                                                                         "python"))


def venv_bin(env_dir: Path) -> str:
    return str(Path(env_dir, "venv", "Scripts" if os.name == "nt" else "bin"))


_PS_BARE = re.compile(r"[A-Za-z0-9_.:\\/-]+")


def shell_ready(path: str, shell_name: str) -> str:
    """`path` written so the swarm's shell runs it as a command."""
    if shell_name in ("powershell", "pwsh"):
        return path if _PS_BARE.fullmatch(path) else "& '" + path.replace("'", "''") + "'"
    if shell_name == "cmd":
        return f'"{path}"'
    return shlex.quote(path)


_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    text = _USERINFO.sub(r"\1***@", text)
    for value in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(value, "***")
    return text


def _secret_values(env: dict[str, str], extra: Iterable[str]) -> tuple[str, ...]:
    return tuple(env[n] for n in (*SECRET_VARS, *extra) if env.get(n))


def link_node_modules(skill_dir: Path, rel: str, target: Path | str) -> bool:
    """A link named node_modules in `skill_dir/rel` to `target` (a session's view): a directory junction on Windows
    (no privilege needed), a symlink elsewhere. False when it cannot be made."""
    link = Path(skill_dir, *[p for p in rel.split("/") if p], "node_modules")
    try:
        if _WINDOWS:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            os.symlink(str(target), str(link), target_is_directory=True)
        return True
    except OSError as exc:
        log.warning("cannot link %s to %s: %s", link, target, exc)
        return False


def _plain(path: str) -> str:
    """`path` without the extended-length prefix (long_path)."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    return path[4:] if path.startswith("\\\\?\\") else path


def _strip_long(path: str) -> str:
    return os.path.normcase(os.path.normpath(_plain(path)))


def link_state(link: Path, target: Path | str) -> str:
    """"ok" (a link to `target`), "missing", "wrong" (a link elsewhere, a dangling or unreadable one) or "real" (a
    folder or a file of its own: an agent's npm install replaced the link)."""
    raw = long_path(link)
    if not os.path.lexists(raw):
        return "missing"
    if not is_link(link):
        return "real"
    try:
        pointed = os.readlink(raw)
    except OSError:
        return "wrong"
    if not os.path.exists(raw):
        return "wrong"
    return "ok" if _strip_long(pointed) == _strip_long(str(target)) else "wrong"


def remove_link(link: Path) -> None:
    raw = long_path(link)
    try:
        os.rmdir(raw)   # a junction (Windows), or a directory symlink there
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(raw)


# ---- checks of a built env ---------------------------------------------------------------------------------------
_LONG_PATHS: list[bool] = []


def long_paths_off() -> bool:
    """Windows with LongPathsEnabled = 0: Python cannot open a file whose path has 260 characters or more."""
    if not _WINDOWS:
        return False
    if not _LONG_PATHS:
        value = 0
        with contextlib.suppress(OSError, ImportError):
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as key:
                value = int(winreg.QueryValueEx(key, "LongPathsEnabled")[0])
        _LONG_PATHS.append(value == 0)
    return _LONG_PATHS[0]


def longest_paths(folder: Path, limit: int = LONG_PATH_LIMIT) -> tuple[int, str] | None:
    """(length, path) of the first file below `folder` whose full plain path has `limit` characters or more (walked
    through the extended-length form, never following a link); None when there is none."""
    stack = [long_path(folder)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    plain = _plain(entry.path)
                    if len(plain) >= limit:
                        return len(plain), plain
                    if entry.is_dir(follow_symlinks=False) and not is_link(entry):
                        stack.append(entry.path)
        except OSError:
            continue
    return None


def _site_packages(env_dir: Path) -> list[Path]:
    venv = Path(env_dir, "venv")
    if os.name == "nt":
        return [venv / "Lib" / "site-packages"]
    return sorted(venv.glob("lib/python*/site-packages"))


def _dist_infos(env_dir: Path) -> dict[str, str]:
    """normalised distribution name -> its .dist-info folder name."""
    out: dict[str, str] = {}
    for folder in _site_packages(env_dir):
        try:
            with os.scandir(long_path(folder)) as it:
                for entry in it:
                    if entry.name.endswith(".dist-info") and entry.is_dir(follow_symlinks=False):
                        out[_normalise(entry.name[:-len(".dist-info")].split("-", 1)[0])] = entry.name
        except OSError:
            continue
    return out


def verify_python(env_dir: Path, env_json: dict) -> bool:
    """Cheap (stats only): the venv's python exists and every installed distribution still has its dist-info."""
    if not os.path.isfile(long_path(venv_python(env_dir))):
        return False
    have = _dist_infos(env_dir)
    return all(_normalise(n) in have for n in env_json.get("installed") or [])


def verify_node(env_dir: Path, env_json: dict) -> bool:
    modules = Path(env_dir, "node_modules")
    if not os.path.isfile(long_path(modules / ".package-lock.json")):
        return False
    return all(os.path.isdir(long_path(modules.joinpath(*n.split("/")))) for n in env_json.get("packages") or [])


# The venv's own entries, by exact name: uv writes every shell's activator on every platform (deactivate.bat in a
# POSIX bin/), and a prefix test would drop real commands (uvicorn, pip-compile, pipdeptree).
_VENV_OWN_POSIX = re.compile(r"(?:python|pip)[0-9.]*|activate(?:[._].*)?|.*\.(?:bat|cmd|ps1|csh|fish|nu|xsh)")
_VENV_OWN_NT = re.compile(r"(?:pythonw?|pip)[0-9.]*", re.I)


def _command_names(names: Iterable[str], windows: bool) -> list[str]:
    out = []
    for name in names:
        if windows:
            if name.lower().endswith(".exe") and not _VENV_OWN_NT.fullmatch(name[:-4]):
                out.append(name[:-4])
        elif not _VENV_OWN_POSIX.fullmatch(name):
            out.append(name)
    return sorted(set(out))[:COMMANDS_MAX]


def _python_commands(env_dir: Path) -> list[str]:
    try:
        with os.scandir(long_path(Path(venv_bin(env_dir)))) as it:
            return _command_names([entry.name for entry in it], os.name == "nt")
    except OSError:
        return []


def _node_commands(env_dir: Path) -> list[str]:
    folder = Path(env_dir, "node_modules", ".bin")
    try:
        with os.scandir(long_path(folder)) as it:
            names = {re.sub(r"\.(cmd|ps1)$", "", e.name, flags=re.I) for e in it}
    except OSError:
        return []
    return sorted(names)[:COMMANDS_MAX]


# ---- OS file locks -----------------------------------------------------------------------------------------------
_K32: list[Any] = []


def _kernel32() -> Any:
    if not _K32:
        import ctypes
        from ctypes import wintypes

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                        ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.POINTER(OVERLAPPED)]
        k32.LockFileEx.restype = wintypes.BOOL
        k32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.POINTER(OVERLAPPED)]
        k32.UnlockFileEx.restype = wintypes.BOOL
        _K32.append((k32, OVERLAPPED, ctypes))
    return _K32[0]


def _oslock(fd: int, *, exclusive: bool) -> bool:
    """Lock byte 0 of the open file `fd`, shared or exclusive, without waiting; False when another holder's lock
    is in the way. The OS releases it when the handle is closed or the process dies."""
    if _WINDOWS:
        import msvcrt

        k32, overlapped, ctypes = _kernel32()
        flags = 0x1 | (0x2 if exclusive else 0)   # LOCKFILE_FAIL_IMMEDIATELY | LOCKFILE_EXCLUSIVE_LOCK
        return bool(k32.LockFileEx(msvcrt.get_osfhandle(fd), flags, 0, 1, 0, ctypes.byref(overlapped())))
    import fcntl

    try:
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _open_lock(path: Path) -> int:
    os.makedirs(long_path(path.parent), exist_ok=True)
    return os.open(long_path(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)


def _try_lock(path: Path, *, exclusive: bool = True) -> int | None:
    """An open handle holding the lock, or None (busy)."""
    fd = _open_lock(path)
    if _oslock(fd, exclusive=exclusive):
        return fd
    os.close(fd)
    return None


def _close(fd: int | None) -> None:
    if fd is not None:
        with contextlib.suppress(OSError):
            os.close(fd)


# ---- the manager -------------------------------------------------------------------------------------------------
def _read_json(path: Path) -> dict | None:
    return skills._read_json(path)


def _write_json(path: Path, data: dict) -> None:
    skills._write_json(path, data)


def _mtime(path: Path) -> float | None:
    try:
        return os.stat(long_path(path)).st_mtime
    except OSError:
        return None


def _touch(path: Path) -> None:
    raw = long_path(path)
    try:
        os.utime(raw)
    except FileNotFoundError:
        with contextlib.suppress(OSError):
            with open(raw, "ab"):
                pass
    except OSError:
        pass


def _error_line(lines: list[str], prefix: str) -> str:
    pattern = re.compile(r"^(ERROR|error|npm ERR!|npm error)(?=[\s:!]|$)")
    found = next((ln for ln in lines if pattern.match(ln.strip())), None)
    if found is None:
        found = next((ln for ln in reversed(lines) if ln.strip()), "it ended with an error")
    text = " ".join(found.split())
    return f"{prefix}: {text}"[:ERROR_MAX] if prefix else text[:ERROR_MAX]


class EnvManager:
    """The environments under one envs root, for this process (docs/SPEC.md §19 "Script dependencies")."""

    _instances: dict[str, "EnvManager"] = {}
    _instances_lock = threading.Lock()

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.RLock()
        self._builds: dict[str, concurrent.futures.Future] = {}
        self._mem: dict[str, dict] = {}         # dirname -> a state of a build here, or a failure kept in memory
        self._holders: dict[str, list] = {}     # dirname -> [shared handle | None, owners of this process]
        self._exclusive: dict[str, int] = {}    # dirname -> exclusive takes of this process under way (no shared take)
        self._subs: list[tuple[asyncio.AbstractEventLoop, Callable]] = []
        self._verified: dict[str, tuple[float, Any, bool]] = {}
        self._slots = threading.Semaphore(INSTALL_PARALLEL)
        self._view_locks: dict[tuple[str, str], threading.Lock] = {}

    @classmethod
    def for_config(cls, config: "Config") -> "EnvManager":
        root = os.path.abspath(str(config.skill_envs_dir))
        key = os.path.normcase(root)
        with cls._instances_lock:
            manager = cls._instances.get(key)
            if manager is None:
                manager = cls._instances[key] = cls(Path(root))
            return manager

    # ---- places ----
    def dir(self, dirname: str) -> Path:
        return self.root / dirname

    def _lock_file(self, dirname: str, kind: str) -> Path:
        return self.root / ".locks" / f"{dirname}.{kind}"

    def log_path(self, dirname: str) -> Path:
        return self.dir(dirname) / "install.log"

    # ---- planning ----
    def plan(self, folder: Path, skill: str, trust: str, settings: "SkillsSettings", *, config: "Config",
             proposal: bool = False, network: bool = True) -> list:
        """The specs of the skill folder's manifests: an EnvSpec per installable ecosystem, a state dict for the
        others (off, unsupported, unavailable). Blocking (it may probe a tool once per process): call it in a
        daemon thread. `network` False (a session without network): a member's proposal gets no install, since
        DreamTeam would download what the member could not (an environment that is ready already is used)."""
        found = detect(folder)
        out: list = []
        for ecosystem, manifest in (("python", found.python), ("node", found.node)):
            if manifest is None:
                continue
            base: dict[str, Any] = {"ecosystem": ecosystem, "files": list(manifest.files),
                                    "packages": list(manifest.packages)}
            if isinstance(manifest, PyManifest):
                base["has_pyproject"] = manifest.has_pyproject
            if manifest.unsupported:
                out.append({**base, "status": "unsupported",
                            "reason": {"code": "unsupported", "detail": manifest.unsupported}})
                continue
            if not settings.install:
                out.append({**base, "status": "off", "reason": {"code": "off"}})
                continue
            if proposal and not settings.install_proposals:
                out.append({**base, "status": "off", "reason": {"code": "proposals"}})
                continue
            os.makedirs(long_path(self.root), exist_ok=True)
            env = installer_env(config, trust)
            tool = (INSTALLERS.python_tool if ecosystem == "python" else INSTALLERS.node_tool)(config, env, self.root)
            if isinstance(tool, dict):
                out.append({**base, "status": "unavailable", "reason": tool})
                continue
            if isinstance(manifest, PyManifest) and manifest.requires_python:
                fits = python_satisfies(manifest.requires_python, tool.version)
                if fits is False:
                    out.append({**base, "status": "unavailable",
                                "reason": {"code": "python", "need": manifest.requires_python,
                                           "have": tool.version}})
                    continue
                if fits is None:
                    log.info("skill %s: requires-python %r is not understood; ignored", skill,
                             manifest.requires_python)
            extra = config.skills.install_env if trust == "user" else ()
            options = BuildOptions(timeout=float(settings.install_timeout), scripts=bool(settings.install_scripts),
                                   env=env, secrets=_secret_values(env, extra))
            proto = EnvSpec(skill=skill, ecosystem=ecosystem, trust=trust, manifest=manifest, source=Path(folder),
                            material=manifest.material, tool=tool, installer=tool.label, key="", dirname="",
                            options=options)
            key = env_key(proto, tool)
            spec = EnvSpec(skill=skill, ecosystem=ecosystem, trust=trust, manifest=manifest, source=Path(folder),
                           material=manifest.material, tool=tool, installer=tool.label, key=key,
                           dirname=env_dirname(skill, ecosystem, key), options=options)
            if proposal and not network and self._disk_state(spec.dirname) != "ready":
                out.append({**base, "status": "off", "reason": {"code": "network"}})
                continue
            out.append(spec)
        return out

    # ---- state ----
    def _base(self, spec: EnvSpec) -> dict:
        base = {"ecosystem": spec.ecosystem, "files": list(spec.manifest.files),
                "packages": list(spec.manifest.packages), "env": str(self.dir(spec.dirname)), "key": spec.key}
        if isinstance(spec.manifest, PyManifest):
            base["has_pyproject"] = spec.manifest.has_pyproject
        return base

    def _verify(self, dirname: str, env_json: dict) -> bool:
        path = self.dir(dirname)
        if env_json.get("ecosystem") == "node":
            return verify_node(path, env_json)
        return verify_python(path, env_json)

    def _disk_state(self, dirname: str, cached: bool = False) -> str:
        """ready | broken (ok, but verification fails) | failed | incomplete (a builder died) | absent."""
        path = self.dir(dirname)
        ok = _read_json(path / "ok")
        if ok is not None:
            if cached:
                with self._lock:
                    seen = self._verified.get(dirname)
                if seen is not None and seen[1] == ok.get("at") and time.monotonic() - seen[0] < VERIFY_SECONDS:
                    return "ready" if seen[2] else "broken"
            good = self._verify(dirname, _read_json(path / "env.json") or {})
            with self._lock:
                self._verified[dirname] = (time.monotonic(), ok.get("at"), good)
            return "ready" if good else "broken"
        if os.path.lexists(long_path(path / "failed.json")):
            return "failed"
        return "incomplete" if os.path.lexists(long_path(path)) else "absent"

    def _ready_state(self, spec: EnvSpec) -> dict:
        path = self.dir(spec.dirname)
        env_json = _read_json(path / "env.json") or {}
        state = {**self._base(spec), "status": "ready", "commands": list(env_json.get("commands") or []),
                 "installer": env_json.get("installer"), "seconds": env_json.get("seconds"),
                 "finished_at": env_json.get("finished_at")}
        if spec.ecosystem == "python":
            state["python"] = venv_python(path)
            state["bin"] = venv_bin(path)
        else:
            state["node_modules"] = str(path / "node_modules")
        return state

    def _failed_state(self, spec: EnvSpec) -> dict:
        failed = _read_json(self.dir(spec.dirname) / "failed.json") or {}
        return {**self._base(spec), "status": "failed", "error": failed.get("error") or "the install failed",
                "installer": failed.get("installer"), "log": str(self.log_path(spec.dirname)),
                "finished_at": failed.get("at")}

    def peek(self, spec: Any, owner: str | None = None) -> dict:
        """The state of `spec` now, from memory and a few stats (never blocks on a build). A broken env (ok, but
        a dist-info or a package folder gone) shows as installing, and its rebuild starts."""
        if isinstance(spec, dict):
            return dict(spec)
        with self._lock:
            mem = self._mem.get(spec.dirname)
            holder = self._holders.get(spec.dirname)
        if mem is not None:
            return {**self._base(spec), **{k: v for k, v in mem.items() if k != "memory"}}
        if owner is not None and holder is not None and owner in holder[1] and holder[0] is None:
            if not self._take_shared(spec.dirname, wait=0.0):
                return {**self._base(spec), "status": "installing"}
        disk = self._disk_state(spec.dirname, cached=True)
        if disk == "ready":
            return self._ready_state(spec)
        if disk == "broken":
            self._start(spec, force=False, owner=owner)
            return {**self._base(spec), "status": "installing"}
        if disk == "failed":
            return self._failed_state(spec)
        return {**self._base(spec), "status": "installing"}

    def building(self, dirname: str) -> bool:
        with self._lock:
            future = self._builds.get(dirname)
            return future is not None and not future.done()

    # ---- subscriptions ----
    def subscribe(self, fn: Callable[[str, dict, str], None]) -> Callable[[], None]:
        """`fn(dirname, state, phase)` on the caller's loop for every published state (phase "queued", "waiting",
        "started" or "done"); returns the unsubscribe function."""
        entry = (asyncio.get_running_loop(), fn)
        with self._lock:
            self._subs.append(entry)

        def unsubscribe() -> None:
            with self._lock:
                if entry in self._subs:
                    self._subs.remove(entry)
        return unsubscribe

    def _publish(self, dirname: str, state: dict, phase: str) -> None:
        with self._lock:
            subs = list(self._subs)
        for loop, fn in subs:
            with contextlib.suppress(RuntimeError):   # a closed loop: that session is gone
                loop.call_soon_threadsafe(fn, dirname, dict(state), phase)

    def _changed(self, dirname: str | None, what: str) -> None:
        """Tell subscribers that something changed outside a build (a cleanup: `what` = "skill-envs")."""
        self._publish(dirname or "", {"what": what}, "changed")

    # ---- in use ----
    def _take_shared(self, dirname: str, wait: float) -> bool:
        deadline = time.monotonic() + wait
        while True:
            with self._lock:
                holder = self._holders.get(dirname)
                if holder is None or holder[0] is not None:
                    return holder is not None
                # not while this process's own reinstall or cleanup takes the lock exclusively (it gave its shared
                # handle up for that): a peek or a use() taking one back then would make it fail. Tried under
                # self._lock, which exclusive() marks the env under first (the try does not block)
                if dirname not in self._exclusive:
                    try:
                        fd = _try_lock(self._lock_file(dirname, "use"), exclusive=False)
                    except OSError as exc:
                        log.warning("cannot open the use lock of %s: %s", dirname, exc)
                        return False
                    if fd is not None:
                        holder[0] = fd
                        return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(LOCK_POLL)

    def use(self, owner: str, dirnames: Iterable[str]) -> None:
        """`owner`'s in-use set (it replaces the previous one): this process holds a shared OS lock on every env
        any of its owners uses, so cleanup and another session's reinstall leave it alone. May wait (up to
        USE_WAIT) while a cleanup or a reinstall holds one exclusively: call it in a daemon thread."""
        wanted = set(dirnames)
        closing: list[int] = []
        with self._lock:
            for dirname, holder in list(self._holders.items()):
                if owner in holder[1] and dirname not in wanted:
                    holder[1].discard(owner)
                    if not holder[1]:
                        if holder[0] is not None:
                            closing.append(holder[0])
                        del self._holders[dirname]
            need = []
            for dirname in wanted:
                holder = self._holders.setdefault(dirname, [None, set()])
                holder[1].add(owner)
                if holder[0] is None:
                    need.append(dirname)
        for fd in closing:
            _close(fd)
        for dirname in need:
            if not self._take_shared(dirname, wait=USE_WAIT):
                log.info("the environment %s is held exclusively by another process; retried later", dirname)

    def users(self, dirname: str) -> set[str]:
        with self._lock:
            holder = self._holders.get(dirname)
            return set(holder[1]) if holder else set()

    @contextlib.contextmanager
    def exclusive(self, dirname: str, requester: str | None = None, skill: str = "") -> Iterator[None]:
        """Hold the env's use lock exclusively (a reinstall, a removal, cleanup): refused with 409 while another
        session of this process, or any other process, uses it."""
        skill = skill or dirname.rsplit("-", 2)[0]
        with self._lock:
            holder = self._holders.get(dirname)
            others = (holder[1] - {requester}) if holder else set()
            if others:
                raise SkillError(f"The environment of skill \"{skill}\" is used by {len(others)} other running "
                                 "session(s); stop them, or reinstall from there.", 409)
            mine = holder[0] if holder else None
            if holder is not None:
                holder[0] = None
            self._exclusive[dirname] = self._exclusive.get(dirname, 0) + 1   # no shared take meanwhile

        def unmark() -> None:
            with self._lock:
                left = self._exclusive.get(dirname, 1) - 1
                if left > 0:
                    self._exclusive[dirname] = left
                else:
                    self._exclusive.pop(dirname, None)

        try:
            _close(mine)
            fd = _try_lock(self._lock_file(dirname, "use"), exclusive=True)
        except BaseException:
            unmark()
            raise
        if fd is None:
            unmark()
            if holder is not None:
                self._take_shared(dirname, wait=1.0)
            raise SkillError(f"The environment of skill \"{skill}\" is used by another DreamTeam process; stop the "
                             "sessions that use it, then try again.", 409)
        try:
            yield
        finally:
            _close(fd)
            unmark()
            if holder is not None:
                self._take_shared(dirname, wait=1.0)

    def check_reinstall(self, spec: EnvSpec, owner: str | None) -> None:
        """Raise the 409 a reinstall of a ready env would meet (other sessions use it); nothing for a failed,
        half-built or broken env, which is rebuilt for everyone."""
        if self._disk_state(spec.dirname) != "ready":
            return
        with self.exclusive(spec.dirname, requester=owner, skill=spec.skill):
            pass

    def touch(self, owner: str, dirnames: Iterable[str]) -> None:
        """The `used` stamps of the envs `owner` uses and of its views (cleanup goes by them)."""
        view = owner_id(owner)
        for dirname in dirnames:
            path = self.dir(dirname)
            if os.path.isdir(long_path(path)):
                _touch(path / "used")
                if os.path.isdir(long_path(path / "views" / view)):
                    _touch(path / "views" / view / "used")

    # ---- builds ----
    def ensure(self, spec: EnvSpec, *, owner: str | None = None, force: bool = False) -> asyncio.Future:
        """Build `spec`'s env in the background unless it is ready (one build per env in this process; others
        join it). The future resolves to the final state."""
        loop = asyncio.get_running_loop()
        result: asyncio.Future = loop.create_future()

        def settle(done: concurrent.futures.Future) -> None:
            if result.cancelled():
                return
            if done.exception() is not None:
                result.set_exception(done.exception())
            else:
                result.set_result(done.result())

        def relay(done: concurrent.futures.Future) -> None:
            with contextlib.suppress(RuntimeError):   # the caller's loop is closed: nobody waits any more
                loop.call_soon_threadsafe(settle, done)

        self._start(spec, force=force, owner=owner).add_done_callback(relay)
        return result

    def _start(self, spec: EnvSpec, *, force: bool, owner: str | None) -> concurrent.futures.Future:
        with self._lock:
            future = self._builds.get(spec.dirname)
            if future is not None and not future.done():
                return future
            future = concurrent.futures.Future()
            self._builds[spec.dirname] = future
            if force or (self._mem.get(spec.dirname) or {}).get("memory"):
                self._mem.pop(spec.dirname, None)
        threading.Thread(target=self._run_build, args=(spec, force, owner, future), name="dreamteam-skill-env",
                         daemon=True).start()
        return future

    def _run_build(self, spec: EnvSpec, force: bool, owner: str | None, future: concurrent.futures.Future) -> None:
        try:
            state = self._build(spec, force, owner)
        except BaseException as exc:  # noqa: BLE001 - handed to whoever awaits the future
            with self._lock:
                self._mem.pop(spec.dirname, None)
                if self._builds.get(spec.dirname) is future:
                    del self._builds[spec.dirname]
            if isinstance(exc, Exception):
                # a refused reinstall (409: the env is in use): its subscribers saw "installing", so they get the
                # state the env is really in, never left waiting for an end that is not published
                with contextlib.suppress(Exception):
                    self._settle_after(spec, exc)
            future.set_exception(exc)
            return
        with self._lock:
            if self._builds.get(spec.dirname) is future:
                del self._builds[spec.dirname]
        future.set_result(state)

    def _settle_after(self, spec: EnvSpec, exc: Exception) -> None:
        """Publish the end of a build that raised: the env as it is on disk (still ready, an older failure), else a
        failure kept in memory with the error."""
        disk = self._disk_state(spec.dirname)
        if disk == "ready":
            self._done(spec, {**self._ready_state(spec), "reused": True})
        elif disk == "failed":
            self._done(spec, self._failed_state(spec))
        else:
            self._done(spec, {**self._base(spec), "status": "failed", "error": str(exc) or type(exc).__name__,
                              "log": str(self.log_path(spec.dirname))}, memory=True)

    def _set(self, spec: EnvSpec, state: dict, phase: str) -> None:
        with self._lock:
            self._mem[spec.dirname] = state
        self._publish(spec.dirname, {**self._base(spec), **state}, phase)

    def _done(self, spec: EnvSpec, state: dict, *, memory: bool = False) -> dict:
        with self._lock:
            if memory:
                self._mem[spec.dirname] = {k: v for k, v in state.items() if k not in self._base(spec)}
                self._mem[spec.dirname]["memory"] = True
            else:
                self._mem.pop(spec.dirname, None)
        public = {k: v for k, v in state.items() if k != "memory"}
        self._publish(spec.dirname, public, "done")
        return public

    def _trash(self, path: Path) -> bool:
        """Rename `path` away, then remove it; False when the rename is refused (Windows: a process has its working
        folder in it)."""
        trash = self.root / f".trash-{_secrets.token_hex(3)}-{int(time.time())}"
        try:
            skills._rename(path, trash)
        except OSError:
            return False
        with contextlib.suppress(OSError):
            rmtree(trash)
        return True

    def _build(self, spec: EnvSpec, force: bool, owner: str | None) -> dict:
        path = self.dir(spec.dirname)
        lock_path = self._lock_file(spec.dirname, "lock")
        deadline = time.monotonic() + spec.options.timeout + BUILD_WAIT_EXTRA
        fd = _try_lock(lock_path)
        if fd is None:
            self._set(spec, {"status": "installing", "other_process": True}, "waiting")
            while fd is None:
                if time.monotonic() >= deadline:
                    return self._done(spec, {**self._base(spec), "status": "failed", "error":
                                             "another DreamTeam process is still installing it",
                                             "log": str(self.log_path(spec.dirname))}, memory=True)
                time.sleep(LOCK_POLL)
                fd = _try_lock(lock_path)
        try:
            disk = self._disk_state(spec.dirname)
            if disk == "ready" and not force:
                _touch(path / "used")
                return self._done(spec, {**self._ready_state(spec), "reused": True})
            if disk == "failed" and not force:
                at = (_read_json(path / "failed.json") or {}).get("at")
                if isinstance(at, (int, float)) and time.time() - at < RETRY_FAILED_SECONDS:
                    return self._done(spec, self._failed_state(spec))
            if disk == "ready":   # a user's reinstall: nobody else may be running it
                with self.exclusive(spec.dirname, requester=owner, skill=spec.skill):
                    self._replace_ok(spec, path, refusal=True)
            elif disk == "broken":
                if not self._replace_ok(spec, path, refusal=False):
                    return self._done(spec, {**self._base(spec), "status": "failed", "error":
                                             "The environment is in use (a running command has its current folder "
                                             "in it); stop it and reinstall.",
                                             "log": str(self.log_path(spec.dirname))}, memory=True)
            elif disk in ("failed", "incomplete") and not self._trash(path):
                return self._done(spec, {**self._base(spec), "status": "failed", "error":
                                         "The environment is in use (a running command has its current folder in "
                                         "it); stop it and reinstall.", "log": str(self.log_path(spec.dirname))},
                                  memory=True)
            if not self._slots.acquire(blocking=False):
                self._set(spec, {"status": "installing", "queued": True}, "queued")
                self._slots.acquire()
            try:
                return self._install(spec, path)
            except Exception as exc:  # noqa: BLE001 - disk full, a lock, a long path: a failure everybody is told
                log.exception("the build of %s failed", spec.dirname)
                return self._fail(spec, path, f"{type(exc).__name__}: {exc}", None, False, time.time())
            finally:
                self._slots.release()
        finally:
            _close(fd)

    def _replace_ok(self, spec: EnvSpec, path: Path, *, refusal: bool) -> bool:
        """Take a ready (or broken) env out of service before its rebuild: `ok` goes first, then the folder."""
        ok = path / "ok"
        saved = _read_json(ok)
        with contextlib.suppress(OSError):
            os.unlink(long_path(ok))
        if self._trash(path):
            with self._lock:
                self._verified.pop(spec.dirname, None)
            return True
        if saved is not None:
            with contextlib.suppress(OSError):
                _write_json(ok, saved)
        if refusal:
            raise SkillError("The environment is in use (a running command has its current folder in it); stop it "
                             "and reinstall.", 409)
        return False

    def _install(self, spec: EnvSpec, path: Path) -> dict:
        started = time.time()
        self._set(spec, {"status": "installing", "started_at": started}, "started")
        os.makedirs(long_path(path / "src"), exist_ok=True)
        log_file = self.log_path(spec.dirname)
        manifest = spec.manifest
        try:
            self._copy_sources(spec, path)
        except (SkillError, OSError) as exc:
            return self._fail(spec, path, f"cannot copy the manifests: {exc}", None, False, started)
        commands = (INSTALLERS.python_commands if spec.ecosystem == "python" else INSTALLERS.node_commands)(spec,
                                                                                                           path)
        prefix = {"uv": "uv", "pip": "pip", "pip-venv": "pip", "npm": "npm"}.get(spec.tool.installer,
                                                                               spec.tool.installer)
        env_json = {"skill": spec.skill, "ecosystem": spec.ecosystem, "key": spec.key, "trust": spec.trust,
                    "files": list(manifest.files), "packages": list(manifest.packages), "tool": spec.tool.key_fields,
                    "installer": spec.installer,
                    "argv": [[redact(a, spec.options.secrets) for a in argv] for argv, _k in commands],
                    "created_at": started}
        _write_json(path / "env.json", env_json)
        with open(long_path(log_file), "wb") as out:
            writer = _LogWriter(out, spec.options.secrets)
            for argv, kind in commands:
                writer.write_line(f"$ {' '.join(redact(a, spec.options.secrets) for a in argv)}")
                timeout = min(VENV_TIMEOUT, spec.options.timeout) if kind == "venv" else spec.options.timeout
                code, timed_out = _run(argv, path, spec.options.env, timeout, writer)
                if timed_out:
                    return self._fail(spec, path, f"timed out after {timeout:g} s", None, True, started)
                if code != 0:
                    return self._fail(spec, path, _error_line(writer.lines, prefix), code, False, started)
        if spec.ecosystem == "python" and long_paths_off():
            deep = longest_paths(path / "venv")
            if deep is not None:
                dist = self._dist_of(path, deep[1])
                return self._fail(spec, path, f"package {dist} has files deeper than Windows allows with long paths "
                                  f"off (a path of {deep[0]} characters): enable LongPathsEnabled, or set [skills] "
                                  "envs_dir to a shorter folder", None, False, started)
        if spec.ecosystem == "python":
            have = _dist_infos(path)
            env_json["installed"] = [n for n in manifest.packages if n in have]
            env_json["commands"] = _python_commands(path)
        else:
            env_json["installed"] = list(manifest.packages)
            env_json["commands"] = _node_commands(path)
        if not self._verify(spec.dirname, env_json):
            return self._fail(spec, path, f"{prefix}: the install finished, but its files are not all there", None,
                              False, started)
        finished = time.time()
        env_json.update(seconds=round(finished - started, 1), finished_at=finished)
        _write_json(path / "env.json", env_json)
        with contextlib.suppress(OSError):
            os.unlink(long_path(path / "failed.json"))
        _touch(path / "used")
        _write_json(path / "ok", {"at": finished, "key": spec.key})   # last: its presence means "ready"
        with self._lock:
            self._verified.pop(spec.dirname, None)
        return self._done(spec, {**self._ready_state(spec), "reused": False, "started_at": started})

    @staticmethod
    def _dist_of(path: Path, deep: str) -> str:
        for site in _site_packages(path):
            prefix = str(site) + os.sep
            plain = deep
            if plain.startswith(prefix):
                return plain[len(prefix):].split(os.sep, 1)[0]
        return Path(deep).name

    def _copy_sources(self, spec: EnvSpec, path: Path) -> None:
        """The skill's manifests into the env (src/, the generated files): the bytes must be the ones the key
        covers, never through a link."""
        manifest = spec.manifest
        material = dict(spec.material)
        if isinstance(manifest, PyManifest):
            for rel in manifest.copies:
                self._copy_one(spec.source, rel, path / "src", material.get(rel))
            for name, text in manifest.generated:
                Path(long_path(path / "src" / name)).write_text(text, encoding="utf-8")
            return
        for rel in manifest.files:
            self._copy_one(spec.source, rel, path / "src", material.get(rel))
        Path(long_path(path / "package.json")).write_text(manifest.generated, encoding="utf-8")
        if manifest.lock:
            lock_rel = manifest.files[-1]
            shutil.copyfile(long_path(path / "src" / Path(*lock_rel.split("/"))), long_path(path / manifest.lock))

    @staticmethod
    def _copy_one(root: Path, rel: str, dest: Path, sha: str | None) -> None:
        target = Path(dest, *rel.split("/"))
        os.makedirs(long_path(target.parent), exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(long_path(target))
        digest = skills._copy_file(long_path(_path(root, rel)), long_path(target), 0o644)
        if sha is not None and digest != sha:
            raise SkillError(f"{rel} changed since it was checked; attach the skill again")

    def _fail(self, spec: EnvSpec, path: Path, error: str, code: int | None, timed_out: bool,
              started: float) -> dict:
        error = redact(error, spec.options.secrets)[:ERROR_MAX]
        try:
            _write_json(path / "failed.json", {"at": time.time(), "error": error, "exit": code,
                                               "timed_out": timed_out, "installer": spec.installer})
            written = True
        except OSError:
            written = False   # (disk full, a lock): kept in memory, else the half-built folder reads as installing
        return self._done(spec, {**self._base(spec), "status": "failed", "error": error,
                                 "installer": spec.installer, "log": str(self.log_path(spec.dirname)),
                                 "started_at": started, "finished_at": time.time()}, memory=not written)

    # ---- Node views ----
    def view_ok(self, spec: EnvSpec, owner: str) -> Path | None:
        """The owner's view of a ready Node env when it matches the env's current build (stats only), else None."""
        env = self.dir(spec.dirname)
        ok = _read_json(env / "ok")
        folder = env / "views" / owner_id(owner)
        view = _read_json(folder / "view.json")
        modules = folder / "node_modules"
        if ok is None or view is None or view.get("ok_at") != ok.get("at"):
            return None
        return modules if os.path.isfile(long_path(modules / ".package-lock.json")) else None

    def view(self, spec: EnvSpec, owner: str) -> Path:
        """`owner`'s own copy of a ready Node env's node_modules (hardlinks; a plain copy where the file system has
        none), rebuilt after the env is: what an agent's npm command does through the mirror link stays in it.
        Blocking."""
        key = (spec.dirname, owner_id(owner))
        with self._lock:
            lock = self._view_locks.setdefault(key, threading.Lock())
        with lock:
            env = self.dir(spec.dirname)
            folder = env / "views" / key[1]
            existing = self.view_ok(spec, owner)
            if existing is not None:
                _touch(folder / "used")
                return existing
            ok = _read_json(env / "ok")
            if ok is None:
                raise SkillError(f"The Node packages of skill \"{spec.skill}\" are not installed.", 409)
            os.makedirs(long_path(folder), exist_ok=True)
            tmp = folder / f".nm-{_secrets.token_hex(3)}"
            try:
                mode = _farm(env / "node_modules", tmp)
                modules = folder / "node_modules"
                if os.path.lexists(long_path(modules)):
                    if is_link(modules):
                        remove_link(modules)
                    else:
                        rmtree(modules)
                skills._rename(tmp, modules)
            except BaseException:
                with contextlib.suppress(OSError):
                    rmtree(tmp)
                raise
            _write_json(folder / "view.json", {"ok_at": ok.get("at"), "mode": mode})
            _touch(folder / "used")
            return modules

    # ---- cleanup ----
    def cleanup(self, retention_days: float, *, every_unused: bool = False) -> dict:
        """Remove envs unused for `retention_days` (0: none by age), failed or half-built ones older than a day,
        session views unused for a day and old trash; never an env any process uses or builds. `every_unused`: the
        user's "Remove unused now": every env no session uses now, whatever its age (and whatever the setting)."""
        removed: list[str] = []
        kept: list[dict] = []
        now = time.time()
        try:
            entries = sorted(os.scandir(long_path(self.root)), key=lambda e: e.name)
        except OSError:
            return {"removed": removed, "kept": kept}
        for entry in entries:
            if entry.name.startswith(".trash-") and entry.is_dir(follow_symlinks=False) and not is_link(entry):
                if now - (_mtime(Path(entry.path)) or now) > TRASH_STALE_SECONDS or self._trash_old(entry.name, now):
                    with contextlib.suppress(OSError):
                        rmtree(Path(entry.path))
        for entry in entries:
            name = entry.name
            if not DIRNAME_RE.fullmatch(name) or is_link(entry) or not entry.is_dir(follow_symlinks=False):
                continue
            path = self.dir(name)
            self._clean_views(name, now)
            state = self._disk_state(name)
            used = _mtime(path / "used") or _mtime(path) or now
            old = every_unused or (retention_days > 0 and now - used > retention_days * 86400)
            stale = state in ("failed", "incomplete") and now - (_mtime(path) or now) > FAILED_STALE_SECONDS
            if not (old or stale):
                continue
            fd = _try_lock(self._lock_file(name, "lock"))
            if fd is None:
                kept.append({"dirname": name, "why": "being built"})
                continue
            gone = False
            try:
                with self.exclusive(name):
                    used = _mtime(path / "used") or _mtime(path) or now
                    if old and not stale and not every_unused and now - used <= retention_days * 86400:
                        continue
                    ok = _read_json(path / "ok")
                    with contextlib.suppress(OSError):
                        os.unlink(long_path(path / "ok"))
                    trash = self.root / f".trash-{_secrets.token_hex(3)}-{int(now)}"
                    try:
                        skills._rename(path, trash)
                        gone = True
                    except OSError:
                        if ok is not None:
                            with contextlib.suppress(OSError):
                                _write_json(path / "ok", ok)
                        kept.append({"dirname": name, "why": "in use"})
            except SkillError:
                kept.append({"dirname": name, "why": "in use"})
            finally:
                _close(fd)
            if gone:
                removed.append(name)
                with self._lock:
                    self._verified.pop(name, None)
                with contextlib.suppress(OSError):
                    rmtree(trash)
        return {"removed": removed, "kept": kept}

    @staticmethod
    def _trash_old(name: str, now: float) -> bool:
        stamp = name.rpartition("-")[2]
        return stamp.isdigit() and now - int(stamp) > TRASH_STALE_SECONDS

    def _clean_views(self, dirname: str, now: float) -> None:
        views = self.dir(dirname) / "views"
        try:
            entries = list(os.scandir(long_path(views)))
        except OSError:
            return
        stale = [e for e in entries if e.is_dir(follow_symlinks=False) and not is_link(e)
                 and now - (_mtime(Path(e.path) / "used") or _mtime(Path(e.path)) or now) > VIEW_STALE_SECONDS]
        if not stale:
            return
        fd = _try_lock(self._lock_file(dirname, "lock"))
        if fd is None:
            return
        try:
            for entry in stale:
                with contextlib.suppress(OSError):
                    rmtree(Path(entry.path))
        finally:
            _close(fd)

    def log_tail(self, dirname: str, max_bytes: int = 65536) -> str:
        try:
            with open(long_path(self.log_path(dirname)), "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - max_bytes))
                return f.read().decode("utf-8", errors="replace")
        except OSError:
            return ""


def _farm(src: Path, dst: Path) -> str:
    """A hardlink farm of the folder `src` at `dst` (folders made, files linked, POSIX symlinks recreated with the
    same target, junctions left out); "copy" when the file system refuses hardlinks (the rest is copied)."""
    mode = "hardlink"
    os.makedirs(long_path(dst))
    stack = [(long_path(src), long_path(dst))]
    while stack:
        here, there = stack.pop()
        with os.scandir(here) as it:
            for entry in it:
                target = os.path.join(there, entry.name)
                if is_link(entry):
                    if not _WINDOWS and entry.is_symlink():
                        os.symlink(os.readlink(entry.path), target)
                    continue
                if entry.is_dir(follow_symlinks=False):
                    os.mkdir(target)
                    stack.append((entry.path, target))
                    continue
                if mode == "hardlink":
                    try:
                        os.link(entry.path, target)
                        continue
                    except OSError:
                        mode = "copy"
                shutil.copy2(entry.path, target)
    return mode


class _LogWriter:
    """install.log: every line redacted as it is written, at most LOG_MAX bytes; the last lines are kept for the
    failure text."""

    def __init__(self, out: Any, secrets: tuple[str, ...]):
        self.out = out
        self.secrets = secrets
        self.size = 0
        self.lines: list[str] = []
        self._lock = threading.Lock()

    def write_line(self, line: str) -> None:
        line = redact(line.rstrip("\r\n"), self.secrets)
        with self._lock:
            self.lines.append(line)
            if len(self.lines) > 400:
                del self.lines[:200]
            data = (line + "\n").encode("utf-8", errors="replace")
            if self.size + len(data) > LOG_MAX:
                return
            self.size += len(data)
            self.out.write(data)
            self.out.flush()


def _run(argv: list[str], cwd: Path, env: dict[str, str], timeout: float, writer: _LogWriter) -> tuple[int, bool]:
    """Run one installer command as a contained process tree (killed whole at the timeout, and with the hub);
    (exit code, timed out)."""
    try:
        tree = ProcessTree.spawn(argv, cwd=cwd, env=env)
    except OSError as exc:
        writer.write_line(f"cannot start {argv[0]}: {exc}")
        return 127, False

    def read() -> None:
        stream = tree.proc.stdout
        try:
            for raw in iter(stream.readline, b""):
                writer.write_line(raw.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            pass
        finally:
            tree.mark_eof()

    reader = threading.Thread(target=read, name="dreamteam-install-log", daemon=True)
    reader.start()
    timed_out = False
    try:
        code = tree.proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        tree.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            tree.proc.wait(timeout=10)
        code = tree.proc.returncode if tree.proc.returncode is not None else -1
    finally:
        tree.release()   # what the installer left running ends with its job / lifeline
    reader.join(timeout=5)
    with contextlib.suppress(OSError):
        tree.proc.stdout.close()
    return code, timed_out


__all__ = ["DIRNAME_RE", "Detected", "EnvManager", "EnvSpec", "NodeManifest", "PyManifest", "ToolInfo",
           "check_lock", "detect", "env_dirname", "env_key", "installer_env", "link_node_modules", "link_state",
           "long_paths_off", "longest_paths", "os_marker", "parse_package_json", "parse_pyproject",
           "parse_requirements", "pick_installer", "probe_node", "probe_python", "python_satisfies", "redact",
           "resolve_node", "resolve_python", "shell_ready", "trust_class", "venv_bin", "venv_python",
           "verify_node", "verify_python"]
