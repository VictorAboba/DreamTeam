"""Saved teams (docs/SPEC.md §17.3 "Saved teams") in the terminal: `--team` and `/team` take a saved team's name or
roles by one rule, refuse what cannot start before anything runs, and copy a team into the starting roles where it is
chosen."""

from __future__ import annotations

import asyncio
import json
import string
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient
from test_tui_app import launch  # noqa: F401 - a fixture
from test_tui_commands import HubApp, settings_view

from dreamteam import cli, roles, tui
from dreamteam import settings as st
from dreamteam.config import CONFIG_FILE
from dreamteam.launcher import create_launcher_app
from dreamteam.sessions import SESSION_FILE, SessionManager
from dreamteam.tui import render
from dreamteam.roles import TEAM_RESET
from dreamteam.tui.commands import execute, find, session_overrides
from dreamteam.tui.i18n import TEXTS

ROLES = [{"name": "dev_gpt", "description": "Backend.", "model": "gpt"}, {"name": "researcher", "hidden": True},
         {"name": "Скрытая", "description": "Hidden.", "hidden": True}]
CODING = ["architect", "dev_gpt", "dev_gpt", "tester"]
TEAMS = [{"name": "Разработка", "roles": CODING},
         {"name": "Тексты", "roles": ["writer", "reviewer"]},
         {"name": "Сломанная", "roles": ["developer", "gone", "Другая", "GONE"]}]
SAVED_NAMES = "Разработка, Тексты, Сломанная"
MODES = [pytest.param(["Goal"], id="interactive"), pytest.param(["-p", "Goal"], id="print")]


def _raw(tmp_path: Path, teams: list[dict] | None) -> dict[str, Any]:
    profile = {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}
    raw = {"swarm": {"name": "Home", "language": "en", "start_agents": 1, "max_agents": 3, "max_cost_usd": 2.0},
           "mcp": {"library_dir": str(tmp_path / "library")},
           "models": {"default": dict(profile), "gpt": dict(profile)},
           "roles": ROLES}
    return {**raw, "teams": teams} if teams else raw


def _home(tmp_path: Path, teams: list[dict] | None = TEAMS) -> list[str]:
    """A home with these saved teams, as the arguments that name it."""
    home = tmp_path / "home"
    home.mkdir()
    st.write_raw(home, _raw(tmp_path, teams))
    return ["--home", str(home)]


@pytest.fixture
def seen(monkeypatch) -> dict[str, Any]:
    """What the TUI or print mode got (empty: neither ran)."""
    got: dict[str, Any] = {}
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(cli, "_piped_stdin", lambda: "")
    monkeypatch.setattr(tui, "main_tui", lambda home, **kw: got.update(**kw) or 0)
    monkeypatch.setattr(tui, "main_print", lambda home, prompt, **kw: got.update(**kw) or 0)
    return got


def _sections(seen: dict[str, Any]) -> dict[str, dict]:
    return session_overrides(seen["overrides"])[0]


# ---- --team: what starts ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("word", ["Разработка", "разработка", "  РАЗРАБОТКА "])
def test_a_saved_team_by_name_goes_as_its_list_of_roles(tmp_path, seen, mode, word):
    assert cli.main([*_home(tmp_path), *mode, "--team", word]) == 0
    assert seen["overrides"]["team"] == CODING
    assert _sections(seen) == {"swarm": {"starting_roles": CODING, "start_agents": 4}}


def test_a_team_of_hidden_roles_can_start(tmp_path, seen):
    team = ["researcher", "Скрытая"]
    assert cli.main([*_home(tmp_path, [{"name": "Поиск", "roles": team}]), "Goal", "--team", "Поиск"]) == 0
    assert seen["overrides"]["team"] == team


def test_a_saved_team_keeps_its_trailing_generalists(tmp_path, seen):
    team = ["writer", "generalist", "generalist"]
    assert cli.main([*_home(tmp_path, [{"name": "Два", "roles": team}]), "Goal", "--team", "Два"]) == 0
    assert _sections(seen) == {"swarm": {"starting_roles": team, "start_agents": 3}}


