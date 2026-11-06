"""Export of a DreamTeam home into one folder that another computer imports (issue #38, stage 1).

`export_home(home, dest)` writes four categories, each optional (`include`):

    dreamteam-export.json                 the manifest, written last (a folder without it is an incomplete export)
    settings/dreamteam.toml               every settings section, [models.*] and [[roles]] included, without
                                          [mcp.external], API keys or secret-looking profile extras
    skills/<name>/                        the skills library: each skill's files plus .dreamteam.json (provenance,
                                          no stat manifest: its mtimes belong to this computer)
    mcp/library/<name>/                   the MCP library (server.py + meta.json, approvals included); a server
                                          saved by a session of this home names it by uid (project_session)
    mcp/external.json                     [mcp.external]: {name: {command, cwd, description, env_keys}}
    sessions/<id>/session.json            with its uid, without MCP env values
    sessions/<id>/.dreamteam/swarm.db     the sqlite backup of the database (WAL content included), VACUUMed
    sessions/<id>/.dreamteam/{mcp,attached-skills,jobs}/
    sessions/<id>/workspace/              the session's own folder or its folder inside the home (treecopy)

The source home is only read. Nothing secret of this computer leaves it: API keys, MCP env values (only their names
travel) and credentials in MCP command arguments are left out, and before the manifest is written every exported
file (and every git object of an exported repository) is searched for the known secrets of this computer
(`known_secrets`); a hit removes the export and fails, naming the place but never the value.

The import side reads the same layout. `plan_import(manager, folder)` changes nothing and says per item what would
happen (add | rewrite | unchanged | skip | refuse); `apply_import(manager, plan)` does it: add or rewrite, never a
deletion of what the export does not carry. Settings are merged with this computer's machine-local keys, API keys
and MCP env values kept (a key stays only with the endpoint it was saved for, an env value only with the same
command); sessions are matched by their uid and a rewritten one keeps its previous copy as a backup.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import dataclasses
import errno
import hashlib
import json
import os
import platform
import re
import secrets as _secrets
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Collection, Iterable, Iterator
from urllib.parse import unquote, urlsplit

from . import __version__, hubauth, hubgit
from . import mcp_library
from .config import (CONFIG_FILE, AgentSettings, MCPSettings, ModelProfile, RoleSettings, ServerSettings,
                     ShellSettings, SkillsSettings, SwarmSettings, TracingSettings, WorkspaceSettings, env_var_name,
                     key_env_names, make_profile)
from .db import foreign_objects, known_columns, loads, max_tools_revision, sqlite_uri, unknown_columns
from .mcp_forge import _split_command
from .roles import role_key
from .sessions import (LEGACY_ID, MOVE_JOURNAL, OVERRIDE_SECTIONS, REPLACED_PREFIX, SESSION_FILE, SESSION_ID,
                       WORKSPACE_DIR, ImportedWorkspace, SessionError, SessionManager, _home_relative, _in_thread,
                       _to_end, new_uid, session_uid, slugify)
from .settings import (PROVIDER_CHOICES, _endpoint, _private, _stored_key, apply_update, deep_merge, dump_toml,
                       read_raw, settings_path, validate)
from .shell import _KNOWN_SECRETS, _SECRET_RE, _URL_USERINFO, _is_secret, strip_url_credentials
from .skills import META_FILE as SKILL_META, NAME_MAX, NAME_RE, ORIGINS, Library, Limits, SkillError, _read_json, \
    _rename, check_skill_dir, copy_skill
from .tools.frozen import TOOLSET_REVISION
from .treecopy import (EXPORT_EXCLUDE_NAMES, EXPORT_EXCLUDE_PATHS, TreeResult, compare_digest, copy_tree, is_within,
                       scan_tree, valid_mode)
from .workspace import format_size, is_link, long_path, looks_like_git_dir, rmtree

FORMAT = "dreamteam-export"
FORMAT_VERSION = 1
MANIFEST = "dreamteam-export.json"
CATEGORIES = ("settings", "skills", "mcp", "sessions")
SETTINGS_FILE = "settings/dreamteam.toml"
SKILLS_DIR = "skills"
LIBRARY_DIR = "mcp/library"
EXTERNAL_FILE = "mcp/external.json"
SESSIONS_DIR = "sessions"
DB_FILE = "swarm.db"
# what an export folder holds at its top level (a folder with anything else is never replaced by a new export)
LAYOUT = frozenset({MANIFEST, "settings", SKILLS_DIR, "mcp", SESSIONS_DIR})
# the parts of a session's .dreamteam/ that travel (its logs, launcher files and lock files stay)
SESSION_DATA_PARTS = ("mcp", "attached-skills", "jobs")
MIN_SECRET = 8           # shorter values are not searched for (too many false hits)
GIT_PATH_MAX = 250       # a repository database deeper than this cannot be opened by git for Windows
FAT32_MAX = (4 << 30) - 1   # the largest file FAT32 holds
STALE_SECONDS = 3600     # leftover temp folders of an interrupted export next to the destination
BASE_URL_CREDENTIALS = "base_url (credentials)"   # the manifest's settings.omitted entry of a stripped base_url
# placeholder keys of local endpoints (settings.PROVIDER_CHOICES: "ollama", "EMPTY", "lm-studio"): not secrets
PLACEHOLDER_KEYS = frozenset(str(p["api_key"]) for p in PROVIDER_CHOICES if p.get("api_key"))
# header and field names that carry credentials whatever shell._SECRET_RE says
SECRET_NAMES = frozenset({"authorization", "proxy-authorization", "x-api-key", "api-key", "x-auth-token", "cookie",
                          "set-cookie", "auth", "key", "sig", "signature", "subscription-key",
                          "ocp-apim-subscription-key"})
# URL query parameters that carry a key whatever shell._SECRET_RE says (`?key=`, `?sig=`, `?code=`)
_QUERY_SECRETS = frozenset({"key", "apikey", "api_key", "token", "access_token", "auth", "sig", "signature",
                            "password", "secret", "code"})
# the last word of a flag, field or variable name that carries a credential (`--token X`, `--github-token X`,
# `--api-key=X`, `-password X`, `--client-secret X`, `--auth X`, `apiKey`, `GITHUB_TOKEN`, `-Ddb.password=X`)
_CREDENTIAL_WORDS = frozenset({"token", "secret", "password", "passwd", "pass", "pwd", "passphrase", "auth",
                               "authorization", "bearer", "apikey", "credential", "credentials"})
_CREDENTIAL_PAIRS = frozenset({("api", "key"), ("access", "key"), ("secret", "key"), ("private", "key")})
# a name whose last word is only "key" or "pat" (`--key X`, `--openai-key X`, `--pat X`) carries a credential only
# when its value looks like a token (`--sort-key created_at` does not)
_WEAK_WORDS = frozenset({"key", "pat"})
_FLAG = re.compile(r"--?[A-Za-z][A-Za-z0-9_.-]*")
_OPTION = re.compile(r"--?[A-Za-z][A-Za-z-]*(?:=.*)?", re.DOTALL)   # `--verbose`, `-v`: no value of a flag
_HEADER_FLAGS = frozenset({"--header", "--headers", "-H"})
_URL_IN_ARG = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+")
_HEADER_ARG = re.compile(r"\s*([A-Za-z0-9_-]+)\s*:\s*(\S.*)", re.DOTALL)
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")   # GITHUB_TOKEN=X (docker -e, env), db.password=X (-D)
_CREDENTIAL_TEXT = re.compile(r"\s*(?:bearer|basic|token)\s+\S{8,}", re.IGNORECASE)
_SCHEME_VALUE = re.compile(r"\s*(bearer|basic|token)\s+(\S+)\s*", re.IGNORECASE)
# a variable the server expands itself (mcp-remote: `--header Authorization:${AUTH_HEADER}`, its value in `env`)
_ENV_REFERENCE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*|%[A-Za-z_][A-Za-z0-9_]*%")
_SPEC_VALUE = re.compile(r"(?:id|src|source|target|env|type|uid|gid|mode)=[^,]*(?:,[A-Za-z]+=[^,]*)*")   # docker
_NO_VALUE = frozenset({"true", "false", "yes", "no", "on", "off", "none", "null"})
_HEADERS_ENV = ("ANTHROPIC_CUSTOM_HEADERS", "OPENAI_CUSTOM_HEADERS")
_WINDOWS = sys.platform == "win32"


class TransferError(ValueError):
    """An export or import that cannot be done (nothing was written). `usage`: a mistake in what was asked (the CLI
    exits 2 for it, 1 otherwise)."""

    def __init__(self, message: str, *, usage: bool = False):
        super().__init__(message)
        self.usage = usage


class SecretFound(TransferError):
    """The export would carry a secret of this computer: `places` name where (a file, `file: table.column:rowid` for
    a database, `file (git object <id>)` for a repository) and what kind of secret, never its value."""

    def __init__(self, places: list[str]):
        shown = "\n".join(f"  - {p}" for p in places[:50]) + (f"\n  - ... ({len(places) - 50} more)"
                                                                 if len(places) > 50 else "")
        command = any("a credential in the command of" in p for p in places)
        super().__init__("Nothing exported: the export would contain a secret of this computer (an API key, a token "
                         f"or a password) in:\n{shown}\nRemove it there, or leave that part out (--include, "
                         "--session), then export again."
                         + ("\nA credential of an MCP server's command that an agent copied into a session's files or "
                            "messages cannot be removed there: leave that session out (--session)." if command
                            else ""))
        self.places = places


class HomeTaken(TransferError):
    """Another DreamTeam process took the home while an import ran (apply_import's `before_swap` raises it): the
    import stops, the items after it are not done."""


# ---- include ---------------------------------------------------------------------------------------------------
def parse_include(values: Iterable[str] | str | None) -> tuple[str, ...]:
    """The categories of `--include` (comma lists, the option may repeat), in CATEGORIES order; every category when
    nothing is given. An unknown word is a TransferError with `usage` set."""
    if values is None:
        return CATEGORIES
    if isinstance(values, str):
        values = [values]
    words = [w.strip().lower() for value in values for w in str(value).split(",") if w.strip()]
    if not words:
        return CATEGORIES
    unknown = sorted({w for w in words if w not in CATEGORIES})
    if unknown:
        raise TransferError(f"Unknown --include {'categories' if len(unknown) > 1 else 'category'}: "
                            f"{', '.join(unknown)} (choose from {', '.join(CATEGORIES)}).", usage=True)
    return tuple(c for c in CATEGORIES if c in words)


# ---- secrets -----------------------------------------------------------------------------------------------------
class Secrets:
    """The secret values an export must not contain, each with a label that names its kind (shown; the value never
    is). Values shorter than MIN_SECRET characters and the placeholder keys of local endpoints are not kept."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def add(self, value: Any, label: str) -> None:
        if not isinstance(value, str):
            return
        for text in {value, value.strip()}:
            if len(text) >= MIN_SECRET and text not in PLACEHOLDER_KEYS:
                self.values.setdefault(text, label)

    def update(self, other: "Secrets | dict[str, str]") -> None:
        for value, label in (other.values if isinstance(other, Secrets) else other).items():
            self.add(value, label)

    def __len__(self) -> int:
        return len(self.values)

    def __contains__(self, value: str) -> bool:
        return value in self.values


def _secret_name(name: Any) -> bool:
    """A settings field, header or variable name that carries a credential."""
    text = str(name)
    return text.strip().lower() in SECRET_NAMES or bool(_SECRET_RE.search(text.replace("-", "_")))


def _list(value: Any) -> list[Any]:
    """`value` when it is a list, else [] (a manifest or file entry of another type)."""
    return value if isinstance(value, list) else []


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _scrub_extra(value: Any, where: str, omitted: list[str], removed: Secrets) -> Any:
    """A model profile's `extra` without secret-looking entries, at any depth: keys named like a credential (shell's
    _SECRET_RE, header names such as Authorization) and values such as "Bearer <token>"."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            place = f"{where}.{key}"
            if _secret_name(key) or (isinstance(item, str) and _CREDENTIAL_TEXT.match(item)):
                omitted.append(place)
                for text in _strings(item):
                    removed.add(text, place)
                continue
            out[key] = _scrub_extra(item, place, omitted, removed)
        return out
    if isinstance(value, list):
        items = []
        for i, item in enumerate(value):
            place = f"{where}[{i}]"
            if isinstance(item, str) and _CREDENTIAL_TEXT.match(item):
                omitted.append(place)
                removed.add(item, place)
                items.append("")
            else:
                items.append(_scrub_extra(item, place, omitted, removed))
        return items
    return value


_KEY_PREFIXES = ("gsk_", "sk_", "AIza", "xai_", "pk_", "rk_")


def _pasted_key(value: Any, environ: Any = None) -> bool:
    """An `api_key_env` value that is an API key typed into the variable-name field: one that cannot be a variable
    name, or one that names no variable set here and looks like a key (a key prefix such as gsk_ or AIza, or 20 or
    more characters with lower-case letters and digits, which variable names rarely have)."""
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    if env_var_name(text) is None:
        return True
    environ = os.environ if environ is None else environ
    if environ.get(text):
        return False
    return text.startswith(_KEY_PREFIXES) or (len(text) >= 20 and any(c.islower() for c in text)
                                              and any(c.isdigit() for c in text))


def scrub_settings(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str], Secrets]:
    """The settings an export carries: `raw` without [mcp.external] (the mcp category carries it, without env
    values), without any API key (`models.*.api_key`, a key typed into `api_key_env`), without secret-looking
    entries of `models.*.extra` and without the user information of a `base_url`. Returns (settings, the places left
    out, the values removed)."""
    out = copy.deepcopy(raw)
    omitted: list[str] = []
    removed = Secrets()
    mcp = out.get("mcp")
    if isinstance(mcp, dict):
        mcp.pop("external", None)
        if not mcp:
            out.pop("mcp")
    models = out.get("models")
    if isinstance(models, dict):
        for name, spec in models.items():
            if not isinstance(spec, dict):
                continue
            where = f"models.{name}"
            key = _stored_key(spec)
            if key:
                removed.add(key, f"the API key of model profile '{name}'")
            if "api_key" in spec:
                del spec["api_key"]
                omitted.append(f"{where}.api_key")
            env = spec.get("api_key_env")
            if _pasted_key(env):
                del spec["api_key_env"]  # a key typed into the variable-name field
                omitted.append(f"{where}.api_key_env")
                removed.add(env.strip(), f"the API key of model profile '{name}'")
            if isinstance(spec.get("extra"), (dict, list)):
                spec["extra"] = _scrub_extra(spec["extra"], f"{where}.extra", omitted, removed)
                if spec["extra"] in ({}, []):
                    del spec["extra"]
            url = spec.get("base_url")
            if isinstance(url, str) and strip_url_credentials(url) != url:   # https://user:password@gateway/v1
                spec["base_url"] = strip_url_credentials(url)
                omitted.append(f"{where}.{BASE_URL_CREDENTIALS}")
                for value in _userinfo_secrets(url):
                    removed.add(value, f"the credentials in the base_url of model profile '{name}'")
    return out, omitted, removed


def _userinfo_secrets(arg: str) -> list[str]:
    """The passwords (and lone http(s) user names, often tokens) that strip_url_credentials removes from `arg`."""
    found = []
    for match in _URL_USERINFO.finditer(arg):
        scheme, userinfo = match.group(1), match.group(2)
        if ":" in userinfo:
            found.append(userinfo.split(":", 1)[1])
        elif scheme.lower().rstrip(":/").rpartition("+")[2] in ("http", "https"):
            found.append(userinfo)
    return [v for text in found for v in {text, unquote(text)} if v]


def _strip_url_query(arg: str) -> tuple[str, list[str]]:
    """`arg` with the values of the credential parameters of every URL query in it emptied (`?api_key=X` ->
    `?api_key=`); returns (arg, the removed values, raw and unquoted)."""
    removed: list[str] = []

    def clean(match: re.Match[str]) -> str:
        url = match.group(0)
        head, mark, rest = url.partition("?")
        if not mark:
            return url
        query, hash_mark, fragment = rest.partition("#")
        pairs = []
        for pair in query.split("&"):
            name, eq, value = pair.partition("=")
            plain = unquote(name).strip()
            if eq and value and (plain.lower() in _QUERY_SECRETS or _secret_name(plain)):
                removed.extend(dict.fromkeys([value, unquote(value)]))
                pair = f"{name}="
            pairs.append(pair)
        return head + mark + "&".join(pairs) + hash_mark + fragment

    if "://" not in arg or "?" not in arg:
        return arg, removed
    return _URL_IN_ARG.sub(clean, arg), removed


def _credential_name(name: Any) -> str | None:
    """"strong" for a flag, field or variable name that carries a credential (its last word is token, password,
    secret, auth, ... or it ends with api key / access key / secret key / private key; `apiKey`, `api-key`, `API_KEY`
    alike), "weak" for one whose last word is only key or pat, None otherwise (also for `no-...`)."""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(name)).lower()
    words = [w for w in re.split(r"[^a-z0-9]+", text) if w]
    if not words or words[0] == "no":
        return None
    if words[-1] in _CREDENTIAL_WORDS or tuple(words[-2:]) in _CREDENTIAL_PAIRS:
        return "strong"
    return "weak" if words[-1] in _WEAK_WORDS else None


def _variable_kind(name: Any) -> str | None:
    """_credential_name of a variable (`GITHUB_TOKEN=X`, `-Ddb.password=X`); a name the shell treats as secret
    (shell._SECRET_RE: PGPASSWORD, AUTH_IDENT) is "strong" as well."""
    kind = _credential_name(name)
    return kind if kind is not None else ("strong" if _is_secret(str(name), set()) else None)


def _flag_kind(flag: str) -> str | None:
    """_credential_name of a command-line flag (`--api-key`, `-token`, `-Dpassword` as a -D definition)."""
    if not _FLAG.fullmatch(flag):
        return None
    kind = _credential_name(flag.lstrip("-"))
    if kind is None and flag.startswith("-D") and not flag.startswith("--") and len(flag) > 2:
        kind = _variable_kind(flag[2:])
    return kind


def _token_like(value: str) -> bool:
    """A value that looks like a generated token: 16 or more characters, no spaces or path separators, letters
    and digits."""
    return (len(value) >= 16 and not any(c.isspace() or c in "/\\" for c in value)
            and any(c.isalpha() for c in value) and any(c.isdigit() for c in value))


def _env_reference(value: str) -> bool:
    """`${AUTH_TOKEN}`, `$TOKEN`, `%TOKEN%` (also after Bearer / Basic / Token): a variable the server or the
    program it starts expands itself, from the env an export carries by name; no credential."""
    text = value.strip()
    scheme = _SCHEME_VALUE.fullmatch(text)
    return bool(_ENV_REFERENCE.fullmatch(scheme.group(2) if scheme else text))


def _path_like(value: str) -> bool:
    """A file system path (`/etc/mcp/auth.json`, `~/.aws`, `./creds.json`, `C:\\keys\\a.pem`): what a credential
    flag such as `--credentials` or `--auth` names when the credential lives in a file."""
    if value.startswith(("~/", "~\\", "./", "../", ".\\", "..\\", "\\\\")) or re.match(r"[A-Za-z]:[\\/]", value):
        return True
    return value.startswith("/") and ("/" in value[1:] or "." in value or os.path.exists(value))


def _credential_value(kind: str | None, value: Any) -> bool:
    """`value`, given under a name of credential `kind` (_credential_name), is a credential: not empty, no variable
    reference (_env_reference), no path, no yes/no word and no docker secret spec (`id=x,src=path`); under a weak
    name only one that looks like a token."""
    if kind is None or not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    if _env_reference(text) or _path_like(text) or text.lower() in _NO_VALUE or _SPEC_VALUE.fullmatch(text):
        return False
    return kind == "strong" or _token_like(text)


def _bearer_arg(value: Any) -> bool:
    """A lone `Bearer X` whose X looks like a token (digits, `+/=_-` or 16 characters: not `Bearer workflows`)."""
    scheme = _SCHEME_VALUE.fullmatch(value) if isinstance(value, str) else None
    if not scheme or scheme.group(1).lower() != "bearer" or _env_reference(scheme.group(2)):
        return False
    token = scheme.group(2)
    return len(token) >= MIN_SECRET and (len(token) >= 16 or any(c.isdigit() or c in "+/=_-" for c in token))


def _credential_parts(value: str) -> list[str]:
    """A removed credential and, for a `Bearer X` / `Basic X` / `Token X` value, the bare X as well (a copy of the
    token elsewhere is found too)."""
    scheme = _SCHEME_VALUE.fullmatch(value)
    return [value.strip()] + ([scheme.group(2)] if scheme else [])


_HEADER_NAMES = frozenset({"authorization", "proxy-authorization", "x-api-key", "api-key", "x-auth-token", "cookie"})


def _json_credentials(text: str) -> tuple[str, list[str]]:
    """A JSON object of a command argument (`{"Authorization": "Bearer X"}`, `--config={"apiKey": "X"}`) without
    its credentials, at any depth: the entries of a credential name (SECRET_NAMES, _credential_name) whose value
    is a credential (_credential_value), and lone `Bearer X` strings. Returns (the text, the removed values); the
    text as it was when nothing was removed or it is no JSON object."""
    found: list[str] = []

    def walk(item: Any) -> Any:
        if isinstance(item, dict):
            out = {}
            for key, entry in item.items():
                name = str(key).strip().lower()
                kind = "strong" if name in SECRET_NAMES and name != "key" else _credential_name(key)
                if isinstance(entry, str) and (_credential_value(kind, entry) or _bearer_arg(entry)):
                    found.extend(_credential_parts(entry))
                    continue
                out[key] = walk(entry)
            return out
        if isinstance(item, list):
            items = []
            for entry in item:
                if _bearer_arg(entry):
                    found.extend(_credential_parts(entry))
                    items.append("")
                else:
                    items.append(walk(entry))
            return items
        return item

    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return text, []
    if not isinstance(data, dict):
        return text, []
    try:
        kept = walk(data)
    except RecursionError:
        return text, []
    return (json.dumps(kept, ensure_ascii=False), found) if found else (text, [])


def strip_command_credentials(command: Any) -> tuple[Any, list[str]]:
    """An MCP server's `command` without credentials: the user information of URLs (shell.strip_url_credentials)
    and their credential query parameters (`?api_key=X`); the values of credential flags (`--token X`,
    `--api-key=X`, `-token=X`, `--password X`, `--key <token>`, `-Dpassword=X`, ...) and variables
    (`GITHUB_TOKEN=X`); credential headers (`Authorization: Bearer X` after `--header`, also given as two
    arguments, or as a JSON object); credentials in JSON arguments (`--config={"apiKey": "X"}`); and lone
    `Bearer X` arguments become empty. Variable references (`${AUTH_TOKEN}`), paths and other non-secret values
    stay (_credential_value). A command written as one string is split as mcp_forge splits it and comes back as a
    list when something was removed. Returns (command, the removed values; a bearer token also bare)."""
    if isinstance(command, str):
        try:
            parts = _split_command(command)
        except ValueError:  # unbalanced quotes: mcp_forge would refuse it too; only URLs are cleaned then
            cleaned = strip_url_credentials(command)
            return cleaned, (_userinfo_secrets(command) if cleaned != command else [])
        stripped, removed = strip_command_credentials(parts)
        return (stripped, removed) if removed else (command, [])
    if not isinstance(command, list):
        return command, []
    out = list(command)
    removed: list[str] = []

    def take(index: int, value: str, kept: str) -> None:
        removed.extend(_credential_parts(value))
        out[index] = kept

    i = 0
    while i < len(out):
        arg = out[i]
        if not isinstance(arg, str):
            i += 1
            continue
        cleaned = strip_url_credentials(arg)
        if cleaned != arg:
            removed.extend(_userinfo_secrets(arg))
            out[i] = arg = cleaned
        cleaned, in_query = _strip_url_query(arg)
        if in_query:
            removed.extend(in_query)
            out[i] = arg = cleaned
        after = out[i + 1] if i + 1 < len(out) and isinstance(out[i + 1], str) else None
        flag, eq, value = arg.partition("=")
        header = _HEADER_ARG.fullmatch(arg)
        if arg.lstrip().startswith("{"):                     # {"Authorization": "Bearer X"}
            out[i], found = _json_credentials(arg)
            removed.extend(found)
        elif eq and flag.startswith("-") and value.lstrip().startswith("{"):   # --config={"apiKey": "X"}
            cleaned, found = _json_credentials(value)
            out[i] = f"{flag}={cleaned}"
            removed.extend(found)
        elif arg in _HEADER_FLAGS and after is not None:
            named = _HEADER_ARG.fullmatch(after)
            if named:                                        # --header "Authorization: Bearer X"
                if _secret_name(named.group(1)) and _credential_value("strong", named.group(2)):
                    take(i + 1, named.group(2), f"{named.group(1)}:")
                i += 1
            elif ":" not in after and _secret_name(after) and i + 2 < len(out) and isinstance(out[i + 2], str) \
                    and not _OPTION.fullmatch(out[i + 2]) and _credential_value("strong", out[i + 2]):
                take(i + 2, out[i + 2], "")                  # --headers Authorization "Bearer X"
                i += 2
        elif _bearer_arg(arg):                               # Bearer X
            take(i, arg, "")
        elif header and header.group(1).lower() in _HEADER_NAMES and _credential_value("strong", header.group(2)):
            take(i, header.group(2), f"{header.group(1)}:")  # Authorization: Bearer X
        elif eq and flag in _HEADER_FLAGS and (named := _HEADER_ARG.fullmatch(value)) \
                and _secret_name(named.group(1)) and _credential_value("strong", named.group(2)):
            take(i, named.group(2), f"{flag}={named.group(1)}:")   # --header=Authorization: Bearer X
        elif eq and _FLAG.fullmatch(flag):
            if _credential_value(_flag_kind(flag), value):   # --token=X, -Dpassword=X
                take(i, value, f"{flag}=")
        elif eq and _ENV_ASSIGNMENT.fullmatch(flag):
            if _credential_value(_variable_kind(flag), value):   # GITHUB_TOKEN=X (docker -e, env, -D name=X)
                take(i, value, f"{flag}=")
        elif after is not None and _FLAG.fullmatch(arg) and not _OPTION.fullmatch(after) \
                and _credential_value(_flag_kind(arg), after):   # --token X (also a token that starts with -)
            take(i + 1, after, "")
            i += 1
        i += 1
    removed = [v for v in removed if v]
    return (out if removed else list(command)), removed


def _server_place(server: str, sid: str | None) -> str:
    return f"[mcp.external.{server}]" + (f" of session {sid}" if sid else "")


def scrub_external(external: Any, sid: str | None = None) -> tuple[dict[str, Any], Secrets, list[str]]:
    """[mcp.external] servers as an export carries them: `env` replaced by `env_keys` (the variable names, sorted:
    the values stay on this computer) and credentials removed from `command` (strip_command_credentials). Other keys
    stay. `sid`: the session whose overrides hold them (labels only). Returns (servers, the removed values: env values
    and command credentials, the servers whose command lost credentials)."""
    removed = Secrets()
    stripped: list[str] = []
    out: dict[str, Any] = {}
    if not isinstance(external, dict):
        return out, removed, stripped
    for name, spec in external.items():
        if not isinstance(spec, dict):
            out[name] = copy.deepcopy(spec)
            continue
        spec = copy.deepcopy(spec)
        env = spec.pop("env", None)
        keys = sorted(str(k) for k in env) if isinstance(env, dict) else []
        for key, value in (env.items() if isinstance(env, dict) else ()):
            removed.add(value, f"the env value {key} of {_server_place(name, sid)}")
        spec["env_keys"] = keys
        if "command" in spec:
            spec["command"], creds = strip_command_credentials(spec["command"])
            for value in creds:
                removed.add(value, f"a credential in the command of {_server_place(name, sid)}")
            if creds:
                stripped.append(name)
        out[name] = spec
    return out, removed, stripped


def external_manifest_spec(spec: Any) -> dict[str, Any]:
    """One server of mcp/external.json: command, cwd, description and env_keys of a scrub_external entry."""
    spec = spec if isinstance(spec, dict) else {}
    return {"command": spec.get("command"), "cwd": spec.get("cwd"), "description": spec.get("description") or "",
            "env_keys": list(spec.get("env_keys") or [])}


def scrub_overrides(overrides: Any, sid: str) -> tuple[Any, Secrets, list[str]]:
    """A session's overrides as an export carries them: its [mcp.external] servers through scrub_external. Returns
    (overrides, the removed values, the servers whose command lost credentials)."""
    if not isinstance(overrides, dict):
        return copy.deepcopy(overrides), Secrets(), []
    out = copy.deepcopy(overrides)
    mcp = out.get("mcp")
    if not isinstance(mcp, dict) or "external" not in mcp:
        return out, Secrets(), []
    mcp["external"], removed, stripped = scrub_external(mcp["external"], sid)
    return out, removed, stripped


def _env_names(raw: Any, section: str, key: str) -> list[str]:
    part = raw.get(section) if isinstance(raw, dict) else None
    names = part.get(key) if isinstance(part, dict) else None
    return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []


def known_secrets(raw: dict[str, Any], overrides: dict[str, Any] | None = None,
                  environ: dict[str, str] | None = None) -> Secrets:
    """The secrets of this computer an export must not contain: the stored API keys of the model profiles (also a
    key typed into `api_key_env`, and the user information of a `base_url`); the values of every variable a profile names and of every variable the shell
    treats as secret (shell._is_secret: its known names such as ANTHROPIC_API_KEY, GITHUB_TOKEN, the profiles' key
    variables, names like *_TOKEN or *PASSWORD*); the header values of ANTHROPIC_CUSTOM_HEADERS /
    OPENAI_CUSTOM_HEADERS; the values of the [shell] pass_env and [skills] install_env variables; the env values of
    the [mcp.external] servers and the credentials in their commands (strip_command_credentials), global and of
    each session (`overrides`: {session id: overrides}), whatever the export includes; the hub token of this
    process and the tokens `dreamteam sandbox` stored. Placeholder keys of local endpoints are left out."""
    lookup = os.environ if environ is None else environ   # (os.environ ignores the case of names on Windows)
    environ = dict(lookup)
    found = Secrets()
    models = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    key_vars: set[str] = set()
    for name, spec in models.items():
        if not isinstance(spec, dict):
            continue
        found.add(_stored_key(spec), f"the API key of model profile '{name}'")
        for value in _userinfo_secrets(spec["base_url"]) if isinstance(spec.get("base_url"), str) else ():
            found.add(value, f"the credentials in the base_url of model profile '{name}'")
        if _pasted_key(spec.get("api_key_env"), lookup):
            found.add(spec["api_key_env"].strip(), f"the API key of model profile '{name}'")
            continue
        variable = env_var_name(spec.get("api_key_env"))
        if variable:
            key_vars.add(variable.upper())
            found.add(lookup.get(variable), f"the value of {variable} (the key of model profile '{name}')")
    names = set(_KNOWN_SECRETS) | set(key_env_names()) | key_vars
    for key, value in environ.items():
        if _is_secret(key, names):
            found.add(value, f"the value of {key}")
    for variable in _HEADERS_ENV:
        for line in (lookup.get(variable) or "").split("\n"):
            header, colon, value = line.partition(":")
            if colon:
                found.add(value.strip(), f"a header of {variable} ({header.strip()})")
    sources = [("", raw)] + [(sid, o) for sid, o in (overrides or {}).items()]
    for sid, part in sources:
        for variable in _env_names(part, "shell", "pass_env") + _env_names(part, "skills", "install_env"):
            found.add(lookup.get(variable), f"the value of {variable}")
        mcp = part.get("mcp") if isinstance(part, dict) else None
        external = mcp.get("external") if isinstance(mcp, dict) else None
        for server, spec in (external.items() if isinstance(external, dict) else ()):
            env = spec.get("env") if isinstance(spec, dict) else None
            for key, value in (env.items() if isinstance(env, dict) else ()):
                found.add(value, f"the env value {key} of {_server_place(server, sid or None)}")
            for value in (strip_command_credentials(spec.get("command"))[1] if isinstance(spec, dict) else ()):
                found.add(value, f"a credential in the command of {_server_place(server, sid or None)}")
    found.add(hubauth.env_token(), "the hub token")
    folder = hubauth.tokens_dir()
    with contextlib.suppress(OSError):
        for path in sorted(folder.glob("*.json")):
            with contextlib.suppress(OSError, ValueError):
                data = json.loads(path.read_text(encoding="utf-8"))
                found.add(data.get("token") if isinstance(data, dict) else None, f"the hub token in {path}")
    return found


# ---- the secret scan ---------------------------------------------------------------------------------------------
def _stored_forms(value: str) -> list[str]:
    """`value` as a file or a database row may hold it: as it is, JSON-escaped (db.dumps, with and without ASCII
    escapes: quotes, backslashes, control and non-ASCII characters), and escaped twice (JSON inside a JSON string,
    such as a tool result that shows a .json file)."""
    once = _credential_needles(value)
    forms = [*once, *(e for n in once for e in (json.dumps(n, ensure_ascii=False)[1:-1], json.dumps(n)[1:-1]))]
    return list(dict.fromkeys(f for f in forms if f))


@dataclass
class _Matcher:
    pattern: re.Pattern[bytes]
    labels: dict[bytes, str]
    needles: list[bytes]
    overlap: int

    @classmethod
    def of(cls, secrets: Secrets) -> "_Matcher | None":
        labels: dict[bytes, str] = {}
        for value, label in secrets.values.items():
            for form in _stored_forms(value):
                for encoding in ("utf-8", "utf-16-le"):
                    labels.setdefault(form.encode(encoding, "surrogatepass"), label)
        if not labels:
            return None
        needles = sorted(labels, key=len, reverse=True)
        return cls(re.compile(b"|".join(re.escape(n) for n in needles)), labels, needles,
                   max(map(len, needles)) - 1)

    def search(self, chunks: Iterable[bytes]) -> set[bytes]:
        """The needles found in a stream of chunks (a match may span two chunks). A long chunk is searched needle by
        needle (bytes search, much faster than one alternation of many needles)."""
        found: set[bytes] = set()
        tail = b""
        for chunk in chunks:
            buf = tail + chunk
            if len(buf) > 4096:
                found.update(n for n in self.needles if n in buf)
            else:
                found.update(m.group(0) for m in self.pattern.finditer(buf))
            tail = buf[-self.overlap:] if self.overlap else b""
        return found


def _file_chunks(path: str, size: int = 1 << 20) -> Iterator[bytes]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as f:
        yield from iter(lambda: f.read(size), b"")


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _scan_db(path: Path, matcher: _Matcher) -> dict[bytes, list[str]]:
    """{needle: where} of every needle in the values of a database, read row by row (a long value is spread over
    overflow pages, so the file's bytes may not hold it in one piece): `table.column:rowid` (at most three rows
    per needle and column), `sqlite_master` for the schema text. Raises sqlite3.Error for a database that cannot be
    read."""
    found: dict[bytes, list[str]] = {}
    counts: dict[tuple[bytes, str], int] = {}

    def check(value: Any, where: str, rowid: Any = None) -> None:
        if isinstance(value, str):
            data = value.encode("utf-8", "surrogatepass")
        elif isinstance(value, bytes):
            data = value
        else:
            return
        for needle in matcher.search([data]):
            seen = counts.get((needle, where), 0)
            counts[(needle, where)] = seen + 1
            if seen < 3:
                found.setdefault(needle, []).append(where if rowid is None else f"{where}:{rowid}")

    conn = sqlite3.connect(sqlite_uri(path, "immutable=1"), uri=True)
    try:
        objects = conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
        for _type, _name, sql in objects:
            check(sql, "sqlite_master")
        for kind, table, sql in objects:
            if kind != "table" or (isinstance(sql, str) and sql.lstrip().upper().startswith("CREATE VIRTUAL")):
                continue   # a virtual table's rows are in its shadow tables, read as tables
            columns = [r[0] for r in conn.execute("SELECT name FROM pragma_table_info(?)", (table,))]
            try:
                rows = conn.execute(f"SELECT rowid, * FROM {_quoted(table)}")
                with_rowid = True
            except sqlite3.OperationalError:   # a table WITHOUT ROWID
                rows = conn.execute(f"SELECT * FROM {_quoted(table)}")
                with_rowid = False
            for row in rows:
                values = row[1:] if with_rowid else row
                for column, value in zip(columns, values):
                    check(value, f"{table}.{column}", row[0] if with_rowid else None)
    finally:
        conn.close()
    return found


def _git_env() -> dict[str, str]:
    env = hubgit.environment()
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return env


def _search_git(git: str, git_dir: Path, matcher: _Matcher) -> list[tuple[str, bytes]]:
    """(object id, needle) of every git object (blobs, trees, commits, tags; packed or loose) of the repository
    database `git_dir` that holds a needle. git runs with no user or system configuration, no hooks, pager or
    fsmonitor, and no transport. Raises TransferError when git fails."""
    longpaths = ["-c", "core.longpaths=true"] if _WINDOWS else []
    argv = [git, *hubgit.fixed_args(), "-c", "safe.directory=*", *longpaths, f"--git-dir={git_dir}", "cat-file",
            "--batch-all-objects", "--batch", "--unordered"]
    kwargs: dict[str, Any] = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if _WINDOWS else {}
    found: list[tuple[str, bytes]] = []
    deep = _WINDOWS and len(str(git_dir)) > GIT_PATH_MAX
    too_deep = (f"Nothing exported: git cannot read the repository {git_dir}: its path in the export is too long "
                "for git on Windows, so its history cannot be checked for secrets. Export to a folder with a shorter "
                "path, or leave that session out (--session).")
    try:
        proc = subprocess.Popen(argv, cwd=str(git_dir), env=_git_env(), stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, **kwargs)
    except OSError as exc:
        if deep or getattr(exc, "winerror", None) in (206, 267):
            raise TransferError(too_deep) from None
        raise TransferError(f"Nothing exported: git could not be started to check the history of {git_dir} for "
                            f"secrets: {exc.strerror or exc}") from None
    with proc:
        out = proc.stdout
        assert out is not None
        try:
            while True:
                header = out.readline()
                if not header:
                    break
                fields = header.split()
                if len(fields) < 3 or fields[1] == b"missing":
                    continue
                oid, size = fields[0].decode("ascii", "replace"), int(fields[2])

                def chunks(left: int = size) -> Iterator[bytes]:
                    while left > 0:
                        data = out.read(min(left, 1 << 20))
                        if not data:
                            raise TransferError(f"git stopped while reading {git_dir}")
                        left -= len(data)
                        yield data

                for needle in matcher.search(chunks()):
                    found.append((oid, needle))
                out.read(1)  # the newline after the contents
        except BaseException:
            proc.kill()
            raise
        code = proc.wait()
    if code != 0:
        if deep:
            raise TransferError(too_deep)
        raise TransferError(f"Nothing exported: git could not read the history of {git_dir} (exit code {code}) to "
                            "check it for secrets.")
    return found


def scan_for_secrets(root: Path, secrets: Secrets, *, git: str | None = None) -> list[str]:
    """Search every file below `root` (bytes; each secret as UTF-8 and as UTF-16LE) and every object of each git
    repository database below it for `secrets`. Returns the places found, `<path>: <label>`, sorted; a database hit
    names `table.column:rowid` when it can be located. Raises TransferError when a repository exists but git is
    missing or fails (its history could not be checked)."""
    matcher = _Matcher.of(secrets)
    if matcher is None:
        return []
    root = Path(os.path.abspath(root))
    places: list[str] = []
    git_dirs: list[str] = []
    stack: list[tuple[str, ...]] = [()]
    while stack:   # by relative parts: every file is opened by its long_path, every message names the relative path
        parts = stack.pop()
        folder = root.joinpath(*parts)
        if parts and looks_like_git_dir(Path(long_path(folder))):
            git_dirs.append("/".join(parts))
        try:
            with os.scandir(long_path(folder)) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            raise TransferError(f"Nothing exported: cannot read {'/'.join(parts) or root} to check it for secrets: "
                                f"{exc.strerror or exc}") from None
        for entry in entries:
            if is_link(entry):
                continue
            if entry.is_dir(follow_symlinks=False):
                stack.append(parts + (entry.name,))
                continue
            rel = "/".join(parts + (entry.name,))
            try:
                hits = matcher.search(_file_chunks(entry.path))
            except OSError as exc:
                raise TransferError(f"Nothing exported: cannot read {rel} to check it for secrets: "
                                    f"{exc.strerror or exc}") from None
            path = root.joinpath(*parts, entry.name)
            if _is_sqlite(path):   # its values too: a long one lies in pieces on overflow pages
                try:
                    in_rows = _scan_db(path, matcher)
                except sqlite3.Error as exc:
                    raise TransferError(f"Nothing exported: cannot read the database {rel} to check it for secrets: "
                                        f"{exc}. Leave that part out (--session), or repair the file.") from None
                by_label: dict[str, set[str]] = {}
                for needle, where in in_rows.items():
                    by_label.setdefault(matcher.labels[needle], set()).update(where)
                places.extend(f"{rel} ({', '.join(sorted(where))}): {label}" for label, where in by_label.items())
                hits -= set(in_rows)
            places.extend(f"{rel}: {label}" for label in sorted({matcher.labels[n] for n in hits}))
    if git_dirs:
        git = git or shutil.which("git")
        if git is None:
            raise TransferError("Nothing exported: git is needed to check the history of the exported repositories "
                                f"for secrets ({', '.join(sorted(git_dirs)[:3])}); install git, or leave those "
                                "sessions out (--session).")
        for rel in sorted(git_dirs):
            for oid, needle in _search_git(git, root.joinpath(*rel.split("/")), matcher):
                places.append(f"{rel} (git object {oid[:12]}): {matcher.labels[needle]}")
    return sorted(set(places))


def _is_sqlite(path: Path) -> bool:
    try:
        with open(long_path(path), "rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


# ---- the report ----------------------------------------------------------------------------------------------
@dataclass
class ExportReport:
    """What an export wrote (or, with dry_run, would write). `left_out`: {"what", "reason", "refused"}; refused
    items were asked for but could not be exported (a damaged session, a broken skill), the others are left out by
    rule (the legacy main session). `not_copied`: sessions whose folder outside the home was not copied."""

    home: Path
    dest: Path
    include: tuple[str, ...]
    dry_run: bool = False
    settings: dict[str, Any] | None = None
    skills: list[dict[str, Any]] = field(default_factory=list)
    mcp: dict[str, Any] | None = None
    sessions: list[dict[str, Any]] = field(default_factory=list)
    left_out: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    not_copied: list[dict[str, Any]] = field(default_factory=list)
    sizes: dict[str, int] = field(default_factory=dict)       # bytes per category
    replaced: bool = False                                     # a previous export in `dest` was replaced
    manifest: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        """Everything asked for was exported (nothing refused)."""
        return not any(item.get("refused") for item in self.left_out)

    def leave_out(self, what: str, reason: str, *, refused: bool = True) -> None:
        self.left_out.append({"what": what, "reason": reason, "refused": refused})

    def to_json(self) -> dict[str, Any]:
        return {"home": str(self.home), "dest": str(self.dest), "include": list(self.include),
                "dry_run": self.dry_run, "settings": self.settings, "skills": self.skills, "mcp": self.mcp,
                "sessions": self.sessions, "left_out": self.left_out, "warnings": self.warnings,
                "not_copied": self.not_copied, "sizes": self.sizes, "replaced": self.replaced, "ok": self.ok}


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{count} {word if count == 1 else (plural or word + 's')}"


def render_export_report(report: ExportReport) -> str:
    """The export's result as the CLI prints it: one line per category, what was left out, the warnings, the
    folders not copied with what to do on the other computer, and how to import it there."""
    lines = [f"Would export {report.home} to {report.dest} (dry run: nothing is written):" if report.dry_run
             else f"Exported {report.home} to {report.dest}"
             + (" (the previous export there was replaced):" if report.replaced else ":")]
    sizes = report.sizes
    if "settings" in report.include and report.settings is not None:
        s = report.settings
        lines.append(f"  settings  dreamteam.toml: {_plural(len(s['sections']), 'section')}, "
                     f"{_plural(len(s['models']), 'model profile')}, {_plural(len(s['roles']), 'role')}"
                     " (API keys stay on this computer)")
    if "skills" in report.include:
        lines.append(f"  skills    {_plural(len(report.skills), 'skill')}, {format_size(sizes.get('skills', 0))}")
    if "mcp" in report.include and report.mcp is not None:
        library, external = report.mcp["library"], report.mcp["external"]
        approved = sum(1 for e in library if e.get("approved"))
        lines.append(f"  mcp       {_plural(len(library), 'library server')} ({approved} approved), "
                     f"{_plural(len(external), 'external server')} (env values stay on this computer)")
    if "sessions" in report.include:
        db = sum(s.get("db_bytes") or 0 for s in report.sessions)
        files = sum((s.get("workspace") or {}).get("bytes") or 0 for s in report.sessions)
        lines.append(f"  sessions  {_plural(len(report.sessions), 'session')}, "
                     f"{format_size(sizes.get('sessions', 0))} (databases {format_size(db)}, workspace files "
                     f"{format_size(files)})")
    if report.left_out:
        lines.append("Left out:")
        lines.extend(f"  - {item['what']}: {item['reason']}" for item in report.left_out)
    if report.warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {text}" for text in report.warnings)
    if report.not_copied:
        lines.append("Not copied (folders outside the DreamTeam home):")
        lines.extend(f"  - session {item['session']} works in {item['path']}: on the other computer put the project "
                     "at that path, or run /cd in the session" for item in report.not_copied)
    if report.dry_run:
        lines.append(f"Total {format_size(sum(sizes.values()))}. Run it without --dry-run to write the folder.")
        lines.append("The check for secrets of this computer (API keys, tokens) in the exported files runs only "
                     "when the folder is written.")
        return "\n".join(lines)
    lines.append(f"Total {format_size(sum(sizes.values()))}."
                 + (" The folder holds the histories of the sessions as they are: keep it private."
                    if report.sessions else ""))
    enter = []
    if report.settings and report.settings.get("models"):
        enter.append("the API keys of the model profiles")
    if report.mcp and any(e.get("env_keys") for e in report.mcp.get("external") or []):
        enter.append("the env values of the MCP servers")
    lines.append("On the other computer: use the same DreamTeam version (dreamteam --version)"
                 + (f", then enter {' and '.join(enter)} there." if enter else "."))
    lines.append(f"Import it there with: dreamteam import {_shell_quote(str(report.dest))}")
    return "\n".join(lines)


def _shell_quote(text: str) -> str:
    return f'"{text}"' if any(c.isspace() for c in text) else text


# ---- export --------------------------------------------------------------------------------------------------
def _is_home(path: Path) -> bool:
    """A launcher home (sessions/ next to dreamteam.toml), as cli.is_home."""
    return (path / "sessions").is_dir() and (path / CONFIG_FILE).exists()


def _key(path: Path) -> str:
    try:
        path = Path(path).resolve()
    except (OSError, RuntimeError):
        path = Path(os.path.abspath(path))
    return os.path.normcase(os.path.normpath(str(path)))


def _inside(path: Path, parent: Path) -> bool:
    """`path` is `parent` or inside it: as resolved text, and by file identity (treecopy.is_within: a file system
    that ignores case where normcase does not, as on macOS)."""
    p, q = _key(path), _key(parent)
    return p == q or p.startswith(q.rstrip(os.sep) + os.sep) or is_within(p, q)


@dataclass
class _Session:
    sid: str
    meta: dict[str, Any]
    uid: str
    kind: str                    # own | in_home | outside
    folder: Path | None          # the folder copied (None: outside the home, or missing)
    stored: str | None           # session.json's workspace path (None: its own folder)
    in_home: str | None
    adopted: bool


def _pick_sessions(manager: SessionManager, wanted: list[str] | None, report: ExportReport) -> list[_Session]:
    ids = manager.ids()
    legacy = LEGACY_ID in ids
    if legacy and (wanted is None or LEGACY_ID in wanted):
        report.leave_out(f"session {LEGACY_ID}", "the main session of a home used before sessions existed is not "
                         "exported yet (create a new session for that work)", refused=wanted is not None)
    ids = [sid for sid in ids if sid != LEGACY_ID]
    metas: dict[str, dict[str, Any]] = {}
    damaged: dict[str, str] = {}
    for sid in ids:
        try:
            metas[sid] = manager.meta(sid)
        except SessionError as exc:
            damaged[sid] = str(exc)
    chosen: list[str] = []
    if wanted is None:
        chosen = ids
    else:
        for item in wanted:
            text = str(item).strip()
            if text == LEGACY_ID and legacy:
                continue   # left out above
            if text in ids:
                match = [text]
            else:
                match = [sid for sid, meta in metas.items() if session_uid(meta) == text.lower()]
            if len(match) != 1:
                raise TransferError(f"Unknown session '{text}'. Sessions: {', '.join(ids) or 'none'}." if not match
                                    else f"Sessions {' and '.join(match)} have the identity {text}: name one by its "
                                    "id.", usage=True)
            if match[0] not in chosen:
                chosen.append(match[0])
    picked: list[_Session] = []
    for sid in chosen:
        if sid in damaged:
            report.leave_out(f"session {sid}", damaged[sid])
            continue
        meta = metas[sid]
        uid = session_uid(meta)
        if uid is None:
            report.leave_out(f"session {sid}", f"its {SESSION_FILE} has no created_at, which the other computer "
                             "needs to recognize the session")
            continue
        if (manager.dir_of(sid) / ".dreamteam" / MOVE_JOURNAL).exists():
            report.leave_out(f"session {sid}", "moving its workspace did not finish (its files may be in two "
                             f"folders): put them together in the folder the session uses and delete .dreamteam/"
                             f"{MOVE_JOURNAL} (starting it says where), then export again")
            continue
        folder, stored, adopted = manager._workspace_entry(sid, meta)
        relative = _home_relative(meta.get("workspace"))
        if stored is None:
            kind = "own"
        elif relative is not None:
            kind = "in_home"
        else:
            kind, folder = "outside", None
        picked.append(_Session(sid, meta, uid, kind, folder, stored, relative, adopted))
    seen: dict[str, str] = {}
    for item in picked:
        if item.uid in seen:
            report.warnings.append(f"sessions {seen[item.uid]} and {item.sid} have the same identity (one was copied "
                                   "by hand): the other computer imports them only by id (--session); each is matched "
                                   "by its id there, and one that is added gets a new identity")
        seen.setdefault(item.uid, item.sid)
    return picked


def _check_dest(home: Path, dest: Path, protected: list[tuple[Path, str]],
                session_folders: list[tuple[Path, str]]) -> str:
    """What `dest` is: "missing", "empty" or "export" (a previous export, replaced). TransferError when it cannot be
    used: a file, a link, a folder with other files, or a place inside what the export reads."""
    if dest.parent == dest:
        raise TransferError(f"{dest} is the root of a drive or of the file system: choose a folder inside it, e.g. "
                            f"{dest / 'dreamteam-export'}.")
    for folder, what in protected:
        if _inside(dest, folder):
            raise TransferError(f"Nothing exported: {dest} is inside {what} ({folder}), which the export reads. "
                                "Choose a folder outside it.")
    for folder, what in [(home, "the DreamTeam home"), *session_folders, *protected]:
        if _inside(folder, dest):
            raise TransferError(f"Nothing exported: {dest} contains {what} ({folder}). Choose another folder.")
    if not os.path.lexists(dest):
        return "missing"
    if is_link(dest):
        raise TransferError(f"Nothing exported: {dest} is a link; give the folder it points to, or a new folder.")
    if not dest.is_dir():
        raise TransferError(f"Nothing exported: {dest} is a file, not a folder.")
    if os.path.ismount(dest):
        raise TransferError(f"Nothing exported: {dest} is the root of a drive (a mounted volume): choose a folder "
                            f"inside it, e.g. {dest / 'dreamteam-export'}.")
    try:
        with os.scandir(long_path(dest)) as it:
            names = [e.name for e in it if not _os_metadata(e.name)]   # Finder, Explorer: no content of their own
    except OSError as exc:
        raise TransferError(f"Nothing exported: cannot read {dest}: {exc.strerror or exc}.") from None
    if not names:
        return "empty"
    if MANIFEST in names and set(names) <= LAYOUT and (dest / MANIFEST).is_file():
        return "export"
    others = sorted(set(names) - LAYOUT)
    if MANIFEST in names and others:
        shown = ", ".join(others[:5]) + (", ..." if len(others) > 5 else "")
        raise TransferError(f"Nothing exported: {dest} holds an earlier DreamTeam export and other files ({shown}): "
                            "move them out to replace the export, or choose another folder.")
    raise TransferError(f"Nothing exported: {dest} is not empty and holds no DreamTeam export. Choose a new or empty "
                        "folder (a folder that holds an earlier export is replaced).")


_OS_METADATA = frozenset({".ds_store", ".fseventsd", ".spotlight-v100", ".trashes", "thumbs.db", "desktop.ini",
                          "system volume information", "$recycle.bin"})


def _os_metadata(name: str) -> bool:
    """A file or folder the OS puts into folders it shows or volumes it mounts (Finder's .DS_Store and ._* files,
    Explorer's Thumbs.db and desktop.ini)."""
    return name.casefold() in _OS_METADATA or name.startswith("._")


def _mkdir_private(path: Path) -> None:
    """A new folder only its owner can read: mode 0700 on POSIX. On Windows the folder keeps the access rules it
    inherits (a mode makes Python give it an owner-only list, which another computer's user cannot read)."""
    if os.name == "nt":
        os.mkdir(long_path(path))
        return
    os.mkdir(long_path(path), 0o700)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def _clean_stale(dest: Path) -> None:
    """Remove the temp folders an interrupted export (or replace) left next to `dest`."""
    prefixes = (f".{dest.name}.export-", f".{dest.name}.old-")
    now = time.time()
    stale: list[Path] = []
    with contextlib.suppress(OSError), os.scandir(long_path(dest.parent)) as it:
        stale = [Path(dest.parent, e.name) for e in it if e.name.startswith(prefixes)
                 and now - e.stat(follow_symlinks=False).st_mtime > STALE_SECONDS]
    for path in stale:
        with contextlib.suppress(OSError):
            if not is_link(path):
                rmtree(path)


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(long_path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8", "replace")


def _settings_part(raw: dict[str, Any], out: Path | None, report: ExportReport, secrets: Secrets) -> None:
    scrubbed, omitted, removed = scrub_settings(raw)
    secrets.update(removed)
    models = scrubbed.get("models") if isinstance(scrubbed.get("models"), dict) else {}
    roles = scrubbed.get("roles") if isinstance(scrubbed.get("roles"), list) else []
    report.settings = {
        "sections": sorted(k for k, v in scrubbed.items() if isinstance(v, dict) or k == "roles"),
        "models": sorted(models),
        "roles": [role_key(r.get("name")) for r in roles if isinstance(r, dict)],
        "omitted": omitted,
    }
    keys_told: set[str] = set()
    raw_models = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    for place in omitted:
        name = place.split(".")[1] if place.count(".") >= 1 else place
        spec = raw_models.get(name) if isinstance(raw_models.get(name), dict) else {}
        if place.endswith(".api_key") and isinstance(spec.get("api_key"), str) and spec["api_key"] in PLACEHOLDER_KEYS:
            continue   # the placeholder of a local server (lm-studio): the import puts it back by itself
        if place.endswith(".api_key") or place.endswith(".api_key_env"):
            if name not in keys_told:
                keys_told.add(name)
                report.warnings.append(f"model profile '{name}': its API key stays on this computer; enter it in "
                                       "Settings on the other computer")
        elif place.endswith(f".{BASE_URL_CREDENTIALS}"):
            report.warnings.append(f"model profile '{name}': re-enter the credentials in its base_url on the other "
                                   "computer (they were removed from it)")
        else:
            report.warnings.append(f"model profile '{name}': {place} looks secret and is left out; enter it again "
                                   "on the other computer")
    try:
        text = ("# DreamTeam settings exported from another computer (dreamteam export). API keys and the\n"
                "# [mcp.external] servers are not in this file.\n\n" + dump_toml(scrubbed)).encode("utf-8")
    except ValueError as exc:
        raise TransferError(f"Nothing exported: the settings cannot be written: {exc}") from None
    report.sizes["settings"] = len(text)
    if out is not None:
        _write_bytes(out / SETTINGS_FILE, text)


def _skills_part(root: Path, limits: Limits, out: Path | None, report: ExportReport) -> None:
    try:
        with os.scandir(long_path(root)) as it:
            names = sorted(e.name for e in it if not e.name.startswith("."))
    except OSError:
        names = []
    total = 0
    for name in names:
        folder = root / name
        if not (len(name) <= NAME_MAX and NAME_RE.fullmatch(name)):
            if folder.is_dir():
                report.leave_out(f"skill folder {name}", "not a valid skill name")
            continue
        if is_link(folder):
            report.leave_out(f"skill {name}", "a link in the library, not a folder")
            continue
        if not folder.is_dir():
            continue
        try:
            info = check_skill_dir(folder, limits)
        except SkillError as exc:
            report.leave_out(f"skill {name}", str(exc))
            continue
        meta = _read_json(folder / SKILL_META) or {}
        changed = (not meta or meta.get("outside") or not isinstance(meta.get("manifest"), list)
                   or meta.get("manifest") != info.manifest)
        source = meta.get("source") if isinstance(meta.get("source"), dict) else {}
        source = {k: (strip_url_credentials(v) if isinstance(v, str) else v) for k, v in source.items()}
        provenance = {"name": name, "description": info.description, "origin": meta.get("origin") or "folder",
                      "source": source, "by": meta.get("by") or "", "via": meta.get("via") or "outside",
                      "added_at": meta.get("added_at"), "updated_at": meta.get("updated_at"), "digest": info.digest}
        if out is not None:
            try:
                copy_skill(folder, out / SKILLS_DIR, name, limits, info=info, meta=lambda _copied: provenance)
            except SkillError as exc:
                report.leave_out(f"skill {name}", str(exc))
                continue
        for skipped in info.skipped:
            report.warnings.append(f"skill {name}: {skipped['path']} is {skipped['error']}")
        total += info.size
        report.skills.append({"name": name, "digest": info.digest, "origin": provenance["origin"],
                              "files": info.files, "size": info.size, "changed_outside": bool(changed)})
    report.sizes["skills"] = total


def _mcp_part(raw: dict[str, Any], library: Path, projects: dict[str, str], out: Path | None, report: ExportReport,
              secrets: Secrets) -> None:
    found = mcp_library.scan(library)
    entries = []
    total = 0
    for name, reason in found.skipped:
        report.leave_out(f"MCP library server {name}", reason)
    for entry in found.entries:
        meta = copy.deepcopy(entry.meta)
        project_session = projects.get(_key(Path(entry.project))) if entry.project else None
        if project_session:
            meta.pop("project", None)
            meta["project_session"] = project_session
        if out is not None:
            folder = out / LIBRARY_DIR / entry.name
            folder.parent.mkdir(parents=True, exist_ok=True)
            mcp_library.write_entry_folder(folder, entry.code, meta)
        size = len(entry.code) + len(_json_bytes(meta))
        total += size
        entries.append({"name": entry.name, "digest": entry.digest, "approved": entry.approved,
                        "changed_outside": entry.changed_outside, "project_session": project_session})
    mcp = raw.get("mcp") if isinstance(raw.get("mcp"), dict) else {}
    servers, removed, stripped = scrub_external(mcp.get("external"))
    secrets.update(removed)
    external = {name: external_manifest_spec(spec) for name, spec in servers.items()}
    for name in stripped:
        report.warnings.append(f"re-enter the credentials of [mcp.external.{name}] on the other computer (they were "
                               "removed from its command)")
    data = _json_bytes(external)
    total += len(data)
    if out is not None:
        _write_bytes(out / EXTERNAL_FILE, data)
    report.mcp = {"library": entries,
                  "external": [{"name": n, "env_keys": s["env_keys"], "credentials_removed": n in stripped}
                               for n, s in external.items()]}
    report.sizes["mcp"] = total


@contextlib.contextmanager
def _read_db(path: Path, timeout: float = 30.0) -> Iterator[tuple[sqlite3.Connection, bool]]:
    """A connection that reads `path` without writing anything next to it, and whether it reads it immutable: a
    database with both its -wal and -shm files is read through them (?mode=ro: the WAL content is included); one
    with a -wal file only (a crash, a copy that skipped the -shm) is read from a private copy of both, since a
    read-only open would create the -shm next to it; one closed cleanly has neither and is read as immutable."""
    wal = os.path.lexists(path.with_name(path.name + "-wal"))
    shm = os.path.lexists(path.with_name(path.name + "-shm"))
    if wal and not shm:
        with tempfile.TemporaryDirectory(prefix="dreamteam-db-") as folder:
            copy = Path(folder) / path.name
            shutil.copyfile(long_path(path), copy)
            shutil.copyfile(long_path(path.with_name(path.name + "-wal")), copy.with_name(copy.name + "-wal"))
            conn = sqlite3.connect(sqlite_uri(copy, "mode=ro"), uri=True, timeout=timeout)
            try:
                yield conn, False
            finally:
                conn.close()
        return
    conn = sqlite3.connect(sqlite_uri(path, "mode=ro" if wal else "immutable=1"), uri=True, timeout=timeout)
    try:
        yield conn, not wal
    finally:
        conn.close()


def _db_state(path: Path) -> tuple[Any, ...]:
    found = []
    for name in (path.name, path.name + "-wal"):
        try:
            st = os.stat(path.with_name(name))
            found.append((st.st_size, st.st_mtime_ns))
        except OSError:
            found.append(None)
    return tuple(found)


def _copy_db(src: Path, dst: Path) -> None:
    """The sqlite backup of `src` into the new file `dst`, read without writing anything next to `src` (_read_db).
    An immutable read sees no other connection: when the file changed meanwhile (a session started), it is read
    again; still changing, the session is refused."""
    for _attempt in range(2):
        before = _db_state(src)
        with _read_db(src) as (source, immutable):
            target = sqlite3.connect(str(dst))
            try:
                source.backup(target)
            finally:
                target.close()
        if not immutable or _db_state(src) == before:
            return
        os.unlink(dst)
    raise _Refused("its database changed while it was copied (it started meanwhile?): stop the session, then "
                   "export again")


def _credential_needles(value: str) -> list[str]:
    """`value` as a database row may hold it: as it is and JSON-escaped (db.dumps, with and without ASCII escapes)."""
    found = [value, json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(value)[1:-1]]
    return [n for i, n in enumerate(found) if n and n not in found[:i]]


def _strip_db_credentials(conn: sqlite3.Connection, credentials: dict[str, str]) -> dict[str, dict[str, int]]:
    """Remove the stripped command credentials `credentials` ({value: MCP server}) from the text values of the
    copied database: the journal (`mcp.created` events), the agents' histories (tool results) and the rest carry
    the command as it was. A value goes, in every form the scan looks for (_stored_forms), only where it stands on
    its own as a command argument or a part of one (between quotes, after `=`, `:` or a space, before a quote,
    `@`, `&`, a space ...), never out of a longer word or path; any other copy stays for the scan, which refuses
    the session then. Values shorter than MIN_SECRET stay (the scan does not look for them either). Returns
    {server: {table: rows changed}}."""
    changed: dict[str, dict[str, int]] = {}
    servers: dict[str, str] = {}
    for value, server in credentials.items():
        if len(value) >= MIN_SECRET:
            for form in _stored_forms(value):
                servers.setdefault(form, server)
    if not servers:
        return changed
    needles = sorted(servers, key=len, reverse=True)
    pattern = re.compile(r"(?<![A-Za-z0-9_.-])(?:" + "|".join(map(re.escape, needles)) + r")(?![A-Za-z0-9_/-]|\.\w)")
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND sql NOT LIKE "
                                         "'CREATE VIRTUAL%' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")]
    any_needle = " OR ".join("instr({c}, ?) > 0" for _ in needles)
    for table in tables:
        columns = [r[0] for r in conn.execute("SELECT name FROM pragma_table_info(?)", (table,))]
        for column in columns:
            col = _quoted(column)
            where = f"typeof({col}) = 'text' AND ({any_needle.format(c=col)})"
            try:
                rows = conn.execute(f"SELECT rowid, {col} FROM {_quoted(table)} WHERE {where}", needles).fetchall()
            except sqlite3.OperationalError:   # a table WITHOUT ROWID (DreamTeam has none): left to the scan
                continue
            for rowid, text in rows:
                found: set[str] = set()

                def cut(match: re.Match[str]) -> str:
                    found.add(servers[match.group(0)])
                    return ""

                cleaned = pattern.sub(cut, text)
                if cleaned == text:
                    continue
                conn.execute(f"UPDATE {_quoted(table)} SET {col} = ? WHERE rowid = ?", (cleaned, rowid))
                for server in found:
                    counts = changed.setdefault(server, {})
                    counts[table] = counts.get(table, 0) + 1
    return changed


def _finish_db(path: Path, sid: str, report: ExportReport, secrets: Secrets,
               credentials: dict[str, str] | None = None) -> dict[str, Any]:
    """Make the copied database self-contained (rollback journal, VACUUMed: no free page keeps deleted text), strip
    credentials from the commands of its configured MCP servers (start() registers them again from the settings of
    the other computer) and from every other place of the copy that holds them (`credentials`: {value: server}
    stripped from the [mcp.external] commands this session uses; also those of every command its journal recorded,
    `mcp.created`: servers removed from the settings since, older passwords), and read its figures."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        stripped = []
        known = dict(credentials or {})
        with contextlib.suppress(sqlite3.Error):
            rows = conn.execute("SELECT name, command FROM mcp_servers WHERE origin = 'config'").fetchall()
            for name, command in rows:
                try:
                    value = loads(command, [])
                except ValueError:
                    continue
                cleaned, creds = strip_command_credentials(value)
                if creds:
                    for cred in creds:
                        secrets.add(cred, f"a credential in the command of MCP server {name} of session {sid}")
                        known.setdefault(cred, str(name))
                    conn.execute("UPDATE mcp_servers SET command = ? WHERE name = ?",
                                 (json.dumps(cleaned, ensure_ascii=False, separators=(",", ":")), name))
                    stripped.append(str(name))
        # the journal keeps every command a server was registered with: also of servers removed from the settings
        # since, or whose command (an older password, another account) changed
        with contextlib.suppress(sqlite3.Error):
            for (data,) in conn.execute("SELECT data FROM events WHERE type = 'mcp.created'"):
                with contextlib.suppress(ValueError, TypeError, RecursionError):
                    value = loads(data, {})
                    server = value.get("server") if isinstance(value, dict) else None
                    if not isinstance(server, dict):
                        continue
                    name = str(server.get("name"))
                    for cred in strip_command_credentials(server.get("command"))[1]:
                        secrets.add(cred, f"a credential in a command of MCP server {name} of session {sid}")
                        known.setdefault(cred, name)
        for name in stripped:
            report.warnings.append(f"session {sid}: re-enter the credentials of MCP server {name} on the other "
                                   "computer (they were removed from its command)")
        try:
            cleaned_servers = _strip_db_credentials(conn, known)
        except sqlite3.Error as exc:
            raise _Refused(f"the credentials of its MCP servers cannot be removed from its database: {exc}") from None
        for name, counts in sorted(cleaned_servers.items()):
            rows = ", ".join(f"{table}: {_plural(n, 'row')}" for table, n in sorted(counts.items()))
            report.warnings.append(f"session {sid}: the credentials of MCP server {name} were removed from its "
                                   f"history (the journal and the agents' messages; {rows})")
        conn.execute("VACUUM")
        check = conn.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise _Refused(f"its database is damaged ({check[0] if check else 'no answer'})")
        stats: dict[str, Any] = {"events": 0, "last_event": None, "mcp_servers": [], "max_tools_rev": None}
        with contextlib.suppress(sqlite3.Error):
            stats["events"] = int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            row = conn.execute("SELECT id, ts, type FROM events ORDER BY id DESC LIMIT 1").fetchone()
            stats["last_event"] = {"id": row[0], "ts": row[1], "type": row[2]} if row else None
        with contextlib.suppress(sqlite3.Error):
            stats["mcp_servers"] = [{"name": r[0], "origin": r[1], "status": r[2]} for r in conn.execute(
                "SELECT name, origin, status FROM mcp_servers ORDER BY name")]
        stats["max_tools_rev"] = max_tools_revision(conn)
    finally:
        conn.close()
    return stats


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    for chunk in _file_chunks(long_path(path)):
        h.update(chunk)
    return h.hexdigest()


def _tree_warnings(result: TreeResult, what: str, report: ExportReport) -> None:
    if result.links:
        report.warnings.append(f"{what}: {_plural(len(result.links), 'link')} not copied ("
                               f"{', '.join(result.links[:3])}{', ...' if len(result.links) > 3 else ''})")
    if result.special:
        report.warnings.append(f"{what}: {_plural(len(result.special), 'special file')} not copied")
    for rel, reason in result.errors[:5]:
        report.warnings.append(f"{what}: {rel} could not be read ({reason}) and is not in the export")
    if len(result.errors) > 5:
        report.warnings.append(f"{what}: {len(result.errors) - 5} more files could not be read")


def _command_credentials(external: Any) -> dict[str, str]:
    """{credential: server} of the [mcp.external] servers `external` (strip_command_credentials)."""
    found: dict[str, str] = {}
    for name, spec in (external.items() if isinstance(external, dict) else ()):
        if isinstance(spec, dict):
            for value in strip_command_credentials(spec.get("command"))[1]:
                found.setdefault(value, str(name))
    return found


def _session_part(manager: SessionManager, item: _Session, out: Path | None, report: ExportReport,
                  secrets: Secrets, running: Collection[str], credentials: dict[str, str] | None = None) -> int:
    """Write (or measure) one session. `credentials`: {value: server} stripped from the global [mcp.external]
    commands; this session's own servers are added (both are removed from its database copy)."""
    sid = item.sid
    data_dir = manager.dir_of(sid) / ".dreamteam"
    raw_overrides = item.meta.get("overrides") or {}
    mcp_overrides = raw_overrides.get("mcp") if isinstance(raw_overrides, dict) else None
    credentials = {**(credentials or {}),
                   **_command_credentials(mcp_overrides.get("external") if isinstance(mcp_overrides, dict) else None)}
    overrides, removed, stripped = scrub_overrides(raw_overrides, sid)
    secrets.update(removed)
    for name in stripped:
        report.warnings.append(f"session {sid}: re-enter the credentials of [mcp.external.{name}] on the other "
                               "computer (they were removed from its command)")
    stored = {k: v for k, v in item.meta.items() if k not in ("id", "legacy")}
    stored["uid"] = item.uid
    stored["overrides"] = overrides
    target = out / SESSIONS_DIR / sid if out is not None else None
    meta_bytes = _json_bytes(stored)
    size = len(meta_bytes)
    entry: dict[str, Any] = {"id": sid, "uid": item.uid, "title": item.meta.get("title") or sid,
                             "created_at": item.meta.get("created_at"), "last_event": None, "events": 0,
                             "max_tools_rev": None, "db_sha256": None, "db_bytes": 0, "attached_skills": [],
                             "mcp_servers": [], "running_at_export": sid in running,
                             "credentials_removed": sorted(stripped)}   # override servers stripped of credentials
    if target is not None:
        os.mkdir(long_path(target))
        _write_bytes(target / SESSION_FILE, meta_bytes)
    db = data_dir / DB_FILE
    if db.is_file():
        if target is not None:
            copied = target / ".dreamteam" / DB_FILE
            copied.parent.mkdir(parents=True, exist_ok=True)
            try:
                _copy_db(db, copied)
                entry.update(_finish_db(copied, sid, report, secrets, credentials))
            except sqlite3.Error as exc:
                raise _Refused(f"its database cannot be read: {exc}") from None
            entry["db_bytes"] = copied.stat().st_size
            entry["db_sha256"] = _sha256(copied)
        else:
            entry["db_bytes"] = sum(p.stat().st_size for p in (db, db.with_name(DB_FILE + "-wal")) if p.is_file())
    size += entry["db_bytes"]
    modes: dict[str, int] = {}
    for part in SESSION_DATA_PARTS:
        folder = data_dir / part
        if is_link(folder) or not folder.is_dir():
            continue
        if target is not None:
            result = copy_tree(folder, target / ".dreamteam" / part, exclude_names=(), exclude_paths=(),
                               modes="export")
            modes.update((f".dreamteam/{part}/{rel}", bits) for rel, bits in result.modes.items())
        else:
            result = scan_tree(folder, exclude_names=(), exclude_paths=(), hash=False)
        _tree_warnings(result, f"session {sid}, .dreamteam/{part}", report)
        size += result.bytes
        if part == "attached-skills":
            with os.scandir(long_path(folder)) as it:
                entry["attached_skills"] = sorted(e.name for e in it if e.is_dir(follow_symlinks=False)
                                                  and not e.name.startswith(".") and not is_link(e))
    # tree_digest: every file (for information); compare_digest: what an import compares, without volatile files;
    # carried False: the folder was missing here, the export holds none of its files
    workspace: dict[str, Any] = {"kind": item.kind, "in_home": item.in_home, "path": item.stored,
                                 "adopted": item.adopted, "files": 0, "bytes": 0, "tree_digest": None,
                                 "compare_digest": None}
    if item.kind == "outside":
        report.not_copied.append({"session": sid, "path": item.stored})
    elif item.folder is None or is_link(item.folder) or not item.folder.is_dir():
        workspace["carried"] = False   # an import keeps the files the session has there
        if item.kind == "in_home":
            report.warnings.append(f"session {sid}: its workspace folder {item.folder} does not exist; the session is "
                                   "exported without workspace files")
    else:
        if target is not None:
            result = copy_tree(item.folder, target / "workspace", exclude_names=EXPORT_EXCLUDE_NAMES,
                               exclude_paths=EXPORT_EXCLUDE_PATHS, modes="export")
            modes.update((f"{WORKSPACE_DIR}/{rel}", bits) for rel, bits in result.modes.items())
            workspace["compare_digest"] = compare_digest(result.entries)
        else:
            result = scan_tree(item.folder, hash=False)
        _tree_warnings(result, f"session {sid}, workspace", report)
        workspace.update(files=result.files, bytes=result.bytes, tree_digest=result.digest)
        size += result.bytes
    entry["workspace"] = workspace
    entry["modes"] = modes   # the executable and read-only files (an export medium may hold no POSIX bits)
    if sid in running:
        report.warnings.append(f"session {sid} is running: it is exported as it was at this moment (its database "
                               "and its files may be a few steps apart)")
    report.sessions.append(entry)
    return size


def _manifest(report: ExportReport, home: Path) -> dict[str, Any]:
    """dreamteam-export.json: what the export holds, per category, and what import checks first (the format, the
    database columns and the tool-set revision this version writes)."""
    return {
        "format": FORMAT, "format_version": FORMAT_VERSION, "dreamteam_version": __version__,
        "created_at": time.time(),
        "source": {"machine": platform.node(), "os": sys.platform, "home": str(home),
                   "python": platform.python_version()},
        "schema": {"db_columns": sorted(known_columns()), "toolset_revision": TOOLSET_REVISION},
        "include": list(report.include),
        "settings": report.settings if "settings" in report.include else None,
        "skills": report.skills if "skills" in report.include else None,
        "mcp": report.mcp if "mcp" in report.include else None,
        "sessions": report.sessions if "sessions" in report.include else None,
        "left_out": report.left_out, "warnings": report.warnings,
        "not_copied": report.not_copied,
    }


def _put_in_place(work: Path, dest: Path, state: str, report: ExportReport) -> None:
    """Move a finished export from its temp folder `work` to `dest` (state "export": swap with the previous one,
    which is restored if the swap fails and removed after it)."""
    if state != "export":
        return
    old = dest.parent / f".{dest.name}.old-{_secrets.token_hex(4)}"
    moved = False
    try:
        _rename(dest, old)
        moved = True
        _rename(work, dest)
    except BaseException as exc:   # also Ctrl+C between the two renames: the previous export goes back
        restored = not moved
        if moved:
            try:
                _rename(old, dest)
                restored = True
            except OSError:
                pass
        if not restored:   # keep the new export in its temp folder: export_home must not discard it
            raise _Kept(f"Nothing exported to {dest}: the previous export is in {old} and the new one in {work}; "
                        f"rename one of them to {dest.name} by hand.") from None
        if isinstance(exc, OSError):
            what = "the previous export in " if not moved else ""
            raise TransferError(f"Nothing exported: {what}{dest} cannot be replaced: {exc.strerror or exc} (a file "
                                "in it is in use?)") from None
        raise
    report.replaced = True
    with contextlib.suppress(OSError, KeyboardInterrupt):   # the new export is in place: done whatever happens
        rmtree(old)
    if os.path.lexists(old):
        report.warnings.append(f"the previous export could not be removed completely: delete {old} by hand")


def export_home(home: str | os.PathLike, dest: str | os.PathLike, *, include: Iterable[str] | None = None,
                sessions: Iterable[str] | None = None, running: Collection[str] | None = None,
                dry_run: bool = False, git: str | None = None) -> ExportReport:
    """Export the DreamTeam home `home` into the folder `dest` (see the module docstring for the layout).

    `include`: categories (CATEGORIES; default all, parse_include). `sessions`: the sessions to export, by id or uid
    (default all; giving any implies the sessions category). `running`: ids of sessions running right now (the
    caller asks the hub): they are exported as a snapshot, with a warning. `dry_run`: plan and measure only,
    nothing is written. `git`: the git program for the history check (default: on PATH).

    `dest` must be missing, an empty folder, or a folder that holds an earlier export (replaced: the new one is built
    next to it and swapped in). The home is only read. Raises TransferError (SecretFound for a secret of this
    computer in the export) with nothing left at `dest`; KeyboardInterrupt removes the partial export as well."""
    home = Path(home).expanduser()
    home = home.resolve() if home.exists() else Path(os.path.abspath(home))
    if not _is_home(home):
        raise TransferError(f"{home} is not a DreamTeam home (no sessions/ folder next to {CONFIG_FILE}).")
    wanted = None if sessions is None else [str(s) for s in sessions]
    categories = parse_include(include)
    if wanted:
        categories = tuple(c for c in CATEGORIES if c in categories or c == "sessions")
    dest = Path(os.path.abspath(Path(dest).expanduser()))
    report = ExportReport(home=home, dest=dest, include=categories, dry_run=dry_run)
    running = set(running or ())
    try:
        raw = read_raw(home)
        config = validate(home, raw)
        manager = SessionManager(home)
    except (OSError, ValueError) as exc:
        raise TransferError(f"Cannot read the settings in {settings_path(home)}: {exc}") from None
    picked = _pick_sessions(manager, wanted, report) if "sessions" in categories else []
    protected = [(home / "sessions", "the sessions folder of the home"),
                 (home / ".dreamteam", "DreamTeam's own data folder of the home")]
    if "skills" in categories:
        protected.append((config.skills_library_dir, "the skills library"))
    if "mcp" in categories:
        protected.append((config.library_dir, "the MCP library"))
    protected += [(s.folder, f"the workspace of session {s.sid}") for s in picked if s.folder is not None]
    session_folders = [(Path(s.stored), f"the workspace of session {s.sid}") for s in picked
                       if s.kind == "outside" and s.stored and Path(s.stored).is_absolute()]
    exported = {s.sid for s in picked}
    for sid in manager.ids():   # also the sessions not exported: their agents commit what is in their folder
        if sid == LEGACY_ID or sid in exported:
            continue
        with contextlib.suppress(SessionError, OSError, ValueError):
            folder, _stored, _adopted = manager._workspace_entry(sid, manager.meta(sid))
            protected.append((folder, f"the workspace of session {sid}"))
    protected += [(folder, what) for folder, what in session_folders]
    state = _check_dest(home, dest, protected, session_folders)
    if dry_run:
        try:
            _export_parts(manager, raw, config, categories, picked, None, report, Secrets(), running)
        except OSError as exc:
            where = f" ({_plain(exc.filename)})" if exc.filename else ""
            raise TransferError(f"Cannot read the home: {exc.strerror or exc}{where}.") from None
        return report
    overrides = {}
    for sid in manager.ids():   # every session's MCP env values, also of sessions left out
        if sid != LEGACY_ID:
            with contextlib.suppress(SessionError):
                overrides[sid] = manager.meta(sid).get("overrides") or {}
    secrets = known_secrets(raw, overrides)
    mcp = raw.get("mcp") if isinstance(raw.get("mcp"), dict) else {}
    credentials = _command_credentials(mcp.get("external"))
    work = dest if state in ("missing", "empty") else dest.parent / f".{dest.name}.export-{_secrets.token_hex(4)}"
    try:   # the destination: a path the user gave (a file on the way, a folder that cannot be written)
        if state == "missing":
            dest.parent.mkdir(parents=True, exist_ok=True)
        else:
            _clean_stale(dest)
        if state == "empty":
            if os.name != "nt":
                with contextlib.suppress(OSError):
                    os.chmod(dest, 0o700)
                st = os.lstat(dest)
                if stat.S_ISLNK(st.st_mode) or st.st_uid != os.geteuid():
                    raise TransferError(f"Nothing exported: {dest} belongs to another user; choose a new folder or "
                                        "one of your own.")
        else:
            _mkdir_private(work)
    except OSError as exc:
        where = f" ({_plain(exc.filename)})" if exc.filename else ""
        raise TransferError(f"Nothing exported: {dest} cannot be written: {exc.strerror or exc}{where}.") from None
    try:
        try:
            _export_parts(manager, raw, config, categories, picked, work, report, secrets, running, credentials)
            places = scan_for_secrets(work, secrets, git=git)
            if places:
                raise SecretFound(places)
            report.manifest = _manifest(report, home)
            tmp = work / f"{MANIFEST}.tmp"
            _write_bytes(tmp, json.dumps(report.manifest, ensure_ascii=False, indent=1).encode("utf-8", "replace"))
            os.replace(long_path(tmp), long_path(work / MANIFEST))
            _put_in_place(work, dest, state, report)
        except OSError as exc:   # e.g. the destination's disk is full, or its file system refuses a name
            where = f" ({_plain(exc.filename)})" if exc.filename else ""
            raise TransferError(f"Nothing exported: {exc.strerror or exc}{where}.") from None
    except _Kept:
        raise
    except BaseException as exc:
        left = _discard(work, keep_folder=state == "empty")
        if left:
            shown = ", ".join(_plain(p) for p in left[:5]) + (", ..." if len(left) > 5 else "")
            note = (f"Could not remove the partial export ({shown}): delete it by hand"
                    + (", it may contain the secret named above." if isinstance(exc, SecretFound) else "."))
            if isinstance(exc, TransferError):
                exc.args = (f"{exc}\n{note}",)
            else:
                print(note, file=sys.stderr)
        raise
    return report


def _plain(path: Any) -> str:
    """A path as messages show it (without the extended-length prefix of Windows)."""
    text = os.fspath(path) if isinstance(path, (str, os.PathLike)) else str(path)
    if text.startswith("\\\\?\\UNC\\"):
        return "\\" + text[7:]
    return text[4:] if text.startswith("\\\\?\\") else text


def _write_refusal(exc: OSError, folder: Path) -> str | None:
    """Why one session cannot be written to the destination (a name, a path or a file size its file system cannot
    hold), or None for an error that stops the whole export (a full disk)."""
    name = exc.filename
    where = ""
    if name:
        text = _plain(name)
        with contextlib.suppress(ValueError):
            text = Path(text).relative_to(folder).as_posix()
        where = f" ({text})"
    code = getattr(exc, "winerror", None)
    if isinstance(exc, FileExistsError):
        return (f"names in it differ only in case{where}, and the destination's file system does not tell them apart: "
                "rename one of them, or leave the session out")
    full = exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)) or code in (112, 39)
    big = False
    size = getattr(exc, "src_size", None)   # treecopy: the size of the file being written
    if isinstance(size, int) and (full or exc.errno == errno.EFBIG):
        big = size > FAT32_MAX
    elif name and (full or exc.errno == errno.EFBIG):
        with contextlib.suppress(OSError, TypeError, ValueError):
            big = os.path.getsize(long_path(Path(_plain(name)))) >= FAT32_MAX - (8 << 20)
    if exc.errno == errno.EFBIG or big:
        return (f"a file of 4 GiB or more{where} does not fit on this destination (FAT32): use one formatted exFAT "
                "or NTFS, or leave the session out")
    if full:
        return None
    if exc.errno in (errno.EINVAL, getattr(errno, "EILSEQ", -1), errno.ENOENT, errno.ENAMETOOLONG) \
            or code in (123, 206, 161, 3):
        return (f"the destination's file system cannot hold this name or path{where} (e.g. ':' '?' '\"' '*', a "
                "trailing dot or space, or a path too long for it): rename it, or leave the session out")
    return None


