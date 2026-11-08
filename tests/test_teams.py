"""Saved teams (docs/SPEC.md §17.3 "Saved teams"), the core: [[teams]] checks and normalization, the rule of --team and
/team, the settings file and view, what a session keeps of a team, and what the hub does not know about them."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from dreamteam import roles
from dreamteam import settings as st
from dreamteam.config import Config, TeamSettings, parse_teams, render_template
from dreamteam.launcher import create_launcher_app
from dreamteam.launcher_mcp import call_launcher_tool
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.sessions import SESSION_FILE, SessionError, SessionManager

ROLES = [{"name": "dev_gpt", "description": "Backend.", "model": "gpt"},
         {"name": "dev_claude", "description": "Backend.", "model": "claude"},
         {"name": "tester", "model": "gpt"},
         {"name": "researcher", "hidden": True}]
TEAMS = [{"name": "Разработка", "roles": ["architect", "dev_claude", "dev_gpt", "dev_gpt", "tester"]},
         {"name": "Тексты", "roles": ["writer", "reviewer"]}]
ROSTER = [("Architect", "default"), ("Tester", "gpt"), ("dev_claude", "claude"), ("dev_gpt", "gpt"), ("dev_gpt", "gpt")]
RESERVED = "the name '{0}' is reserved: /team {0} goes back to the starting team of the settings"
NOT_A_ROLE = "'{}' is not a role name: write one name per agent, at most 60 characters, without ',' or '*'"
TESTER = {"roles": ["tester"]}


def _raw(tmp_path: Path) -> dict[str, Any]:
    profile = {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}
    return {"swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 2.0},
            "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 0.2},
            "mcp": {"library_dir": str(tmp_path / "library")},
            "models": {"default": dict(profile), "gpt": dict(profile), "claude": dict(profile)},
            "roles": ROLES, "teams": TEAMS}


def _manager(tmp_path: Path, raw: dict[str, Any] | None = None) -> SessionManager:
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, raw or _raw(tmp_path))

    def factory(profile, agent):
        return ScriptedLLM(lambda system, messages, tools: "Nothing to do.", profile)

    return SessionManager(home, llm_factory=factory)


def _roster(manager: SessionManager, sid: str) -> list[tuple[str, str]]:
    return sorted((member["role"], member["model"]) for member in manager.swarms[sid].members())


def test_the_order_of_the_sections():
    assert st.SECTIONS == ("swarm", "agents", "server", "shell", "workspace", "mcp", "skills", "tracing", "teams",
                           "roles", "models")


# ---- loading: the shape, and only the shape ----------------------------------------------------------------
@pytest.mark.parametrize("teams, message", [
    ({"name": "x"}, "[[teams]] must be a list of team tables (write each team as a [[teams]] table)"),
    ("x", "[[teams]] must be a list of team tables (write each team as a [[teams]] table)"),
    ([{"name": f"t{i}", **TESTER} for i in range(101)], "At most 100 teams"),
    (["x"], "[teams.1] must be a table, got 'x'"),
    ([{"name": "x", **TESTER, "model": "gpt"}], "Unknown keys for [teams.1]: model"),
    ([{"name": 5, **TESTER}], "[teams.1] name must be a string, got 5"),
    ([{"name": "x", "roles": "tester"}], "[teams.1] roles must be a list of strings, got 'tester'"),
    ([{"name": "x", "roles": [1]}], "[teams.1] roles must be a list of strings, got [1]"),
    ([TESTER], "[teams.1] name is empty"),
    ([{"name": "  ", **TESTER}], "[teams.1] name is empty"),
    ([{"name": "y" * 61, **TESTER}], "[teams.1] name must be at most 60 characters"),
    ([{"name": "a,b", **TESTER}], "[teams.1] name cannot contain ',' or '*' or start with '-'"),
    ([{"name": "a*2", **TESTER}], "[teams.1] name cannot contain ',' or '*' or start with '-'"),
    ([{"name": "-a", **TESTER}], "[teams.1] name cannot contain ',' or '*' or start with '-'"),
    ([{"name": "-", **TESTER}], "[teams.1] name cannot contain ',' or '*' or start with '-'"),
    ([{"name": "\x1b]0;x\x07\x1b[31mred", **TESTER}], "[teams.1] name has control or invisible characters"),
    ([{"name": "​", **TESTER}], "[teams.1] name has control or invisible characters"),
    ([{"name": "a﻿b", **TESTER}], "[teams.1] name has control or invisible characters"),
    ([{"name": "‮abc", **TESTER}], "[teams.1] name has control or invisible characters"),
    ([{"name": "­abc", **TESTER}], "[teams.1] name has control or invisible characters"),
    ([{"name": "‍", **TESTER}], "[teams.1] name is empty"),   # the joiners alone name nothing
    ([{"name": "‌‍\U000e0067", **TESTER}], "[teams.1] name is empty"),
    ([{"name": "default", **TESTER}], "[teams.1] " + RESERVED.format("default")),
    ([{"name": "DEFAULT", **TESTER}], "[teams.1] " + RESERVED.format("DEFAULT")),
    ([{"name": "reset", **TESTER}], "[teams.1] " + RESERVED.format("reset")),
    ([{"name": "Сброс", **TESTER}], "[teams.1] " + RESERVED.format("Сброс")),
    ([{"name": "A  b", **TESTER}, {"name": "a B", **TESTER}],
     "[teams.2] the team 'a B' is listed twice (also as team 1)"),
    ([{"name": "x", "roles": []}], "[teams.1] the team 'x' has no roles"),
    ([{"name": "x"}], "[teams.1] the team 'x' has no roles"),
    ([{"name": "x", "roles": ["tester"] * 201}], "[teams.1] the team 'x' has more than 200 agents"),
    ([{"name": "x", "roles": [" "]}], "[teams.1] " + NOT_A_ROLE.format("")),
    ([{"name": "x", "roles": ["r" * 61]}], "[teams.1] " + NOT_A_ROLE.format("r" * 61)),
    ([{"name": "x", "roles": ["developer*2"]}], "[teams.1] " + NOT_A_ROLE.format("developer*2")),
    ([{"name": "x", "roles": ["a,b"]}], "[teams.1] " + NOT_A_ROLE.format("a,b")),
    ([{"name": "x", **TESTER}, {"name": "y", "roles": ["tester", "a,b"]}], "[teams.2] " + NOT_A_ROLE.format("a,b")),
])
def test_refused_at_load(teams, message):
    with pytest.raises(ValueError) as refused:
        Config.from_dict(".", {"teams": teams})
    assert str(refused.value) == message


def test_a_team_is_found_by_casefold():
    team = Config.from_dict(".", {"teams": [{"name": "Стройка", **TESTER}]}).teams[0]
    assert roles.find_team([team], "СТРОЙКА") is team and roles.team_choice("стройка", [team], [])[0] == "team"
    assert roles.team_key("STRASSE") == roles.team_key("straße")


@pytest.mark.parametrize("name", [
    "\U0001f9d1‍\U0001f4bb Разработка",                    # technologist: person + ZWJ + laptop
    "برنامه‌نویس",                                        # a Persian word with ZWNJ
    "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f England",   # a flag of tag characters
])
def test_a_name_may_hold_what_ordinary_text_is_made_of(name):
    """A team's name with joiners loads, and is found by what was typed."""
    team = Config.from_dict(".", {"teams": [{"name": name, **TESTER}]}).teams[0]
    assert team.name == name and roles.find_team([team], name) is team and roles.team_choice(name, [team], [])[0] == "team"


