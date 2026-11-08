"""Saved teams in the web UI (static/home.js, docs/SPEC.md §17.3 "Saved teams"), executed in Node.js with the DOM stub
of tests/test_ui.py: the «Команда» section of the settings, and the team select of the new-session dialog."""

from __future__ import annotations

import json
import re

import pytest
from test_ui import needs_node, role_presets, run_js

from dreamteam import roles
from dreamteam import settings as st
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.sessions import SessionManager

pytestmark = needs_node

EVIL = '<img src=x onerror="alert(1)">'

SETTINGS = {
    "swarm": {"name": "S", "language": "en", "start_agents": 3, "max_agents": 4, "max_cost_usd": 5,
              "starting_roles": ["dev_gpt"], "independent_review": True},
    "agents": {"default_model": "default"}, "shell": {}, "workspace": {}, "server": {}, "tracing": {},
    "mcp": {"external": {}},
    "models": {"default": {"provider": "openai", "model": "a", "api_key_set": False},
               "gpt": {"provider": "openai", "model": "b", "api_key_set": False}},
    "roles": [{"name": "dev_gpt", "description": "Backend.", "model": "gpt"}, {"name": "qa", "description": "QA."},
              {"name": "researcher", "hidden": True}],
    "teams": [{"name": "Coding", "roles": ["architect", "dev_gpt", "dev_gpt", "researcher", "generalist"]},
              {"name": "Text", "roles": ["writer", "qa"]}],
}

# The settings page over a stub DOM. `row` and `tname` act on a team card by its position; `press` on a role card.
SETUP = r"""
  const home = loadHomeJs();
  home.H.lang = process.env.DT_LANG;
  home.H.rolePresets = JSON.parse(process.env.DT_PRESETS);
  for (const id of ["settings-form", "settings-nav", "profiles", "save-bar", "save-msg", "add-profile", "roles",
                    "team-defaults", "teams"]) el(id);
  const settings = JSON.parse(process.env.DT_SETTINGS);
  const load = (view) => {
    home.H.view = JSON.parse(JSON.stringify(view)); home.H.original = JSON.parse(JSON.stringify(view));
    home.renderSettings(true);
  };
  load(settings);
  const tcard = (i) => home.H.teamCards[i];
  const click = (attrs) => home.onSettingsClick({ type: "click", target: {
    closest: (sel) => (sel === "[data-team]" ? { dataset: attrs } : null) } });
  const row = (i, kind, n) => click({ team: kind, composer: tcard(i).uid, row: String(n) });
  const pickRole = (i, n, value) => home.onSettingsInput({ type: "change", target: { value, dataset: {},
    closest: () => ({ dataset: { team: "role", composer: tcard(i).uid, row: String(n) }, value }) } });
  const tname = (i, value) => home.onSettingsInput({ type: "input", target: { value, dataset: { tc: "name" },
    closest: (sel) => (sel === "[data-tc-uid]" ? { dataset: { tcUid: tcard(i).uid } } : null) } });
  const tbtn = (attrs, i) => home.onSettingsClick({ type: "click", target: { closest: (sel) => (sel === "button"
    ? { dataset: attrs, closest: () => (i === undefined ? null : { dataset: { tcUid: tcard(i).uid } }) } : null) } });
  const rcard = (name) => home.H.roleCards.find((c) => (c.builtin ? c.id : c.name) === name);
  const edit = (c, field, value) => home.onSettingsInput({ type: "input", target: { value, dataset: { rc: field },
    closest: (sel) => (sel === ".role-card" ? { dataset: { roleUid: c.uid }, querySelector: () => null } : null) } });
  const press = (c, attr) => home.onSettingsClick({ type: "click", target: { closest: (sel) => (sel === "button"
    ? { dataset: { [attr]: "" } } : sel === ".role-card" ? { dataset: { roleUid: c.uid } } : null) } });
  const collect = () => { try { return home.collectSettings(); } catch (e) { return e.message; } };
  const saveBar = async () => { await home.saveSettings(); return elements["save-msg"].textContent; };
"""


def run_settings(body: str, settings: dict | None = None, lang: str = "en") -> dict:
    return run_js(SETUP + body, env={"DT_PRESETS": json.dumps(role_presets()), "DT_LANG": lang,
                                     "DT_SETTINGS": json.dumps(settings or SETTINGS)})


# ------------------------------------------------------------------------------------------------ settings
def test_the_view_of_the_server_comes_back_unchanged():
    """An untouched form sends the teams of public_view as they are, trailing generalists included, and the server keeps
    them: names of presets in either language, a role's other case and a role the catalog lacks."""
    raw = {"agents": {"default_model": "default"},
           "models": {"default": {"provider": "anthropic", "model": "claude-opus-5-5"}},
           "roles": [{"name": "QA", "description": "Checks."}],
           "teams": [{"name": " Разработка  1 ", "roles": ["Тестировщик", "qa", "qa", "generalist", "generalist"]},
                     {"name": "a.b", "roles": ["developer", "qa", "developer"]},
                     {"name": 'say "hi"', "roles": ["Gone role", "writer"]}]}
    view = st.public_view(raw)
    assert view["teams"][0] == {"name": "Разработка 1", "roles": ["tester", "QA", "QA", "generalist", "generalist"]}
    out = run_settings(r"""
      out.dirty = home.H.dirty;
      out.collected = collect();
    """, settings=view)
    assert out["dirty"] is False
    assert out["collected"]["teams"] == view["teams"]
    stored = st.apply_update(raw, {"teams": out["collected"]["teams"]})
    assert st.public_view(stored)["teams"] == view["teams"]


def test_a_home_with_no_teams_saves_none_and_a_view_without_the_key_stays_without_it():
    out = run_settings(r"""
      out.markup = elements["teams"].innerHTML;
      out.none = collect().teams;
      const older = JSON.parse(process.env.DT_SETTINGS);
      delete older.teams;
      load(older);
      out.older = "teams" in collect();
    """, settings={**SETTINGS, "teams": []})
    assert "No teams yet." in out["markup"] and "role-card" not in out["markup"]
    assert out["none"] == [] and out["older"] is False
    assert "teams" not in st.apply_update({"teams": [{"name": "a", "roles": ["writer"]}]}, {"teams": out["none"]})


def test_section_order_and_card_markup():
    out = run_settings(r"""
      out.form = elements["settings-form"].innerHTML;
      out.nav = elements["settings-nav"].innerHTML;
      out.cards = elements["teams"].innerHTML;
    """)
    form = out["form"]
    assert form.index('id="s-roles"') < form.index('id="s-teams"') < form.index('id="s-swarm"')
    nav = out["nav"]
    assert nav.index('data-nav="roles"') < nav.index('data-nav="teams"') < nav.index('data-nav="swarm"')
    assert '<div class="roles" id="teams"></div>' in form and "data-tc-add" in form and "Add a team" in form
    cards = out["cards"]
    assert cards.count('class="role-card team-card"') == 2 and cards.count("data-tc-del") == 2
    assert cards.count('data-tc="name"') == 2 and 'class="role-name"' in cards and 'value="Coding"' in cards
    assert 'title="Delete team"' in cards and 'placeholder="e.g. Development"' in cards and 'title="Team name"' in cards
    # the composer of a saved team: its own head, catalog roles only, no starting-team hint
    assert ">Roles<" in cards and "Starting team" not in cards and "Other role" not in cards
    assert 'data-team="custom"' not in cards and 'class="field-hint"' not in cards
    assert "Agents in all: 5" in cards and "Agents in all: 2" in cards
    assert "(hidden)" in cards and "model: gpt" in cards  # a hidden role's row, and the profile the role brings
    # a composer's id is generated, never the team's name
    assert re.findall(r'data-tc-uid="(\w+)"', cards) == re.findall(r'id="team-(tm\d+)"', cards)
    assert "data-composer=\"Coding\"" not in cards


