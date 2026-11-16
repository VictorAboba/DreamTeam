"""[api] settings (docs/SPEC.md §21.9, §21.10 "Settings"): the defaults and checks of ApiSettings and of the shape of
[api] folders, a section edited by hand only (a settings-page save never writes it), the template's commented block,
and an import that carries the limits but never this computer's folders."""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from dreamteam import settings as st
from dreamteam.config import ApiFolder, ApiSettings, Config, _fill, native_absolute, parse_api_folders, render_template
from dreamteam.launcher import create_launcher_app
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.sessions import OVERRIDE_SECTIONS, SessionError, SessionManager
from dreamteam.transfer import LOCAL_KEYS, apply_import, export_home, merge_settings, plan_import, scrub_settings

PROFILE = {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}
FOLDERS = {"jobs": {"path": "/srv/site/jobs", "in_place": True}, "docs-2": {"path": "C:\\Users\\me\\docs"}}
HERE = {"here": {"path": "/srv/here", "in_place": True}}
NOT_A_NAME = "is not a folder name: lowercase letters, digits and '-' (not first), at most 64 characters"
NOT_ABSOLUTE = "[api.folders.jobs] path must be an absolute path, got"
JSON = {"content-type": "application/json"}


def _raw(**api: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 2.0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 0.2},
        "models": {"default": dict(PROFILE)}}
    if api:
        raw["api"] = api
    return raw


def _home(tmp_path: Path, name: str, raw: dict[str, Any]) -> Path:
    home = tmp_path / name
    (home / "sessions").mkdir(parents=True)
    st.write_raw(home, raw)
    return home


def _manager(home: Path) -> SessionManager:
    def factory(profile, agent):
        return ScriptedLLM(lambda system, messages, tools: "Nothing to do.", profile)

    return SessionManager(home, llm_factory=factory)


# ---- loading ----------------------------------------------------------------------------------------------------
def test_the_defaults_and_a_global_section():
    api = Config.from_dict(".", {}).api
    assert api == ApiSettings() == ApiSettings(max_running=3, uploads_pending=32, archive_max_mb=1024, folders={})
    assert "api" not in OVERRIDE_SECTIONS
    with pytest.raises(SessionError, match=r"Sessions cannot override \[api\]"):
        SessionManager.clean_overrides({"api": {"max_running": 1}})


def test_the_numbers():
    api = Config.from_dict(".", {"api": {"max_running": 0, "uploads_pending": 1, "archive_max_mb": 2.0}}).api
    assert (api.max_running, api.uploads_pending, api.archive_max_mb) == (0, 1, 2)   # 0 runs: no limit
    assert type(api.archive_max_mb) is int
    assert Config.from_dict(".", {"api": {"max_running": None}}).api.max_running == 3   # None: not set


@pytest.mark.parametrize("api, message", [
    (1, "[api] must be a table, got 1"),
    ({"max_running": "3"}, "[api] max_running must be a number, got '3'"),
    ({"max_running": True}, "[api] max_running must be a number, got True"),
    ({"max_running": -1}, "[api] max_running must be >= 0, got -1"),
    ({"uploads_pending": 0}, "[api] uploads_pending must be >= 1, got 0"),
    ({"archive_max_mb": 0}, "[api] archive_max_mb must be >= 1, got 0"),
    ({"archive_max_mb": 1.5}, "[api] archive_max_mb must be a whole number, got 1.5"),
    ({"max_runing": 3}, "Unknown keys for [api]: max_runing"),
])
def test_a_wrong_value_names_the_section(api, message):
    with pytest.raises(ValueError) as caught:
        Config.from_dict(".", {"api": api})
    assert str(caught.value) == message


def test_the_folders_shape_only():
    folders = Config.from_dict(".", {"api": {"folders": FOLDERS}}).api.folders
    assert folders == {"jobs": ApiFolder("jobs", "/srv/site/jobs", True),
                       "docs-2": ApiFolder("docs-2", "C:\\Users\\me\\docs", False)}
    # absolute on POSIX or on Windows, whatever this computer is: the sandbox's hub reads a Windows home's file too;
    # where it lies is not checked at load (an unplugged disk must not stop the hub)
    for path in ("/srv/x", "/nowhere/at/all", "C:\\x", "C:/x", "\\\\server\\share\\x", "//server/share/x",
                 "\\\\?\\C:\\x"):
        assert parse_api_folders({"x": {"path": path}}) == {"x": ApiFolder("x", path, False)}
    assert parse_api_folders(None) == {} == parse_api_folders({})
    name = "a-0" + "z" * 61
    assert parse_api_folders({name: {"path": "/x", "in_place": None}})[name].in_place is False
    assert list(parse_api_folders({"0-": {"path": "/x"}, "9": {"path": "/y"}})) == ["0-", "9"]