def test_a_load_without_teams_has_none():
    assert Config.from_dict(".", {}).teams == []
    assert Config.from_dict(".", {"teams": []}).teams == []
    assert parse_teams(None, []) == []


def test_the_limits_themselves_load():
    assert len(Config.from_dict(".", {"teams": [{"name": f"t{i}", **TESTER} for i in range(100)]}).teams) == 100
    team = Config.from_dict(".", {"teams": [{"name": "y" * 60, "roles": ["r" * 60] * 200}]}).teams[0]
    assert len(team.name) == 60 and len(team.roles) == 200


def test_load_normalizes_the_name_and_the_roles():
    cfg = Config.from_dict(".", {
        "roles": [{"name": "QA", "description": "Checks."}, {"name": "Тестировщик", "instructions": "Pytest."}],
        "teams": [{"name": "  Моя   команда ",
                   "roles": ["Тестировщик", "qa", "qa ", "Gone  role", "Developer", "generalist"]}],
    })
    team = cfg.teams[0]
    assert isinstance(team, TeamSettings) and team.name == "Моя команда"
    # a preset by its id, an own role as [[roles]] spells it, a name the catalog lacks as written; trailing
    # generalists stay
    assert team.roles == ["tester", "QA", "QA", "Gone role", "developer", "generalist"]


