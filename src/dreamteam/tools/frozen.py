"""Frozen LLM tool sets (docs/SPEC.md §6.1).

The tools array an LLM agent sends is part of its prompt prefix, and Anthropic binds thinking blocks to the
exact prefix: a tool whose name, description or parameters change under a running agent costs it its cache
and its thinking. So every agent keeps the array it was given at its first runtime start, stored in kv
`tools:<name>` as {"rev", "language", "tools", "mode"}, and sends exactly that. It moves to the current array of its
session's mode only inside a compaction (agent.py `_replace_with_summary`), when its history no longer holds an
assistant turn bound to the old one.

Every released array is a numbered revision of one session mode (docs/SPEC.md §22.5), never edited, and a number is
never reused: the next release of either mode takes the next free number. The revisions a session this version
opens can hold (OLDEST_OPEN_REVISION..NEWEST_REVISION) are shipped in `snapshots/llm_r<N>.json` (board) and
`snapshots/llm_thread_r<N>.json` (thread). A change to an LLM tool schema (a name, description, parameter,
`required`, an enum or the order) is released as a new revision in every mode whose array changes (rule R0′):
1. change the code;
2. set the mode's entry of TOOLSET_REVISIONS to NEWEST_REVISION + 1 (`--check` says which modes changed);
3. run `python -m dreamteam.tools.frozen --write`;
4. pin the new exact-order digests in tests/test_frozen_tools.py (RELEASED).
The registry must keep serving every shipped revision of each mode (no tool or property removed, enums only
grow...): arguments are still validated against the current registry. tests/test_frozen_tools.py checks all of it.
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

from ..db import dumps

log = logging.getLogger(__name__)

# what schemas_for("llm", ..., mode) builds, per session mode; a new number with EVERY LLM schema change of that
# mode, one numbering for both (1-4: the tool sets of 0.1.x, whose sessions are not opened; 5: crew tasks,
# task_create and task_update gain crew and directions; 6: the thread mode's first tool set)
TOOLSET_REVISIONS = {"board": 5, "thread": 6}
NEWEST_REVISION = max(TOOLSET_REVISIONS.values())   # the newest number this version knows
# a session with a member recorded below it (or an agent with a history and no record) was made by an earlier
# version and is not opened (docs/SPEC.md §17.2); a fixed literal for both modes, never derived from the others
OLDEST_OPEN_REVISION = 5
MODES = ("board", "thread")
KV_PREFIX = "tools:"
LANGUAGES = ("en", "ru")
SNAPSHOT_DIR = Path(__file__).resolve().with_name("snapshots")
_SNAPSHOT_NAME = re.compile(r"llm_(?:thread_)?r(\d+)\.json")


def kv_key(name: str) -> str:
    return KV_PREFIX + name


def normalize(tools: list[dict]) -> list[dict]:
    """`tools` as the kv store gives it back (plain JSON): what is stored is exactly what is sent."""
    return json.loads(dumps(tools))


def _language(language: str | None) -> str:
    return language if language in LANGUAGES else "en"


def _file_name(rev: int, mode: str) -> str:
    return f"llm_r{rev}.json" if mode == "board" else f"llm_{mode}_r{rev}.json"


def _shipped() -> list[int]:
    """The revisions whose snapshot files ship with this code (llm_r<N>.json, llm_thread_r<N>.json)."""
    found = (_SNAPSHOT_NAME.fullmatch(p.name) for p in SNAPSHOT_DIR.glob("llm_*.json"))
    return [int(m.group(1)) for m in found if m]


@functools.lru_cache(maxsize=None)
def _snapshot_text(rev: int) -> str:
    """The shipped file of revision `rev`, whatever its mode (FileNotFoundError: not shipped)."""
    for mode in MODES:
        path = SNAPSHOT_DIR / _file_name(rev, mode)
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError(f"no snapshot of the tool-set revision {rev}")


def mode_of(rev: int) -> str:
    """The mode of a shipped revision (a file without "mode" is a board one); ValueError for a number this code
    does not ship."""
    try:
        data = json.loads(_snapshot_text(int(rev)))
    except FileNotFoundError:
        raise ValueError(f"the tool-set revision {rev} is not shipped with this code") from None
    return data.get("mode") or "board"


def revisions(mode: str | None = None) -> list[int]:
    """The revisions shipped with this code, sorted: all of them, or those of one mode."""
    return sorted(rev for rev in _shipped() if mode is None or mode_of(rev) == mode)


def snapshot(rev: int, language: str | None) -> list[dict]:
    """A fresh copy of the shipped array of revision `rev`, whatever its mode (a language without texts gets
    English, like schemas_for)."""
    return json.loads(_snapshot_text(int(rev)))[_language(language)]


def current(language: str | None, mode: str = "board") -> list[dict]:
    """The array the code builds now for a session of `mode` (revision TOOLSET_REVISIONS[mode])."""
    from . import schemas_for  # the tool modules import this one's package

    return normalize(schemas_for("llm", language or "en", mode))


def record(rev: int, language: str | None, tools: list[dict], mode: str = "board") -> dict:
    return {"rev": int(rev), "language": language, "tools": tools, "mode": mode}


def current_record(language: str | None, mode: str = "board") -> dict:
    return record(TOOLSET_REVISIONS[mode], language, current(language, mode), mode)


def record_mode(rec: dict) -> str:
    """The mode a valid record states: its "mode", board when it has none (0.4.x wrote board records without it)."""
    return rec.get("mode") or "board"


def valid(rec: Any) -> bool:
    """A usable record: a dict with an integer (non-bool) `rev`, "mode" absent or a known mode, and `tools` a list
    of dicts, each with a str `name` and a dict `parameters`."""
    if not isinstance(rec, dict) or not isinstance(rec.get("rev"), int) or isinstance(rec.get("rev"), bool):
        return False
    if "mode" in rec and rec["mode"] not in MODES:
        return False
    tools = rec.get("tools")
    return isinstance(tools, list) and all(
        isinstance(t, dict) and isinstance(t.get("name"), str) and isinstance(t.get("parameters"), dict)
        for t in tools)


def read(db: Any, name: str, mode: str | None = None) -> dict | None:
    """The agent's stored record; None when it has none (or an unusable one, which is logged). With `mode`, a
    record of another mode (its "mode", missing = board, or the mode of its revision when this code ships it) is
    unusable too: only a damaged database holds one."""
    try:
        rec = db.get(kv_key(name))
    except ValueError:
        rec = "(not JSON)"
    if rec is None:
        return None
    if not valid(rec):
        log.warning("the stored tool set of %s is invalid and is ignored", name)
        return None
    if mode is not None:
        stated = record_mode(rec)
        try:
            shipped = mode_of(rec["rev"])
        except ValueError:
            shipped = stated  # a revision this code does not ship: its record's word
        if stated != mode or shipped != mode:
            log.warning("the stored tool set of %s is of the %s mode, not of the session's %s mode, and is ignored",
                        name, stated if stated != mode else shipped, mode)
            return None
    return rec


def write(db: Any, name: str, rec: dict) -> None:
    """Store the agent's record (also inside `db.transaction()`: the lock is re-entrant)."""
    db.set(kv_key(name), rec)