def _export_parts(manager: SessionManager, raw: dict[str, Any], config, categories: tuple[str, ...],
                  picked: list[_Session], out: Path | None, report: ExportReport, secrets: Secrets,
                  running: set[str], credentials: dict[str, str] | None = None) -> None:
    """Write (or with `out` None, measure) each category, in the manifest's order."""
    if "settings" in categories:
        _settings_part(raw, out, report, secrets)
    if "skills" in categories:
        _skills_part(config.skills_library_dir, Limits.from_config(config), out, report)
    if "mcp" in categories:
        projects = {}
        for sid in manager.ids():
            if sid == LEGACY_ID:
                continue
            with contextlib.suppress(SessionError):
                uid = session_uid(manager.meta(sid))
                if uid:
                    projects[_key(manager.dir_of(sid))] = uid
        _mcp_part(raw, config.library_dir, projects, out, report, secrets)
    if "sessions" in categories:
        total = 0
        if out is not None:
            (out / SESSIONS_DIR).mkdir(exist_ok=True)
        for item in picked:
            mark = (len(report.warnings), len(report.not_copied), len(report.sessions))
            folder = out / SESSIONS_DIR / item.sid if out is not None else None
            try:
                try:
                    total += _session_part(manager, item, out, report, secrets, running, credentials)
                except OSError as exc:   # the destination cannot hold one of its files: this session only
                    reason = _write_refusal(exc, folder) if folder is not None else None
                    if reason is None:
                        raise
                    raise _Refused(reason) from None
            except _Refused as exc:   # this session only: the others go on
                del report.warnings[mark[0]:], report.not_copied[mark[1]:], report.sessions[mark[2]:]
                if folder is not None and os.path.lexists(folder):
                    with contextlib.suppress(OSError):
                        rmtree(folder)
                    if os.path.lexists(folder):
                        report.warnings.append(f"session {item.sid} could not be removed from the export "
                                               f"completely: delete {folder} by hand")
                report.leave_out(f"session {item.sid}", str(exc))
        report.sizes["sessions"] = total


