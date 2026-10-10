"""roles.py and the [[roles]] settings (docs/SPEC.md §17.3): the built-in presets in both languages, validation of
the entries, the merged catalog, bilingual resolution, the starting team and its grouped form, `/team` parsing."""

from __future__ import annotations

import re
import tomllib

import pytest

from dreamteam import prompts, roles
from dreamteam.config import Config, RoleSettings, parse_roles, render_template
from dreamteam.models import Status
from dreamteam.tools import REGISTRY, tools_for

LANGS = ("en", "ru")
_CODE_IDENT = re.compile(r"`([a-z_]+)`")
_SENTENCE_END = re.compile(r"[.!?…](?=\s|$)")
PROFILES = ("default", "fast")


def code_idents(text: str) -> set[str]:
    """Identifiers written in backticks (as in tests/test_prompts.py): tool names, statuses, parameter names."""
    return set(_CODE_IDENT.findall(text))


def entry(**kw) -> RoleSettings:
    return RoleSettings(**kw)


# ------------------------------------------------------------------------------------- presets
def test_presets_have_the_same_ids_and_both_languages():
    assert tuple(roles.ROLE_PRESETS) == ("en", "ru")
    assert tuple(roles.ROLE_PRESETS["en"]) == tuple(roles.ROLE_PRESETS["ru"]) == roles.PRESET_IDS
    assert roles.PRESET_IDS == ("generalist", "developer", "tester", "reviewer", "analyst", "architect", "writer",
                                "researcher")
    assert roles.PRESET_IDS[0] == roles.DEFAULT_ROLE_ID
    llm_tools = {s.name for s in tools_for("llm")}
    params = {p for spec in REGISTRY.values() for p in spec.parameters.get("properties", {})}
    owner: dict[str, str] = {}
    for rid in roles.PRESET_IDS:
        en_name, en_text = roles.ROLE_PRESETS["en"][rid]
        ru_name, ru_text = roles.ROLE_PRESETS["ru"][rid]
        assert code_idents(en_text) == code_idents(ru_text), rid
        for lang, name, text in (("en", en_name, en_text), ("ru", ru_name, ru_text)):
            assert name and len(name) <= roles.NAME_LIMIT and "," not in name and "*" not in name, (lang, rid)
            assert name == roles.normalize(name), (lang, rid)
            assert 3 <= len(_SENTENCE_END.findall(text)) <= 6 and len(text) <= 1200, (lang, rid)
            assert text == roles.normalize(text) and text.endswith("."), (lang, rid)
            assert not code_idents(text) - llm_tools - set(Status.ALL) - params, (lang, rid)
            # the planner's contracts file is the only file a preset names
            assert set(re.findall(r"\b[\w-]+\.(?:md|txt|json|toml|ya?ml)\b", text)) <= {"CONTRACTS.md"}, (lang, rid)
            assert not re.search(r"contracts (?:once )?to a file|контракты в файл", text), (lang, rid)
        for spelling in (rid, en_name, ru_name, en_name.upper(), f" {ru_name.lower()} "):
            # every id and name (any case and spacing) resolves to exactly one preset
            assert owner.setdefault(spelling.strip().casefold(), rid) == rid, spelling
            assert roles.preset_id(spelling) == rid, spelling
    for rid in ("architect", "developer"):
        for lang in LANGS:
            assert "CONTRACTS.md" in roles.ROLE_PRESETS[lang][rid][1], (lang, rid)
    for lang in LANGS:
        assert prompts.DEFAULT_ROLE[lang] == roles.ROLE_PRESETS[lang]["generalist"][0]


def test_presets_view_has_both_languages_in_catalog_order():
    view = roles.presets_view()
    assert [p["id"] for p in view] == list(roles.PRESET_IDS)
    tester = view[roles.PRESET_IDS.index("tester")]
    assert tester["name"] == {"ru": "Тестировщик", "en": "Tester"}
    assert tester["description"] == {lang: roles.ROLE_PRESETS[lang]["tester"][1] for lang in ("ru", "en")}