def test_load_does_not_ask_the_catalog():
    cfg = Config.from_dict(".", {
        "swarm": {"max_agents": 2},
        "roles": [{"name": "researcher", "hidden": True}],
        "teams": [{"name": "dangling", "roles": ["gone_role"]},          # a role the catalog lacks
                  {"name": "writer", "roles": ["researcher"]},           # the name of a role, and a hidden role
                  {"name": "big", "roles": ["tester", "tester", "tester"]},  # more than max_agents
                  {"name": "a-b.c Сброс", "roles": ["-odd"]}],           # only the whole name is reserved
    })
    assert [team.name for team in cfg.teams] == ["dangling", "writer", "big", "a-b.c Сброс"]
    assert roles.missing_roles(cfg.teams[0], cfg.roles) == ["gone_role"]
    assert roles.missing_roles(cfg.teams[1], cfg.roles) == []


# ---- finding a team --------------------------------------------------------------------------------------------
def test_team_key_collapses_whitespace_and_case_only():
    assert roles.team_key("  Моя   КОМАНДА ") == "моя команда"
    assert roles.team_key("Тестировщик") == "тестировщик"  # no preset behind a team's name
    assert roles.team_key(None) == ""


def test_find_team_reads_settings_and_views_alike():
    for teams in (Config.from_dict(".", {"teams": TEAMS}).teams, TEAMS):
        assert roles.find_team(teams, "  РАЗРАБОТКА ") is teams[0]
        assert roles.find_team(teams, "тексты") is teams[1]
        assert roles.find_team(teams, "нет такой") is None
        assert roles.find_team(teams, "") is None
        assert roles.find_team(teams, None) is None
    assert roles.find_team(None, "x") is None
    assert roles.find_team([], "x") is None


def test_missing_roles():
    entries = [{"name": "QA", "description": "Checks."}, {"name": "researcher", "hidden": True},
               {"name": "Secret", "hidden": True}]
    team = {"name": "t", "roles": ["Gone", "tester", "qa", "researcher", "Secret", "other", "gone", "Gone", "other"]}
    assert roles.missing_roles(team, entries) == ["Gone", "other"]  # in order, each once; hidden counts as present
    assert roles.missing_roles(team, None) == ["Gone", "qa", "Secret", "other"]
    assert roles.missing_roles(TeamSettings(name="t", roles=["tester"]), None) == []
    assert roles.missing_roles(TeamSettings(name="t", roles=["QA"]), entries) == []
    assert roles.missing_roles({"name": "no roles key"}, None) == []


def test_saved_team_in():
    teams = [{"name": "coding", **TESTER}, {"name": "writer", **TESTER}]
    assert roles.saved_team_in(["tester", " Coding "], teams, None) == "Coding"
    assert roles.saved_team_in(["tester", "new"], teams, None) is None
    assert roles.saved_team_in(["writer"], teams, None) is None  # a role of that name: it is a role
    assert roles.saved_team_in(["coding"], teams, [{"name": "Coding", "description": "Own role."}]) is None
    assert roles.saved_team_in(["coding"], None, None) is None


# ---- the rule of --team and /team ------------------------------------------------------------------------------
SAVED = [{"name": "coding", "roles": ["developer", "developer"]}, {"name": "writer", "roles": ["researcher"]}]
CATALOG = [{"name": "researcher", "hidden": True}, {"name": "QA", "description": "Checks."}]


