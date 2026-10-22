"""Frozen LLM tool sets (docs/SPEC.md §6.1).

The tools array an LLM agent sends is part of its prompt prefix, and Anthropic binds thinking blocks to the
exact prefix: a tool whose name, description or parameters change under a running agent costs it its cache
and its thinking. So every agent keeps the array it was given at its first runtime start, stored in kv
`tools:<name>` as {"rev", "language", "tools"}, and sends exactly that. It moves to the current array only
inside a compaction (agent.py `_replace_with_summary`), when its history no longer holds an assistant turn bound
to the old one.

Every released array is a numbered revision shipped in `snapshots/llm_r<N>.json` and never edited. A change to
an LLM tool schema (a name, description, parameter, `required`, an enum or the order) is released as a new
revision (rule R0′):
1. change the code;
2. bump TOOLSET_REVISION;
3. run `python -m dreamteam.tools.frozen --write`;
4. pin the new exact-order digests in tests/test_frozen_tools.py (RELEASED).
The registry must keep serving every released revision (no tool or property removed, enums only grow...):
arguments are still validated against the current registry. tests/test_frozen_tools.py checks all of it.
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

# what schemas_for("llm", ...) builds; bump it with EVERY LLM schema change (2: background jobs; 3: skill_propose)
TOOLSET_REVISION = 3
BASELINE_REVISION = 1  # the array agents created before tool sets were frozen last sent (sorted digest e3c5cbc7…)
KV_PREFIX = "tools:"
LANGUAGES = ("en", "ru")
SNAPSHOT_DIR = Path(__file__).resolve().with_name("snapshots")
_SNAPSHOT_NAME = re.compile(r"llm_r(\d+)\.json")


def kv_key(name: str) -> str:
    return KV_PREFIX + name


def normalize(tools: list[dict]) -> list[dict]:
    """`tools` as the kv store gives it back (plain JSON): what is stored is exactly what is sent."""
    return json.loads(dumps(tools))


def _language(language: str | None) -> str:
    return language if language in LANGUAGES else "en"


@functools.lru_cache(maxsize=None)
def _snapshot_text(rev: int) -> str:
    return (SNAPSHOT_DIR / f"llm_r{rev}.json").read_text(encoding="utf-8")


def revisions() -> list[int]:
    """The revisions shipped with this code, sorted."""
    found = (_SNAPSHOT_NAME.fullmatch(p.name) for p in SNAPSHOT_DIR.glob("llm_r*.json"))
    return sorted(int(m.group(1)) for m in found if m)


def snapshot(rev: int, language: str | None) -> list[dict]:
    """A fresh copy of the shipped array of revision `rev` (a language without texts gets English, like
    schemas_for)."""
    return json.loads(_snapshot_text(int(rev)))[_language(language)]


def current(language: str | None) -> list[dict]:
    """The array the code builds now (revision TOOLSET_REVISION)."""
    from . import schemas_for  # the tool modules import this one's package

    return normalize(schemas_for("llm", language or "en"))


def record(rev: int, language: str | None, tools: list[dict]) -> dict:
    return {"rev": int(rev), "language": language, "tools": tools}


def current_record(language: str | None) -> dict:
    return record(TOOLSET_REVISION, language, current(language))


def initial_record(language: str | None, has_history: bool) -> dict:
    """The record of an agent that has none yet. An agent with a history ran before tool sets were frozen, so it
    last sent the baseline revision; a new one gets the current revision."""
    if has_history:
        return record(BASELINE_REVISION, language, snapshot(BASELINE_REVISION, language))
    return current_record(language)


def _valid(rec: Any) -> bool:
    if not isinstance(rec, dict) or not isinstance(rec.get("rev"), int) or isinstance(rec.get("rev"), bool):
        return False
    tools = rec.get("tools")
    return isinstance(tools, list) and all(
        isinstance(t, dict) and isinstance(t.get("name"), str) and isinstance(t.get("parameters"), dict)
        for t in tools)


def read(db: Any, name: str) -> dict | None:
    """The agent's stored record; None when it has none (or an unusable one, which is logged)."""
    try:
        rec = db.get(kv_key(name))
    except ValueError:
        rec = "(not JSON)"
    if rec is None:
        return None
    if _valid(rec):
        return rec
    log.warning("the stored tool set of %s is invalid and is ignored", name)
    return None