# ------------------------------------------------------------------------------- parse_roles
@pytest.mark.parametrize("raw, message", [
    ({"name": "tester"}, "[[roles]] must be a list of role tables (write each role as a [[roles]] table)"),
    ("tester", "[[roles]] must be a list of role tables"),
    ([{"name": "tester"}] * 101, "At most 100 roles"),
    (["tester"], "[roles.1] must be a table"),
    ([{"name": "tester", "colour": "red"}], "Unknown keys for [roles.1]: colour"),
    ([{"name": "tester"}, {"name": "writer", "hidden": "yes"}], "[roles.2] hidden must be true or false"),
    ([{"name": "tester", "model": 3}], "[roles.1] model must be a string"),
    ([{"name": "  "}], "[roles.1] name is empty"),
    ([{"description": "x"}], "[roles.1] name is empty"),
    ([{"name": "x" * 61, "description": "d"}], "[roles.1] name must be at most 60 characters"),
    ([{"name": "QA, ops", "description": "d"}], "[roles.1] name cannot contain ',' or '*'"),
    ([{"name": "QA*2", "description": "d"}], "[roles.1] name cannot contain ',' or '*'"),
    ([{"name": "tester"}, {"name": "Tester"}], "[roles.2] the role 'Tester' is listed twice (also as role 1)"),
    ([{"name": "Tester"}, {"name": "writer"}, {"name": "тестировщик"}],
     "[roles.3] the role 'тестировщик' is listed twice (also as role 1)"),
    ([{"name": "Data engineer", "description": "d"}, {"name": "data  ENGINEER", "description": "e"}],
     "[roles.2] the role 'data ENGINEER' is listed twice (also as role 1)"),
    ([{"name": "QA", "description": "d" * 4001}], "[roles.1] description must be at most 4000 characters"),
    ([{"name": "tester", "instructions": "i" * 4001}], "[roles.1] instructions must be at most 4000 characters"),
    ([{"name": "QA"}], "[roles.1] 'QA': a role of your own needs a description"),
    ([{"name": "QA", "description": "   "}], "[roles.1] 'QA': a role of your own needs a description"),
    ([{"name": "tester", "model": "turbo"}],
     "[roles.1] 'tester' uses the model profile 'turbo', which is not defined in [models]"),
])
def test_parse_roles_rules(raw, message):
    with pytest.raises(ValueError) as exc:
        parse_roles(raw, PROFILES)
    assert str(exc.value).startswith(message)


def test_parse_roles_normalizes_entries():
    assert parse_roles(None, PROFILES) == []
    assert parse_roles([], PROFILES) == []
    got = parse_roles([
        {"name": " Тестировщик ", "model": "", "instructions": "  Use pytest.\n"},
        {"name": "Technical  Writer", "description": "  Writes the docs.  ", "model": "fast"},
        {"name": "  Data   engineer ", "description": " Builds pipelines. "},
        {"name": "Ghost", "hidden": True},                  # an own role may skip the description when hidden
        {"name": "researcher", "hidden": True},
    ], PROFILES)
    assert got == [
        RoleSettings(name="tester", instructions="Use pytest."),
        RoleSettings(name="writer", description="Writes the docs.", model="fast"),
        RoleSettings(name="Data engineer", description="Builds pipelines."),
        RoleSettings(name="Ghost", hidden=True),
        RoleSettings(name="researcher", hidden=True),
    ]
    assert len(parse_roles([{"name": f"Role {i}", "description": "d"} for i in range(100)], PROFILES)) == 100