@pytest.mark.parametrize("text, expected", [
    ("coding", ("team", SAVED[0])),
    ("  CODING ", ("team", SAVED[0])),
    ("writer", ("team", SAVED[1])),                     # the team, though a preset has that name
    ("writer*1", ("roles", ["writer"])),                # its name with *N reaches the role
    ("writer, tester", ("roles", ["writer", "tester"])),
    ("tester", ("roles", ["tester"])),
    ("Тестировщик", ("roles", ["Тестировщик"])),        # a catalog role in either language, kept as typed
    ("qa", ("roles", ["qa"])),
    ("Researcher", ("roles", ["Researcher"])),          # hidden: not offered, still a role
    ("codng", ("unknown", "codng")),
    ("  Not   known ", ("unknown", "Not known")),
    ("codng*1", ("roles", ["codng"])),                  # how a new role is written once teams are saved
    ("new, tester", ("roles", ["new", "tester"])),      # a list is roles
    ("new, tester*2", ("roles", ["new", "tester", "tester"])),
    ("coding, tester", ("mixed", "coding")),
    ("tester, CODING", ("mixed", "CODING")),
    ("coding*2", ("mixed", "coding")),
])
def test_team_choice(text, expected):
    assert roles.team_choice(text, SAVED, CATALOG) == expected


@pytest.mark.parametrize("text", [" , ", "", None, "x*0", "x*51", "x*y", "*2", "r" * 61])
@pytest.mark.parametrize("teams", [SAVED, [], None])
def test_team_choice_bad_syntax(text, teams):
    with pytest.raises(ValueError):
        roles.team_choice(text, teams, CATALOG)


@pytest.mark.parametrize("teams", [[], None])
def test_team_choice_with_no_team_saved_a_bare_word_is_a_new_role(teams):
    assert roles.team_choice("Fresh", teams, None) == ("roles", ["Fresh"])
    assert roles.team_choice("tester", teams, None) == ("roles", ["tester"])
    assert roles.team_choice("Fresh*2", teams, None) == ("roles", ["Fresh", "Fresh"])


def test_team_choice_tolerates_a_view_without_the_keys():
    assert roles.team_choice("Fresh", None, None) == ("roles", ["Fresh"])
    assert roles.team_choice("tester", SAVED, None) == ("roles", ["tester"])
    assert roles.team_choice("Fresh", SAVED, None) == ("unknown", "Fresh")


def test_team_choice_returns_the_team_it_was_given():
    cfg = Config.from_dict(".", {"roles": CATALOG, "teams": SAVED})
    kind, team = roles.team_choice("WRITER", cfg.teams, cfg.roles)
    assert kind == "team" and team is cfg.teams[1] and team.roles == ["researcher"]
    assert roles.missing_roles(team, cfg.roles) == []


# ---- the template ----------------------------------------------------------------------------------------------
def test_the_template_has_no_team_and_its_examples_load():
    text = render_template("X", "ru", "anthropic")
    assert Config.from_dict(".", tomllib.loads(text)).teams == []
    head, marker, tail = text.partition("# Каталог ролей")
    assert "# [[teams]]" in head and "[[teams]]" not in tail
    teams = re.sub(r"(?m)^# (\[\[teams\]\]|name = |roles = )", r"\1", head)
    own_roles = re.sub(r"(?m)^# (\[\[roles\]\]|name = |model = |description = )", r"\1", tail)
    assert re.findall(r"(?m)^\[\[teams\]\]$", teams) == ["[[teams]]"] * 2

    cfg = Config.from_dict(".", tomllib.loads(teams + marker + tail))
    assert [(team.name, team.roles) for team in cfg.teams] == [
        ("Разработка", ["architect", "developer", "developer", "tester"]),
        ("Тексты", ["researcher", "writer", "reviewer"])]
    assert all(roles.missing_roles(team, cfg.roles) == [] for team in cfg.teams)
    assert cfg.roles == []

    cfg = Config.from_dict(".", tomllib.loads(head + marker + own_roles))  # the roles example, as before
    assert [role.name for role in cfg.roles] == ["tester", "Дата-инженер"] and cfg.teams == []

    cfg = Config.from_dict(".", tomllib.loads(teams + marker + own_roles))  # both
    assert len(cfg.teams) == 2 and len(cfg.roles) == 2


