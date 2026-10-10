"""Global settings (dreamteam.settings) and the session manager (dreamteam.sessions)."""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import socket
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from dreamteam import settings as st
from dreamteam.config import Config, render_template
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.sessions import LEGACY_ID, TRASH_PREFIX, SessionError, SessionManager, read_stats, slugify


def _raw(tmp_path: Path) -> dict[str, Any]:
    return {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 2.0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 0.2},
        "mcp": {"library_dir": str(tmp_path / "library"), "external": {"fetch": {"command": ["uvx", "f"],
                                                                                 "env": {"A": "1"}}}},
        "models": {
            "default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0,
                        "api_key": "sk-secret-123456"},
            "fast": {"provider": "openai", "model": "m", "base_url": "http://127.0.0.1:9/v1",
                     "api_key_env": "DT_TEST_KEY_ENV"},
        },
    }


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, _raw(tmp_path))
    return home


def _manager(home: Path) -> SessionManager:
    def factory(profile, agent):
        return ScriptedLLM(lambda system, messages, tools: "Nothing to do right now.", profile)

    return SessionManager(home, llm_factory=factory)


# ---- settings -----------------------------------------------------------------------------------------
def test_dump_toml_roundtrip(tmp_path):
    raw = _raw(tmp_path)
    raw["swarm"]["starting_roles"] = ["Аналитик", 'He said "hi"\\']
    text = st.dump_toml(raw)
    assert tomllib.loads(text) == raw
    assert "[models.default]" in text and "[mcp.external.fetch]" in text
    Config.from_dict(tmp_path, tomllib.loads(text))


def test_ensure_home_creates_template(tmp_path):
    home = st.ensure_home(tmp_path / "h")
    assert (home / "sessions").is_dir()
    cfg = Config.load(home)
    assert cfg.models["default"].provider == "anthropic" and cfg.tracing.enabled


def test_public_view_hides_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("DT_TEST_KEY_ENV", "x")
    view = st.public_view(_raw(tmp_path))
    default = view["models"]["default"]
    assert default["api_key"] is None and default["api_key_set"] and default["api_key_hint"] == "…3456"
    assert "sk-secret" not in str(view)
    assert view["models"]["fast"]["api_key_env_set"] is True and view["models"]["fast"]["api_key_set"] is False
    assert view["tracing"]["enabled"] is True and "goal" not in view["swarm"]
    monkeypatch.delenv("DT_TEST_KEY_ENV")
    assert st.public_view(_raw(tmp_path))["models"]["fast"]["api_key_env_set"] is False


def test_apply_update_key_semantics(tmp_path):
    raw = _raw(tmp_path)
    view = st.public_view(raw)
    view["swarm"]["max_agents"] = 7
    # keep the stored key (api_key null), rename "fast" to "cheap" with a new key, drop nothing else
    cheap = dict(view["models"].pop("fast"), renamed_from="fast", api_key="sk-new-key-0000")
    view["models"]["cheap"] = cheap
    new = st.apply_update(raw, view)
    assert new["swarm"]["max_agents"] == 7
    assert new["models"]["default"]["api_key"] == "sk-secret-123456"
    assert new["models"]["cheap"]["api_key"] == "sk-new-key-0000" and "fast" not in new["models"]
    assert "api_key_set" not in new["models"]["default"]
    Config.from_dict(tmp_path, new)
    # "" clears the key
    view = st.public_view(new)
    view["models"]["default"]["api_key"] = ""
    assert "api_key" not in st.apply_update(new, {"models": view["models"]})["models"]["default"]
    with pytest.raises(ValueError):
        st.apply_update(raw, {"models": {}})
    with pytest.raises(ValueError):
        st.apply_update(raw, {"bogus": {}})
    with pytest.raises(ValueError):
        st.apply_update(raw, {"models": {"bad name": {"provider": "openai"}}})