def test_native_absolute_reads_a_path_as_this_system_does():
    # a root absolute only on the other system loads (the sandbox), but a use outside it refuses the root
    windows = os.name == "nt"
    for path in ("C:\\x", "C:/x", "\\\\server\\share\\x", "\\\\?\\C:\\x"):
        assert native_absolute(path) is windows, path
    assert native_absolute("/srv/x") is not windows
    assert native_absolute("//server/share/x")
    for path in ("x", "C:x", "\\x", "\\\\server", "\\\\?\\x", "~/x"):
        assert not native_absolute(path), path


@pytest.mark.parametrize("folders, message", [
    ([], "[api] folders must be a table of folders (write each as an [api.folders.<name>] table), got []"),
    ("/srv", "[api] folders must be a table of folders (write each as an [api.folders.<name>] table), got '/srv'"),
    ({"Jobs": {"path": "/x"}}, f"[api.folders] 'Jobs' {NOT_A_NAME}"),
    ({"jobs_1": {"path": "/x"}}, f"[api.folders] 'jobs_1' {NOT_A_NAME}"),
    ({"a.b": {"path": "/x"}}, f"[api.folders] 'a.b' {NOT_A_NAME}"),
    ({"jöbs": {"path": "/x"}}, f"[api.folders] 'jöbs' {NOT_A_NAME}"),
    ({"": {"path": "/x"}}, f"[api.folders] '' {NOT_A_NAME}"),
    ({"a" * 65: {"path": "/x"}}, f"[api.folders] '{'a' * 65}' {NOT_A_NAME}"),
    ({"-": {"path": "/x"}}, f"[api.folders] '-' {NOT_A_NAME}"),
    ({"-x": {"path": "/x"}}, f"[api.folders] '-x' {NOT_A_NAME}"),
    ({"jobs": "/srv"}, "[api.folders.jobs] must be a table, got '/srv'"),
    ({"jobs": {}}, f"{NOT_ABSOLUTE} None"),
    ({"jobs": {"path": ""}}, f"{NOT_ABSOLUTE} ''"),
    ({"jobs": {"path": "jobs"}}, f"{NOT_ABSOLUTE} 'jobs'"),
    ({"jobs": {"path": "~/jobs"}}, f"{NOT_ABSOLUTE} '~/jobs'"),
    ({"jobs": {"path": "C:jobs"}}, f"{NOT_ABSOLUTE} 'C:jobs'"),
    ({"jobs": {"path": "\\jobs"}}, f"{NOT_ABSOLUTE} '\\\\jobs'"),
    # a share without its name and a bare `\\?\` prefix: refused on every Python (3.13 calls them absolute)
    ({"jobs": {"path": "\\\\server"}}, f"{NOT_ABSOLUTE} '\\\\\\\\server'"),
    ({"jobs": {"path": "\\\\?\\x"}}, f"{NOT_ABSOLUTE} '\\\\\\\\?\\\\x'"),
    ({"jobs": {"path": " /srv"}}, f"{NOT_ABSOLUTE} ' /srv'"),
    ({"jobs": {"path": "/a\x00b"}}, f"{NOT_ABSOLUTE} '/a\\x00b'"),
    ({"jobs": {"path": ["/srv"]}}, f"{NOT_ABSOLUTE} ['/srv']"),
    ({"jobs": {"path": "/srv", "in_place": "true"}}, "[api.folders.jobs] in_place must be true or false, got 'true'"),
    ({"jobs": {"path": "/srv", "in_place": 1}}, "[api.folders.jobs] in_place must be true or false, got 1"),
    ({"jobs": {"path": "/srv", "mode": "copy"}}, "Unknown keys for [api.folders.jobs]: mode"),
])
def test_a_wrong_folder_refuses_the_settings(tmp_path, folders, message):
    with pytest.raises(ValueError) as caught:
        Config.from_dict(".", {"api": {"folders": folders}})
    assert str(caught.value) == message
    with pytest.raises(ValueError) as caught:   # any way of filling ApiSettings checks the folders
        _fill(ApiSettings, {"folders": folders})
    assert str(caught.value) == message
    with pytest.raises(ValueError):
        st.write_raw(tmp_path, _raw(folders=folders))
    assert not st.settings_path(tmp_path).exists()