# -------------------------------------------------------------------------------- the catalog
def test_catalog_merge_override_hide_reset():
    entries = [
        entry(name="tester", model="fast", instructions="Use pytest."),
        entry(name="writer", description="Writes short docs."),
        entry(name="researcher", hidden=True),
        entry(name="Data engineer", description="Builds pipelines. Checks them.", model="fast"),
        entry(name="Ghost", hidden=True),
    ]
    for lang in LANGS:
        full = roles.catalog(entries, lang, include_hidden=True)
        assert [r.key for r in full] == [*roles.PRESET_IDS, "data engineer", "ghost"]
        by_key = {r.key: r for r in full}
        tester = by_key["tester"]
        assert (tester.name, tester.description) == roles.ROLE_PRESETS[lang]["tester"]
        assert (tester.model, tester.instructions, tester.builtin, tester.customized) == \
               ("fast", "Use pytest.", True, True)
        # a changed description of a built-in role is used in both languages
        assert by_key["writer"].description == "Writes short docs."
        assert by_key["writer"].name == roles.ROLE_PRESETS[lang]["writer"][0]
        assert by_key["researcher"].hidden and by_key["ghost"].hidden
        own = by_key["data engineer"]
        assert (own.name, own.model, own.builtin, own.customized) == ("Data engineer", "fast", False, False)
        untouched = by_key["developer"]
        assert (untouched.customized, untouched.model, untouched.instructions) == (False, None, "")
        visible = roles.catalog(entries, lang)
        assert [r.key for r in visible] == [k for k in by_key if k not in ("researcher", "ghost")]
    # removing an entry restores the preset
    reset = {r.key: r for r in roles.catalog(entries[1:], "en")}
    assert reset["tester"] == roles.Role("tester", "Tester", roles.ROLE_PRESETS["en"]["tester"][1], builtin=True)
    assert roles.catalog([], "en") == roles.catalog(None, "de")
    assert [r.key for r in roles.catalog([], "en")] == list(roles.PRESET_IDS)
    # dict entries (the settings view) work the same as RoleSettings
    as_dicts = [{"name": e.name, "description": e.description, "instructions": e.instructions, "model": e.model,
                 "hidden": e.hidden} for e in entries]
    assert roles.catalog(as_dicts, "ru", include_hidden=True) == roles.catalog(entries, "ru", include_hidden=True)


def test_role_to_dict_and_catalog_view():
    entries = [entry(name="tester", model="fast"), entry(name="Data engineer", description="Builds pipelines."),
               entry(name="analyst", hidden=True)]
    tester = roles.find_role(entries, "tester", "ru")
    assert tester.to_dict() == {"key": "tester", "name": "Тестировщик",
                                "description": roles.ROLE_PRESETS["ru"]["tester"][1], "instructions": "",
                                "model": "fast", "builtin": True, "customized": True, "hidden": False}
    view = {r["key"]: r for r in roles.catalog_view(entries)}
    assert list(view) == [*roles.PRESET_IDS, "data engineer"]
    assert view["tester"]["name"] == {"ru": "Тестировщик", "en": "Tester"}
    assert view["tester"]["model"] == "fast" and view["tester"]["customized"]
    assert view["analyst"]["hidden"] and not view["developer"]["customized"]
    assert view["data engineer"]["name"] == {"ru": "Data engineer", "en": "Data engineer"}
    assert view["data engineer"]["description"] == {"ru": "Builds pipelines.", "en": "Builds pipelines."}
    assert not view["data engineer"]["builtin"]


def test_find_role_is_bilingual_and_case_insensitive():
    entries = [entry(name="Data engineer", description="Builds pipelines."), entry(name="researcher", hidden=True)]
    for name in ("tester", "Tester", "TESTER", "  тестировщик ", "Тестировщик"):
        assert roles.find_role(entries, name, "en").name == "Tester", name
        assert roles.find_role(entries, name, "ru").name == "Тестировщик", name
    assert roles.find_role(entries, "technical   WRITER", "ru").name == "Технический писатель"
    assert roles.find_role(entries, "data  engineer", "ru").name == "Data engineer"
    assert roles.find_role(entries, "Исследователь", "en").hidden       # hidden roles still resolve
    assert roles.find_role(entries, "QA engineer", "en") is None
    assert roles.find_role(entries, "", "en") is None
    assert (roles.preset_id("Разработчик"), roles.preset_id(" DEVELOPER "), roles.preset_id("dev")) == \
           ("developer", "developer", None)
    assert (roles.role_key("Тестировщик"), roles.role_key(" Data  Engineer ")) == ("tester", "data engineer")
    assert roles.normalize(None) == "" and roles.normalize(" a \t b\n") == "a b"