class _Refused(Exception):
    """One session that cannot be exported (the reason is shown); the others are."""


class _Kept(TransferError):
    """An export that could not be put in place, kept where the message says (not discarded)."""


DISCARD_TRIES = 5   # a file held open for a moment (a scanner, an indexer) is tried again
DISCARD_PAUSE = 0.2


def _entries(folder: Path) -> list[Path]:
    try:
        with os.scandir(long_path(folder)) as it:
            return [Path(folder, e.name) for e in it]
    except OSError:
        return []


def _discard(work: Path, *, keep_folder: bool) -> list[Path]:
    """Remove a partial export: the whole temp folder, or the contents of the user's empty folder. Returns what
    could not be removed (tried again a few times)."""
    for attempt in range(DISCARD_TRIES):
        if keep_folder:
            for path in _entries(work):
                with contextlib.suppress(OSError):
                    if path.is_dir() and not is_link(path):
                        rmtree(path)
                    else:
                        os.chmod(long_path(path), stat.S_IWRITE)
                        os.unlink(long_path(path))
            left = _entries(work)
        else:
            with contextlib.suppress(OSError):
                rmtree(work)
            left = [work] if os.path.lexists(work) else []
        if not left:
            return []
        if attempt < DISCARD_TRIES - 1:
            time.sleep(DISCARD_PAUSE)
    return left


