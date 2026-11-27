"""The options a new session is created with, as session overrides (docs/SPEC.md §21.4 "Settings"): the starting
team, the model profile and the limits a caller gives. One mapping for the launcher MCP's `session_create` (§18.2)
and the HTTP API v1's create (§21.4): `overrides_from(body, config, strict=...)`. Strict (v1) refuses what the launcher
MCP lets through for the settings' own check: a role the catalog does not have (the board invents a role; a
program's typo should fail), a model profile the settings lack, a value of the wrong kind; each refusal is an
OptionError with its v1 code. Stdlib and the role catalog only: never imports dreamteam.tui."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Mapping

from . import roles
from .tools import MODES

if TYPE_CHECKING:
    from .config import Config

LANGUAGES = ("ru", "en")
NETWORKS = ("on", "off")
GOAL_RULES = ("democracy", "veto")   # forum.RULES (docs/SPEC.md §22.4), kept here: no import of the forum
GOAL_RULE_BOARD = "goal_rule applies to a thread session: give it with mode \"thread\"."
NAMES_SHOWN = 20   # saved teams, model profiles or roles named in a refusal


class OptionError(ValueError):
    """A create option that cannot be used: `code` is the HTTP API v1's (docs/SPEC.md §21.1: `bad_request`,
    `unknown_team`, `unknown_model`, `unknown_role`), `status` 400. The launcher MCP answers its text as a ToolError."""

    def __init__(self, code: str, text: str):
        super().__init__(text)
        self.code = code
        self.status = 400


def _names(items: list[str]) -> str:
    shown = ", ".join(items[:NAMES_SHOWN]) + (", …" if len(items) > NAMES_SHOWN else "")
    return shown or "(none)"


def _given(body: Mapping[str, Any], key: str) -> bool:
    """`key` is set: present and neither null nor "" (both mean "not given", as the launcher MCP takes them)."""
    return body.get(key) not in (None, "")


def _int_or_none(value: Any) -> int | None:
    """The launcher MCP's number: anything int() takes ("3" too)."""
    try:
        return None if value in (None, "") else int(value)
    except (TypeError, ValueError):
        raise OptionError("bad_request", f"Expected an integer, got {value!r}.") from None


def _team(value: Any) -> list[str]:
    """A list team: role names in order (normalized; [] when absent or empty)."""
    if value is None:
        return []
    names = [roles.normalize(x) for x in value] if isinstance(value, list) and all(
        isinstance(x, str) for x in value) else None
    if names is None or not all(names):
        raise OptionError("bad_request",
                          "team must be a list of role names, e.g. [\"architect\", \"developer\", \"tester\"].")
    if len(names) > roles.TEAM_MAX:
        raise OptionError("bad_request", f"A team has at most {roles.TEAM_MAX} agents.")
    long = next((name for name in names if len(name) > roles.NAME_LIMIT), None)
    if long is not None:
        raise OptionError("bad_request",
                          f"'{long[:80]}': a role name cannot be longer than {roles.NAME_LIMIT} characters.")
    # "developer*2" / "architect, developer" is the --team syntax: here each item is one agent's role
    joined = next((name for name in names if "," in name or "*" in name), None)
    if joined is not None:
        raise OptionError("bad_request", f"'{joined}': a role name cannot contain ',' or '*'; give one item per agent "
                                         "and repeat a name for several agents, e.g. [\"developer\", \"developer\"].")
    return names


def _saved_team(name: Any, config: "Config", *, strict: bool, mode: str = "board") -> list[str]:
    """The roles of the saved team `name` (docs/SPEC.md §17.3 "Saved teams"), checked now: the session holds them as
    any other starting team and never looks the team up."""
    if not isinstance(name, str):
        raise OptionError("bad_request", "team_name must be the name of a saved team.")
    team = roles.find_team(config.teams, name)
    if team is None:
        known = ", ".join(saved.name for saved in config.teams)
        raise OptionError("unknown_team", f"No saved team '{roles.normalize(name)[:80]}'. "
                          + (f"Saved teams: {known}." if known else "None are saved yet: Settings > Teams."))
    missing = roles.missing_roles(team, config.roles, mode)
    if missing:
        raise OptionError("unknown_role" if strict else "bad_request",
                          f"The team '{team.name}' uses roles that are not in the role catalog: "
                          f"{', '.join(missing)}. Fix it in Settings > Teams.")
    return list(team.roles)