def test_hidden_roles_are_not_offered_to_other_rows():
    out = run_settings(r"""
      click({ team: "add", composer: tcard(1).uid });
      out.rows = home.H.teams[tcard(1).uid].map((r) => r.role);
      out.card = home.teamCardHtml(tcard(1));
    """)
    assert out["rows"] == ["writer", "qa", "generalist"]
    assert 'value="researcher"' not in out["card"] and 'value="dev_gpt"' in out["card"]


@pytest.mark.parametrize("step", ["""row(1, "inc", 0);""", """row(0, "del", 1);""", """tname(1, "Text 2");""",
                                  """tbtn({ tcDel: "" }, 0);""", """tbtn({ tcAdd: "" });"""])
def test_any_edit_marks_the_form_dirty(step: str):
    out = run_settings(f"""
      out.before = home.H.dirty;
      {step}
      out.after = home.H.dirty;
      out.message = elements["save-msg"].textContent;
    """)
    assert out["before"] is False and out["after"] is True and out["message"] == "Unsaved changes"


def test_edits_reach_the_payload():
    out = run_settings(r"""
      row(1, "inc", 1);
      out.counted = collect().teams[1];
      tname(1, "  Text   2 ");
      out.named = collect().teams[1];
      pickRole(1, 0, "reviewer");
      out.picked = collect().teams[1];
      row(1, "del", 1);
      out.removed = collect().teams[1];
    """)
    assert out["counted"] == {"name": "Text", "roles": ["writer", "qa", "qa"]}
    assert out["named"] == {"name": "Text 2", "roles": ["writer", "qa", "qa"]}
    assert out["picked"] == {"name": "Text 2", "roles": ["reviewer", "qa", "qa"]}
    assert out["removed"] == {"name": "Text 2", "roles": ["reviewer"]}


SAVE_ERRORS = """
  out.steps = [];
  const step = async (name) => { tname(1, name); out.steps.push(await saveBar()); };
  await step("  ");
  await step("a,b");
  await step("a*b");
  await step("-a");
  await step("x".repeat(61));
  await step("Default");
  await step("сброс");
  await step("RESET");
  await step("coding");
  tname(1, "Text 2");
  row(1, "del", 0); row(1, "del", 0);
  out.steps.push(await saveBar());
  out.sent = calls.length;
"""


def test_save_bar_errors_in_english():
    out = run_settings(SAVE_ERRORS)
    bad = "at most 60 characters, no “,” or “*”, not starting with “-”."
    reserved = "is taken: /team {0} goes back to the starting team of the settings."
    assert out["steps"] == ["Enter the team name.",
                            f"Team name “a,b”: {bad}",
                            f"Team name “a*b”: {bad}",
                            f"Team name “-a”: {bad}",
                            f"Team name “{'x' * 61}”: {bad}",
                            "The name “Default” " + reserved.format("Default"),
                            "The name “сброс” " + reserved.format("сброс"),
                            "The name “RESET” " + reserved.format("RESET"),
                            "Two teams are named “coding”.",
                            "Add at least one role to the team “Text 2”."]
    assert out["sent"] == 0


def test_save_bar_errors_in_russian():
    out = run_settings(SAVE_ERRORS, lang="ru")
    bad = "не длиннее 60 символов, без «,» и «*», не с «-» в начале."
    reserved = "занято: /team {0} возвращает стартовую команду из настроек."
    assert out["steps"] == ["Укажите название команды.",
                            f"Название команды «a,b»: {bad}",
                            f"Название команды «a*b»: {bad}",
                            f"Название команды «-a»: {bad}",
                            f"Название команды «{'x' * 61}»: {bad}",
                            "Название «Default» " + reserved.format("Default"),
                            "Название «сброс» " + reserved.format("сброс"),
                            "Название «RESET» " + reserved.format("RESET"),
                            "Две команды с названием «coding».",
                            "Добавьте в команду «Text 2» хотя бы одну роль."]
    assert out["sent"] == 0


def test_the_gone_role_texts_in_russian():
    out = run_settings(r"""
      press(rcard("qa"), "rcDel");
      out.card = home.teamCardHtml(tcard(1));
      out.refused = collect();
    """, lang="ru")
    assert "Этой роли больше нет в каталоге: выберите другую или уберите строку." in out["card"]
    assert out["refused"] == ("В команде «Text» есть роль «qa», которой нет в каталоге: выберите другую или уберите "
                              "строку.")


def test_each_text_has_the_same_placeholders_in_both_languages():
    out = run_settings(r"""
      out.ru = home.I18N.ru;
      out.en = home.I18N.en;
    """)
    def fields(texts: dict) -> dict:
        return {key: sorted(set(re.findall(r"\{(\w+)\}", text))) for key, text in texts.items()}

    assert fields(out["ru"]) == fields(out["en"])


def test_every_new_text_is_translated():
    out = run_settings(r"""
      out.ru = home.I18N.ru;
      out.en = home.I18N.en;
    """)
    new = [key for key in out["en"] if key.startswith(("tm_", "f_team_pick"))
           or key in ("s_teams", "s_teams_hint", "team_model_session")]
    assert [key for key in new if out["ru"][key] == out["en"][key]] == []
    assert [key for key in new if key.startswith("tm_") and not re.search(r"[а-яё]", out["ru"][key], re.I)] == []


def test_a_name_at_the_limit_is_taken_as_the_server_counts_it():
    """The server counts code points: 60 of them pass, emoji or not, and the form must not stop every other save."""
    out = run_settings(r"""
      out.teamCard = home.teamCardHtml(tcard(1));
      out.roleCard = home.roleCard(rcard("qa"));
      tname(1, "x".repeat(60));
      out.plain = collect().teams[1].name;
      tname(1, "😀".repeat(60));
      out.astral = collect().teams[1].name;
      tname(1, "😀".repeat(61));
      out.over = collect();
      out.role = home.roleEntries([{ builtin: false, name: "😀".repeat(60), description: "D." }])[0].name;
    """)
    assert out["plain"] == "x" * 60 and out["astral"] == "😀" * 60 and out["role"] == "😀" * 60
    assert out["over"].startswith("Team name “") and "at most 60 characters" in out["over"]
    # maxlength counts UTF-16 units: it would cut a name of 31 emoji with no message
    assert "maxlength" not in out["teamCard"]
    assert "maxlength" not in re.search(r'<input[^>]*data-rc="name"[^>]*>', out["roleCard"]).group()