# ==== import ====================================================================================================
# settings keys of this computer: an import keeps their value here (global settings and session overrides alike)
LOCAL_KEYS: tuple[tuple[str, str], ...] = (
    ("server", "host"), ("server", "port"), ("shell", "program"), ("skills", "library_dir"), ("skills", "envs_dir"),
    ("skills", "python"), ("mcp", "library_dir"))
# the order apply_import works in: the MCP library last but [mcp.external], since its entries name imported sessions
APPLY_ORDER = ("settings", "skills", "sessions", "mcp")
_SECTION_FIELDS: dict[str, frozenset[str]] = {
    name: frozenset(f.name for f in dataclasses.fields(cls)) for name, cls in (
        ("swarm", SwarmSettings), ("agents", AgentSettings), ("server", ServerSettings), ("shell", ShellSettings),
        ("workspace", WorkspaceSettings), ("mcp", MCPSettings), ("tracing", TracingSettings),
        ("skills", SkillsSettings))}
_PROFILE_FIELDS = frozenset(f.name for f in dataclasses.fields(ModelProfile)) - {"name"}
_ROLE_FIELDS = frozenset(f.name for f in dataclasses.fields(RoleSettings))
_PROFILE_NAME = re.compile(r"[A-Za-z0-9_-]+")
_EMPTY_HEADER = re.compile(r"\s*([A-Za-z0-9_-]+)\s*:\s*")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
_DONE = ("added", "rewritten", "unchanged")
_WINDOWS_BAD = re.compile(r'[<>:"|?*\\\x00-\x1f]')
_WINDOWS_DEVICE = re.compile(r"(?i)(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?")