@pytest.mark.parametrize("flags, expected", [
    (["--max-agents", "9"], {"swarm": {"starting_roles": CODING, "start_agents": 4, "max_agents": 9}}),
    # below the team's size: as with a literal team
    (["--max-agents", "2"], {"swarm": {"starting_roles": CODING, "start_agents": 4, "max_agents": 2}}),
    (["--model", "gpt"], {"swarm": {"starting_roles": CODING, "start_agents": 4},
                          "agents": {"default_model": "gpt"}}),
])
def test_other_flags_pass_through_with_a_team(tmp_path, seen, flags, expected):
    assert cli.main([*_home(tmp_path), "Goal", "--team", "Разработка", *flags]) == 0
    assert _sections(seen) == expected


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("text, expected", [
    ("architect, developer*2", ["architect", "developer", "developer"]),
    ("tester", ["tester"]),                 # a catalog role
    ("DEV_GPT", ["DEV_GPT"]),               # an own one
    ("Researcher", ["Researcher"]),         # hidden means not offered: it still is a role
    ("скрытая", ["скрытая"]),               # the same for an own one
    ("codng*1", ["codng"]),                 # a new role, asked for
    ("tstr, tester", ["tstr", "tester"]),   # a list is roles: its new names are new roles
])
def test_roles_go_as_they_were_typed(tmp_path, seen, mode, text, expected):
    assert cli.main([*_home(tmp_path), *mode, "--team", text]) == 0
    assert seen["overrides"]["team"] == text
    assert _sections(seen) == {"swarm": {"starting_roles": expected, "start_agents": len(expected)}}


def test_agents_stay_valid_with_a_role(tmp_path, seen):
    assert cli.main([*_home(tmp_path), "-p", "Goal", "--team", "tester", "--agents", "2"]) == 0
    assert _sections(seen) == {"swarm": {"starting_roles": ["tester"], "start_agents": 2}}


def test_a_team_named_like_a_role_is_the_bare_word_and_the_role_is_still_reached(tmp_path, seen):
    base = _home(tmp_path, [{"name": "writer", "roles": ["tester", "tester"]}])
    assert cli.main([*base, "Goal", "--team", "WRITER"]) == 0
    assert seen["overrides"]["team"] == ["tester", "tester"]
    assert cli.main([*base, "Goal", "--team", "writer*1"]) == 0
    assert _sections(seen)["swarm"]["starting_roles"] == ["writer"]
    assert cli.main([*base, "Goal", "--team", "writer, tester"]) == 0   # in a list it is the role, not a saved team
    assert _sections(seen)["swarm"]["starting_roles"] == ["writer", "tester"]


def test_with_continue_the_flag_works_as_before(tmp_path, seen):
    base = _home(tmp_path)
    assert cli.main([*base, "-p", "Goal", "-c", "--team", "tester"]) == 0   # print mode ignores it there
    seen.clear()
    assert cli.main([*base, "-c", "--team", "Тексты"]) == 0                 # for the later /new of the window
    assert seen["continue_last"] is True and seen["overrides"]["team"] == ["writer", "reviewer"]


@pytest.mark.parametrize("which", [["-c"], ["-r", "abc"]])
@pytest.mark.parametrize("flags", [["--team", "QA"], ["--team", "Сломанная"], ["--team", "Тексты, tester"],
                                   ["--team", "Тексты", "--agents", "2"]])
def test_print_mode_that_continues_a_session_looks_up_no_team(tmp_path, seen, capsys, which, flags):
    """It never reads the team, so nothing about the team can stop it, and nothing is said about it."""
    assert cli.main([*_home(tmp_path), "-p", "Goal", *which, *flags]) == 0
    assert seen["overrides"]["team"] == flags[1] and capsys.readouterr().err == ""


@pytest.mark.parametrize("which", [["-c"], ["-r", "abc"]])
def test_print_mode_that_continues_a_session_still_checks_the_syntax(tmp_path, seen, capsys, which):
    assert cli.main([*_home(tmp_path), "-p", "Goal", *which, "--team", "tester*0"]) == 2
    assert capsys.readouterr().err.strip() == ("dreamteam: --team: 'tester*0': the count must be a whole number from 1 "
                                               "to 50")
    assert seen == {}