def names(tools: list[dict]) -> list[str]:
    return [t["name"] for t in tools]


def for_span(db: Any, name: str, language: str | None, revision: Any) -> list[dict]:
    """The tools an LLM call of agent `name` sent, for traces: the agent's record when its revision matches, else
    that revision's shipped snapshot, else the record's tools (the current set when it has none)."""
    rec = read(db, name)
    if rec is not None and rec["rev"] == revision:
        return rec["tools"]
    if revision in revisions():
        return snapshot(revision, language)
    return rec["tools"] if rec is not None else current(language)


def member_tool_names(swarm: Any, member: dict) -> frozenset[str]:
    """The tools a member can call: an LLM agent's own frozen set (from its runtime, else from its record, else
    the set its first start will freeze; nothing is written), the live registry of the session's mode for the other
    kinds."""
    from . import tools_for

    mode = getattr(swarm, "mode", "board")
    name = member.get("name")
    runtime = (getattr(swarm, "runtimes", None) or {}).get(name)
    live = getattr(runtime, "tool_names", None)
    if live is not None:
        return frozenset(live)
    kind = member.get("kind") or "user"
    if kind != "llm":
        return frozenset(spec.name for spec in tools_for(kind, mode))
    rec = read(swarm.db, name, mode)
    if rec is None:  # a session this version opens has no agent with a history and no record (§17.2)
        rec = current_record(member.get("language") or swarm.language, mode)
    return frozenset(names(rec["tools"]))