def test_names_are_split_on_the_whitespace_of_the_server():
    """roles.normalize is str.split(), whose whitespace is not JS's \\s: they differ on U+FEFF, U+0085 and U+001C-1F."""
    out = run_settings(r"""
      out.spaces = [];
      for (let code = 0; code < 0x10000; code++) {
        if (home.normRole("a" + String.fromCharCode(code) + "b") === "a b") out.spaces.push(code);
      }
      out.role = home.roleEntries([{ builtin: false, name: "a" + String.fromCharCode(0xfeff) + "b", description: "D." }])[0].name;
      tname(1, "a\x85b");
      out.team = collect().teams[1].name;
    """)
    assert out["spaces"] == [code for code in range(0x10000) if roles.normalize(f"a{chr(code)}b") == "a b"]
    assert out["role"] == "a\N{ZERO WIDTH NO-BREAK SPACE}b"   # a role is not rewritten by a save of something else
    assert out["team"] == "a b" == st.validate(".", {"teams": [{"name": "a\x85b", "roles": ["writer"]}]}).teams[0].name


@pytest.mark.parametrize("name", ["​", "a​b", "‮abc", "a\x07b", "\x1b[31mred",
                                  "\N{ZERO WIDTH NO-BREAK SPACE}abc"])
def test_a_name_with_control_or_invisible_characters_is_refused(name):
    out = run_settings(f"""
      tname(1, {json.dumps(name)});
      out.refused = collect();
    """)
    assert out["refused"] == f"The team name “{name}” has control or invisible characters."
    with pytest.raises(ValueError, match="name has control or invisible characters"):
        st.validate(".", {"teams": [{"name": name, "roles": ["writer"]}]})


@pytest.mark.parametrize("name", ["\U0001f9d1‍\U0001f4bb Разработка", "برنامه‌نویس",
                                  "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f England"])
def test_a_name_with_joiners_is_taken_as_the_server_takes_it(name):
    out = run_settings(f"""
      tname(1, {json.dumps(name)});
      out.team = collect().teams[1];
    """)
    assert out["team"]["name"] == name
    assert st.validate(".", {"teams": [out["team"]]}).teams[0].name == name


@pytest.mark.parametrize("name", ["‍", "‌‍"])
def test_joiners_alone_are_no_name(name):
    out = run_settings(f"""
      tname(1, {json.dumps(name)});
      out.refused = collect();
    """)
    assert out["refused"] == "Enter the team name."
    with pytest.raises(ValueError, match="name is empty"):
        st.validate(".", {"teams": [{"name": name, "roles": ["writer"]}]})


@pytest.mark.parametrize("first, second, message", [
    ("Straße", "STRASSE", "Two teams are named “STRASSE”."),
    ("ΣΑΣ", "σας", "Two teams are named “σας”."),
    ("STRAẞE", "STRASSE", "Two teams are named “STRASSE”."),
])
def test_two_names_of_one_casefold_are_one_name(first, second, message):
    """The twin of roles.team_key: the save bar answers, in the user's language, what the server would refuse."""
    out = run_settings(f"""
      tname(0, {json.dumps(first)});
      tname(1, {json.dumps(second)});
      out.refused = collect();
    """)
    assert out["refused"] == message
    assert roles.team_key(first) == roles.team_key(second)


def test_two_names_that_differ_by_a_dotless_i_are_two_names():
    """ı has no case fold of its own in Python; a false «two teams» here would block every save of the settings."""
    out = run_settings(r"""
      tname(0, "Kır");
      tname(1, "Kir");
      out.names = collect().teams.map((team) => team.name);
    """)
    assert out["names"] == ["Kır", "Kir"]
    assert roles.team_key("Kır") != roles.team_key("Kir")


def test_a_reserved_word_of_another_case_form_is_refused_by_the_form_too():
    out = run_settings(r"""
      tname(1, "reſet");
      out.refused = collect();
    """)
    assert out["refused"] == "The name “reſet” is taken: /team reſet goes back to the starting team of the settings."
    with pytest.raises(ValueError, match="is reserved"):
        st.validate(".", {"teams": [{"name": "reſet", "roles": ["writer"]}]})


def test_reset_words_are_the_ones_of_roles_py():
    assert run_settings("out.words = home.TEAM_RESET_WORDS;")["words"] == list(roles.TEAM_RESET)


def test_add_and_delete_a_card():
    out = run_settings(r"""
      const gone = tcard(1).uid;
      tbtn({ tcDel: "" }, 1);
      out.afterDelete = collect().teams.map((x) => x.name);
      out.stale = gone in home.H.teams;
      out.markup = elements["teams"].innerHTML;
      tbtn({ tcAdd: "" });
      out.blank = collect();
      out.markupAdded = elements["teams"].innerHTML;
      tname(1, "New");
      out.noRows = collect();
      click({ team: "add", composer: tcard(1).uid });
      out.added = collect().teams[1];
    """)
    assert out["afterDelete"] == ["Coding"] and out["stale"] is False and 'value="Text"' not in out["markup"]
    assert out["blank"] == "Enter the team name." and out["markupAdded"].count('class="role-card team-card"') == 2
    assert out["noRows"] == "Add at least one role to the team “New”."
    assert out["added"] == {"name": "New", "roles": ["generalist"]}


def test_deleting_the_last_card_shows_the_empty_text_and_saves_none():
    out = run_settings(r"""
      tbtn({ tcDel: "" }, 0); tbtn({ tcDel: "" }, 0);
      out.markup = elements["teams"].innerHTML;
      out.teams = collect().teams;
    """)
    assert "No teams yet." in out["markup"] and out["teams"] == []


def test_a_reload_leaves_no_stale_composer():
    out = run_settings(r"""
      const before = home.H.teamCards.map((c) => c.uid);
      tbtn({ tcAdd: "" });
      const added = tcard(2).uid;
      home.renderSettings(true);
      out.oldKept = [...before, added].filter((k) => k in home.H.teams);
      out.saved = Object.keys(home.H.teams).filter((k) => k.startsWith("tm")).length;
      out.defaults = "defaults" in home.H.teams;
      out.dirty = home.H.dirty;
    """)
    assert out["oldKept"] == [] and out["saved"] == 2 and out["defaults"] is True and out["dirty"] is False


def test_the_language_switch_keeps_what_was_typed():
    out = run_settings(r"""
      tname(1, "Typed");
      row(1, "inc", 0);
      home.H.lang = "ru";
      home.renderSettings(false);
      out.cards = elements["teams"].innerHTML;
      out.team = collect().teams[1];
    """)
    assert 'value="Typed"' in out["cards"] and ">Состав<" in out["cards"]
    assert out["team"] == {"name": "Typed", "roles": ["writer", "writer", "qa"]}


# ------------------------------------------------------------------------------------------------ role changes
def test_deleting_a_role_turns_the_row_gone_and_refuses_until_fixed():
    out = run_settings(r"""
      const box = el("team-" + tcard(0).uid);
      press(rcard("dev_gpt"), "rcDel");
      out.card = box.innerHTML;                                        // re-rendered at once
      out.refused = await saveBar();
      out.sent = calls.length;
      row(0, "del", 1);                                                // the row of dev_gpt in the team
      click({ team: "del", composer: "defaults", row: "0" });          // and in the starting team of the settings
      click({ team: "add", composer: "defaults" });
      out.fixed = collect();
    """)
    card = out["card"]
    assert 'value="dev_gpt" selected disabled' in card and "This role is no longer in the catalog" in card
    assert "Other role" not in card and "model: gpt" not in card and 'data-team="custom"' not in card
    assert out["refused"] == ("The team “Coding” uses the role “dev_gpt”, which is not in the catalog: choose another "
                              "or remove the row.")
    assert out["sent"] == 0
    assert out["fixed"]["teams"][0]["roles"] == ["architect", "researcher", "generalist"]


