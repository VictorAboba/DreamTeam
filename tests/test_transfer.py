"""Export of a DreamTeam home (dreamteam.transfer.export_home): the folder layout, the secret rules, the guards of the
destination, and that the source home is only read. Import (plan_import / apply_import): add or rewrite by uid,
this computer's keys, env values, machine-local settings and folders kept, backups, refusals, undo and Ctrl+C."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dreamteam import hubauth, mcp_library, transfer
from dreamteam import sessions as sessions_module
from dreamteam import settings as st
from dreamteam.db import Database, known_columns
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.mcp_forge import snapshot_digest
from dreamteam.sessions import MOVE_JOURNAL, SessionManager, session_uid
from dreamteam.skills import Library
from dreamteam.tools.frozen import TOOLSET_REVISION
from dreamteam.transfer import (MANIFEST, SecretFound, Secrets, TransferError, apply_import, export_home,
                                known_secrets, merge_external, merge_settings, parse_include, plan_import,
                                read_manifest, render_export_report, render_import_plan, render_import_result,
                                scan_for_secrets, scrub_external, scrub_settings, strip_command_credentials)
from dreamteam.treecopy import compare_digest, scan_tree
from dreamteam.workspace import rmtree

KEY = "sk-secret-123456"
PASTED = "sk-pasted-key-0987654321"
BEARER = "Bearer abcdefgh12345"
PG_PASSWORD = "pgpass-12345"
SESSION_TOKEN = "tok-session-abcdef"
SESSION_ENV = "session-env-secret"
GLOBAL_ENV = "global-env-secret"
GLOBAL_KEY = "globalkey-123456"
GLOBAL_PW = "globalpw-7890"


def _raw(**models: Any) -> dict[str, Any]:
    return {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 2.0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 0.2},
        "mcp": {"external": {"fetch": {
            "command": ["uvx", "mcp-server-fetch", f"--api-key={GLOBAL_KEY}", f"https://user:{GLOBAL_PW}@host/x"],
            "description": "Fetch pages", "env": {"A": "1", "FETCH_TOKEN": GLOBAL_ENV}}}},
        "models": {
            "default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0,
                        "api_key": KEY,
                        "extra": {"extra_headers": {"Authorization": BEARER, "X-Trace": "on"}, "top_k": 3}},
            "fast": {"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:9/v1",
                     "api_key_env": "DT_TEST_KEY_ENV"},
            "pasted": {"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:9/v1",
                       "api_key_env": PASTED},
            **models,
        },
        "roles": [{"name": "Archivist", "description": "Keeps the notes in order"}],
    }


def _home(tmp_path: Path, raw: dict[str, Any] | None = None) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, raw or _raw())
    return home


def _manager(home: Path) -> SessionManager:
    def factory(profile, agent):
        return ScriptedLLM(lambda system, messages, tools: "Nothing to do right now.", profile)

    return SessionManager(home, llm_factory=factory)


async def _session(manager: SessionManager, title: str, **kw: Any) -> str:
    return (await manager.create(title, start=False, **kw))["id"]


def _db(home: Path, sid: str) -> Database:
    return Database(home / "sessions" / sid / ".dreamteam" / "swarm.db")


def _fill_db(home: Path, sid: str, text: str = "hello") -> None:
    db = _db(home, sid)
    try:
        db.execute("INSERT INTO events(ts, type, actor, data) VALUES (?, 'session.started', 'user', '{}')", (1.0,))
        db.execute("INSERT INTO events(ts, type, actor, data) VALUES (?, 'message', 'alice', '{}')", (2.0,))
        db.execute("INSERT INTO history(agent, seq, msg) VALUES ('alice', 1, ?)", (json.dumps({"text": text}),))
    finally:
        db.close()


def _write(path: Path, data: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def _workspace(home: Path, sid: str) -> Path:
    return home / "sessions" / sid / "workspace"


def _set_meta(home: Path, sid: str, **changes: Any) -> None:
    path = home / "sessions" / sid / "session.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
    path.write_text(json.dumps(meta), encoding="utf-8")


def _all_bytes(folder: Path) -> bytes:
    return b"\n".join(p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file())


def _paths(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*"))


def _manifest(dest: Path) -> dict[str, Any]:
    return json.loads((dest / MANIFEST).read_text(encoding="utf-8"))


def _skill(tmp_path: Path, name: str, body: str = "Body\n") -> Path:
    folder = tmp_path / "skill-src" / name
    _write(folder / "SKILL.md", f"---\nname: {name}\ndescription: Does {name} things\n---\n{body}")
    _write(folder / "scripts" / "run.py", "print('run')\n")
    return folder


def _library_root() -> Path:
    return Path(os.environ["DREAMTEAM_MCP_LIBRARY_DIR"])


def _library_entry(name: str, project: str | None, *, approved: bool = True) -> None:
    code = b"from mcp.server import MCPServer\n"
    digest = snapshot_digest(code, [])
    meta = {"name": name, "description": "helps", "owner": "alice", "requirements": [], "tools": [],
            "digest": digest, "approved_digest": digest if approved else None}
    if project:
        meta["project"] = project
    _library_root().mkdir(parents=True, exist_ok=True)
    mcp_library.write_entry_folder(_library_root() / name, code, meta)


def _home_state(home: Path) -> tuple[str | None, list[str], dict[str, list[str]]]:
    """Every file of the home byte for byte (its digest), every path (folders and -wal/-shm files included) and the
    content of every database."""
    tree = scan_tree(home, exclude_names=(), exclude_paths=())
    dbs = {}
    for path in sorted(home.rglob("swarm.db")):
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?immutable=1", uri=True)
        try:
            dbs[path.relative_to(home).as_posix()] = list(conn.iterdump())
        finally:
            conn.close()
    return tree.digest, _paths(home), dbs


async def _full_home(tmp_path: Path) -> tuple[Path, SessionManager, str, str]:
    home = _home(tmp_path)
    manager = _manager(home)
    pg = {"pg": {"command": ["pgmcp", f"postgres://bob:{PG_PASSWORD}@db/x", "--token", SESSION_TOKEN],
                 "env": {"B": SESSION_ENV}}}
    one = await _session(manager, "Alpha", overrides={"mcp": {"external": pg}})
    two = await _session(manager, "Beta")
    _fill_db(home, one)
    db = _db(home, one)
    db.execute("INSERT INTO mcp_servers(name, owner, scope, command, origin, status) VALUES (?, 'user', 'swarm', ?, "
               "'config', 'active')", ("pg", json.dumps(pg["pg"]["command"])))
    db.close()
    _write(_workspace(home, one) / "src" / "app.py", "print('alpha')\n")
    _write(home / "sessions" / one / ".dreamteam" / "mcp" / "helper" / "server.py", "snapshot\n")
    _write(home / "sessions" / one / ".dreamteam" / "jobs" / "1.log", "job output\n")
    _write(home / "sessions" / one / ".dreamteam" / "attached-skills" / "csv-tool" / "SKILL.md", "attached\n")
    _write(home / "sessions" / one / ".dreamteam" / "logs" / "alice.log", "a log\n")
    Library(Path(os.environ["DREAMTEAM_SKILLS_DIR"])).add(_skill(tmp_path, "csv-tool"), "folder",
                                                           {"path": "C:/src/csv-tool"})
    _library_entry("helper", str(home / "sessions" / one))
    _library_entry("other", "/somewhere/else", approved=False)
    return home, manager, one, two


# ---- the layout ---------------------------------------------------------------------------------------------
async def test_export_writes_every_category_without_the_secrets(tmp_path):
    home, manager, one, two = await _full_home(tmp_path)
    dest = tmp_path / "out"
    report = export_home(home, dest)
    assert report.ok and not report.dry_run and not report.replaced
    assert sorted(os.listdir(dest)) == ["dreamteam-export.json", "mcp", "sessions", "settings", "skills"]
    data = _all_bytes(dest)
    for secret in (KEY, PASTED, BEARER, PG_PASSWORD, SESSION_TOKEN, SESSION_ENV, GLOBAL_ENV, GLOBAL_KEY, GLOBAL_PW):
        assert secret.encode() not in data, secret

    settings = tomllib.loads((dest / "settings" / "dreamteam.toml").read_text(encoding="utf-8"))
    assert "external" not in settings.get("mcp", {})
    assert "api_key" not in settings["models"]["default"] and "api_key_env" not in settings["models"]["pasted"]
    assert settings["models"]["fast"]["api_key_env"] == "DT_TEST_KEY_ENV"
    assert settings["models"]["default"]["extra"] == {"extra_headers": {"X-Trace": "on"}, "top_k": 3}
    assert settings["roles"] == [{"name": "Archivist", "description": "Keeps the notes in order"}]
    assert settings["swarm"]["name"] == "Home"

    external = json.loads((dest / "mcp" / "external.json").read_text(encoding="utf-8"))
    assert external == {"fetch": {"command": ["uvx", "mcp-server-fetch", "--api-key=", "https://host/x"],
                                  "cwd": None, "description": "Fetch pages", "env_keys": ["A", "FETCH_TOKEN"]}}

    session = json.loads((dest / "sessions" / one / "session.json").read_text(encoding="utf-8"))
    assert session["uid"] == session_uid(manager.meta(one)) and session["title"] == "Alpha"
    assert session["overrides"]["mcp"]["external"]["pg"] == {"command": ["pgmcp", "postgres://db/x", "--token", ""],
                                                             "env_keys": ["B"]}
    copy = sqlite3.connect(dest / "sessions" / one / ".dreamteam" / "swarm.db")
    try:
        assert json.loads(copy.execute("SELECT command FROM mcp_servers WHERE name = 'pg'").fetchone()[0]) == \
            ["pgmcp", "postgres://db/x", "--token", ""]
        assert copy.execute("SELECT msg FROM history").fetchone()[0] == json.dumps({"text": "hello"})
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert copy.execute("PRAGMA freelist_count").fetchone()[0] == 0
    finally:
        copy.close()
    assert sorted(os.listdir(dest / "sessions" / one / ".dreamteam")) == ["attached-skills", "jobs", "mcp", "swarm.db"]
    assert (dest / "sessions" / one / "workspace" / "src" / "app.py").read_text() == "print('alpha')\n"

    skill_meta = json.loads((dest / "skills" / "csv-tool" / ".dreamteam.json").read_text(encoding="utf-8"))
    assert skill_meta["origin"] == "folder" and skill_meta["source"] == {"path": "C:/src/csv-tool"}
    assert skill_meta["digest"] and skill_meta["by"] == "user" and "manifest" not in skill_meta
    assert sorted(_paths(dest / "skills" / "csv-tool")) == [".dreamteam.json", "SKILL.md", "scripts",
                                                            "scripts/run.py"]

    helper = json.loads((dest / "mcp" / "library" / "helper" / "meta.json").read_text(encoding="utf-8"))
    assert helper["project_session"] == session_uid(manager.meta(one)) and "project" not in helper
    assert helper["approved_digest"] == helper["digest"]
    other = json.loads((dest / "mcp" / "library" / "other" / "meta.json").read_text(encoding="utf-8"))
    assert other["project"] == "/somewhere/else" and "project_session" not in other

    manifest = _manifest(dest)
    assert manifest["format"] == "dreamteam-export" and manifest["format_version"] == 1
    assert manifest["schema"] == {"db_columns": sorted(known_columns()), "toolset_revision": TOOLSET_REVISION}
    assert manifest["include"] == ["settings", "skills", "mcp", "sessions"]
    assert manifest["source"]["os"] and manifest["dreamteam_version"]
    assert manifest["settings"]["models"] == ["default", "fast", "pasted"]
    assert manifest["settings"]["roles"] == ["archivist"]
    assert sorted(manifest["settings"]["omitted"]) == sorted([
        "models.default.api_key", "models.default.extra.extra_headers.Authorization", "models.pasted.api_key_env"])
    assert [s["name"] for s in manifest["skills"]] == ["csv-tool"]
    library = {e["name"]: e for e in manifest["mcp"]["library"]}
    assert library["helper"]["approved"] and not library["other"]["approved"]
    assert manifest["mcp"]["external"] == [{"name": "fetch", "env_keys": ["A", "FETCH_TOKEN"],
                                            "credentials_removed": True}]
    by_id = {s["id"]: s for s in manifest["sessions"]}
    first = by_id[one]
    assert first["uid"] == session["uid"] and first["events"] == 2 and not first["running_at_export"]
    assert first["last_event"]["type"] == "message" and first["last_event"]["ts"] == 2.0
    assert first["db_bytes"] == (dest / "sessions" / one / ".dreamteam" / "swarm.db").stat().st_size
    assert len(first["db_sha256"]) == 64
    assert first["attached_skills"] == ["csv-tool"]
    assert first["mcp_servers"] == [{"name": "pg", "origin": "config", "status": "active"}]
    assert first["workspace"]["kind"] == "own" and first["workspace"]["files"] == 1
    assert first["workspace"]["tree_digest"] == scan_tree(_workspace(home, one)).digest
    assert by_id[two]["db_sha256"] is None and by_id[two]["events"] == 0
    warnings = "\n".join(report.warnings)
    assert "[mcp.external.fetch]" in warnings and f"session {one}: re-enter the credentials of [mcp.external.pg]" \
        in warnings and f"session {one}: re-enter the credentials of MCP server pg" in warnings
    assert "model profile 'default': its API key stays on this computer" in warnings
    text = render_export_report(report)
    assert f"Import it there with: dreamteam import {dest}" in text
    assert "settings  dreamteam.toml: " in text and "skills    1 skill" in text and "sessions  2 sessions" in text
    assert "mcp       2 library servers (1 approved), 1 external server" in text
    await manager.shutdown()


async def test_machine_files_are_never_exported(tmp_path):
    home, manager, one, _two = await _full_home(tmp_path)
    st.write_raw(home, _raw())   # a second save leaves dreamteam.toml.bak
    assert (home / "dreamteam.toml.bak").exists()
    for name in ("hub.json", "launcher-123.pid", "tui_history", "logs/hub.log", "swarm.db"):
        _write(home / ".dreamteam" / name, "x")   # (a legacy main session too)
    _write(home / "sessions" / f".deleted-{one}-1" / "session.json", "{}")
    _write(home / "sessions" / ".importing-x-1" / "session.json", "{}")
    _write(home / "sessions" / f".replaced-{one}-1" / "session.json", "{}")
    report = export_home(home, tmp_path / "out")
    names = _paths(tmp_path / "out")
    for bad in (".bak", "hub.json", "launcher-", "tui_history", "logs", ".deleted-", ".importing-", ".replaced-",
                "-wal", "-shm"):
        assert not any(bad in n for n in names), bad
    assert report.left_out == [{"what": "session main", "reason": report.left_out[0]["reason"], "refused": False}]
    assert report.ok
    await manager.shutdown()


async def test_rows_still_in_the_wal_of_an_open_database_are_exported(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    sid = await _session(manager, "Live")
    db = _db(home, sid)
    try:
        db.execute("PRAGMA wal_autocheckpoint=0")
        db.execute("INSERT INTO history(agent, seq, msg) VALUES ('bob', 1, 'still in the wal')")
        assert (home / "sessions" / sid / ".dreamteam" / "swarm.db-wal").stat().st_size > 0
        report = export_home(home, tmp_path / "out", include=["sessions"], running={sid})
    finally:
        db.close()
    copy = sqlite3.connect(tmp_path / "out" / "sessions" / sid / ".dreamteam" / "swarm.db")
    try:
        assert copy.execute("SELECT msg FROM history WHERE agent = 'bob'").fetchone()[0] == "still in the wal"
    finally:
        copy.close()
    assert report.sessions[0]["running_at_export"] is True
    assert any(f"session {sid} is running" in w for w in report.warnings)
    assert not (tmp_path / "out" / "settings").exists() and not (tmp_path / "out" / "skills").exists()
    await manager.shutdown()


async def test_the_source_home_is_only_read(tmp_path):
    home, manager, one, _two = await _full_home(tmp_path)
    skills_before = scan_tree(Path(os.environ["DREAMTEAM_SKILLS_DIR"]), exclude_names=(), exclude_paths=()).digest
    library_before = scan_tree(_library_root(), exclude_names=(), exclude_paths=()).digest
    before = _home_state(home)
    assert not any(p.endswith(("-wal", "-shm")) for p in before[1])   # every database is closed
    export_home(home, tmp_path / "out")
    export_home(home, tmp_path / "dry", dry_run=True)
    assert _home_state(home) == before
    assert scan_tree(Path(os.environ["DREAMTEAM_SKILLS_DIR"]), exclude_names=(), exclude_paths=()).digest == \
        skills_before
    assert scan_tree(_library_root(), exclude_names=(), exclude_paths=()).digest == library_before
    await manager.shutdown()


async def test_a_session_that_ran_exports_its_history(tmp_path):
    home = _home(tmp_path, {**_raw(), "mcp": {}})
    manager = _manager(home)
    summary = await manager.create("Worked", "Write a haiku", start=True)
    sid = summary["id"]
    for _ in range(100):
        if manager.summary(sid)["last_activity"]:
            break
        await asyncio.sleep(0.05)
    await manager.stop(sid)
    report = export_home(home, tmp_path / "out", include=["sessions"])
    item = report.sessions[0]
    assert item["events"] > 0 and item["last_event"]["id"] > 0
    assert item["max_tools_rev"] in (None, TOOLSET_REVISION)
    copy = sqlite3.connect(tmp_path / "out" / "sessions" / sid / ".dreamteam" / "swarm.db")
    try:
        assert copy.execute("SELECT COUNT(*) FROM agents").fetchone()[0] >= 1
        assert copy.execute("SELECT value FROM kv WHERE key = 'goal'").fetchone() is not None
    finally:
        copy.close()
    await manager.shutdown()


# ---- workspaces -----------------------------------------------------------------------------------------------
async def test_workspaces_inside_and_outside_the_home(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    inside = await _session(manager, "Inside")
    outside = await _session(manager, "Outside")
    project = _write(home / "proj" / "README.md", "in the home\n").parent
    _set_meta(home, inside, workspace={"path": str(project), "adopted": True, "in_home": "proj"})
    elsewhere = _write(tmp_path / "elsewhere" / "big.bin", b"x" * 1000).parent
    _set_meta(home, outside, workspace={"path": str(elsewhere), "adopted": True})
    _write(_workspace(home, inside) / "stale.txt", "the unused own folder\n")
    report = export_home(home, tmp_path / "out", include=["sessions"])
    by_id = {s["id"]: s for s in report.sessions}
    assert by_id[inside]["workspace"] == {"kind": "in_home", "in_home": "proj", "path": str(manager.home / "proj"),
                                          "adopted": True, "files": 1, "bytes": len("in the home\n"),
                                          "tree_digest": scan_tree(project).digest,
                                          "compare_digest": compare_digest(scan_tree(project).entries)}
    assert _paths(tmp_path / "out" / "sessions" / inside / "workspace") == ["README.md"]
    assert by_id[outside]["workspace"]["kind"] == "outside" and by_id[outside]["workspace"]["files"] == 0
    assert by_id[outside]["workspace"]["path"] == str(elsewhere)
    assert not (tmp_path / "out" / "sessions" / outside / "workspace").exists()
    assert report.not_copied == [{"session": outside, "path": str(elsewhere)}]
    assert f"session {outside} works in {elsewhere}: on the other computer put the project at that path, or run " \
           "/cd in the session" in render_export_report(report)
    await manager.shutdown()


def _link(target: Path, link: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
        return True
    except (OSError, NotImplementedError):
        if os.name == "nt" and target.is_dir():
            return subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                  capture_output=True).returncode == 0
        return False


async def test_workspace_excludes_and_links(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    sid = await _session(manager, "Files")
    ws = _workspace(home, sid)
    for rel in ("node_modules/x/index.js", ".venv/pyvenv.cfg", "venv/x.py", "src/__pycache__/a.pyc",
                ".scratch/_skills/s/SKILL.md", ".unavailable-workspace/x"):
        _write(ws / rel, "left out\n")
    for rel in (".git/HEAD", ".scratch/alice/notes.md", "src/a.py"):
        _write(ws / rel, "kept\n")
    _write(tmp_path / "outside" / "private.txt", "outside\n")
    linked = _link(tmp_path / "outside", ws / "linked")
    report = export_home(home, tmp_path / "out", include=["sessions"])
    copied = _paths(tmp_path / "out" / "sessions" / sid / "workspace")
    assert [p for p in copied if not (tmp_path / "out" / "sessions" / sid / "workspace" / p).is_dir()] == \
        [".git/HEAD", ".scratch/alice/notes.md", "src/a.py"]
    if linked:
        assert any("1 link not copied (linked)" in w for w in report.warnings)
    await manager.shutdown()


async def test_a_session_whose_workspace_move_did_not_finish_is_refused(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    stuck = await _session(manager, "Stuck")
    fine = await _session(manager, "Fine")
    _write(home / "sessions" / stuck / ".dreamteam" / MOVE_JOURNAL, json.dumps({"from": "a", "to": "b"}))
    report = export_home(home, tmp_path / "out", include=["sessions"])
    assert [s["id"] for s in report.sessions] == [fine]
    assert report.left_out[0]["what"] == f"session {stuck}" and report.left_out[0]["refused"]
    assert "did not finish" in report.left_out[0]["reason"] and not report.ok
    assert not (tmp_path / "out" / "sessions" / stuck).exists()
    assert f"session {stuck}: moving its workspace did not finish" in render_export_report(report)
    await manager.shutdown()


async def test_broken_items_are_left_out_and_the_rest_is_exported(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    broken = await _session(manager, "Broken")
    fine = await _session(manager, "Fine")
    damaged = await _session(manager, "Damaged")
    _write(home / "sessions" / broken / ".dreamteam" / "swarm.db", b"this is not a database" * 100)
    _write(_workspace(home, broken) / "a.txt", "a\n")
    _fill_db(home, fine)
    (home / "sessions" / damaged / "session.json").write_text("{not json", encoding="utf-8")
    skills_root = Path(os.environ["DREAMTEAM_SKILLS_DIR"])
    Library(skills_root).add(_skill(tmp_path, "good-one"), "folder", {})
    _write(skills_root / "no-skill-md" / "README.md", "no SKILL.md\n")
    _library_entry("helper", None)
    _write(_library_root() / "nometa" / "server.py", "code\n")
    report = export_home(home, tmp_path / "out")
    assert [s["id"] for s in report.sessions] == [fine]
    refused = {item["what"]: item["reason"] for item in report.left_out if item["refused"]}
    assert refused[f"session {broken}"].startswith("its database cannot be read")
    assert "is damaged" in refused[f"session {damaged}"]
    assert "No SKILL.md" in refused["skill no-skill-md"]
    assert "MCP library server nometa" in refused
    assert not report.ok
    assert sorted(os.listdir(tmp_path / "out" / "sessions")) == [fine]
    assert sorted(os.listdir(tmp_path / "out" / "skills")) == ["good-one"]
    assert sorted(os.listdir(tmp_path / "out" / "mcp" / "library")) == ["helper"]
    assert _manifest(tmp_path / "out")["left_out"] == report.left_out
    await manager.shutdown()


async def test_sessions_are_chosen_by_id_or_uid_and_imply_the_category(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    one = await _session(manager, "One")
    two = await _session(manager, "Two")
    report = export_home(home, tmp_path / "a", include=["settings"], sessions=[two])
    assert report.include == ("settings", "sessions") and [s["id"] for s in report.sessions] == [two]
    uid = session_uid(manager.meta(one))
    assert [s["id"] for s in export_home(home, tmp_path / "b", sessions=[uid.upper()]).sessions] == [one]
    with pytest.raises(TransferError, match="Unknown session 'nope'"):
        export_home(home, tmp_path / "c", sessions=["nope"])
    assert not (tmp_path / "c").exists()
    await manager.shutdown()


async def test_a_session_without_uid_gets_its_derived_uid_written_and_copies_are_flagged(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    sid = await _session(manager, "Old")
    _set_meta(home, sid, uid=None)
    derived = session_uid(manager.meta(sid))
    shutil.copytree(home / "sessions" / sid, home / "sessions" / f"{sid}-copy")
    nameless = await _session(manager, "Nameless")
    _set_meta(home, nameless, uid=None, created_at=None)
    report = export_home(home, tmp_path / "out", include=["sessions"])
    stored = json.loads((tmp_path / "out" / "sessions" / sid / "session.json").read_text(encoding="utf-8"))
    assert stored["uid"] == derived
    assert {s["id"] for s in report.sessions} == {sid, f"{sid}-copy"}
    assert any("have the same identity" in w for w in report.warnings)
    assert report.left_out[0]["what"] == f"session {nameless}" and "created_at" in report.left_out[0]["reason"]
    with pytest.raises(TransferError, match=f"Sessions {sid} and {sid}-copy have the identity"):
        export_home(home, tmp_path / "by-uid", sessions=[derived])
    assert [s["id"] for s in export_home(home, tmp_path / "by-id", sessions=[f"{sid}-copy"]).sessions] == \
        [f"{sid}-copy"]
    with pytest.raises(TransferError, match="Unknown session 'main'"):
        export_home(home, tmp_path / "main", sessions=["main"])
    await manager.shutdown()


# ---- the secret scan -------------------------------------------------------------------------------------------
async def test_a_stored_key_in_a_file_or_a_history_row_fails_the_export(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    sid = await _session(manager, "Leaky")
    _fill_db(home, sid, text=f"the key is {KEY}")
    _write(_workspace(home, sid) / "notes" / "keys.txt", f"key={KEY}\n")
    dest = tmp_path / "out"
    with pytest.raises(SecretFound) as err:
        export_home(home, dest)
    assert not dest.exists()
    places = err.value.places
    assert f"sessions/{sid}/workspace/notes/keys.txt: the API key of model profile 'default'" in places
    assert any(p.startswith(f"sessions/{sid}/.dreamteam/swarm.db (history.msg:1)") for p in places)
    assert KEY not in str(err.value) and "Nothing exported" in str(err.value)
    # an empty folder given as the destination stays, empty
    (tmp_path / "empty").mkdir()
    with pytest.raises(SecretFound):
        export_home(home, tmp_path / "empty")
    assert os.listdir(tmp_path / "empty") == []
    await manager.shutdown()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "core.autocrlf=false",
                    "-c", "commit.gpgSign=false", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
async def test_a_key_committed_and_then_deleted_fails_the_export(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    sid = await _session(manager, "History")
    ws = _workspace(home, sid)
    _git(ws, "init", "-q")
    _write(ws / "config.py", f"KEY = '{KEY}'\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "add config")
    (ws / "config.py").write_text("KEY = None\n")
    _git(ws, "commit", "-q", "-am", "drop the key")
    _git(ws, "gc", "-q")   # packed: the old blob is compressed in a pack file
    with pytest.raises(SecretFound) as err:
        export_home(home, tmp_path / "out", include=["sessions"])
    assert any(p.startswith(f"sessions/{sid}/workspace/.git (git object ") for p in err.value.places)
    assert not (tmp_path / "out").exists()
    # without the key in the history the same repository exports
    rmtree(ws / ".git")
    _git(ws, "init", "-q")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "clean")
    report = export_home(home, tmp_path / "out", include=["sessions"])
    assert (tmp_path / "out" / "sessions" / sid / "workspace" / ".git" / "HEAD").is_file() and report.ok
    await manager.shutdown()


async def test_a_repository_without_git_to_read_it_fails_the_export(tmp_path, monkeypatch):
    home = _home(tmp_path)
    manager = _manager(home)
    sid = await _session(manager, "Repo")
    for rel in ("HEAD", "objects/.keep", "refs/heads/.keep"):
        _write(_workspace(home, sid) / ".git" / rel, "x\n")
    monkeypatch.setattr(transfer, "shutil", SimpleNamespace(which=lambda name: None))
    with pytest.raises(TransferError, match="git is needed to check the history"):
        export_home(home, tmp_path / "out", include=["sessions"])
    assert not (tmp_path / "out").exists()
    await manager.shutdown()


async def test_a_key_in_a_utf16_file_fails_the_export(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    sid = await _session(manager, "Windows")
    _write(_workspace(home, sid) / "out.txt", f"\ufeffpowershell > out.txt: {KEY}\r\n".encode("utf-16-le"))
    with pytest.raises(SecretFound) as err:
        export_home(home, tmp_path / "out")
    assert err.value.places == [f"sessions/{sid}/workspace/out.txt: the API key of model profile 'default'"]
    await manager.shutdown()


async def test_the_placeholder_key_of_lm_studio_is_no_secret(tmp_path):
    raw = _raw(local={"provider": "openai", "model": "m", "base_url": "http://localhost:1234/v1",
                      "api_key": "lm-studio"})
    home = _home(tmp_path, raw)
    manager = _manager(home)
    sid = await _session(manager, "Local")
    _write(_workspace(home, sid) / "README.md", "Run the model in lm-studio first.\n")
    report = export_home(home, tmp_path / "out")
    assert report.ok and "models.local.api_key" in report.settings["omitted"]
    assert not any("'local': its API key" in w for w in report.warnings)   # the import puts the placeholder back
    assert "lm-studio" not in known_secrets(raw).values
    await manager.shutdown()


@pytest.mark.parametrize("kind", ["env", "hub", "hub_file", "headers", "install_env", "pass_env", "key_env",
                                  "session_env"])
async def test_secrets_of_the_environment_fail_the_export(tmp_path, monkeypatch, kind):
    value = f"value-of-{kind}-0123456789"
    raw = _raw()
    if kind == "env":
        monkeypatch.setenv("DT_SERVICE_TOKEN", value)
    elif kind == "hub":
        monkeypatch.setenv(hubauth.TOKEN_ENV, value)
    elif kind == "hub_file":
        _write(hubauth.tokens_dir() / "8800.json", json.dumps({"port": 8800, "token": value}))
    elif kind == "headers":
        monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", f"X-Gateway: {value}")
    elif kind == "install_env":
        raw["skills"] = {"install_env": ["DT_INDEX_URL_PLAIN"]}
        monkeypatch.setenv("DT_INDEX_URL_PLAIN", value)
    elif kind == "pass_env":
        raw["shell"] = {"pass_env": ["DT_PASSED_PLAIN"]}
        monkeypatch.setenv("DT_PASSED_PLAIN", value)
    elif kind == "key_env":
        monkeypatch.setenv("DT_TEST_KEY_ENV", value)
    home = _home(tmp_path, raw)
    manager = _manager(home)
    overrides = {"mcp": {"external": {"x": {"command": ["x"], "env": {"K": value}}}}} if kind == "session_env" else None
    sid = await _session(manager, "Env", overrides=overrides)
    other = await _session(manager, "Other")
    _write(_workspace(home, other) / "leak.txt", f"{value}\n")
    with pytest.raises(SecretFound) as err:
        export_home(home, tmp_path / "out", sessions=[other])
    assert len(err.value.places) == 1 and err.value.places[0].startswith(f"sessions/{other}/workspace/leak.txt: ")
    assert value not in str(err.value)
    assert sid
    await manager.shutdown()


def test_scan_for_secrets_names_places_not_values(tmp_path):
    secrets = Secrets()
    secrets.add("abcdefgh-secret", "the test secret")
    secrets.add("short", "too short to search")
    secrets.add("lm-studio", "a placeholder")
    assert set(secrets.values) == {"abcdefgh-secret"}
    _write(tmp_path / "a.txt", b"x" * (1 << 20) + b"abcdefgh-secret")   # across a chunk boundary
    _write(tmp_path / "b" / "c.txt", "nothing here, lm-studio, short")
    db = sqlite3.connect(tmp_path / "d.db")
    db.execute("CREATE TABLE notes (body TEXT, blob BLOB)")
    db.execute("INSERT INTO notes VALUES ('clean', NULL), ('has abcdefgh-secret in it', NULL)")
    db.execute("INSERT INTO notes VALUES (NULL, ?)", ("abcdefgh-secret".encode("utf-16-le"),))
    db.commit()
    db.close()
    places = scan_for_secrets(tmp_path, secrets)
    assert places == ["a.txt: the test secret", "d.db (notes.blob:3, notes.body:2): the test secret"]
    assert scan_for_secrets(tmp_path, Secrets()) == []


# ---- the destination -------------------------------------------------------------------------------------------
async def test_destinations_that_are_refused(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    sid = await _session(manager, "Guarded")
    project = (home / "proj")
    project.mkdir()
    _set_meta(home, sid, workspace={"path": str(project), "adopted": True, "in_home": "proj"})
    busy = _write(tmp_path / "busy" / "mine.txt", "mine\n").parent
    cases = {
        busy: "is not empty and holds no DreamTeam export",
        busy / "mine.txt": "is a file",
        home / "sessions" / "exports": "inside the sessions folder",
        home / "sessions" / sid / "workspace" / "x": "inside the sessions folder",
        project / "export": f"inside the workspace of session {sid}",
        home / ".dreamteam" / "x": "DreamTeam's own data folder",
        Path(os.environ["DREAMTEAM_SKILLS_DIR"]) / "x": "inside the skills library",
        _library_root() / "x": "inside the MCP library",
        tmp_path: "contains the DreamTeam home",
        home: "contains the DreamTeam home",
        Path(Path(tmp_path).anchor): "the root of a drive",
    }
    for dest, text in cases.items():
        with pytest.raises(TransferError, match=text.replace("(", r"\(")):
            export_home(home, dest)
    assert not (home / "sessions" / "exports").exists() and not (project / "export").exists()
    assert os.listdir(busy) == ["mine.txt"]
    # a folder holding an export plus another file is not replaced either
    export_home(home, tmp_path / "out")
    _write(tmp_path / "out" / "mine.txt", "mine\n")
    with pytest.raises(TransferError, match=r"holds an earlier DreamTeam export and other files \(mine.txt\): move "
                                            "them out to replace the export"):
        export_home(home, tmp_path / "out")
    with pytest.raises(TransferError, match="not a DreamTeam home"):
        export_home(tmp_path / "busy", tmp_path / "x")
    await manager.shutdown()


async def test_a_new_export_replaces_an_earlier_one(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    first = await _session(manager, "First")
    dest = tmp_path / "usb" / "dt"
    export_home(home, dest)
    assert [s["id"] for s in _manifest(dest)["sessions"]] == [first]
    second = await _session(manager, "Second")
    report = export_home(home, dest)
    assert report.replaced and "the previous export there was replaced" in render_export_report(report)
    assert [s["id"] for s in _manifest(dest)["sessions"]] == [first, second]
    assert sorted(os.listdir(tmp_path / "usb")) == ["dt"]   # no temp folder or old copy left
    if os.name != "nt":
        assert (dest.stat().st_mode & 0o777) == 0o700
    await manager.shutdown()


async def test_a_failure_leaves_no_export_behind(tmp_path, monkeypatch):
    home = _home(tmp_path)
    manager = _manager(home)
    await _session(manager, "Crash")
    dest = tmp_path / "out"
    export_home(home, dest)
    before = _all_bytes(dest)

    def boom(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(transfer, "scan_for_secrets", boom)
    with pytest.raises(KeyboardInterrupt):
        export_home(home, dest)   # replacing: the earlier export stays as it was
    assert _all_bytes(dest) == before and sorted(os.listdir(tmp_path)) == ["home", "out"]
    with pytest.raises(KeyboardInterrupt):
        export_home(home, tmp_path / "new")
    assert not (tmp_path / "new").exists()
    monkeypatch.undo()

    def disk_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(transfer, "_manifest", disk_full)
    with pytest.raises(TransferError, match="Nothing exported: No space left on device"):
        export_home(home, tmp_path / "new")
    assert not (tmp_path / "new").exists() and _all_bytes(dest) == before
    await manager.shutdown()


async def test_dry_run_writes_nothing_and_reports_sizes(tmp_path):
    home, manager, one, _two = await _full_home(tmp_path)
    before = _home_state(home)
    report = export_home(home, tmp_path / "out", dry_run=True)
    assert not (tmp_path / "out").exists() and report.dry_run and report.manifest is None
    assert {s["id"] for s in report.sessions} == {one, _two}
    assert report.sizes["sessions"] > 0 and report.sizes["skills"] > 0 and report.sizes["settings"] > 0
    item = next(s for s in report.sessions if s["id"] == one)
    assert item["db_bytes"] > 0 and item["workspace"]["files"] == 1 and item["workspace"]["tree_digest"] is None
    text = render_export_report(report)
    assert text.startswith(f"Would export {report.home} to {report.dest} (dry run: nothing is written):")
    assert "Run it without --dry-run" in text and "Import it there" not in text
    assert _home_state(home) == before
    await manager.shutdown()


# ---- the pure helpers -------------------------------------------------------------------------------------------
def test_parse_include():
    assert parse_include(None) == ("settings", "skills", "mcp", "sessions")
    assert parse_include([]) == parse_include(None)
    assert parse_include(["sessions,settings", "mcp"]) == ("settings", "mcp", "sessions")
    assert parse_include("Skills") == ("skills",)
    with pytest.raises(TransferError) as err:
        parse_include(["settings,models"])
    assert err.value.usage and "models" in str(err.value)


@pytest.mark.parametrize("command, cleaned, removed", [
    (["srv", "postgres://user:pass-word-1@db:5432/x"], ["srv", "postgres://db:5432/x"], ["pass-word-1"]),
    (["srv", "https://token-abcdefgh@host/x"], ["srv", "https://host/x"], ["token-abcdefgh"]),
    (["srv", "ssh://git@host/x"], ["srv", "ssh://git@host/x"], []),
    (["srv", "--token", "tok-123456789"], ["srv", "--token", ""], ["tok-123456789"]),
    (["srv", "--api-key=key-123456789", "--port", "80"], ["srv", "--api-key=", "--port", "80"], ["key-123456789"]),
    (["srv", "--github-token", "ghp_1234567890", "--x"], ["srv", "--github-token", "", "--x"], ["ghp_1234567890"]),
    (["srv", "--password", "--verbose"], ["srv", "--password", "--verbose"], []),
    (["srv", "--no-auth", "path"], ["srv", "--no-auth", "path"], []),
    (["srv", "--author", "me"], ["srv", "--author", "me"], []),
    (["mcp-remote", "--header", "Authorization: Bearer abcdefghij"], ["mcp-remote", "--header", "Authorization:"],
     ["Bearer abcdefghij", "abcdefghij"]),
    (["npx", "mcp-remote", "https://mcp.example.com/sse?api_key=abcdef123456&x=1"],
     ["npx", "mcp-remote", "https://mcp.example.com/sse?api_key=&x=1"], ["abcdef123456"]),
    (["srv", "--url=https://x/mcp?token=abc%2Fdef12345#top"], ["srv", "--url=https://x/mcp?token=#top"],
     ["abc%2Fdef12345", "abc/def12345"]),
    (["srv", "https://x/mcp?page=2&format=json"], ["srv", "https://x/mcp?page=2&format=json"], []),
    (["mcp-proxy", "--headers", "Authorization", "Bearer abcdef123456", "http://x/sse"],
     ["mcp-proxy", "--headers", "Authorization", "", "http://x/sse"], ["Bearer abcdef123456", "abcdef123456"]),
    (["server", "-token=abcdef123456"], ["server", "-token="], ["abcdef123456"]),
    (["server", "-api-key", "abcdef123456"], ["server", "-api-key", ""], ["abcdef123456"]),
    (["server", "-t", "abcdef123456"], ["server", "-t", "abcdef123456"], []),
    (["server", "--headers", '{"Authorization": "Bearer abcdef123456", "Accept": "json"}'],
     ["server", "--headers", '{"Accept": "json"}'], ["Bearer abcdef123456", "abcdef123456"]),
    (["server", "Bearer abcdef123456"], ["server", ""], ["Bearer abcdef123456", "abcdef123456"]),
    (["srv", "--header=X-Api-Key: k-123456789"], ["srv", "--header=X-Api-Key:"], ["k-123456789"]),
    (["docker", "run", "-e", "GITHUB_TOKEN=ghp_abcdefghij", "img"], ["docker", "run", "-e", "GITHUB_TOKEN=", "img"],
     ["ghp_abcdefghij"]),
    (["srv", "MODE=fast"], ["srv", "MODE=fast"], []),
    ("srv --token tok-abcdefghi 'postgres://u:p%40ss-word@h/db'", ["srv", "--token", "", "postgres://h/db"],
     ["tok-abcdefghi", "p%40ss-word", "p@ss-word"]),
    ("srv --verbose", "srv --verbose", []),
])
def test_strip_command_credentials(command, cleaned, removed):
    got, found = strip_command_credentials(command)
    assert got == cleaned and sorted(set(found)) == sorted(removed)


def test_scrub_settings_and_external():
    raw = _raw()
    out, omitted, removed = scrub_settings(raw)
    assert raw["models"]["default"]["api_key"] == KEY   # the input is not changed
    assert "mcp" not in out and out["models"]["fast"] == raw["models"]["fast"]   # [mcp] held [mcp.external] only
    assert {KEY, PASTED, BEARER} <= set(removed.values)
    assert "models.pasted.api_key_env" in omitted
    extra = {"headers": [{"cookie": "c-123456789"}, "Bearer zyxwvuts98"], "temperature": 1, "x_api_key": "k"}
    out, omitted, removed = scrub_settings({"models": {"p": {"provider": "openai", "extra": extra}}})
    assert out["models"]["p"]["extra"] == {"headers": [{}, ""], "temperature": 1}
    assert sorted(omitted) == ["models.p.extra.headers[0].cookie", "models.p.extra.headers[1]",
                               "models.p.extra.x_api_key"]
    servers, removed, stripped = scrub_external({"a": {"command": "uvx a --token=t-123456789", "env": {"K": "v" * 9},
                                                       "cwd": "x", "extra_key": 1}, "b": {"command": ["b"]}})
    assert servers == {"a": {"command": ["uvx", "a", "--token="], "env_keys": ["K"], "cwd": "x", "extra_key": 1},
                       "b": {"command": ["b"], "env_keys": []}}
    assert stripped == ["a"] and set(removed.values) == {"t-123456789", "v" * 9}
    labels = known_secrets(raw, {"016-x": {"mcp": {"external": {"s": {"env": {"T": "session-token-1"}}}}}}, {}).values
    assert labels[KEY] == "the API key of model profile 'default'" and labels[PASTED].startswith("the API key")
    assert labels["session-token-1"] == "the env value T of [mcp.external.s] of session 016-x"
    assert labels[GLOBAL_ENV] == "the env value FETCH_TOKEN of [mcp.external.fetch]"
    assert "1" not in labels


# ==== import ===================================================================================================
B_KEY = "sk-b-own-key-777777"
B_FAST_KEY = "sk-b-fast-key-555555"
B_FETCH_KEY = "bkey-12345678"
B_FETCH_TOKEN = "b-fetch-token-999"


def _raw_b() -> dict[str, Any]:
    """The settings of the other computer: its own keys, MCP env values and profiles, roles and servers."""
    return {
        "swarm": {"name": "Other", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 2.0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 0.2},
        "server": {"port": 8800},
        "mcp": {"external": {
            "fetch": {"command": ["uvx", "mcp-server-fetch", f"--api-key={B_FETCH_KEY}", "https://host/x"],
                      "description": "Fetch pages",
                      "env": {"A": "b-one", "FETCH_TOKEN": B_FETCH_TOKEN, "ONLY_B": "only-on-b"}},
            "local": {"command": ["python", "server.py"], "env": {"K": "local-value-1"}}}},
        "models": {
            "default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0,
                        "api_key": B_KEY},
            "fast": {"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:8/v1", "api_key": B_FAST_KEY},
            "bonly": {"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:7/v1",
                      "api_key": "sk-bonly-key-4444"},
        },
        "roles": [{"name": "Librarian", "description": "Only on this computer"}],
    }


def _home_named(tmp_path: Path, name: str, raw: dict[str, Any]) -> Path:
    home = tmp_path / name
    (home / "sessions").mkdir(parents=True)
    st.write_raw(home, raw)
    return home


def _libraries(monkeypatch, tmp_path: Path, name: str) -> None:
    """Each "computer" has its own skills and MCP libraries."""
    monkeypatch.setenv("DREAMTEAM_SKILLS_DIR", str(tmp_path / f"libs-{name}" / "skills"))
    monkeypatch.setenv("DREAMTEAM_MCP_LIBRARY_DIR", str(tmp_path / f"libs-{name}" / "mcp"))


async def _import(home: Path, folder: Path, *, before_swap=None, **kw: Any):
    manager = _manager(home)
    plan = plan_import(manager, folder, **kw)
    result = await apply_import(manager, plan, before_swap=before_swap)
    return manager, plan, result


def _statuses(result) -> dict[tuple[str, str], str]:
    return {(i.category, i.name): i.status for i in result.items}


def _item(items, category: str, name: str):
    return next(i for i in items if i.category == category and i.name == name)


def _event(home: Path, sid: str, ts: float, kind: str = "message") -> None:
    db = _db(home, sid)
    try:
        db.execute("INSERT INTO events(ts, type, actor, data) VALUES (?, ?, 'user', '{}')", (ts, kind))
    finally:
        db.close()


def _digest(folder: Path) -> str | None:
    return scan_tree(folder, exclude_names=(), exclude_paths=()).digest


def _leftovers(home: Path) -> list[str]:
    return sorted(p.name for p in (home / "sessions").iterdir() if p.name.startswith((".importing-", ".replaced-")))


def _set_db_sha(dest: Path, sid: str) -> None:
    """The manifest records the database after a test changed it (as if the export had written it so)."""
    manifest = _manifest(dest)
    for entry in manifest["sessions"]:
        if entry["id"] == sid:
            entry["db_sha256"] = transfer._sha256(dest / "sessions" / sid / ".dreamteam" / "swarm.db")
    (dest / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")


async def test_a_round_trip_keeps_this_computers_keys_env_values_and_own_things(tmp_path, monkeypatch):
    _libraries(monkeypatch, tmp_path, "a")
    home_a, manager_a, one, two = await _full_home(tmp_path)
    uid_one = session_uid(manager_a.meta(one))
    export_home(home_a, tmp_path / "usb")
    await manager_a.shutdown()

    _libraries(monkeypatch, tmp_path, "b")
    home_b = _home_named(tmp_path, "b", _raw_b())
    _library_entry("bown", None)
    skills_b = Path(os.environ["DREAMTEAM_SKILLS_DIR"])
    Library(skills_b).add(_skill(tmp_path, "b-tool"), "folder", {})
    manager_b = _manager(home_b)
    own = await _session(manager_b, "Only here")
    _write(_workspace(home_b, own) / "mine.txt", "B's own\n")
    await manager_b.shutdown()
    own_before = _digest(home_b / "sessions" / own)
    b_tool_before = _digest(skills_b / "b-tool")
    bown_before = _digest(_library_root() / "bown")

    manager, plan, result = await _import(home_b, tmp_path / "usb")
    assert plan.changes and plan.ok and plan.id_map == {one: one, two: two}
    assert result.ok and result.exit_code == 0, render_import_result(result)
    assert _statuses(result) == {
        ("settings", "dreamteam.toml"): "rewritten", ("skills", "csv-tool"): "added", ("sessions", one): "added",
        ("sessions", two): "added", ("mcp", "library helper"): "added", ("mcp", "library other"): "added",
        ("mcp", "external fetch"): "unchanged"}

    raw = st.read_raw(home_b)
    models = raw["models"]
    assert models["default"]["api_key"] == B_KEY   # the same endpoint: the key of this computer stays
    assert models["default"]["extra"] == {"extra_headers": {"X-Trace": "on"}, "top_k": 3}
    assert "api_key" not in models["fast"] and models["fast"]["base_url"] == "http://127.0.0.1:9/v1"
    assert models["fast"]["api_key_env"] == "DT_TEST_KEY_ENV"
    assert "api_key" not in models["pasted"] and "api_key_env" not in models["pasted"]
    assert models["bonly"]["api_key"] == "sk-bonly-key-4444"
    assert [r["name"] for r in raw["roles"]] == ["Librarian", "Archivist"]
    assert raw["server"] == {"port": 8800} and raw["swarm"]["name"] == "Home"
    fetch = raw["mcp"]["external"]["fetch"]
    assert fetch["env"] == {"A": "b-one", "FETCH_TOKEN": B_FETCH_TOKEN, "ONLY_B": "only-on-b"}
    assert fetch["command"] == _raw_b()["mcp"]["external"]["fetch"]["command"]   # with its credentials here
    assert raw["mcp"]["external"]["local"] == _raw_b()["mcp"]["external"]["local"]
    warnings = "\n".join(w for i in result.items for w in i.warnings)
    assert "enter the key for profile fast again" in warnings
    assert "model profile 'pasted' uses http://127.0.0.1:9/v1: that server must run on this computer" in warnings
    assert f"enter B for pg in the settings of session {one}" in warnings
    assert "re-enter the credentials of pg on this computer" in warnings

    meta = manager.meta(one)
    assert meta["uid"] == uid_one and meta["autostart"] is False and meta["title"] == "Alpha"
    assert meta["overrides"]["mcp"]["external"]["pg"] == {"command": ["pgmcp", "postgres://db/x", "--token", ""]}
    assert (_workspace(home_b, one) / "src" / "app.py").read_text() == "print('alpha')\n"
    assert (home_b / "sessions" / one / ".dreamteam" / "jobs" / "1.log").exists()
    assert not (home_b / "sessions" / one / ".dreamteam" / "logs").exists()
    copy = sqlite3.connect(home_b / "sessions" / one / ".dreamteam" / "swarm.db")
    try:
        assert copy.execute("SELECT msg FROM history").fetchone()[0] == json.dumps({"text": "hello"})
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        copy.close()
    entry = Library(skills_b).get("csv-tool")
    assert entry["via"] == "import" and entry["source"] == {"path": "C:/src/csv-tool"} and not entry["changed_outside"]
    helper = mcp_library.read_entry(_library_root(), "helper")
    assert helper.project == str((home_b / "sessions" / one).resolve()) and helper.approved
    assert "project_session" not in helper.meta
    assert _digest(home_b / "sessions" / own) == own_before
    assert _digest(skills_b / "b-tool") == b_tool_before and _digest(_library_root() / "bown") == bown_before
    assert _leftovers(home_b) == []
    text = render_import_result(result)
    assert f"sessions  {one}: added" in text and "Imported sessions do not start by themselves" in text

    # the same export again: nothing changes
    plan = plan_import(manager, tmp_path / "usb")
    assert not plan.changes and {i.action for i in plan.items} == {"unchanged"}, render_import_plan(plan)
    assert "Nothing to change here." in render_import_plan(plan)
    await manager.shutdown()


async def test_an_imported_session_starts_and_its_members_resume_their_history(tmp_path, monkeypatch):
    _libraries(monkeypatch, tmp_path, "a")
    home_a = _home_named(tmp_path, "a", {**_raw(), "mcp": {}})
    manager_a = _manager(home_a)
    sid = (await manager_a.create("Worked", "Write a haiku", start=True))["id"]
    for _ in range(400):
        if manager_a.swarms[sid].db.scalar("SELECT COUNT(*) FROM history"):
            break
        await asyncio.sleep(0.025)
    await asyncio.sleep(0.3)
    await manager_a.stop(sid)
    db = _db(home_a, sid)
    try:
        histories: dict[str, list] = {}
        for row in db.all("SELECT agent, msg FROM history ORDER BY agent, seq"):
            histories.setdefault(row["agent"], []).append(json.loads(row["msg"]))
    finally:
        db.close()
    assert histories
    uid = session_uid(manager_a.meta(sid))
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    await manager_a.shutdown()

    _libraries(monkeypatch, tmp_path, "b")
    home_b = _home_named(tmp_path, "b", {**_raw_b(), "mcp": {}})
    manager, _plan, result = await _import(home_b, tmp_path / "usb")
    assert result.ok, render_import_result(result)
    assert manager.meta(sid)["autostart"] is False and manager.meta(sid)["uid"] == uid
    assert await manager.resume() == []   # an imported session never starts by itself
    swarm = await manager.start(sid)
    for _ in range(200):
        if all(name in swarm.runtimes and len(swarm.runtimes[name].history) >= len(messages)
               for name, messages in histories.items()):
            break
        await asyncio.sleep(0.02)
    for name, messages in histories.items():
        assert swarm.runtimes[name].history[:len(messages)] == messages
    assert swarm.goal() is not None
    await manager.shutdown()


async def test_the_owners_round_trips_rewrite_with_one_backup_and_keep_this_computers_state(tmp_path, monkeypatch):
    _libraries(monkeypatch, tmp_path, "a")
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    main = await _session(manager_a, "Main work")
    side = await _session(manager_a, "Project")
    await manager_a.shutdown()
    _fill_db(home_a, main)
    _write(_workspace(home_a, main) / "a.txt", "from A\n")
    export_home(home_a, tmp_path / "x1")

    _libraries(monkeypatch, tmp_path, "b")
    home_b = _home_named(tmp_path, "b", _raw_b())
    assert (await _import(home_b, tmp_path / "x1"))[2].ok
    # work on B: an event, a file, environments; the side session moves to a project of the user outside the home
    _event(home_b, main, 3.0)
    _write(_workspace(home_b, main) / "b.txt", "from B\n")
    _write(_workspace(home_b, main) / ".venv" / "pyvenv.cfg", "home = here\n")
    _write(_workspace(home_b, main) / "web" / "node_modules" / "x" / "index.js", "x\n")
    project = _write(tmp_path / "projects-b" / "README.md", "the project on B\n").parent
    _set_meta(home_b, side, workspace={"path": str(project), "adopted": True})
    export_home(home_b, tmp_path / "x2")

    _libraries(monkeypatch, tmp_path, "a")
    manager, plan, result = await _import(home_a, tmp_path / "x2")
    item = _item(plan.items, "sessions", main)
    assert item.action == "rewrite" and item.reason.startswith("the export has newer work in it; the old copy is kept")
    assert result.ok, render_import_result(result)
    backup = Path(_item(result.items, "sessions", main).backup)
    assert (backup / "session" / "workspace" / "a.txt").read_text() == "from A\n"
    assert (_workspace(home_a, main) / "b.txt").read_text() == "from B\n"
    assert st.read_raw(home_a)["models"]["default"]["api_key"] == KEY
    assert st.read_raw(home_a)["mcp"]["external"]["fetch"]["env"] == _raw()["mcp"]["external"]["fetch"]["env"]
    assert manager.workspace_of(side)["path"] == str(project)
    await manager.shutdown()
    # work on A: both sessions; the side session goes back to its own folder
    _event(home_a, main, 4.0)
    _event(home_a, side, 4.5)
    _set_meta(home_a, side, workspace=None)
    export_home(home_a, tmp_path / "x3")

    _libraries(monkeypatch, tmp_path, "b")
    manager, plan, result = await _import(home_b, tmp_path / "x3")
    assert result.ok, render_import_result(result)
    assert _statuses(result)[("sessions", main)] == "rewritten" and _statuses(result)[("sessions", side)] == "rewritten"
    raw = st.read_raw(home_b)
    assert raw["models"]["default"]["api_key"] == B_KEY and raw["models"]["bonly"]["api_key"] == "sk-bonly-key-4444"
    assert raw["mcp"]["external"]["fetch"]["env"]["FETCH_TOKEN"] == B_FETCH_TOKEN
    assert raw["mcp"]["external"]["local"]["env"] == {"K": "local-value-1"}
    assert manager.workspace_of(side)["path"] == str(project)   # B's folder outside the home stays
    assert (project / "README.md").read_text() == "the project on B\n"
    assert (_workspace(home_b, main) / ".venv" / "pyvenv.cfg").read_text() == "home = here\n"
    assert (_workspace(home_b, main) / "web" / "node_modules" / "x" / "index.js").exists()
    third = Path(_item(result.items, "sessions", main).backup)
    assert manager.backups(main) == [third]
    assert _item(result.items, "sessions", main).data["kept"] == [".venv", "web/node_modules"]
    await manager.shutdown()

    # A works again, and again A -> B: only the last backup of each session stays
    _event(home_a, main, 5.0)
    export_home(home_a, tmp_path / "x4")
    manager, plan, result = await _import(home_b, tmp_path / "x4")
    assert result.ok
    fourth = Path(_item(result.items, "sessions", main).backup)
    assert manager.backups(main) == [fourth] and str(third) in result.removed_backups and not third.exists()
    assert (_workspace(home_b, main) / ".venv" / "pyvenv.cfg").exists()
    assert _statuses(result)[("sessions", side)] == "unchanged"
    text = render_import_result(result)
    assert f"rewritten (previous copy: {fourth})" in text and "Older copies of the rewritten sessions" in text
    await manager.shutdown()


async def test_sessions_are_matched_by_uid_and_never_by_id(tmp_path, monkeypatch):
    _libraries(monkeypatch, tmp_path, "a")
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    alpha = await _session(manager_a, "Alpha")
    beta = await _session(manager_a, "Beta")
    await manager_a.shutdown()
    _library_entry("helper", str(home_a / "sessions" / alpha))
    _libraries(monkeypatch, tmp_path, "b")
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager_b = _manager(home_b)
    theirs = await _session(manager_b, "Alpha")   # both computers made 001-alpha
    assert theirs == alpha
    await manager_b.shutdown()
    shutil.copytree(home_a / "sessions" / beta, home_b / "sessions" / "050-renamed")   # copied and renamed by hand
    _fill_db(home_a, beta)   # newer work on A
    _libraries(monkeypatch, tmp_path, "a")
    export_home(home_a, tmp_path / "usb")
    _libraries(monkeypatch, tmp_path, "b")
    theirs_before = _digest(home_b / "sessions" / alpha)

    manager, plan, result = await _import(home_b, tmp_path / "usb")
    added = _item(plan.items, "sessions", alpha)
    assert added.action == "add" and added.target == "051-alpha" and added.reason == f"as 051-alpha: {alpha} is " \
        "taken here"
    renamed = _item(plan.items, "sessions", beta)
    assert renamed.action == "rewrite" and renamed.target == "050-renamed"
    assert f"sessions  {alpha}: add as 051-alpha: {alpha} is taken here" in render_import_plan(plan)
    assert result.ok and result.id_map == {alpha: "051-alpha", beta: "050-renamed"}
    assert _digest(home_b / "sessions" / alpha) == theirs_before
    assert session_uid(manager.meta("051-alpha")) == session_uid(json.loads(
        (tmp_path / "usb" / "sessions" / alpha / "session.json").read_text(encoding="utf-8")))
    assert not (home_b / "sessions" / beta).exists() and manager.meta("050-renamed")["title"] == "Beta"
    helper = mcp_library.read_entry(_library_root(), "helper")
    assert helper.project == str((home_b / "sessions" / "051-alpha").resolve())   # the session's id here
    assert "added as 051-alpha (001-alpha is taken here)" in render_import_result(result)
    await manager.shutdown()


async def test_a_uid_two_sessions_hold_is_refused_unless_the_id_is_given(tmp_path, monkeypatch):
    _libraries(monkeypatch, tmp_path, "a")
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    sid = await _session(manager_a, "Twice")
    other = await _session(manager_a, "Other")
    await manager_a.shutdown()
    _fill_db(home_a, sid)
    export_home(home_a, tmp_path / "usb")
    home_b = _home_named(tmp_path, "b", _raw_b())
    (home_b / "sessions").mkdir(exist_ok=True)
    for name in (sid, "009-twice-copy"):   # the same folder copied twice by hand
        shutil.copytree(home_a / "sessions" / sid, home_b / "sessions" / name)
    _event(home_a, sid, 9.0)
    export_home(home_a, tmp_path / "usb")

    manager, plan, result = await _import(home_b, tmp_path / "usb")
    item = _item(plan.items, "sessions", sid)
    assert item.action == "refuse" and f"sessions {sid} and 009-twice-copy here have the identity" in item.reason
    assert f"--session {sid}, which rewrites {sid} here" in item.reason and not plan.ok and plan.exit_code == 1
    assert _statuses(result)[("sessions", other)] == "added" and result.exit_code == 1
    assert _item(result.items, "sessions", sid).status == "refused"
    copy_before = _digest(home_b / "sessions" / "009-twice-copy")
    plan = plan_import(manager, tmp_path / "usb", sessions=[sid])
    item = _item(plan.items, "sessions", sid)
    assert [i.name for i in plan.items if i.category == "sessions"] == [sid]
    assert item.action == "rewrite" and item.target == sid
    result = await apply_import(manager, plan)
    assert result.ok and _digest(home_b / "sessions" / "009-twice-copy") == copy_before
    with pytest.raises(TransferError, match="the export holds no session 'nope'") as err:
        plan_import(manager, tmp_path / "usb", sessions=["nope"])
    assert err.value.usage
    await manager.shutdown()


async def test_sessions_of_the_export_with_one_uid_are_imported_one_at_a_time(tmp_path, monkeypatch):
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    sid = await _session(manager_a, "Copied")
    await manager_a.shutdown()
    shutil.copytree(home_a / "sessions" / sid, home_a / "sessions" / f"{sid}-copy")
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager = _manager(home_b)
    plan = plan_import(manager, tmp_path / "usb")
    assert {i.action for i in plan.items} == {"refuse"}
    assert all("of the export have the same identity" in i.reason for i in plan.items)
    uid = session_uid(manager_a.meta(sid))
    with pytest.raises(TransferError, match="give one of their ids"):
        plan_import(manager, tmp_path / "usb", sessions=[uid])
    plan = plan_import(manager, tmp_path / "usb", sessions=[f"{sid}-copy"])
    assert [(i.name, i.action) for i in plan.items] == [(f"{sid}-copy", "add")]
    assert (await apply_import(manager, plan)).ok
    await manager.shutdown()


async def test_a_session_copied_by_hand_without_uid_matches_by_created_at(tmp_path, monkeypatch):
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    sid = await _session(manager_a, "Old")
    await manager_a.shutdown()
    _set_meta(home_a, sid, uid=None)   # made before sessions had a uid
    _fill_db(home_a, sid)
    home_b = _home_named(tmp_path, "b", _raw_b())
    (home_b / "sessions").mkdir(exist_ok=True)
    shutil.copytree(home_a / "sessions" / sid, home_b / "sessions" / "077-old")   # robocopy, renamed
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    manager = _manager(home_b)
    plan = plan_import(manager, tmp_path / "usb")
    assert [(i.name, i.action, i.target) for i in plan.items] == [(sid, "unchanged", "077-old")]
    _event(home_a, sid, 8.0)
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    manager, plan, result = await _import(home_b, tmp_path / "usb")
    assert [(i.name, i.action, i.target) for i in plan.items] == [(sid, "rewrite", "077-old")]
    assert result.ok and json.loads((home_b / "sessions" / "077-old" / "session.json").read_text(
        encoding="utf-8"))["uid"] == session_uid(manager_a.meta(sid))
    assert not (home_b / "sessions" / sid).exists()
    await manager.shutdown()


async def test_workspaces_in_the_home_go_to_a_sibling_when_taken_and_never_nest(tmp_path, monkeypatch):
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    sid = await _session(manager_a, "Inside")
    nested = await _session(manager_a, "Nested")
    outside = await _session(manager_a, "Outside")
    await manager_a.shutdown()
    _write(home_a / "proj" / "app.py", "A's project\n")
    _set_meta(home_a, sid, workspace={"path": str(home_a / "proj"), "adopted": True, "in_home": "proj"})
    _write(home_a / "shared" / "sub" / "x.txt", "x\n")
    _set_meta(home_a, nested, workspace={"path": str(home_a / "shared" / "sub"), "adopted": False,
                                         "in_home": "shared/sub"})
    _set_meta(home_a, outside, workspace={"path": str(tmp_path / "nowhere" / "proj"), "adopted": True})
    export_home(home_a, tmp_path / "usb", include=["sessions"])

    home_b = _home_named(tmp_path, "b", _raw_b())
    _write(home_b / "proj" / "mine.txt", "B's own folder\n")
    manager_b = _manager(home_b)
    keeper = await _session(manager_b, "Keeper")
    _write(home_b / "shared" / "keep.txt", "B's session folder\n")
    _set_meta(home_b, keeper, workspace={"path": str(home_b / "shared"), "adopted": True, "in_home": "shared"})
    await manager_b.shutdown()
    manager, plan, result = await _import(home_b, tmp_path / "usb")
    sibling = home_b / f"proj-{sid}"
    item = _item(plan.items, "sessions", sid)
    assert item.action == "add" and any(f"{home_b / 'proj'} is taken here, so its workspace goes to {sibling}" in w
                                        for w in item.warnings)
    refused = _item(plan.items, "sessions", nested)
    assert refused.action == "refuse" and f"of session {keeper}" in refused.reason
    away = _item(plan.items, "sessions", outside)
    assert any("which is not on this computer: put the project at that path, or run /cd" in w for w in away.warnings)
    assert _statuses(result) == {("sessions", sid): "added", ("sessions", nested): "refused",
                                 ("sessions", outside): "added"}
    assert (sibling / "app.py").read_text() == "A's project\n" and (home_b / "proj" / "mine.txt").exists()
    assert manager.workspace_of(sid)["path"] == str(manager.home / f"proj-{sid}")
    assert manager.meta(sid)["workspace"]["in_home"] == f"proj-{sid}" and manager.meta(sid)["workspace"]["adopted"]
    assert manager.workspace_of(outside)["path"] == str(tmp_path / "nowhere" / "proj")
    assert not (home_b / "sessions" / outside / "workspace").exists()
    await manager.shutdown()


async def test_a_session_of_a_newer_dreamteam_is_refused_and_the_others_are_imported(tmp_path, monkeypatch):
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    column = await _session(manager_a, "Column")
    tools = await _session(manager_a, "Tools")
    changed = await _session(manager_a, "Changed")
    fine = await _session(manager_a, "Fine")
    await manager_a.shutdown()
    for sid in (column, tools, changed, fine):
        _fill_db(home_a, sid)
    dest = tmp_path / "usb"
    export_home(home_a, dest, include=["sessions"])
    for sid, sql in ((column, "ALTER TABLE agents ADD COLUMN future TEXT"),
                     (tools, "INSERT INTO kv(key, value) VALUES ('tools:alice', '{\"rev\": 99, \"tools\": []}')"),
                     (changed, "INSERT INTO kv(key, value) VALUES ('x', 'y')")):
        conn = sqlite3.connect(dest / "sessions" / sid / ".dreamteam" / "swarm.db")
        conn.execute(sql)
        conn.commit()
        conn.close()
        if sid != changed:
            _set_db_sha(dest, sid)
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager, plan, result = await _import(home_b, dest)
    reasons = {i.name: i.reason for i in plan.items}
    assert reasons[column].startswith("it was made by a newer DreamTeam (its database has agents.future)")
    assert reasons[tools].startswith("its agents use the tool set 99, which this DreamTeam does not know")
    assert "update DreamTeam here" in reasons[column] and "update DreamTeam here" in reasons[tools]
    assert reasons[changed].startswith("its database is not the one the export wrote")
    assert _statuses(result) == {("sessions", column): "refused", ("sessions", tools): "refused",
                                 ("sessions", changed): "refused", ("sessions", fine): "added"}
    assert result.exit_code == 1 and sorted(os.listdir(home_b / "sessions")) == [fine]
    assert f"sessions  {column}: refused (it was made by a newer DreamTeam" in render_import_result(result)
    await manager.shutdown()


async def test_settings_merge_keeps_machine_local_keys_and_drops_what_this_version_does_not_know(tmp_path,
                                                                                                    monkeypatch):
    raw_a = _raw(lm={"provider": "openai", "model": "x", "base_url": "http://localhost:1234/v1",
                     "api_key": "lm-studio"})
    raw_a.update(server={"port": 9000, "host": "0.0.0.0"}, shell={"program": "bash", "timeout": 60},
                 skills={"python": "3.12", "max_mb": 20})
    home_a = _home_named(tmp_path, "a", raw_a)
    dest = tmp_path / "usb"
    export_home(home_a, dest, include=["settings"])
    path = dest / "settings" / "dreamteam.toml"
    exported = tomllib.loads(path.read_text(encoding="utf-8"))
    exported["swarm"]["future_key"] = 1
    exported["future"] = {"x": 1}
    exported["models"]["default"]["future_field"] = "y"
    exported["roles"].append({"name": "Scout", "description": "Looks ahead", "model": "missing"})
    path.write_text(st.dump_toml(exported), encoding="utf-8")
    raw_b = _raw_b()
    raw_b["shell"] = {"program": "pwsh"}
    home_b = _home_named(tmp_path, "b", raw_b)
    manager, plan, result = await _import(home_b, dest)
    item = _item(plan.items, "settings", "dreamteam.toml")
    assert item.action == "rewrite"
    assert "kept as they are here: server.host, server.port, shell.program, skills.python" in item.reason
    assert "profiles added: pasted, lm" in item.reason and "roles added: Archivist, Scout" in item.reason
    assert "settings this DreamTeam does not know are left out: swarm.future_key, [future], " \
           "models.default.future_field" in item.warnings
    assert result.ok, render_import_result(result)
    raw = st.read_raw(home_b)
    assert raw["server"] == {"port": 8800} and raw["shell"] == {"program": "pwsh", "timeout": 60}
    assert raw["skills"] == {"max_mb": 20} and "future" not in raw and "future_key" not in raw["swarm"]
    assert raw["models"]["lm"]["api_key"] == "lm-studio"   # the placeholder of the LM Studio preset comes back
    assert "api_key" not in raw["models"]["fast"] and raw["models"]["default"]["api_key"] == B_KEY
    assert {r["name"]: r for r in raw["roles"]}["Scout"] == {"name": "Scout", "description": "Looks ahead"}
    warnings = "\n".join(item.warnings)
    assert "model profile 'fast' points at another endpoint now, so the API key stored here was removed: enter the " \
           "key for profile fast again" in warnings
    assert "role 'Scout' uses the model profile 'missing'" in warnings
    assert "model profile 'lm' uses http://localhost:1234/v1: that server must run on this computer" in warnings
    await manager.shutdown()


def test_merge_settings_and_merge_external():
    target = {"models": {"p": {"provider": "openai", "model": "m", "base_url": "https://a/v1", "api_key": "k" * 9},
                         "q": {"provider": "openai", "model": "m", "api_key_env": "sk-pasted-key-12345"}},
              "agents": {"default_model": "p"}}
    exported = {"models": {"p": {"provider": "openai", "model": "m2", "base_url": "https://a/v1/"},
                           "q": {"provider": "openai", "model": "m", "base_url": "https://b/v1"}},
                "agents": {"default_model": "nothere"}}
    m = merge_settings(target, exported, environ={})
    assert m.error is None and m.models_rewritten == ["p", "q"] and m.keys_removed == ["q"]
    assert m.new_raw["models"]["p"]["api_key"] == "k" * 9 and m.new_raw["models"]["p"]["model"] == "m2"
    assert "api_key" not in m.new_raw["models"]["q"] and "api_key_env" not in m.new_raw["models"]["q"]
    assert m.new_raw["agents"] == {"default_model": "p"} and "agents" not in m.update
    assert any("the default model profile 'nothere'" in w for w in m.warnings)
    assert not merge_settings(target, {"models": {"p": target["models"]["p"]}}).changed   # its key is not compared
    assert merge_settings({}, {"models": {"x": {"provider": 3}}}).error

    here = {"s": {"command": ["srv", "--token", "tok-here-1234"], "cwd": "/w", "env": {"A": "a-here", "B": "b"}},
            "t": {"command": ["old"], "env": {"T": "t-here"}}, "only": {"command": ["x"]}}
    exported = {"s": {"command": ["srv", "--token", ""], "cwd": "/w", "description": "", "env_keys": ["A", "C"]},
                "t": {"command": ["new"], "env_keys": ["T"]}, "n": {"command": "n --x", "env_keys": []}}
    servers, actions, notes = merge_external(here, exported)
    assert servers["s"] == here["s"] and actions["s"] == "unchanged"   # credentials and env values of this computer
    assert notes["s"] == ["enter C for s in Settings"]
    assert servers["t"] == {"command": ["new"]} and actions["t"] == "rewrite"
    assert any("the env values stored here for it were dropped: enter T" in w for w in notes["t"])
    assert actions["n"] == "add" and servers["only"] == {"command": ["x"]}
    servers, _actions, notes = merge_external(here, {"s": {"command": ["srv", "--token", ""], "cwd": "/other"}},
                                              keep_others=False, where="the settings of session 1")
    assert list(servers) == ["s"] and "env" not in servers["s"] and servers["s"]["command"] == ["srv", "--token", ""]
    assert any("re-enter the credentials of s on this computer" in w for w in notes["s"])


async def test_a_dry_run_changes_nothing(tmp_path, monkeypatch):
    _libraries(monkeypatch, tmp_path, "a")
    home_a, manager_a, one, _two = await _full_home(tmp_path)
    export_home(home_a, tmp_path / "usb")
    await manager_a.shutdown()
    _libraries(monkeypatch, tmp_path, "b")
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager_b = _manager(home_b)
    mine = await _session(manager_b, "Alpha")
    await manager_b.shutdown()
    _fill_db(home_b, mine)
    shutil.copytree(home_a / "sessions" / one, home_b / "sessions" / "033-copy")
    _event(home_b, "033-copy", 7.0)
    libraries = tmp_path / "libs-b"
    libraries.mkdir(exist_ok=True)
    before = (_home_state(home_b), scan_tree(libraries, exclude_names=(), exclude_paths=()).digest)
    manager = _manager(home_b)
    plan = plan_import(manager, tmp_path / "usb")
    text = render_import_plan(plan)
    assert (_home_state(home_b), scan_tree(libraries, exclude_names=(), exclude_paths=()).digest) == before
    assert f"sessions  {one}: rewrite (this computer has newer work in it; the old copy is kept in sessions/" \
           f".replaced-033-copy-<time>)" in text
    assert "Nothing else here changes: what the export does not carry stays as it is." in text
    assert plan.to_json()["changes"] is True
    await manager.shutdown()


async def test_a_failed_swap_is_undone_completely(tmp_path, monkeypatch):
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    sid = await _session(manager_a, "Inside")
    await manager_a.shutdown()
    _write(home_a / "proj" / "app.py", "v1\n")
    _set_meta(home_a, sid, workspace={"path": str(home_a / "proj"), "adopted": False, "in_home": "proj"})
    _fill_db(home_a, sid)
    export_home(home_a, tmp_path / "x1", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    assert (await _import(home_b, tmp_path / "x1"))[2].ok
    _write(home_b / "proj" / "app.py", "v1 on B\n")
    _event(home_a, sid, 5.0)
    _write(home_a / "proj" / "app.py", "v2\n")
    export_home(home_a, tmp_path / "x2", include=["sessions"])
    before = _home_state(home_b)

    real = sessions_module._rename_retry

    def refusing(src, dst, done=None):
        if Path(dst) == home_b / "sessions" / sid and Path(src).name.startswith(".importing-"):
            raise PermissionError(13, "The process cannot access the file", str(dst))
        return real(src, dst, done)

    monkeypatch.setattr(sessions_module, "_rename_retry", refusing)
    manager, _plan, result = await _import(home_b, tmp_path / "x2")
    item = _item(result.items, "sessions", sid)
    assert item.status == "failed" and "its files are in use" in item.error and "Nothing was changed" in item.error
    assert _home_state(home_b) == before and _leftovers(home_b) == []
    assert (home_b / "proj" / "app.py").read_text() == "v1 on B\n"
    monkeypatch.setattr(sessions_module, "_rename_retry", real)
    manager, _plan, result = await _import(home_b, tmp_path / "x2")
    assert result.ok and (home_b / "proj" / "app.py").read_text() == "v2\n"
    backup = Path(_item(result.items, "sessions", sid).backup)
    assert (backup / "workspace" / "app.py").read_text() == "v1 on B\n"
    assert (backup / "session" / "session.json").exists()
    await manager.shutdown()


async def test_ctrl_c_finishes_the_session_being_imported_then_stops(tmp_path, monkeypatch):
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    first = await _session(manager_a, "First")
    second = await _session(manager_a, "Second")
    await manager_a.shutdown()
    export_home(home_a, tmp_path / "usb", include=["sessions", "mcp"])
    home_b = _home_named(tmp_path, "b", {**_raw_b(), "mcp": {}})
    # a pid no process can have (pid 1 is init on POSIX, so it would count as a live import)
    _write(home_b / "sessions" / ".importing-left-4294967294-abc" / "x.txt", "from an interrupted import\n")
    manager = _manager(home_b)
    plan = plan_import(manager, tmp_path / "usb")
    calls = []

    def ctrl_c(sid):
        calls.append(sid)
        asyncio.current_task().cancel()

    result = await asyncio.create_task(apply_import(manager, plan, before_swap=ctrl_c))
    assert calls == [first] and result.interrupted and result.exit_code == 130
    assert _statuses(result)[("sessions", first)] == "added"
    assert _statuses(result)[("sessions", second)] == "not done"
    assert _statuses(result)[("mcp", "external fetch")] == "not done"
    assert sorted(os.listdir(home_b / "sessions")) == [first]   # the stale staging folder is gone too
    text = render_import_result(result)
    assert "stopped (Ctrl+C)" in text and f"{second}: not done" in text and "run the import again" in text

    def interrupt(sid):
        raise KeyboardInterrupt

    plan = plan_import(manager, tmp_path / "usb")
    result = await apply_import(manager, plan, before_swap=interrupt)
    assert result.interrupted and _statuses(result)[("sessions", second)] == "not done"
    assert sorted(os.listdir(home_b / "sessions")) == [first]

    def taken(sid):
        raise TransferError("a hub started meanwhile")

    plan = plan_import(manager, tmp_path / "usb")
    result = await apply_import(manager, plan, before_swap=taken)
    assert _item(result.items, "sessions", second).error == "a hub started meanwhile" and not result.interrupted
    assert _statuses(result)[("mcp", "external fetch")] == "added" and result.exit_code == 1
    await manager.shutdown()


async def test_what_an_import_refuses_before_it_starts(tmp_path, monkeypatch):
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    sid = await _session(manager_a, "One")
    await manager_a.shutdown()
    dest = tmp_path / "usb"
    export_home(home_a, dest)
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager = _manager(home_b)
    with pytest.raises(TransferError, match="is not a folder"):
        plan_import(manager, tmp_path / "missing")
    (tmp_path / "half").mkdir()
    with pytest.raises(TransferError, match="no dreamteam-export.json: the export did not finish"):
        read_manifest(tmp_path / "half")
    manifest = _manifest(dest)
    for change, message in (({"format_version": 2}, "made by a newer DreamTeam"), ({"format": "zip"}, "not the "
                            "manifest"), ({"format_version": "1"}, "no valid export format")):
        (dest / MANIFEST).write_text(json.dumps({**manifest, **change}), encoding="utf-8")
        with pytest.raises(TransferError, match=message):
            read_manifest(dest)
    (dest / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(TransferError, match="Unknown --include category") as err:
        plan_import(manager, dest, include=["sessions,nope"])
    assert err.value.usage
    plan = plan_import(manager, dest, include=["sessions"])
    assert plan.include == ("sessions",) and [i.name for i in plan.items] == [sid]
    shutil.copytree(dest, home_b / "sessions" / "inside")
    with pytest.raises(TransferError, match="is inside the sessions folder of this home"):
        plan_import(manager, home_b / "sessions" / "inside")
    assert (await _import(home_b, dest, include=["sessions"]))[2].ok
    manager.running.add(sid)
    _event(home_a, sid, 1.0)
    export_home(home_a, dest, include=["sessions"])
    plan = plan_import(manager, dest)
    assert [(i.action, i.reason) for i in plan.items] == [("skip", "it is running here: stop it first")]
    export_home(home_a, tmp_path / "settings-only", include=["settings"])
    plan = plan_import(manager, tmp_path / "settings-only", include=["skills"])
    assert plan.items == [] and plan.warnings == ["the export holds no skills (it was made with --include settings)"]
    assert plan.exit_code == 1 and plan.missing == ["skills"]   # what was asked for cannot be imported
    with pytest.raises(TransferError, match="the export holds no sessions") as err:   # not: the other categories
        plan_import(manager, tmp_path / "settings-only", sessions=[sid])
    assert err.value.usage
    plan = plan_import(manager, dest, sessions=[sid])   # --session: no warnings about categories not asked for
    assert not any(w.startswith("the export holds no") for w in plan.warnings)
    await manager.shutdown()


def test_portability_warnings_name_what_the_file_system_here_refuses(tmp_path, monkeypatch):
    (tmp_path / "ws").mkdir()
    monkeypatch.setattr(transfer.os, "walk", lambda top: iter([(str(top), ["Sub", "sub"], ["nul", "x.", "ok.txt"])]))
    monkeypatch.setattr(transfer, "_target_platform", lambda: "win32")
    warnings = transfer._portability_warnings(tmp_path / "ws", "001-x")
    assert warnings[0].startswith("session 001-x: 2 file names in its workspace cannot exist on Windows (nul, x.)")
    assert "differ only in case (Sub and sub)" in warnings[1] and len(warnings) == 2
    monkeypatch.setattr(transfer, "_target_platform", lambda: "darwin")
    assert len(transfer._portability_warnings(tmp_path / "ws", "001-x")) == 1
    monkeypatch.setattr(transfer, "_target_platform", lambda: "linux")
    assert transfer._portability_warnings(tmp_path / "ws", "001-x") == []


@pytest.mark.skipif(os.name == "nt", reason="such names cannot exist on Windows")
async def test_names_the_target_file_system_cannot_hold_are_warned(tmp_path, monkeypatch):
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    sid = await _session(manager_a, "Names")
    await manager_a.shutdown()
    ws = _workspace(home_a, sid)
    for name in ("con.txt", "a:b", "ok.txt"):
        _write(ws / name, "x\n")
    _write(ws / "Readme", "one\n")
    sensitive = not (ws / "README").exists()
    if sensitive:
        _write(ws / "README", "two\n")
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager = _manager(home_b)
    monkeypatch.setattr(transfer, "_target_platform", lambda: "win32")
    item = _item(plan_import(manager, tmp_path / "usb").items, "sessions", sid)
    assert item.action == "add"   # warned, not refused
    warnings = "\n".join(item.warnings)
    assert "2 file names in its workspace cannot exist on Windows (a:b, con.txt)" in warnings
    assert ("differ only in case (README and Readme)" in warnings) == sensitive
    monkeypatch.setattr(transfer, "_target_platform", lambda: "linux")
    assert _item(plan_import(manager, tmp_path / "usb").items, "sessions", sid).warnings == []
    await manager.shutdown()


# ---- review fixes ------------------------------------------------------------------------------------------------
async def _one_session(tmp_path: Path, name: str = "a", title: str = "Alpha", **meta: Any) -> tuple[Path, str]:
    home = _home_named(tmp_path, name, _raw())
    manager = _manager(home)
    sid = await _session(manager, title)
    await manager.shutdown()
    if meta:
        _set_meta(home, sid, **meta)
    return home, sid


async def test_an_outside_folder_is_checked_here_and_an_existing_one_is_the_users_project(tmp_path, monkeypatch):
    home_b = _home_named(tmp_path, "b", _raw_b())
    project = _write(tmp_path / "project" / "README.md", "the user's project on B\n").parent
    users = tmp_path / "users"
    _write(users / "me" / "x.txt", "x\n")
    for path in (home_b, users, project):
        home_a, sid = await _one_session(tmp_path / f"case-{path.name}", workspace={"path": str(path),
                                                                                   "adopted": False})
        export_home(home_a, tmp_path / f"usb-{path.name}", include=["sessions"])
    monkeypatch.setenv("HOME", str(users / "me"))
    monkeypatch.setenv("USERPROFILE", str(users / "me"))
    manager = _manager(home_b)
    item = _item(plan_import(manager, tmp_path / "usb-b").items, "sessions", sid)
    assert item.action == "refuse" and "DreamTeam home" in item.reason
    item = _item(plan_import(manager, tmp_path / "usb-users").items, "sessions", sid)
    assert item.action == "refuse" and "contains your home folder" in item.reason
    plan = plan_import(manager, tmp_path / "usb-project")
    item = _item(plan.items, "sessions", sid)
    assert item.action == "add" and "adopted as the user's project" in render_import_plan(plan)
    result = await apply_import(manager, plan)
    assert result.ok and manager.meta(sid)["workspace"] == {"path": str(project), "adopted": True}
    await manager.shutdown()


async def test_an_outside_folder_another_session_works_in_is_refused(tmp_path, monkeypatch):
    shared = _write(tmp_path / "proj" / "README.md", "x\n").parent
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    same = await _session(manager_a, "Same")
    sub = await _session(manager_a, "Sub")
    fine = await _session(manager_a, "Fine")
    await manager_a.shutdown()
    _set_meta(home_a, same, workspace={"path": str(shared), "adopted": True})
    _set_meta(home_a, sub, workspace={"path": str(shared / "sub"), "adopted": True})
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager = _manager(home_b)
    plan = plan_import(manager, tmp_path / "usb")   # 002-sub lies inside the folder of 001-same of this import
    assert _item(plan.items, "sessions", same).action == "add"
    assert _item(plan.items, "sessions", sub).action == "refuse"
    assert "two sessions cannot share files" in _item(plan.items, "sessions", sub).reason
    keeper = await _session(manager, "Keeper")
    _set_meta(home_b, keeper, workspace={"path": str(shared), "adopted": True})
    plan = plan_import(manager, tmp_path / "usb")
    assert {i.name: i.action for i in plan.items} == {same: "refuse", sub: "refuse", fine: "add"}
    assert f"which is the folder of session {keeper}" in _item(plan.items, "sessions", same).reason
    result = await apply_import(manager, plan)
    assert _statuses(result)[("sessions", fine)] == "added"
    await manager.shutdown()


async def test_a_session_override_never_inherits_the_env_values_of_another_command(tmp_path, monkeypatch):
    _libraries(monkeypatch, tmp_path, "a")
    home_a, sid = await _one_session(tmp_path, overrides={"mcp": {"external": {"github": {
        "command": ["python", "-c", "print(1)"]}}}})
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    _libraries(monkeypatch, tmp_path, "b")
    raw_b = _raw_b()
    raw_b["mcp"]["external"]["github"] = {"command": ["npx", "server-github"], "env": {"GITHUB_TOKEN": "ghp_on_b_1234"}}
    home_b = _home_named(tmp_path, "b", raw_b)
    manager, plan, result = await _import(home_b, tmp_path / "usb")
    assert result.ok
    assert any(f"enter GITHUB_TOKEN for github in the settings of session {sid}" in w
               for w in _item(plan.items, "sessions", sid).warnings)
    effective = manager._merged(manager.raw, manager.meta(sid)["overrides"])["mcp"]["external"]["github"]
    assert effective["command"] == ["python", "-c", "print(1)"] and effective["env"] == {"GITHUB_TOKEN": ""}
    await manager.shutdown()


async def test_a_credential_in_an_mcp_command_leaves_every_place_of_the_history(tmp_path, monkeypatch):
    password = "s3cretpassw0rd-zz"
    raw = {**_raw(), "mcp": {"external": {"pg": {"command": [sys.executable, "-c", "pass",
                                                             f"postgres://bob:{password}@db/x"]}}}}
    home = _home_named(tmp_path, "a", raw)
    manager = _manager(home)
    sid = await _session(manager, "Pg")
    swarm = await manager.start(sid)
    for _ in range(200):
        if swarm.db.scalar("SELECT COUNT(*) FROM events WHERE type = 'mcp.created'"):
            break
        await asyncio.sleep(0.025)
    await manager.shutdown()
    db = _db(home, sid)
    try:
        assert db.scalar("SELECT COUNT(*) FROM events WHERE instr(data, ?) > 0", (password,))
        command = json.dumps(raw["mcp"]["external"]["pg"]["command"])   # an mcp_list result an agent got
        db.execute("INSERT INTO history(agent, seq, msg) VALUES ('alice', 7, ?)", (json.dumps({"text": command}),))
    finally:
        db.close()
    report = export_home(home, tmp_path / "usb")
    assert report.ok
    assert password.encode() not in _all_bytes(tmp_path / "usb")
    assert f"session {sid}: the credentials of MCP server pg were removed from its history" in "\n".join(
        report.warnings)


def test_a_secret_split_over_overflow_pages_is_found(tmp_path):
    key = "sk-ant-" + "k" * 51
    secrets = Secrets()
    secrets.add(key, "the test key")
    for suffix in range(4000, 8200, 20):
        path = tmp_path / f"m{suffix}.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE messages (body TEXT)")
        conn.execute("INSERT INTO messages VALUES (?)", ("x" * 1000 + key + "y" * suffix,))
        conn.commit()
        conn.close()
        if key.encode() not in path.read_bytes():
            break
        path.unlink()
    else:
        pytest.skip("no row length splits the key on this SQLite page size")
    assert scan_for_secrets(tmp_path, secrets) == [f"{path.name} (messages.body:1): the test key"]


async def test_imported_job_logs_point_at_the_copy_or_nowhere(tmp_path, monkeypatch):
    from dreamteam.jobs import JobManager

    home_a, sid = await _one_session(tmp_path)
    _fill_db(home_a, sid)
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    victim = _write(tmp_path / "victim" / "thesis.docx", "precious\n")
    _write(tmp_path / "usb" / "sessions" / sid / ".dreamteam" / "jobs" / "j2.log", "job two\n")
    conn = sqlite3.connect(tmp_path / "usb" / "sessions" / sid / ".dreamteam" / "swarm.db")
    other = home_a / "sessions" / "017-other" / ".dreamteam" / "jobs" / "j2.log"
    for job_id, log in ((1, str(victim)), (2, str(other)), (3, ".scratch/alice/jobs/j3.log")):
        conn.execute("INSERT INTO jobs(id, owner, command, origin, status, log) VALUES (?, 'alice', 'x', 'shell', "
                     "'done', ?)", (job_id, log))
    conn.commit()
    conn.close()
    _set_db_sha(tmp_path / "usb", sid)
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager, _plan, result = await _import(home_b, tmp_path / "usb")
    assert result.ok
    db = _db(home_b, sid)
    try:
        logs = {r["id"]: r["log"] for r in db.all("SELECT id, log FROM jobs")}
    finally:
        db.close()
    jobs_here = home_b / "sessions" / sid / ".dreamteam" / "jobs"
    assert logs == {1: None, 2: str(jobs_here / "j2.log"), 3: ".scratch/alice/jobs/j3.log"}
    stub = SimpleNamespace(swarm=SimpleNamespace(config=SimpleNamespace(data_dir=jobs_here.parent),
                                                 workspace=SimpleNamespace(root=str(_workspace(home_b, sid)))))
    assert JobManager._log_file(stub, str(victim)) is None   # never a file outside the session's jobs folder
    assert JobManager._log_file(stub, str(jobs_here / "j2.log")) == jobs_here / "j2.log"
    assert victim.exists()
    await manager.shutdown()


async def test_the_plan_shows_external_commands_and_approvals(tmp_path, monkeypatch):
    _libraries(monkeypatch, tmp_path, "a")
    raw = {**_raw(), "mcp": {"external": {"helper": {"command": ["uvx", "helper-mcp", "--token", "tok-123456789"],
                                                     "cwd": "/srv/helper"}}}}
    home_a = _home_named(tmp_path, "a", raw)
    _library_entry("tool", None)
    export_home(home_a, tmp_path / "usb", include=["mcp"])
    _libraries(monkeypatch, tmp_path, "b")
    raw_b = {**_raw_b(), "mcp": {"require_approval": True}}
    home_b = _home_named(tmp_path, "b", raw_b)
    manager = _manager(home_b)
    plan = plan_import(manager, tmp_path / "usb")
    text = render_import_plan(plan)
    assert "mcp       external helper: add (runs uvx helper-mcp --token '' in /srv/helper)" in text
    assert "mcp       library tool: add (approved on the other computer)" in text
    assert "MCP server tool is approved in the export, so agents here can run it without asking" in text
    st.write_raw(home_b, {**raw_b, "mcp": {"external": {"helper": {"command": ["uvx", "old-helper"]}}}})
    manager = _manager(home_b)
    text = render_import_plan(plan_import(manager, tmp_path / "usb"))
    assert "external helper: rewrite (command changes: uvx old-helper -> uvx helper-mcp --token '' in " \
           "/srv/helper)" in text


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership")
async def test_an_empty_destination_of_another_user_is_refused(tmp_path, monkeypatch):
    home, _sid = await _one_session(tmp_path)
    dest = tmp_path / "squatted"
    dest.mkdir()
    real = os.lstat

    def foreign(path, *args, **kw):
        result = real(path, *args, **kw)
        if Path(path) == dest:
            values = list(result)
            values[4] = os.geteuid() + 1   # st_uid
            return os.stat_result(values)
        return result

    monkeypatch.setattr(transfer.os, "lstat", foreign)
    with pytest.raises(TransferError, match="belongs to another user"):
        export_home(home, dest, include=["settings"])
    assert os.listdir(dest) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
async def test_a_new_or_empty_destination_is_private(tmp_path):
    home, _sid = await _one_session(tmp_path)
    export_home(home, tmp_path / "new", include=["settings"])
    assert (tmp_path / "new").stat().st_mode & 0o777 == 0o700
    (tmp_path / "empty").mkdir(mode=0o755)
    export_home(home, tmp_path / "empty", include=["settings"])
    assert (tmp_path / "empty").stat().st_mode & 0o777 == 0o700


def test_a_new_destination_keeps_the_inherited_access_rules_on_windows(tmp_path, monkeypatch):
    calls = []
    real = os.mkdir

    def recording(path, *args, **kw):
        calls.append(args or tuple(kw.values()))
        return real(path, *args, **kw)

    monkeypatch.setattr(transfer.os, "mkdir", recording)
    transfer._mkdir_private(tmp_path / "x")
    assert calls == ([()] if os.name == "nt" else [(0o700,)])


async def test_a_target_session_whose_workspace_move_did_not_finish_is_not_rewritten(tmp_path, monkeypatch):
    home_a, sid = await _one_session(tmp_path)
    export_home(home_a, tmp_path / "x1", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    assert (await _import(home_b, tmp_path / "x1"))[2].ok
    _event(home_a, sid, 7.0)
    export_home(home_a, tmp_path / "x2", include=["sessions"])
    journal = _write(home_b / "sessions" / sid / ".dreamteam" / MOVE_JOURNAL, json.dumps({"from": "a", "to": "b"}))
    manager = _manager(home_b)
    plan = plan_import(manager, tmp_path / "x2")
    item = _item(plan.items, "sessions", sid)
    assert item.action == "refuse" and f"delete .dreamteam/{MOVE_JOURNAL}" in item.reason
    staged = manager.staging_path(sid)
    staged.mkdir()
    with pytest.raises(sessions_module.SessionError, match="did not finish") as err:
        await manager.import_rewrite(sid, staged, manager.meta(sid), sessions_module.ImportedWorkspace("own"))
    assert err.value.status == 409 and journal.exists() and manager.backups(sid) == []
    await manager.shutdown()


async def test_the_settings_before_the_import_stay_in_the_backup_file(tmp_path, monkeypatch):
    raw = _raw()
    raw["mcp"]["external"]["fetch"]["command"] = ["uvx", "another-fetch"]   # another command: B's env values go
    home_a = _home_named(tmp_path, "a", raw)
    export_home(home_a, tmp_path / "usb", include=["settings", "mcp"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    before = st.settings_path(home_b).read_bytes()
    manager, _plan, result = await _import(home_b, tmp_path / "usb")
    statuses = _statuses(result)
    assert statuses[("settings", "dreamteam.toml")] == "rewritten" and statuses[("mcp", "external fetch")] == \
        "rewritten"
    bak = st.settings_path(home_b).with_name("dreamteam.toml.bak")
    assert bak.read_bytes() == before and B_FAST_KEY.encode() in before
    assert f"the settings from before the import are in {bak}" in render_import_result(result)
    await manager.shutdown()


async def test_an_import_that_died_halfway_through_a_swap_is_undone_by_the_next(tmp_path, monkeypatch):
    home_a, sid = await _one_session(tmp_path)
    _write(_workspace(home_a, sid) / "a.txt", "A\n")
    export_home(home_a, tmp_path / "x1", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    assert (await _import(home_b, tmp_path / "x1"))[2].ok
    _write(_workspace(home_b, sid) / "b.txt", "work on B\n")
    _event(home_a, sid, 3.0)
    export_home(home_a, tmp_path / "x2", include=["sessions"])
    real = sessions_module._rename_retry
    calls = []

    def dying(src, dst, done=None):   # the process "dies" after the first rename: nothing is undone
        calls.append(dst)
        if len(calls) > 1:
            raise OSError(5, "power loss")
        return real(src, dst, done)

    monkeypatch.setattr(sessions_module, "_rename_retry", dying)
    monkeypatch.setattr(sessions_module, "_undo_renames", lambda done, created: ["(not undone)"])
    manager, _plan, result = await _import(home_b, tmp_path / "x2")
    assert _statuses(result)[("sessions", sid)] == "failed" and sid not in manager.ids()
    monkeypatch.setattr(sessions_module, "_rename_retry", real)
    monkeypatch.setattr(sessions_module, "_undo_renames", sessions_module._undo_renames)
    manager = _manager(home_b)
    plan = plan_import(manager, tmp_path / "x2")
    assert any("an earlier import stopped while it replaced a session" in w for w in plan.warnings)
    result = await apply_import(manager, plan)
    assert sid in manager.ids() and (_workspace(home_b, sid) / "b.txt").read_text() == "work on B\n"
    assert any(f"the import of session {sid} that stopped halfway was undone" in w for w in result.warnings)
    assert manager.interrupted_swaps() == []
    manager, plan, result = await _import(home_b, tmp_path / "x2")
    assert result.ok and _statuses(result)[("sessions", sid)] == "rewritten"
    assert (_workspace(home_b, sid) / "a.txt").exists() and not (_workspace(home_b, sid) / "b.txt").exists()
    await manager.shutdown()


def test_merge_settings_keeps_the_credentials_set_up_here_for_the_same_endpoint():
    header = {"provider": "openai", "model": "m", "base_url": "https://gw.example.com/v1",
              "extra": {"headers": {"Authorization": "Bearer abcdefgh123"}, "temperature": 0.2}}
    exported, _omitted, _removed = scrub_settings({"models": {"gw": header}})
    m = merge_settings({"models": {"gw": header}}, exported)
    assert not m.models_rewritten and not m.changed   # the same profile: unchanged, its header stays
    changed = {**exported["models"]["gw"], "model": "m2"}
    m = merge_settings({"models": {"gw": header}}, {"models": {"gw": changed}})
    assert m.new_raw["models"]["gw"]["extra"]["headers"] == {"Authorization": "Bearer abcdefgh123"}
    assert m.new_raw["models"]["gw"]["model"] == "m2"
    mine = {"provider": "openai", "model": "gpt", "api_key_env": "MY_OPENAI_KEY"}
    m = merge_settings({"models": {"default": mine}}, {"models": {"default": {"provider": "openai", "model": "gpt5"}}},
                       environ={})
    assert m.new_raw["models"]["default"]["api_key_env"] == "MY_OPENAI_KEY"
    assert any("(or set MY_OPENAI_KEY)" in w for w in m.warnings)
    other = {**changed, "base_url": "https://other.example.com/v1"}
    m = merge_settings({"models": {"gw": {**header, "api_key_env": "GW_KEY"}}}, {"models": {"gw": other}},
                       environ={})
    assert "headers" not in m.new_raw["models"]["gw"].get("extra", {}) or \
        m.new_raw["models"]["gw"]["extra"]["headers"] == {}
    assert "api_key_env" not in m.new_raw["models"]["gw"]
    text = "\n".join(m.warnings)
    assert "extra.headers.Authorization configured here was removed" in text and "Bearer" not in text
    assert "the key variable GW_KEY configured here was removed" in text


def test_merge_external_compares_the_command_as_it_runs():
    here = {"gh": {"command": "npx -y @modelcontextprotocol/server-github", "env": {"GITHUB_TOKEN": "t-123456789"}},
            "f": {"command": ["fetch"], "env": {}}}
    exported = {"gh": {"command": ["npx", "-y", "@modelcontextprotocol/server-github"], "env_keys": ["GITHUB_TOKEN"]},
                "f": {"command": ["fetch"], "env_keys": []}}
    servers, actions, notes = merge_external(here, exported)
    assert actions == {"gh": "unchanged", "f": "unchanged"} and servers["gh"] == here["gh"]
    assert notes == {"gh": [], "f": []}
    flipped = {"gh": {"command": ["npx", "-y", "@modelcontextprotocol/server-github"], "env": {"GITHUB_TOKEN": "t-1"}}}
    servers, actions, _notes = merge_external(flipped, {"gh": {"command": "npx -y @modelcontextprotocol/server-github",
                                                               "env_keys": ["GITHUB_TOKEN"]}})
    assert servers["gh"]["env"] == {"GITHUB_TOKEN": "t-1"} and actions["gh"] == "unchanged"


async def test_copies_of_one_session_in_the_export_are_matched_by_id(tmp_path, monkeypatch):
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    sid = await _session(manager_a, "Copied")
    await manager_a.shutdown()
    _write(_workspace(home_a, sid) / "who.txt", "first\n")
    copy = f"{sid}-copy"
    shutil.copytree(home_a / "sessions" / sid, home_a / "sessions" / copy)
    _write(_workspace(home_a, copy) / "who.txt", "second\n")
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager, _plan, result = await _import(home_b, tmp_path / "usb", sessions=[sid])
    assert result.ok
    plan = plan_import(manager, tmp_path / "usb", sessions=[copy])
    assert [(i.name, i.action, i.target) for i in plan.items] == [(copy, "add", copy)]
    assert "with a new identity" in plan.items[0].reason
    assert (await apply_import(manager, plan)).ok
    assert (_workspace(home_b, sid) / "who.txt").read_text() == "first\n"
    assert (_workspace(home_b, copy) / "who.txt").read_text() == "second\n"
    assert session_uid(manager.meta(sid)) != session_uid(manager.meta(copy))
    for name in (sid, copy):   # imported again by id: each matches its own copy here
        assert [(i.action, i.target) for i in plan_import(manager, tmp_path / "usb", sessions=[name]).items] == \
            [("unchanged", name)]
    await manager.shutdown()
    home_c = _home_named(tmp_path, "c", _raw_b())
    manager, _plan, result = await _import(home_c, tmp_path / "usb", sessions=[sid, copy])
    assert result.ok and session_uid(manager.meta(sid)) != session_uid(manager.meta(copy))
    await manager.shutdown()


async def test_copies_here_that_the_export_cannot_name_are_refused_without_the_session_advice(tmp_path, monkeypatch):
    home_a, sid = await _one_session(tmp_path)
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    for name in ("010-alpha", "011-alpha-copy"):
        shutil.copytree(home_a / "sessions" / sid, home_b / "sessions" / name)
    manager = _manager(home_b)
    item = _item(plan_import(manager, tmp_path / "usb").items, "sessions", sid)
    assert item.action == "refuse" and "delete or move the copy you do not want here" in item.reason
    assert "--session" not in item.reason
    with pytest.raises(TransferError) as err:
        plan_import(manager, tmp_path / "usb", sessions=["010-alpha"])
    assert err.value.usage


async def test_a_changed_title_or_setting_is_imported_without_a_backup(tmp_path, monkeypatch):
    home_a, sid = await _one_session(tmp_path)
    _fill_db(home_a, sid)
    export_home(home_a, tmp_path / "x1", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    assert (await _import(home_b, tmp_path / "x1"))[2].ok
    manager_a = _manager(home_a)
    manager_a.update(sid, title="Renamed alpha", overrides={"agents": {"max_steps_per_turn": 7}})
    await manager_a.shutdown()
    export_home(home_a, tmp_path / "x2", include=["sessions"])
    manager, plan, result = await _import(home_b, tmp_path / "x2")
    item = _item(plan.items, "sessions", sid)
    assert item.action == "rewrite" and item.reason.startswith("its title and settings differ: the export's replace "
                                                               "the ones here, which may be newer")
    assert "no copy is kept" in item.reason
    assert "keeps its previous copy" not in render_import_plan(plan)   # no backup is promised: none is made
    assert "rewritten (its session.json only, no copy kept)" in render_import_result(result)
    assert result.ok and manager.meta(sid)["title"] == "Renamed alpha"
    assert manager.meta(sid)["overrides"] == {"agents": {"max_steps_per_turn": 7}}
    assert manager.backups(sid) == [] and _leftovers(home_b) == []
    assert [i.action for i in plan_import(manager, tmp_path / "x2").items] == ["unchanged"]
    manager.update(sid, title="Renamed here")
    assert _item(plan_import(manager, tmp_path / "x2").items, "sessions", sid).reason.startswith("its title differs")
    await manager.shutdown()


async def test_git_rewriting_its_index_leaves_a_session_unchanged(tmp_path, monkeypatch):
    home_a, sid = await _one_session(tmp_path)
    ws = _workspace(home_a, sid)
    _write(ws / "app.py", "print(1)\n")
    _write(ws / ".git" / "index", b"DIRC old index")
    _write(ws / ".git" / "logs" / "HEAD", "0000 1111 first\n")
    _write(ws / ".git" / "refs" / "heads" / "main", "1111\n")
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    assert (await _import(home_b, tmp_path / "usb"))[2].ok
    here = _workspace(home_b, sid) / ".git"
    (here / "index").write_bytes(b"DIRC index refreshed by git status")
    (here / "logs" / "HEAD").write_text("0000 1111 first\n1111 1111 checkout\n")
    _write(here / "index.lock", "")
    manager = _manager(home_b)
    assert [i.action for i in plan_import(manager, tmp_path / "usb").items] == ["unchanged"]
    (here / "refs" / "heads" / "main").write_text("2222\n")   # a real commit here is a change
    assert [i.action for i in plan_import(manager, tmp_path / "usb").items] == ["rewrite"]


async def test_a_round_trip_keeps_the_folder_the_session_has_on_each_computer(tmp_path, monkeypatch):
    home_a, sid = await _one_session(tmp_path)
    _write(home_a / "proj" / "app.py", "v1\n")
    _set_meta(home_a, sid, workspace={"path": str(home_a / "proj"), "adopted": True, "in_home": "proj"})
    export_home(home_a, tmp_path / "x1", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    _write(home_b / "proj" / "mine.txt", "B's own\n")
    assert (await _import(home_b, tmp_path / "x1"))[2].ok
    assert (home_b / f"proj-{sid}" / "app.py").exists()
    _write(home_b / f"proj-{sid}" / "app.py", "v2 from B\n")
    _event(home_b, sid, 4.0)
    export_home(home_b, tmp_path / "x2", include=["sessions"])
    manager, plan, result = await _import(home_a, tmp_path / "x2")
    assert result.ok and not any("moves" in w for w in _item(plan.items, "sessions", sid).warnings)
    assert manager.meta(sid)["workspace"]["in_home"] == "proj"
    assert (home_a / "proj" / "app.py").read_text() == "v2 from B\n" and not (home_a / f"proj-{sid}").exists()
    await manager.shutdown()


async def test_the_result_says_that_work_here_was_replaced(tmp_path, monkeypatch):
    home_a, sid = await _one_session(tmp_path)
    _fill_db(home_a, sid)
    export_home(home_a, tmp_path / "x1", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    assert (await _import(home_b, tmp_path / "x1"))[2].ok
    _event(home_a, sid, 5.0)
    _event(home_b, sid, 6.0)
    export_home(home_a, tmp_path / "x2", include=["sessions"])
    manager, _plan, result = await _import(home_b, tmp_path / "x2")
    assert "rewritten (both computers worked on it since they last had the same copy; previous copy: " in \
        render_import_result(result)
    await manager.shutdown()


async def test_session_overrides_keep_this_computers_machine_local_keys_and_env_values(tmp_path, monkeypatch):
    overrides = {"shell": {"program": "bash"}, "agents": {"default_model": "missing"},
                 "mcp": {"external": {"srv": {"command": ["srv"], "env": {"K": "a-value-123"}}}}}
    home_a, sid = await _one_session(tmp_path, overrides=overrides)
    export_home(home_a, tmp_path / "x1", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager, plan, result = await _import(home_b, tmp_path / "x1")
    assert result.ok
    meta = manager.meta(sid)["overrides"]
    assert "shell" not in meta and "agents" not in meta and meta["mcp"]["external"]["srv"] == {"command": ["srv"]}
    warnings = "\n".join(_item(plan.items, "sessions", sid).warnings)
    assert "model profile 'missing' does not exist here" in warnings and "enter K for srv" in warnings
    _set_meta(home_b, sid, overrides={"shell": {"program": "pwsh"},
                                      "mcp": {"external": {"srv": {"command": ["srv"], "env": {"K": "b-value-456"}}}}})
    await manager.shutdown()
    _event(home_a, sid, 2.0)
    export_home(home_a, tmp_path / "x2", include=["sessions"])
    manager, _plan, result = await _import(home_b, tmp_path / "x2")
    assert _statuses(result)[("sessions", sid)] == "rewritten"
    meta = manager.meta(sid)["overrides"]
    assert meta["shell"] == {"program": "pwsh"} and meta["mcp"]["external"]["srv"]["env"] == {"K": "b-value-456"}
    await manager.shutdown()
    _set_meta(home_a, sid, overrides={"mcp": {"external": {"srv": {"command": ["srv2"]}}}})
    _event(home_a, sid, 3.0)
    export_home(home_a, tmp_path / "x3", include=["sessions"])
    manager, plan, result = await _import(home_b, tmp_path / "x3")
    assert "env" not in manager.meta(sid)["overrides"]["mcp"]["external"]["srv"]
    assert any("its command or folder is not the one here" in w for w in _item(plan.items, "sessions", sid).warnings)
    await manager.shutdown()


async def test_a_plan_with_other_events_does_not_hash_the_workspace_here(tmp_path, monkeypatch):
    home_a, sid = await _one_session(tmp_path)
    export_home(home_a, tmp_path / "x1", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    assert (await _import(home_b, tmp_path / "x1"))[2].ok
    _event(home_a, sid, 3.0)
    export_home(home_a, tmp_path / "x2", include=["sessions"])
    calls = []
    real = transfer._tree
    monkeypatch.setattr(transfer, "_tree", lambda folder: calls.append(folder) or real(folder))
    plan = plan_import(_manager(home_b), tmp_path / "x2")
    assert [i.action for i in plan.items] == ["rewrite"] and calls == []


async def test_a_database_changed_between_the_plan_and_the_copy_fails_that_session(tmp_path, monkeypatch):
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    one = await _session(manager_a, "One")
    two = await _session(manager_a, "Two")
    await manager_a.shutdown()
    for sid in (one, two):
        _fill_db(home_a, sid)
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager = _manager(home_b)
    plan = plan_import(manager, tmp_path / "usb")
    with open(tmp_path / "usb" / "sessions" / one / ".dreamteam" / "swarm.db", "ab") as f:
        f.write(b"\0" * 16)
    result = await apply_import(manager, plan)
    assert _statuses(result) == {("sessions", one): "failed", ("sessions", two): "added"}
    assert "is not the one the export wrote" in _item(result.items, "sessions", one).error
    assert one not in manager.ids() and _leftovers(home_b) == []
    await manager.shutdown()


async def test_one_file_the_destination_cannot_hold_leaves_only_that_session_out(tmp_path, monkeypatch):
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    one = await _session(manager_a, "One")
    two = await _session(manager_a, "Two")
    await manager_a.shutdown()
    _write(_workspace(home_a, one) / "logs" / "run 12-30.txt", "x\n")
    _write(_workspace(home_a, two) / "fine.txt", "y\n")
    from dreamteam import treecopy

    real = treecopy.os.open

    def refusing(path, flags, *args, **kwargs):   # os.open is global: shutil.rmtree calls it with dir_fd on POSIX
        if str(path).endswith("run 12-30.txt") and flags & os.O_CREAT:
            raise OSError(22, "Invalid argument", str(path))
        return real(path, flags, *args, **kwargs)

    monkeypatch.setattr(treecopy.os, "open", refusing)
    report = export_home(home_a, tmp_path / "usb")
    assert not report.ok and [s["id"] for s in report.sessions] == [two]
    left = next(i for i in report.left_out if i["what"] == f"session {one}")
    assert "cannot hold this name or path" in left["reason"] and "run 12-30.txt" in left["reason"]
    assert (tmp_path / "usb" / "settings" / "dreamteam.toml").exists()
    assert not (tmp_path / "usb" / "sessions" / one).exists()


async def test_a_partial_export_that_cannot_be_removed_is_named(tmp_path, monkeypatch):
    home, sid = await _one_session(tmp_path)
    _write(_workspace(home, sid) / "notes.txt", f"the key {KEY}\n")
    monkeypatch.setattr(transfer, "DISCARD_PAUSE", 0)
    monkeypatch.setattr(transfer, "rmtree", lambda path: None)
    with pytest.raises(SecretFound) as err:
        export_home(home, tmp_path / "usb", include=["sessions"])
    assert "Could not remove the partial export" in str(err.value) and "delete it by hand" in str(err.value)
    assert "it may contain the secret named above" in str(err.value)


async def test_a_destination_in_another_case_is_still_inside_the_home(tmp_path, monkeypatch):
    if not Path(str(tmp_path).swapcase()).exists():
        pytest.skip("this file system tells case apart")
    home, sid = await _one_session(tmp_path)
    home = home.resolve()
    monkeypatch.setattr(os.path, "normcase", lambda s: s)   # as on macOS: no case folding, realpath keeps the case
    monkeypatch.setattr(os.path, "realpath", lambda p, *a, **k: os.path.abspath(p))
    dest = Path(str(home).swapcase()) / "sessions" / sid / "workspace" / "exp"
    with pytest.raises(TransferError, match="is inside"):
        export_home(home, dest)


def test_sqlite_uri_keeps_unc_servers_and_escapes_names(tmp_path):
    from dreamteam.db import sqlite_uri

    if os.name == "nt":
        assert sqlite_uri(r"\\server\share\x.db", "mode=ro") == "file:////server/share/x.db?mode=ro"
        assert sqlite_uri(r"C:\a b\x#1?.db", "immutable=1") == "file:/C:/a%20b/x%231%3F.db?immutable=1"
        share = Path(rf"\\127.0.0.1\{str(tmp_path)[0]}$" + str(tmp_path)[2:])
        if os.path.isdir(share):
            db = sqlite3.connect(tmp_path / "s.db")
            db.execute("CREATE TABLE t (x)")
            db.commit()
            db.close()
            conn = sqlite3.connect(sqlite_uri(share / "s.db", "mode=ro"), uri=True)
            assert conn.execute("SELECT COUNT(*) FROM t").fetchone() == (0,)
            conn.close()
    else:
        assert sqlite_uri("/a b/x#1?.db", "mode=ro") == "file:/a%20b/x%231%3F.db?mode=ro"
    folder = tmp_path / ("odd #%" if os.name == "nt" else "odd #?%")
    folder.mkdir()
    db = sqlite3.connect(folder / "x.db")
    db.execute("CREATE TABLE t (x)")
    db.commit()
    db.close()
    conn = sqlite3.connect(sqlite_uri(folder / "x.db", "immutable=1"), uri=True)
    assert conn.execute("SELECT name FROM sqlite_master").fetchone() == ("t",)
    conn.close()


def test_a_git_that_cannot_start_names_the_repository(tmp_path, monkeypatch):
    def broken(*args, **kw):
        raise NotADirectoryError(267, "The directory name is invalid")

    monkeypatch.setattr(transfer.subprocess, "Popen", broken)
    secrets = Secrets()
    secrets.add("abcdefgh-secret", "x")
    matcher = transfer._Matcher.of(secrets)
    with pytest.raises(TransferError, match="Nothing exported: git"):
        transfer._search_git("git", tmp_path / ".git", matcher)


async def test_an_imported_database_with_a_trigger_is_refused(tmp_path, monkeypatch):
    home_a, sid = await _one_session(tmp_path)
    _fill_db(home_a, sid)
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    db = tmp_path / "usb" / "sessions" / sid / ".dreamteam" / "swarm.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TRIGGER t AFTER INSERT ON jobs BEGIN UPDATE jobs SET log = '/x'; END")
    conn.commit()
    conn.close()
    _set_db_sha(tmp_path / "usb", sid)
    item = _item(plan_import(_manager(_home_named(tmp_path, "b", _raw_b())), tmp_path / "usb").items, "sessions",
                 sid)
    assert item.action == "refuse" and "trigger t" in item.reason


def test_a_database_with_a_wal_and_no_shm_is_read_without_writing_next_to_it(tmp_path):
    path = tmp_path / "src" / "swarm.db"
    path.parent.mkdir()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES ('in the wal')")
    conn.commit()
    shutil.copyfile(path, tmp_path / "db")
    shutil.copyfile(str(path) + "-wal", tmp_path / "wal")
    conn.close()
    shutil.copyfile(tmp_path / "db", path)   # as a crash leaves it: the database and its -wal, no -shm
    shutil.copyfile(tmp_path / "wal", str(path) + "-wal")
    for extra in ("-shm",):
        if os.path.lexists(str(path) + extra):
            os.unlink(str(path) + extra)
    before = sorted(os.listdir(path.parent))
    transfer._copy_db(path, tmp_path / "copy.db")
    assert sorted(os.listdir(path.parent)) == before
    copy = sqlite3.connect(tmp_path / "copy.db")
    assert copy.execute("SELECT x FROM t").fetchall() == [("in the wal",)]
    copy.close()


async def test_an_earlier_export_that_the_os_put_metadata_in_is_replaced(tmp_path):
    home, _sid = await _one_session(tmp_path)
    export_home(home, tmp_path / "usb", include=["settings"])
    _write(tmp_path / "usb" / ".DS_Store", b"\0\0")
    _write(tmp_path / "usb" / "Thumbs.db", b"\0")
    assert export_home(home, tmp_path / "usb", include=["settings"]).replaced
    transfer._clean_stale(tmp_path / "no-such-folder" / "x")   # (a parent that cannot be listed: nothing to do)
    assert transfer._discard(tmp_path / "no-such-folder", keep_folder=True) == []


async def test_a_plan_that_can_import_nothing_says_so(tmp_path, monkeypatch):
    home_a, sid = await _one_session(tmp_path)
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    for name in ("010-alpha", "011-alpha-copy"):
        shutil.copytree(home_a / "sessions" / sid, home_b / "sessions" / name)
    manager, plan, result = await _import(home_b, tmp_path / "usb")
    assert "Nothing can be imported: see the items refused or skipped above." in render_import_plan(plan)
    assert render_import_result(result).startswith(f"Import of {tmp_path / 'usb'} into {home_b} finished with "
                                                   "problems:")
    await manager.shutdown()


# ---- review fixes, second round -----------------------------------------------------------------------------------
async def test_credentials_of_earlier_commands_in_the_journal_leave_the_export(tmp_path):
    """The journal keeps the command a server was registered with: also of a server removed from the settings since
    (its row is gone) and of an older password (the row has the new one)."""
    old, new, gone = "oldpassw0rd-111", "newpassw0rd-222", "tok-abcdefgh-12345678"
    raw = {**_raw(), "mcp": {"external": {"pg": {"command": ["python", "-c", "pass", f"postgres://bob:{new}@db/x"]}}}}
    home = _home_named(tmp_path, "a", raw)
    manager = _manager(home)
    sid = await _session(manager, "Pg")
    await manager.shutdown()
    db = _db(home, sid)
    try:
        for seq, (name, command) in enumerate((("pg", ["python", "-c", "pass", f"postgres://bob:{old}@db/x"]),
                                               ("search", ["python", "-c", "pass", "--api-key", gone])), 1):
            server = {"name": name, "origin": "config", "command": command}
            db.execute("INSERT INTO events(ts, type, actor, data) VALUES (1, 'mcp.created', NULL, ?)",
                       (json.dumps({"server": server}),))
            db.execute("INSERT INTO history(agent, seq, msg) VALUES ('alice', ?, ?)",
                       (seq, json.dumps({"text": json.dumps(command)})))   # an mcp_list result an agent got
    finally:
        db.close()
    report = export_home(home, tmp_path / "usb", include=["sessions"])
    assert report.ok
    data = _all_bytes(tmp_path / "usb")
    for value in (old, new, gone):
        assert value.encode() not in data and value.encode("utf-16-le") not in data
    warnings = "\n".join(report.warnings)
    for name in ("pg", "search"):
        assert f"session {sid}: the credentials of MCP server {name} were removed from its history" in warnings


async def test_a_global_command_credential_is_searched_for_whatever_is_exported(tmp_path):
    token = "ghp_globaltoken_abcdef123456"
    raw = {**_raw(), "mcp": {"external": {"gh": {"command": ["npx", "gh-mcp", "--token", token]}}}}
    home = _home_named(tmp_path, "a", raw)
    manager = _manager(home)
    sid = await _session(manager, "Work")
    await manager.shutdown()
    _write(_workspace(home, sid) / "notes.md", f"the server runs with {token}\n")   # copied by an agent
    with pytest.raises(SecretFound) as err:
        export_home(home, tmp_path / "usb", include=["sessions"])
    assert any("notes.md" in p and "a credential in the command of [mcp.external.gh]" in p for p in err.value.places)


async def test_a_secret_with_quotes_or_other_letters_is_found_in_its_json_form(tmp_path, monkeypatch):
    from dreamteam.db import dumps

    secret = 'Pa"ss\\w\u00f6rd-123456'
    monkeypatch.setenv("SERVICE_PASSWORD", secret)
    home, sid = await _one_session(tmp_path)
    db = _db(home, sid)
    try:   # a lone surrogate in the same value: db.dumps escapes every non-ASCII letter then
        db.execute("INSERT INTO events(ts, type, actor, data) VALUES (1, 'message', 'user', ?)",
                   (dumps({"text": f"the password is {secret} \ud800"}),))
    finally:
        db.close()
    _write(_workspace(home, sid) / "cfg.json", json.dumps({"password": secret}))
    with pytest.raises(SecretFound) as err:
        export_home(home, tmp_path / "usb", include=["sessions"])
    places = [p for p in err.value.places if "SERVICE_PASSWORD" in p]
    assert any("swarm.db" in p for p in places) and any("cfg.json" in p for p in places)


async def test_the_plan_asks_for_the_credentials_of_every_form_the_export_removed(tmp_path, monkeypatch):
    token = "zyxwvut-987654321"
    _libraries(monkeypatch, tmp_path, "a")
    raw = {**_raw(), "mcp": {"external": {
        "hdr": {"command": ["npx", "srv", f"--header=Authorization: Bearer {token}"]},
        "url": {"command": ["npx", "mcp-remote", f"https://user:{token}@host/mcp"]},
        "query": {"command": ["npx", "mcp-remote", f"https://host/sse?api_key={token}"]}}}}
    home_a = _home_named(tmp_path, "a", raw)
    report = export_home(home_a, tmp_path / "usb", include=["mcp"])
    assert token.encode() not in _all_bytes(tmp_path / "usb")
    assert sum("re-enter the credentials of [mcp.external." in w for w in report.warnings) == 3
    _libraries(monkeypatch, tmp_path, "b")
    plan = plan_import(_manager(_home_named(tmp_path, "b", _raw_b())), tmp_path / "usb")
    for name in ("hdr", "url", "query"):
        assert any(w.startswith(f"re-enter the credentials of {name} on this computer")
                   for w in _item(plan.items, "mcp", f"external {name}").warnings), name


async def test_a_copy_added_with_its_own_identity_keeps_it_when_it_is_rewritten(tmp_path, monkeypatch):
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    sid = await _session(manager_a, "Copied")
    await manager_a.shutdown()
    _write(_workspace(home_a, sid) / "who.txt", "first\n")
    copy = f"{sid}-copy"
    shutil.copytree(home_a / "sessions" / sid, home_a / "sessions" / copy)
    export_home(home_a, tmp_path / "x1", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager = _manager(home_b)
    for name in (sid, copy):
        assert (await apply_import(manager, plan_import(manager, tmp_path / "x1", sessions=[name]))).ok
    shared, fresh = session_uid(manager.meta(sid)), session_uid(manager.meta(copy))
    assert shared != fresh and manager.meta(copy)["origin_uid"] == shared
    _write(_workspace(home_a, copy) / "who.txt", "second v2\n")
    _event(home_a, copy, 99.0)
    export_home(home_a, tmp_path / "x2", include=["sessions"])
    plan = plan_import(manager, tmp_path / "x2", sessions=[copy])
    assert [(i.action, i.target) for i in plan.items] == [("rewrite", copy)]
    assert (await apply_import(manager, plan)).ok
    assert session_uid(manager.meta(copy)) == fresh and manager.meta(copy)["origin_uid"] == shared
    assert (_workspace(home_b, copy) / "who.txt").read_text() == "second v2\n"
    manager_a = _manager(home_a)
    manager_a.update(copy, title="Copy renamed")   # only its session.json differs then
    await manager_a.shutdown()
    export_home(home_a, tmp_path / "x3", include=["sessions"])
    plan = plan_import(manager, tmp_path / "x3", sessions=[copy])
    assert plan.items[0].data.get("settings_only")
    assert (await apply_import(manager, plan)).ok
    assert session_uid(manager.meta(copy)) == fresh and manager.meta(copy)["title"] == "Copy renamed"
    export_home(home_a, tmp_path / "x4", include=["sessions"], sessions=[sid])
    assert [(i.action, i.target) for i in plan_import(manager, tmp_path / "x4").items] == [("unchanged", sid)]
    await manager.shutdown()


async def test_a_copy_with_its_own_identity_comes_back_to_the_copy_it_was_made_of(tmp_path, monkeypatch):
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    sid = await _session(manager_a, "Copied")
    await manager_a.shutdown()
    copy = f"{sid}-copy"
    shutil.copytree(home_a / "sessions" / sid, home_a / "sessions" / copy)
    export_home(home_a, tmp_path / "x1", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager_b, _plan, result = await _import(home_b, tmp_path / "x1", sessions=[sid, copy])
    assert result.ok
    fresh = session_uid(manager_b.meta(copy))
    _write(_workspace(home_b, copy) / "b.txt", "work on b\n")
    _event(home_b, copy, 50.0)
    await manager_b.shutdown()
    export_home(home_b, tmp_path / "x2", include=["sessions"])
    manager, plan, result = await _import(home_a, tmp_path / "x2", sessions=[sid, copy])
    assert {i.name: (i.action, i.target) for i in plan.items} == {sid: ("unchanged", sid), copy: ("rewrite", copy)}
    assert result.ok and sorted(manager.ids()) == [sid, copy]
    assert (_workspace(home_a, copy) / "b.txt").read_text() == "work on b\n"
    assert session_uid(manager.meta(copy)) == fresh != session_uid(manager.meta(sid))
    await manager.shutdown()


async def test_a_file_too_large_for_the_destination_leaves_only_that_session_out(tmp_path, monkeypatch):
    from dreamteam import treecopy

    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    one = await _session(manager_a, "One")
    two = await _session(manager_a, "Two")
    await manager_a.shutdown()
    _write(_workspace(home_a, one) / "big.bin", b"x" * 1000)
    _write(_workspace(home_a, two) / "fine.txt", "y\n")
    real_open, real_fdopen = treecopy.os.open, treecopy.os.fdopen
    full: set[int] = set()

    def opening(path, flags, *args, **kwargs):   # os.open is global: shutil.rmtree calls it with dir_fd on POSIX
        fd = real_open(path, flags, *args, **kwargs)
        if str(path).endswith("big.bin") and flags & os.O_CREAT:
            full.add(fd)
        return fd

    class Full:   # FAT32 on Windows: writing past 4 GiB reports a full disk, and a write names no file
        def __init__(self, handle):
            self.handle, self.fd = handle, handle.fileno()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            full.discard(self.fd)
            self.handle.close()

        def write(self, data):
            raise OSError(errno.ENOSPC, "There is not enough space on the disk")

    monkeypatch.setattr(treecopy.os, "open", opening)
    monkeypatch.setattr(treecopy.os, "fdopen", lambda fd, *a, **k: Full(real_fdopen(fd, *a, **k)) if fd in full
                        else real_fdopen(fd, *a, **k))
    monkeypatch.setattr(transfer, "FAT32_MAX", 999)   # big.bin stands for a file of 4 GiB
    report = export_home(home_a, tmp_path / "usb")
    assert not report.ok and [s["id"] for s in report.sessions] == [two]
    left = next(i for i in report.left_out if i["what"] == f"session {one}")
    assert "4 GiB or more (workspace/big.bin)" in left["reason"]
    assert (tmp_path / "usb" / "settings" / "dreamteam.toml").exists()
    monkeypatch.setattr(transfer, "FAT32_MAX", 4 << 30)   # a small file on a full disk: nothing is exported
    with pytest.raises(TransferError, match="Nothing exported: There is not enough space on the disk"):
        export_home(home_a, tmp_path / "usb2")


async def test_an_in_home_path_that_is_not_relative_never_leaves_the_home(tmp_path, monkeypatch):
    for text in ("//x/y", "/x", "C:/x", "../x", "a/../../x", "\\\\server\\x"):
        assert sessions_module._home_relative({"in_home": text}) is None, text
    assert sessions_module._home_relative({"in_home": "proj/sub"}) == "proj/sub"
    home_a, sid = await _one_session(tmp_path)
    _write(home_a / "proj" / "authorized_keys", "ssh-ed25519 AAAA\n")
    _set_meta(home_a, sid, workspace={"path": str(home_a / "proj"), "adopted": False, "in_home": "proj"})
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    victim = tmp_path / "victim" / ".ssh"
    path = tmp_path / "usb" / "sessions" / sid / "session.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta["workspace"] = {"path": "x", "in_home": "//" + victim.as_posix().split(":", 1)[-1].lstrip("/")}
    path.write_text(json.dumps(meta), encoding="utf-8")
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager, _plan, _result = await _import(home_b, tmp_path / "usb")
    assert not victim.exists() and not (tmp_path / "victim").exists()
    assert sid not in manager.ids() or "in_home" not in (manager.meta(sid).get("workspace") or {})
    await manager.shutdown()


async def test_links_in_an_export_are_never_followed(tmp_path, monkeypatch):
    home_a, sid = await _one_session(tmp_path)
    _fill_db(home_a, sid)
    export_home(home_a, tmp_path / "usb", include=["sessions", "mcp"])
    other, other_sid = await _one_session(tmp_path, name="c", title="Private")
    _fill_db(other, other_sid, "PRIVATE OF C")
    data = tmp_path / "usb" / "sessions" / sid / ".dreamteam"
    rmtree(data)
    if not _link(other / "sessions" / other_sid / ".dreamteam", data):
        pytest.skip("links cannot be made here")
    home_b = _home_named(tmp_path, "b", _raw_b())
    manager = _manager(home_b)
    item = _item(plan_import(manager, tmp_path / "usb").items, "sessions", sid)
    assert item.action == "refuse" and "its .dreamteam folder in the export is a link" in item.reason
    library = tmp_path / "usb" / "mcp" / "library"
    if library.exists():
        rmtree(library)
    (tmp_path / "elsewhere").mkdir()
    assert _link(tmp_path / "elsewhere", library)
    with pytest.raises(TransferError, match="is a link"):
        plan_import(manager, tmp_path / "usb")
    await manager.shutdown()


async def test_a_dropped_model_profile_leaves_a_reimport_unchanged(tmp_path, monkeypatch):
    overrides = {"agents": {"default_model": "pasted", "max_steps_per_turn": 5}}
    home_a, sid = await _one_session(tmp_path, overrides=overrides)
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())   # no profile 'pasted' there
    manager, plan, result = await _import(home_b, tmp_path / "usb")
    assert result.ok and manager.meta(sid)["overrides"] == {"agents": {"max_steps_per_turn": 5}}
    assert any("model profile 'pasted' does not exist here" in w for w in _item(plan.items, "sessions", sid).warnings)
    again = plan_import(manager, tmp_path / "usb")
    assert [i.action for i in again.items] == ["unchanged"] and again.exit_code == 0
    await manager.shutdown()


async def test_an_export_without_the_workspace_folder_keeps_the_files_here(tmp_path, monkeypatch):
    home_a, sid = await _one_session(tmp_path)
    _fill_db(home_a, sid)
    _write(home_a / "proj" / "important.txt", "keep me\n")
    _set_meta(home_a, sid, workspace={"path": str(home_a / "proj"), "adopted": True, "in_home": "proj"})
    export_home(home_a, tmp_path / "x1", include=["sessions"])
    home_b = _home_named(tmp_path, "b", _raw_b())
    assert (await _import(home_b, tmp_path / "x1"))[2].ok
    shutil.rmtree(home_a / "proj")   # moved away, or on a drive that is not plugged in
    report = export_home(home_a, tmp_path / "x2", include=["sessions"])
    assert any("does not exist" in w for w in report.warnings)
    manager = _manager(home_b)
    assert [i.action for i in plan_import(manager, tmp_path / "x2").items] == ["unchanged"]
    _event(home_a, sid, 9.0)
    export_home(home_a, tmp_path / "x3", include=["sessions"])
    item = _item(plan_import(manager, tmp_path / "x3").items, "sessions", sid)
    assert item.action == "refuse" and "carries no workspace files" in item.reason
    assert (home_b / "proj" / "important.txt").read_text() == "keep me\n"
    await manager.shutdown()


async def test_an_exported_database_without_its_checksum_or_with_side_files_is_refused(tmp_path, monkeypatch):
    home_a, sid = await _one_session(tmp_path)
    _fill_db(home_a, sid)
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    manager = _manager(_home_named(tmp_path, "b", _raw_b()))
    db = tmp_path / "usb" / "sessions" / sid / ".dreamteam" / "swarm.db"
    _write(db.with_name("swarm.db-wal"), b"\0" * 32)
    assert "-wal, -shm or -journal" in _item(plan_import(manager, tmp_path / "usb").items, "sessions", sid).reason
    os.unlink(db.with_name("swarm.db-wal"))
    manifest = _manifest(tmp_path / "usb")
    manifest["sessions"][0]["db_sha256"] = None
    (tmp_path / "usb" / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    assert "names no checksum" in _item(plan_import(manager, tmp_path / "usb").items, "sessions", sid).reason


def test_a_manifest_nested_too_deeply_is_refused(tmp_path):
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / MANIFEST).write_text("[" * 100000, encoding="utf-8")
    with pytest.raises(TransferError, match="cannot be read"):
        read_manifest(tmp_path / "x")


async def test_a_library_entry_keeps_the_project_it_has_here(tmp_path, monkeypatch):
    _libraries(monkeypatch, tmp_path, "a")
    home_a = _home_named(tmp_path, "a", _raw())
    _library_entry("helper", "/elsewhere/on/a")
    export_home(home_a, tmp_path / "usb", include=["mcp"])
    _libraries(monkeypatch, tmp_path, "b")
    _library_entry("helper", "/mine/on/b", approved=False)
    manager, _plan, result = await _import(_home_named(tmp_path, "b", _raw_b()), tmp_path / "usb")
    assert result.ok and mcp_library.read_entry(_library_root(), "helper").meta["project"] == "/mine/on/b"
    _libraries(monkeypatch, tmp_path, "c")   # added: the other computer's folder means nothing here
    manager, _plan, result = await _import(_home_named(tmp_path, "c", _raw_b()), tmp_path / "usb")
    assert result.ok and "project" not in mcp_library.read_entry(_library_root(), "helper").meta


def test_a_key_pasted_as_a_variable_name_or_in_a_query_extra_is_left_out(monkeypatch):
    pasted = "gsk_AbCdEfGh1234567890abcdefXYZ"
    monkeypatch.delenv(pasted, raising=False)
    raw = {"models": {"g": {"provider": "openai", "api_key_env": pasted},
                      "v": {"provider": "openai", "api_key_env": "MY_PROVIDER_KEY_2024"},
                      "q": {"provider": "openai", "extra": {"extra_query": {"key": "AIzaSyD-1234567890", "v": "1"}}}}}
    out, omitted, removed = scrub_settings(raw)
    assert "api_key_env" not in out["models"]["g"] and out["models"]["v"]["api_key_env"] == "MY_PROVIDER_KEY_2024"
    assert out["models"]["q"]["extra"] == {"extra_query": {"v": "1"}}
    assert {pasted, "AIzaSyD-1234567890"} <= set(removed.values)
    assert pasted in known_secrets(raw, environ={}).values


def test_a_long_command_change_shows_where_it_differs():
    program = "C:/Users/someone/projects/" + "deep/" * 20 + "python.exe"
    text = transfer._command_change({"command": [program, "-c", "pass", "postgres://db/x"]},
                                    {"command": [program, "-c", "pass", "postgres://db/y"]})
    assert text == "... postgres://db/x -> ... postgres://db/y"


@pytest.mark.skipif(os.name == "nt", reason="POSIX names are bytes")
def test_sqlite_uri_opens_a_folder_whose_name_is_not_utf8(tmp_path):
    from dreamteam.db import sqlite_uri

    folder = tmp_path / os.fsdecode(b"caf\xe9")
    try:
        folder.mkdir()
    except (OSError, UnicodeError):
        pytest.skip("this file system takes UTF-8 names only")
    Database(folder / "swarm.db").close()
    conn = sqlite3.connect(sqlite_uri(folder / "swarm.db", "mode=ro"), uri=True)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone() == (0,)
    conn.close()
    assert sessions_module.read_stats(folder / "swarm.db")["tasks"]["total"] == 0


# ---- review fixes, third round ------------------------------------------------------------------------------------
UUID_KEY = "3f2a9c1e-77b1-4d2a-9a51-0c8e5b1d2f44"


@pytest.mark.parametrize("command, cleaned, removed", [
    (["npx", "-y", "@smithery/cli@latest", "run", "@org/srv", "--key", UUID_KEY],
     ["npx", "-y", "@smithery/cli@latest", "run", "@org/srv", "--key", ""], [UUID_KEY]),
    (["srv", f"--key={UUID_KEY}"], ["srv", "--key="], [UUID_KEY]),
    (["uvx", "srv", '--config={"apiKey":"abcd1234efgh5678zz"}'], ["uvx", "srv", "--config={}"],
     ["abcd1234efgh5678zz"]),
    (["srv", '--headers={"Authorization": "Bearer tok-abcdefgh12345678"}'], ["srv", "--headers={}"],
     ["Bearer tok-abcdefgh12345678", "tok-abcdefgh12345678"]),
    (["srv", "--token", "-Xq9LEAKtoken0123456789"], ["srv", "--token", ""], ["-Xq9LEAKtoken0123456789"]),
    (["srv", "--pat", "ghp_abcdefghij1234567890"], ["srv", "--pat", ""], ["ghp_abcdefghij1234567890"]),
    (["srv", "--bearer", "abcdefgh12345678"], ["srv", "--bearer", ""], ["abcdefgh12345678"]),
    (["srv", "--openai-key", "sk-proj-abcdef1234567890"], ["srv", "--openai-key", ""], ["sk-proj-abcdef1234567890"]),
    (["srv", "--apiKey", "abcdef1234567890"], ["srv", "--apiKey", ""], ["abcdef1234567890"]),
    (["java", "-Dpassword=hunter2hunter2", "-jar", "x.jar"], ["java", "-Dpassword=", "-jar", "x.jar"],
     ["hunter2hunter2"]),
    (["java", "-D", "db.password=hunter2hunter2"], ["java", "-D", "db.password="], ["hunter2hunter2"]),
    (["srv", "--header", "Authorization: Bearer ${X}abcdef"], ["srv", "--header", "Authorization:"],
     ["Bearer ${X}abcdef", "${X}abcdef"]),   # more than a variable reference
    # no credentials: the command stays as it is
    (["srv", "--sort-key", "created_at"], None, []),
    (["srv", "--key-file", "/etc/x.pem"], None, []),
    (["srv", "--token", "--verbose"], None, []),
    (["server", "--mode", "Basic workflows"], None, []),
    (["server", "--mode", "token counting"], None, []),
    (["server", '{"sort_key": "created_at", "limit": 10}'], None, []),
    (["server", "--label", "sort_key:created_at"], None, []),
    (["docker", "run", "-v", "app_secrets:/run/secrets", "-v", "aws_credentials:/root/.aws:ro", "img"], None, []),
    (["docker", "build", "--secret", "id=npmrc,src=/home/me/.npmrc", "."], None, []),
    (["mcp-x", "-auth", "/etc/mcp/auth.json"], None, []),
    (["srv", "--require-auth", "/srv/data", "--credentials", "~/.config/creds.json"], None, []),
    (["srv", "-DisableAuth=true"], None, []),
    # variables the server expands itself (mcp-remote, its env carries the value): no credential either
    (["npx", "mcp-remote", "https://h/sse", "--header", "Authorization:${AUTH_HEADER}"], None, []),
    (["npx", "mcp-remote", "https://h/sse", "--header", "Authorization: Bearer ${AUTH_TOKEN}"], None, []),
    ("npx mcp-remote https://h/sse --header 'Authorization: Bearer ${AUTH_TOKEN}'", None, []),
    (["srv", "--token=${GITHUB_TOKEN}"], None, []),
    (["srv", "--api-key", "$OPENAI_API_KEY"], None, []),
    (["srv", "--api-key", "%OPENAI_API_KEY%"], None, []),
    (["docker", "run", "-e", "GITHUB_TOKEN=${GITHUB_TOKEN}", "img"], None, []),
])
def test_strip_command_credentials_of_more_forms(command, cleaned, removed):
    got, found = strip_command_credentials(command)
    assert got == (command if cleaned is None else cleaned) and sorted(set(found)) == sorted(removed)
    if cleaned is None:
        assert not transfer._blank_credentials(got)


def test_an_import_sees_the_new_blank_forms():
    for command in (["srv", "--key="], ["java", "-Dpassword="], ["java", "-D", "db.password="], ["srv", "--pat", ""]):
        assert transfer._blank_credentials(command), command


@pytest.mark.parametrize("token", ["pässwort-12345678", "пароль-12345678",
                                   "pa\\ss-word-12345", 'pa"ss-word-12345'])
async def test_a_credential_with_escaped_letters_leaves_every_form_of_the_history(tmp_path, token):
    command = ["python", "-c", "pass", "--password", token]
    home = _home_named(tmp_path, "a", {**_raw(), "mcp": {"external": {"pg": {"command": command}}}})
    manager = _manager(home)
    sid = await _session(manager, "Pg")
    await manager.shutdown()
    db = _db(home, sid)
    try:   # a tool result that showed the command as JSON text (escaped twice), and tool-call arguments (once)
        db.execute("INSERT INTO history(agent, seq, msg) VALUES ('alice', 1, ?)",
                   (json.dumps({"text": json.dumps(command)}),))
        db.execute("INSERT INTO history(agent, seq, msg) VALUES ('alice', 2, ?)",
                   (json.dumps({"args": {"command": command}}, ensure_ascii=False),))
    finally:
        db.close()
    report = export_home(home, tmp_path / "usb", include=["sessions"])
    assert report.ok
    data = _all_bytes(tmp_path / "usb")
    for form in transfer._stored_forms(token):
        assert form.encode() not in data and form.encode("utf-16-le") not in data, form
    assert f"session {sid}: the credentials of MCP server pg were removed from its history" in "\n".join(
        report.warnings)


async def test_arguments_that_are_no_credentials_stay_and_a_smithery_key_goes(tmp_path):
    servers = {
        "smithery": {"command": ["npx", "-y", "@smithery/cli@latest", "run", "@org/srv", "--key", UUID_KEY]},
        "dynamo": {"command": ["uvx", "mcp-dynamo", '{"table": "orders", "sort_key": "created_at"}']},
        "notes": {"command": ["uvx", "mcp-notes", "--profile", "Basic workflows", "-auth", "/etc/mcp/auth.json"]},
        "remote": {"command": ["npx", "mcp-remote", "https://mcp.example.com/sse", "--header",
                               "Authorization: Bearer ${AUTH_TOKEN}"], "env": {"AUTH_TOKEN": "tok-env-abcdef123456"}},
    }
    home = _home_named(tmp_path, "a", {**_raw(), "mcp": {"external": servers}})
    manager = _manager(home)
    sid = await _session(manager, "Work")
    await manager.shutdown()
    _write(_workspace(home, sid) / "README.md", "our release workflows; the key is in /etc/mcp/auth.json\n")
    _write(_workspace(home, sid) / "deploy.sh", 'curl -H "Authorization: Bearer ${AUTH_TOKEN}" https://x\n')
    report = export_home(home, tmp_path / "usb", include=["mcp", "sessions"])
    assert report.ok
    assert UUID_KEY.encode() not in _all_bytes(tmp_path / "usb")
    external = json.loads((tmp_path / "usb" / "mcp" / "external.json").read_text(encoding="utf-8"))
    for name in ("dynamo", "notes", "remote"):
        assert external[name]["command"] == servers[name]["command"], name
    warnings = "\n".join(report.warnings)
    assert "re-enter the credentials of [mcp.external.smithery] on the other computer" in warnings
    assert not any(f"[mcp.external.{name}]" in warnings for name in ("dynamo", "notes", "remote"))


async def test_history_text_that_mentions_a_command_argument_stays_as_it_is(tmp_path):
    token = "ghp_realtoken1234567890abcdef"
    command = ["uvx", "mcp-files", "--require-auth", "/srv/data", "--profile", "Basic workflows", "--token", token]
    home = _home_named(tmp_path, "a", {**_raw(), "mcp": {"external": {"files": {"command": command}}}})
    manager = _manager(home)
    sid = await _session(manager, "Files")
    await manager.shutdown()
    text = "Saved the report to /srv/data/report.csv; our release workflows are green."
    db = _db(home, sid)
    try:
        db.execute("INSERT INTO history(agent, seq, msg) VALUES ('alice', 1, ?)", (json.dumps({"text": text}),))
        db.execute("INSERT INTO history(agent, seq, msg) VALUES ('alice', 2, ?)",
                   (json.dumps({"text": json.dumps(command)}),))   # a tool result that showed the command
        db.execute("INSERT INTO events(ts, type, actor, data) VALUES (1, 'message', 'user', ?)",
                   (json.dumps({"text": "see /srv/data/report.csv"}),))
    finally:
        db.close()
    report = export_home(home, tmp_path / "usb", include=["sessions"])
    assert report.ok
    conn = sqlite3.connect(tmp_path / "usb" / "sessions" / sid / ".dreamteam" / "swarm.db")
    try:
        rows = dict(conn.execute("SELECT seq, msg FROM history WHERE agent = 'alice'").fetchall())
        message = conn.execute("SELECT data FROM events WHERE type = 'message'").fetchone()[0]
    finally:
        conn.close()
    assert json.loads(rows[1])["text"] == text and json.loads(message)["text"] == "see /srv/data/report.csv"
    assert token not in rows[2] and "/srv/data" in rows[2] and "Basic workflows" in rows[2]
    assert f"session {sid}: the credentials of MCP server files were removed from its history (the journal and the " \
           "agents' messages; history: 1 row)" in report.warnings
    db = _db(home, sid)
    try:   # a copy that is no argument of its own stays, and the scan refuses the session
        db.execute("INSERT INTO history(agent, seq, msg) VALUES ('alice', 3, ?)",
                   (json.dumps({"text": f"x{token}y"}),))
    finally:
        db.close()
    with pytest.raises(SecretFound) as err:
        export_home(home, tmp_path / "usb2", include=["sessions"])
    assert any("history.msg" in p and "[mcp.external.files]" in p for p in err.value.places), err.value.places


async def test_the_credentials_in_a_base_url_stay_on_this_computer(tmp_path, monkeypatch):
    url = "https://u:longpassword1@gw.example/v1"
    _libraries(monkeypatch, tmp_path, "a")
    raw = _raw(gw={"provider": "openai", "model": "m", "base_url": url, "api_key": "sk-gateway-key-a1"})
    report = export_home(_home_named(tmp_path, "a", raw), tmp_path / "usb", include=["settings"])
    assert report.ok and b"longpassword1" not in _all_bytes(tmp_path / "usb")
    assert "model profile 'gw': re-enter the credentials in its base_url on the other computer (they were removed " \
           "from it)" in report.warnings
    assert "longpassword1" in known_secrets(raw, environ={}).values
    # a computer that has the same gateway keeps its URL with the credentials, and its key
    _libraries(monkeypatch, tmp_path, "b")
    raw_b = _raw_b()
    raw_b["models"]["gw"] = {"provider": "openai", "model": "m", "base_url": url, "api_key": "sk-gateway-key-b1"}
    home_b = _home_named(tmp_path, "b", raw_b)
    manager, plan, result = await _import(home_b, tmp_path / "usb")
    assert result.ok and not any("base_url" in w for w in _item(plan.items, "settings", "dreamteam.toml").warnings)
    await manager.shutdown()
    assert st.read_raw(home_b)["models"]["gw"] == raw_b["models"]["gw"]
    exported = {"models": {"gw": {"provider": "openai", "model": "m2", "base_url": "https://gw.example/v1"}}}
    m = merge_settings(raw_b, exported)
    assert m.new_raw["models"]["gw"] == {**raw_b["models"]["gw"], "model": "m2"}
    # a computer without it is asked for them
    _libraries(monkeypatch, tmp_path, "c")
    plan = plan_import(_manager(_home_named(tmp_path, "c", _raw_b())), tmp_path / "usb")
    assert "re-enter the credentials in the base_url of model profile 'gw' on this computer: the export carries it " \
           "without them" in _item(plan.items, "settings", "dreamteam.toml").warnings


@pytest.mark.parametrize("relative", [True, False])
async def test_a_swap_journal_after_the_home_was_moved(tmp_path, relative):
    home, sid = await _one_session(tmp_path)
    _write(_workspace(home, sid) / "work.txt", "newer local work\n")
    manager = _manager(home)
    backup = manager.sessions_dir / f".replaced-{sid}-20261105-120000"
    backup.mkdir()
    planned = [(manager.dir_of(sid), backup / "session"), (manager.sessions_dir / ".importing-x", manager.dir_of(sid))]
    os.rename(manager.dir_of(sid), backup / "session")   # the process died after the first rename
    # (False: the absolute paths a journal of an older version holds)
    sessions_module._write_swap_journal(backup / sessions_module.SWAP_JOURNAL, sid, planned, planned[:1], [backup],
                                        manager.home if relative else tmp_path / "elsewhere")
    await manager.shutdown()
    moved = tmp_path / "moved"
    os.rename(home, moved)
    manager = _manager(moved)
    messages = manager.recover_swaps()
    if relative:
        assert messages == [f"the import of session {sid} that stopped halfway was undone: the session is as it was "
                            "before it"]
        assert sid in manager.ids() and (_workspace(moved, sid) / "work.txt").exists()
        assert manager.interrupted_swaps() == []
    else:   # the journal names places that are gone: reported, and the journal stays
        assert len(messages) == 1 and "could not be undone completely" in messages[0] and "neither is there" in \
            messages[0]
        assert sid not in manager.ids() and len(manager.interrupted_swaps()) == 1
    await manager.shutdown()


async def test_a_swap_journal_of_a_live_process_or_with_its_place_taken_stays(tmp_path):
    home, sid = await _one_session(tmp_path)
    manager = _manager(home)
    backup = manager.sessions_dir / f".replaced-{sid}-20261105-120000"
    backup.mkdir()
    staged = manager.sessions_dir / ".importing-x"
    planned = [(manager.dir_of(sid), backup / "session"), (staged / "workspace", home / "proj"),
               (staged, manager.dir_of(sid))]
    os.rename(manager.dir_of(sid), backup / "session")
    journal = backup / sessions_module.SWAP_JOURNAL
    sessions_module._write_swap_journal(journal, sid, planned, planned[:1], [backup], manager.home)
    data = json.loads(journal.read_text(encoding="utf-8"))
    journal.write_text(json.dumps({**data, "pid": os.getppid()}), encoding="utf-8")   # a process that runs
    messages = manager.recover_swaps()
    assert len(messages) == 1 and "has not finished" in messages[0] and journal.exists()
    journal.write_text(json.dumps({**data, "pid": 0}), encoding="utf-8")
    manager.dir_of(sid).mkdir()   # the session's place was taken meanwhile
    messages = manager.recover_swaps()
    assert len(messages) == 1 and "which exists now" in messages[0] and journal.exists()
    assert manager.interrupted_swaps() == [str(backup)]
    await manager.shutdown()


async def test_the_plan_asks_for_the_credentials_a_session_override_lost(tmp_path, monkeypatch):
    _libraries(monkeypatch, tmp_path, "a")
    home_a, sid = await _one_session(tmp_path, overrides={"mcp": {"external": {
        "r": {"command": ["npx", "mcp-remote", "https://user:tok-abcdef123456@host/mcp"]},
        "q": {"command": ["npx", "mcp-remote", "https://host/sse?api_key=abcdef123456"]}}}})
    export_home(home_a, tmp_path / "usb", include=["sessions"])
    assert _manifest(tmp_path / "usb")["sessions"][0]["credentials_removed"] == ["q", "r"]
    _libraries(monkeypatch, tmp_path, "b")
    plan = plan_import(_manager(_home_named(tmp_path, "b", _raw_b())), tmp_path / "usb")
    warnings = _item(plan.items, "sessions", sid).warnings
    for name in ("q", "r"):
        assert sum(w.startswith(f"re-enter the credentials of {name} on this computer") for w in warnings) == 1, \
            warnings


async def test_a_library_entry_nested_too_deeply_is_refused(tmp_path, monkeypatch):
    _libraries(monkeypatch, tmp_path, "a")
    _library_entry("helper", None)
    export_home(_home_named(tmp_path, "a", _raw()), tmp_path / "usb", include=["mcp"])
    (tmp_path / "usb" / "mcp" / "library" / "helper" / "meta.json").write_text("[" * 20000, encoding="utf-8")
    _libraries(monkeypatch, tmp_path, "b")
    plan = plan_import(_manager(_home_named(tmp_path, "b", _raw_b())), tmp_path / "usb")
    item = _item(plan.items, "mcp", "library helper")
    assert item.action == "refuse" and "is not valid JSON" in item.reason


def test_a_key_variable_named_in_lower_case_is_no_pasted_key(tmp_path, monkeypatch):
    name = "openrouter_key_work2025"
    monkeypatch.setenv(name, "or-value-123456789")
    raw = _raw(router={"provider": "openai", "model": "m", "api_key_env": name})
    report = export_home(_home_named(tmp_path, "a", raw), tmp_path / "usb", include=["settings"])
    assert report.ok and name in (tmp_path / "usb" / "settings" / "dreamteam.toml").read_text(encoding="utf-8")


def test_a_manifest_with_a_value_of_another_type_is_refused(tmp_path):
    (tmp_path / "x").mkdir()
    for key, value in (("sessions", 5), ("include", True), ("mcp", {"external": "x"}), ("settings", [1])):
        (tmp_path / "x" / MANIFEST).write_text(json.dumps({"format": "dreamteam-export", "format_version": 1,
                                                           key: value}), encoding="utf-8")
        with pytest.raises(TransferError, match="is damaged"):
            read_manifest(tmp_path / "x")
    assert sessions_module._home_relative({"in_home": "proj\x00x"}) is None


async def test_an_import_stops_when_another_process_takes_the_home(tmp_path, monkeypatch):
    _libraries(monkeypatch, tmp_path, "a")
    _library_entry("helper", None)
    home_a = _home_named(tmp_path, "a", _raw())
    manager_a = _manager(home_a)
    one, two = await _session(manager_a, "One"), await _session(manager_a, "Two")
    await manager_a.shutdown()
    export_home(home_a, tmp_path / "usb")
    _libraries(monkeypatch, tmp_path, "b")

    def taken(sid: str) -> None:
        raise transfer.HomeTaken("another DreamTeam process started meanwhile")

    manager, _plan, result = await _import(_home_named(tmp_path, "b", _raw_b()), tmp_path / "usb", before_swap=taken)
    statuses = _statuses(result)
    assert statuses[("sessions", one)] == "failed" and statuses[("sessions", two)] == "not done"
    assert statuses[("mcp", "library helper")] == "not done"
    assert not result.interrupted and result.exit_code == 1 and not (_library_root() / "helper").exists()
    await manager.shutdown()