def _snapshot_document(mode: str) -> str:
    """A mode's current file; a board one has no "mode" inside (as llm_r5.json)."""
    head: dict[str, Any] = {"revision": TOOLSET_REVISIONS[mode]}
    if mode != "board":
        head["mode"] = mode
    data = {**head, **{lang: current(lang, mode) for lang in LANGUAGES}}
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n"


def _matches(path: Path, mode: str) -> bool:
    """The shipped file holds exactly what the code builds for `mode` (exact order: it decides the request bytes)."""
    try:
        shipped = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (shipped.get("revision") == TOOLSET_REVISIONS[mode] and (shipped.get("mode") or "board") == mode
            and all(dumps(shipped.get(lang)) == dumps(current(lang, mode)) for lang in LANGUAGES))


def _path(mode: str) -> Path:
    return SNAPSHOT_DIR / _file_name(TOOLSET_REVISIONS[mode], mode)


def _bump_hint(mode: str) -> str:
    return f'set TOOLSET_REVISIONS["{mode}"] to NEWEST_REVISION + 1 ({NEWEST_REVISION + 1})'


def main(argv: list[str] | None = None) -> int:
    shown = ", ".join(f"{m} {TOOLSET_REVISIONS[m]}" for m in MODES)
    parser = argparse.ArgumentParser(prog="python -m dreamteam.tools.frozen",
                                     description=f"Release or check the LLM tool sets of this code ({shown}).")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="write the missing current snapshot files from the code")
    action.add_argument("--check", action="store_true", help="exit 1 if the code differs from a current file")
    parser.add_argument("--force", action="store_true", help="with --write: overwrite a different existing file")
    parser.add_argument("--mode", choices=MODES, help="only this mode's file (default: every mode's)")
    args = parser.parse_args(argv)
    modes = [args.mode] if args.mode else list(MODES)
    status = 0
    for mode in modes:
        path, rev = _path(mode), TOOLSET_REVISIONS[mode]
        same = _matches(path, mode)
        if args.check:
            if same:
                print(f"{path.name}: the code builds the {mode} revision {rev}")
            elif not path.exists():
                print(f"{path.name} is missing: release the {mode} revision {rev} with --write.", file=sys.stderr)
            else:
                print(f"{path.name}: the code differs from the {mode} revision {rev}. Release the change as a new "
                      f"revision: {_bump_hint(mode)}, then run --write.", file=sys.stderr)
            status = status if same else 1
            continue
        if same:
            print(f"{path.name}: the code builds the {mode} revision {rev}, unchanged")
            continue
        if path.exists() and not args.force:
            print(f"{path.name} exists and differs from the code. A released revision is never edited: "
                  f"{_bump_hint(mode)} for a schema change (--force overwrites it anyway).", file=sys.stderr)
            status = 2
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_snapshot_document(mode).encode("utf-8"))  # bytes: the same "\n" line ends on every system
        _snapshot_text.cache_clear()
        print(f"wrote {path}")
    return status


if __name__ == "__main__":
    sys.exit(main())