SMALL_FILE_MAX = 64 << 20   # the manifest, settings, session.json and external.json of an export


def _read_small(path: Path, limit: int = SMALL_FILE_MAX) -> bytes:
    """The bytes of a file of the export that an import reads whole: a plain file (no link) of at most `limit`
    bytes, else ValueError."""
    if is_link(path):
        raise ValueError(f"{path.name} is a link")
    with open(long_path(path), "rb") as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path.name} is larger than {format_size(limit)}")
    return data


def _read_json_small(path: Path) -> Any:
    """The JSON of a file of the export (_read_small); any decoding failure, also a nesting too deep for Python,
    is a ValueError."""
    data = _read_small(path)
    try:
        return json.loads(data.decode("utf-8"))
    except RecursionError:
        raise ValueError("it is nested too deeply") from None


def read_manifest(folder: str | os.PathLike) -> dict[str, Any]:
    """The manifest of the export in `folder`. TransferError ("Nothing imported: ...") for a folder that holds no
    complete export (no manifest: the export did not finish, or it is another folder), a damaged manifest, another
    format, or an export format this version does not know (made by a newer DreamTeam)."""
    folder = Path(os.path.abspath(Path(folder).expanduser()))
    if not folder.is_dir():
        raise TransferError(f"Nothing imported: {folder} is not a folder.")
    path = folder / MANIFEST
    if not path.is_file():
        raise TransferError(f"Nothing imported: {folder} holds no complete DreamTeam export (there is no {MANIFEST}: "
                            "the export did not finish, or this is another folder). Export it again.")
    for name in [*sorted(LAYOUT), LIBRARY_DIR]:
        if is_link(folder / name):
            raise TransferError(f"Nothing imported: {folder / name} is a link; an export holds plain folders and "
                                "files only.")
    try:
        data = _read_json_small(path)
    except (OSError, ValueError) as exc:
        raise TransferError(f"Nothing imported: {path} cannot be read: {exc}") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        raise TransferError(f"Nothing imported: {path} is not the manifest of a DreamTeam export.")
    version = data.get("format_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise TransferError(f"Nothing imported: {path} names no valid export format ({str(version)[:20]}).")
    if version > FORMAT_VERSION:
        made = data.get("dreamteam_version")
        raise TransferError(f"Nothing imported: the export was made by a newer DreamTeam"
                            f"{f' ({made})' if isinstance(made, str) else ''} in export format {version}; this is "
                            f"DreamTeam {__version__} (format {FORMAT_VERSION}). Update DreamTeam here first.")
    mcp = data.get("mcp")
    wrong = [key for key, kind in (("include", list), ("settings", dict), ("skills", list), ("mcp", dict),
                                   ("sessions", list), ("left_out", list), ("warnings", list), ("not_copied", list))
             if data.get(key) is not None and not isinstance(data[key], kind)]
    wrong += [f"mcp.{key}" for key in ("library", "external") if isinstance(mcp, dict) and mcp.get(key) is not None
              and not isinstance(mcp[key], list)]
    if wrong:
        raise TransferError(f"Nothing imported: {path} is damaged ({', '.join(wrong)} of another type).")
    return data


@dataclass
class ImportItem:
    """One thing an import adds or rewrites: the settings, a skill, a session, an MCP library entry or an
    [mcp.external] server. `action` is what the plan decided (add | rewrite | unchanged | skip | refuse, why in
    `reason`); `status` what apply_import did (added | rewritten | unchanged | skipped | refused | failed |
    not done; `error` for a failure). Sessions: `name` is the id in the export, `target` the id here, `backup` the
    folder that keeps the copy a rewrite replaced."""
    category: str
    name: str
    action: str
    reason: str = ""
    target: str | None = None
    warnings: list[str] = field(default_factory=list)
    status: str | None = None
    error: str = ""
    backup: str | None = None
    data: dict[str, Any] = field(default_factory=dict, repr=False)

    def to_json(self) -> dict[str, Any]:
        return {"category": self.category, "name": self.name, "action": self.action, "reason": self.reason,
                "target": self.target, "warnings": list(self.warnings), "status": self.status, "error": self.error,
                "backup": self.backup}


@dataclass
class ImportPlan:
    """What an import would do; plan_import changes nothing. `id_map`: exported session id -> its id here."""
    home: Path
    folder: Path
    manifest: dict[str, Any]
    include: tuple[str, ...] = ()
    items: list[ImportItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    id_map: dict[str, str] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)   # categories `include` names that the export does not hold

    @property
    def changes(self) -> bool:
        """Something would be added or rewritten."""
        return any(item.action in ("add", "rewrite") for item in self.items)

    @property
    def blocked(self) -> bool:
        """Something is skipped or refused."""
        return any(item.action in ("skip", "refuse") for item in self.items)

    @property
    def ok(self) -> bool:
        """Nothing is skipped or refused, and the export holds every category asked for."""
        return not self.blocked and not self.missing

    @property
    def exit_code(self) -> int:
        """For a dry run: 0 when everything can be imported (or is unchanged), 1 when something is not."""
        return 0 if self.ok else 1

    def to_json(self) -> dict[str, Any]:
        return {"home": str(self.home), "folder": str(self.folder), "include": list(self.include),
                "items": [i.to_json() for i in self.items], "warnings": list(self.warnings),
                "id_map": dict(self.id_map), "missing": list(self.missing), "changes": self.changes, "ok": self.ok}


@dataclass
class ImportResult:
    """What apply_import did. `interrupted`: Ctrl+C stopped it after the item it was on (finished or undone); the
    items after it are "not done". `backups`: the folders that keep the copies rewritten sessions replaced."""
    plan: ImportPlan
    items: list[ImportItem]
    warnings: list[str] = field(default_factory=list)
    id_map: dict[str, str] = field(default_factory=dict)
    backups: list[str] = field(default_factory=list)
    removed_backups: list[str] = field(default_factory=list)
    interrupted: bool = False

    @property
    def ok(self) -> bool:
        return not self.interrupted and not self.plan.missing and all(item.status in _DONE for item in self.items)

    @property
    def exit_code(self) -> int:
        """0: everything done or unchanged; 1: something skipped, refused or failed; 130: stopped by Ctrl+C."""
        return 130 if self.interrupted else (0 if self.ok else 1)

    def to_json(self) -> dict[str, Any]:
        return {"home": str(self.plan.home), "folder": str(self.plan.folder),
                "items": [i.to_json() for i in self.items], "warnings": list(self.warnings),
                "id_map": dict(self.id_map), "backups": list(self.backups),
                "removed_backups": list(self.removed_backups), "interrupted": self.interrupted, "ok": self.ok}


# ---- settings ----------------------------------------------------------------------------------------------------
@dataclass
class SettingsMerge:
    """Exported settings merged over this computer's (merge_settings). `update` is what
    SessionManager.update_settings gets: only the sections that change, `models` and `roles` as complete lists
    holding every profile and role of this computer too, so nothing that exists only here is deleted. `new_raw`:
    the settings after it. `error`: why the result cannot be used here (nothing is applied then)."""
    update: dict[str, Any] = field(default_factory=dict)
    new_raw: dict[str, Any] = field(default_factory=dict)
    sections: list[str] = field(default_factory=list)
    models_added: list[str] = field(default_factory=list)
    models_rewritten: list[str] = field(default_factory=list)
    roles_added: list[str] = field(default_factory=list)
    roles_rewritten: list[str] = field(default_factory=list)
    keys_removed: list[str] = field(default_factory=list)   # profiles whose stored key goes: another endpoint now
    local_kept: list[str] = field(default_factory=list)     # machine-local keys whose value here stays
    dropped: list[str] = field(default_factory=list)        # what this version does not know, left out
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.update)


def _known_part(values: dict[str, Any], fields: frozenset[str], where: str, dropped: list[str]) -> dict[str, Any]:
    out = {}
    for key, value in values.items():
        if key in fields:
            out[key] = copy.deepcopy(value)
        else:
            dropped.append(f"{where}.{key}")
    return out


def _profile_endpoint(name: str, spec: dict[str, Any]) -> tuple[str, str | None] | None:
    try:
        return _endpoint(make_profile(name, spec))
    except (ValueError, TypeError):
        return None


def _placeholder_key(spec: dict[str, Any]) -> str | None:
    """The placeholder key of the local-endpoint preset (Ollama, vLLM, LM Studio: settings.PROVIDER_CHOICES) the
    profile `spec` points at; an export leaves it out like every key."""
    endpoint = _profile_endpoint("check", spec)
    for preset in PROVIDER_CHOICES:
        if endpoint is not None and preset.get("api_key") and \
                _endpoint(ModelProfile(provider=preset["provider"], base_url=preset["base_url"])) == endpoint:
            return str(preset["api_key"])
    return None


def _profile_warnings(name: str, spec: Any, environ: Any) -> list[str]:
    """What an imported profile needs here: an API key, or the local server it points at."""
    try:
        profile = make_profile(name, spec)
    except (ValueError, TypeError):
        return []
    provider = (profile.provider or "").strip().lower()
    if provider == "scripted":
        return []
    found = []
    host = None
    with contextlib.suppress(ValueError):
        host = urlsplit(profile.base_url).hostname if profile.base_url else None
    local = host in _LOCAL_HOSTS
    if local:
        found.append(f"model profile '{name}' uses {profile.base_url}: that server must run on this computer")
    if not profile.api_key and not local:
        variable = profile.api_key_env or ("ANTHROPIC_API_KEY" if provider == "anthropic"
                                           else "OPENAI_API_KEY" if not profile.base_url else None)
        if not (variable and environ.get(variable)):
            found.append(f"model profile '{name}' has no API key here: enter the key for profile {name} in Settings"
                         + (f" (or set {variable})" if variable else ""))
    return found


def _secret_entry(key: Any, item: Any) -> bool:
    return _secret_name(key) or bool(isinstance(item, str) and _CREDENTIAL_TEXT.match(item))