# ---- the settings file, the view, updates ----------------------------------------------------------------------
def test_the_settings_file_round_trip(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    raw = _raw(tmp_path)
    raw["teams"] = [*TEAMS, {"name": "a.b", **TESTER}, {"name": 'Кав"ычки', **TESTER},
                    {"name": "back\\slash", **TESTER}]
    text = st.dump_toml(raw)
    assert text.count("[[teams]]") == 5 and text.index("[[teams]]") > text.index("[models.default]")  # after the tables
    assert tomllib.loads(text) == raw
    st.write_raw(home, raw)
    assert st.read_raw(home) == raw
    assert [team.name for team in Config.load(home).teams] == ["Разработка", "Тексты", "a.b", 'Кав"ычки', "back\\slash"]


def test_the_public_view(tmp_path):
    raw = _raw(tmp_path)
    view = st.public_view(raw)
    assert list(view)[-3:] == ["teams", "roles", "models"]
    assert view["teams"] == TEAMS
    assert st.public_view({k: v for k, v in raw.items() if k != "teams"})["teams"] == []
    raw["teams"] = [{"name": " Моя   команда ", "roles": ["Тестировщик", "DEV_GPT", "gone  role", "generalist"]}]
    assert st.public_view(raw)["teams"] == [
        {"name": "Моя команда", "roles": ["tester", "dev_gpt", "gone role", "generalist"]}]


def test_apply_update_takes_the_complete_list(tmp_path):
    raw = _raw(tmp_path)
    # the view goes back unchanged, and a section not sent stays
    assert st.apply_update(raw, {"teams": st.public_view(raw)["teams"]})["teams"] == TEAMS
    assert st.apply_update(raw, {"agents": {"max_steps_per_turn": 5}})["teams"] == TEAMS
    new = st.apply_update(raw, {"teams": [TEAMS[1]]})
    assert new["teams"] == [TEAMS[1]] and raw["teams"] == TEAMS
    assert "teams" not in st.apply_update(raw, {"teams": []})
    assert "teams" not in st.dump_toml(st.apply_update(raw, {"teams": []}))
    # only None is dropped: the rest is for `validate` to judge
    assert st.apply_update(raw, {"teams": [{"name": "x", "roles": ["tester"], "note": None}]})["teams"] == [
        {"name": "x", "roles": ["tester"]}]
    blank = st.apply_update(raw, {"teams": [{"name": "", "roles": []}]})
    assert blank["teams"] == [{"name": "", "roles": []}]
    with pytest.raises(ValueError, match=r"\[teams.1\] name is empty"):
        st.validate(tmp_path, blank)


@pytest.mark.parametrize("teams, message", [
    ({"name": "x"}, "Section teams must be a list of teams"),
    ("x", "Section teams must be a list of teams"),
    (None, "Section teams must be a list of teams"),
    (["x"], "Team 1 must be an object"),
    ([{"name": "x", **TESTER}, 5], "Team 2 must be an object"),
])
def test_apply_update_refuses_a_bad_shape(tmp_path, teams, message):
    with pytest.raises(ValueError) as refused:
        st.apply_update(_raw(tmp_path), {"teams": teams})
    assert str(refused.value) == message


async def test_an_old_home_saves_no_teams_key(tmp_path):
    raw = _raw(tmp_path)
    del raw["teams"]
    manager = _manager(tmp_path, raw)
    try:
        view = manager.settings_view()
        assert view["teams"] == []
        await manager.update_settings(view)
        assert "teams" not in st.read_raw(manager.home)
        assert "teams" not in st.settings_path(manager.home).read_text(encoding="utf-8")
    finally:
        await manager.shutdown()


async def test_a_save_may_leave_a_team_on_a_missing_role(tmp_path):
    manager = _manager(tmp_path)
    try:
        without = [role for role in ROLES if role["name"] != "dev_gpt"]
        result = await manager.update_settings({"roles": without})
        assert result["settings"]["teams"] == TEAMS
        cfg = Config.load(manager.home)  # it loads
        assert roles.missing_roles(cfg.teams[0], cfg.roles) == ["dev_gpt"]
        assert roles.missing_roles(cfg.teams[1], cfg.roles) == []
        # it blocks no other save, and an update that sends it back passes too
        await manager.update_settings({"agents": {"default_model": "gpt"}})
        result = await manager.update_settings(manager.settings_view())
        assert result["settings"]["agents"]["default_model"] == "gpt" and result["settings"]["teams"] == TEAMS
        assert st.read_raw(manager.home)["teams"] == TEAMS
    finally:
        await manager.shutdown()


async def test_a_hand_edit_counts_and_a_bad_one_is_ignored(tmp_path):
    manager = _manager(tmp_path)
    try:
        st.write_raw(manager.home, {**st.read_raw(manager.home), "teams": [TEAMS[1]]})
        assert [team.name for team in manager.global_config().teams] == ["Тексты"]
        assert manager.settings_view()["teams"] == [TEAMS[1]]
        path = st.settings_path(manager.home)
        path.write_text(path.read_text(encoding="utf-8") + '\n[[teams]]\nname = "ТЕКСТЫ"\nroles = ["writer"]\n',
                        encoding="utf-8")
        assert [team.name for team in manager.global_config().teams] == ["Тексты"]  # the last valid settings stay
    finally:
        await manager.shutdown()
    with pytest.raises(ValueError, match=r"\[teams.2\] the team 'ТЕКСТЫ' is listed twice \(also as team 1\)"):
        SessionManager(manager.home)


# ---- a session holds its roster, from session_create(team_name=...) --------------------------------------------
@pytest.mark.parametrize("change", [
    pytest.param({"teams": [{"name": "Разработка", "roles": ["writer"]}, TEAMS[1]]}, id="edited"),
    pytest.param({"teams": [TEAMS[1]]}, id="deleted"),
    pytest.param({"teams": []}, id="the last one deleted"),
])
async def test_a_started_session_keeps_its_roster(tmp_path, change):
    manager = _manager(tmp_path)
    try:
        text, error = await call_launcher_tool(manager, "session_create", {"goal": "Goal", "team_name": "разработка"})
        assert not error, text
        sid = manager.ids()[0]
        assert _roster(manager, sid) == ROSTER  # each member has its role's own profile
        meta = manager.meta(sid)
        assert meta["overrides"]["swarm"] == {"starting_roles": TEAMS[0]["roles"], "start_agents": 5}
        saved = (manager.home / "sessions" / sid / SESSION_FILE).read_text(encoding="utf-8")
        assert "team_name" not in saved and "Разработка" not in saved
        summary = json.dumps(manager.summary(sid), ensure_ascii=False)
        assert "team_name" not in summary and "Разработка" not in summary

        await manager.update_settings(change)
        await manager.stop(sid)
        await manager.start(sid)
        assert _roster(manager, sid) == ROSTER
        assert manager.meta(sid)["overrides"] == meta["overrides"]
    finally:
        await manager.shutdown()


async def test_a_session_not_started_keeps_its_roster(tmp_path):
    manager = _manager(tmp_path)
    try:
        text, error = await call_launcher_tool(manager, "session_create",
                                               {"goal": "Goal", "team_name": "Разработка", "start": False})
        assert not error, text
        sid = manager.ids()[0]
        await manager.update_settings({"teams": []})
        await manager.start(sid)
        assert _roster(manager, sid) == ROSTER
    finally:
        await manager.shutdown()


async def test_the_hub_knows_no_team_name(tmp_path):
    manager = _manager(tmp_path)
    try:
        with pytest.raises(SessionError, match=r"Unknown keys for \[swarm\]: team_name"):
            await manager.create("", "Goal", overrides={"swarm": {"team_name": "Тексты"}}, start=False)
        with pytest.raises(SessionError, match=r"Sessions cannot override \[teams\]"):
            await manager.create("", "Goal", overrides={"teams": []}, start=False)
        assert manager.ids() == []
        # a saved team's name among literal roles is a new role, not that team
        literal = {"swarm": {"starting_roles": ["Тексты"], "start_agents": 1}}
        session = await manager.create("", "Goal", overrides=literal)
        assert _roster(manager, session["id"]) == [("Тексты", "default")]
    finally:
        await manager.shutdown()


async def test_a_literal_team_is_as_it_was(tmp_path):
    manager = _manager(tmp_path)
    try:  # a new role with no description, more agents than max_agents: all of them start
        literal = {"swarm": {"starting_roles": ["Fresh"] * 5, "start_agents": 5}}
        session = await manager.create("", "Goal", overrides=literal)
        assert session["max_agents"] == 3
        assert _roster(manager, session["id"]) == [("Fresh", "default")] * 5
    finally:
        await manager.shutdown()


# ---- REST ------------------------------------------------------------------------------------------------------
@pytest.fixture
def client(tmp_path):
    manager = _manager(tmp_path)
    with TestClient(create_launcher_app(manager), base_url="http://127.0.0.1:8765") as c:
        c.manager = manager
        yield c
        c.portal.call(manager.shutdown)


def _send(c, method: str, path: str, body: Any):
    return c.request(method, path, content=json.dumps(body), headers={"content-type": "application/json"})


def test_rest_get_then_put_leaves_the_teams_as_they_were(client):
    settings = client.get("/api/settings").json()["settings"]
    assert settings["teams"] == TEAMS
    resp = _send(client, "PUT", "/api/settings", {"settings": settings})
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"]["teams"] == TEAMS
    assert st.read_raw(client.manager.home)["teams"] == TEAMS


def test_rest_put_changes_and_removes(client):
    resp = _send(client, "PUT", "/api/settings", {"settings": {"teams": [TEAMS[1]]}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"]["teams"] == [TEAMS[1]]
    assert st.read_raw(client.manager.home)["teams"] == [TEAMS[1]]
    resp = _send(client, "PUT", "/api/settings", {"settings": {"teams": []}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"]["teams"] == [] and client.get("/api/settings").json()["settings"]["teams"] == []
    assert "teams" not in st.read_raw(client.manager.home)


@pytest.mark.parametrize("teams, message", [
    ({"name": "x"}, "Section teams must be a list of teams"),
    (["x"], "Team 1 must be an object"),
    ([{"name": "x", "roles": []}], "[teams.1] the team 'x' has no roles"),
    ([{"name": "x", **TESTER, "model": "gpt"}], "Unknown keys for [teams.1]: model"),
    ([{"name": "сброс", **TESTER}], "[teams.1] " + RESERVED.format("сброс")),
])
def test_rest_put_refuses_a_bad_shape_and_writes_nothing(client, teams, message):
    before = st.settings_path(client.manager.home).read_bytes()
    resp = _send(client, "PUT", "/api/settings", {"settings": {"teams": teams}})
    assert resp.status_code == 400 and resp.json()["error"] == message
    assert st.settings_path(client.manager.home).read_bytes() == before


def test_rest_put_does_not_ask_the_catalog(client):
    dangling = [{"name": "dangling", "roles": ["gone_role"]}]
    resp = _send(client, "PUT", "/api/settings", {"settings": {"teams": dangling}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"]["teams"] == dangling


def test_rest_a_session_takes_literal_roles_and_no_team_name(client):
    team = TEAMS[1]["roles"]
    resp = _send(client, "POST", "/api/sessions", {
        "goal": "G", "start": False, "overrides": {"swarm": {"starting_roles": team, "start_agents": len(team)}}})
    assert resp.status_code == 201, resp.text
    sid = resp.json()["session"]["id"]
    assert client.manager.meta(sid)["overrides"]["swarm"] == {"starting_roles": team, "start_agents": 2}
    assert client.get(f"/api/sessions/{sid}").json()["config"]["teams"] == TEAMS

    for overrides, why in (({"swarm": {"team_name": "Тексты"}}, "Unknown keys for [swarm]: team_name"),
                           ({"teams": []}, "Sessions cannot override [teams]")):
        resp = _send(client, "POST", "/api/sessions", {"goal": "G", "start": False, "overrides": overrides})
        assert resp.status_code == 400 and why in resp.json()["error"]
    assert client.manager.ids() == [sid]