def test_close_roles():
    entries = [entry(name="Data engineer", description="Builds pipelines."), entry(name="writer", hidden=True)]
    assert roles.close_roles(entries, "Testr", "en") == ["Tester"]
    assert roles.close_roles(entries, "testr", "ru") == ["Тестировщик"]      # the other language's name or the id
    assert roles.close_roles(entries, "data enginer", "en") == ["Data engineer"]
    assert roles.close_roles(entries, "writr", "en") == []                    # hidden roles are not offered
    assert roles.close_roles(entries, "zzz", "en") == []
    assert len(roles.close_roles([], "er", "en", n=2)) <= 2


def test_summary_is_the_first_sentence():
    assert roles.summary("One. Two.") == "One."
    assert roles.summary("Is it? Yes.") == "Is it?"
    assert roles.summary("  No   end  ") == "No end"
    assert roles.summary("") == ""
    assert roles.summary(".scratch/ is used. Then more.") == ".scratch/ is used."
    long = "word " * 80
    assert roles.summary(long, 50).endswith("…") and len(roles.summary(long, 50)) <= 50
    for lang in LANGS:
        for name, description in roles.ROLE_PRESETS[lang].values():
            first = roles.summary(description)
            assert len(first) <= 200 and first.endswith((".", "…")), (lang, name)
            assert description.startswith(first.removesuffix("…")), (lang, name)


# ---------------------------------------------------------------------------- starting team
def test_starting_team_rows_and_value():
    assert roles.starting_team([], 3) == ["generalist"] * 3
    assert roles.team_rows([], 3) == [("generalist", 3)]
    assert roles.team_value([("generalist", 3)]) == ([], 3)
    assert roles.starting_team(["developer"], 3) == ["developer", "generalist", "generalist"]
    assert roles.team_rows(["developer"], 3) == [("developer", 1), ("generalist", 2)]
    assert roles.team_value(roles.team_rows(["developer"], 3)) == (["developer"], 3)
    assert roles.starting_team(["dev", "tester"], 1) == ["dev"]            # extra roles are ignored
    assert roles.starting_team(["dev"], 0) == [] and roles.team_rows(["dev"], -1) == []
    assert roles.starting_team(["", "  ", "QA"], 3) == ["generalist", "generalist", "QA"]
    assert roles.team_rows(["Разработчик", "developer", " Data  engineer ", "tester", "developer"], 6) == [
        ("developer", 2), ("Data engineer", 1), ("tester", 1), ("developer", 1), ("generalist", 1)]
    # trailing generalists only are implicit; the same role may appear in two rows
    assert roles.team_value([("generalist", 1), ("developer", 2), ("Универсал", 2)]) == \
           (["generalist", "developer", "developer"], 5)
    assert roles.team_value([("developer", 1), ("tester", 1), ("developer", 1)]) == \
           (["developer", "tester", "developer"], 3)
    assert roles.team_value([("Тестировщик", 2), ("QA", 0)]) == (["tester", "tester"], 2)
    assert roles.team_value([]) == ([], 0)
    for starting, agents in (([], 3), (["architect", "developer", "developer", "tester"], 4), (["QA"], 2),
                             (["developer", "generalist", "generalist"], 3)):
        rows = roles.team_rows(starting, agents)
        assert roles.team_rows(*roles.team_value(rows)) == rows
        assert [roles.role_key(r) for r in roles.starting_team(*roles.team_value(rows))] == \
               [roles.role_key(r) for r in roles.starting_team(starting, agents)]