def test_a_section_written_by_hand(tmp_path):
    # roots named like the tables written inline elsewhere ([models.x] extra, [mcp.external.x] env) stay tables
    folders = {**FOLDERS, "env": {"path": "/srv/env", "in_place": True}, "extra": {"path": "/srv/extra"}}
    raw = _raw(max_running=0, folders=folders)
    raw["tracing"] = {"enabled": False}
    raw["teams"] = [{"name": "Build", "roles": ["developer"]}]
    text = st.dump_toml(raw)
    assert text.index("[tracing]") < text.index("[api]") < text.index("[api.folders.jobs]") \
        < text.index("[api.folders.env]") < text.index("[api.folders.extra]") < text.index("[models.default]") \
        < text.index("[[teams]]")
    assert "env = {" not in text and "extra = {" not in text
    home = _home(tmp_path, "home", raw)
    assert st.read_raw(home) == raw
    assert Config.load(home).api == ApiSettings(max_running=0, folders={
        "jobs": ApiFolder("jobs", "/srv/site/jobs", True), "docs-2": ApiFolder("docs-2", "C:\\Users\\me\\docs"),
        "env": ApiFolder("env", "/srv/env", True), "extra": ApiFolder("extra", "/srv/extra")})
    (home / "dreamteam.toml").write_text('[api.folders.web]\npath = "/var/www"\n\n[models.default]\n'
                                         'provider = "scripted"\nmodel = "scripted"\n', encoding="utf-8")
    assert Config.load(home).api == ApiSettings(folders={"web": ApiFolder("web", "/var/www")})


# ---- the settings page never writes it ---------------------------------------------------------------------------
def _put(c: TestClient, settings: dict[str, Any]):
    return c.put("/api/settings", content=json.dumps({"settings": settings}), headers=JSON)


def test_a_settings_page_save_never_writes_the_section(tmp_path):
    home = _home(tmp_path, "home", _raw())
    manager = _manager(home)
    with TestClient(create_launcher_app(manager), base_url="http://127.0.0.1:8765") as c:
        view = c.get("/api/settings").json()["settings"]
        assert "api" not in view
        view["agents"]["max_steps_per_turn"] = 9
        saved = _put(c, view)
        assert saved.status_code == 200, saved.text
        assert "api" not in saved.json()["settings"]
        raw = st.read_raw(home)
        assert raw["agents"]["max_steps_per_turn"] == 9 and "api" not in raw
        assert "[api" not in st.settings_path(home).read_text(encoding="utf-8")
        # a default sent by hand writes nothing either; another value is written
        assert _put(c, {"api": {"max_running": 3, "uploads_pending": 32, "folders": {}}}).status_code == 200
        assert "api" not in st.read_raw(home)
        assert _put(c, {"api": {"max_running": 7}}).status_code == 200
        assert st.read_raw(home)["api"] == {"max_running": 7}
        assert manager.global_config().api.max_running == 7
        refused = _put(c, {"api": {"max_running": "x"}})
        assert refused.status_code == 400 and "[api] max_running must be a number" in refused.json()["error"]
        c.portal.call(manager.shutdown)


def test_a_settings_page_save_keeps_a_section_written_by_hand(tmp_path):
    home = _home(tmp_path, "home", _raw(max_running=5, folders=FOLDERS))
    manager = _manager(home)
    with TestClient(create_launcher_app(manager), base_url="http://127.0.0.1:8765") as c:
        data = c.get("/api/settings")
        view = data.json()["settings"]
        assert "api" not in view and "/srv/site/jobs" not in data.text   # no path of this computer shown either
        view["swarm"]["max_agents"] = 4
        assert _put(c, view).status_code == 200
        raw = st.read_raw(home)
        assert raw["swarm"]["max_agents"] == 4 and raw["api"] == {"max_running": 5, "folders": FOLDERS}
        c.portal.call(manager.shutdown)


def test_apply_update_stores_only_what_differs_from_the_defaults():
    raw = _raw()
    defaults = {"max_running": 3, "uploads_pending": 32, "archive_max_mb": 1024, "folders": {}}
    assert "api" not in st.apply_update(raw, {"api": defaults})
    assert "api" not in st.apply_update(raw, {"api": {"max_running": 3.0, "uploads_pending": None}})
    assert "api" not in st.apply_update(raw, {"api": {}})
    assert st.apply_update(raw, {"api": {"max_running": 0, "archive_max_mb": 1024}})["api"] == {"max_running": 0}
    sent = {"folders": FOLDERS, "uploads_pending": 32}
    assert st.apply_update(raw, {"api": sent})["api"] == {"folders": FOLDERS} and sent["uploads_pending"] == 32
    # the section sent replaces the stored one, as every section does, and an empty one removes it
    stored = _raw(max_running=5, folders=FOLDERS)
    assert st.apply_update(stored, {"api": {"folders": HERE}})["api"] == {"folders": HERE}
    assert "api" not in st.apply_update(stored, {"api": {"max_running": 3}})
    # a section not sent stays
    assert st.apply_update(stored, {"agents": {"max_steps_per_turn": 5}})["api"] == stored["api"]
    # the rest is for validate to judge
    assert st.apply_update(raw, {"api": {"max_running": True, "nope": 1}})["api"] == {"max_running": True, "nope": 1}
    with pytest.raises(ValueError, match="Section api must be an object"):
        st.apply_update(raw, {"api": [1]})