def write(db: Any, name: str, rec: dict) -> None:
    """Store the agent's record (also inside `db.transaction()`: the lock is re-entrant)."""
    db.set(kv_key(name), rec)


def names(tools: list[dict]) -> list[str]:
    return [t["name"] for t in tools]


def for_span(db: Any, name: str, language: str | None, revision: Any) -> list[dict]:
    """The tools an LLM call of agent `name` sent, for traces. A span without a revision was recorded before
    tool sets were frozen: the agent then sent the baseline."""
    if revision is None:
        return snapshot(BASELINE_REVISION, language)
    rec = read(db, name)
    if rec is not None and rec["rev"] == revision:
        return rec["tools"]
    if revision in revisions():
        return snapshot(revision, language)
    return rec["tools"] if rec is not None else current(language)


def member_tool_names(swarm: Any, member: dict) -> frozenset[str]:
    """The tools a member can call: an LLM agent's own frozen set (from its runtime, else from its record, else
    the set its first start will freeze; nothing is written), the live registry for the other kinds."""
    from . import tools_for

    name = member.get("name")
    runtime = (getattr(swarm, "runtimes", None) or {}).get(name)
    live = getattr(runtime, "tool_names", None)
    if live is not None:
        return frozenset(live)
    kind = member.get("kind") or "user"
    if kind != "llm":
        return frozenset(spec.name for spec in tools_for(kind))
    rec = read(swarm.db, name)
    if rec is None:
        has_history = swarm.db.scalar("SELECT 1 FROM history WHERE agent = ? LIMIT 1", (name,))
        rec = initial_record(member.get("language") or swarm.language, bool(has_history))
    return frozenset(names(rec["tools"]))


def _snapshot_document() -> str:
    data = {"revision": TOOLSET_REVISION, **{lang: current(lang) for lang in LANGUAGES}}
    return json.dumps(data, ensure_ascii=False, indent=1) + "\n"


def _matches(path: Path) -> bool:
    """The shipped file holds exactly what the code builds (exact order: it decides the request bytes)."""
    try:
        shipped = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return shipped.get("revision") == TOOLSET_REVISION and all(
        dumps(shipped.get(lang)) == dumps(current(lang)) for lang in LANGUAGES)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dreamteam.tools.frozen",
                                     description="Release or check the LLM tool set of this code "
                                                 f"(revision {TOOLSET_REVISION}).")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true",
                        help=f"write snapshots/llm_r{TOOLSET_REVISION}.json from the code")
    action.add_argument("--check", action="store_true", help="exit 1 if the code differs from that file")
    parser.add_argument("--force", action="store_true", help="with --write: overwrite a different existing file")
    args = parser.parse_args(argv)
    path = SNAPSHOT_DIR / f"llm_r{TOOLSET_REVISION}.json"
    if args.check:
        if _matches(path):
            print(f"{path.name}: the code builds revision {TOOLSET_REVISION}")
            return 0
        if not path.exists():
            print(f"{path.name} is missing: release revision {TOOLSET_REVISION} with --write.", file=sys.stderr)
        else:
            print(f"{path.name}: the code differs from revision {TOOLSET_REVISION}. Release the change as a new "
                  "revision: bump TOOLSET_REVISION, then run --write.", file=sys.stderr)
        return 1
    if path.exists() and not _matches(path) and not args.force:
        print(f"{path.name} exists and differs from the code. A released revision is never edited: bump "
              "TOOLSET_REVISION for a schema change (--force overwrites it anyway).", file=sys.stderr)
        return 2
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_snapshot_document().encode("utf-8"))  # bytes: the same "\n" line ends on every system
    _snapshot_text.cache_clear()
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
