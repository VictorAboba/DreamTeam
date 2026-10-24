"""The session's skills (docs/SPEC.md §19): the copies attached to one swarm, the agents' read-only mirror of them
in the workspace, and what agents are told about them (the wake-up index, notifications); members' proposals and the
user's library decision on them (`decide_files` also on a stopped session's files); the skills roles bring.

The authoritative copies live in `<data_dir>/attached-skills/<name>/` with the session's `.dreamteam.json` (no
database table: a stopped session's skills are a folder listing). Agents read
`<workspace>/.scratch/_skills/<name>/`, a read-only copy rebuilt from them at every start, on every change and
whenever its in-memory listing no longer matches (the scheduler looks once a minute). Nothing in the mirror is trusted.

A skill's script dependencies (skill_deps.py) are planned when it is attached and at every start, built in the
background into environments shared by every session, and shown in its index line; a Node skill's mirror folder gets
a `node_modules` link to this session's own view of its environment.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Collection

from . import netoff, procs, prompts, roles, skill_deps, skills
from .models import USER, AgentKind, ToolError
from .skills import (META_FILE, NAME_MAX, NAME_RE, SKILL_MD, Library, Limits, SkillError, check_name, check_skill_dir,
                     clean_stale, copy_skill, for_agents)
from .shell import resolve_shell
from .workspace import SCRATCH_DIR, SKILLS_MIRROR, _covers, format_size, is_link, long_path, rmtree, scratch_is_local

if TYPE_CHECKING:
    from .config import Config
    from .swarm import Swarm

log = logging.getLogger(__name__)

SKILLS_CHECK_SECONDS = 60.0     # the scheduler compares the mirror with its manifest at most this often
INDEX_DESCRIPTION_MAX = 300     # characters of a description in the wake-up index (notifications carry it whole)
DETACHED_KEY = "skills_detached"  # kv: names the user detached (a role never attaches them again)
PROPOSED_KEY = "skills_proposed"  # kv: the names members proposed in this session ([skills] max_proposals counts them)
COMMENT_MAX = 2000              # characters of the user's comment on a library decision
FROM = ("library", "claude-user", "claude-project", "host-library", "agent")
LIBRARY_STATUS = ("none", "pending", "saved", "rejected")
_SCP_URL = re.compile(r"[^@/\\\s]+@[^:/\\\s]+:")   # user@host:path (a Git source, not a folder)


def skill_error(exc: SkillError) -> ToolError:
    """A SkillError as the ToolError tools report; `status` and `extra` stay on it for REST callers."""
    return _error(str(exc), exc.status, **exc.extra)


def _error(message: str, status: int = 400, **extra: Any) -> ToolError:
    error = ToolError(message)
    error.status = status   # type: ignore[attr-defined]
    error.extra = extra     # type: ignore[attr-defined]
    return error


def skills_dir(data_dir: Path) -> Path:
    """Where a session keeps its attached skills. Not `skills`: for a project in the home folder the data dir is
    ~/.dreamteam, whose skills/ is the default shared library."""
    return Path(data_dir) / "attached-skills"


def _path_key(path: Path) -> str:
    try:
        path = Path(path).resolve()
    except (OSError, ValueError, RuntimeError):
        pass
    return os.path.normcase(os.path.normpath(str(path)))


def library_clash(data_dir: Path, library_dir: Path | None) -> bool:
    """The session's skills folder is the shared library, or inside it or contains it (a library_dir set to it):
    its entries would pass for attached skills, and a detach would delete one."""
    if library_dir is None:
        return False
    a, b = _path_key(skills_dir(data_dir)), _path_key(library_dir)
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep) or b.startswith(a.rstrip(os.sep) + os.sep)


@dataclass
class SessionSkill:
    """One attached skill, as its `.dreamteam.json` in the session's skills folder describes it."""

    name: str
    description: str
    digest: str
    files: int
    size: int
    frm: str                                       # library | claude-user | claude-project | host-library | agent
    source: dict = field(default_factory=dict)     # the library entry's source (+ its "origin"), or {"path"}
    attached_by: str = USER
    attached_at: float = 0.0
    updated_at: float = 0.0
    owner: str | None = None                       # only from = agent
    attached_via: str = "user"                     # user | role:<Role> | create | proposal
    manual: bool = False                           # disable-model-invocation: only when the user asks for it
    library: str = "none"                          # none | pending | saved | rejected
    comment: str = ""
    decided_while_stopped: bool = False

    def meta(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "digest": self.digest, "files": self.files,
                "size": self.size, "from": self.frm, "source": dict(self.source), "attached_by": self.attached_by,
                "attached_at": self.attached_at, "updated_at": self.updated_at, "owner": self.owner,
                "attached_via": self.attached_via, "manual": self.manual, "library": self.library,
                "comment": self.comment, "decided_while_stopped": self.decided_while_stopped}

    @classmethod
    def from_meta(cls, meta: Any, name: str) -> "SessionSkill | None":
        """None for metadata that is missing or not for this folder."""
        if not isinstance(meta, dict) or meta.get("name") != name or not isinstance(meta.get("digest"), str):
            return None
        frm = meta.get("from") if meta.get("from") in FROM else "library"
        library = meta.get("library") if meta.get("library") in LIBRARY_STATUS else "none"

        def number(key: str) -> float:
            value = meta.get(key)
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0

        owner = meta.get("owner")
        return cls(name=name, description=str(meta.get("description") or ""), digest=meta["digest"],
                   files=int(number("files")), size=int(number("size")), frm=frm,
                   source=meta.get("source") if isinstance(meta.get("source"), dict) else {},
                   attached_by=str(meta.get("attached_by") or USER), attached_at=number("attached_at"),
                   updated_at=number("updated_at"), owner=owner if isinstance(owner, str) and owner else None,
                   attached_via=str(meta.get("attached_via") or "user"), manual=meta.get("manual") is True,
                   library=library, comment=str(meta.get("comment") or ""),
                   decided_while_stopped=meta.get("decided_while_stopped") is True)


def read_session_skills(data_dir: Path, library_dir: Path | None = None) -> list[SessionSkill]:
    """The skills attached to the session whose data dir is `data_dir`, by name (no Swarm needed: also a stopped
    session). A folder without valid metadata is left out (logged); nothing when the folder clashes with the
    library `library_dir` (library_clash)."""
    folder = skills_dir(data_dir)
    if library_clash(data_dir, library_dir):
        log.warning("the session skills folder %s overlaps the skills library %s: no skill is read from it", folder,
                    library_dir)
        return []
    try:
        with os.scandir(long_path(folder)) as it:
            entries = sorted(it, key=lambda e: e.name)
    except OSError:
        return []
    out = []
    for entry in entries:
        name = entry.name
        if (name.startswith(".") or len(name) > NAME_MAX or not NAME_RE.fullmatch(name) or is_link(entry)
                or not entry.is_dir(follow_symlinks=False)):
            continue
        skill = SessionSkill.from_meta(skills._read_json(folder / name / META_FILE), name)
        if skill is None:
            log.warning("the session skill folder %s has no valid %s: left out", folder / name, META_FILE)
            continue
        out.append(skill)
    return out


def public_item(s: SessionSkill, *, path: str, roles: list[str], deps: list[dict]) -> dict:
    """One item of the snapshot's `skills` (docs/SPEC.md §14.1)."""
    return {"name": s.name, "description": s.description, "from": s.frm, "owner": s.owner, "library": s.library,
            "comment": s.comment, "attached_by": s.attached_by, "attached_at": s.attached_at,
            "updated_at": s.updated_at, "attached_via": s.attached_via, "files": s.files, "size": s.size,
            "digest": s.digest, "manual": s.manual, "path": path, "roles": roles, "deps": deps}


def listed_by_roles(config: "Config", language: str) -> dict[str, list[str]]:
    """skill name -> the catalog roles (canonical names, `language`) that list it."""
    out: dict[str, list[str]] = {}
    for role in roles.catalog(config.roles, language, include_hidden=True):
        for name in role.skills:
            out.setdefault(name, []).append(role.name)
    return out


def stopped_public(config: "Config") -> list[dict]:
    """The snapshot items of a session without a loaded Swarm (stopped, its page never opened), read from its files
    (`config`: the session's, with its workspace): the keys of SessionSkills.public, `deps` empty (nothing is
    planned or installed while it is stopped). Blocking: run it in a thread."""
    root = config.workspace_dir   # (no .scratch yet, as before a first start: the start makes it a plain folder)
    local = not os.path.lexists(long_path(root / SCRATCH_DIR)) or scratch_is_local(root)
    listed = listed_by_roles(config, config.swarm.language)
    return [public_item(s, path=f"{SKILLS_MIRROR}/{s.name}/{SKILL_MD}" if local else
                        str(skills_dir(config.data_dir) / s.name / SKILL_MD), roles=listed.get(s.name, []), deps=[])
            for s in read_session_skills(config.data_dir, config.skills_library_dir)]


def _save_meta(data_dir: Path, skill: SessionSkill) -> None:
    """Rewrite a session copy's metadata (a decision: the files are unchanged)."""
    skills._write_json(skills_dir(data_dir) / skill.name / META_FILE, skill.meta())