def _restore_secret_extra(new: Any, old: Any) -> Any:
    """An exported profile's `extra` with the secret-looking entries of this computer's `old` one (which an export
    leaves out, _scrub_extra) put back where the export has none."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = dict(new)
        for key, item in old.items():
            if _secret_entry(key, item):
                if key not in out:
                    out[key] = copy.deepcopy(item)
            elif key in out:
                out[key] = _restore_secret_extra(out[key], item)
        return out
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        return [copy.deepcopy(o) if n == "" and isinstance(o, str) and _CREDENTIAL_TEXT.match(o)
                else _restore_secret_extra(n, o) for n, o in zip(new, old)]
    return new


def merge_settings(target: dict[str, Any], exported: dict[str, Any], *, home: str | os.PathLike = ".",
                   environ: dict[str, str] | None = None) -> SettingsMerge:
    """The exported settings `exported` (settings/dreamteam.toml of an export) merged over this computer's `target`
    (pure; `home` only for the validation). Sections are deep-merged, the exported values winning, except:
    machine-local keys (LOCAL_KEYS) keep this computer's value; the goal stays (goals belong to sessions); keys
    this version does not know are left out and listed; [mcp.external] is the mcp category's (merge_external).
    Model profiles: missing ones are added, existing ones rewritten from the export, the others stay; the key
    stored here stays only while the profile's endpoint (provider, base_url) is the same, else it is removed
    ("enter the key again"); a local-endpoint preset gets its placeholder key back. Roles (by roles.role_key):
    added or rewritten, the others stay; a role model this computer does not have is dropped."""
    environ = os.environ if environ is None else environ
    m = SettingsMerge()
    target = target if isinstance(target, dict) else {}
    exported = exported if isinstance(exported, dict) else {}
    for name, section in exported.items():
        if name in ("models", "roles"):
            continue
        if name not in _SECTION_FIELDS or not isinstance(section, dict):
            m.dropped.append(f"[{name}]")
            continue
        values = _known_part(section, _SECTION_FIELDS[name], name, m.dropped)
        if name == "mcp":
            values.pop("external", None)
        if name == "swarm":
            values.pop("goal", None)
            values.pop("goal_description", None)
        current = target.get(name) if isinstance(target.get(name), dict) else {}
        for section_name, key in LOCAL_KEYS:
            if section_name == name and key in values:
                if values[key] != current.get(key):
                    m.local_kept.append(f"{name}.{key}")
                del values[key]
        merged = deep_merge(current, values)
        if merged != current:
            m.update[name] = merged
            m.sections.append(name)
    target_models = target.get("models") if isinstance(target.get("models"), dict) else {}
    models = copy.deepcopy(target_models)   # every profile of this computer stays, with its stored key
    exported_models = exported.get("models")
    for name, spec in (exported_models.items() if isinstance(exported_models, dict) else ()):
        if not isinstance(name, str) or not _PROFILE_NAME.fullmatch(name) or not isinstance(spec, dict):
            m.dropped.append(f"models.{name}")
            continue
        spec = _known_part(spec, _PROFILE_FIELDS, f"models.{name}", m.dropped)
        spec.pop("api_key", None)   # an export carries no key, and one written into it is not taken
        variable = spec.get("api_key_env")
        if isinstance(variable, str) and variable.strip() and env_var_name(variable) is None:
            del spec["api_key_env"]
        old = target_models.get(name)
        keep = False
        if isinstance(old, dict):
            # this computer's profile as an export would carry it: without its key, its secret extras and a key
            # variable the export does not name (the source may keep its key elsewhere)
            mine = {k: v for k, v in old.items() if k != "api_key"}
            if isinstance(mine.get("api_key_env"), str) and env_var_name(mine["api_key_env"]) is None:
                del mine["api_key_env"]   # a key typed into the variable field
            if "api_key_env" not in spec:
                mine.pop("api_key_env", None)
            secret_extra: list[str] = []
            if isinstance(mine.get("extra"), (dict, list)):
                mine["extra"] = _scrub_extra(mine["extra"], "extra", secret_extra, Secrets())
                if mine["extra"] in ({}, []):
                    del mine["extra"]
            url_here = old.get("base_url") if isinstance(old.get("base_url"), str) else None
            if url_here is not None:
                mine["base_url"] = strip_url_credentials(url_here)   # an export carries it without them
            if mine == spec:
                continue
            entry = dict(spec)
            if url_here is not None and url_here != mine["base_url"] and spec.get("base_url") == mine["base_url"]:
                entry["base_url"] = url_here   # the same endpoint: the credentials entered here stay
            endpoint = _profile_endpoint(name, entry)
            same = endpoint is not None and endpoint == _profile_endpoint(name, old)
            variable_here = old.get("api_key_env") if env_var_name(old.get("api_key_env")) else None
            if same:   # the credentials set up here for this endpoint stay
                if secret_extra:
                    restored = _restore_secret_extra(entry.get("extra", {} if isinstance(old["extra"], dict)
                                                               else []), old["extra"])
                    if restored not in ({}, []):
                        entry["extra"] = restored
                if variable_here and "api_key_env" not in spec:
                    entry["api_key_env"] = variable_here
            else:
                for place in secret_extra:
                    m.warnings.append(f"model profile '{name}' points at another endpoint now, so {place} "
                                      "configured here was removed: enter it again")
                if variable_here and "api_key_env" not in spec:
                    m.warnings.append(f"model profile '{name}' points at another endpoint now, so the key variable "
                                      f"{variable_here} configured here was removed: enter it again")
            if _stored_key(old):
                if same:
                    keep = True   # (no api_key in the update: apply_update keeps the stored one)
                else:
                    entry["api_key"] = ""   # the key stored here goes only to the endpoint it was saved with
                    m.keys_removed.append(name)
                    m.warnings.append(f"model profile '{name}' points at another endpoint now, so the API key stored "
                                      f"here was removed: enter the key for profile {name} again")
            m.models_rewritten.append(name)
        else:
            entry = dict(spec)
            m.models_added.append(name)
        if not keep and not entry.get("api_key_env"):
            placeholder = _placeholder_key(spec)
            if placeholder:
                entry["api_key"] = placeholder
        models[name] = entry
    if m.models_added or m.models_rewritten:
        m.update["models"] = models
    profiles = set(models) or {"default"}
    exported_roles = exported.get("roles")
    if isinstance(exported_roles, list):
        roles = [copy.deepcopy(r) for r in target["roles"]] if isinstance(target.get("roles"), list) else []
        index = {role_key(r.get("name")): i for i, r in enumerate(roles) if isinstance(r, dict)}
        for number, item in enumerate(exported_roles, 1):
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
                m.dropped.append(f"roles.{number}")
                continue
            role = _known_part(item, _ROLE_FIELDS, f"roles.{item['name']}", m.dropped)
            model = role.get("model")
            if isinstance(model, str) and model and model not in profiles:
                del role["model"]
                m.warnings.append(f"role '{role['name']}' uses the model profile '{model}', which this computer does "
                                  "not have: it uses the default profile here")
            key = role_key(role["name"])
            if key in index:
                if roles[index[key]] == role:
                    continue
                roles[index[key]] = role
                m.roles_rewritten.append(role["name"])
            else:
                index[key] = len(roles)
                roles.append(role)
                m.roles_added.append(role["name"])
        if m.roles_added or m.roles_rewritten:
            m.update["roles"] = roles
    agents = m.update.get("agents")
    default = agents.get("default_model") if isinstance(agents, dict) else None
    if isinstance(default, str) and default not in profiles:
        mine = target.get("agents") if isinstance(target.get("agents"), dict) else {}
        if "default_model" in mine:
            agents["default_model"] = mine["default_model"]
        else:
            del agents["default_model"]
        m.warnings.append(f"the default model profile '{default}' of the export does not exist here: the default "
                          "profile of this computer stays")
        if agents == mine:
            del m.update["agents"]
            m.sections.remove("agents")
    try:
        m.new_raw = apply_update(target, m.update) if m.update else copy.deepcopy(target)
        validate(home, m.new_raw)
    except ValueError as exc:
        m.error = str(exc)
        return m
    new_models = m.new_raw.get("models") if isinstance(m.new_raw.get("models"), dict) else {}
    for name in m.models_added + m.models_rewritten:
        m.warnings.extend(w for w in _profile_warnings(name, new_models.get(name), environ)
                          if name not in m.keys_removed or "no API key" not in w)
    return m


def _blank_credentials(command: Any) -> bool:
    """The command holds arguments an export emptied (strip_command_credentials): a flag's value, a header, a
    variable assignment."""
    parts = command
    if isinstance(command, str):
        try:
            parts = _split_command(command)
        except ValueError:
            return False
    if not isinstance(parts, list):
        return False
    for i, arg in enumerate(parts):
        if not isinstance(arg, str):
            continue
        if arg == "" and i > 0:
            return True
        flag, eq, value = arg.partition("=")
        if eq and not value and (_flag_kind(flag) or (_ENV_ASSIGNMENT.fullmatch(flag) and _variable_kind(flag))):
            return True   # --token=  -Dpassword=  GITHUB_TOKEN=
        header = _EMPTY_HEADER.fullmatch(value if eq and flag.startswith("-") else arg)
        if header and _secret_name(header.group(1)):
            return True   # Authorization:  or  --header=Authorization:
    return False


def merge_external(target: Any, exported: Any, *, keep_others: bool = True, where: str = "Settings"
                   ) -> tuple[dict[str, Any], dict[str, str], dict[str, list[str]]]:
    """[mcp.external] servers of an export (as scrub_external writes them: `env_keys` instead of `env`) merged with
    this computer's servers `target`. command, cwd, description and the other keys come from the export. The env
    values stored here for a server stay (keys only here as well) when its command, credentials aside, and its cwd
    are unchanged, and so does the command with its credentials; otherwise they are dropped. `keep_others`: the
    servers that exist only here stay. `where`: where the user enters values (warnings). Returns (servers,
    {exported name: add | rewrite | unchanged | refuse}, {exported name: warnings})."""
    target = target if isinstance(target, dict) else {}
    out = {k: copy.deepcopy(v) for k, v in target.items()} if keep_others else {}
    actions: dict[str, str] = {}
    notes: dict[str, list[str]] = {}
    for name, spec in (exported.items() if isinstance(exported, dict) else ()):
        warnings = notes.setdefault(name, [])
        if not isinstance(spec, dict):
            actions[name] = "refuse"
            warnings.append(f"[mcp.external.{name}] of the export is not a table")
            continue
        new = {k: copy.deepcopy(v) for k, v in spec.items() if k not in ("env", "env_keys") and v is not None}
        keys = {str(k) for k in _list(spec.get("env_keys")) if isinstance(k, str)}
        if isinstance(spec.get("env"), dict):   # (only the names of values a hand-made file may hold)
            keys |= {str(k) for k in spec["env"]}
        old = target.get(name) if isinstance(target.get(name), dict) else None
        if new.get("description") == "" and "description" not in (old or {}):
            del new["description"]
        env: dict[str, Any] = {}
        told: set[str] = set()
        if old is not None:
            same = _same_program(old, new)
            stored = old.get("env") if isinstance(old.get("env"), dict) else {}
            if same:
                if "command" in old:
                    new["command"] = copy.deepcopy(old["command"])   # with the credentials stored here
                env = copy.deepcopy(stored)
            elif stored:
                told = {str(k) for k in stored}
                warnings.append(f"[mcp.external.{name}]: its command or folder is not the one here, so the env values "
                                f"stored here for it were dropped: enter {', '.join(sorted(stored))} in {where} again")
        missing = sorted(k for k in keys if env.get(k) in (None, "") and k not in told)
        if missing:
            warnings.append(f"enter {', '.join(missing)} for {name} in {where}")
        if _blank_credentials(new.get("command")):
            warnings.append(f"re-enter the credentials of {name} on this computer: the export carries its command "
                            f"without them ({where})")
        if env or (old is not None and old.get("env") == {} and "env" not in new):
            new["env"] = env
        actions[name] = "add" if old is None else ("unchanged" if new == old else "rewrite")
        out[name] = new
    return out, actions, notes


def _command_key(command: Any) -> Any:
    """A server's command as it runs, credentials aside: a string is split as mcp_forge splits it, so "npx -y X"
    and ["npx", "-y", "X"] are the same command."""
    cleaned = strip_command_credentials(command)[0]
    if isinstance(cleaned, str):
        try:
            cleaned = _split_command(cleaned)
        except ValueError:
            return cleaned
    return list(cleaned) if isinstance(cleaned, list) else cleaned


def _same_program(old: dict[str, Any], new: dict[str, Any]) -> bool:
    """Two [mcp.external] entries run the same command (credentials aside) in the same folder."""
    return (_command_key(old.get("command")) == _command_key(new.get("command"))
            and (old.get("cwd") or None) == (new.get("cwd") or None))


def _session_overrides(exported: Any, old: Any, sid: str, global_external: Any = None, flagged: Iterable[str] = ()
                       ) -> tuple[dict[str, Any], list[str]]:
    """An imported session's overrides: sections and keys this version does not know left out, machine-local keys
    (LOCAL_KEYS) as this computer had them (`old`: the overrides of the session it replaces, or None), its
    [mcp.external] servers through merge_external (env values of the replaced copy kept by the same rule; the
    servers `flagged` as stripped of credentials ask for them unless their command here is kept). A
    server the session overrides is merged over this computer's global one of that name (`global_external`): when
    the session runs another command or folder there, the global env values it would inherit are blanked in the
    override (by the same rule: env values go only to the command they were entered for)."""
    warnings: list[str] = []
    exported = exported if isinstance(exported, dict) else {}
    old = old if isinstance(old, dict) else {}
    dropped: list[str] = []
    out: dict[str, Any] = {}
    for section, values in exported.items():
        if section not in OVERRIDE_SECTIONS or not isinstance(values, dict):
            dropped.append(f"[{section}]")
            continue
        values = _known_part(values, _SECTION_FIELDS[section], section, dropped)
        for name, key in LOCAL_KEYS:
            if name == section:
                values.pop(key, None)
        out[section] = values
    for name, key in LOCAL_KEYS:
        if isinstance(old.get(name), dict) and key in old[name] and name in OVERRIDE_SECTIONS:
            out.setdefault(name, {})[key] = copy.deepcopy(old[name][key])
    if dropped:
        warnings.append(f"session {sid}: settings this DreamTeam does not know are left out: {', '.join(dropped)}")
    mcp = out.get("mcp")
    if isinstance(mcp, dict) and "external" in mcp:
        old_mcp = old.get("mcp") if isinstance(old.get("mcp"), dict) else {}
        exported_servers = mcp["external"] if isinstance(mcp["external"], dict) else {}
        servers, _actions, notes = merge_external(old_mcp.get("external"), exported_servers, keep_others=False,
                                                  where=f"the settings of session {sid}")
        mcp["external"] = servers
        old_servers = old_mcp.get("external") if isinstance(old_mcp.get("external"), dict) else {}
        for name in flagged:   # the export stripped their commands (its manifest says so, whatever form they had)
            spec, mine = exported_servers.get(name), old_servers.get(name)
            text = f"re-enter the credentials of {name} on this computer"
            if not isinstance(spec, dict) or any(w.startswith(text) for w in notes.get(name, [])) or (
                    isinstance(mine, dict) and "command" in mine and _same_program(mine, spec)):
                continue
            notes.setdefault(name, []).append(f"{text}: the export carries its command without them (the settings "
                                              f"of session {sid})")
        warnings.extend(w for found in notes.values() for w in found)
        here = global_external if isinstance(global_external, dict) else {}
        for name, spec in servers.items():
            base = here.get(name)
            if not isinstance(spec, dict) or not isinstance(base, dict) or not isinstance(base.get("env"), dict):
                continue
            effective = deep_merge(base, spec)
            own = spec.get("env") if isinstance(spec.get("env"), dict) else {}
            inherited = sorted(k for k, v in base["env"].items() if k not in own and v not in (None, ""))
            if inherited and not _same_program(base, effective):
                spec["env"] = {**own, **{k: "" for k in inherited}}
                warnings.append(f"enter {', '.join(inherited)} for {name} in the settings of session {sid} (it runs "
                                "another command than the server of that name in Settings here, so the values "
                                "stored there are not passed to it)")
    return {k: v for k, v in out.items() if v}, warnings


# ---- the plan -----------------------------------------------------------------------------------------------------
class _Planner:
    def __init__(self, manager: SessionManager, folder: Path, manifest: dict[str, Any], plan: ImportPlan):
        self.manager = manager
        self.folder = folder
        self.manifest = manifest
        self.plan = plan
        self.config = manager.global_config()
        self.models_after: set[str] = set(self.config.models)
        self.reserved_ids: set[str] = set()
        self.reserved_folders: list[Path] = []     # in_home folders this import places sessions in
        self.reserved_outside: list[Path] = []     # folders outside the home sessions of this import work in
        self.planned_uids: set[str] = set()        # the identities this import adds or keeps
        self.uid_targets: dict[str, str] = {}   # uid -> session id here once imported (or already here)
        self.target_metas: dict[str, dict[str, Any]] = {}


def _workspace_kind(meta: Any) -> tuple[str, str | None, str | None, bool]:
    """(own | in_home | outside, the in_home path, the stored path, adopted) of a session.json."""
    workspace = meta.get("workspace") if isinstance(meta, dict) else None
    if not isinstance(workspace, dict) or not isinstance(workspace.get("path"), str) or not workspace["path"].strip():
        return "own", None, None, False
    relative = _home_relative(workspace)
    return ("in_home" if relative else "outside"), relative, workspace["path"], bool(workspace.get("adopted"))


def _last_event(path: Path) -> tuple[Any, ...] | None:
    """(id, ts, type) of the last event of a session database; None without one."""
    if not path.is_file():
        return None
    try:
        with _read_db(path, 10.0) as (conn, _immutable):
            row = conn.execute("SELECT id, ts, type FROM events ORDER BY id DESC LIMIT 1").fetchone()
    except (sqlite3.Error, OSError):
        return None
    return tuple(row) if row else None


def _has_event(path: Path, event: tuple[Any, ...] | None) -> bool:
    if event is None:
        return True
    if not path.is_file():
        return False
    try:
        with _read_db(path, 10.0) as (conn, _immutable):
            return conn.execute("SELECT 1 FROM events WHERE id = ? AND ts IS ? AND type IS ?",
                                event).fetchone() is not None
    except (sqlite3.Error, OSError):
        return False


def _agent_models(path: Path) -> dict[str, list[str]]:
    """{model profile: agent names} of the agents of a session database."""
    found: dict[str, list[str]] = {}
    if not path.is_file():
        return found
    try:
        with _read_db(path, 10.0) as (conn, _immutable):
            for name, model in conn.execute("SELECT name, model FROM agents WHERE model IS NOT NULL AND model != '' "
                                            "ORDER BY name"):
                found.setdefault(str(model), []).append(str(name))
    except (sqlite3.Error, OSError):
        pass
    return found


def _tree(folder: Path) -> str:
    """The comparison digest of a workspace as an export copies it (treecopy.compare_digest: without git's index
    and the like; an empty tree for a missing folder)."""
    if is_link(folder) or not folder.is_dir():
        return compare_digest([])
    try:
        return compare_digest(scan_tree(folder).entries)
    except (NotADirectoryError, OSError):
        return ""


def _target_platform() -> str:
    return sys.platform


def _portability_warnings(folder: Path, sid: str) -> list[str]:
    """Names in an exported workspace this computer's file system cannot hold: names Windows refuses (on Windows),
    names that differ only in case (Windows, macOS). Warned, not refused: copying those files fails the session."""
    platform_name = _target_platform()
    windows = platform_name == "win32"
    if not (windows or platform_name == "darwin") or is_link(folder) or not folder.is_dir():
        return []
    refused: list[str] = []
    clashes: list[str] = []
    for top, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not is_link(Path(top, d))]
        base = Path(top).relative_to(folder)
        seen: dict[str, str] = {}
        for name in sorted(dirs + files):
            rel = (base / name).as_posix()
            if windows and (_WINDOWS_BAD.search(name) or name.endswith((".", " ")) or _WINDOWS_DEVICE.fullmatch(name)):
                refused.append(rel)
            key = unicodedata.normalize("NFC", name).casefold()
            if key in seen:
                clashes.append(f"{(base / seen[key]).as_posix()} and {rel}")
            seen.setdefault(key, name)
    warnings = []
    if refused:
        warnings.append(f"session {sid}: {_plural(len(refused), 'file name')} in its workspace cannot exist on Windows "
                        f"({', '.join(refused[:3])}{', ...' if len(refused) > 3 else ''}): rename them on the other "
                        "computer, or the session cannot be imported here")
    if clashes:
        warnings.append(f"session {sid}: names in its workspace differ only in case ({'; '.join(clashes[:3])}"
                        f"{'; ...' if len(clashes) > 3 else ''}), which this file system cannot hold: rename them "
                        "on the other computer, or the session cannot be imported here")
    return warnings


def _free_id(manager: SessionManager, title: str, reserved: set[str]) -> str:
    """A new session id as SessionManager._new_id makes one, past the ids this import already plans."""
    names = [p.name for p in manager.sessions_dir.iterdir()] if manager.sessions_dir.is_dir() else []
    numbers = [int(m.group(1)) for name in [*names, *reserved] if (m := re.match(r"^(\d+)", name))]
    slug = slugify(title)
    number = max(numbers, default=0) + 1
    sid = f"{number:03d}-{slug}" if slug else f"{number:03d}"
    return sid[:64].rstrip("-")


_SHA_CACHE: dict[tuple[str, int, int], str] = {}   # (path, size, mtime) -> sha256: an import plans twice


def _cached_sha256(path: Path) -> str:
    st = os.stat(path)
    key = (_key(path), st.st_size, st.st_mtime_ns)
    if key not in _SHA_CACHE:
        if len(_SHA_CACHE) > 4096:
            _SHA_CACHE.clear()
        _SHA_CACHE[key] = _sha256(path)
    return _SHA_CACHE[key]


def _check_export_db(path: Path, entry: dict[str, Any]) -> str | None:
    """Why the database of an exported session cannot be imported here, or None."""
    expected = entry.get("db_sha256")
    if not os.path.lexists(path):
        return "its database is missing from the export" if expected else None
    if is_link(path) or not path.is_file():
        return "its database in the export is not a file"
    if not isinstance(expected, str):   # an export always names it: the copy is checked against it
        return "the manifest of the export names no checksum of its database (it was changed by hand)"
    if any(os.path.lexists(path.with_name(path.name + s)) for s in ("-wal", "-shm", "-journal")):
        return ("its database in the export has a -wal, -shm or -journal file next to it, which an export never "
                "writes (it was opened or changed since)")
    try:
        if _cached_sha256(path) != expected:
            return "its database is not the one the export wrote (it changed or was damaged since)"
        unknown = unknown_columns(path)
        revision = max_tools_revision(path)
        foreign = foreign_objects(path)
    except (sqlite3.Error, OSError) as exc:
        return f"its database cannot be read: {exc}"
    if unknown:
        return (f"it was made by a newer DreamTeam (its database has {', '.join(unknown[:5])}"
                f"{', ...' if len(unknown) > 5 else ''}): update DreamTeam here")
    if foreign:
        return (f"its database has {', '.join(foreign[:5])}{', ...' if len(foreign) > 5 else ''}, which DreamTeam "
                "does not create (another program changed it, or a newer DreamTeam made it)")
    if revision is not None and revision > TOOLSET_REVISION:
        return (f"its agents use the tool set {revision}, which this DreamTeam does not know (it knows "
                f"{TOOLSET_REVISION}): update DreamTeam here")
    return None


def _plan_settings(p: _Planner, raw: dict[str, Any]) -> dict[str, Any]:
    """The settings item; returns the settings as they would be after it."""
    item = ImportItem("settings", "dreamteam.toml", "refuse")
    p.plan.items.append(item)
    path = p.folder / SETTINGS_FILE
    try:
        exported = tomllib.loads(_read_small(path).decode("utf-8"))
    except FileNotFoundError:
        item.reason = f"{SETTINGS_FILE} is missing from the export"
        return raw
    except (OSError, ValueError, RecursionError) as exc:
        item.reason = f"{SETTINGS_FILE} of the export cannot be read: {exc}"
        return raw
    m = merge_settings(raw, exported, home=p.manager.home)
    item.warnings.extend(m.warnings)
    if m.dropped:
        item.warnings.append(f"settings this DreamTeam does not know are left out: {', '.join(m.dropped)}")
    if m.error:
        item.reason = f"the settings cannot be used here: {m.error}"
        return raw
    item.data["exported"] = exported
    item.action = "rewrite" if m.changed else "unchanged"
    listed = p.manifest.get("settings") if isinstance(p.manifest.get("settings"), dict) else {}
    after = m.new_raw.get("models") if isinstance(m.new_raw.get("models"), dict) else {}
    for place in _list(listed.get("omitted")):
        name = place[len("models."):-len(f".{BASE_URL_CREDENTIALS}")] if isinstance(place, str) and \
            place.startswith("models.") and place.endswith(f".{BASE_URL_CREDENTIALS}") else None
        url = after.get(name, {}).get("base_url") if isinstance(after.get(name), dict) else None
        if isinstance(url, str) and strip_url_credentials(url) == url:
            item.warnings.append(f"re-enter the credentials in the base_url of model profile '{name}' on this "
                                 "computer: the export carries it without them")
    parts = []
    for label, names in (("profiles added", m.models_added), ("profiles rewritten", m.models_rewritten),
                         ("roles added", m.roles_added), ("roles rewritten", m.roles_rewritten),
                         ("sections", m.sections), ("kept as they are here", m.local_kept)):
        if names:
            parts.append(f"{label}: {', '.join(names)}")
    item.reason = "; ".join(parts)
    return m.new_raw if m.changed else raw


def _plan_skills(p: _Planner) -> None:
    root = p.config.skills_library_dir
    limits = Limits.from_config(p.config)
    listed = p.manifest.get("skills") or []
    for name in [e.get("name") for e in listed if isinstance(e, dict) and isinstance(e.get("name"), str)]:
        item = ImportItem("skills", name, "refuse")
        p.plan.items.append(item)
        src = p.folder / SKILLS_DIR / name
        if not (len(name) <= NAME_MAX and NAME_RE.fullmatch(name)):
            item.reason = "not a valid skill name"
            continue
        if is_link(src) or not src.is_dir():
            item.reason = "its folder is missing from the export"
            continue
        try:
            info = check_skill_dir(src, limits)
        except SkillError as exc:
            item.reason = str(exc)
            continue
        if info.name != name:
            item.reason = f"its SKILL.md names it '{info.name}'"
            continue
        meta = _read_json(src / SKILL_META) or {}
        origin = meta.get("origin") if meta.get("origin") in ORIGINS else "folder"
        source = meta.get("source") if isinstance(meta.get("source"), dict) else {}
        item.data.update(src=src, origin=origin, source=source, by=str(meta.get("by") or "user"))
        here = root / name
        if is_link(here):
            item.reason = f"the skills library here has a link of that name ({here}): remove it first"
            continue
        if not os.path.lexists(here):
            item.action = "add"
            continue
        try:
            same = check_skill_dir(here, limits).digest == info.digest
        except SkillError:
            same = False
        item.action = "unchanged" if same else "rewrite"
        if not same:
            item.reason = "its files differ"


def _plan_sessions(p: _Planner, wanted: list[str] | None, raw_after: dict[str, Any],
                   target_metas: dict[str, dict[str, Any]], target_uids: dict[str, list[str]]) -> None:
    listed = [e for e in p.manifest.get("sessions") or [] if isinstance(e, dict) and isinstance(e.get("id"), str)]
    entries: dict[str, dict[str, Any]] = {}
    metas: dict[str, dict[str, Any]] = {}
    problems: dict[str, str] = {}
    export_uids: dict[str, list[str]] = {}
    for entry in listed:
        eid = entry["id"]
        entries[eid] = entry
        if not SESSION_ID.match(eid) or eid == LEGACY_ID:
            problems[eid] = "not a valid session id"
            continue
        if is_link(p.folder / SESSIONS_DIR / eid):
            problems[eid] = "its folder in the export is a link"
            continue
        try:
            meta = _read_json_small(p.folder / SESSIONS_DIR / eid / SESSION_FILE)
            if not isinstance(meta, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as exc:
            problems[eid] = f"its {SESSION_FILE} in the export cannot be read ({exc})"
            continue
        uid = session_uid(meta)
        if uid is None or (isinstance(entry.get("uid"), str) and entry["uid"].lower() != uid):
            problems[eid] = f"its {SESSION_FILE} in the export has no identity (uid) that matches the manifest"
            continue
        metas[eid] = meta
        export_uids.setdefault(uid, []).append(eid)
    chosen: list[str] = []
    named: set[str] = set()
    for text in (list(entries) if wanted is None else wanted):
        if text in entries:
            match = text
            if wanted is not None:
                named.add(text)
        else:
            found = [eid for eid, meta in metas.items() if session_uid(meta) == text.lower()]
            if not found:
                raise TransferError(f"Nothing imported: the export holds no session '{text}' (give its id or uid).",
                                    usage=True)
            if len(found) > 1:
                raise TransferError(f"Nothing imported: sessions {' and '.join(found)} of the export have the "
                                    f"identity {text}: give one of their ids.", usage=True)
            match = found[0]
        if match not in chosen:
            chosen.append(match)
    claimed: dict[str, str] = {}
    for eid in chosen:
        item = _plan_session(p, eid, entries[eid], metas.get(eid), problems.get(eid), export_uids, eid in named,
                             target_metas, target_uids, claimed, raw_after)
        p.plan.items.append(item)
        if item.action in ("add", "rewrite", "unchanged") and item.target:
            p.plan.id_map[eid] = item.target
            p.uid_targets[item.data["uid"]] = item.target


def _plan_session(p: _Planner, eid: str, entry: dict[str, Any], meta: dict[str, Any] | None, problem: str | None,
                  export_uids: dict[str, list[str]], named: bool, target_metas: dict[str, dict[str, Any]],
                  target_uids: dict[str, list[str]], claimed: dict[str, str], raw_after: dict[str, Any]) -> ImportItem:
    manager = p.manager
    item = ImportItem("sessions", eid, "refuse")

    def refuse(reason: str) -> ImportItem:
        item.action, item.reason = "refuse", reason
        return item

    if problem or meta is None:
        return refuse(problem or "it cannot be read")
    src = p.folder / SESSIONS_DIR / eid
    uid = session_uid(meta)
    item.data["uid"] = uid
    linked = _linked_part(src)
    if linked:
        return refuse(f"its {linked} folder in the export is a link; an export holds plain folders and files only")
    if os.path.lexists(src / ".dreamteam" / MOVE_JOURNAL):
        return refuse("moving its workspace did not finish on the other computer: put its files together there in "
                      f"the folder the session uses and delete .dreamteam/{MOVE_JOURNAL} (starting it says where), "
                      "then export it again")
    copied = len(export_uids.get(uid, [])) > 1   # a folder copied by hand on the other computer
    if copied and not named:
        return refuse(f"sessions {' and '.join(export_uids[uid])} of the export have the same identity (one was copied "
                      "by hand): import them by id with --session ID; each is matched by its id here, and one that "
                      "is added gets a new identity")
    reason = _check_export_db(src / ".dreamteam" / DB_FILE, entry)
    if reason:
        return refuse(reason)
    targets = target_uids.get(uid, [])
    target = None
    fresh_uid = None
    origin = str(meta.get("origin_uid") or "").lower()
    if copied:   # matched by its id, never by the identity the copies share
        here = target_metas.get(eid)
        if here is not None and uid in (session_uid(here), _origin_uid(here)):
            target = eid
        elif targets or uid in p.planned_uids:
            fresh_uid = new_uid()
    elif not targets and origin and uid not in p.planned_uids \
            and target_metas.get(eid) is not None and session_uid(target_metas[eid]) == origin:
        target = eid   # a copy that got its own identity on the other computer comes back to the copy it was made of
    elif len(targets) > 1:
        if not (named and eid in targets):
            if eid in targets:
                return refuse(f"sessions {' and '.join(targets)} here have the identity of session {eid} (one was "
                              f"copied by hand): import it with --session {eid}, which rewrites {eid} here")
            return refuse(f"sessions {' and '.join(targets)} here have the identity of session {eid} (one was copied "
                          "by hand): delete or move the copy you do not want here, then import again")
        target = eid
    elif targets:
        target = targets[0]
    if target is not None and target in claimed:
        return refuse(f"session {claimed[target]} of this import is the same session (it rewrites {target} here)")
    title = str(meta.get("title") or eid)
    kind, relative, stored, adopted = _workspace_kind(meta)
    old = target_metas.get(target) if target is not None else None
    old_kind, old_relative, old_stored, old_adopted = _workspace_kind(old)
    old_in_home = manager.home / old_relative if old is not None and old_relative else None
    db = src / ".dreamteam" / DB_FILE
    mcp = raw_after.get("mcp") if isinstance(raw_after.get("mcp"), dict) else {}
    overrides, notes = _session_overrides(meta.get("overrides"), old.get("overrides") if old else None, eid,
                                          mcp.get("external"),
                                          [n for n in _list(entry.get("credentials_removed")) if isinstance(n, str)])
    try:   # exactly what the import writes, so that a re-import of the same export is unchanged
        overrides, dropped_model = manager.import_overrides(overrides, p.models_after)
    except SessionError as exc:
        return refuse(f"its settings cannot be used here: {exc}")
    exported_workspace = entry.get("workspace") if isinstance(entry.get("workspace"), dict) else {}
    carried = exported_workspace.get("carried") is not False   # False: its folder was missing on the other computer
    if target is None:
        sid = eid if not os.path.lexists(manager.dir_of(eid)) and eid not in p.reserved_ids \
            else _free_id(manager, title, p.reserved_ids)
        item.action, item.target = "add", sid
        if sid != eid:
            item.reason = f"as {sid}: {eid} is taken here"
        if fresh_uid:
            item.reason = "; ".join(filter(None, [item.reason, "with a new identity: a session with the same one "
                                                               "is here or in this import"]))
    else:
        sid = item.target = target
        if os.path.lexists(manager.dir_of(sid) / ".dreamteam" / MOVE_JOURNAL):
            return refuse("moving its workspace did not finish here (its files may be in two folders): put them "
                          f"together in the folder the session uses and delete .dreamteam/{MOVE_JOURNAL} (starting "
                          "it says where), then import again")
        if sid in manager.running:
            item.action, item.reason = "skip", "it is running here: stop it first"
            return item
        here_db = manager.dir_of(sid) / ".dreamteam" / DB_FILE
        mine, theirs = _last_event(here_db), _last_event(db)
        here_files = old_in_home if old_kind == "in_home" and old_in_home is not None \
            else manager.default_workspace(sid)

        def files_match() -> bool:
            if old_kind == "outside":
                return True   # the session keeps its folder here; the export's files do not go there
            if kind == "outside":
                return False
            if not carried:
                return True   # the export has no files of it: the ones here stay
            exported = exported_workspace.get("compare_digest")
            return (exported or _tree(src / WORKSPACE_DIR)) == _tree(here_files)

        if mine == theirs and files_match():   # the same history and files: only session.json may differ
            differ = _settings_differ(meta, old, overrides)
            if not differ:
                item.action = "unchanged"
                claimed[sid] = eid
                p.planned_uids.add(uid)
                return item
            item.action = "rewrite"
            item.reason = (f"its {' and '.join(differ)} differ{'s' if len(differ) == 1 else ''}: the export's replace "
                           "the ones here, which may be newer (only its session.json is written; no copy is kept)")
            item.data["settings_only"] = True
        elif not carried and kind != "outside" and old_kind != "outside" \
                and _tree(here_files) != compare_digest([]):
            return refuse("the export carries no workspace files of it (its folder was missing on the other "
                          f"computer), and a rewrite would replace the files in {here_files}: export it again with "
                          "its folder in place")
        else:
            kept = f"the old copy is kept in sessions/{REPLACED_PREFIX}{sid}-<time>"
            if mine == theirs:
                why = "its files differ"
            elif _has_event(db, mine):
                why = "the export has newer work in it"
            elif _has_event(here_db, theirs):
                why = "this computer has newer work in it"
            else:
                why = "both computers worked on it since they last had the same copy"
            item.action, item.reason = "rewrite", f"{why}; {kept}"
            item.data["why"] = why
    if item.data.get("settings_only"):
        workspace = ImportedWorkspace("own")
    else:
        try:
            workspace = _placement(p, item, sid, src, kind, relative, stored, adopted, old, old_kind, old_stored,
                                   old_adopted, old_in_home, old_relative)
        except _Refused as exc:
            return refuse(str(exc))
        if workspace.kind != "outside" or old_kind == "outside":
            item.warnings.extend(_portability_warnings(src / WORKSPACE_DIR, sid))
    item.warnings.extend(w.replace(f"session {eid}", f"session {sid}") if sid != eid else w for w in notes)
    if not carried and item.action == "add":
        item.warnings.append(f"session {sid}: the export has no workspace files of it (its folder was missing on the "
                             "other computer)")
    if dropped_model:
        item.warnings.append(f"session {sid}: its model profile '{dropped_model}' does not exist here, so it uses "
                             "the default profile")
    problem = _check_session_settings(p, raw_after, sid, overrides)
    if problem:
        return refuse(problem)
    for model, names in _agent_models(db).items():
        if model not in p.models_after:
            item.warnings.append(f"session {sid}: {', '.join(names)} use{'s' if len(names) == 1 else ''} the model "
                                 f"profile '{model}', which this computer does not have: the default profile is used")
    new_meta = {k: copy.deepcopy(v) for k, v in meta.items() if k not in ("id", "legacy", "workspace_notice")}
    # a copy here that got its own identity when it was added (origin_uid: the one it shares) keeps it
    kept_uid = session_uid(old) if old is not None and session_uid(old) != uid and _origin_uid(old) == uid else None
    new_meta.update(uid=fresh_uid or kept_uid or uid, overrides=overrides)
    if fresh_uid or kept_uid:
        new_meta["origin_uid"] = uid   # matched by the copy's id when it is imported again (--session)
    item.data.update(src=src, meta=new_meta, workspace=workspace, expect=old, title=title,
                     db_sha256=entry.get("db_sha256"), modes=entry.get("modes"))
    p.reserved_ids.add(sid)
    p.planned_uids.add(uid)
    claimed[sid] = eid
    if workspace.kind == "in_home" and workspace.folder is not None:
        p.reserved_folders.append(workspace.folder)
    elif workspace.kind == "outside" and workspace.path and Path(workspace.path).is_absolute():
        p.reserved_outside.append(Path(workspace.path))
    return item


def _settings_differ(meta: dict[str, Any], old: dict[str, Any] | None, overrides: dict[str, Any]) -> list[str]:
    """What the exported session.json `meta` brings that the target's `old` does not have: its "title", its
    "settings" (as the import writes them, `overrides`), its "pending goal" (empty: nothing)."""
    old = old or {}
    return [name for name, same in (("title", meta.get("title") == old.get("title")),
                                    ("settings", overrides == (old.get("overrides") or {})),
                                    ("pending goal", meta.get("pending_goal") == old.get("pending_goal"))) if not same]


def _origin_uid(meta: Any) -> str:
    """The identity a session copy was made from (`origin_uid`: it got its own when it was added), or ""."""
    return str(meta.get("origin_uid") or "").lower() if isinstance(meta, dict) else ""


def _linked_part(src: Path) -> str | None:
    """The folder of an exported session (`src`: sessions/<id>) that is a link, which an import never follows:
    ".dreamteam", one of its parts, or "workspace"; None when there is none."""
    for rel in (".dreamteam", *(f".dreamteam/{part}" for part in SESSION_DATA_PARTS), WORKSPACE_DIR):
        if is_link(src.joinpath(*rel.split("/"))):
            return rel
    return None


def _placement(p: _Planner, item: ImportItem, sid: str, src: Path, kind: str, relative: str | None,
               stored: str | None, adopted: bool, old: dict[str, Any] | None, old_kind: str, old_stored: str | None,
               old_adopted: bool, old_in_home: Path | None, old_relative: str | None = None) -> ImportedWorkspace:
    """Where the imported session works here: a rewrite keeps a folder outside the home the session works in here,
    and its folder inside the home; an in_home folder goes to the same place in this home, or to a `<folder>-<id>`
    sibling when that is taken. A folder outside the home that exists here is checked as /cd checks one (never the
    home, the user's home, a library, ...; never another session's folder) and becomes the user's project
    (adopted: DreamTeam never configures its repository) unless it is empty."""
    manager = p.manager
    if old is not None and old_kind == "outside" and old_stored:
        if kind != "outside" and (src / WORKSPACE_DIR).is_dir() and any((src / WORKSPACE_DIR).iterdir()):
            item.warnings.append(f"session {sid} keeps working in {old_stored} on this computer; the workspace files "
                                 f"of the export go to its own folder {manager.default_workspace(sid)}")
        return ImportedWorkspace("outside", path=old_stored, adopted=old_adopted)
    if kind == "own":
        return ImportedWorkspace("own")
    others = [*manager._other_workspaces(sid), *(("of this import", f) for f in p.reserved_folders),
              *(("of this import", f) for f in p.reserved_outside)]
    if kind == "outside" and stored:
        folder = Path(stored)
        if not folder.is_absolute():
            item.warnings.append(f"session {sid} works in {stored}, which is not on this computer: put the project at "
                                 "that path, or run /cd in the session")
            return ImportedWorkspace("outside", path=stored, adopted=adopted)
        for other, theirs in others:
            if _inside(folder, theirs) or _inside(theirs, folder):
                raise _Refused(f"it works in {stored}, which is the folder of session {other} ({theirs}), or inside "
                               "it or contains it: two sessions cannot share files; run /cd in it on the other "
                               f"computer, or import it after moving session {other}")
        if _inside(folder, p.folder) or _inside(p.folder, folder):
            raise _Refused(f"it works in {stored}, which overlaps the export folder {p.folder}")
        if not folder.is_dir():
            if os.path.lexists(folder):
                raise _Refused(f"it works in {stored}, which is a file on this computer")
            item.warnings.append(f"session {sid} works in {stored}, which is not on this computer: put the project at "
                                 "that path, or run /cd in the session")
            return ImportedWorkspace("outside", path=stored, adopted=adopted)
        try:
            real = folder.resolve()
            refusal = manager._target_refusal(real, sid)
            empty = next(real.iterdir(), None) is None
        except (OSError, RuntimeError) as exc:
            raise _Refused(f"its folder {stored} cannot be read here: {exc}") from None
        if refusal is not None:
            raise _Refused(f"it works in a folder no session may work in here: {refusal}")
        if not empty:   # the files there are this computer's: the user's project, whatever the export says
            item.reason = "; ".join(filter(None, [item.reason, f"works in {stored}, an existing folder here: adopted "
                                                               "as the user's project"]))
            return ImportedWorkspace("outside", path=stored, adopted=True)
        item.reason = "; ".join(filter(None, [item.reason, f"works in {stored} (an empty folder here)"]))
        return ImportedWorkspace("outside", path=stored, adopted=adopted)
    assert relative is not None
    home = manager.home
    names = [relative, f"{relative}-{sid}", *(f"{relative}-{sid}-{n}" for n in range(2, 100))]
    if old_kind == "in_home" and old_relative and old_in_home is not None:
        names = [old_relative, *(n for n in names if n != old_relative)]   # a rewrite keeps the folder it has here
    for name in names:
        candidate = home / name
        if not _inside(candidate, home) or _key(candidate) == _key(home):
            raise _Refused(f"its workspace folder {name} in the export is not a folder inside the home")
        if _inside(candidate, home / SESSIONS_DIR) or _inside(candidate, home / ".dreamteam") \
                or _inside(home / SESSIONS_DIR, candidate):
            raise _Refused(f"its workspace folder {candidate} would be inside DreamTeam's own folders of this home")
        if _inside(candidate, p.folder) or _inside(p.folder, candidate):
            raise _Refused(f"its workspace folder {candidate} would overlap the export folder {p.folder}")
        mine = old_in_home is not None and _key(candidate) == _key(old_in_home)
        if (os.path.lexists(candidate) and not mine) or any(_key(candidate) == _key(f) for f in p.reserved_folders):
            continue   # taken: the next sibling
        for other, folder in others:
            if _inside(candidate, folder) or _inside(folder, candidate):
                raise _Refused(f"its workspace folder {candidate} would be inside the folder of session {other} "
                               f"({folder}) or contain it: two sessions cannot share files")
        try:
            refusal = manager._target_refusal(Path(os.path.abspath(candidate)).resolve(), sid)
        except (OSError, RuntimeError) as exc:
            refusal = str(exc)
        if refusal is not None:
            raise _Refused(f"its workspace folder {candidate} cannot be used here: {refusal}")
        if old_in_home is not None and not mine:
            item.warnings.append(f"session {sid}: its workspace moves from {old_in_home} to {candidate}")
        elif name != relative and not mine:
            item.warnings.append(f"session {sid}: {home / relative} is taken here, so its workspace goes to "
                                 f"{candidate}")
        return ImportedWorkspace("in_home", folder=candidate, adopted=adopted)
    raise _Refused(f"no free folder for its workspace next to {home / relative}")


def _check_session_settings(p: _Planner, raw_after: dict[str, Any], sid: str, overrides: dict[str, Any]
                            ) -> str | None:
    """Why the session's overrides (SessionManager.import_overrides) cannot be used with this home's settings (as
    the import leaves them), or None."""
    try:
        validate(p.manager.dir_of(sid), p.manager._merged(raw_after, overrides))
    except ValueError as exc:
        return f"its settings cannot be used here: {exc}"
    return None


def _plan_mcp(p: _Planner, raw_after: dict[str, Any]) -> None:
    listed = p.manifest.get("mcp") if isinstance(p.manifest.get("mcp"), dict) else {}
    root = p.config.library_dir
    for name in [e.get("name") for e in listed.get("library") or [] if isinstance(e, dict)
                 and isinstance(e.get("name"), str)]:
        item = ImportItem("mcp", f"library {name}", "refuse", data={"kind": "library", "server": name})
        p.plan.items.append(item)
        src = p.folder / LIBRARY_DIR / name
        try:
            entry = mcp_library.read_folder(src, name)
            mcp_library.check_requirements(entry.meta.get("requirements") or [])
        except mcp_library.LibraryError as exc:
            item.reason = str(exc)
            continue
        uid = entry.meta.get("project_session")
        uid = uid.lower() if isinstance(uid, str) else None
        item.data.update(src=src, project_session=uid)
        here = root / name
        current = mcp_library.read_entry(root, name)
        updates, drop = _project_updates(p.manager, uid, p.uid_targets.get(uid) if uid else None,
                                         current.meta.get("project") if current is not None else None)
        expected = {k: v for k, v in {**entry.meta, **updates}.items() if k not in drop}
        if entry.requirements and shutil.which("uv") is None:
            item.warnings.append(f"MCP server {name} needs packages ({', '.join(entry.requirements[:3])}"
                                 f"{', ...' if len(entry.requirements) > 3 else ''}): install uv on this computer")
        if is_link(here):
            item.action, item.reason = "rewrite", "a link of that name in the library here is replaced (the link only)"
            continue
        if not os.path.lexists(here):
            item.action = "add"
            continue
        if current is None:
            item.action, item.reason = "rewrite", "the entry here cannot be read"
            continue
        if current.approved and current.digest == entry.digest and expected.get("approved_digest") != entry.digest:
            expected["approved_digest"] = entry.digest
        if current.code == entry.code and current.meta == expected:
            item.action = "unchanged"
        else:
            item.action = "rewrite"
            item.reason = ("other code" if current.code != entry.code
                           else "its project folder changes" if current.meta.get("project") != expected.get("project")
                           else "its description or approval differ")
    for item in p.plan.items:   # what an approval brought from the other computer lets agents do here
        if item.data.get("kind") != "library" or item.action not in ("add", "rewrite") or "src" not in item.data:
            continue
        with contextlib.suppress(mcp_library.LibraryError):
            entry = mcp_library.read_folder(item.data["src"], item.data["server"])
            if entry.meta.get("approved_digest") == entry.digest:
                item.reason = "; ".join(filter(None, [item.reason, "approved on the other computer"]))
                if p.config.mcp.require_approval:
                    item.warnings.append(f"MCP server {item.data['server']} is approved in the export, so agents "
                                         "here can run it without asking (import only your own exports)")
    servers = listed.get("external") or []
    if not servers:
        return
    try:
        exported = _read_json_small(p.folder / EXTERNAL_FILE)
        if not isinstance(exported, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        for server in servers:
            if isinstance(server, dict) and isinstance(server.get("name"), str):
                p.plan.items.append(ImportItem("mcp", f"external {server['name']}", "refuse",
                                               f"{EXTERNAL_FILE} of the export cannot be read ({exc})",
                                               data={"kind": "external", "server": server["name"]}))
        return
    mcp = raw_after.get("mcp") if isinstance(raw_after.get("mcp"), dict) else {}
    here = mcp.get("external") if isinstance(mcp.get("external"), dict) else {}
    _servers, actions, notes = merge_external(here, exported)
    # the servers whose command the export stripped of credentials (its manifest says so, whatever form they had)
    flagged = {e["name"] for e in servers if isinstance(e, dict) and isinstance(e.get("name"), str)
               and e.get("credentials_removed") is True}
    for name, spec in exported.items():
        item = ImportItem("mcp", f"external {name}", actions.get(name, "refuse"), warnings=list(notes.get(name, [])),
                          data={"kind": "external", "server": name, "spec": spec})
        if item.action == "refuse":
            item.reason = "not a table"
        old = here.get(name) if isinstance(here.get(name), dict) else None
        kept_here = old is not None and "command" in old and isinstance(spec, dict) and _same_program(old, spec)
        if name in flagged and item.action in ("add", "rewrite") and not kept_here:
            text = (f"re-enter the credentials of {name} on this computer: the export carries its command without "
                    "them (Settings)")
            if text not in item.warnings:
                item.warnings.append(text)
        if item.action in ("add", "rewrite") and isinstance(spec, dict):
            new_text = _command_text(spec)
            if old is None:
                item.reason = f"runs {new_text}"
            elif not _same_program(old, spec):
                item.reason = f"command changes: {_command_change(old, spec)}"
                for sid, meta in sorted(p.target_metas.items()):   # a session override with env values only
                    overrides = meta.get("overrides") if isinstance(meta.get("overrides"), dict) else {}
                    ext = (overrides.get("mcp") or {}).get("external") if isinstance(overrides.get("mcp"), dict) \
                        else None
                    mine = ext.get(name) if isinstance(ext, dict) else None
                    if isinstance(mine, dict) and mine.get("env") and "command" not in mine and "cwd" not in mine:
                        item.warnings.append(f"session {sid} keeps its env values for {name} ("
                                             f"{', '.join(sorted(mine['env']))}): they now go to the new command")
        command = spec.get("command") if isinstance(spec, dict) else None
        program = command[0] if isinstance(command, list) and command and isinstance(command[0], str) else None
        if program and shutil.which(program) is None:
            item.warnings.append(f"[mcp.external.{name}]: {program} is not on this computer's PATH")
        cwd = spec.get("cwd") if isinstance(spec, dict) else None
        if isinstance(cwd, str) and Path(cwd).is_absolute() and not Path(cwd).is_dir():
            item.warnings.append(f"[mcp.external.{name}]: its folder {cwd} does not exist on this computer")
        p.plan.items.append(item)


def _command_text(spec: dict[str, Any]) -> str:
    """An [mcp.external] server's command as a plan line shows it: credentials removed, at most about 100
    characters, with its folder."""
    command = strip_command_credentials(spec.get("command"))[0]
    if isinstance(command, list):
        text = shlex.join(str(a) for a in command)
    else:
        text = str(command)
    if len(text) > 100:
        text = text[:97] + "..."
    cwd = spec.get("cwd")
    return text + (f" in {cwd}" if isinstance(cwd, str) and cwd else "")


def _command_change(old: dict[str, Any], new: dict[str, Any]) -> str:
    """How an [mcp.external] server's command changes, as a plan line shows it: the whole commands when they are
    short, else from the first argument that differs on (credentials removed), each with its folder."""
    before, after = _command_key(old.get("command")), _command_key(new.get("command"))
    if isinstance(before, list) and isinstance(after, list) and len(shlex.join(map(str, before))) > 97:
        start = next((i for i, (a, b) in enumerate(zip(before, after)) if a != b), min(len(before), len(after)))
        before, after = (["...", *before[start:]], ["...", *after[start:]]) if start else (before, after)
    return (f"{_command_text({'command': before, 'cwd': old.get('cwd')})} -> "
            f"{_command_text({'command': after, 'cwd': new.get('cwd')})}")


def _project_updates(manager: SessionManager, uid: str | None, sid: str | None, current: Any = None
                     ) -> tuple[dict[str, Any], list[str]]:
    """meta.json changes of an imported MCP library entry: when an exported session saved it (`project_session`:
    its uid) and is here as `sid`, `project` becomes that session's folder (its MCP servers compare it with their
    session's folder); otherwise the `project` of the entry here (`current`) stays, and the export's, a folder of
    the other computer, is not taken."""
    if uid and sid:
        return {"project": str((manager.sessions_dir / sid).resolve())}, ["project_session"]
    drop = ["project_session"] if uid else []
    if isinstance(current, str) and current:
        return {"project": current}, drop
    return {}, [*drop, "project"]


def _check_source(manager: SessionManager, folder: Path) -> None:
    """The export folder must not be where the import writes: the sessions of this home or their folders."""
    home = manager.home
    places = [(home / SESSIONS_DIR, "the sessions folder of this home"),
              (home / ".dreamteam", "DreamTeam's own data folder of this home")]
    for sid in manager.ids():
        if sid == LEGACY_ID:
            continue
        with contextlib.suppress(SessionError, OSError, ValueError):
            workspace, stored, _adopted = manager._workspace_entry(sid, manager.meta(sid))
            if stored is None or _home_relative(manager.meta(sid).get("workspace")):
                places.append((workspace, f"the workspace of session {sid}"))
    for place, what in places:
        if _inside(folder, place):
            raise TransferError(f"Nothing imported: the export folder {folder} is inside {what} ({place}), which the "
                                "import may replace. Move the export folder elsewhere first.")


def _target_sessions(manager: SessionManager) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    """({id: session.json}, {uid: [ids]}) of the sessions of this home (the legacy main session and damaged ones
    have no identity to match)."""
    metas: dict[str, dict[str, Any]] = {}
    uids: dict[str, list[str]] = {}
    for sid in manager.ids():
        if sid == LEGACY_ID:
            continue
        try:
            meta = manager.meta(sid)
        except SessionError:
            continue
        metas[sid] = meta
        uid = session_uid(meta)
        if uid:
            uids.setdefault(uid, []).append(sid)
    return metas, uids


def check_session_names(folder: str | os.PathLike, sessions: Iterable[str] | None) -> None:
    """The `--session` values of an import name sessions the export holds (by id or uid, as its manifest lists
    them): TransferError with `usage` set otherwise (also for an export without sessions). Reads the manifest only
    (read_manifest)."""
    wanted = [text for text in (str(s).strip() for s in sessions or ()) if text]
    if not wanted:
        return
    manifest = read_manifest(folder)
    held = [c for c in CATEGORIES if c in (manifest.get("include") or [])]
    if "sessions" not in held:
        raise TransferError(f"Nothing imported: the export holds no sessions (it was made with --include "
                            f"{','.join(held) or 'nothing'}).", usage=True)
    listed = [e for e in manifest.get("sessions") or [] if isinstance(e, dict)]
    ids = {e.get("id") for e in listed}
    uids = {str(e.get("uid")).lower() for e in listed if isinstance(e.get("uid"), str)}
    for text in wanted:
        if text not in ids and text.lower() not in uids:
            raise TransferError(f"Nothing imported: the export holds no session '{text}' (give its id or uid).",
                                usage=True)


def plan_import(manager: SessionManager, folder: str | os.PathLike, *, include: Iterable[str] | str | None = None,
                sessions: Iterable[str] | None = None) -> ImportPlan:
    """What importing the export in `folder` into the home of `manager` would do; nothing is changed.

    `include`: categories (parse_include; default: every category the export holds). `sessions`: ids or uids of
    the export's sessions to import (default all; giving any implies the sessions category). Each item of the plan
    is add | rewrite | unchanged | skip | refuse with its reason (ImportItem). Sessions are matched by their uid,
    never by id: a new one is added (under a new id when its id is taken here), an existing one is rewritten,
    also when this computer has newer or other work in it (the plan says so; the old copy is kept as a backup), and
    unchanged when its last event and its workspace files are the same. A uid two sessions hold, on either side, is
    refused unless `sessions` names the session by its id. Raises TransferError (read_manifest; usage errors with
    `usage` set: an unknown category or session)."""
    folder = Path(os.path.abspath(Path(folder).expanduser()))
    manifest = read_manifest(folder)
    categories = parse_include(include)
    wanted = [text for text in (str(s).strip() for s in sessions) if text] if sessions is not None else None
    if not wanted:
        wanted = None
    else:
        categories = tuple(c for c in CATEGORIES if c in categories or c == "sessions")
    held = [c for c in CATEGORIES if c in (manifest.get("include") or [])]
    plan = ImportPlan(home=manager.home, folder=folder, manifest=manifest)
    if wanted and "sessions" not in held:
        raise TransferError(f"Nothing imported: the export holds no sessions (it was made with --include "
                            f"{','.join(held) or 'nothing'}).", usage=True)
    if include is not None:   # only what was asked for by name is missing
        for category in parse_include(include):
            if category not in held:
                plan.missing.append(category)
                plan.warnings.append(f"the export holds no {category} (it was made with --include "
                                     f"{','.join(held) or 'nothing'})")
    categories = tuple(c for c in categories if c in held)
    plan.include = categories
    _check_source(manager, folder)
    p = _Planner(manager, folder, manifest, plan)
    raw = copy.deepcopy(manager.raw)
    raw_after = _plan_settings(p, raw) if "settings" in categories else raw
    with contextlib.suppress(ValueError):
        p.models_after = set(validate(manager.home, raw_after).models)
    if "skills" in categories:
        _plan_skills(p)
    target_metas, target_uids = _target_sessions(manager)
    p.target_metas = target_metas
    p.uid_targets = {uid: sids[0] for uid, sids in target_uids.items() if len(sids) == 1}
    for backup in manager.interrupted_swaps():
        plan.warnings.append(f"an earlier import stopped while it replaced a session ({backup} holds its previous "
                             "copy): this import finishes or undoes that first")
    if "sessions" in categories:
        _plan_sessions(p, wanted, raw_after, target_metas, target_uids)
    if "mcp" in categories:
        _plan_mcp(p, raw_after)
    for left in manifest.get("left_out") or []:
        if isinstance(left, dict) and left.get("what"):
            plan.warnings.append(f"the export left out {left['what']}: {left.get('reason') or ''}".rstrip(": "))
    source_os = (manifest.get("source") or {}).get("os") if isinstance(manifest.get("source"), dict) else None
    if isinstance(source_os, str) and source_os != sys.platform and any(
            i.category == "sessions" and i.action in ("add", "rewrite") for i in plan.items):
        plan.warnings.append(f"the export was made on {source_os}: agents hired there keep its shell (e.g. "
                             "PowerShell or bash) in their instructions, and paths in their histories are of that "
                             "computer")
    return plan


# ---- apply ------------------------------------------------------------------------------------------------------
def _copy_checked(src: Path, dst: Path, expected: Any) -> None:
    """Copy the file `src` to the new file `dst`, checking its sha256 against `expected` on the way: the database
    the plan checked must be the one copied."""
    h = hashlib.sha256()
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(long_path(dst), flags, 0o666), "wb") as out:
        for chunk in _file_chunks(long_path(src)):
            h.update(chunk)
            out.write(chunk)
    if h.hexdigest() != expected:
        raise TransferError("its database is not the one the export wrote (it changed or was damaged since the plan)")


def _stage_session(src: Path, staged: Path, expected_sha: Any = None, modes: Any = None) -> None:
    """Copy an exported session (its database, .dreamteam/{mcp,attached-skills,jobs}, workspace/) into the new
    staging folder `staged`. The permission bits the export medium reports are not taken: the files get this
    computer's defaults, and the bits the export recorded (`modes`: {path in the session: bits}) on top. A file of
    the export that cannot be read, or a database that is not the one the manifest names (`expected_sha`), fails
    it (TransferError), and so does a folder of it that is a link (never followed)."""
    linked = _linked_part(src)
    if linked:
        raise TransferError(f"its {linked} folder in the export is a link; an export holds plain folders and files only")
    os.mkdir(staged)
    data = staged / ".dreamteam"
    data.mkdir()
    db = src / ".dreamteam" / DB_FILE
    if db.is_file() and not is_link(db):
        if not isinstance(expected_sha, str):
            raise TransferError("the manifest of the export names no checksum of its database")
        _copy_checked(db, data / DB_FILE, expected_sha)
    recorded = {k: v for k, v in modes.items() if isinstance(k, str) and valid_mode(v) is not None} \
        if isinstance(modes, dict) else {}
    parts = [(src / ".dreamteam" / part, data / part, f".dreamteam/{part}/") for part in SESSION_DATA_PARTS]
    for folder, target, prefix in [*parts, (src / WORKSPACE_DIR, staged / WORKSPACE_DIR, f"{WORKSPACE_DIR}/")]:
        if is_link(folder) or not folder.is_dir():
            continue
        result = copy_tree(folder, target, exclude_names=(), exclude_paths=(), modes="import",
                           apply_modes={k[len(prefix):]: v for k, v in recorded.items() if k.startswith(prefix)})
        if result.errors:
            rel, reason = result.errors[0]
            raise TransferError(f"{folder / rel} cannot be read in the export ({reason})")


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror + (f": {_plain(exc.filename)}" if exc.filename else "")
    return str(exc) or type(exc).__name__


@dataclass
class _Applying:
    manager: SessionManager
    result: ImportResult
    uid_targets: dict[str, str]
    before_swap: Callable[[str], None] | None


async def apply_import(manager: SessionManager, plan: ImportPlan, *,
                       before_swap: Callable[[str], None] | None = None, new_home: bool = False) -> ImportResult:
    """Do what `plan` (plan_import, for the same manager) says, in this order: the settings (one save through
    SessionManager.update_settings), the skills, the sessions, the MCP library (an entry an imported session saved
    gets that session's folder here as its `project`), then the [mcp.external] servers (one save). One item that
    fails does not stop the others. Staged copies an interrupted import left (`.importing-*`) are removed first.

    `before_swap(session id)`: called right before each session is renamed in (the CLI checks there that no hub
    or launcher took the home meanwhile); an exception it raises fails that session, which stays as it was, and
    HomeTaken also stops the import (the items after it are not done). `new_home`: the home was made for this
    import (its settings were the template: no dreamteam.toml.bak is left of them).
    Ctrl+C (the task's cancellation, or KeyboardInterrupt) finishes or undoes the item being imported, then stops:
    the result has `interrupted` set and the items after it "not done". Returns an ImportResult."""
    items = [dataclasses.replace(i, warnings=list(i.warnings), data=dict(i.data)) for i in plan.items]
    result = ImportResult(plan=plan, items=items, warnings=list(plan.warnings))
    _metas, target_uids = _target_sessions(manager)
    state = _Applying(manager, result, {uid: sids[0] for uid, sids in target_uids.items() if len(sids) == 1},
                      before_swap)
    task = asyncio.current_task()
    stop = False
    taken: str | None = None   # HomeTaken: why the items after it are not done
    recovered: list[str] = []
    put_back = False
    try:
        before = set(manager.ids())
        recovered = await asyncio.to_thread(manager.recover_swaps)
        put_back = set(manager.ids()) != before
        left = await asyncio.to_thread(manager.clean_staging)
    except (OSError, asyncio.CancelledError, KeyboardInterrupt) as exc:
        left = []
        stop = not isinstance(exc, OSError)
    result.warnings.extend(recovered)
    result.warnings.extend(f"an interrupted import left {path}: delete it by hand" for path in left)
    path = settings_path(manager.home)
    try:   # dreamteam.toml.bak keeps the settings from before the import, however many saves it makes
        settings_before = path.read_bytes() if path.is_file() and not new_home else None
    except OSError:
        settings_before = None

    def order(item: ImportItem) -> tuple[int, int]:
        return APPLY_ORDER.index(item.category), 1 if item.data.get("kind") == "external" else 0

    try:
        for item in sorted(items, key=order):
            if item.status is not None:
                continue   # done with its group ([mcp.external])
            if (stop or taken) and item.action in ("add", "rewrite"):
                item.status = "not done"
                if taken and not stop:
                    item.error = taken
                continue
            if item.action == "refuse":
                item.status, item.error = "refused", item.reason
                continue
            if item.action == "skip":
                item.status = "skipped"
                continue
            if item.action == "unchanged":
                item.status = "unchanged"
                if item.category == "sessions" and item.target:
                    result.id_map[item.name] = item.target
                    if item.data.get("uid"):   # as the plan maps it (the MCP library entries it saved)
                        state.uid_targets[item.data["uid"]] = item.target
                continue
            if put_back and item.category == "sessions":   # the plan was made before the sessions were put back
                item.status, item.error = "not done", "an earlier import was put in order first: import again"
                continue
            try:
                await _apply_item(state, item)
            except (asyncio.CancelledError, KeyboardInterrupt):
                stop = True
                for other in items:
                    if other.status is None and (other is item or other.data.get("group") == id(item)):
                        other.status = "not done"
            except HomeTaken as exc:   # another process runs the home now: nothing more is written into it
                item.status, item.error = "failed", _error_text(exc)
                taken = item.error
            except Exception as exc:
                item.status, item.error = "failed", _error_text(exc)
            if task is not None and task.cancelling():
                stop = True
    finally:
        saved = any(i.status in ("added", "rewritten") and (i.category == "settings" or i.data.get("kind") ==
                                                            "external") for i in items)
        backup = path.with_name(path.name + ".bak")
        if saved and new_home:   # (the template the home was made with, which each save keeps as .bak)
            with contextlib.suppress(OSError):
                backup.unlink()
        elif saved and settings_before is not None:
            try:
                backup.write_bytes(settings_before)
                _private(backup)
                result.warnings.append(f"the settings from before the import are in {backup}")
            except OSError as exc:
                result.warnings.append(f"the settings from before the import could not be kept in {backup}: "
                                       f"{exc.strerror or exc}")
    result.interrupted = stop
    return result


async def _apply_item(state: _Applying, item: ImportItem) -> None:
    manager = state.manager
    if item.category == "settings":
        manager._refresh_settings()
        merge = merge_settings(manager.raw, item.data["exported"], home=manager.home)
        if merge.error:
            raise TransferError(f"the settings cannot be used here: {merge.error}")
        if not merge.changed:
            item.status = "unchanged"
            return
        await manager.update_settings(merge.update)
        item.status = "rewritten"
    elif item.category == "skills":
        config = manager.global_config()
        library = Library(config.skills_library_dir, Limits.from_config(config))
        d = item.data
        entry = await _to_end(lambda: library.add(d["src"], d["origin"], d["source"], replace=True, by=d["by"],
                                                  via="import"))
        item.status = "unchanged" if entry.get("unchanged") else ("added" if item.action == "add" else "rewritten")
    elif item.category == "sessions":
        await _apply_session(state, item)
    elif item.data.get("kind") == "library":
        root = manager.global_config().library_dir
        uid = item.data.get("project_session")
        sid = state.uid_targets.get(uid) if uid else None
        current = mcp_library.read_entry(root, item.data["server"])
        updates, drop = _project_updates(manager, uid, sid if sid and manager.exists(sid) else None,
                                         current.meta.get("project") if current is not None else None)
        status = await _to_end(lambda: mcp_library.add_entry(root, item.data["src"], name=item.data["server"],
                                                             replace=True, meta_updates=updates, meta_drop=drop))
        item.status = status
    else:
        await _apply_external(state, item)


async def _apply_session(state: _Applying, item: ImportItem) -> None:
    manager, d = state.manager, item.data
    planned = item.target or item.name
    staged = manager.staging_path(planned)

    def hook(sid: str) -> None:
        if state.before_swap is not None:
            state.before_swap(sid)

    if d.get("settings_only"):   # the same history and files here: only its session.json is written
        hook(planned)
        done = await manager.import_settings(planned, d["meta"], expect=d.get("expect"))
        item.status = "rewritten"
        _dropped_model(item, done)
        item.target = done["id"]
        state.result.id_map[item.name] = done["id"]
        state.uid_targets[d["uid"]] = done["id"]
        return
    try:
        await _in_thread(_stage_session, d["src"], staged, d.get("db_sha256"), d.get("modes"))
        if item.action == "add":
            done = await manager.import_add(staged, d["meta"], d["workspace"], sid=planned, before_swap=hook)
            item.status = "added"
            if done["id"] != planned:
                item.reason = f"as {done['id']}: {planned} was taken meanwhile"
        else:
            done = await manager.import_rewrite(planned, staged, d["meta"], d["workspace"], expect=d.get("expect"),
                                                before_swap=hook)
            item.status, item.backup = "rewritten", done["backup"]
            state.result.backups.append(done["backup"])
            state.result.removed_backups.extend(done["removed_backups"])
            item.warnings.extend(done["warnings"])
            if done["kept"]:
                item.data["kept"] = done["kept"]
        _dropped_model(item, done)
        item.target = done["id"]
        state.result.id_map[item.name] = done["id"]
        state.uid_targets[d["uid"]] = done["id"]
    finally:
        if os.path.lexists(staged):
            with contextlib.suppress(OSError):
                rmtree(staged)


def _dropped_model(item: ImportItem, done: dict[str, Any]) -> None:
    """Warn about an `agents.default_model` override the session lost when it was written (the profile is not
    here), unless the plan already said so."""
    model = done.get("dropped_model")
    if model:
        text = f"session {done['id']}: its model profile '{model}' does not exist here, so it uses the default profile"
        if text not in item.warnings:
            item.warnings.append(text)


async def _apply_external(state: _Applying, first: ImportItem) -> None:
    """Every [mcp.external] server of the import in one settings save (`first` and the ones after it)."""
    manager, result = state.manager, state.result
    group = [i for i in result.items if i.category == "mcp" and i.data.get("kind") == "external"
             and i.status is None and i.action in ("add", "rewrite")]
    for other in group:
        other.data["group"] = id(first)
    manager._refresh_settings()
    raw = manager.raw
    mcp = copy.deepcopy(raw.get("mcp")) if isinstance(raw.get("mcp"), dict) else {}
    servers, actions, _notes = merge_external(mcp.get("external"), {i.data["server"]: i.data["spec"] for i in group})
    mcp["external"] = servers
    try:
        await manager.update_settings({"mcp": mcp})
    except Exception as exc:
        for other in group:
            other.status, other.error = "failed", _error_text(exc)
        return
    for other in group:
        other.status = {"add": "added", "rewrite": "rewritten"}.get(actions.get(other.data["server"], ""),
                                                                    "unchanged")


# ---- texts --------------------------------------------------------------------------------------------------------
def _warnings(plan_warnings: list[str], items: list[ImportItem]) -> list[str]:
    seen: list[str] = []
    for text in [*plan_warnings, *(w for i in items for w in i.warnings)]:
        if text not in seen:
            seen.append(text)
    return seen


def _by_order(items: list[ImportItem]) -> list[ImportItem]:
    return sorted(items, key=lambda i: (APPLY_ORDER.index(i.category), 1 if i.data.get("kind") == "external" else 0))


def _line(item: ImportItem, text: str) -> str:
    return f"  {item.category:<9} {item.name}: {text}"


def render_import_plan(plan: ImportPlan) -> str:
    """The plan as the CLI prints it before asking: one line per item (what happens and why), the warnings, and
    what stays as it is."""
    manifest = plan.manifest
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    about = []
    if isinstance(source.get("machine"), str) and source["machine"]:
        about.append(f"exported on {source['machine']}")
    if isinstance(manifest.get("created_at"), (int, float)) and not isinstance(manifest["created_at"], bool):
        with contextlib.suppress(OverflowError, OSError, ValueError):
            about.append(time.strftime("%Y-%m-%d %H:%M", time.localtime(manifest["created_at"])))
    if isinstance(manifest.get("dreamteam_version"), str):
        about.append(f"DreamTeam {manifest['dreamteam_version']}")
    lines = [f"Import {plan.folder} into {plan.home}" + (f" ({', '.join(about)})" if about else "") + ":"]
    for item in _by_order(plan.items):
        if item.action == "add":
            text = "add" + (f" {item.reason}" if item.reason.startswith("as ") else
                            f" ({item.reason})" if item.reason else "")
        elif item.action == "refuse":
            text = f"refused ({item.reason})"
        elif item.reason:
            text = f"{item.action} ({item.reason})"
        else:
            text = item.action
        lines.append(_line(item, text))
    if not plan.items:
        lines.append("  nothing to import")
    warnings = _warnings(plan.warnings, plan.items)
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {text}" for text in warnings)
    if plan.changes:
        lines.append("Nothing else here changes: what the export does not carry stays as it is.")
        if any(i.category == "sessions" and i.action == "rewrite" and not i.data.get("settings_only")
               for i in plan.items):
            lines.append(f"A rewritten session keeps its previous copy in sessions/{REPLACED_PREFIX}<id>-<time>/ "
                         "(only the last one of each session).")
    elif plan.items and plan.blocked:
        lines.append("Nothing can be imported: see the items refused or skipped above.")
    elif plan.items:
        lines.append("Nothing to change here.")
    return "\n".join(lines)


def render_import_result(result: ImportResult) -> str:
    """What the import did, as the CLI prints it: one line per item, the warnings, the backups, and that imported
    sessions do not start by themselves."""
    plan = result.plan
    lines = [f"Import of {plan.folder} into {plan.home} stopped (Ctrl+C):" if result.interrupted
             else f"Imported {plan.folder} into {plan.home}:" if result.ok
             else f"Import of {plan.folder} into {plan.home} finished with problems:"]
    for item in _by_order(result.items):
        status = item.status or "not done"
        if status == "added" and item.category == "sessions" and item.target and item.target != item.name:
            text = f"added as {item.target} ({item.name} is taken here)"
        elif status == "rewritten" and item.category == "sessions" and item.target:
            why = item.data.get("why")
            about = [why] if why and why != "the export has newer work in it" else []
            if item.data.get("settings_only"):
                about.append("its session.json only, no copy kept")
            if item.backup:
                about.append(f"previous copy: {item.backup}")
            text = "rewritten" + (f" as {item.target}" if item.target != item.name else "") + \
                (f" ({'; '.join(about)})" if about else "")
        elif status in ("refused", "skipped"):
            text = f"{status} ({item.error or item.reason})"
        elif status == "failed" or (status == "not done" and item.error):
            text = f"{status} ({item.error})"
        else:
            text = status
        lines.append(_line(item, text))
    if not result.items:
        lines.append("  nothing to import")
    warnings = _warnings(result.warnings, result.items)
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {text}" for text in warnings)
    if result.removed_backups:
        lines.append("Older copies of the rewritten sessions were removed (only the last one is kept): "
                     + ", ".join(result.removed_backups))
    if any(i.category == "sessions" and i.status in ("added", "rewritten") for i in result.items):
        lines.append("Imported sessions do not start by themselves: start them on the sessions page or with "
                     "dreamteam -r ID.")
    if result.interrupted or any(i.status == "not done" for i in result.items):
        lines.append("What is marked 'not done' was not imported: run the import again to bring it in.")
    return "\n".join(lines)