def test_print_mode_with_a_bare_resume_makes_a_new_session_so_the_team_counts(tmp_path, seen, capsys):
    base = _home(tmp_path)
    assert cli.main([*base, "-p", "Goal", "-r", "--team", "Тексты"]) == 0
    assert seen["overrides"]["team"] == ["writer", "reviewer"]
    seen.clear()
    assert cli.main([*base, "-p", "Goal", "-r", "--team", "QA"]) == 2
    assert "no saved team or role 'QA'" in capsys.readouterr().err and seen == {}


def test_help_names_a_saved_team(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")   # argparse wraps to the terminal, and breaks a long word in a narrow one
    choices = next(iter(cli.build_parser()._subparsers._group_actions)).choices
    help_text = " ".join(choices["tui"].format_help().split())
    assert ('--team TEAM starting team of a new session: roles in order, ROLE*N for several, e.g. '
            '"architect,developer*2,tester", or the name of a team saved in the settings') in help_text


# ---- --team: what is refused, before anything runs ---------------------------------------------------------------
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("flags, why", [
    (["--team", "codng"],
     f"no saved team or role 'codng' (there are: {SAVED_NAMES}). For a new role with no description write 'codng*1'"),
    (["--team", "tster"],
     f"no saved team or role 'tster' (there are: Tester, {SAVED_NAMES}). For a new role with no description write "
     "'tster*1'"),
    (["--team", "Тексты, tester"], "'Тексты' is a saved team: choose it whole, with no other roles and no *N"),
    (["--team", "тексты*2"], "'тексты' is a saved team: choose it whole, with no other roles and no *N"),
    (["--team", "Сломанная"],
     "the team 'Сломанная' uses roles that are not in the role catalog: gone, Другая. Fix it in Settings > Teams"),
    (["--team", "Тексты", "--agents", "2"], "the team 'Тексты' sets the number of agents: --agents cannot go with it"),
    (["--team", "Тексты", "--agents", "0"], "the team 'Тексты' sets the number of agents: --agents cannot go with it"),
    (["--team", "dev_gp"],
     f"no saved team or role 'dev_gp' (there are: dev_gpt, {SAVED_NAMES}). For a new role with no description write "
     "'dev_gp*1'"),
    (["--team", "Тексты*0"], "'Тексты*0': the count must be a whole number from 1 to 50"),
    (["--team", " , "], "The team is empty"),
])
def test_refused_with_the_exact_text(tmp_path, seen, capsys, mode, flags, why):
    assert cli.main([*_home(tmp_path), *mode, *flags]) == 2
    assert capsys.readouterr().err.strip() == f"dreamteam: --team: {why}"
    assert seen == {}


def test_a_role_and_a_team_of_one_name_are_listed_once(tmp_path, seen, capsys):
    assert cli.main([*_home(tmp_path, [{"name": "Tester", "roles": ["writer"]}]), "Goal", "--team", "tster"]) == 2
    assert "(there are: Tester). For a new role" in capsys.readouterr().err
    assert seen == {}


# ---- --team: a word that is a role and a saved team --------------------------------------------------------------
@pytest.mark.parametrize("agents", ["2", "60", "-1"])
def test_a_team_named_like_a_role_refuses_agents_and_says_how_to_reach_the_role(tmp_path, seen, capsys, agents):
    base = _home(tmp_path, [{"name": "Tester", "roles": ["writer", "writer"]}])
    assert cli.main([*base, "-p", "Goal", "--team", "tester", "--agents", agents]) == 2
    assert capsys.readouterr().err.strip() == ("dreamteam: --team: the team 'Tester' sets the number of agents: "
                                               "--agents cannot go with it. For the role write 'tester*1' and keep "
                                               "--agents")
    assert seen == {}


def test_the_way_to_a_role_named_like_a_team_starts_what_the_word_meant_before(tmp_path, seen, capsys):
    """One tester, and generalists beyond the list, as `--team tester --agents 2` is with no such team."""
    base = _home(tmp_path, [{"name": "Tester", "roles": ["writer", "writer"]}])
    assert cli.main([*base, "-p", "Goal", "--team", "tester*1", "--agents", "2"]) == 0
    assert _sections(seen) == {"swarm": {"starting_roles": ["tester"], "start_agents": 2}}
    assert cli.main([*base, "-p", "Goal", "--team", "tester*2"]) == 0
    assert _sections(seen) == {"swarm": {"starting_roles": ["tester", "tester"], "start_agents": 2}}
    # the bare word is the team, with no word about it: that is the rule
    assert cli.main([*base, "-p", "Goal", "--team", "tester"]) == 0
    assert seen["overrides"]["team"] == ["writer", "writer"]
    assert capsys.readouterr().err == ""


# ---- --team: a word this home does not know ------------------------------------------------------------------------
def test_a_bare_new_word_names_the_home_it_was_looked_up_in(tmp_path, seen, capsys):
    base = _home(tmp_path, None)
    assert cli.main([*base, "-p", "Goal", "--team", "coding"]) == 0
    assert seen["overrides"]["team"] == "coding"
    out = capsys.readouterr()
    assert out.out == "" and out.err.strip() == (f"dreamteam: --team: no role or saved team 'coding' in {base[1]}: "
                                                 "starting a new role with no description (write 'coding*1' to mean "
                                                 "that)")


def test_the_home_in_the_current_folder_is_the_one_a_team_is_looked_up_in(tmp_path, seen, capsys, monkeypatch):
    """The teams live in $DREAMTEAM_HOME, and the current folder has a home of its own: the run goes on, and says so."""
    monkeypatch.setenv("DREAMTEAM_HOME", _home(tmp_path)[1])
    project = tmp_path / "project"
    project.mkdir()
    st.write_raw(project, _raw(tmp_path, None))
    monkeypatch.chdir(project)
    assert cli.main(["-p", "Goal", "--team", "Тексты"]) == 0
    assert seen["overrides"]["team"] == "Тексты"
    assert f"no role or saved team 'Тексты' in {project}" in capsys.readouterr().err


@pytest.mark.parametrize("text", ["tester", "DEV_GPT", "скрытая", "QA*1", "QA*2", "QA, tester", "tester, developer"])
def test_a_role_or_a_list_needs_no_note(tmp_path, seen, capsys, text):
    assert cli.main([*_home(tmp_path, None), "-p", "Goal", "--team", text]) == 0
    assert seen["overrides"]["team"] == text and capsys.readouterr().err == ""


# ---- --team: homes with no saved team, and settings that cannot be read ---------------------------------------------
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("flags, section", [
    (["--team", "QA"], {"starting_roles": ["QA"], "start_agents": 1}),
    (["--team", "QA", "--agents", "2"], {"starting_roles": ["QA"], "start_agents": 2}),
])
def test_a_home_with_no_saved_team_takes_a_bare_word_as_a_new_role(tmp_path, seen, mode, flags, section):
    assert cli.main([*_home(tmp_path, None), *mode, *flags]) == 0
    assert seen["overrides"]["team"] == "QA"
    assert _sections(seen) == {"swarm": section}


def test_a_home_not_made_yet_is_a_home_with_no_saved_team(tmp_path, seen):
    fresh = tmp_path / "fresh"
    for word in ("tester", "QA"):
        assert cli.main(["--home", str(fresh), "Goal", "--team", word]) == 0
        assert seen["overrides"]["team"] == word
    assert not (fresh / CONFIG_FILE).exists()


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("text, reason", [
    ("[swarm]\nnope = 1\n", "Unknown keys for [swarm]: nope"),
    ("[[teams]]\nname = 'x'\nroles = []\n", "[teams.1] the team 'x' has no roles"),
    ("[swarm\n", None),   # not TOML: the reason is the parser's
])
def test_settings_that_cannot_be_read_refuse_a_bare_word_and_pass_a_list(tmp_path, seen, capsys, mode, text, reason):
    base = _home(tmp_path)
    (Path(base[1]) / CONFIG_FILE).write_text(text, encoding="utf-8")
    assert cli.main([*base, *mode, "--team", "  Тексты "]) == 2
    err = capsys.readouterr().err.strip()
    head = "dreamteam: --team: cannot read the settings to look up 'Тексты': "
    if reason:
        assert err == head + reason
    else:
        assert err.startswith(head) and len(err) > len(head)
    assert seen == {}
    for team in ("tester, developer", "QA*2"):
        assert cli.main([*base, *mode, "--team", team]) == 0
        assert seen["overrides"]["team"] == team
    seen.clear()
    assert cli.main([*base, *mode, "--team", "x*0"]) == 2   # the syntax comes first and needs no settings
    assert capsys.readouterr().err.strip() == "dreamteam: --team: 'x*0': the count must be a whole number from 1 to 50"
    assert seen == {}


@pytest.mark.parametrize("word", ["tester", "Тестировщик", " DEVELOPER "])
def test_a_built_in_role_needs_no_settings_to_be_read(tmp_path, seen, capsys, word):
    """A running hub ignores a broken hand edit and serves its last valid settings, so the session can start."""
    base = _home(tmp_path)
    (Path(base[1]) / CONFIG_FILE).write_text("[swarm\n", encoding="utf-8")
    assert cli.main([*base, "-p", "Goal", "--team", word]) == 0
    assert seen["overrides"]["team"] == word and capsys.readouterr().err == ""


def test_a_settings_file_that_cannot_be_opened(tmp_path, seen, capsys):
    """A directory where the file should be: OSError, on every system."""
    base = _home(tmp_path)
    path = Path(base[1]) / CONFIG_FILE
    path.unlink()
    path.mkdir()
    assert cli.main([*base, "-p", "Goal", "--team", "Тексты"]) == 2
    assert capsys.readouterr().err.startswith("dreamteam: --team: cannot read the settings to look up 'Тексты': ")
    assert seen == {}
    assert cli.main([*base, "-p", "Goal", "--team", "tester, developer"]) == 0
    assert seen["overrides"]["team"] == "tester, developer"


# ---- /team -----------------------------------------------------------------------------------------------------------
def _app(teams: list[dict] | None = TEAMS, *, lang: str = "en") -> HubApp:
    """A window on a hub whose settings hold these teams; app.view is what it answers, for a test to edit."""
    app = HubApp()
    app.ctx = render.RenderContext(lang=lang)
    app.view = settings_view()
    app.view["settings"].update({"teams": list(teams or []), "roles": ROLES})

    async def settings():
        return app.view

    app.client.settings = settings
    return app


def _flat(text: str) -> str:
    return " ".join(text.split())


async def _refused(app: HubApp, line: str, text: str) -> None:
    before = json.loads(json.dumps(app.overrides))
    await execute(app, line)
    assert _flat(app.out[-1]) == f"⚠ {text}"
    assert app.overrides == before


async def test_slash_team_picks_a_saved_team_by_name_and_case():
    app = _app()
    await execute(app, "/team  разработка ")
    assert app.overrides == {"swarm": {"starting_roles": CODING, "start_agents": 4}}
    assert _flat(app.out[-1]).endswith("New sessions from this window start with the team “Разработка”: "
                                       "Architect ×1 · dev_gpt ×2 · Tester ×1 (4 in all)")


async def test_slash_team_copies_the_team_where_it_is_chosen():
    app = _app()
    await execute(app, "/team Разработка")
    window = app.overrides["swarm"]["starting_roles"]
    assert window == CODING and window is not TEAMS[0]["roles"]
    app.view["settings"]["teams"] = [{"name": "Разработка", "roles": ["writer"]}]
    await execute(app, "/team")
    assert "Architect ×1 · dev_gpt ×2 · Tester ×1 (4 in all)" in _flat(app.out[-2])   # the copy it holds
    assert app.overrides["swarm"]["starting_roles"] == CODING
    await execute(app, "/team Разработка")
    assert app.overrides == {"swarm": {"starting_roles": ["writer"], "start_agents": 1}}


async def test_slash_team_without_an_argument_lists_the_saved_teams():
    app = _app()
    await execute(app, "/team")
    assert "Starting team of new sessions: Architect ×1 · Generalist ×2 (3 in all)" in _flat(app.out[-2])
    assert _flat(app.out[-1]).endswith(f"Saved teams: Разработка (4), Тексты (2), Сломанная (4). "
                                       "To choose one: /team Разработка")
    assert app.overrides == {}
    await execute(app, "/team tester")
    await execute(app, "/team")
    assert "New sessions from this window start with: Tester ×1 (1 in all)" in _flat(app.out[-2])
    assert "Saved teams:" in _flat(app.out[-1])


@pytest.mark.parametrize("line, expected", [
    ("/team developer*2, tester", ["developer", "developer", "tester"]),
    ("/team tester", ["tester"]),
    ("/team DEV_GPT", ["DEV_GPT"]),             # an own role
    ("/team Researcher", ["Researcher"]),       # hidden: not offered, still a role
    ("/team скрытая", ["скрытая"]),             # the same for an own one
    ("/team codng*1", ["codng"]),
    ("/team tstr, tester", ["tstr", "tester"]),
])
async def test_slash_team_takes_roles(line, expected):
    app = _app()
    await execute(app, line)
    assert app.overrides == {"swarm": {"starting_roles": expected, "start_agents": len(expected)}}
    assert "New sessions from this window start with: " in _flat(app.out[-1])


async def test_slash_team_refuses_and_changes_nothing():
    app = _app()
    await execute(app, "/team tester")
    await _refused(app, "/team Тексты*2", "“Тексты” is a saved team: choose it whole, with no other roles and no *N: "
                                          "/team Тексты")
    await _refused(app, "/team тексты, x", "“тексты” is a saved team: choose it whole, with no other roles and no *N: "
                                           "/team тексты")
    await _refused(app, "/team codng", f"No team or role “codng”. There are: {SAVED_NAMES}. A new role with no "
                                       "description: /team codng*1")
    await _refused(app, "/team tster", f"No team or role “tster”. There are: Tester, {SAVED_NAMES}. A new role with no "
                                       "description: /team tster*1")
    await _refused(app, "/team dev_gp", f"No team or role “dev_gp”. There are: dev_gpt, {SAVED_NAMES}. A new role with "
                                        "no description: /team dev_gp*1")
    await _refused(app, "/team Сломанная", "The team “Сломанная” uses roles that are not in the catalog: gone, Другая. "
                                           "Fix it in the settings or choose another.")
    await _refused(app, "/team x*0", "'x*0': the count must be a whole number from 1 to 50. Example: /team architect, "
                                     "developer*2, tester")
    assert app.overrides == {"swarm": {"starting_roles": ["tester"], "start_agents": 1}}


async def test_slash_team_lists_a_role_and_a_team_of_one_name_once():
    app = _app([{"name": "Tester", "roles": ["writer"]}])
    await _refused(app, "/team tster", "No team or role “tster”. There are: Tester. A new role with no description: "
                                       "/team tster*1")


async def test_slash_team_takes_a_quoted_name():
    app = _app([{"name": "Моя команда", "roles": ["writer"]}])
    await execute(app, '/team "моя команда"')
    assert app.overrides == {"swarm": {"starting_roles": ["writer"], "start_agents": 1}}
    await execute(app, "/team 'Моя команда'")
    assert "“Моя команда”" in app.out[-1]
    await _refused(app, '/team "нет такой"', "No team or role “нет такой”. There are: Моя команда. A new role with no "
                                             "description: /team нет такой*1")


@pytest.mark.parametrize("line, team", [('/team "architect", "tester"', ["architect", "tester"]),
                                        ('''/team 'architect', "tester"*2''', ["architect", '"tester"', '"tester"']),
                                        ('/team "architect, tester*2"', ["architect", "tester", "tester"])])
async def test_slash_team_takes_quotes_around_each_item_or_the_whole_list(line, team):
    app = _app([{"name": "Разработка", "roles": ["writer"]}])
    await execute(app, line)
    assert app.overrides == {"swarm": {"starting_roles": team, "start_agents": len(team)}}


async def test_quoted_saved_teams_in_a_list_are_refused_as_unquoted_ones_are():
    app = _app([{"name": "Разработка", "roles": ["writer"]}, {"name": "Тексты", "roles": ["tester"]}])
    await _refused(app, '/team "Разработка", "Тексты"', "“Разработка” is a saved team: choose it whole, with no other "
                                                        "roles and no *N: /team Разработка")


async def test_slash_team_takes_a_quoted_word_in_a_home_with_no_team():
    app = _app(None)
    await execute(app, '/team "QA"')
    assert app.overrides == {"swarm": {"starting_roles": ["QA"], "start_agents": 1}}


async def test_a_team_of_a_hidden_role_can_start():
    team = ["researcher", "Скрытая"]
    app = _app([{"name": "Поиск", "roles": team}])
    await execute(app, "/team Поиск")
    assert app.overrides == {"swarm": {"starting_roles": team, "start_agents": 2}}


@pytest.mark.parametrize("word", TEAM_RESET)
async def test_slash_team_reset_words_come_first_and_leave_other_overrides(word):
    app = _app()
    await execute(app, "/team Разработка")
    app.overrides["swarm"]["max_agents"] = 9
    await execute(app, f"/team {word}")
    assert app.overrides == {"swarm": {"max_agents": 9}}
    assert "comes from the settings" in app.out[-1]
    del app.overrides["swarm"]["max_agents"]
    await execute(app, "/team Разработка")
    await execute(app, f"/team {word.upper()}")
    assert "max_agents" not in app.overrides.get("swarm", {}) and "starting_roles" not in app.overrides.get("swarm", {})


@pytest.mark.parametrize("teams", [TEAMS, None], ids=["with teams", "with none"])
@pytest.mark.parametrize("text", ['"reset"', "'default'", '"-"', '"Сброс"'])
async def test_slash_team_takes_a_quoted_reset_word_as_the_word(teams, text):
    app = _app(teams)
    await execute(app, "/team tester")
    await execute(app, f"/team {text}")
    assert app.overrides == {} and "comes from the settings" in app.out[-1]


async def test_the_view_without_teams_or_roles_behaves_as_before():
    app = HubApp()   # settings_view(): no "teams", no "roles"
    await execute(app, "/team QA")
    assert app.overrides == {"swarm": {"starting_roles": ["QA"], "start_agents": 1}}
    assert "New sessions from this window start with: QA ×1 (1 in all)" in _flat(app.out[-1])
    await execute(app, "/team")
    assert "New sessions from this window start with: QA ×1" in _flat(app.out[-1]) and "Saved" not in app.text
    await execute(app, "/team -")
    await execute(app, "/team")
    assert "Starting team of new sessions" in _flat(app.out[-1]) and "Saved" not in app.text


async def test_no_saved_team_and_empty_teams_are_the_same():
    app = _app(None)
    await execute(app, "/team QA")
    assert app.overrides == {"swarm": {"starting_roles": ["QA"], "start_agents": 1}}
    await execute(app, "/team")
    assert "Saved" not in app.text


# ---- the texts -------------------------------------------------------------------------------------------------------
async def test_slash_team_in_russian():
    app = _app(lang="ru")
    await execute(app, "/team")
    assert _flat(app.out[-1]).endswith("Сохранённые команды: Разработка (4), Тексты (2), Сломанная (4). "
                                       "Выбрать: /team Разработка")
    await execute(app, "/team Тексты")
    assert _flat(app.out[-1]).endswith("Новые сессии из этого окна стартуют с командой «Тексты»: "
                                       "Технический писатель ×1 · Ревьюер ×1 — всего 2")
    await execute(app, "/team tester")   # a roster, in the words of the starting team: no saved team is involved here
    assert _flat(app.out[-1]).endswith("Стартовая команда новых сессий из этого окна: Тестировщик ×1 — всего 1")
    await _refused(app, "/team Тексты*2", "«Тексты» — сохранённая команда: её выбирают целиком, без других ролей и без "
                                          "*N: /team Тексты")
    await _refused(app, "/team codng", f"Нет команды или роли «codng». Есть: {SAVED_NAMES}. Новая роль без описания: "
                                       "/team codng*1")
    await _refused(app, "/team тестировщек", f"Нет команды или роли «тестировщек». Есть: Тестировщик, {SAVED_NAMES}. "
                                             "Новая роль без описания: /team тестировщек*1")
    await _refused(app, "/team Сломанная", "В команде «Сломанная» есть роли, которых нет в каталоге: gone, Другая. "
                                           "Исправьте её в настройках или выберите другую.")


def test_the_new_texts_exist_in_both_languages_with_the_same_fields():
    fields = {"team_picked": {"name", "team", "n"}, "team_saved": {"teams", "example"},
              "team_unknown": {"name", "known"}, "team_mixed": {"name"}, "team_broken": {"name", "roles"}}
    for key, names in fields.items():
        for lang in ("ru", "en"):
            assert {f for _, f, _, _ in string.Formatter().parse(TEXTS[lang][key]) if f} == names, (lang, key)


def test_the_usage_line():
    assert find("team").usage("en") == "/team [team | role*N, …]"
    assert find("team").usage("ru") == "/team [команда | роль*N, …]"


# ---- with the real settings view and session store (no port) ---------------------------------------------------------
def test_both_surfaces_make_the_record_of_a_literal_team(tmp_path, seen):
    base = _home(tmp_path)
    home = Path(base[1])
    manager = SessionManager(home)
    head = {"content-type": "application/json"}

    def create(c: TestClient, overrides: dict) -> dict:
        body = {"goal": "G", "start": False, "overrides": overrides}
        response = c.post("/api/sessions", content=json.dumps(body), headers=head)
        assert response.status_code == 201, response.text
        return response.json()["session"]

    with TestClient(create_launcher_app(manager), base_url="http://127.0.0.1:8765") as c:
        app = HubApp()

        async def settings():
            return c.get("/api/settings").json()

        app.client.settings = settings
        asyncio.run(execute(app, "/team разработка"))
        window = create(c, app.overrides)
        assert cli.main([*base, "Goal", "--team", "разработка"]) == 0
        flag = create(c, _sections(seen))
        record = {"swarm": {"starting_roles": CODING, "start_agents": 4}}
        for sid in (window["id"], flag["id"]):
            assert manager.meta(sid)["overrides"] == record
            stored = (home / "sessions" / sid / SESSION_FILE).read_text(encoding="utf-8")
            assert "Разработка" not in stored and "team_name" not in stored
        # a hand edit of the file is what the next choice reads, on both surfaces
        st.write_raw(home, _raw(tmp_path, [{"name": "Новая", "roles": ["tester"]}]))
        asyncio.run(execute(app, "/team Новая"))
        assert app.overrides == {"swarm": {"starting_roles": ["tester"], "start_agents": 1}}
        assert cli.main([*base, "Goal", "--team", "Новая"]) == 0
        assert seen["overrides"]["team"] == ["tester"]
        c.portal.call(manager.shutdown)


# ---- on a running hub ------------------------------------------------------------------------------------------------
def _hand_edit(tmp_path: Path, teams: list[dict]) -> None:
    raw = st.read_raw(tmp_path / "home")
    st.write_raw(tmp_path / "home", {**raw, "teams": teams})


async def test_a_window_starts_the_team_it_picked(launch, tmp_path):
    conn = await launch()
    _hand_edit(tmp_path, [{"name": "Тексты", "roles": ["writer", "reviewer"]}])   # read by the hub at the next look
    h = launch.tui(conn)
    await h.until(lambda: h.tui.ready.is_set())
    await h.run("/team", "Saved teams: Тексты (2). To choose one: /team Тексты")
    await h.run("/team тексты", "New sessions from this window start with the team “Тексты”: Technical writer ×1")
    h.type("Check the parser\r")
    await h.until(lambda: h.tui.attached and len(h.tui.members) == 2, 30, "the team of two")
    agents = (await conn.client.state(h.tui.sid))["agents"]
    assert sorted(a["role"] for a in agents) == ["Reviewer", "Technical writer"]
    meta = await conn.client.session(h.tui.sid)
    assert meta["session"]["overrides"]["swarm"] == {"starting_roles": ["writer", "reviewer"], "start_agents": 2}
    assert await h.quit() == 0


async def test_a_window_opened_with_the_flag_starts_the_team(launch, tmp_path, seen):
    """The whole chain: what the real `--team NAME` hands the TUI is what a real TUI starts."""
    conn = await launch()
    _hand_edit(tmp_path, [{"name": "Тексты", "roles": ["writer", "reviewer"]}])
    assert cli.main(["--home", str(tmp_path / "home"), "--team", "тексты"]) == 0
    assert seen["overrides"]["team"] == ["writer", "reviewer"]
    h = launch.tui(conn, overrides=seen["overrides"])
    await h.until(lambda: h.tui.ready.is_set())
    h.type("Check the parser\r")
    await h.until(lambda: h.tui.attached and len(h.tui.members) == 2, 30, "the team of two")
    agents = (await conn.client.state(h.tui.sid))["agents"]
    assert sorted(a["role"] for a in agents) == ["Reviewer", "Technical writer"]
    assert await h.quit() == 0