def test_parse_team():
    assert roles.parse_team("architect, developer*2, tester") == ["architect", "developer", "developer", "tester"]
    assert roles.parse_team(" Data   engineer * 3 ,, QA ") == ["Data engineer"] * 3 + ["QA"]
    assert roles.parse_team("Тестировщик*1") == ["Тестировщик"]
    assert len(roles.parse_team("developer*50, tester*50, writer*50, analyst*50")) == 200
    for text, message in (
        ("developer*0", "'developer*0': the count must be a whole number from 1 to 50"),
        ("developer*51", "'developer*51': the count must be a whole number from 1 to 50"),
        ("developer*x", "'developer*x': the count must be a whole number from 1 to 50"),
        ("developer*-1", "'developer*-1': the count must be a whole number from 1 to 50"),
        ("developer*²", "'developer*²': the count must be a whole number from 1 to 50"),
        ("a*b*2", "'a*b*2': the count must be a whole number from 1 to 50"),
        ("*2", "'*2': the role name is empty"),
        ("x" * 61, f"'{'x' * 61}': a role name cannot be longer than 60 characters"),
        ("", "The team is empty"),
        (" , ,", "The team is empty"),
        ("developer*50, tester*50, writer*50, analyst*50, QA", "A team has at most 200 agents"),
    ):
        with pytest.raises(ValueError) as exc:
            roles.parse_team(text)
        assert str(exc.value) == message, text


# ------------------------------------------------------------------------------- settings
def test_config_reads_roles_and_independent_review():
    cfg = Config.from_dict(".", {})
    assert cfg.roles == [] and cfg.swarm.independent_review is True
    raw = {"swarm": {"independent_review": False},
           "models": {"default": {"provider": "scripted"}, "fast": {"provider": "scripted"}},
           "roles": [{"name": "Тестировщик", "model": "fast"}, {"name": "QA", "description": "Checks things."}]}
    cfg = Config.from_dict(".", raw)
    assert cfg.swarm.independent_review is False
    assert cfg.roles == [RoleSettings(name="tester", model="fast"),
                         RoleSettings(name="QA", description="Checks things.")]
    assert raw["roles"][0]["name"] == "Тестировщик"            # the caller's data is not changed
    with pytest.raises(ValueError, match=r"\[swarm\] independent_review must be true or false"):
        Config.from_dict(".", {"swarm": {"independent_review": "yes"}})
    with pytest.raises(ValueError, match=r"\[roles.1\] 'tester' uses the model profile 'fast'"):
        Config.from_dict(".", {"roles": [{"name": "tester", "model": "fast"}]})
    with pytest.raises(ValueError, match=r"\[\[roles\]\] must be a list of role tables"):
        Config.from_dict(".", {"roles": {"name": "tester"}})


def test_template_documents_the_review_switch_and_the_role_catalog():
    text = render_template("X", "ru", "anthropic")
    assert "independent_review = true" in text and '["architect", "developer", "developer", "tester"]' in text
    cfg = Config.from_dict(".", tomllib.loads(text))
    assert cfg.swarm.independent_review is True and cfg.roles == []
    # the commented [[roles]] example is valid once uncommented (the anthropic preset has a "fast" profile)
    head, marker, tail = text.partition("# Каталог ролей")
    example = head + marker + re.sub(r"(?m)^# (\[\[roles\]\]|name = |model = |description = )", r"\1", tail)
    assert re.findall(r"(?m)^\[\[roles\]\]$", example) == ["[[roles]]"] * 2
    cfg = Config.from_dict(".", tomllib.loads(example))
    assert [(r.name, r.model) for r in cfg.roles] == [("tester", "fast"), ("Дата-инженер", None)]
    assert roles.find_role(cfg.roles, "Тестировщик", "ru").model == "fast"