def test_renaming_a_role_keeps_the_row_on_the_old_name_and_asks_at_save():
    out = run_settings(r"""
      const box = el("team-" + tcard(1).uid);
      edit(rcard("qa"), "name", "qa2");
      out.rows = home.H.teams[tcard(1).uid].map((r) => r.role);
      out.card = box.innerHTML;                                        // re-rendered at once
      out.refused = collect();
      pickRole(1, 1, "qa2");
      out.fixed = collect().teams[1];
    """)
    assert out["rows"] == ["writer", "qa"] and 'value="qa" selected disabled' in out["card"]
    assert 'value="qa2"' in out["card"]  # the new name is on offer
    assert out["refused"] == ("The team “Text” uses the role “qa”, which is not in the catalog: choose another or "
                              "remove the row.")
    assert out["fixed"] == {"name": "Text", "roles": ["writer", "qa2"]}


def test_a_gone_row_stands_out_and_offers_no_hidden_role():
    out = run_settings(r"""
      press(rcard("qa"), "rcDel");
      out.card = home.teamCardHtml(tcard(1));
      out.other = home.teamCardHtml(tcard(0));
    """)
    assert out["card"].count('class="team-row team-gone"') == 1 and 'class="team-row team-gone"' not in out["other"]
    gone = out["card"].split('class="team-row team-gone"', 1)[1]
    assert 'value="qa" selected disabled' in gone and 'value="researcher"' not in gone


def test_a_refused_save_leads_to_the_row():
    """The teams come after the role cards on a long page: the bar names the row, and the page goes to it."""
    out = run_settings(r"""
      press(rcard("qa"), "rcDel");
      const uid = tcard(1).uid;
      const select = { scrollIntoView() { out.scrolled = true; }, focus() { out.focused = true; } };
      const query = document.querySelector;
      document.querySelector = (sel) => (sel === `#team-${uid} [data-team="role"][data-row="1"]` ? select : query(sel));
      out.bar = await saveBar();
      document.querySelector = query;
      out.sent = calls.length;
    """)
    assert out["bar"].startswith("The team “Text” uses the role “qa”") and out["sent"] == 0
    assert out["scrolled"] is True and out["focused"] is True


def test_a_refused_save_keeps_its_message_when_an_edited_input_held_the_focus():
    """A tap can fire Save with the focus still in the input: its change event runs at the first move of the focus, and
    after the refusal it would overwrite the message and replace the row."""
    out = run_settings(r"""
      press(rcard("qa"), "rcDel");
      const uid = tcard(1).uid;
      let pending = true;
      const moved = () => { if (pending) { pending = false; edit(rcard("dev_gpt"), "description", "Changed."); } };
      el("settings-save", { focus: moved });
      const select = { scrollIntoView() {}, focus: moved };
      const query = document.querySelector;
      document.querySelector = (sel) => (sel === `#team-${uid} [data-team="role"][data-row="1"]` ? select : query(sel));
      out.bar = await saveBar();
      document.querySelector = query;
    """)
    assert out["bar"].startswith("The team “Text” uses the role “qa”")


def test_another_refused_save_moves_nothing():
    out = run_settings(r"""
      tname(1, "  ");
      const query = document.querySelector;
      document.querySelector = (sel) => {
        if (sel.includes('data-team="role"')) out.moved = true;
        return query(sel);
      };
      out.bar = await saveBar();
      document.querySelector = query;
    """)
    assert out["bar"] == "Enter the team name." and "moved" not in out


def test_a_change_of_a_role_reaches_the_starting_team_composer_too():
    out = run_settings(r"""
      const box = elements["team-defaults"];
      box.innerHTML = "stale";
      edit(rcard("qa"), "description", "Changed.");
      out.afterEdit = box.innerHTML;
      box.innerHTML = "stale";
      press(rcard("dev_gpt"), "rcDel");
      out.afterDelete = box.innerHTML;
      box.innerHTML = "stale";
      edit(rcard("qa"), "name", "qa2");
      out.afterRename = box.innerHTML;
    """)
    assert "stale" not in (out["afterEdit"], out["afterDelete"], out["afterRename"])
    assert 'value="qa2"' in out["afterRename"]


def test_a_row_above_the_row_limit_is_not_cut_down_by_its_buttons():
    """30 + 30 of one role come back from the server as one row of 60: − goes to 59 and + stays, no 10 agents lost."""
    settings = json.loads(json.dumps(SETTINGS))
    settings["teams"] = [{"name": "Big", "roles": ["tester"] * 60 + ["writer"] * 2}]
    out = run_settings(r"""
      const rows = () => home.H.teams[tcard(0).uid].map((r) => r.count);
      out.opened = rows();
      out.input = home.teamCardHtml(tcard(0));
      row(0, "inc", 0);
      out.inc = rows();
      row(0, "dec", 0);
      out.dec = rows();
      out.size = collect().teams[0].roles.length;
      row(0, "inc", 1);
      out.small = rows();
    """, settings=settings)
    assert out["opened"] == [60, 2] and 'max="60"' in out["input"] and 'max="50"' in out["input"]
    assert out["inc"] == [60, 2] and out["dec"] == [59, 2] and out["size"] == 61 and out["small"] == [59, 3]


COUNT = r"""
  const type = (i, n, value) => {
    const field = { value, dataset: {} };
    home.onSettingsInput({ type: "input", target: { value, dataset: {},
      closest: () => Object.assign(field, { dataset: { team: "count", composer: tcard(i).uid, row: String(n) } }) } });
    return field.value;
  };
  const rows = () => home.H.teams[tcard(0).uid].map((r) => r.count);
"""


def test_the_limit_of_a_big_row_stays_what_it_came_with():
    settings = json.loads(json.dumps(SETTINGS))
    settings["teams"] = [{"name": "Big", "roles": ["tester"] * 60}]
    out = run_settings(COUNT + r"""
      row(0, "dec", 0);
      row(0, "inc", 0);
      out.undone = rows();                // what − took, + gives back
      out.first = type(0, 0, "5");
      out.typed = [type(0, 0, "58"), rows()];   // and the digit just typed is no limit for the next
      out.above = [type(0, 0, "70"), rows()];
      row(0, "dec", 0);
      out.card = home.teamCardHtml(tcard(0));
      out.size = collect().teams[0].roles.length;
    """, settings=settings)
    assert out["undone"] == [60] and out["first"] == "5" and out["typed"] == ["58", [58]]
    assert out["above"] == [60, [60]]   # the field shows what is kept
    assert 'max="60"' in out["card"] and 'value="59"' in out["card"] and out["size"] == 59


def test_a_typed_count_over_the_limit_shows_what_is_kept():
    out = run_settings(COUNT + r"""
      out.field = type(1, 0, "60");
      out.team = collect().teams[1];
      out.low = type(1, 0, "0");
    """)
    assert out["field"] == 50 and len(out["team"]["roles"]) == 51 and out["low"] == 1


def test_the_total_of_a_saved_team_follows_a_typed_count():
    out = run_settings(COUNT + r"""
      el("team-total-" + tcard(1).uid);
      type(1, 0, "5");
      out.total = elements["team-total-" + tcard(1).uid].textContent;
    """)
    assert out["total"] == "Agents in all: 6"