def decide_files(data_dir: Path, library: Library, name: str, approve: bool, *, comment: str = "",
                 replace: bool = False, via: str = "stopped-session", project: str = "", session: str | None = None,
                 stopped: bool = True) -> tuple[SessionSkill, str]:
    """The user's library decision on a member's proposal, on the files of the session whose data dir is `data_dir`
    (no Swarm needed: also a stopped session, `stopped` then records it so the owner is told at the next start).
    approve: the session copy is added to `library` (origin `agent`, `by` the user, `via`); otherwise it stays
    attached to the session only. Returns (the updated skill, the result text). ToolErrors with a `status`."""
    try:
        name = check_name(name)
    except SkillError as exc:
        raise skill_error(exc) from None
    if library_clash(data_dir, library.root):
        raise _error(f"The session's skills folder {skills_dir(data_dir)} is the skills library {library.root}, or "
                     "inside it or contains it.", 409)
    found = {s.name: s for s in read_session_skills(data_dir, library.root)}
    skill = found.get(name)
    if skill is None:
        raise _error(f"No skill '{name}' is attached. Attached: {', '.join(found) or 'none'}.", 404)
    if skill.library != "pending":
        raise _error(f"Skill '{name}' is not waiting for a library decision (status: {skill.library}).", 409)
    comment = " ".join(str(comment or "").split())[:COMMENT_MAX]
    if approve:
        # the library gets the content that was proposed and announced, never what an agent's shell put into the
        # session copy since (copy_skill refuses bytes that differ from this check)
        folder = skills_dir(data_dir) / name
        try:
            info = check_skill_dir(folder, library.limits)
        except SkillError:
            info = None
        if info is None or info.digest != skill.digest:
            raise _error(f"Skill '{name}' changed since it was proposed, so it is not saved. Detach it, or ask its "
                         "owner to propose it again.", 409)
        source = {"owner": skill.owner, "project": project, "session": session, "swarm": project}
        try:
            library.add(folder, "agent", source, replace=replace, by=USER, via=via, info=info)
        except SkillError as exc:
            if exc.status == 409:
                raise _error(f"The library already has a different skill '{name}'. Approve with replace to "
                             "overwrite it, or reject.", 409, exists=[name]) from None
            raise skill_error(exc) from None
        text = f"Skill \"{name}\" saved to the skills library."
    else:
        text = (f"Skill \"{name}\" will not be saved to the library; it stays attached to this swarm (detach it to "
                "remove it).")
    updated = dataclasses.replace(skill, library="saved" if approve else "rejected", comment=comment,
                                  decided_while_stopped=stopped)
    _save_meta(data_dir, updated)
    return updated, text


def attach_files(data_dir: Path, folder: Path, name: str, frm: str, source: dict, limits: Limits, *,
                 info: skills.SkillInfo | None = None, attached_via: str = "create",
                 library_dir: Path | None = None) -> tuple[SessionSkill, str]:
    """Copy the checked skill folder `folder` into the session whose data dir is `data_dir` (no Swarm: a session
    being created, or a stopped one about to start, whose Swarm.start then mirrors it). The same name with the same
    digest is "unchanged", other content a 409 (the session's Skills tab replaces it). Returns (skill, "attached" |
    "unchanged"); ToolErrors with a `status`. The caller checks max_attached."""
    try:
        name = check_name(name)
        info = info or check_skill_dir(folder, limits)
    except SkillError as exc:
        raise skill_error(exc) from None
    if library_clash(data_dir, library_dir):
        raise _error(f"The session's skills folder {skills_dir(data_dir)} is the skills library {library_dir}, or "
                     "inside it or contains it.", 409)
    existing = {s.name: s for s in read_session_skills(data_dir, library_dir)}.get(name)
    if existing is not None:
        if existing.digest == info.digest:
            return existing, "unchanged"
        raise _error(f"A skill named '{name}' is already attached to the session (different content): replace it "
                     "in the session's Skills tab.", 409, exists=[name])
    now = time.time()
    skill = SessionSkill(name=name, description=info.description, digest=info.digest, files=info.files,
                         size=info.size, frm=frm, source=dict(source), attached_by=USER, attached_at=now,
                         updated_at=now, attached_via=attached_via, manual=info.manual)
    try:
        skills_dir(data_dir).mkdir(parents=True, exist_ok=True)
        copy_skill(folder, skills_dir(data_dir), name, limits, info=info, meta=lambda copied: skill.meta())
    except SkillError as exc:
        raise skill_error(exc) from None
    except OSError as exc:
        raise _error(f"Cannot copy the skill '{name}': {exc.strerror or exc}", 500) from None
    return skill, "attached"


def files_text(count: int) -> str:
    return "1 file" if count == 1 else f"{count} files"


def decision_comment(comment: str) -> str:
    """The user's comment as n_skill_not_saved ends with it: after a space, or nothing."""
    return f" {comment}" if comment else ""


def role_skill_pairs(config: "Config") -> tuple:
    """(role key, its skill names) of every [[roles]] entry: a reconfigure attaches role skills only when it changed."""
    return tuple((roles.role_key(r.name), tuple(r.skills)) for r in config.roles)


def source_spec(text: Any) -> dict:
    """An import spec (skills.stage_source) for a source given as text: a Git URL (`scheme://...`, `user@host:path`)
    or a folder or .zip path on the hub's machine."""
    value = str(text or "").strip()
    if not value:
        raise ToolError("Give a folder or .zip path on the hub's machine, or a Git URL.")
    if "://" in value or _SCP_URL.match(value):
        return {"git": value}
    return {"path": value}


def _listing(folder: Path, cap: int | None = None, skip: Collection[str] = ()) -> list[list] | None:
    """Every entry below a mirror copy as it is now, [rel, kind, size, mtime_ns], sorted: unlike the skill walk, the
    skip-list (node_modules/, .git, ...) and links are counted too, since nothing an agent puts there may stay.
    `skip`: the places of DreamTeam's node_modules links (checked by themselves). None when unreadable or past `cap`
    entries (a command filling the folder is not walked to its end)."""
    out: list[list] = []
    stack = [(long_path(folder), "")]
    while stack:
        path, prefix = stack.pop()
        try:
            with os.scandir(path) as it:
                for entry in it:
                    rel = prefix + entry.name
                    if rel in skip:
                        continue
                    if is_link(entry):
                        out.append([rel, "link", 0, 0])
                    elif entry.is_dir(follow_symlinks=False):
                        out.append([rel, "dir", 0, 0])
                        stack.append((entry.path, rel + "/"))
                    else:
                        st = entry.stat(follow_symlinks=False)
                        out.append([rel, "file", st.st_size, st.st_mtime_ns])
                    if cap is not None and len(out) > cap:
                        return None
        except OSError:
            return None
    return sorted(out)


def _expected_listing(manifest: list[list]) -> list[list]:
    """The _listing of a mirror copy just written, from copy_skill's manifest of it (taken before the swap, so a
    command changing the copy right after it is still caught): its files and the folders holding them."""
    out = [[rel, "file", size, mtime] for rel, size, mtime in manifest]
    folders = {rel.rsplit("/", i)[0] for rel, *_x in manifest for i in range(1, rel.count("/") + 1)}
    out += [[rel, "dir", 0, 0] for rel in folders]
    return sorted(out)


def _node_place(rel: str) -> str:
    """Where a skill's node_modules link goes, relative to its folder ("node_modules", "scripts/node_modules")."""
    return f"{rel}/node_modules" if rel else "node_modules"


def _no_parens(text: str) -> str:
    """A reason's detail is shown inside parentheses: none of its own."""
    return text.replace("(", "").replace(")", "")


def _drop(path: Path) -> bool:
    """Remove a folder (or a file or link) of the skills: renamed away first, so it is gone at once, then removed.
    False when that is refused (on Windows a process holding a file there, or using it as its working directory)."""
    if is_link(path) or not os.path.isdir(long_path(path)):
        with contextlib.suppress(OSError):
            skills._remove(path)
        return not os.path.lexists(long_path(path))
    trash = path.parent / skills._temp_name("o")
    try:
        skills._rename(path, trash)
    except OSError:
        return False
    with contextlib.suppress(OSError):
        skills._remove(trash)
    return True