def test_write_raw_validates_and_backs_up(tmp_path):
    home = _home(tmp_path)
    bad = _raw(tmp_path)
    bad["agents"]["default_model"] = "missing"
    with pytest.raises(ValueError):
        st.write_raw(home, bad)
    assert st.read_raw(home)["agents"].get("default_model") is None
    good = _raw(tmp_path)
    good["swarm"]["max_agents"] = 5
    st.write_raw(home, good)
    assert st.read_raw(home)["swarm"]["max_agents"] == 5
    assert (home / "dreamteam.toml.bak").exists()


def test_profile_from_view_uses_stored_key(tmp_path):
    raw = _raw(tmp_path)
    profile = st.profile_from_view(raw, {"provider": "openai", "model": "x", "api_key": None,
                                         "api_key_set": True}, name="default")
    assert profile.api_key == "sk-secret-123456" and profile.name == "default"
    with pytest.raises(ValueError):
        st.profile_from_view(raw, {"provider": "openai", "nonsense": 1})


async def test_check_profile_reports_unreachable_endpoint(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    profile = st.profile_from_view({}, {"provider": "openai", "model": "m", "base_url": f"http://127.0.0.1:{port}/v1",
                                        "api_key": "k"})
    result = await st.check_profile(profile, timeout=3)
    assert result["ok"] is False and result["models_error"]
    with pytest.raises(ValueError):
        await st.list_models(st.profile_from_view({}, {"provider": "scripted", "model": "m"}))


def test_slugify():
    assert slugify("Змейка на Python!") == "zmeyka-na-python"
    assert slugify("   ") == ""


# ---- sessions -----------------------------------------------------------------------------------------
async def test_session_lifecycle(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    events: list[dict] = []
    manager.listen(events.append)
    assert manager.list() == []

    summary = await manager.create("Змейка", "Write a snake game", "In Python",
                                   overrides={"swarm": {"max_cost_usd": 1.5, "start_agents": 2, "goal": "x"}})
    sid = summary["id"]
    assert sid == "001-zmeyka" and summary["status"] == "running"
    assert summary["max_cost_usd"] == 1.5 and summary["agents"]["total"] == 2
    assert summary["goal"]["title"] == "Write a snake game" and summary["tasks"]["total"] >= 1
    assert (home / "sessions" / sid / ".dreamteam" / "swarm.db").exists()
    assert (home / "sessions" / sid / "workspace").is_dir()
    swarm = manager.swarms[sid]
    assert swarm.session_id == sid and swarm.config.swarm.max_cost_usd == 1.5
    assert "goal" not in manager.meta(sid)["overrides"]["swarm"]

    second = await manager.create("Second", start=False)
    assert second["id"] == "002-second" and second["status"] == "stopped" and not second["loaded"]
    assert [s["id"] for s in manager.list()] == [sid, "002-second"]  # running first
    assert manager.default_session() == sid

    await manager.stop(sid)
    stopped = manager.summary(sid)
    assert stopped["status"] == "stopped" and not stopped["loaded"] and not stopped["autostart"]
    assert stopped["goal"]["title"] == "Write a snake game" and stopped["agents"]["total"] == 2

    await manager.start(sid)
    await manager.shutdown()  # server stop keeps the autostart mark
    assert manager.meta(sid)["autostart"] is True

    again = _manager(home)
    assert await again.resume() == [sid]
    assert again.summary(sid)["status"] == "running"

    renamed = again.update(sid, title="Snake")
    assert renamed["title"] == "Snake" and again.swarms[sid].session_title == "Snake"
    with pytest.raises(SessionError):
        again.update(sid, overrides={"server": {"port": 1}})
    with pytest.raises(SessionError):
        again.update(sid, overrides={"agents": {"default_model": "missing"}})

    viewer = await again.load("002-second")
    assert viewer is again.swarms["002-second"] and "002-second" not in again.running
    await again.delete("002-second")
    assert not (home / "sessions" / "002-second").exists() and not again.exists("002-second")
    with pytest.raises(SessionError) as err:
        again.summary("002-second")
    assert err.value.status == 404
    with pytest.raises(SessionError):
        await again.load("../etc")
    await again.shutdown()
    assert {e["change"] for e in events} >= {"created", "started", "stopped"}


async def test_pending_goal_set_at_first_start(tmp_path):
    manager = _manager(_home(tmp_path))
    summary = await manager.create("Later", "Do it later", start=False)
    assert summary["goal"]["status"] == "pending"
    swarm = await manager.start(summary["id"])
    assert swarm.goal()["title"] == "Do it later"
    assert "pending_goal" not in manager.meta(summary["id"])
    await manager.shutdown()


async def test_legacy_project_is_main_session(tmp_path):
    from dreamteam.db import Database

    home = _home(tmp_path)
    Database(home / ".dreamteam" / "swarm.db").close()
    manager = _manager(home)
    assert manager.ids() == [LEGACY_ID]
    summary = manager.summary(LEGACY_ID)
    assert summary["legacy"] and summary["title"] == "Home" and summary["dir"] == str(home.resolve())
    swarm = await manager.start(LEGACY_ID)
    assert swarm.config.project_dir == home.resolve()
    with pytest.raises(SessionError):
        await manager.delete(LEGACY_ID)
    with pytest.raises(SessionError):
        manager.update(LEGACY_ID, overrides={"swarm": {"max_agents": 2}})
    manager.update(LEGACY_ID, title="Old project")
    assert manager.summary(LEGACY_ID)["title"] == "Old project"
    await manager.shutdown()


async def test_update_settings_reconfigures_loaded_sessions(tmp_path):
    manager = _manager(_home(tmp_path))
    summary = await manager.create("A", start=True)
    calls: list[Config] = []
    manager.swarms[summary["id"]].reconfigure = calls.append
    view = manager.settings_view()
    view["agents"]["max_steps_per_turn"] = 11
    result = await manager.update_settings({"agents": view["agents"], "models": view["models"]})
    assert result["applied_to"] == [summary["id"]] and calls[0].agents.max_steps_per_turn == 11
    assert st.read_raw(manager.home)["models"]["default"]["api_key"] == "sk-secret-123456"
    with pytest.raises(ValueError):
        await manager.update_settings({"agents": {"default_model": "nope"}})
    assert manager.config.agents.max_steps_per_turn == 11
    await manager.shutdown()


# ---- regressions (adversarial review) ----------------------------------------------------------------
BS = "\\"


def test_dump_toml_tricky_values(tmp_path):
    raw = _raw(tmp_path)
    raw["swarm"]["name"] = 'Q"uote' + BS + " tab\t nl\n del\x7f nul\x00 ёжик 🚀  "
    raw["mcp"]["library_dir"] = "C:" + BS + "Users" + BS + "кузьми" + BS + "lib"
    raw["mcp"]["external"]["my.srv"] = {"command": ["C:" + BS + "x y" + BS + "a.exe"], "env": {"": "e", "K-1": "v"},
                                        "since": datetime.date(2024, 1, 2)}  # a date from a hand-edited file
    raw["models"]["default"]["extra"] = {"nested": {"deep": {"x": 1.5e-7}}, "list": [{"a": 1}, {"b": [True]}],
                                         'we"ird.key': 2 ** 62}
    raw["tracing"] = {}
    text = st.dump_toml(raw)
    assert tomllib.loads(text) == raw
    for bad in ("a\ud800", 2 ** 64, float("nan")):  # cannot be stored: an error, never a broken file
        with pytest.raises(ValueError):
            st.dump_toml({"swarm": {"name": bad}})


def test_write_raw_never_writes_invalid_settings(tmp_path):
    home = _home(tmp_path)
    before = (home / "dreamteam.toml").read_bytes()
    bad_values = [
        ("swarm", "max_cost_usd", "5"), ("swarm", "max_agents", 0), ("swarm", "max_agents", True),
        ("swarm", "starting_roles", "Analyst"), ("agents", "max_steps_per_turn", 2.5),
        ("server", "port", 70000), ("server", "resume_sessions", "no"), ("workspace", "git", "yes"),
        ("tracing", "retention_days", -1), ("tracing", "max_payload_chars", "big"),
        ("swarm", "name", "bad\ud800"), ("swarm", "max_cost_usd", float("inf")), ("swarm", "language", "de"),
    ]
    for section, key, value in bad_values:
        raw = _raw(tmp_path)
        raw.setdefault(section, {})[key] = value
        with pytest.raises(ValueError):
            st.write_raw(home, raw)
    raw = _raw(tmp_path)
    raw["models"]["fast"]["max_tokens"] = "big"
    with pytest.raises(ValueError, match=r"models\.fast"):
        st.write_raw(home, raw)
    raw = _raw(tmp_path)
    raw["models"]["fast"]["api_key"] = 123456789012345
    with pytest.raises(ValueError) as err:
        st.write_raw(home, raw)
    assert "123456789012345" not in str(err.value)  # a key is never echoed
    raw = _raw(tmp_path)
    raw["mcp"]["external"]["fetch"] = "uvx fetch"
    with pytest.raises(ValueError):
        st.write_raw(home, raw)
    assert (home / "dreamteam.toml").read_bytes() == before
    assert not list(home.glob("*.tmp"))
    raw = _raw(tmp_path)
    raw["agents"]["max_steps_per_turn"] = 12.0  # a whole number written as a float by hand
    assert st.validate(home, raw).agents.max_steps_per_turn == 12


def test_config_normalizes_api_key_env(tmp_path, monkeypatch):
    from dreamteam.shell import scrubbed_env

    monkeypatch.setenv("DT_MY_LLM_SECRET_VAR", "value-1")
    cfg = Config.from_dict(tmp_path, {"models": {
        "default": {"provider": "openai", "model": "m", "api_key_env": "%DT_MY_LLM_SECRET_VAR%"},
        "pasted": {"provider": "openai", "model": "m", "api_key_env": "sk-pasted-in-a-hand-edit-42"},
        "both": {"provider": "openai", "model": "m", "api_key": "sk-real", "api_key_env": "sk-other value"},
    }})
    default, pasted, both = cfg.models["default"], cfg.models["pasted"], cfg.models["both"]
    assert default.api_key_env == "DT_MY_LLM_SECRET_VAR" and default.resolve_api_key() == "value-1"
    assert "DT_MY_LLM_SECRET_VAR" not in scrubbed_env(cfg)  # agents' shells never get the key
    assert pasted.api_key_env is None and pasted.resolve_api_key() == "sk-pasted-in-a-hand-edit-42"
    assert both.api_key == "sk-real" and both.api_key_env is None
    view = st.public_view({"models": {"default": {"provider": "openai", "model": "m",
                                                  "api_key_env": "sk-pasted-in-a-hand-edit-42"}}})
    assert "sk-pasted" not in json.dumps(view) and view["models"]["default"]["api_key_set"]


def test_render_template_escapes_name_and_goal():
    name = 'My "team" ' + BS + " x"
    raw = tomllib.loads(render_template(name, "en", "openai", goal='line1\nline2 "q"'))
    assert raw["swarm"]["name"] == name and raw["swarm"]["goal"] == 'line1\nline2 "q"'


def test_public_view_masks_mcp_env_and_keys_in_the_env_field(tmp_path):
    raw = _raw(tmp_path)
    raw["mcp"]["external"]["gh"] = {"command": ["gh-mcp"], "env": {"GITHUB_TOKEN": "ghp_secret_value_123456"}}
    raw["models"]["fast"]["api_key_env"] = "sk-pasted-into-env-field-9876"
    view = st.public_view(raw)
    text = json.dumps(view, ensure_ascii=False)
    assert "ghp_secret" not in text and "sk-pasted" not in text and "sk-secret" not in text
    assert view["mcp"]["external"]["gh"]["env"]["GITHUB_TOKEN"] == st.SECRET_MASK + "…3456"
    fast = view["models"]["fast"]
    assert fast["api_key_env"] is None and fast["api_key_set"] and fast["api_key_hint"] == "…9876"
    # the view sent back unchanged keeps every secret
    new = st.apply_update(raw, view)
    assert new["mcp"]["external"]["gh"]["env"]["GITHUB_TOKEN"] == "ghp_secret_value_123456"
    assert new["mcp"]["external"]["fetch"]["env"] == {"A": "1"}
    assert new["models"]["fast"]["api_key"] == "sk-pasted-into-env-field-9876"
    assert "api_key_env" not in new["models"]["fast"]
    Config.from_dict(tmp_path, new)
    # a server renamed in the JSON finds its value, a changed value replaces it, an unknown mask is an error
    view["mcp"]["external"]["github"] = view["mcp"]["external"].pop("gh")
    view["mcp"]["external"]["fetch"]["env"]["A"] = "2"
    new = st.apply_update(raw, {"mcp": view["mcp"]})
    assert new["mcp"]["external"]["github"]["env"]["GITHUB_TOKEN"] == "ghp_secret_value_123456"
    assert new["mcp"]["external"]["fetch"]["env"]["A"] == "2"
    view["mcp"]["external"]["github"]["env"]["OTHER"] = st.SECRET_MASK + "…zzzz"
    with pytest.raises(ValueError):
        st.apply_update(raw, {"mcp": view["mcp"]})


def test_apply_update_env_names_keys_and_renames(tmp_path):
    raw = _raw(tmp_path)
    for given, stored in (("$OPENAI_API_KEY", "OPENAI_API_KEY"), ("%MY_KEY%", "MY_KEY"), ("${X_1}", "X_1")):
        view = st.public_view(raw)
        view["models"]["fast"]["api_key_env"] = given
        assert st.apply_update(raw, {"models": view["models"]})["models"]["fast"]["api_key_env"] == stored
    view = st.public_view(raw)
    view["models"]["fast"].update(api_key_env="sk-typed-in-env-0001", api_key="sk-explicit-0002")
    fast = st.apply_update(raw, {"models": view["models"]})["models"]["fast"]
    assert fast["api_key"] == "sk-explicit-0002" and "api_key_env" not in fast
    # renaming the default profile keeps it the default, even when [agents] still names the old one
    view = st.public_view(raw)
    view["models"]["main"] = dict(view["models"].pop("default"), renamed_from="default")
    new = st.apply_update(raw, {"models": view["models"], "agents": view["agents"]})
    assert new["agents"]["default_model"] == "main" and new["models"]["main"]["api_key"] == "sk-secret-123456"
    Config.from_dict(tmp_path, new)
    view = st.public_view(raw)  # model: a string is kept (even "": the server's default), None is not stored
    view["models"]["fast"]["model"] = None
    assert "model" not in st.apply_update(raw, {"models": view["models"]})["models"]["fast"]
    with pytest.raises(ValueError):
        st.apply_update(raw, ["not", "an", "object"])


async def test_check_profile_never_returns_the_key():
    key = "sk-echoed-secret-ABCDEFGHIJ"
    bodies: list[bytes] = []

    class Echo(BaseHTTPRequestHandler):  # an endpoint that echoes the rejected key in its error
        def log_message(self, *args):
            pass

        def _reply(self):
            if self.command == "POST":
                bodies.append(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            auth = self.headers.get("Authorization") or self.headers.get("x-api-key") or ""
            body = json.dumps({"error": {"message": f"bad key {auth}", "type": "invalid_request_error"}}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _reply

    server = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        for provider, base in (("openai", f"http://127.0.0.1:{port}/v1"), ("anthropic", f"http://127.0.0.1:{port}")):
            profile = st.profile_from_view({}, {"provider": provider, "model": "m", "base_url": base, "api_key": key})
            result = await st.check_profile(profile, live=True, timeout=5)
            assert result["ok"] is False and result["error"] and key not in json.dumps(result)
            with pytest.raises(ValueError) as err:
                await st.list_models(profile, timeout=5)
            assert key not in str(err.value) and "…GHIJ" in str(err.value)
        # a profile without effort is probed without it (some endpoints reject the parameter)
        assert bodies and all(b"reasoning_effort" not in b and b"effort" not in b for b in bodies)
    finally:
        server.shutdown()
        server.server_close()
    with pytest.raises(ValueError, match="max_tokens"):
        st.profile_from_view({}, {"provider": "openai", "model": "m", "max_tokens": "big"})


def test_read_stats_of_odd_databases(tmp_path):
    from dreamteam.db import Database

    path = tmp_path / "папка с пробелом #1 %20" / "swarm.db"
    db = Database(path)
    db.set("paused", True)  # not the expected objects
    db.set("limits", [1])
    db.execute("INSERT INTO agents (name, kind, status, role, cost_usd) VALUES ('A', 'llm', 'working', 'r', 1.5)")
    db.close()
    stats = read_stats(path)
    assert stats["paused"] is False and stats["limits"] == {}
    assert stats["agents"]["working"] == 1 and stats["spent_usd"] == 1.5
    (tmp_path / "junk.db").write_bytes(b"not a database" * 50)
    assert read_stats(tmp_path / "junk.db")["tasks"]["total"] == 0
    assert read_stats(tmp_path / "missing.db")["agents"]["total"] == 0


async def test_lifecycle_races_with_delete(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    sid = (await manager.create("Race", start=True))["id"]
    # a viewer's load, a start, a stop and a second delete arrive while the delete holds the session lock:
    # none of them may recreate the directory or keep a swarm of the deleted session
    results = await asyncio.gather(manager.delete(sid), manager.load(sid), manager.start(sid), manager.stop(sid),
                                   manager.delete(sid), return_exceptions=True)
    assert results[0] is None
    assert all(isinstance(r, SessionError) and r.status == 404 for r in results[1:]), results
    assert not manager.swarms and not manager.running and list((home / "sessions").iterdir()) == []
    # stop and start racing on a running session end in a consistent state
    sid = (await manager.create("Again", start=True))["id"]
    await asyncio.gather(manager.stop(sid), manager.start(sid))
    assert sid in manager.running and manager.swarms[sid]._started
    await manager.shutdown()


async def test_damaged_sessions_do_not_break_the_list_or_the_settings(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    a, b, c = [(await manager.create(t, start=False))["id"] for t in ("A", "B", "C")]
    (home / "sessions" / a / "session.json").write_text("[1, 2]", encoding="utf-8")
    (home / "sessions" / b / "session.json").write_text('{"title": "B", "created_at": "yesterday"}', encoding="utf-8")
    meta = json.loads((home / "sessions" / c / "session.json").read_text(encoding="utf-8"))
    meta["overrides"] = {"swarm": {"max_agents": "many"}}  # written by an older version without type checks
    (home / "sessions" / c / "session.json").write_text(json.dumps(meta), encoding="utf-8")
    items = {s["id"]: s for s in manager.list()}
    assert "error" in items[a] and items[b]["title"] == "B" and "max_agents" in items[c]["config_error"]
    with pytest.raises(SessionError) as err:
        await manager.load(c)
    assert err.value.status == 400
    view = manager.settings_view()
    view["agents"]["max_steps_per_turn"] = 9
    await manager.update_settings({"agents": view["agents"]})  # neither one blocks the global settings
    assert st.read_raw(home)["agents"]["max_steps_per_turn"] == 9
    with pytest.raises(SessionError) as err:
        manager.meta("nope")
    assert err.value.status == 404


async def test_bad_session_inputs_are_400(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    for overrides in (["x"], "x", {"swarm": {"max_agents": "abc"}}, {"swarm": "x"},
                      {"agents": {"default_model": "nope"}}):
        with pytest.raises(SessionError) as err:
            await manager.create("X", "goal", overrides=overrides, start=False)
        assert err.value.status == 400
    assert list((home / "sessions").iterdir()) == []  # nothing half-created
    sid = (await manager.create("Y", start=False))["id"]
    for kwargs in ({"title": 5}, {"title": "   "}, {"overrides": "x"}, {"overrides": {"server": {"port": 1}}}):
        with pytest.raises(SessionError) as err:
            manager.update(sid, **kwargs)
        assert err.value.status == 400
    for bad in ("../x", sid + "\n", sid.upper(), "", None, "main"):
        assert not manager.exists(bad)
    with pytest.raises(SessionError) as err:
        await manager.load(sid + "\n")
    assert err.value.status == 404


async def test_titles_ids_and_id_collisions(tmp_path):
    manager = _manager(_home(tmp_path))
    s = await manager.create("Кафе «Ёлка» café ß — part\ud8001" + "x" * 300, start=False)
    assert s["id"].startswith("001-kafe-elka-cafe-ss-part") and len(s["id"]) <= 64
    assert len(s["title"]) <= 200 and "\ud800" not in s["title"]
    json.loads((manager.dir_of(s["id"]) / "session.json").read_text(encoding="utf-8"))
    real, calls = manager._new_id, []

    def racing(title):  # another process creates the chosen directory first
        calls.append(title)
        if len(calls) == 1:
            (manager.sessions_dir / "002-twin").mkdir()
            return "002-twin"
        return real(title)

    manager._new_id = racing
    assert (await manager.create("Twin", start=False))["id"] == "003-twin" and len(calls) == 2


async def test_home_goal_is_not_inherited_by_sessions(tmp_path):
    home = _home(tmp_path)
    raw = st.read_raw(home)
    raw["swarm"]["goal"] = "Home project goal"  # e.g. a project home made by `dreamteam init --goal`
    st.write_raw(home, raw)
    manager = _manager(home)
    summary = await manager.create("Own", "Session goal", start=True)
    assert summary["goal"]["title"] == "Session goal" and manager.config_for(summary["id"]).swarm.goal == ""
    await manager.shutdown()


async def test_profile_renames_and_deletions_follow_into_sessions(tmp_path):
    manager = _manager(_home(tmp_path))
    overrides = {"agents": {"default_model": "fast", "max_steps_per_turn": 5}}
    pinned = (await manager.create("Pinned", start=False, overrides=overrides))["id"]
    other = (await manager.create("Other", start=False, overrides={"agents": {"default_model": "fast"}}))["id"]
    events: list[dict] = []
    manager.listen(events.append)
    view = manager.settings_view()
    view["models"]["cheap"] = dict(view["models"].pop("fast"), renamed_from="fast")
    await manager.update_settings({"models": view["models"]})
    assert manager.meta(pinned)["overrides"]["agents"] == {"default_model": "cheap", "max_steps_per_turn": 5}
    view = manager.settings_view()
    view["models"].pop("cheap")  # deleting a profile a session uses no longer blocks the save
    await manager.update_settings({"models": view["models"]})
    assert manager.meta(pinned)["overrides"]["agents"] == {"max_steps_per_turn": 5}
    assert manager.meta(other)["overrides"] == {}
    summary = manager.summary(other)
    assert summary["model_profile"] == "default" and summary["config_error"] is None
    assert {(e["session"], e["change"]) for e in events} >= {(pinned, "updated"), (other, "updated"),
                                                             (None, "settings")}


async def test_update_overrides_apply_to_the_loaded_swarm(tmp_path):
    manager = _manager(_home(tmp_path))
    sid = (await manager.create("Live", start=True))["id"]
    swarm = manager.swarms[sid]
    manager.update(sid, overrides={"swarm": {"max_cost_usd": 7.5}, "tracing": {"enabled": False}})
    assert swarm.config.swarm.max_cost_usd == 7.5 and swarm.tracer.enabled is False
    assert swarm.config.project_dir == swarm.project_dir and manager.summary(sid)["max_cost_usd"] == 7.5
    await manager.shutdown()


@pytest.mark.skipif(os.name != "nt", reason="an open file blocks renaming its directory only on Windows")
async def test_delete_of_a_session_in_use_keeps_it_intact(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    sid = (await manager.create("Busy", "Goal", start=True))["id"]
    held = open(home / "sessions" / sid / "workspace" / "notes.txt", "w")
    try:
        with pytest.raises(SessionError) as err:
            await manager.delete(sid)
        assert err.value.status == 409
    finally:
        held.close()
    assert manager.exists(sid) and manager.summary(sid)["goal"]["title"] == "Goal"
    (home / "sessions" / f"{TRASH_PREFIX}old-1").mkdir()  # left by an earlier delete that could not finish
    await manager.delete(sid)
    assert list((home / "sessions").iterdir()) == []


async def test_hand_edited_settings_are_picked_up(tmp_path):
    home = _home(tmp_path)
    manager = _manager(home)
    path = home / "dreamteam.toml"
    text = path.read_text(encoding="utf-8").replace("sk-secret-123456", "sk-edited-by-hand-7777")
    path.write_text("# my notes\n" + text, encoding="utf-8")
    view = manager.settings_view()
    assert view["models"]["default"]["api_key_hint"] == "…7777"
    await manager.update_settings({"models": view["models"]})  # the UI save keeps the key edited by hand
    assert st.read_raw(home)["models"]["default"]["api_key"] == "sk-edited-by-hand-7777"
    path.write_text("[swarm\nbroken", encoding="utf-8")  # an invalid edit is ignored until it is fixed
    assert manager.settings_view()["models"]["default"]["api_key_hint"] == "…7777"


def test_sessions_main_dir_is_not_a_second_main(tmp_path):
    home = _home(tmp_path)
    (home / "sessions" / "main").mkdir(parents=True)
    (home / "sessions" / "main" / "session.json").write_text("{}", encoding="utf-8")
    manager = _manager(home)
    assert manager.ids() == [] and not manager.exists("main")


async def test_a_session_that_cannot_be_opened_does_not_keep_its_files_open(tmp_path, monkeypatch):
    """A failed Swarm() (a corrupt swarm.db, or any later step) kept the SQLite connection open until garbage
    collection: on Windows the session then could not be deleted (409 'files are in use') until a restart."""
    import dreamteam.swarm as swarm_module
    from dreamteam.db import Database

    home = _home(tmp_path)
    manager = _manager(home)
    sid = (await manager.create("Corrupt", start=False))["id"]
    db = home / "sessions" / sid / ".dreamteam" / "swarm.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"this is not a database " * 200)
    with pytest.raises(SessionError) as err:  # (err keeps the original exception and its frames alive)
        await manager.load(sid)
    assert err.value.status == 500
    with pytest.raises(Exception):
        Database(db)
    os.replace(db, db.with_name("moved.db"))  # nothing holds the file (Windows refuses to move an open file)
    # a failure after the database was opened closes it too
    other = (await manager.create("Broken", start=False))["id"]

    def broken_forge(swarm):
        raise RuntimeError("forge failed")

    monkeypatch.setattr(swarm_module, "MCPForge", broken_forge)
    with pytest.raises(SessionError) as err:
        await manager.load(other)
    assert "forge failed" in str(err.value)
    await manager.delete(other)
    await manager.delete(sid)
    assert manager.ids() == []


async def test_viewing_a_stopped_session_closes_traces_a_crash_left_running(tmp_path):
    """Tracer.recover() ran only in Swarm.start(): a session loaded for viewing (after a crash, with
    resume_sessions off) showed its interrupted activations as running, with an ever-growing duration."""
    manager = _manager(_home(tmp_path))
    sid = (await manager.create("Crashed", start=False))["id"]
    swarm = await manager.load(sid)
    trace = swarm.tracer.begin("Ann Lee", "wake up")  # an activation the crashed process never finished
    await manager.unload_idle(sid)
    swarm = await manager.load(sid)
    assert swarm.tracer.get_trace(trace.id)["status"] == "interrupted"
    await manager.shutdown()


async def test_config_view_survives_invalid_overrides(tmp_path):
    home = _home(tmp_path)
    raw = st.read_raw(home)
    raw["swarm"]["goal"] = "Home project goal"
    st.write_raw(home, raw)
    manager = _manager(home)
    sid = (await manager.create("Odd", start=False, overrides={"swarm": {"max_agents": 2}}))["id"]
    view = manager.config_view(sid)
    assert view["swarm"]["max_agents"] == 2 and "goal" not in view["swarm"]
    assert "sk-secret" not in json.dumps(view)
    meta = json.loads((home / "sessions" / sid / "session.json").read_text(encoding="utf-8"))
    meta["overrides"] = {"swarm": {"max_agents": "many"}}  # written by an older version
    (home / "sessions" / sid / "session.json").write_text(json.dumps(meta), encoding="utf-8")
    assert manager.config_view(sid)["swarm"]["max_agents"] == 3  # the global settings, not an error
    assert manager.summary(sid)["config_error"]
    with pytest.raises(SessionError) as err:
        manager.config_view("nope")
    assert err.value.status == 404
