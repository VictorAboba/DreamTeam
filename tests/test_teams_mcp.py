"""Saved teams through the launcher MCP (docs/SPEC.md §17.3 "Saved teams"): session_create(team_name=...) copies a
team's roles into the session as a literal team, and refuses what it cannot start; no other tool changes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from dreamteam import settings as st
from dreamteam.hub_mcp import INSTRUCTIONS as HUB_INSTRUCTIONS
from dreamteam.launcher_mcp import (INSTRUCTIONS, LAUNCHER_TOOLS, build_launcher_server, call_launcher_tool,
                                    list_launcher_tools)
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.sessions import SESSION_FILE, SessionManager

ROLES = [{"name": "dev_gpt", "description": "Backend.", "model": "gpt"},
         {"name": "researcher", "hidden": True}]
BUILD = ["architect", "dev_gpt", "dev_gpt", "tester", "generalist"]   # a trailing generalist stays
PROSE = ["writer", "reviewer"]
TEAMS = [{"name": "Разработка", "roles": BUILD}, {"name": "Тексты", "roles": PROSE}]
ALONE = "Error: team_name sets the roles and their number: give it without team and start_agents."
NOT_A_NAME = "Error: team_name must be the name of a saved team."
NONE_SAVED = "None are saved yet: Settings > Teams."


def _raw(tmp_path: Path, **sections: Any) -> dict[str, Any]:
    profile = {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}
    raw = {"swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 2.0},
           "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 0.2},
           "mcp": {"library_dir": str(tmp_path / "library")},
           "models": {"default": dict(profile), "gpt": dict(profile), "claude": dict(profile)},
           "roles": ROLES, "teams": TEAMS, **sections}
    return {name: section for name, section in raw.items() if section}


def _manager(tmp_path: Path, **sections: Any) -> SessionManager:
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, _raw(tmp_path, **sections))

    def factory(profile, agent):
        return ScriptedLLM(lambda system, messages, tools: "Nothing to do.", profile)

    return SessionManager(home, llm_factory=factory)


@pytest.fixture
async def manager(tmp_path):
    made = _manager(tmp_path)
    yield made
    await made.shutdown()


@pytest.fixture
async def make_manager(tmp_path):
    made: list[SessionManager] = []

    def make(**sections: Any) -> SessionManager:
        made.append(_manager(tmp_path, **sections))
        return made[-1]

    yield make
    for each in made:
        await each.shutdown()


async def _create(manager: SessionManager, **args: Any) -> tuple[str, bool]:
    return await call_launcher_tool(manager, "session_create", {"goal": "Goal", "start": False, **args})


async def _overrides(manager: SessionManager, **args: Any) -> dict[str, Any]:
    """What a session created with these arguments holds."""
    before = set(manager.ids())
    text, error = await _create(manager, **args)
    assert not error, text
    sid = (set(manager.ids()) - before).pop()
    return manager.meta(sid)["overrides"]


async def _refused(manager: SessionManager, **args: Any) -> str:
    """The error of a call that must create nothing."""
    before = manager.ids()
    text, error = await _create(manager, **args)
    assert error, text
    assert manager.ids() == before
    return text


# ---- the tool's description ------------------------------------------------------------------------------------
async def test_session_create_declares_team_name_after_team(manager):
    create = next(tool for tool in list_launcher_tools(manager) if tool.name == "session_create")
    props = create.input_schema["properties"]
    assert props["team_name"] == {
        "type": "string",
        "description": "A team saved in the settings instead of team and start_agents: its roles in order; each "
                       "agent's model comes from its role. An unknown name is answered with the saved names."}
    assert list(props).index("team_name") == list(props).index("team") + 1
    assert props["team"]["type"] == "array" and props["team"]["items"] == {"type": "string"}
    assert create.input_schema["required"] == ["goal"]


async def test_no_new_tool_and_no_other_tool_takes_a_team_name(manager):
    assert [tool["name"] for tool in LAUNCHER_TOOLS] == [
        "sessions_list", "session_create", "session_start", "session_stop", "session_cd", "skill_library"]
    tools = list_launcher_tools(manager)
    assert [tool.name for tool in tools if "team_name" in tool.input_schema.get("properties", {})] == ["session_create"]
    assert not [tool.name for tool in tools if "teams" in tool.name]


def test_the_instructions_tell_about_team_name():
    assert ("`session_create(..., team_name=<name>)` starts with a team saved in the settings; an unknown name is "
            "answered with the saved names." in " ".join(INSTRUCTIONS.split()))
    assert INSTRUCTIONS.endswith(HUB_INSTRUCTIONS)


# ---- a team chosen ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["Разработка", "разработка", "  РАЗРАБОТКА ", "Разработка\t"])
async def test_team_name_sets_the_roles_in_order_and_their_number(manager, name):
    assert await _overrides(manager, team_name=name) == {"swarm": {"starting_roles": BUILD, "start_agents": 5}}


async def test_the_session_holds_what_a_literal_team_makes(manager):
    by_name = await _overrides(manager, team_name="Разработка")
    assert by_name == await _overrides(manager, team=BUILD)
    sid = manager.ids()[0]
    text, error = await _create(manager, team_name="Тексты")
    assert not error and "Тексты" not in text
    saved = (manager.home / "sessions" / sid / SESSION_FILE).read_text(encoding="utf-8")
    assert "team_name" not in saved and "Разработка" not in saved
    summary = json.dumps(manager.summary(sid), ensure_ascii=False)
    assert "team_name" not in summary and "Разработка" not in summary
    listed, error = await call_launcher_tool(manager, "sessions_list", {})
    assert not error and "Разработка" not in listed and "Тексты" not in listed


async def test_the_other_arguments_go_along(manager):
    assert await _overrides(manager, team_name="Тексты", max_cost_usd=1.5, language="ru", clarify=True, model="claude",
                            network="off") == {
        "swarm": {"starting_roles": PROSE, "start_agents": 2, "max_cost_usd": 1.5, "language": "ru", "clarify": True,
                  "network": "off"},
        "agents": {"default_model": "claude"}}


async def test_a_started_team_above_max_agents_all_start(manager):
    """As a literal team: max_agents is not raised, and it is for hiring."""
    assert await _overrides(manager, team_name="Разработка", max_agents=2) == {
        "swarm": {"max_agents": 2, "starting_roles": BUILD, "start_agents": 5}}
    assert await _overrides(manager, team=BUILD, max_agents=2) == {
        "swarm": {"max_agents": 2, "starting_roles": BUILD, "start_agents": 5}}
    text, error = await call_launcher_tool(manager, "session_create",
                                           {"goal": "Goal", "team_name": "Разработка", "max_agents": 2})
    assert not error, text
    sid = manager.ids()[-1]
    assert len(manager.swarms[sid].members()) == 5 and manager.summary(sid)["max_agents"] == 2


async def test_each_agent_has_its_roles_model(manager):
    text, error = await call_launcher_tool(manager, "session_create",
                                           {"goal": "Goal", "team_name": "Разработка", "model": "claude"})
    assert not error, text
    members = manager.swarms[manager.ids()[0]].members()
    assert sorted((member["role"], member["model"]) for member in members) == [
        ("Architect", "claude"), ("Generalist", "claude"), ("Tester", "claude"), ("dev_gpt", "gpt"), ("dev_gpt", "gpt")]


async def test_a_team_with_a_hidden_role_starts(make_manager):
    manager = make_manager(teams=[{"name": "Поиск", "roles": ["researcher", "researcher"]}])
    assert await _overrides(manager, team_name="Поиск") == {
        "swarm": {"starting_roles": ["researcher", "researcher"], "start_agents": 2}}


async def test_a_team_named_like_a_role(make_manager):
    own = {"name": "Поиск", "description": "Searches."}
    teams = [{"name": name, "roles": PROSE} for name in ("writer", "поиск", "researcher")]
    manager = make_manager(roles=[*ROLES, own], teams=teams)
    for name in ("WRITER", "Поиск", "Researcher"):
        assert await _overrides(manager, team_name=name) == {"swarm": {"starting_roles": PROSE, "start_agents": 2}}
    # `team` holds roles, and these are roles: a preset, an own role, a hidden one
    literal = ["writer", "Поиск", "researcher"]
    assert await _overrides(manager, team=literal) == {"swarm": {"starting_roles": literal, "start_agents": 3}}


async def test_the_settings_are_read_when_the_session_is_created(manager):
    earlier = await _overrides(manager, team_name="Тексты")
    await manager.update_settings({"teams": [{"name": "Тексты", "roles": ["tester"]}]})
    assert await _overrides(manager, team_name="Тексты") == {"swarm": {"starting_roles": ["tester"], "start_agents": 1}}
    assert manager.meta(manager.ids()[0])["overrides"] == earlier

    # a hand edit of the file counts too
    st.write_raw(manager.home, {**st.read_raw(manager.home), "teams": [{"name": "Новая", "roles": PROSE}]})
    assert await _overrides(manager, team_name="Новая") == {"swarm": {"starting_roles": PROSE, "start_agents": 2}}
    assert await _refused(manager, team_name="Тексты") == "Error: No saved team 'Тексты'. Saved teams: Новая."


# ---- none given ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("absent", ["", None])
async def test_an_empty_team_name_means_none_was_given(manager, absent):
    assert await _overrides(manager, team_name=absent) == {}
    assert await _overrides(manager, team_name=absent, team=["tester"], start_agents=2) == {
        "swarm": {"start_agents": 2, "starting_roles": ["tester"]}}
    assert await _overrides(manager, team_name=absent, start_agents=2) == {"swarm": {"start_agents": 2}}


@pytest.mark.parametrize("unset", [{"team": []}, {"team": None}, {"start_agents": None}, {"start_agents": ""}])
async def test_an_unset_team_or_number_does_not_stand_in_the_way(manager, unset):
    assert await _overrides(manager, team_name="Тексты", **unset) == {
        "swarm": {"starting_roles": PROSE, "start_agents": 2}}


# ---- refused, with nothing created -----------------------------------------------------------------------------
@pytest.mark.parametrize("args", [
    {"team": ["tester"]},
    {"start_agents": 1},
    {"start_agents": 0},
    {"team": ["tester"], "start_agents": 1},
    {"team": ["Тексты"]},
])
async def test_team_name_goes_without_team_and_start_agents(manager, args):
    assert await _refused(manager, team_name="Тексты", **args) == ALONE
    assert await _refused(manager, team_name=5, **args) == ALONE  # the first check of team_name
    assert await _refused(manager, team_name="нет такой", **args) == ALONE


@pytest.mark.parametrize("value", [5, 0, 1.5, True, False, [], ["Тексты"], {"name": "Тексты"}])
async def test_team_name_must_be_a_string(manager, value):
    assert await _refused(manager, team_name=value) == NOT_A_NAME


@pytest.mark.parametrize("name, shown", [
    ("codng", "codng"),
    ("  Not   known ", "Not known"),
    ("writer", "writer"),               # a role is no team: this argument never reaches the catalog
    ("Тексты*2", "Тексты*2"),
    ("Тексты, tester", "Тексты, tester"),
    ("x" * 200, "x" * 80),
    ("   ", ""),
])
async def test_an_unknown_name_is_answered_with_the_saved_names(manager, name, shown):
    assert await _refused(manager, team_name=name) == (
        f"Error: No saved team '{shown}'. Saved teams: Разработка, Тексты.")


async def test_with_none_saved_the_answer_says_where_to_save_one(make_manager):
    manager = make_manager(teams=None)
    assert await _refused(manager, team_name="x") == f"Error: No saved team 'x'. {NONE_SAVED}"


async def test_the_last_team_deleted_team_name_is_still_refused(manager):
    await manager.update_settings({"teams": []})
    assert await _refused(manager, team_name="Тексты") == f"Error: No saved team 'Тексты'. {NONE_SAVED}"


async def test_a_team_that_cannot_start(make_manager):
    broken = ["gone", "tester", "dev_gpt", "other", "gone", "GONE"]
    manager = make_manager(teams=[{"name": "Разработка", "roles": broken}, {"name": "Тексты", "roles": PROSE}])
    assert await _refused(manager, team_name="разработка") == (
        "Error: The team 'Разработка' uses roles that are not in the role catalog: gone, other. "
        "Fix it in Settings > Teams.")
    # the others are not held back, and neither is a save
    assert (await _overrides(manager, team_name="Тексты"))["swarm"]["starting_roles"] == PROSE
    await manager.update_settings({"agents": {"default_model": "gpt"}})
    await manager.update_settings({"roles": [*ROLES, {"name": "gone", "description": "Back."},
                                             {"name": "other", "description": "New."}]})
    assert (await _overrides(manager, team_name="разработка"))["swarm"]["start_agents"] == 6


async def test_a_role_lost_by_a_save_refuses_the_team_when_it_is_chosen(manager):
    await manager.update_settings({"roles": [role for role in ROLES if role["name"] != "dev_gpt"]})
    assert manager.settings_view()["teams"] == TEAMS
    assert await _refused(manager, team_name="Разработка") == (
        "Error: The team 'Разработка' uses roles that are not in the role catalog: dev_gpt. "
        "Fix it in Settings > Teams.")
    assert (await _overrides(manager, team_name="Тексты"))["swarm"]["starting_roles"] == PROSE


@pytest.mark.parametrize("team, shown", [
    (["Тексты"], "Тексты"),
    ([" тексты "], "тексты"),
    (["tester", "РАЗРАБОТКА", "Тексты"], "РАЗРАБОТКА"),
])
async def test_a_saved_team_in_team_is_answered_with_team_name(manager, team, shown):
    answer = f"Error: '{shown}' is a saved team, not a role: pass it as team_name."
    assert await _refused(manager, team=team) == answer
    assert await _refused(manager, team=team, team_name="") == answer


async def test_a_new_role_in_team_is_as_it_was(manager):
    assert await _overrides(manager, team=["Fresh", "Fresh"]) == {
        "swarm": {"starting_roles": ["Fresh", "Fresh"], "start_agents": 2}}


async def test_with_no_team_saved_any_name_in_team_is_a_role(make_manager):
    manager = make_manager(teams=None)
    assert await _overrides(manager, team=["Тексты"]) == {"swarm": {"starting_roles": ["Тексты"], "start_agents": 1}}


@pytest.mark.parametrize("team", ["Тексты", {"name": "Тексты"}])
async def test_team_stays_an_array(manager, team):
    assert await _refused(manager, team=team) == (
        "Error: team must be a list of role names, e.g. [\"architect\", \"developer\", \"tester\"].")


# ---- through an MCP client -------------------------------------------------------------------------------------
async def test_through_an_mcp_client(manager):
    async with Client(build_launcher_server(manager)) as client:
        assert "team_name=<name>" in client.instructions
        tools = await client.list_tools()
        create = next(tool for tool in tools.tools if tool.name == "session_create")
        assert create.input_schema["properties"]["team_name"]["type"] == "string"

        done = await client.call_tool("session_create", {"goal": "Goal", "team_name": "тексты", "start": False})
        assert not done.is_error, done.content[0].text
        assert manager.meta(manager.ids()[0])["overrides"] == {"swarm": {"starting_roles": PROSE, "start_agents": 2}}
        for args, answer in (({"team_name": "нет"}, "Error: No saved team 'нет'. Saved teams: Разработка, Тексты."),
                             ({"team_name": 5}, NOT_A_NAME),
                             ({"team_name": "Тексты", "start_agents": 1}, ALONE)):
            refused = await client.call_tool("session_create", {"goal": "Goal", "start": False, **args})
            assert refused.is_error and refused.content[0].text == answer
        assert len(manager.ids()) == 1