def test_a_row_moves_up_and_down_and_the_saved_order_follows():
    out = run_settings(r"""
      out.first = home.teamCardHtml(tcard(0));
      out.defaults = elements["settings-form"].innerHTML;
      row(0, "up", 2);
      out.up = collect().teams[0].roles;
      out.dirty = home.H.dirty;
      row(0, "down", 0);
      out.down = collect().teams[0].roles;
      row(0, "up", 0);   // at the end of the list
      out.end = collect().teams[0].roles;
    """)
    # Coding: architect, dev_gpt × 2, researcher, generalist
    assert out["up"] == ["architect", "researcher", "dev_gpt", "dev_gpt", "generalist"] and out["dirty"] is True
    assert out["down"] == ["researcher", "architect", "dev_gpt", "dev_gpt", "generalist"] == out["end"]
    head, *_ = out["first"].split('data-team="del"')
    assert re.search(r'data-team="up"[^>]*disabled', head) and not re.search(r'data-team="down"[^>]*disabled', head)
    assert re.search(r'data-team="down"[^>]*disabled', out["first"].rsplit('class="team-row', 1)[1])
    assert 'title="Move up"' in out["first"]
    assert 'data-team="dec" data-composer="defaults"' in out["defaults"] and 'data-team="up"' not in out["defaults"]


def test_a_move_shows_on_the_screen():
    out = run_settings(r"""
      const box = el("team-" + tcard(0).uid);
      row(0, "up", 2);
      out.html = box.innerHTML;
    """)
    picked = re.findall(r'<option value="([^"]+)" selected', out["html"])
    assert picked == ["architect", "researcher", "dev_gpt", "generalist"]


def test_the_move_buttons_say_what_they_do():
    card = run_settings("""out.card = home.teamCardHtml(tcard(0));""")["card"]
    assert re.search(r'data-team="up"[^>]*title="Move up"[^>]*aria-label="Move up"[^>]*>↑<', card)
    assert re.search(r'data-team="down"[^>]*title="Move down"[^>]*aria-label="Move down"[^>]*>↓<', card)


@pytest.mark.parametrize("kind, row, follows", [("up", 2, 'data-team="up"][data-row="1"]'),
                                                ("up", 1, 'data-team="down"][data-row="0"]'),
                                                ("down", 1, 'data-team="down"][data-row="2"]'),
                                                ("down", 2, 'data-team="up"][data-row="3"]')])
def test_the_focus_follows_a_moved_row(kind, row, follows):
    """The button it was on, or at the end of the list, where that one is disabled, the other one."""
    out = run_settings(f"""
      const query = document.querySelector;
      document.querySelector = (sel) => (sel.includes("data-row") ? {{ focus() {{ out.focused = sel; }} }} : query(sel));
      row(0, {json.dumps(kind)}, {row});
      document.querySelector = query;
    """)
    assert out["focused"].endswith(follows)


def test_the_focus_goes_on_after_a_team_is_deleted():
    out = run_settings(r"""
      const query = document.querySelector;
      out.focused = [];
      document.querySelector = (sel) => (sel.includes("data-tc-") && !sel.startsWith("#")
        ? { focus() { out.focused.push(sel); } } : query(sel));
      const second = tcard(1).uid;
      tbtn({ tcDel: "" }, 0);       // the next card is now in its place
      tbtn({ tcDel: "" }, 0);       // none left
      document.querySelector = query;
      out.second = second;
    """)
    assert out["focused"] == [f'.team-card[data-tc-uid="{out["second"]}"] [data-tc-del]', "[data-tc-add]"]


CARD_FOCUS = r"""
  const query = document.querySelector;
  out.focused = [];
  document.querySelector = (sel) => (sel.includes("data-tc-") && !sel.startsWith("#")
    ? { focus() { out.focused.push(sel); } } : query(sel));
  tbtn({ tcAdd: "" });                  // three cards now
  out.uids = home.H.teamCards.map((card) => card.uid);
"""


def test_the_focus_goes_to_the_name_of_a_new_card():
    out = run_settings(CARD_FOCUS)
    assert out["focused"] == [f'.team-card[data-tc-uid="{out["uids"][2]}"] .role-name']


@pytest.mark.parametrize("deleted, goes_to", [(0, 1), (1, 2), (2, 1)])   # the card now in its place, else the last one
def test_the_focus_after_deleting_one_of_three_cards(deleted, goes_to):
    out = run_settings(CARD_FOCUS + f"""
      tbtn({{ tcDel: "" }}, {deleted});
    """)
    assert out["focused"][-1] == f'.team-card[data-tc-uid="{out["uids"][goes_to]}"] [data-tc-del]'


def test_labels_of_a_saved_team_composer_and_the_badge_of_a_role_with_no_profile():
    out = run_settings(r"""
      out.saved = home.teamCardHtml(tcard(1));
      out.defaults = elements["settings-form"].innerHTML;
      home.H.lang = "ru";
      out.ru = home.teamCardHtml(tcard(1));
      out.hint = home.I18N.ru.s_teams_hint;
    """)
    assert ">Add a row<" in out["saved"] and ">Add a role<" not in out["saved"] and ">Add a role<" in out["defaults"]
    assert 'class="team-model session">model: the session' in out["saved"]  # writer, qa: no profile of their own
    assert 'class="team-model">model: gpt<' in out["defaults"]
    assert ">Добавить строку<" in out["ru"] and "модель: как у сессии" in out["ru"]
    assert "в разделе «Роли»" in out["hint"]


def test_badges_and_gone_marks_by_composer():
    out = run_settings(r"""
      press(rcard("qa"), "rcDel");
      out.card = home.teamCardHtml(tcard(1));
      home.H.teams.defaults = [{ role: "writer", count: 1 }, { role: "Mine", count: 1, custom: true }];
      out.defaults = home.teamComposer("defaults");
    """)
    gone =out["card"].split('class="team-row team-gone"', 1)[1]
    assert "team-model" not in gone   # no model is claimed for a role that is not there
    assert "team-gone" not in out["defaults"]   # a new role is not an error in the other composers
    # a name typed in «Other role…» may be a role with a profile of its own: only the catalog row says the session's
    assert out["defaults"].count('class="team-model') == 1 and out["defaults"].count('class="team-model session"') == 1
    assert '<p class="field-hint">The agent of the first row is woken first' in out["defaults"]


def test_a_role_in_another_case_is_the_same_role():
    out = run_settings(r"""
      edit(rcard("qa"), "name", "QA");
      out.card = home.teamCardHtml(tcard(1));
      out.team = collect().teams[1];
    """)
    assert "selected disabled" not in out["card"]
    assert [x.casefold() for x in out["team"]["roles"]] == ["writer", "qa"]


def test_a_role_already_gone_blocks_no_other_save():
    settings = json.loads(json.dumps(SETTINGS))
    settings["teams"][0]["roles"].append("Old role")
    out = run_settings(r"""
      out.card = home.teamCardHtml(tcard(0));
      tname(1, "Text 2");
      out.collected = collect();
      press(rcard("qa"), "rcDel");
      out.justLost = collect();
    """, settings=settings)
    assert 'value="Old role" selected disabled' in out["card"]
    assert "This role is no longer in the catalog" in out["card"]
    assert out["collected"]["teams"][0]["roles"][-1] == "Old role" and out["collected"]["teams"][1]["name"] == "Text 2"
    # a role lost in this edit still blocks it
    assert out["justLost"] == ("The team “Text 2” uses the role “qa”, which is not in the catalog: choose another or "
                               "remove the row.")