def _strict_count(body: Mapping[str, Any], key: str) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OptionError("bad_request", f"'{key}' must be a whole number of at least 1.")
    return value


def _strict_cost(value: Any) -> float | None:
    """A budget in USD as a float: a JSON number (not a bool) that is finite as a float and at least 0, else None. A
    whole number too large for a float (10**400) is None too, never an OverflowError."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _strict_choice(body: Mapping[str, Any], key: str, choices: tuple[str, ...]) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if value not in choices:
        raise OptionError("bad_request", f"'{key}' must be one of {', '.join(choices)}.")
    return value


def _strict_roles(names: list[str], config: "Config", language: str, mode: str = "board") -> None:
    """unknown_role for a name the catalog does not resolve in the session's `mode` (roles.find_role: an id, a
    preset's name in either language, an own role's name; hidden roles count; "Critic" is the reviewer in a thread
    session only), with the saved team a name may mean and close names."""
    unknown = list(dict.fromkeys(name for name in names
                                 if roles.find_role(config.roles, name, language, mode) is None))
    if not unknown:
        return
    saved = roles.saved_team_in(unknown, config.teams, config.roles, mode)
    hint = f" '{saved}' is a saved team: give it as team (a string), not inside a list." if saved else ""
    close = list(dict.fromkeys(match for name in unknown
                               for match in roles.close_roles(config.roles, name, language, mode=mode)))
    if close and not hint:
        hint = f" Did you mean: {_names(close)}?"
    plural = "roles" if len(unknown) > 1 else "role"
    raise OptionError("unknown_role", f"Unknown {plural}: {_names(unknown)}. GET /api/v1/options lists the roles."
                                      + hint)


def overrides_from(body: Mapping[str, Any], config: "Config", *, strict: bool) -> dict[str, Any]:
    """The session overrides a create's options make ({"swarm": {...}, "agents": {"default_model"}}; {} for none),
    against the global settings `config` (its saved teams, roles and model profiles). The keys it reads:
    `start_agents`, `max_agents`, `max_cost_usd`, `language`, `network`, `clarify`, `model`, `mode` and `goal_rule`
    (docs/SPEC.md §22.9: `swarm.mode` and `swarm.goal_rule`; a `goal_rule` with an effective board mode, the given
    `mode` else the global one, is refused in both paths), and `team`: a list is the starting roles in order (it sets
    `start_agents` unless that is given).
    - not strict (the launcher MCP's `session_create`, unchanged): numbers as int() and float() take them, `team` a
      list only and `team_name` a saved team (not with `team` or `start_agents`); a list that names a saved team and no
      role is refused; any role name passes (a new role), and the model, a bad `mode` or `goal_rule` and the rest are
      left to the settings' check.
    - strict (the HTTP API v1, docs/SPEC.md §21.4): `team` a saved team's name (a string: unknown_team with the saved
      names; not with `start_agents`) or a list of roles the catalog resolves (unknown_role); `model` a profile of the
      settings (unknown_model with their names); `start_agents` and `max_agents` whole numbers of at least 1,
      `max_cost_usd` a number of at least 0, `language` ru | en, `network` on | off, `clarify` a bool, `mode` board |
      thread, `goal_rule` democracy | veto (bad_request otherwise); the team's roles resolve in the effective mode; a
      `start_agents` the request sets (itself or through its team) is at most the session's `max_agents`, and so is
      the settings' when the request sets `max_agents` alone (the starting team ignores `max_agents`). null means
      "not given". `team_name` is no key of v1's.
    OptionError (400) with its code; the overrides still go through SessionManager.validated_overrides."""
    swarm_over: dict[str, Any] = {}
    if strict:
        for key in ("start_agents", "max_agents"):
            count = _strict_count(body, key)
            if count is not None:
                swarm_over[key] = count
        language = _strict_choice(body, "language", LANGUAGES)
        lang = language or config.swarm.language
        mode = _strict_choice(body, "mode", MODES)
        rule = _strict_choice(body, "goal_rule", GOAL_RULES)
        effective = mode or config.swarm.mode
        if rule is not None and effective != "thread":
            raise OptionError("bad_request", GOAL_RULE_BOARD)
        if mode is not None:
            swarm_over["mode"] = mode
        if rule is not None:
            swarm_over["goal_rule"] = rule
        team_value = body.get("team")
        team: list[str] = []
        if isinstance(team_value, str):
            if "start_agents" in swarm_over:
                raise OptionError("bad_request", "A saved team sets the roles and their number: give team without "
                                                 "start_agents.")
            team = _saved_team(team_value, config, strict=True, mode=effective)
        elif team_value is not None:
            team = _team(team_value)
            if not team:
                raise OptionError("bad_request", "team must name at least one role, or be a saved team's name.")
            _strict_roles(team, config, lang, effective)
        if team:   # the starting roles in order; the team's size unless start_agents is given
            swarm_over["starting_roles"] = team
            swarm_over.setdefault("start_agents", len(team))
        if "start_agents" in swarm_over or "max_agents" in swarm_over:
            start = swarm_over.get("start_agents", config.swarm.start_agents)
            most = swarm_over.get("max_agents", config.swarm.max_agents)
            if start > most:
                given = "the team's size" if team and body.get("start_agents") is None else (
                    "start_agents" if "start_agents" in swarm_over else "the settings' start_agents")
                raise OptionError("bad_request", f"{given} ({start}) is over max_agents ({most}): the session would "
                                                 "start more agents than it may have.")
        cost = body.get("max_cost_usd")
        if cost is not None:
            value = _strict_cost(cost)
            if value is None:
                raise OptionError("bad_request", "'max_cost_usd' must be a number of at least 0 (USD).")
            swarm_over["max_cost_usd"] = value
        if language is not None:
            swarm_over["language"] = language
        network = _strict_choice(body, "network", NETWORKS)
        if network is not None:
            swarm_over["network"] = network
        clarify = body.get("clarify")
        if clarify is not None:
            if not isinstance(clarify, bool):
                raise OptionError("bad_request", "'clarify' must be true or false.")
            swarm_over["clarify"] = clarify
        overrides: dict[str, Any] = {"swarm": swarm_over} if swarm_over else {}
        model = body.get("model")
        if model is not None:
            if not isinstance(model, str) or model not in config.models:
                shown = model[:80] if isinstance(model, str) else repr(model)[:80]
                raise OptionError("unknown_model", f"No model profile '{shown}'. Profiles: "
                                                   f"{_names(list(config.models))}.")
            overrides["agents"] = {"default_model": model}
        return overrides
    # the launcher MCP's session_create, as it always was, and the session's mode (docs/SPEC.md §22.9); a bad mode
    # is the settings' check's (Config: "swarm.mode must be 'board' or 'thread'"), which then decides no goal_rule
    mode = body.get("mode") if _given(body, "mode") else None
    effective = mode if mode is not None else config.swarm.mode
    if _given(body, "goal_rule") and effective != "thread" and effective in MODES:
        raise OptionError("bad_request", GOAL_RULE_BOARD)
    for key in ("start_agents", "max_agents"):
        if _given(body, key):
            swarm_over[key] = _int_or_none(body[key])
    team = _team(body.get("team"))
    role_mode = effective if effective in MODES else "board"
    if _given(body, "team_name"):
        if team or "start_agents" in swarm_over:
            raise OptionError("bad_request", "team_name sets the roles and their number: give it without team and "
                                             "start_agents.")
        team = _saved_team(body["team_name"], config, strict=False, mode=role_mode)
    elif team and (saved := roles.saved_team_in(team, config.teams, config.roles, role_mode)):
        raise OptionError("bad_request", f"'{saved}' is a saved team, not a role: pass it as team_name.")
    if team:  # the starting roles in order; the team's size unless start_agents is given
        swarm_over["starting_roles"] = team
        swarm_over.setdefault("start_agents", len(team))
    if _given(body, "max_cost_usd"):
        swarm_over["max_cost_usd"] = float(body["max_cost_usd"])
    if body.get("language"):
        swarm_over["language"] = str(body["language"])
    if body.get("network"):
        swarm_over["network"] = str(body["network"])
    if body.get("clarify") is not None:  # a bool: false overrides a global true
        if not isinstance(body["clarify"], bool):
            raise OptionError("bad_request", "clarify must be true or false.")
        swarm_over["clarify"] = body["clarify"]
    if mode is not None:
        swarm_over["mode"] = mode
    if _given(body, "goal_rule"):
        swarm_over["goal_rule"] = body["goal_rule"]
    overrides = {"swarm": swarm_over} if swarm_over else {}
    if body.get("model"):
        overrides["agents"] = {"default_model": str(body["model"])}
    return overrides


__all__ = ["GOAL_RULES", "LANGUAGES", "NETWORKS", "OptionError", "overrides_from"]