# ---- the template ------------------------------------------------------------------------------------------------
def test_the_template_has_only_a_commented_block_and_it_loads_once_uncommented():
    text = render_template("X", "ru", "anthropic")
    raw = tomllib.loads(text)
    assert "api" not in raw and Config.from_dict(".", raw).api == ApiSettings()
    assert re.findall(r"(?m)^# \[api[^\]]*\]$", text) == ["# [api]", "# [api.folders.jobs]"]
    example = re.sub(r"(?m)^# (\[api\]|\[api\.folders\.jobs\]|max_running = |uploads_pending = |archive_max_mb = "
                     r"|path = |in_place = )", r"\1", text)
    api = Config.from_dict(".", tomllib.loads(example)).api
    assert api == ApiSettings(folders={"jobs": ApiFolder("jobs", "/srv/site/jobs", False)})   # the defaults it shows
    # its Windows hint: single quotes keep the backslash ("C:\jobs" in double quotes is not valid TOML)
    assert "'C:\\jobs'" in text and '"C:/jobs"' in text
    hint = tomllib.loads("[api.folders.jobs]\npath = 'C:\\jobs'\n")
    assert Config.from_dict(".", hint).api.folders["jobs"].path == "C:\\jobs"
    with pytest.raises(tomllib.TOMLDecodeError):
        tomllib.loads('[api.folders.jobs]\npath = "C:\\jobs"\n')


# ---- export and import -------------------------------------------------------------------------------------------
def test_merge_settings_carries_the_limits_and_never_the_folders():
    assert ("api", "folders") in LOCAL_KEYS
    m = merge_settings(_raw(uploads_pending=4, folders=HERE), _raw(max_running=1, uploads_pending=32, folders=FOLDERS))
    assert m.error is None and m.sections == ["api"] and m.local_kept == ["api.folders"]
    assert m.new_raw["api"] == {"max_running": 1, "folders": HERE}   # the exported default 32 wins over 4
    # an exported default changes nothing, nor do the folders alone
    assert not merge_settings(_raw(), _raw(max_running=3, archive_max_mb=1024)).changed
    m = merge_settings(_raw(), _raw(folders=FOLDERS))
    assert not m.changed and m.local_kept == ["api.folders"] and "api" not in m.new_raw
    m = merge_settings(_raw(max_running=5, folders=FOLDERS), _raw(max_running=3))
    assert m.changed and m.new_raw["api"] == {"folders": FOLDERS}
    # the exported folders are never looked at, whatever they hold (another system's, a newer version's, garbage):
    # an import neither takes them nor fails on them
    for exported in ({"Bad": {"path": "rel", "x": 1}}, "garbage", ["/srv"]):
        m = merge_settings(_raw(folders=HERE), _raw(folders=exported, max_running=1))
        assert m.error is None and m.local_kept == ["api.folders"], exported
        assert m.new_raw["api"] == {"max_running": 1, "folders": HERE}
    # a wrong value refuses the settings, as in any section; an unknown key is left out
    assert merge_settings(_raw(), _raw(max_running="x")).error == "[api] max_running must be a number, got 'x'"
    m = merge_settings(_raw(), _raw(future=1, archive_max_mb=10))
    assert "api.future" in m.dropped and m.new_raw["api"] == {"archive_max_mb": 10}


async def test_an_export_carries_the_section_and_an_import_keeps_the_folders_here(tmp_path):
    assert scrub_settings(_raw(max_running=1, folders=FOLDERS))[0]["api"] == {"max_running": 1, "folders": FOLDERS}
    home_a = _home(tmp_path, "a", _raw(max_running=1, archive_max_mb=10, folders=FOLDERS))
    dest = tmp_path / "usb"
    report = export_home(home_a, dest, include=["settings"])
    assert report.ok, report.errors
    exported = tomllib.loads((dest / "settings" / "dreamteam.toml").read_text(encoding="utf-8"))
    assert exported["api"] == {"max_running": 1, "archive_max_mb": 10, "folders": FOLDERS}
    home_b = _home(tmp_path, "b", _raw(uploads_pending=4, folders=HERE))
    manager = _manager(home_b)
    try:
        plan = plan_import(manager, dest)
        item = next(i for i in plan.items if i.category == "settings")
        assert item.action == "rewrite"
        assert "sections: api" in item.reason and "kept as they are here: api.folders" in item.reason
        result = await apply_import(manager, plan)
        assert result.ok
    finally:
        await manager.shutdown()
    assert st.read_raw(home_b)["api"] == {"uploads_pending": 4, "max_running": 1, "archive_max_mb": 10,
                                          "folders": HERE}