def test_a_role_added_in_the_same_edit_can_be_used():
    out = run_settings(r"""
      press({ uid: "" }, "roleAdd");
      const fresh = home.H.roleCards[home.H.roleCards.length - 1];
      edit(fresh, "name", "Designer");
      edit(fresh, "description", "Draws.");
      out.offered = home.teamCardHtml(tcard(1));
      pickRole(1, 0, "Designer");
      out.team = collect().teams[1];
    """)
    assert 'value="Designer"' in out["offered"]
    assert out["team"] == {"name": "Text", "roles": ["Designer", "qa"]}


def test_hiding_a_role_leaves_its_rows_valid():
    out = run_settings(r"""
      press(rcard("writer"), "rcHide");
      out.card = home.teamCardHtml(tcard(1));
      out.team = collect().teams[1];
    """)
    assert "(hidden)" in out["card"] and "selected disabled" not in out["card"]
    assert out["team"] == {"name": "Text", "roles": ["writer", "qa"]}


# ------------------------------------------------------------------------------------------------ the dialog
DIALOG = r"""
  const home = loadHomeJs();
  home.H.lang = process.env.DT_LANG || "en";
  home.H.rolePresets = JSON.parse(process.env.DT_PRESETS);
  const base = { swarm: { language: "en", start_agents: 3, max_agents: 2, max_cost_usd: 5,
                          starting_roles: ["architect"] },
                 agents: { default_model: "default" }, mcp: {}, models: { default: { model: "m" } },
                 roles: [{ name: "dev_gpt", description: "Backend.", model: "gpt", skills: ["lint-rules"] },
                         { name: "researcher", hidden: true }] };
  const teams = [{ name: "Coding", roles: ["architect", "dev_gpt", "dev_gpt", "researcher"] },
                 { name: "Small", roles: ["writer", "generalist"] },
                 { name: "Broken", roles: ["writer", "Gone", "Other"] }];
  const listeners = [];
  const open = async (original) => {
    home.H.original = JSON.parse(JSON.stringify(original));
    el("modal"); el("modal-backdrop");
    el("ns-team", { addEventListener: (type, fn) => listeners.push([type, fn === home.onNsTeamPick]) });
    el("ns-max", { value: "2", addEventListener: (type, fn) => listeners.push([type, fn === home.onNsMaxInput]) });
    el("team-ns"); el("ns-role-skills"); el("ns-error");
    await home.newSessionModal();
    return elements.modal.innerHTML;
  };
  const pick = (name) => { elements["ns-team"].value = name; home.onNsTeamPick(); };
  const typeMax = (value) => { elements["ns-max"].value = value; home.onNsMaxInput(); };
  const rows = () => JSON.parse(JSON.stringify(home.H.teams.ns));
  const fire = (type, attrs, value) => home.onTeamEvent({ type, target: {
    closest: () => ({ dataset: attrs, value }) } });
  const form = () => ({ lang: "en", max: elements["ns-max"].value, budget: "5", team: home.H.teams.ns });
  const submit = async () => {   // the real submit, with «Запустить сразу» off: what goes to POST /api/sessions
    for (const [id, props] of Object.entries({
      "ns-goal": { value: "Goal", focus() {} }, "ns-title": {}, "ns-desc": {}, "ns-lang": { value: "en" },
      "ns-budget": { value: "5" }, "ns-model": { value: "default" }, "ns-steps": {}, "ns-approval": { checked: false },
      "ns-offline": { checked: false }, "ns-clarify": { checked: false }, "ns-run": { checked: false }, "ns-submit": {},
    })) el(id, props);
    calls.length = 0;
    await home.submitNewSession({ preventDefault() {} });
    return calls.find((c) => c.url === "/api/sessions").body.overrides;
  };
"""


def run_dialog(body: str, lang: str = "en", evil: str = EVIL) -> dict:
    return run_js(DIALOG + body, env={"DT_PRESETS": json.dumps(role_presets()), "DT_LANG": lang, "EVIL": evil})


def test_dialog_without_teams_is_as_it_was():
    out = run_dialog(r"""
      out.html = await open(base);
      out.empty = await open({ ...base, teams: [] });
      out.pick = home.nsTeamPick(base);
      out.rows = rows();
    """)
    for html in (out["html"], out["empty"]):
        assert 'id="ns-team"' not in html and 'id="team-ns"' in html
    assert out["pick"] == ""
    assert out["rows"] == [{"role": "architect", "count": 1}, {"role": "generalist", "count": 2}]


def test_a_home_with_no_teams_works_with_no_select():
    """The stub answers `any` for an element that is not there, a browser null: most homes have no saved team."""
    out = run_dialog(r"""
      const query = document.querySelector;
      document.querySelector = (sel) => (sel === "#ns-team" ? null : query(sel));
      await open(base);
      delete elements["ns-team"];
      elements["ns-max"].value = "8";
      fire("click", { team: "inc", composer: "ns", row: "0" });
      out.rows = rows();
      out.sent = await submit();
    """)
    assert out["rows"][0]["count"] == 2
    assert out["sent"]["swarm"]["start_agents"] == 4


def test_the_dialog_keeps_the_hint_of_the_starting_team():
    out = run_dialog("""out.html = await open(base);""")
    assert '<p class="field-hint">The agent of the first row is woken first' in out["html"]


def test_dialog_select_options():
    out = run_dialog(r"""
      out.html = await open({ ...base, teams });
      out.listeners = listeners;
      out.rows = rows();
    """)
    html = out["html"]
    assert html.index('id="ns-team"') < html.index('id="team-ns"')
    assert "<span>Team <" in html and "fills in the roles below; you can still change them" in html
    assert '<option value="">The roles below</option>' in html
    assert '<option value="Coding">Coding (4)</option>' in html and '<option value="Small">Small (2)</option>' in html
    # broken: disabled, the first gone role named instead of its size
    assert '<option value="Broken" disabled>Broken (no role “Gone”)</option>' in html
    assert out["listeners"] == [["change", True], ["input", True]]
    # it opens on the first option, with the saved starting team
    assert out["rows"] == [{"role": "architect", "count": 1}, {"role": "generalist", "count": 2}]


def test_dialog_texts_in_russian():
    out = run_dialog(r"""
      out.html = home.nsTeamPick({ ...base, teams });
    """, lang="ru")
    html = out["html"]
    assert ">Команда <" in html and "заполнит состав ниже; его можно изменить" in html
    assert '<option value="">Роли ниже</option>' in html
    assert '<option value="Broken" disabled>Broken (нет роли «Gone»)</option>' in html