class SessionSkills:
    """The skills of one swarm (`Swarm.skills`). Only the user attaches, detaches and decides what is saved to the
    library (a UX rule, docs/SPEC.md §19 "Security"); members propose; every member sees every attached skill."""

    def __init__(self, swarm: "Swarm"):
        self.swarm = swarm
        self._lock = asyncio.Lock()       # one change (attach, detach, mirror sync) at a time
        # the mirror's files: a sync's thread outlives a cancelled caller (the scheduler at stop), and the removal
        # at stop must not run beside it
        self._files = threading.Lock()
        self._cache: dict[str, SessionSkill] = {}
        # name -> (digest of the session copy, _listing of its mirror copy as it was written)
        self._mirror: dict[str, tuple[str, list]] = {}
        self._pending_removal: set[str] = set()   # mirror folders Windows would not remove yet (retried each sync)
        self._local: bool | None = None      # the mirror is in the workspace (scratch_is_local), as of the last sync
        self._last_sync = 0.0
        self._reported: set[tuple] = set()   # (kind, role, names) the user was told about (missing, over the cap)
        self._hired: dict[str, dict] = {}    # new member -> the ensure_role_skills report of its hire (hire_report)
        # script dependencies (skill_deps.py): name -> ((digest, settings hash), its EnvSpecs and state dicts)
        self._plans: dict[str, tuple[tuple, list]] = {}
        self._seen: dict[tuple[str, str], str] = {}   # (name, ecosystem) -> the status this session last saw
        # name -> {folder of its package.json: this session's view of the Node env}: every view known, and the
        # links the mirror (or the session copy) should hold now (an agent's own node_modules drops one)
        self._link_targets: dict[str, dict[str, str]] = {}
        self._mirror_links: dict[str, dict[str, str]] = {}
        self._node_link: dict[str, str] = {}          # name -> "linked" | "failed"
        self._view_failed: dict[str, str] = {}        # name -> why its view could not be made (check() retries it)
        self._dropped_logged: set[tuple[str, str]] = set()
        self._tasks: set[asyncio.Task] = set()        # view builds after a Node install (stop_deps cancels them)
        self._unsubscribe: Any = None
        self._active = False
        self._last_touch = 0.0
        self._shell: tuple[str, str] | None = None    # ([shell] program, the shell it resolves to)

    # ---- places ---------------------------------------------------------------------------------------------
    @property
    def dir(self) -> Path:
        return skills_dir(self.swarm.config.data_dir)

    @property
    def mirror_dir(self) -> Path:
        return Path(self.swarm.workspace.root, *SKILLS_MIRROR.split("/"))

    def limits(self) -> Limits:
        return Limits.from_config(self.swarm.config)

    def _is_local(self) -> bool:
        if self._local is None:
            self._local = scratch_is_local(self.swarm.workspace.root)
        return self._local

    def shown_path(self, name: str) -> tuple[str, bool]:
        """(path of the skill's SKILL.md as agents are shown it, inside the workspace): the mirror's, relative to the
        workspace root; or, when `.scratch` is not a plain local folder, the session copy's absolute path."""
        if self._is_local():
            return f"{SKILLS_MIRROR}/{name}/{SKILL_MD}", True
        return str(self.dir / name / SKILL_MD), False

    def agent_dir(self) -> str:
        """DREAMTEAM_SKILLS of the agents' shells: the folder holding the skills they read."""
        return str(self.mirror_dir if self._is_local() else self.dir)

    def _path_text(self, name: str, lang: str) -> str:
        path, inside = self.shown_path(name)
        return path if inside else prompts.t(lang, "skills_path_outside", path=path)

    # ---- reading --------------------------------------------------------------------------------------------
    def _check_dir(self) -> None:
        """Refuse a change when the session's skills folder overlaps the shared library (library_clash)."""
        library = self.swarm.config.skills_library_dir
        if library_clash(self.swarm.config.data_dir, library):
            raise _error(f"The swarm's skills folder {self.dir} is the skills library {library}, or inside it or "
                         "contains it: set [skills] library_dir (or DREAMTEAM_SKILLS_DIR) to another folder.", 409)

    def load(self) -> None:
        """Read every attached skill's metadata into the cache (synchronous, small files)."""
        library = self.swarm.config.skills_library_dir
        if not library_clash(self.swarm.config.data_dir, library):  # never the library's own leftovers
            clean_stale(self.dir)
        self._cache = {s.name: s for s in read_session_skills(self.swarm.config.data_dir, library)}

    def list(self) -> list[SessionSkill]:
        return [self._cache[n] for n in sorted(self._cache)]

    def get(self, name: str) -> SessionSkill | None:
        return self._cache.get(name)

    def names(self) -> list[str]:
        return sorted(self._cache)

    def pending(self) -> list[str]:
        """Names of proposals waiting for the user's library decision."""
        return [s.name for s in self.list() if s.library == "pending"]

    def public(self) -> list[dict]:
        """The snapshot's `skills` (docs/SPEC.md §14.1): from memory, no query."""
        listed = self._listed_by_roles()
        return [public_item(s, path=self.shown_path(s.name)[0], roles=listed.get(s.name, []), deps=self.deps(s.name))
                for s in self.list()]

    def _listed_by_roles(self) -> dict[str, list[str]]:
        """skill name -> the catalog roles (canonical names, swarm language) that list it."""
        return listed_by_roles(self.swarm.config, self.swarm.language)

    def role_skills(self, member: dict) -> tuple[str, ...]:
        """The skills `member`'s role lists in the catalog, resolved live from its role (catalog edits apply at
        once); marked "your role's" in its index."""
        entry = roles.find_role(self.swarm.config.roles, member.get("role"),
                                member.get("language") or self.swarm.language)
        return entry.skills if entry is not None else ()

    def source_digest(self, skill: SessionSkill) -> str | None:
        """The digest of the skill's own source now (its `from` only, never a same-named skill elsewhere); None when
        the source is gone or it has none (a member's proposal). Synchronous file work."""
        config = self.swarm.config
        try:
            if skill.frm == "library":
                return Library.from_config(config).digest_of(skill.name)
            if skill.frm == "host-library":
                host = Library.host(config)
                return host.digest_of(skill.name) if host is not None else None
            if skill.frm in ("claude-user", "claude-project") and skill.source.get("path"):
                return check_skill_dir(Path(skill.source["path"]), self.limits()).digest
        except SkillError:
            return None
        return None

    # ---- what agents see --------------------------------------------------------------------------------------
    def _ordered(self, member: dict) -> tuple[list[SessionSkill], set[str]]:
        """The skills shown to `member`: its role's first, then the rest, each by name; and its role's names."""
        own = set(self.role_skills(member))
        shown = [s for s in self.list() if s.name not in self._pending_removal]
        return sorted(shown, key=lambda s: (s.name not in own, s.name)), own

    def _marks(self, skill: SessionSkill, own: set[str], lang: str) -> str:
        return ((prompts.t(lang, "skills_own") if skill.name in own else "")
                + (prompts.t(lang, "skills_manual") if skill.manual else ""))

    def item_lines(self, name: str, lang: str | None = None) -> list[str]:
        """One `skills_item` line per skill shown to member `name` (in `lang`, default its language)."""
        member = self.swarm.member(name) or {"name": name}
        lang = lang or member.get("language") or self.swarm.language
        quiet = member.get("kind") != AgentKind.EXTERNAL
        items, own = self._ordered(member)
        lines = []
        for s in items:
            description = for_agents(s.description)
            if len(description) > INDEX_DESCRIPTION_MAX:
                description = description[:INDEX_DESCRIPTION_MAX - 1].rstrip() + "…"
            lines.append(prompts.t(lang, "skills_item", name=s.name, own=self._marks(s, own, lang),
                                   description=description, path=self._path_text(s.name, lang),
                                   deps=self.deps_fragment(s.name, lang, quiet=quiet)))
        return lines

    def index_text(self, name: str) -> str:
        """The full Skills index of a wake-up or a compaction for member `name`; "" when none is attached."""
        member = self.swarm.member(name) or {"name": name}
        lang = member.get("language") or self.swarm.language
        lines = self.item_lines(name, lang)
        if not lines:
            return ""
        items, own = self._ordered(member)
        key = "skills_index_own" if own & {s.name for s in items} else "skills_index"
        return prompts.t(lang, key, skills="\n".join(lines))

    def names_line(self, name: str) -> str:
        """The one line of every later wake-up: the attached names with their marks."""
        member = self.swarm.member(name) or {"name": name}
        lang = member.get("language") or self.swarm.language
        items, own = self._ordered(member)
        return prompts.t(lang, "skills_names", names=", ".join(s.name + self._marks(s, own, lang) for s in items))

    def _announce(self, key: str, exclude: str | Collection[str] | None, *, name: str,
                  description: str | None = None, owner: str | None = None) -> None:
        """Tell every member but `exclude` (a name or several) about a skill: AI agents quietly (it shows at their
        next render, at no activation cost), external members with a wake (wait_for_events returns). `path` and
        `deps` (the dependency fragment of skills_item) are computed per recipient (its language)."""
        skip = {exclude} if isinstance(exclude, str) else set(exclude or ())
        deps: dict[tuple[str, bool], str] = {}
        for m in self.swarm.members():
            if m["name"] in skip:
                continue
            lang = m.get("language") or self.swarm.language
            quiet = m["kind"] == AgentKind.LLM
            if (lang, quiet) not in deps:
                deps[(lang, quiet)] = self.deps_fragment(name, lang, quiet=quiet)
            kw: dict[str, Any] = {"name": name, "deps": deps[(lang, quiet)]}
            if description is not None:
                kw["description"] = for_agents(description)
                kw["path"] = self._path_text(name, lang)
            if owner is not None:
                kw["owner"] = owner
            self.swarm.notify(m["name"], key, wake=not quiet, **kw)

    # ---- changes --------------------------------------------------------------------------------------------
    async def start(self) -> None:
        """At every Swarm.start: the cache read again, every skill's dependencies planned (off the loop) and held in
        use, so the first wake-up shows the environments ready on disk as ready, the mirror rebuilt from the session
        copies (with the node_modules links), and the owners of proposals the user decided while the session was
        stopped told (quietly: it waits for their next wake-up). Swarm.start then runs ensure_deps in the
        background."""
        async with self._lock:
            await asyncio.to_thread(self.load)
            self._local = None
            self._active = True
            if self._unsubscribe is None:
                self._unsubscribe = self.manager.subscribe(self._on_env)
            await self._plan()
            await self._sync(full=True)
            for skill in [s for s in self.list() if s.decided_while_stopped]:
                updated = dataclasses.replace(skill, decided_while_stopped=False)
                try:
                    await asyncio.to_thread(_save_meta, self.swarm.config.data_dir, updated)
                except OSError:
                    log.warning("cannot record that the owner of the skill %s was told", skill.name)
                self._cache[skill.name] = updated
                if skill.owner and skill.owner != USER and self.swarm.is_member(skill.owner):
                    key = "n_skill_saved" if skill.library == "saved" else "n_skill_not_saved"
                    self.swarm.notify(skill.owner, key, wake=False, name=skill.name,
                                      comment=decision_comment(skill.comment))

    async def attach(self, actor: str, ref: str, *, replace: bool = False, announce: bool = True,
                     attached_via: str = "user", via: str = "act",
                     exclude: Collection[str] = ()) -> tuple[SessionSkill, str]:
        """Attach the skill a reference names (`x`, `library:x`, `claude-user:x`, `claude-project:x`, `host-library:x`;
        a bare name: library, then ~/.claude/skills, then the host library; the workspace's .claude/skills only when
        named). Returns (skill, "attached" | "updated" | "unchanged"). `attached_via`: user | role:<Role> | create;
        `via`: the surface (recorded on the event); `exclude`: members not told (a new hire sees the full index)."""
        if actor != USER:
            raise ToolError("Only the user can attach skills to the swarm.")
        config = self.swarm.config
        try:
            found = await asyncio.to_thread(skills.resolve_ref, config, ref, workspace=self.swarm.workspace.root)
            source = await asyncio.to_thread(self._source_of, found)
        except SkillError as exc:
            raise skill_error(exc) from None
        return await self._attach(actor, found["name"], Path(found["path"]), found["origin"], source,
                                  replace=replace, announce=announce, attached_via=attached_via, via=via,
                                  exclude=exclude)

    def _source_of(self, found: dict) -> dict:
        if found["origin"] == "library":
            entry = Library.from_config(self.swarm.config).get(found["name"]) or {}
            return {"origin": entry.get("origin") or "folder", **(entry.get("source") or {})}
        return {"path": str(found["path"])}

    async def _attach(self, actor: str, name: str, folder: Path, frm: str, source: dict, *, replace: bool,
                      announce: bool, attached_via: str, via: str,
                      exclude: Collection[str] = ()) -> tuple[SessionSkill, str]:
        config = self.swarm.config
        self._check_dir()
        async with self._lock:
            limits = self.limits()
            try:
                info = await asyncio.to_thread(check_skill_dir, folder, limits)
            except SkillError as exc:
                raise skill_error(exc) from None
            existing = self._cache.get(name)
            if existing is not None and existing.digest == info.digest:
                self._undetach(name)
                return existing, "unchanged"
            if existing is not None and not replace:
                raise _error(f"A skill named '{name}' is already attached (different content). Attach it with "
                             "replace to update it.", 409, exists=[name])
            if existing is None and len(self._cache) >= config.skills.max_attached:
                raise _error(f"The swarm already has the maximum of {config.skills.max_attached} skills ([skills] "
                             "max_attached): detach one first.", 409)
            now = time.time()
            skill = SessionSkill(name=name, description=info.description, digest=info.digest, files=info.files,
                                 size=info.size, frm=frm, source=source, attached_by=actor,
                                 attached_at=existing.attached_at if existing else now, updated_at=now,
                                 attached_via=attached_via, manual=info.manual)
            try:
                await asyncio.to_thread(copy_skill, folder, self.dir, name, limits, info=info,
                                        meta=lambda copied: skill.meta())
            except SkillError as exc:
                raise skill_error(exc) from None
            except OSError as exc:
                raise _error(f"Cannot copy the skill '{name}': {exc.strerror or exc}", 500) from None
            self._cache[name] = skill
            await self._plan([name])   # before the mirror and the news: a ready environment shows as ready
            await self._sync(full=False)
            self.swarm.bus.emit("skill.attached", actor, name=name, files=skill.files, replaced=existing is not None,
                                via=via, attached_via=attached_via, **{"from": frm})
            self._undetach(name)
            if announce:
                key = "n_skill_updated" if existing is not None else "n_skill_added"
                self._announce(key, exclude, name=name, description=skill.description)
            await self.ensure_deps([name])   # in the background: the attach never waits for a build
            return skill, ("updated" if existing is not None else "attached")

    async def attach_source(self, actor: str, source: str, *, pick: list[str] | None = None, replace: bool = False,
                            via: str = "act") -> dict:
        """Import a source (a folder or .zip path on the hub's machine, or a Git URL) into the library, then attach
        what was imported. The import report (skills.import_source) plus `"attached": [{"name", "status", "files"}]`
        and `"failed": [{"name", "error", "status"}]` (imported into the library, not attached); with `need_pick`
        nothing is imported or attached."""
        if actor != USER:
            raise ToolError("Only the user can attach skills to the swarm.")
        spec = source_spec(source)
        lib = Library.from_config(self.swarm.config)
        try:
            report = await asyncio.to_thread(skills.import_source, lib, spec, pick=pick, replace=replace,
                                             workspace=self.swarm.workspace.root, by=USER, via=via)
        except SkillError as exc:
            raise skill_error(exc) from None
        attached, failed = [], []
        for entry in report["imported"]:
            try:   # one refusal (the cap, a clash) leaves the others attached: all of it is reported
                skill, status = await self.attach(actor, f"library:{entry['name']}", replace=replace, via=via)
            except ToolError as exc:
                failed.append({"name": entry["name"], "error": str(exc), "status": getattr(exc, "status", 400)})
                continue
            attached.append({"name": skill.name, "status": status, "files": skill.files})
        return {**report, "attached": attached, "failed": failed}

    async def detach(self, actor: str, name: str, *, via: str = "act") -> SessionSkill:
        """Remove an attached skill (its session copy and its mirror). A mirror folder Windows will not remove yet
        is left out of the index and removed by a later sync; the detach itself succeeds."""
        if actor != USER:
            raise ToolError("Only the user can detach skills from the swarm.")
        try:
            name = check_name(name)
        except SkillError as exc:
            raise skill_error(exc) from None
        self._check_dir()
        async with self._lock:
            skill = self._cache.get(name)
            if skill is None:
                raise _error(f"No skill '{name}' is attached. Attached: {', '.join(self.names()) or 'none'}.", 404)
            if not await asyncio.to_thread(_drop, self.dir / name):
                raise _error(f"Cannot detach the skill '{name}': a file of its session copy is in use; try again.",
                             409)
            del self._cache[name]
            self._forget_deps(name)
            await self._use()   # its environments stay for other sessions; cleanup decides later
            await self._sync(full=False)
            self.swarm.bus.emit("skill.detached", actor, name=name, via=via)
            self._detached_add(name)
            self._announce("n_skill_removed", None, name=name)
            return skill

    def detached(self) -> list[str]:
        """Names the user detached: a role never attaches them again (until the user attaches one)."""
        names = self.swarm.db.get(DETACHED_KEY) or []
        return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []

    def _detached_add(self, name: str) -> None:
        names = self.detached()
        if name not in names:
            self.swarm.db.set(DETACHED_KEY, names + [name])

    def _undetach(self, name: str) -> None:
        names = self.detached()
        if name in names:
            self.swarm.db.set(DETACHED_KEY, [n for n in names if n != name])

    def attached_again(self, names: Collection[str]) -> None:
        """The user attached these names again (SessionManager.start wrote their copies before the Swarm started):
        they leave the names the user detached, as a live attach takes them out."""
        for name in names:
            self._undetach(name)

    # ---- proposals and the user's decision ------------------------------------------------------------------
    def proposed(self) -> list[str]:
        """The names members proposed in this session (also those detached since): max_proposals counts them, and
        proposing one's own name again replaces it without counting."""
        names = self.swarm.db.get(PROPOSED_KEY) or []
        return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []

    def _taken_elsewhere(self, name: str) -> bool:
        """The library or the host library has `name`, or a catalog role lists it: a proposal under it would pass
        for that skill (a role attach, the approval clashing with the real one). Synchronous file work."""
        config = self.swarm.config
        if name in self._listed_by_roles():
            return True
        try:
            if Library.from_config(config).get(name) is not None:
                return True
            host = Library.host(config)
            return host is not None and host.get(name) is not None
        except SkillError:
            return True

    def _free_name(self, name: str, actor: str) -> str:
        """A name to suggest instead of a taken one: `<name>-<the proposer's first name>`, then `<name>-2`, ..."""
        first = skills.slug(actor.split(" ")[0]) if actor else ""
        tails = ([first] if first else []) + [str(i) for i in range(2, 100)]
        for tail in tails:
            candidate = f"{name[:NAME_MAX - len(tail) - 1].rstrip('-')}-{tail}"
            if candidate not in self._cache and not self._taken_elsewhere(candidate):
                return candidate
        return f"{name[:NAME_MAX - 3]}-99"

    def _intact(self, skill: SessionSkill, limits: Limits) -> bool:
        """The session copy still holds the content recorded for it (an agent's shell may have written there)."""
        try:
            return check_skill_dir(self.dir / skill.name, limits).digest == skill.digest
        except SkillError:
            return False

    async def propose(self, actor: str, path: str) -> str:
        """A member's skill_propose: the folder (or its SKILL.md) at `path` in the workspace is checked strictly and
        attached at once (`from` agent, `library` pending), every other member and the user are told, and the user
        decides whether it is saved to the library (decide). Proposing one's own name again replaces it."""
        config = self.swarm.config
        if actor == USER:
            raise ToolError("Members propose skills; the user attaches them with skill_attach.")
        if config.skills.max_proposals <= 0:
            raise ToolError("Proposing skills is turned off in this swarm's settings.")
        workspace = self.swarm.workspace
        folder = workspace.resolve(path)
        if folder.is_file() and folder.name.casefold() == SKILL_MD.casefold():
            folder = folder.parent
        rel = workspace.rel(folder)
        if _covers(SKILLS_MIRROR, rel):
            raise ToolError("That is the swarm's copy of an attached skill. Copy the folder (without node_modules) "
                            "into your scratch folder, edit it there, and propose that copy.")
        if not folder.is_dir():
            raise ToolError(f"'{rel}' is not a folder: give the skill's folder (the one holding its SKILL.md)." if
                            folder.exists() else f"'{rel}' does not exist: write the skill's folder first.")
        self._check_dir()
        limits = self.limits()
        try:
            info = await asyncio.to_thread(check_skill_dir, folder, limits, strict=True)
        except SkillError as exc:
            raise _error(f"{exc} Fix SKILL.md and call skill_propose again.", exc.status) from None
        name = info.name
        async with self._lock:
            existing = self._cache.get(name)
            own = existing is not None and existing.frm == "agent" and existing.owner == actor
            if existing is not None and not own:
                suggestion = await asyncio.to_thread(self._free_name, name, actor)
                raise _error(f"A skill named '{name}' is already attached; choose another name, e.g. "
                             f"'{suggestion}'.", 409)
            if not own and await asyncio.to_thread(self._taken_elsewhere, name):
                suggestion = await asyncio.to_thread(self._free_name, name, actor)
                raise _error(f"The skills library already has a skill named '{name}' (or a role lists it); choose "
                             f"another name, e.g. '{suggestion}'.", 409)
            proposed = self.proposed()
            if name not in proposed and len(proposed) >= config.skills.max_proposals:
                raise _error(f"The members of this swarm have proposed the maximum of {config.skills.max_proposals} "
                             "skills ([skills] max_proposals). Improve one of yours by proposing it again under its "
                             "name.", 409)
            if existing is None and len(self._cache) >= config.skills.max_attached:
                raise _error(f"The swarm already has the maximum of {config.skills.max_attached} skills ([skills] "
                             "max_attached); the user can detach some.", 409)
            path_text = self._path_text(name, self.swarm.language_of(actor))
            if (existing is not None and existing.digest == info.digest
                    and await asyncio.to_thread(self._intact, existing, limits)):
                return (f"Your skill \"{name}\" is unchanged (same content as the attached one, library status: "
                        f"{existing.library}); every member sees it at {path_text}.")
            now = time.time()
            skill = SessionSkill(name=name, description=info.description, digest=info.digest, files=info.files,
                                 size=info.size, frm="agent", source={"path": rel}, attached_by=actor,
                                 attached_at=existing.attached_at if existing else now, updated_at=now, owner=actor,
                                 attached_via="proposal", manual=info.manual, library="pending")
            try:
                await asyncio.to_thread(copy_skill, folder, self.dir, name, limits, strict=True, info=info,
                                        meta=lambda copied: skill.meta())
            except SkillError as exc:
                raise skill_error(exc) from None
            except OSError as exc:
                raise _error(f"Cannot copy the skill '{name}': {exc.strerror or exc}", 500) from None
            self._cache[name] = skill
            if name not in proposed:
                self.swarm.db.set(PROPOSED_KEY, proposed + [name])
            try:
                await self._plan([name])
                await self._sync(full=False)   # whoever is told finds the mirror copy already there
            finally:
                # told even when the sync's await is cancelled: no silent proposal (as mcp_create)
                self.swarm.bus.emit("skill.proposed", actor, name=name, owner=actor, files=skill.files,
                                    replaced=existing is not None)
                if existing is not None:
                    self._announce("n_skill_updated", actor, name=name, description=skill.description)
                else:
                    self._announce("n_skill_shared", actor, name=name, description=skill.description, owner=actor)
                self.swarm.notify(USER, "u_skill_proposed", owner=actor, name=name, files=skill.files,
                                  description=skill.description.rstrip(" ."),   # the text puts its own period
                                  installs=self._installs_text(name))
            await self.ensure_deps([name])
        size = f"{files_text(skill.files)}, {format_size(skill.size)}"
        if existing is not None:
            head = (f"Your skill \"{name}\" was replaced by the new version ({size}); every member sees it at "
                    f"{path_text}. The user was asked again whether to save it to the skills library")
        else:
            head = (f"Skill \"{name}\" is attached to the swarm ({size}) and every member sees it at {path_text}. "
                    "The user was asked whether to save it to the skills library")
        text = head + "; you are told the answer. Edit it by proposing the same name again."
        if info.skipped:
            text += " Left out: " + "; ".join(f"{s['path']} ({s['error']})" for s in info.skipped[:20]) + "."
        return text + self.deps_result(name, "you are", quiet=self.swarm.kind_of(actor) == AgentKind.LLM)

    async def decide(self, actor: str, name: str, approve: bool, *, comment: str = "", replace: bool = False,
                     via: str = "act") -> str:
        """The user's skill_approve: save a pending proposal to the library (approve) or not (it stays attached to
        this session either way). `via`: the surface, recorded in the library entry and on `skill.library`."""
        if actor != USER:
            raise ToolError("Only the user can decide whether a proposed skill is saved to the skills library.")
        try:
            name = check_name(name)
        except SkillError as exc:
            raise skill_error(exc) from None
        self._check_dir()
        config = self.swarm.config
        async with self._lock:
            if name not in self._cache:
                raise _error(f"No skill '{name}' is attached. Attached: {', '.join(self.names()) or 'none'}.", 404)
            updated, text = await asyncio.to_thread(
                decide_files, config.data_dir, Library.from_config(config), name, approve, comment=comment,
                replace=replace, via=via, project=config.swarm.name, session=self.swarm.session_id, stopped=False)
            self._cache[name] = updated
            self.swarm.bus.emit("skill.library", actor, name=name, owner=updated.owner, status=updated.library,
                                comment=updated.comment, via=via)
            if updated.owner and updated.owner != USER and self.swarm.is_member(updated.owner):
                key = "n_skill_saved" if approve else "n_skill_not_saved"
                self.swarm.notify(updated.owner, key, name=name, comment=decision_comment(updated.comment))
            return text

    # ---- role skills ----------------------------------------------------------------------------------------
    async def ensure_role_skills(self, names: list[str] | None = None, *, announce: bool = True) -> dict:
        """Attach the skills the roles of the given members (default: every member) list and the session lacks
        (actor USER, `attached_via` "role:<Role>"; resolved like a bare name: the library, ~/.claude/skills, the host
        library, never the workspace's .claude/skills). Names the user detached stay detached; the user is told once
        about names nothing has and about names past max_attached. Never raises (a hire or a start goes on).
        `announce`: the other members are told (the given ones see the full index at their next wake-up). Returns
        {"attached": [names], "missing" / "detached" / "over_cap": {role: [names]}}."""
        report: dict[str, Any] = {"attached": [], "missing": {}, "detached": {}, "over_cap": {}}
        try:
            await self._ensure_role_skills(names, announce, report)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - role skills never keep a member from being hired or a swarm from starting
            log.exception("could not attach the role skills")
        return report

    async def _ensure_role_skills(self, names: list[str] | None, announce: bool, report: dict) -> None:
        config = self.swarm.config
        members = [m for m in self.swarm.members() if names is None or m["name"] in names]
        detached = set(self.detached())
        done: set[str] = set()
        for member in members:
            entry = roles.find_role(config.roles, member.get("role"), member.get("language") or self.swarm.language)
            if entry is None or not entry.skills or entry.key in done:
                continue
            done.add(entry.key)
            role = entry.name
            for name in entry.skills:
                if name in self._cache:
                    continue
                if name in detached:
                    report["detached"].setdefault(role, []).append(name)
                    continue
                if len(self._cache) >= config.skills.max_attached:
                    report["over_cap"].setdefault(role, []).append(name)
                    continue
                try:
                    await self.attach(USER, name, announce=announce, attached_via=f"role:{role}", via="role",
                                      exclude=names or ())
                except ToolError as exc:
                    status = getattr(exc, "status", 400)
                    if status == 404:
                        report["missing"].setdefault(role, []).append(name)
                    elif status == 409 and name not in self._cache and len(self._cache) >= config.skills.max_attached:
                        report["over_cap"].setdefault(role, []).append(name)
                    else:
                        log.warning("cannot attach the skill %s of the role %s: %s", name, role, exc)
                    continue
                report["attached"].append(name)
        for kind in ("missing", "over_cap"):
            for role, listed in report[kind].items():
                seen = (kind, role, tuple(listed))
                if seen in self._reported:
                    continue
                self._reported.add(seen)
                if kind == "missing":
                    self.swarm.notify(USER, "u_role_skills_missing", role=role, names=", ".join(listed))
                else:
                    self.swarm.notify(USER, "u_role_skills_over_cap", role=role, names=", ".join(listed),
                                      max=config.skills.max_attached)

    def note_hire(self, name: str, report: dict) -> None:
        self._hired[name] = report

    def hire_report(self, name: str) -> dict | None:
        """The ensure_role_skills report of `name`'s hire (once: hire_agent words its result with it)."""
        return self._hired.pop(name, None)

    # ---- script dependencies (skill_deps.py) ----------------------------------------------------------------
    @property
    def manager(self) -> skill_deps.EnvManager:
        return skill_deps.EnvManager.for_config(self.swarm.config)

    @property
    def owner(self) -> str:
        """This session in the environments' in-use sets and view names: its id, or (a standalone `dreamteam run`)
        its project folder."""
        return self.swarm.session_id or str(Path(self.swarm.config.project_dir).resolve())

    def _specs(self, name: str) -> list[skill_deps.EnvSpec]:
        entry = self._plans.get(name)
        return [s for s in (entry[1] if entry else []) if isinstance(s, skill_deps.EnvSpec)]

    def _dirnames(self) -> set[str]:
        return {s.dirname for name in list(self._plans) for s in self._specs(name)}

    def _shell_name(self) -> str:
        """The swarm's shell ({python} is written for it): every member runs commands through the hub's `shell`."""
        program = self.swarm.config.shell.program
        if self._shell is None or self._shell[0] != program:
            try:
                resolved = resolve_shell(program)[0]
            except ValueError:
                resolved = "bash"
            self._shell = (program, resolved)
        return self._shell[1]

    def _plan_files(self, wanted: list[SessionSkill], known: dict[str, tuple[tuple, list]]) -> tuple[dict, dict]:
        """(in a daemon thread) The plans of `wanted` whose content or [skills] dependency settings changed, this
        session's in-use set (before anything is peeked), and the views of ready Node environments."""
        config = self.swarm.config
        manager = self.manager
        network = not netoff.is_off(config)   # a session without network installs no member's proposal
        settings_key = (skill_deps.settings_hash(config.skills), network)
        made: dict[str, tuple[tuple, list]] = {}
        for skill in wanted:
            key = (skill.digest, settings_key)
            if known.get(skill.name, ((),))[0] == key:
                continue
            try:
                items = manager.plan(self.dir / skill.name, skill.name, skill_deps.trust_class(skill.frm),
                                     config.skills, config=config, proposal=skill.frm == "agent", network=network)
            except Exception:  # noqa: BLE001 - a skill's dependencies never keep it from being attached
                log.exception("cannot plan the dependencies of the skill %s", skill.name)
                items = []
            made[skill.name] = (key, items)
        plans = {**known, **made}
        names = {s.name for s in wanted} | set(known)
        manager.use(self.owner, {s.dirname for n in names for s in (plans.get(n) or ((), []))[1]
                                 if isinstance(s, skill_deps.EnvSpec)})
        views: dict[str, dict[str, str]] = {}
        for skill in wanted:
            for spec in (plans.get(skill.name) or ((), []))[1]:
                if not isinstance(spec, skill_deps.EnvSpec) or spec.ecosystem != "node":
                    continue
                if manager.peek(spec, self.owner)["status"] != "ready":
                    continue
                try:
                    views.setdefault(skill.name, {})[spec.manifest.dir] = str(manager.view(spec, self.owner))
                except (SkillError, OSError) as exc:
                    log.warning("cannot make the Node view of the skill %s: %s", skill.name, exc)
        return made, views

    async def _plan(self, names: list[str] | None = None) -> None:
        """Plan the dependencies of the given attached skills (default: all) off the loop and hold them in use;
        note what this session sees (a later change is announced only after it saw `installing`)."""
        wanted = [self._cache[n] for n in (names if names is not None else self.names()) if n in self._cache]
        known = {n: e for n, e in self._plans.items() if n in self._cache}
        try:
            made, views = await procs.daemon_call(self._plan_files, wanted, known)
        except Exception:  # noqa: BLE001 - see _plan_files
            log.exception("cannot plan the skills' dependencies")
            return
        for name in [n for n in self._plans if n not in self._cache]:
            self._forget_deps(name)
        for name, entry in made.items():
            old = self._plans.get(name)
            if old is not None and {s.dirname for s in self._specs(name)} != {
                    s.dirname for s in entry[1] if isinstance(s, skill_deps.EnvSpec)}:
                self._forget_links(name)
            self._plans[name] = entry
        for name, found in views.items():
            self._link_targets.setdefault(name, {}).update(found)
            self._mirror_links.setdefault(name, {}).update(found)
        for name in made:
            for item in made[name][1]:
                state = self.manager.peek(item, self.owner)
                status = state["status"]
                if status == "ready" and state["ecosystem"] == "node" and name not in views:
                    status = "installing"   # its view is made next (_prepare_node), then the news
                self._seen[(name, state["ecosystem"])] = status

    async def _use(self) -> None:
        try:
            await procs.daemon_call(self.manager.use, self.owner, self._dirnames())
        except Exception:  # noqa: BLE001 - only cleanup's view of what is in use depends on it
            log.exception("cannot update the skills' environments in use")

    def _forget_links(self, name: str) -> None:
        self._link_targets.pop(name, None)
        self._mirror_links.pop(name, None)
        self._node_link.pop(name, None)
        self._view_failed.pop(name, None)

    def _forget_deps(self, name: str) -> None:
        self._plans.pop(name, None)
        self._forget_links(name)
        for key in [k for k in self._seen if k[0] == name]:
            del self._seen[key]

    def deps(self, name: str) -> list[dict]:
        """The dependency states of an attached skill (skill_deps.EnvManager.peek, from memory and stats; [] when it
        lists none). A ready Node environment shows as installing until this session's view is linked, as failed
        while that view cannot be made: an agent is never pointed at the shared environment itself."""
        out = []
        entry = self._plans.get(name)
        for item in entry[1] if entry else []:
            state = self.manager.peek(item, self.owner)
            if state.get("ecosystem") == "node" and state.get("status") == "ready":
                link = self._node_link.get(name)
                rel = item.manifest.dir if isinstance(item, skill_deps.EnvSpec) else ""
                view = self._link_targets.get(name, {}).get(rel)
                if view is None and name in self._view_failed:
                    state = {k: v for k, v in state.items() if k not in ("node_modules", "commands")}
                    state.update(status="failed", log=str(self.manager.log_path(item.dirname)),
                                 error=f"this session's view of its node_modules could not be made: "
                                       f"{self._view_failed[name]}")
                elif link is None or view is None:
                    state = {**state, "status": "installing"}
                else:
                    state["linked"] = link == "linked"
                    state["view"] = view
                    state["bin"] = self._node_bin(name, rel)
            out.append(state)
        return out

    def _node_bin(self, name: str, rel: str) -> str:
        if self._is_local():
            return f"{SKILLS_MIRROR}/{name}/{_node_place(rel)}/.bin"
        return str(Path(self.dir, name, *[p for p in rel.split("/") if p], "node_modules", ".bin"))

    def _reason(self, reason: dict, lang: str) -> str:
        code = reason.get("code")
        if code == "unavailable":
            return prompts.t(lang, "deps_reason_unavailable", detail=_no_parens(str(reason.get("detail") or "")))
        if code == "python":
            return prompts.t(lang, "deps_reason_python", need=_no_parens(str(reason.get("need") or "")),
                             have=_no_parens(str(reason.get("have") or "")))
        if code == "unsupported":
            return prompts.t(lang, "deps_reason_unsupported", detail=_no_parens(str(reason.get("detail") or "")))
        if code == "proposals":
            return prompts.t(lang, "deps_reason_proposals")
        if code == "network":
            return prompts.t(lang, "deps_reason_network")
        return prompts.t(lang, "deps_reason_off")

    def _fragment(self, state: dict, lang: str, shell: str, quiet: bool = True) -> str:
        files = ", ".join(state.get("files") or [])
        status = state.get("status")
        if status == "ready":
            commands = list(state.get("commands") or [])
            names = ", ".join(commands[:5]) + (", …" if len(commands) > 5 else "")
            extra = prompts.t(lang, "skills_deps_bin", names=names, bin=state.get("bin") or "") if commands else ""
            if state.get("ecosystem") == "python":
                if state.get("has_pyproject"):
                    extra += prompts.t(lang, "skills_deps_own")
                return prompts.t(lang, "skills_deps_python", python=skill_deps.shell_ready(state["python"], shell),
                                 files=files, extra=extra)
            if state.get("linked") is False:
                return prompts.t(lang, "skills_deps_node_path", files=files, path=state.get("view") or "")
            return prompts.t(lang, "skills_deps_node", files=files, extra=extra)
        if status == "installing":
            return prompts.t(lang, "skills_deps_installing_quiet" if quiet else "skills_deps_installing", files=files)
        if status == "failed":
            return prompts.t(lang, "skills_deps_failed", files=files, error=state.get("error") or "",
                             log=state.get("log") or "")
        return prompts.t(lang, "skills_deps_manual", files=files, reason=self._reason(state.get("reason") or {}, lang))

    def deps_fragment(self, name: str, language: str, shell: str | None = None, *, quiet: bool = True) -> str:
        """The dependency part of a skill's index line and notifications (docs/SPEC.md §19), "" when it lists none:
        the Python to run its scripts with (written for the swarm's shell), its linked Node packages, or why
        nothing is installed. `quiet`: for an AI member, whom the news of a finished build does not wake (an
        external member's wait_for_events returns with it)."""
        shell = shell or self._shell_name()
        return "".join(self._fragment(state, language, shell, quiet) for state in self.deps(name))

    def _installs_text(self, name: str) -> str:
        """u_skill_installs for a proposal whose packages are installed automatically, else ""."""
        specs = self._specs(name)
        if not specs:
            return ""
        files = ", ".join(f for s in specs for f in s.manifest.files)
        packages = [p for s in specs for p in s.manifest.packages]
        listed = ", ".join(packages[:10]) + (", …" if len(packages) > 10 else "")
        return prompts.t(self.swarm.language_of(USER), "u_skill_installs", files=files, packages=listed)

    def deps_result(self, name: str, who: str = "members are", *, quiet: bool = False) -> str:
        """The sentence a skill_attach or skill_propose result ends with (English; "" when it lists no packages).
        `quiet`: the caller is an AI member, whom the news of the finished build does not wake."""
        states = self.deps(name)
        if not states:
            return ""
        files = ", ".join(f for s in states for f in s.get("files") or [])
        if any(s["status"] == "installing" for s in states):
            if quiet:
                return (f" Its packages ({files}) are being installed; a later wake-up of yours says when they are "
                        "ready (the install does not wake you).")
            return f" Its packages ({files}) are being installed; {who} told when they are ready."
        if all(s["status"] == "ready" for s in states):
            return " Its packages are installed already."
        manual = [s for s in states if s["status"] in ("off", "unsupported", "unavailable")]
        if manual:
            return " " + "; ".join(f"Its {', '.join(s.get('files') or [])} is not installed automatically: "
                                   f"{self._reason(s.get('reason') or {}, 'en')}" for s in manual) + "."
        failed = next(s for s in states if s["status"] == "failed")
        return f" Installing its packages failed: {failed.get('error')}."

    def deps_summary(self, name: str) -> str:
        """`packages: python ready; node manual (...)` for the user's skills listing ("" without packages)."""
        parts = []
        for s in self.deps(name):
            status = s["status"]
            if status == "failed":
                status = f"failed ({s.get('error')})"
            elif status in ("off", "unsupported", "unavailable"):
                status = f"manual ({self._reason(s.get('reason') or {}, 'en')})"
            parts.append(f"{s['ecosystem']} {status}")
        return "packages: " + "; ".join(parts) if parts else ""

    def _spawn(self, coro: Any, label: str) -> None:
        task = asyncio.get_running_loop().create_task(coro, name=label)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("a skill dependency task failed", exc_info=task.exception())

    @staticmethod
    def _watch(future: asyncio.Future) -> None:
        """A build's outcome arrives through the subscription; its future only has its exception retrieved."""
        def done(f: asyncio.Future) -> None:
            if not f.cancelled() and f.exception() is not None:
                log.warning("a skill environment build failed: %s", f.exception())
        future.add_done_callback(done)

    async def replan(self) -> None:
        """The session's network changed: every skill's dependencies are planned again (without network a member's
        proposal gets no install) and what the new plans lack is built."""
        if not self._active:
            return
        async with self._lock:
            await self._plan()
        await self.ensure_deps()

    async def ensure_deps(self, names: list[str] | None = None, *, force: bool = False,
                          ecosystem: str | None = None) -> dict:
        """Build the environments the given skills (default: all attached) still lack, in the background: never
        waits for a build (the subscription tells the members when it ends). {"started": [...], "ready": [...]},
        each "name/ecosystem"."""
        report: dict[str, list[str]] = {"started": [], "ready": []}
        if not self._active:
            return report
        for name in names if names is not None else self.names():
            for spec in self._specs(name):
                if ecosystem is not None and spec.ecosystem != ecosystem:
                    continue
                state = self.manager.peek(spec, self.owner)
                if state["status"] == "ready" and not force:
                    report["ready"].append(f"{name}/{spec.ecosystem}")
                    if spec.ecosystem == "node" and self._node_link.get(name) is None:
                        self._spawn(self._prepare_node(name, spec), "skill-deps-view")
                    continue
                self._watch(self.manager.ensure(spec, owner=self.owner, force=force))
                report["started"].append(f"{name}/{spec.ecosystem}")
        return report

    async def _prepare_node(self, name: str, spec: skill_deps.EnvSpec) -> None:
        """A Node environment is ready: this session's view of it, the link in the mirror, then the news (when the
        members were told it was being installed, or that it failed). A view that cannot be made is a failure the
        minute check() retries."""
        try:
            view: Path | None = await procs.daemon_call(self.manager.view, spec, self.owner)
            why = ""
        except (SkillError, OSError) as exc:
            log.warning("cannot make the Node view of the skill %s: %s", name, exc)
            view, why = None, _no_parens(str(exc)) or type(exc).__name__
        if name not in self._cache or spec.dirname not in {s.dirname for s in self._specs(name)}:
            return   # detached or replaced meanwhile
        key = (name, "node")
        before = self._seen.get(key)
        if view is None:
            self._view_failed[name] = why
            self._seen[key] = "failed"
            if before in ("installing", "retrying"):
                self._announce("n_skill_deps_failed", None, name=name)
            return
        self._view_failed.pop(name, None)
        self._link_targets.setdefault(name, {})[spec.manifest.dir] = str(view)
        self._mirror_links.setdefault(name, {})[spec.manifest.dir] = str(view)
        await self.sync_mirror(full=False)
        self._node_link.setdefault(name, "failed")
        self._seen[key] = "ready"
        if before in ("installing", "retrying", "failed"):
            self._announce("n_skill_deps_ready", None, name=name)

    def _on_env(self, dirname: str, state: dict, phase: str) -> None:
        """A published environment state (EnvManager, on this loop): for every skill of this session using it, the
        `skill.deps` event; at the end of a build the members' news."""
        if not self._active or phase not in ("started", "done"):
            return
        for name in list(self._plans):
            for spec in self._specs(name):
                if spec.dirname == dirname:
                    self._env_changed(name, spec, state, phase)

    def _env_changed(self, name: str, spec: skill_deps.EnvSpec, state: dict, phase: str) -> None:
        status = state.get("status")
        data = {"name": name, "ecosystem": spec.ecosystem, "status": status, "files": list(spec.manifest.files),
                "env": state.get("env"), "key": spec.key, "installer": state.get("installer"),
                "reused": bool(state.get("reused")), "seconds": state.get("seconds"), "error": state.get("error"),
                "reason": state.get("reason")}
        self.swarm.bus.emit("skill.deps", None, **{k: v for k, v in data.items() if v is not None})
        key = (name, spec.ecosystem)
        before = self._seen.get(key)
        if phase != "done":
            if before == "failed":   # the hourly retry: its success is news, another failure is not
                self._seen[key] = "retrying"
            return
        if status == "ready":
            if spec.ecosystem == "node":   # told once its view is linked
                self._spawn(self._prepare_node(name, spec), "skill-deps-view")
                return
            self._seen[key] = "ready"
            if before in ("installing", "retrying", "failed"):   # failed: built meanwhile by another process
                self._announce("n_skill_deps_ready", None, name=name)
        elif status == "failed":
            self._seen[key] = "failed"
            if before == "installing":
                self._announce("n_skill_deps_failed", None, name=name)
                self.swarm.notify(USER, "u_skill_deps_failed", name=name, files=", ".join(spec.manifest.files),
                                  error=state.get("error") or "", log=state.get("log") or "")

    async def reinstall(self, actor: str, name: str, ecosystem: str | None = None) -> str:
        """The user's skill_reinstall: build the skill's environments again (one ecosystem or both), also before
        the hourly retry of a failure. A ready environment other sessions use is refused with 409."""
        if actor != USER:
            raise ToolError("Only the user can reinstall the packages of a skill.")
        try:
            name = check_name(name)
        except SkillError as exc:
            raise skill_error(exc) from None
        if ecosystem not in (None, "", "python", "node"):
            raise ToolError("ecosystem is python or node.")
        if name not in self._cache:
            raise _error(f"No skill \"{name}\" is attached to this swarm.", 404)
        entry = self._plans.get(name)
        if entry is not None and any(isinstance(i, dict) and i.get("status") == "unavailable" for i in entry[1]):
            skill_deps.forget_failed_probes()   # planned again: the tool may answer now
            del self._plans[name]
        if name not in self._plans:
            await self._plan([name])
        items = (self._plans.get(name) or ((), []))[1]
        if not items:
            return f"Skill \"{name}\" has no requirements.txt, pyproject.toml or package.json."
        chosen = [i for i in items if not ecosystem or (i.ecosystem if isinstance(i, skill_deps.EnvSpec)
                                                        else i.get("ecosystem")) == ecosystem]
        if not chosen:
            return (f"Skill \"{name}\" has no Python packages (requirements.txt or pyproject.toml)."
                    if ecosystem == "python" else f"Skill \"{name}\" has no Node packages (package.json).")
        specs = [i for i in chosen if isinstance(i, skill_deps.EnvSpec)]
        if not specs:
            reasons = "; ".join(self._reason(i.get("reason") or {}, "en") for i in chosen)
            return f"Skill \"{name}\": its packages are not installed automatically ({reasons})."
        if any(self.manager.building(s.dirname) for s in specs):
            return (f"The packages of skill \"{name}\" are being installed already; members are told when they are "
                    "ready.")
        for spec in specs:
            try:
                await procs.daemon_call(self.manager.check_reinstall, spec, self.owner)
            except SkillError as exc:
                raise skill_error(exc) from None
        for spec in specs:
            self._seen[(name, spec.ecosystem)] = "installing"
            if spec.ecosystem == "node":
                self._node_link.pop(name, None)
                self._view_failed.pop(name, None)
            self._watch(self.manager.ensure(spec, owner=self.owner, force=True))
        files = ", ".join(f for s in specs for f in s.manifest.files)
        return f"Reinstalling the packages of skill \"{name}\" ({files}); members are told when they are ready."

    async def touch_envs(self, force: bool = False) -> None:
        """The scheduler's hourly look at this session's environments: their `used` stamps (cleanup goes by them),
        a broken one rebuilt (peek starts it), a failure older than an hour retried, and one that reads as being
        installed while nothing in this process builds it (a half-built folder a build left) built again."""
        now = time.time()
        if not self._active or (not force and now - self._last_touch < skill_deps.ENV_TOUCH_SECONDS):
            return
        self._last_touch = now
        dirnames = self._dirnames()
        if not dirnames:
            return
        try:
            await procs.daemon_call(self.manager.touch, self.owner, dirnames)
        except Exception:  # noqa: BLE001 - the stamps are refreshed at the next look
            log.exception("cannot touch the skills' environments")
        for name in list(self._plans):
            for spec in self._specs(name):
                state = self.manager.peek(spec, self.owner)
                if state["status"] == "installing":
                    self._seen[(name, spec.ecosystem)] = "installing"
                    if not self.manager.building(spec.dirname):   # (another process's build: ensure waits for it)
                        self._watch(self.manager.ensure(spec, owner=self.owner))
                elif state["status"] == "failed":
                    self._watch(self.manager.ensure(spec, owner=self.owner))

    async def stop_deps(self) -> None:
        """Swarm.stop: no more news about builds (they go on in the process: another session may share them), the
        view tasks cancelled, this session's in-use set released."""
        self._active = False
        try:
            if self._unsubscribe is not None:
                self._unsubscribe()
                self._unsubscribe = None
            tasks = list(self._tasks)
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            self.manager.use(self.owner, set())
        except Exception:  # noqa: BLE001 - the stop goes on; the process ends its locks anyway
            log.exception("cannot release the skills' environments")

    # ---- the mirror -----------------------------------------------------------------------------------------
    async def sync_mirror(self, full: bool = False) -> None:
        async with self._lock:
            await self._sync(full)

    async def check(self) -> None:
        """The scheduler's look at the mirror (at most every SKILLS_CHECK_SECONDS): a copy an agent's `shell` changed
        is rebuilt, a folder put there removed, a node_modules link repaired, a session view an agent's command
        emptied rebuilt."""
        if time.time() - self._last_sync < SKILLS_CHECK_SECONDS or self._lock.locked():
            return
        await self.sync_mirror()
        for name in set(self._mirror_links) | set(self._view_failed):
            wanted = self._mirror_links.get(name, {})
            for spec in self._specs(name):
                if spec.ecosystem != "node":
                    continue
                if ((name in self._view_failed or (spec.manifest.dir in wanted
                                                   and self.manager.view_ok(spec, self.owner) is None))
                        and self.manager.peek(spec, self.owner)["status"] == "ready"):
                    self._spawn(self._prepare_node(name, spec), "skill-deps-view")

    async def _sync(self, full: bool) -> None:
        cache = dict(self._cache)
        links = {n: dict(v) for n, v in self._mirror_links.items() if n in cache}
        targets = {n: dict(v) for n, v in self._link_targets.items() if n in cache}
        try:
            result = await asyncio.to_thread(self._sync_files, cache, full, links, targets)
        except Exception:  # noqa: BLE001 - the mirror is repaired by the next sync; the change itself stands
            log.exception("could not update the skills mirror")
            return
        for name, outcome in result.items():
            if name not in self._cache:
                continue
            if outcome.get("rebuilt"):   # a fresh copy: every known view is linked again
                self._mirror_links[name] = dict(self._link_targets.get(name, {}))
            for rel in outcome.get("dropped", ()):
                self._mirror_links.get(name, {}).pop(rel, None)
            if outcome.get("linked"):
                self._node_link[name] = outcome["linked"]

    def _ensure_links(self, base: Path, wanted: dict[str, str]) -> dict:
        """The node_modules links `wanted` ({folder of package.json: view}) in the skill folder `base`: a missing one
        made, a dangling or re-pointed one made again, a real folder (an agent's own npm install there) left alone
        and dropped from the expected links. {"linked": "linked" | "failed" | None, "dropped": [folders]}."""
        out: dict[str, Any] = {"linked": None, "dropped": []}
        for rel, target in wanted.items():
            folder = Path(base, *[p for p in rel.split("/") if p])
            link = folder / "node_modules"
            state = skill_deps.link_state(link, target)
            if state == "ok":
                out["linked"] = out["linked"] or "linked"
                continue
            if state == "real":
                out["dropped"].append(rel)
                out["linked"] = out["linked"] or "linked"
                if (str(base), rel) not in self._dropped_logged:
                    self._dropped_logged.add((str(base), rel))
                    log.info("%s holds a node_modules folder of its own (an agent's install): left alone", folder)
                continue
            if state == "wrong":
                skill_deps.remove_link(link)
            if not os.path.isdir(long_path(folder)) or not os.path.isdir(long_path(target)):
                continue   # the copy has no such folder; a view gone is rebuilt by check()
            out["linked"] = "linked" if skill_deps.link_node_modules(folder, "", target) else "failed"
        return out

    def _sync_files(self, cache: dict[str, SessionSkill], full: bool, links: dict[str, dict[str, str]] | None = None,
                    targets: dict[str, dict[str, str]] | None = None) -> dict[str, dict]:
        """Bring `.scratch/_skills/` in line with the session copies (in a thread, under the files lock): a copy is
        rebuilt when `full`, when it is missing, or when the listing of every entry in it (_listing: also the
        skip-list's names and links, but not DreamTeam's node_modules links) differs from the one written; anything
        else there is removed. Every rebuild is copy_skill's build-and-swap, read-only; then the Node links of
        `links` (`targets`: every known view, linked again in a rebuilt copy). Without a mirror the links go into the
        session copies. Returns {name: {"rebuilt", "linked", "dropped"}}."""
        links, targets = links or {}, targets or {}
        out: dict[str, dict] = {}
        with self._files:
            try:
                workspace = self.swarm.workspace
                workspace.ensure_scratch()
                self._local = scratch_is_local(workspace.root)
                if not self._local:  # no mirror: agents are shown the session copies (read with `shell`)
                    self._mirror.clear()
                    self._pending_removal.clear()
                    for name in sorted(cache):
                        if links.get(name):
                            out[name] = self._ensure_links(self.dir / name, links[name])
                    return out
                mirror = self.mirror_dir
                if is_link(mirror) or (os.path.lexists(long_path(mirror)) and not os.path.isdir(long_path(mirror))):
                    skills._remove(mirror)   # put there by a command: replaced, a link never followed
                os.makedirs(long_path(mirror), exist_ok=True)
                clean_stale(mirror, older_than=0)   # an interrupted rebuild's leftovers (only this lock writes here)
                limits = self.limits()
                for name in sorted(cache):
                    target = mirror / name
                    known = self._mirror.get(name)
                    spots = {_node_place(rel) for rel in targets.get(name, {})}
                    if (not full and known is not None and known[0] == cache[name].digest and not is_link(target)
                            and _listing(target, cap=len(known[1]), skip=spots) == known[1]):
                        if links.get(name):
                            out[name] = self._ensure_links(target, links[name])
                        continue
                    try:
                        copied = copy_skill(self.dir / name, mirror, name, limits, readonly=True)
                    except (SkillError, OSError) as exc:
                        self._mirror.pop(name, None)
                        log.warning("cannot copy the skill %s into %s: %s", name, SKILLS_MIRROR, exc)
                        continue
                    self._mirror[name] = (cache[name].digest, _expected_listing(copied.manifest))
                    self._pending_removal.discard(name)
                    out[name] = {"rebuilt": True, **self._ensure_links(target, targets.get(name, {}))}
                with os.scandir(long_path(mirror)) as it:
                    extra = sorted(e.name for e in it if e.name not in cache and e.name[:3] not in (".t-", ".o-"))
                for name in extra:   # detached, or put there by a command
                    self._mirror.pop(name, None)
                    if _drop(mirror / name):
                        self._pending_removal.discard(name)
                    else:
                        self._pending_removal.add(name)
                self._pending_removal &= set(extra)
            finally:
                self._last_sync = time.time()
        return out

    async def remove_mirror(self) -> None:
        """Remove `.scratch/_skills` (Swarm.stop in an adopted folder: no copy of a library or a private Claude Code
        skill stays in the user's project). Best effort."""
        try:
            await asyncio.to_thread(self._remove_mirror)
        except Exception:  # noqa: BLE001 - the stop goes on
            log.exception("could not remove the skills mirror")

    def _remove_mirror(self) -> None:
        with self._files:
            mirror = self.mirror_dir
            if scratch_is_local(self.swarm.workspace.root) and os.path.lexists(long_path(mirror)):
                if is_link(mirror) or not os.path.isdir(long_path(mirror)):
                    skills._remove(mirror)
                else:
                    rmtree(mirror)
            self._mirror.clear()
            self._pending_removal.clear()


__all__ = ["DETACHED_KEY", "PROPOSED_KEY", "SKILLS_CHECK_SECONDS", "SessionSkill", "SessionSkills", "decide_files",
           "listed_by_roles", "public_item", "read_session_skills", "role_skill_pairs", "skill_error", "skills_dir",
           "source_spec", "stopped_public"]