def test_picking_a_team_fills_the_rows_and_the_max():
    out = run_dialog(r"""
      await open({ ...base, teams });
      pick("Coding");
      out.rows = rows();
      out.max = elements["ns-max"].value;
      out.composer = elements["team-ns"].innerHTML;
      out.skills = elements["ns-role-skills"].innerHTML;
      out.sent = home.sessionOverrides(home.H.original, form());
      out.error = home.teamError(home.H.teams.ns, elements["ns-max"].value);
      // a smaller team after a bigger one: the max comes back down to the saved one, or the team's size
      pick("Small");
      out.maxSmall = elements["ns-max"].value;
      out.composerSmall = elements["team-ns"].innerHTML;
      out.sentSmall = home.sessionOverrides(home.H.original, form());
    """)
    assert out["rows"] == [{"role": "architect", "count": 1}, {"role": "dev_gpt", "count": 2},
                           {"role": "researcher", "count": 1}]
    assert str(out["max"]) == "4" and out["error"] is None
    assert "(hidden)" in out["composer"] and "Agents in all: 4" in out["composer"] and "lint-rules" in out["skills"]
    # the rows are what the session is created from: no team name goes to the hub
    assert out["sent"] == {"swarm": {"language": "en", "start_agents": 4, "max_agents": 4, "max_cost_usd": 5,
                                     "starting_roles": ["architect", "dev_gpt", "dev_gpt", "researcher"]}}
    assert str(out["maxSmall"]) == "2" and "Agents in all: 2" in out["composerSmall"]
    # a trailing generalist stays implicit: start_agents carries the total, so the roster is the same
    assert out["sentSmall"] == {"swarm": {"language": "en", "start_agents": 2, "max_agents": 2, "max_cost_usd": 5,
                                          "starting_roles": ["writer"]}}


def test_picking_a_small_team_keeps_a_larger_saved_max():
    out = run_dialog(r"""
      await open({ ...base, swarm: { ...base.swarm, max_agents: 8 }, teams });
      pick("Small");
      out.max = elements["ns-max"].value;
    """)
    assert str(out["max"]) == "8"


def test_a_typed_max_stays_through_a_pick_unless_the_team_is_larger():
    out = run_dialog(r"""
      await open({ ...base, swarm: { ...base.swarm, max_agents: 8 }, teams });
      const steps = [];
      const after = (name) => { pick(name); steps.push(String(elements["ns-max"].value)); };
      typeMax("3");
      after("Small");    // 2 agents: what was typed stays
      after("Coding");   // 4 agents: it has to rise, or the form refuses
      after("Small");    // and comes back to what was typed, not to 4
      typeMax("30");
      after("Small");    // a larger one stays too
      typeMax("");
      after("Small");    // the field emptied: nothing is typed any more
      out.steps = steps;
      typeMax("3");
      await open({ ...base, swarm: { ...base.swarm, max_agents: 8 }, teams });
      pick("Small");     // a new dialog has nothing typed in it
      out.reopened = String(elements["ns-max"].value);
    """)
    assert out["steps"] == ["3", "4", "3", "30", "8"]
    assert out["reopened"] == "8"


DEFAULT_ROLES = ["architect", "tester"]
PIN = r"""
  const pair = { ...base,
                 swarm: { ...base.swarm, start_agents: 2, max_agents: 8, starting_roles: ["architect", "tester"] },
                 teams: [{ name: "Pair", roles: ["architect", "tester"] }, { name: "Prefix", roles: ["architect"] },
                         { name: "Other", roles: ["writer", "writer"] }] };
"""


@pytest.mark.parametrize("team, expected", [("Pair", DEFAULT_ROLES), ("Prefix", ["architect"]),
                                            ("Other", ["writer", "writer"])])
async def test_a_picked_team_is_copied_even_when_the_default_equals_it_today(tmp_path, team, expected):
    """The select names the team, so the session holds it: a default that merely equals it must not stand in for it, or
    the roster would follow whatever the default says at the first start."""
    out = run_dialog(PIN + f"""
      await open(pair);
      pick({json.dumps(team)});
      out.sent = await submit();
    """)
    assert out["sent"]["swarm"]["starting_roles"] == expected and out["sent"]["swarm"]["start_agents"] == len(expected)
    home = tmp_path / "home"
    home.mkdir()
    profile = {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}
    st.write_raw(home, {"swarm": {"language": "en", "start_agents": 2, "max_agents": 8,
                                  "starting_roles": DEFAULT_ROLES},
                        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 0.2},
                        "mcp": {"library_dir": str(tmp_path / "library")}, "models": {"default": profile}})
    manager = SessionManager(home, llm_factory=lambda p, agent: ScriptedLLM(lambda s, m, t: "Nothing to do.", p))
    try:
        sid = (await manager.create("", "Goal", overrides=out["sent"], start=False))["id"]
        await manager.update_settings({"swarm": {"starting_roles": ["reviewer", "reviewer"]}})
        await manager.start(sid)
        names = {"architect": "Architect", "tester": "Tester", "writer": "Technical writer"}
        assert sorted(m["role"] for m in manager.swarms[sid].members()) == sorted(names[role] for role in expected)
    finally:
        await manager.shutdown()


def test_rows_not_from_a_pick_follow_the_old_rule():
    out = run_dialog(PIN + r"""
      await open(pair);
      out.untouched = await submit();
      pick("Pair");
      fire("click", { team: "inc", composer: "ns", row: "0" });
      fire("click", { team: "dec", composer: "ns", row: "0" });    // the select went back: these rows are nobody's
      out.select = elements["ns-team"].value;
      out.edited = await submit();
      fire("change", { team: "role", composer: "ns", row: "0" }, "writer");
      out.other = await submit();
    """)
    assert "starting_roles" not in out["untouched"]["swarm"] and "starting_roles" not in out["edited"]["swarm"]
    assert out["select"] == "" and out["other"]["swarm"]["starting_roles"] == ["writer", "tester"]


def test_a_team_the_settings_no_longer_hold_is_not_applied():
    """Another client saved while the dialog is open: its options are the old ones, H.original the new."""
    out = run_dialog(r"""
      await open({ ...base, teams: [...teams, { name: "WithOwn", roles: ["writer", "dev_gpt"] }] });
      const before = JSON.stringify(rows());
      const max = elements["ns-max"].value;
      const state = () => ({ select: elements["ns-team"].value, same: JSON.stringify(rows()) === before,
                             max: elements["ns-max"].value === max, error: elements["ns-error"].textContent });
      home.H.original = JSON.parse(JSON.stringify({ ...base, roles: [{ name: "researcher", hidden: true }],
        teams: [{ name: "Coding2", roles: ["architect"] }, { name: "WithOwn", roles: ["writer", "dev_gpt"] },
                { name: "Small", roles: ["writer", "generalist"] }] }));
      pick("Coding");
      out.renamed = state();
      pick("WithOwn");
      out.lostRole = state();
      elements["ns-error"].textContent = "old";
      pick("");
      out.first = state();
      pick("Small");
      out.good = { error: elements["ns-error"].textContent, select: elements["ns-team"].value, rows: rows() };
    """)
    assert out["renamed"] == {"select": "", "same": True, "max": True,
                              "error": "The team “Coding” has changed in the settings: open this dialog again."}
    assert out["lostRole"]["select"] == "" and out["lostRole"]["same"] is True
    assert out["lostRole"]["error"] == "The team “WithOwn” has changed in the settings: open this dialog again."
    assert out["first"]["error"] == "old"  # the first option says nothing, and does nothing
    assert out["good"]["error"] == "" and out["good"]["select"] == "Small"
    assert out["good"]["rows"] == [{"role": "writer", "count": 1}, {"role": "generalist", "count": 1}]


def test_a_stale_team_is_said_in_russian():
    out = run_dialog(r"""
      await open({ ...base, teams });
      home.H.original = JSON.parse(JSON.stringify({ ...base, teams: [] }));
      pick("Coding");
      out.error = elements["ns-error"].textContent;
    """, lang="ru")
    assert out["error"] == "Команда «Coding» изменилась в настройках: откройте это окно заново."


@pytest.mark.parametrize("focused, target", [
    ({"team": "role", "row": "1"}, '[data-team="role"][data-row="1"]'),
    ({"team": "inc", "row": "0"}, '[data-team="inc"][data-row="0"]'),
    ({"team": "add"}, '[data-team="add"]'),
])
def test_a_render_gives_the_focus_back_to_the_same_control(focused, target):
    out = run_dialog(FOCUS + f"""
      out.got = render({json.dumps(focused)}, (sel) => sel === {json.dumps(target)});
    """)
    assert out["got"] == [target]


def test_a_render_with_the_row_gone_gives_the_focus_to_the_add_button():
    out = run_dialog(FOCUS + r"""
      out.got = render({ team: "del", row: "5" }, (sel) => sel === '[data-team="add"]');
    """)
    assert out["got"] == ['[data-team="add"]']


def test_a_render_leaves_a_focus_outside_the_composer_alone():
    out = run_dialog(FOCUS + r"""
      elements["team-ns"].contains = () => false;
      out.got = render({ team: "role", row: "0" }, () => true);
    """)
    assert out["got"] == []


FOCUS = r"""
  await open({ ...base, teams });
  const render = (dataset, found) => {
    const focused = [];
    document.activeElement = { dataset };
    const box = elements["team-ns"];
    box.contains = box.contains || (() => true);
    box.querySelector = (sel) => (found(sel) ? { focus: () => focused.push(sel) } : null);
    home.renderTeam("ns");
    return focused;
  };
"""


@pytest.mark.parametrize("edit", ["""fire("click", { team: "inc", composer: "ns", row: "0" });""",
                                  """fire("click", { team: "del", composer: "ns", row: "0" });""",
                                  """fire("click", { team: "add", composer: "ns" });""",
                                  """fire("change", { team: "role", composer: "ns", row: "0" }, "writer");""",
                                  """fire("input", { team: "count", composer: "ns", row: "0" }, "3");"""])
def test_editing_a_row_resets_the_select(edit: str):
    out = run_dialog(f"""
      await open({{ ...base, teams }});
      pick("Coding");
      out.picked = elements["ns-team"].value;
      {edit}
      out.after = elements["ns-team"].value;
    """)
    assert out["picked"] == "Coding" and out["after"] == ""


def test_after_a_row_edit_the_first_option_leaves_the_rows():
    out = run_dialog(r"""
      await open({ ...base, teams });
      pick("Coding");
      fire("click", { team: "inc", composer: "ns", row: "0" });
      const before = JSON.stringify(rows());
      const max = elements["ns-max"].value;
      pick("");
      out.same = JSON.stringify(rows()) === before && elements["ns-max"].value === max;
    """)
    assert out["same"] is True


def test_the_first_option_undoes_a_pick():
    out = run_dialog(r"""
      await open({ ...base, teams });
      const opened = JSON.stringify(rows()), openedMax = elements["ns-max"].value;
      pick("Coding");
      pick("Small");        // from one team to another: back means back to before the first
      pick("");
      out.undone = JSON.stringify(rows()) === opened && elements["ns-max"].value === openedMax;
      out.overrides = home.sessionOverrides(home.H.original, form());
      pick("");             // nothing to undo now
      out.still = JSON.stringify(rows()) === opened;

      fire("click", { team: "inc", composer: "ns", row: "0" });   // the user's own rows come back the same way
      const edited = JSON.stringify(rows());
      pick("Coding");
      pick("");
      out.edited = JSON.stringify(rows()) === edited;

      pick("Coding");
      typeMax("12");        // a max typed after the pick stays
      pick("");
      out.typed = elements["ns-max"].value;

      pick("Coding");
      elements["ns-error"].textContent = "The starting team has 4 agents";   // a refusal of the rows that are now gone
      pick("");
      out.error = elements["ns-error"].textContent;

      await open({ ...base, teams });   // a dialog opened anew has nothing to undo
      pick("");
      out.reopened = JSON.stringify(rows()) === opened;
    """)
    assert out["undone"] is True and out["still"] is True and out["edited"] is True and out["reopened"] is True
    assert "starting_roles" not in out["overrides"]["swarm"] and out["overrides"]["swarm"]["start_agents"] == 3
    assert str(out["typed"]) == "12" and out["error"] == ""


def test_an_undo_shows_on_the_screen():
    out = run_dialog(r"""
      await open({ ...base, teams });
      pick("Coding");
      out.pickedSkills = elements["ns-role-skills"].innerHTML;
      pick("");
      out.composer = elements["team-ns"].innerHTML;
      out.skills = elements["ns-role-skills"].innerHTML;
    """)
    assert "lint-rules" in out["pickedSkills"] and "lint-rules" not in out["skills"]
    assert "Agents in all: 3" in out["composer"]
    assert "dev_gpt" not in re.findall(r'value="([^"]+)" selected', out["composer"])


def test_a_dialog_opened_anew_forgets_the_rows_of_the_last():
    out = run_dialog(r"""
      await open({ ...base, teams });
      const opened = JSON.stringify(rows());
      fire("click", { team: "inc", composer: "ns", row: "0" });
      pick("Coding");                     // and closed, with the pick not undone
      await open({ ...base, teams });
      pick("Small");
      pick("");
      out.same = JSON.stringify(rows()) === opened;
    """)
    assert out["same"] is True


def test_a_max_below_the_team_is_refused():
    out = run_dialog(r"""
      await open({ ...base, teams });
      pick("Coding");
      elements["ns-max"].value = "3";
      out.error = home.teamError(home.H.teams.ns, elements["ns-max"].value);
    """)
    assert out["error"] == "The starting team has 4 agents, more than the maximum (3)."


# ------------------------------------------------------------------------------------------------ escaping
def test_team_and_role_names_are_escaped():
    out = run_dialog(r"""
      const evil = process.env.EVIL;
      out.pick = home.nsTeamPick({ ...base, teams: [{ name: evil, roles: ["writer"] }, { name: "b", roles: [evil] }] });
      out.card = home.teamCardHtml({ uid: "tm1", name: evil });
      home.H.teams.tm2 = [{ role: evil, count: 1 }];
      out.gone = home.teamCardHtml({ uid: "tm2", name: "ok" });
    """)
    for html in (out["pick"], out["card"], out["gone"]):
        assert "<img" not in html and "&lt;img" in html and 'onerror="' not in html


def test_a_catalog_role_with_markup_is_escaped_in_a_team_card():
    settings = json.loads(json.dumps(SETTINGS))
    settings["roles"] = [{"name": EVIL, "description": EVIL, "model": EVIL}]
    settings["teams"] = [{"name": "x", "roles": [EVIL]}]
    out = run_settings("out.card = home.teamCardHtml(tcard(0));", settings=settings)
    assert "<img" not in out["card"] and 'onerror="' not in out["card"]


# ------------------------------------------------------------------------------------------------ texts
def test_section_texts():
    out = run_settings(r"""
      out.ru = home.I18N.ru.s_teams;
      out.en = home.I18N.en.s_teams;
    """)
    assert out["ru"] == "Команда" and out["en"] == "Teams"
