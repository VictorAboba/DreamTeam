"""prompts.py: ru/en parity, placeholders used by callers, tool references, the guidance that drives
self-organization, and a Russian end-to-end run of a scripted swarm."""

from __future__ import annotations

import ast
import asyncio
import re
import string
import time
from pathlib import Path
from typing import Any

import pytest

from dreamteam import prompts
from dreamteam import swarm as swarm_module
from dreamteam.config import Config, ShellSettings
from dreamteam.llm.scripted import ScriptedLLM
from dreamteam.models import USER, Status
from dreamteam.swarm import Swarm
from dreamteam.tools import REGISTRY, tools_for

SRC = Path(prompts.__file__).resolve().parent
LANGS = ("en", "ru")
SYSTEMS = {"en": prompts.SYSTEM_EN, "ru": prompts.SYSTEM_RU}
SYSTEM_KW = dict(name="Ann Lee", swarm="Swarm X", role="Tester", role_description="Checks things.",
                 instructions="Check {curly} things", shell="bash", scratch=".scratch/ann.lee/", max_agents=7,
                 foreground_max=95)
_FMT = string.Formatter()
_CODE_IDENT = re.compile(r"`([a-z_]+)`")


def placeholders(template: str) -> set[str]:
    return {field for _, field, _, _ in _FMT.parse(template) if field is not None}


def code_idents(text: str) -> set[str]:
    """Identifiers written in backticks: tool names, statuses, parameter names."""
    return set(_CODE_IDENT.findall(text))


# ------------------------------------------------------------------------------------- parity
def test_languages_have_the_same_keys_placeholders_and_tool_names():
    assert set(prompts.TEXTS) == set(LANGS)
    assert set(prompts.TEXTS["en"]) == set(prompts.TEXTS["ru"])
    for key, en in prompts.TEXTS["en"].items():
        ru = prompts.TEXTS["ru"][key]
        assert placeholders(en) == placeholders(ru), key
        assert code_idents(en) == code_idents(ru), key


def test_system_prompts_have_the_same_placeholders_and_tool_names():
    assert placeholders(prompts.SYSTEM_EN) == placeholders(prompts.SYSTEM_RU) == {
        "name", "swarm", "role", "role_description", "instructions", "shell", "scratch", "max_agents",
        "foreground_max"}
    assert code_idents(prompts.SYSTEM_EN) == code_idents(prompts.SYSTEM_RU)


def test_planning_task_and_default_role_exist_in_both_languages():
    for lang in LANGS:
        title, goal, description = prompts.PLANNING_TASK[lang]
        assert title and goal and description and len(title) <= 200
        assert "depends_on" in description
        # contract-first planning: the shared contracts are written once, to CONTRACTS.md
        assert "CONTRACTS.md" in description and "CONTRACTS.md" in goal
        assert "`agents`" in description  # hiring picks a role from the role catalog
        assert prompts.DEFAULT_ROLE[lang]


PLANNING_SNIPPETS = {
    # step: (en, ru) phrases the step must contain
    2: (("Work that needs no contracts", "literature", "data", "before CONTRACTS.md is written",
         "Only you create the tasks of the plan"),
        ("Работа, которой не нужны контракты", "литература", "данные", "ещё до того, как написан CONTRACTS.md",
         "Задачи плана создаёшь только ты")),
    3: (("Then the contracts", "Before creating the other tasks"),
        ("Затем контракты", "Прежде чем создавать остальные задачи")),
    4: (("Then the rest of the tasks",), ("Затем остальные задачи",)),
    5: (("depends only on the tasks without whose results it cannot be written", "exploratory and optional tasks do "
         "not block it", "still open"),
        ("зависит только от задач, без результатов которых её нельзя написать", "исследовательские и необязательные "
         "задачи её не блокируют", "ещё открыты")),
}


def test_planning_task_starts_independent_work_before_the_contracts():
    """The members waiting for the plan get work at once: tasks that need no contracts come before CONTRACTS.md,
    and a final task waits only for what it cannot be written without. Both languages number the same steps."""
    steps = {}
    for lang in LANGS:
        description = prompts.PLANNING_TASK[lang][2]
        steps[lang] = {int(m.group(1)): m.group(2) for m in re.finditer(r"^(\d+)\. (.*)$", description, re.M)}
        assert sorted(steps[lang]) == list(range(1, 10)), lang
    for number, (en, ru) in PLANNING_SNIPPETS.items():
        for lang, snippets in (("en", en), ("ru", ru)):
            for snippet in snippets:
                assert snippet in steps[lang][number], (lang, number, snippet)
    # the others are told that tasks come before the plan is complete; new members learn the order
    assert "before the plan is complete" in prompts.TEXTS["en"]["n_kickoff"]
    assert "до того, как план готов" in prompts.TEXTS["ru"]["n_kickoff"]
    assert "tasks that need no contracts at once, then the shared contracts" in prompts.SYSTEM_EN
    assert "сразу задачи, которым не нужны контракты, затем общие контракты" in prompts.SYSTEM_RU
    assert "a final task (report, summary, release) depends only on the tasks it cannot be written without" in \
        prompts.SYSTEM_EN
    assert "итоговая задача (отчёт, сводка, релиз) зависит только от задач, без которых её нельзя написать" in \
        prompts.SYSTEM_RU


def test_dependency_removal_texts():
    """The assignee of a blocked task may drop dependencies (Board._plan_deps): the wake-up line, the creator's
    notification and the discussion record exist in both languages with the same placeholders."""
    for lang in LANGS:
        texts = prompts.TEXTS[lang]
        assert placeholders(texts["my_blocked"]) == {"id", "blockers"}
        assert "`task_update`" in texts["my_blocked"] and "depends_on" in texts["my_blocked"]
        assert placeholders(texts["n_deps_removed"]) == {"by", "id", "title", "deps"}
        assert placeholders(texts["post_deps_removed"]) == {"by", "deps"}
        # removed blockers are not done: the unblock news must not claim they are
        assert placeholders(texts["n_unblocked_removed"]) == {"by", "id", "title", "deps"}
        assert "`task_claim`" in texts["n_unblocked_removed"]


def test_planning_task_title_follows_its_steps():
    """The title (shown in n_kickoff and on the board) names the same order as the steps: independent tasks before
    the contracts, the rest after them."""
    for lang, (independent, contracts, rest) in {"en": ("independent tasks", "contracts", "the rest"),
                                                  "ru": ("независимые задачи", "контракты", "остальное")}.items():
        title = prompts.PLANNING_TASK[lang][0]
        assert title.index(independent) < title.index(contracts) < title.index(rest), (lang, title)


def test_clarify_planning_task_keeps_the_steps_and_asks_first():
    """The clarify card ([swarm] clarify) asks the user first: its head replaces the first line of the classic
    description and the nine steps follow verbatim, so every snippet of the classic card still holds; the head
    overrides the system prompt's "continue with the work that doesn't depend on the answer" and names only tools
    every revision a session this version opens has (from frozen.OLDEST_OPEN_REVISION on; no pause_swarm: the card
    never relies on it)."""
    from dreamteam.tools import frozen
    from dreamteam.tui.printmode import AUTO_REPLY

    shared: set[str] | None = None
    for rev in (r for r in frozen.revisions() if r >= frozen.OLDEST_OPEN_REVISION):
        for lang in LANGS:
            names = set(frozen.names(frozen.snapshot(rev, lang)))
            shared = names if shared is None else shared & names
    allowed = (shared or set()) | set(Status.ALL)
    idents = {}
    for lang, words in {"en": ("independent tasks", "contracts", "the rest", "asked", "decide yourself",
                               "This time, unlike the usual rule, do not continue with the work that doesn't depend "
                               "on the answer"),
                        "ru": ("независимые задачи", "контракты", "остальное", "заданы вопросы", "прими решение сам",
                               "На этот раз, в отличие от обычного правила, не продолжай работу, которая не зависит "
                               "от ответа")}.items():
        independent, contracts, rest, asked, decide, override = words
        card = prompts.planning_task(lang, True)
        assert card == prompts.PLANNING_CLARIFY[lang] and len(card) == 3
        title, goal, description = card
        assert len(title) <= 200 and title.index(independent) < title.index(contracts) < title.index(rest)
        assert "depends_on" in description and "CONTRACTS.md" in description and "CONTRACTS.md" in goal
        assert "`agents`" in description
        assert goal.endswith(prompts.PLANNING_TASK[lang][1]) and asked in goal[:160]
        # the numbered steps are exactly the classic ones, and nothing before step 1 is numbered
        steps = {int(m.group(1)): m.group(2) for m in re.finditer(r"^(\d+)\. (.*)$", description, re.M)}
        classic = {int(m.group(1)): m.group(2)
                   for m in re.finditer(r"^(\d+)\. (.*)$", prompts.PLANNING_TASK[lang][2], re.M)}
        assert sorted(steps) == list(range(1, 10)) and steps == classic
        head = description[:description.index("\n1. ")]
        assert not re.search(r"^\d+\. ", head, re.M)
        assert description.endswith(prompts.PLANNING_TASK[lang][2].split("\n", 1)[1])
        for number, snippets in PLANNING_SNIPPETS.items():
            for snippet in snippets[LANGS.index(lang)]:
                assert snippet in steps[number], (lang, number, snippet)
        assert override in head and '["user"]' in head
        assert decide in head and decide in AUTO_REPLY[lang]  # what -p answers in place of the user
        idents[lang] = code_idents(title + goal + description)
        assert "pause_swarm" not in idents[lang] and "pause_swarm" not in description
        assert idents[lang] <= allowed, (lang, idents[lang] - allowed)
        assert {"send_message", "waiting", "task_update", "wait", "in_progress"} <= code_idents(head)
        # the classic card is unchanged, byte for byte (tests and the TUI compare with PLANNING_TASK)
        assert prompts.planning_task(lang, False) is prompts.PLANNING_TASK[lang]
        assert prompts.planning_task(lang) is prompts.PLANNING_TASK[lang]
    assert idents["en"] == idents["ru"]
    assert prompts.planning_task("de", True) == prompts.PLANNING_CLARIFY["en"]
    assert prompts.CLARIFY_TAG == "clarify"


def test_question_texts():
    """The texts of a member's question that pauses the swarm (docs/SPEC.md §3 "Clarify mode"): the same
    placeholders in both languages; only the user's notice names a tool (swarm_control, a registry tool)."""
    expected = {"n_user_answer": {"asker", "id", "question", "answer"},
                "n_user_answer_seen": {"asker", "id", "question", "answer_id"},
                "n_question_unanswered": {"id"}, "n_clarify_off": {"task_id"},
                "u_question": {"name", "id"}, "u_question_left": {"name", "id"}}
    for lang in LANGS:
        texts = prompts.TEXTS[lang]
        for key, fields in expected.items():
            assert placeholders(texts[key]) == fields, (lang, key)
            assert "pause_swarm" not in texts[key], (lang, key)
        assert code_idents(texts["u_question"]) == {"swarm_control"}
        assert code_idents(texts["n_clarify_off"]) == {"in_progress", "waiting"}
        for key in ("n_user_answer", "n_user_answer_seen", "n_question_unanswered"):
            assert not code_idents(texts[key]), (lang, key)
        # the user is told what answers the question and how to go on without an answer
        assert "/pause" in texts["u_question"] and "/resume" in texts["u_question"]
        rendered = prompts.t(lang, "n_user_answer", asker="Anna Smith", id=7, question="Q {x}?", answer="A {y}")
        assert "Anna Smith" in rendered and "#7" in rendered and "Q {x}?" in rendered and "A {y}" in rendered


def test_texts_reference_only_real_tools():
    llm_tools = {s.name for s in tools_for("llm")}
    params = {p for spec in REGISTRY.values() for p in spec.parameters.get("properties", {})}
    common = set(Status.ALL) | params
    for lang in LANGS:
        unknown = code_idents(SYSTEMS[lang]) - llm_tools - common
        assert not unknown, f"system prompt ({lang}) names unknown tools: {unknown}"
        for key, text in prompts.TEXTS[lang].items():
            # u_* texts go to the user, who has the user tools (e.g. mcp_approve); the rest go to agents
            allowed = (set(REGISTRY) if key.startswith("u_") else llm_tools) | common
            assert not code_idents(text) - allowed, (lang, key, code_idents(text) - allowed)


def test_agent_texts_name_only_baseline_tools():
    """Notifications and wake-up texts reach agents of every tool set revision a session this version opens
    (tools/frozen.py: an agent keeps its tools until a compaction; sessions with a member below
    frozen.OLDEST_OPEN_REVISION are not opened), so they may name only the tools and parameters that every shipped
    revision from OLDEST_OPEN_REVISION on has, plus statuses. A text that points at a newer tool is either tailored
    by its caller with ToolContext.has_tool (listed in TAILORED) or would send old agents to a tool they lack. The
    system prompts are exempt: only new members, who get the current revision, receive them."""
    from dreamteam.tools import frozen

    tailored: set[str] = set()
    shared: set[str] | None = None
    for rev in (r for r in frozen.revisions() if r >= frozen.OLDEST_OPEN_REVISION):
        for lang in LANGS:
            tools = frozen.snapshot(rev, lang)
            names = set(frozen.names(tools)) | {p for t in tools for p in t["parameters"].get("properties", {})}
            shared = names if shared is None else shared & names
    allowed = (shared or set()) | set(Status.ALL)
    for lang in LANGS:
        for key, text in prompts.TEXTS[lang].items():
            if key.startswith("u_") or key in tailored:
                continue
            assert not code_idents(text) - allowed, (lang, key, code_idents(text) - allowed)


def test_lock_texts():
    """File locks (docs/SPEC.md §7 "Lock waiters"): the waiter's wake-up and the holder's notice name only
    `lock_file`, say that no reply is needed, and never put a path in backticks."""
    for lang in LANGS:
        texts = prompts.TEXTS[lang]
        assert placeholders(texts["n_lock_free"]) == {"paths"}
        assert placeholders(texts["n_lock_wanted"]) == {"path", "held", "waiters"}
        assert placeholders(texts["lock_waiter_task"]) == {"name", "tasks"}
        for key in ("n_lock_free", "n_lock_wanted"):
            assert code_idents(texts[key]) == {"lock_file"} and "`{" not in texts[key], (lang, key)
        assert ("No reply is needed" if lang == "en" else "Отвечать не нужно") in texts["n_lock_wanted"]
    text = prompts.t("en", "n_lock_wanted", path="docs/AUDIT.md", held="6 min", waiters="Anna Smith (task #7 Parser)")
    assert text.startswith("docs/AUDIT.md, which you have held for 6 min, is wanted by: Anna Smith (task #7 Parser).")


def test_trigger_texts():
    """Trigger notices (docs/SPEC.md §8): the file_changed() fragments go after "{condition}." and start with a space
    (`by` with its own separator), the held notice says how often it may wake; none names a tool."""
    for lang in LANGS:
        texts = prompts.TEXTS[lang]
        assert placeholders(texts["n_trigger_files"]) == {"id", "condition", "changes", "note"}
        assert placeholders(texts["trigger_file_settled"]) == {"path", "count", "by", "quiet"}
        assert placeholders(texts["trigger_file_changing"]) == {"path", "count", "by", "duration"}
        assert placeholders(texts["trigger_file_by"]) == {"names"}
        assert placeholders(texts["n_trigger_held"]) == {"id", "condition", "count", "since", "last", "interval",
                                                         "changes", "note"}
        # the limit switched off with fires held: the same notice without the interval sentence
        assert placeholders(texts["n_trigger_held_off"]) == placeholders(texts["n_trigger_held"]) - {"interval"}
        limit = {"en": " A repeating trigger wakes you at most once every {interval}.",
                 "ru": " Повторяющийся триггер будит не чаще раза в {interval}."}[lang]
        assert texts["n_trigger_held_off"] == texts["n_trigger_held"].replace(limit, "")
        keys = ("n_trigger_files", "trigger_file_settled", "trigger_file_changing", "trigger_file_by", "n_trigger_held",
                "n_trigger_held_off")
        for key in keys:
            assert not code_idents(texts[key]) and "`" not in texts[key], (lang, key)
        for key in ("n_trigger_files", "n_trigger_held", "n_trigger_held_off"):
            assert "{condition}.{changes} " in texts[key] and texts[key].endswith(" {note}"), (lang, key)
        assert texts["trigger_file_settled"].startswith(" ") and texts["trigger_file_changing"].startswith(" ")
        assert texts["trigger_file_by"].startswith((", ", "; "))


def test_cost_unknown_texts():
    """The user's notice about calls counted as $0 (docs/SPEC.md §10): the profile, the model and the missing price
    fields, where to set the prices and that 0 means free; it names no tool."""
    for lang in LANGS:
        text = prompts.TEXTS[lang]["u_cost_unknown"]
        assert placeholders(text) == {"profile", "model", "fields"} and not code_idents(text) and "`" not in text
        assert "$0" in text and "dreamteam.toml" in text and " 0" in text
    text = prompts.t("en", "u_cost_unknown", profile="remote", model="some-model-v1", fields="output_price")
    assert text.startswith('DreamTeam cannot count the cost of model profile "remote" (model some-model-v1): it has '
                           "no output_price, and the provider does not report the cost of a call")
    assert "Settings → Models and endpoints" in text
    assert "«Настройки» → «Модели и эндпоинты»" in prompts.TEXTS["ru"]["u_cost_unknown"]


def test_background_job_texts():
    for lang in LANGS:
        texts = prompts.TEXTS[lang]
        # a job's whole output is in its log, which every agent can read (read_file, shell)
        assert "{log}" in texts["n_job_done"] and "{log}" in texts["n_job_gone"]
        assert "{log}" in texts["n_job_command_done"]
        for key in ("my_jobs", "my_job", "job_exit", "job_gone", "job_no_output", "n_job_done", "n_job_command_done",
                    "n_job_stopped", "n_job_limit", "n_job_gone", "u_jobs_running"):
            assert not code_idents(texts[key]) and "job_stop" not in texts[key], (lang, key)
            # a job is shown as j3 (#3 is a task); in Russian it is a «фоновый процесс», «задача» is a board task
            assert "#{id}" not in texts[key], (lang, key)
            if lang == "ru":
                assert "задач" not in texts[key].lower(), key
        assert "j{id}" in texts["my_job"] and "{tools}" in texts["tools_added"]
    # the command of a job ended while programs it started run on: the job goes on, so its notice never says that the
    # job has ended (the job's own end comes later, with n_job_done)
    assert "has ended" not in prompts.TEXTS["en"]["n_job_command_done"]
    assert "процесс j{id} завершился" not in prompts.TEXTS["ru"]["n_job_command_done"]


@pytest.mark.parametrize("lang", LANGS)
def test_system_prompt_teaches_background_jobs(lang):
    """New members (revision 2 of the tool set) learn the jobs protocol: a command still running at its timeout, or
    after [shell] foreground_max seconds, goes on as a background job; long runs start with `background`, then
    `wait`; `jobs` and `job_stop` show and stop a job; the whole output is in the scratch folder. The end of a job
    is among the events that wake the member (How you work, item 10)."""
    system = SYSTEMS[lang]
    for snippet in ("`{scratch}jobs/`", "`background`", "`jobs`", "`job_stop`", "`timeout`", "{foreground_max}",
                    "(j1, j2, …)"):
        assert snippet in system, snippet
    shared = system[system.index("`shell`"):system.index("`{scratch}jobs/`")]
    assert "`wait`" in shared and "{foreground_max}" in shared  # long runs: background, then wait
    item10 = next(line for line in system.splitlines() if line.startswith("10. "))
    assert "`wait`" in item10
    assert ("background jobs" if lang == "en" else "фоновые процессы") in item10
    assert ("the end of your background jobs wake you" if lang == "en"
            else "завершение твоих фоновых процессов") in item10
    if lang == "ru":  # a job is a «фоновый процесс»: «задача» is a board task
        assert "фоновая задача" not in system.lower()
    # the team limit counts the AI agents: members who joined through MCP do not count
    assert ("not counting members who joined through MCP" if lang == "en"
            else "не считая участников, подключившихся через MCP") in system
    text = prompts.system_prompt(lang, **SYSTEM_KW)
    assert "95" in text and ".scratch/ann.lee/jobs/" in text


@pytest.mark.parametrize("lang", LANGS)
def test_system_prompt_teaches_skills(lang):
    """New members learn that skills exist, that the first wake-up lists them and later ones name them, and that a
    skill's SKILL.md is read with `read_file` before work it fits; item 1 says the wake-up shows them."""
    system = SYSTEMS[lang]
    bullet = next(line for line in system.splitlines() if line.startswith("- **Skills**" if lang == "en"
                                                                          else "- **Скиллы**"))
    assert "SKILL.md" in bullet and "`read_file`" in bullet
    assert ("The first wake-up lists them" if lang == "en" else "Первое сообщение о пробуждении перечисляет") in bullet
    item1 = next(line for line in system.splitlines() if line.startswith("1. "))
    assert ("the skills attached to the swarm" if lang == "en" else "подключённые к рою скиллы") in item1
    if lang == "ru":
        assert "скилл" in bullet and "навык" not in system.lower()
    # new members have skill_propose (revision 3): a procedure that worked can be proposed as a skill
    assert "`skill_propose`" in bullet and "`name`" in bullet and "`description`" in bullet
    assert ("the user decides whether it is kept" if lang == "en" else "пользователь решит, сохранить ли его") in bullet


def test_skill_texts():
    """The index and its notifications: the same placeholders in both languages, the Claude Code tool mapping, a
    literal ${CLAUDE_SKILL_DIR} (not a placeholder) and only tools every revision has."""
    for lang in LANGS:
        texts = prompts.TEXTS[lang]
        for key in ("skills_index", "skills_index_own"):
            assert placeholders(texts[key]) == {"skills"}, (lang, key)
            rendered = prompts.t(lang, key, skills="- x")
            assert "${CLAUDE_SKILL_DIR}" in rendered and rendered.endswith("\n- x")
            assert {"read_file", "shell", "write_file", "edit_file"} <= code_idents(texts[key])
        assert "«" in texts["skills_index_own"] or "\"Your role's\"" in texts["skills_index_own"]
        assert placeholders(texts["skills_item"]) == {"name", "own", "description", "path", "deps"}
        assert placeholders(texts["skills_names"]) == {"names"}
        assert placeholders(texts["skills_path_outside"]) == {"path"} and "`shell`" in texts["skills_path_outside"]
        for key in ("n_skill_added", "n_skill_updated"):
            assert placeholders(texts[key]) == {"name", "description", "path", "deps"} and "`read_file`" in texts[key]
        assert placeholders(texts["n_skill_removed"]) == {"name"}
        # proposals and the user's decision; agents of every revision get these, so they name no skill_propose
        assert placeholders(texts["n_skill_shared"]) == {"owner", "name", "description", "path", "deps"}
        assert placeholders(texts["n_skill_saved"]) == {"name"}
        assert placeholders(texts["n_skill_not_saved"]) == {"name", "comment"}
        assert placeholders(texts["u_skill_proposed"]) == {"owner", "name", "description", "files", "installs"}
        assert "`skill_approve`" in texts["u_skill_proposed"]
        assert placeholders(texts["u_role_skills_missing"]) == {"role", "names"}
        assert placeholders(texts["u_role_skills_over_cap"]) == {"role", "names", "max"}
        for key in ("n_skill_shared", "n_skill_saved", "n_skill_not_saved"):
            assert "skill_propose" not in texts[key], (lang, key)


def test_skill_deps_texts():
    """A skill's script dependencies (skill_deps.py): fragments of the index line start with a space, name only
    `shell` / `read_file` (every revision has them), and the reasons render inside skills_deps_manual without a
    sentence of their own; the user's failure text points at skill_reinstall."""
    deps_keys = [k for k in prompts.TEXTS["en"] if k.startswith(("skills_deps_", "deps_reason_", "n_skill_deps_"))]
    assert len(deps_keys) == 17   # deps_reason_network: a session without network (docs/SPEC.md §7.2)
    for lang in LANGS:
        texts = prompts.TEXTS[lang]
        # an AI member's "being installed" promises no wake-up: the news of the end reaches it quietly
        assert placeholders(texts["skills_deps_installing_quiet"]) == placeholders(texts["skills_deps_installing"])
        assert ("does not wake you" if lang == "en" else "не будит") in texts["skills_deps_installing_quiet"]
        for key in deps_keys:
            assert code_idents(texts[key]) <= {"shell", "read_file"}, (lang, key)
            if key.startswith("skills_deps_"):
                assert texts[key].startswith(" "), (lang, key)
            if key.startswith("deps_reason_"):
                assert "." not in texts[key] and "(" not in texts[key] and ")" not in texts[key], (lang, key)
        assert code_idents(texts["u_skill_deps_failed"]) == {"skill_reinstall"}
        assert not code_idents(texts["u_skill_installs"]) and texts["u_skill_installs"].startswith(" ")
        assert placeholders(texts["u_skill_deps_failed"]) == {"name", "files", "error", "log"}
        assert placeholders(texts["u_skill_installs"]) == {"files", "packages"}
        ending = {k for k, v in texts.items() if v.endswith("{deps}")}
        assert ending == {"n_skill_added", "n_skill_updated", "n_skill_shared", "n_skill_deps_ready",
                          "n_skill_deps_failed", "skills_item"}
        assert texts["skills_item"].endswith("{path}{deps}")
        # the index header says what a line with or without packages means (the frozen system prompt does not)
        for key in ("skills_index", "skills_index_own"):
            text = prompts.t(lang, key, skills="- x")
            assert "requirements.txt, pyproject.toml" in text and "npm ci" in text, (lang, key)
        assert ("Your role's" if lang == "en" else "«Твоей роли»") in prompts.t(lang, "skills_index_own", skills="")
        # a path is never put in backticks: code_idents would read a piece of it as a tool name
        assert not any("`{" in texts[k] for k in deps_keys), lang


# ------------------------------------------------------------------------------- crew tasks
# every text of crew tasks and subtasks (docs/SPEC.md §4.2) with its placeholders: the board, the scheduler and the
# workspace render them by these names, in both languages
CREW_KEYS = {
    "n_subtasks_done": {"id", "title", "total", "counts", "ending"},
    "n_direction_failed": {"by", "id", "title", "parent"},
    "n_direction_cancelled": {"by", "id", "title", "parent"},
    "n_subtask_cancelled": {"by", "id", "title", "parent"},
    "n_direction_taken": {"by", "id", "title", "parent"},
    "n_direction_freed": {"id", "title", "by"},
    "n_crew_owner": {"id", "title", "crew", "directions", "instructions"},
    "n_crew_changed": {"by", "id", "title", "crew"},
    "n_parent_closed": {"id", "title", "status", "sub"},
    "n_crew_review_requested": {"by", "id", "title", "n"},
    "n_marks_stalled": {"text"},
    "crew_draft": {"id", "items"}, "crew_draft_cut": {"id"}, "crew_draft_more": {"k", "id"},
    "crew_draft_no_result": set(),
    "count_done": {"n"}, "count_cancelled": {"n"}, "count_failed": {"n"},
    "ending_plain": set(), "ending_plain_creator": {"id"},
    "ending_crew_failed": {"failed", "id"}, "ending_crew_failed_creator": {"failed", "id"},
    "ending_crew_cancelled": set(), "ending_crew_cancelled_creator": {"id"},
    "ending_crew_finished": {"id"}, "ending_crew_finished_creator": {"id"},
    "owner_prefix_plain": set(), "owner_prefix_failed": set(), "owner_prefix_cancelled": set(),
    "owner_prefix_finished": set(),
    "review_rejected_plain": set(), "review_rejected_crew": {"id"},
    "crew_owner_take": {"id", "crew"}, "crew_owner_own": {"id", "crew", "sub", "status"},
    "crew_owner_all_taken": {"id", "crew"}, "crew_owner_none": {"id"},
    "crew_directions": {"items"}, "crew_directions_more": {"k", "id"},
    "direction_owner_named": {"name"}, "direction_owner_none": set(), "direction_owner_you": {"id"},
    "note_direction_card": {"sub", "id", "owner"}, "note_reviewer_cleared": {"id"},
    "note_own_part": {"sub", "title", "id"}, "note_own_part_blocked": {"sub", "title", "id", "blockers"},
    "note_still_open": {"sub"}, "note_crew_say_why": {"id"},
    "mark_subtasks_done": {"id", "title", "ending"}, "mark_free_directions": {"id", "directions"},
    "mark_direction_to_take": {"sub", "title", "id"},
    "lock_waiter_direction": {"id", "title", "parent"},
    # held directions (docs/SPEC.md §4.2 "A held direction", "Also assigned")
    "n_direction_held_busy": {"by", "id", "title", "parent", "m"},
    "n_direction_held_full": {"by", "id", "title", "parent", "working", "crew", "names"},
    "note_direction_held_busy": {"sub", "id", "name", "m", "working", "crew"},
    "note_direction_held_busy_you": {"sub", "id", "m"},
    "note_direction_held_full": {"sub", "id", "name", "working", "crew", "names"},
    "note_direction_held_full_you": {"sub", "id", "working", "crew", "names"},
    "note_direction_held_tail": {"sub", "id", "name"}, "note_direction_held_tail_you": {"sub"},
    "note_direction_held_also": {"sub", "id", "m"},
}
# changed keys of existing texts
CHANGED_KEYS = {"n_review_rejected": {"by", "id", "title", "comment", "ending"}, "n_stalled": {"tasks", "marks"}}


def test_crew_texts_have_their_placeholders():
    for lang in LANGS:
        texts = prompts.TEXTS[lang]
        for key, fields in {**CREW_KEYS, **CHANGED_KEYS}.items():
            assert placeholders(texts[key]) == fields, (lang, key)
        # fragments appended as they are start with a space; the ones after "...: " do not
        for key in CREW_KEYS:
            spaced = key.startswith(("crew_owner_", "crew_directions", "note_", "mark_"))
            assert texts[key].startswith(" ") == spaced, (lang, key)
        for key in ("n_stalled", "n_review_rejected"):
            assert texts[key].endswith(("{marks}", "\n{ending}")), (lang, key)
        assert texts["n_marks_stalled"] == "{text}"
        for key in ("owner_prefix_plain", "owner_prefix_failed", "owner_prefix_cancelled", "owner_prefix_finished"):
            assert texts[key].endswith(": "), (lang, key)
    # the old n_stalled text is kept, with the marks after it; without marks it reads as before
    assert prompts.t("en", "n_stalled", tasks="#3", marks="").endswith("with a comment.")


def test_crew_texts_say_crew_task_and_direction_in_russian():
    """Russian texts call a crew task «групповая задача» and a direction «направление», never «команда» (a team of
    roles is a «команда»); the tools they name in parentheses are real tools and parameters."""
    ru = prompts.TEXTS["ru"]
    for key in CREW_KEYS:
        assert "команд" not in ru[key].lower(), key
    for key in ("n_direction_failed", "n_direction_cancelled", "n_direction_taken", "n_direction_freed",
                "n_crew_review_requested", "crew_owner_take", "mark_free_directions", "note_direction_card",
                "mark_direction_to_take", "n_direction_held_busy", "n_direction_held_full", "note_direction_held_busy",
                "note_direction_held_busy_you", "note_direction_held_also"):
        assert "групповой задачи" in ru[key] or "групповая задача" in ru[key], key
        assert "направлен" in ru[key], key
    for key in ("note_direction_held_full", "note_direction_held_full_you"):  # the places of a crew task
        assert "в групповой задаче #{id}" in ru[key], key
    for text in (prompts.SYSTEM_RU, prompts.planning_task("ru")[2]):
        lines = {int(m.group(1)): m.group(2) for m in re.finditer(r"^(\d+)\. (.*)$", text, re.M)}
        for number in (3, 6, 8) if text is prompts.SYSTEM_RU else (4, 9):
            assert "команд" not in lines[number].lower(), number
            assert "групповую задачу" in lines[number] or "групповой задач" in lines[number] \
                or "групповая задача" in lines[number].lower() or number == 6, number
    known = set(REGISTRY) | {p for spec in REGISTRY.values() for p in spec.parameters.get("properties", {})}
    for lang in LANGS:
        for key in [*CREW_KEYS, *CHANGED_KEYS]:
            words = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", prompts.TEXTS[lang][key]))
            assert words <= known, (lang, key, words - known)


def test_crew_texts_render_the_spec_wording():
    """The English texts docs/SPEC.md §4.2 fixes word for word, rendered."""
    t = prompts.t
    assert t("en", "n_subtasks_done", id=7, title="auth", total=6,
             counts=", ".join([t("en", "count_done", n=5), t("en", "count_cancelled", n=1)]),
             ending=t("en", "ending_plain")) == \
        'All 6 subtasks of #7 "auth" are finished (5 done, 1 cancelled): assemble their results and complete it.'
    assert t("en", "n_crew_review_requested", by="Anna Smith", id=7, title="auth", n=3) == (
        '#7 "auth" (sent by Anna Smith) is a crew task whose 3 directions are done: check it as a whole — do the '
        "directions fit together, and does the result meet its goal? Approve it, or return it with what does not fit "
        "(task_review).")
    assert t("en", "crew_owner_take", id=7, crew=3) == (
        " You own crew task #7: take one of its directions yourself (task_claim; with #7 it counts as one task in "
        "progress); the other members take the rest (up to 3 members at once, you included) and write their cards. "
        "Keep its directions consistent: add the ones missing (task_update directions), cancel the ones not needed, "
        "and settle the interfaces between them in #7's discussion. Each direction is handed in on its own; when all "
        "of them are finished, the board tells you and gives you their results as a draft: then check that they fit "
        "together, write #7's result and complete it (task_complete).")
    take, own = t("en", "crew_owner_take", id=7, crew=3), t("en", "crew_owner_own", id=7, crew=3, sub=15,
                                                            status="in_progress")
    assert own == take.replace("take one of its directions yourself (task_claim; with #7 it counts as one task in "
                               "progress)", "your own direction is #15 [in_progress]")
    assert t("en", "crew_owner_all_taken", id=7, crew=3).startswith(
        " You own crew task #7: all its directions are taken: if a part of #7 is left that nobody does, add it as a "
        "direction (task_update directions) and it is yours; otherwise keep the directions consistent until they are "
        "finished, then complete #7; the other members")
    assert t("en", "crew_owner_none", id=7).endswith(
        " It has no directions yet: add them now (task_update directions; the first one is your own part), so the "
        "others can join; if #7 needs no split after all, make it an ordinary task (crew 0), or do it yourself and "
        "complete it (task_complete).")
    owner_form = t("en", "owner_prefix_plain") + t("en", "ending_plain")
    assert t("en", "mark_subtasks_done", id=7, title="auth", ending=owner_form) == \
        ' #7 "auth": All its subtasks are finished: assemble their results and complete it.'
    assert t("en", "mark_free_directions", id=7, directions='#17 "oidc"') == (
        ' #7 is a crew task with free directions nobody has taken: #17 "oidc". Take one yourself (task_claim), or '
        "cancel the ones it does without (task_update status=cancelled).")
    assert t("en", "crew_draft", id=7, items="- #15") == ("Draft for #7's result: its directions in the order they "
                                                          "were created, with the results of the done ones:\n- #15")
    assert t("en", "crew_draft_more", k=2, id=7) == \
        "… more directions not shown: 2; task_get #7 lists them, and task_get on a direction shows its result."
    assert t("ru", "crew_draft_more", k=2, id=7).startswith("… ещё не показано направлений: 2; ")
    assert t("en", "review_rejected_crew", id=7) == (
        "#7 is a crew task: fix it yourself, or with new directions (task_update directions) and, once they are "
        "finished, update #7's result; then call `task_complete` again.")
    assert t("ru", "review_rejected_crew", id=7) == (
        "#7 — групповая задача: исправь её сам(а) или новыми направлениями (task_update directions) и, когда они "
        "завершатся, обнови результат #7; затем снова вызови `task_complete`.")
    assert t("en", "review_rejected_plain") == "Fix it, then call `task_complete` again."
    assert t("ru", "review_rejected_plain") == "Исправь и снова вызови `task_complete`."
    assert t("en", "ending_crew_failed", failed=16, id=7) == (
        "reopen the failed #16 to be done again (task_update #16 status=todo), cancel it if #7 does without it "
        "(status=cancelled), or fail #7.")
    assert t("en", "ending_crew_failed_creator", failed=16, id=7).endswith(", or cancel #7.")
    assert t("en", "ending_crew_finished_creator", id=7) == \
        "take #7 (task_claim), " + t("en", "ending_crew_finished", id=7)
    assert t("en", "lock_waiter_direction", id=15, title="pkce", parent=7) == "#15 pkce (direction of #7)"
    assert t("en", "n_direction_freed", id=15, title="pkce", by="Anna Smith") == \
        '#15 "pkce", a direction of your crew task, is free again: Anna Smith left the swarm.'
    # "A held direction", "Also assigned", "The next direction"
    assert t("en", "note_direction_held_busy", sub=16, id=8, name="Bob Brown", m=15, working=2, crew=3) == (
        " #16 waits: Bob Brown works on #15, another direction of crew task #8, and can take #16 only after handing "
        "#15 in; #8 has 2 of its 3 places taken now.")
    assert t("en", "note_direction_held_busy_you", sub=16, id=8, m=15) == (
        " #16 waits for you: you work on #15, another direction of crew task #8, and can take #16 only after handing "
        "#15 in (task_complete). If you take other work after that, unassign yourself from #16 first (task_update "
        "assignee=''), so it does not wait for you.")
    names = "Anna Smith, Bob Brown, Carl Diaz"
    assert t("en", "note_direction_held_full", sub=16, id=8, name="Dan Ito", working=3, crew=3, names=names) == (
        " #16 waits: all places in crew task #8 are taken (3 of 3: Anna Smith, Bob Brown, Carl Diaz), so Dan Ito can "
        "take #16 only when a place is free.")
    assert t("en", "note_direction_held_full_you", sub=16, id=8, working=3, crew=3, names="…") == (
        " #16 waits for you: all places in crew task #8 are taken (3 of 3: …); you can take #16 only when a place is "
        "free. If you take other work meanwhile that will take long, unassign yourself from #16 first (task_update "
        "assignee=''), so it does not wait for you.")
    assert t("en", "note_direction_held_tail_you", sub=16) == (
        " If another member can do #16 now, unassign yourself (task_update assignee='') and a free member is offered "
        "it.")
    assert t("en", "note_direction_held_tail", sub=16, id=8, name="Bob Brown") == (
        " If another member can do #16 now, unassign it (task_update assignee='') and a free member is offered it; if "
        "only Bob Brown can, keep it: a free place need not be filled. Add a direction only for a part of #8 that no "
        "direction covers yet and that can be done now (task_update directions).")
    assigned = 'Task #16 "title", a direction of crew task #8, was assigned to you by X.'
    assert t("en", "n_direction_held_busy", by="X", id=16, title="title", parent=8, m=15) == (
        assigned + " You work on #15, another direction of #8: keep working on it; you can take #16 only after "
        "handing #15 in (task_complete), and you are told when you can claim #16 (task_claim). If you take other "
        "work after handing #15 in, unassign yourself from #16 first (task_update assignee=''), so it does not wait "
        "for you.")
    assert t("en", "n_direction_held_full", by="X", id=16, title="title", parent=8, working=3, crew=3,
             names=names) == (
        assigned + " All places in #8 are taken now (3 of 3: Anna Smith, Bob Brown, Carl Diaz): you can take #16 "
        "only when a place is free, and you are told when you can claim #16 (task_claim). If you take other work "
        "meanwhile that will take long, unassign yourself from #16 first (task_update assignee=''), so it does not "
        "wait for you.")
    assert t("en", "note_direction_held_also", sub=16, id=8, m=15) == (
        " #16, also assigned to you, waits: you can take it only after handing #15 in (task_complete), since a member "
        "works on one direction of crew task #8 at a time. If you take other work after that, unassign yourself from "
        "#16 first (task_update assignee=''), so it does not wait for you.")
    assert t("en", "mark_direction_to_take", sub=16, title="title", id=8) == (
        ' #16 "title", your direction of crew task #8, can be taken now: claim it (task_claim) and do it, or unassign '
        "yourself if you will not (task_update assignee='').")


def test_russian_subtasks_done_needs_no_plural_forms():
    """`n_subtasks_done` and its counts put the number after a colon in Russian, so one subtask and six read right."""
    t = prompts.t
    one = t("ru", "n_subtasks_done", id=7, title="парсер", total=1, counts=t("ru", "count_done", n=1),
            ending=t("ru", "ending_plain"))
    assert one == "Все подзадачи #7 «парсер» завершены (всего: 1; выполнено: 1): собери их итоги и заверши её."
    six = t("ru", "n_subtasks_done", id=7, title="парсер", total=6,
            counts=", ".join([t("ru", "count_done", n=5), t("ru", "count_cancelled", n=1)]),
            ending=t("ru", "ending_crew_finished", id=7))
    assert six.startswith("Все подзадачи #7 «парсер» завершены (всего: 6; выполнено: 5, отменено: 1): проверь")
    for key in ("count_done", "count_cancelled", "count_failed", "crew_draft_more", "crew_directions_more",
                "n_crew_review_requested"):
        text = prompts.TEXTS["ru"][key]
        number = "{n}" if "{n}" in text else "{k}"
        assert f": {number}" in text, key


@pytest.mark.parametrize("lang", LANGS)
def test_system_prompt_teaches_crew_tasks(lang):
    """Rules 3, 6 and 8 of the system prompt (docs/SPEC.md §4.1 "Several reviews at once", §4.2 "System prompt");
    the planning card says the same in step 4 and counts a crew task with its own direction as one task in step 9."""
    lines = {int(m.group(1)): m.group(2) for m in re.finditer(r"^(\d+)\. (.*)$", SYSTEMS[lang], re.M)}
    snippets = {
        "en": {3: ('A crew task ("crew 2/3" on the board) is split into directions', "you own it",
                   "count as one task in progress", "(task_update crew, directions: the first direction is your own "
                   "part); crew = 0 makes it an ordinary task again", "A direction is never split into a crew"),
               6: ("Reviews are not tasks in progress: when several wait for you, settle them together, one "
                   "task_review each (independent ones in one response), each checked against its own goal.",),
               8: ("When a deliverable is too big for one member and its detailed split is unclear before the work "
                   "starts, create one crew task: crew = how many members may work on it at once, its owner "
                   "included; directions = its areas, one per item; the members who take them write the details.",)},
        "ru": {3: ("Групповая задача («crew 2/3» на доске) разделена на направления", "ты её владелец",
                   "считаются одной задачей в работе", "(task_update crew, directions: первое направление — твоя "
                   "часть); crew = 0 снова делает её обычной задачей", "Направление никогда не становится групповой "
                   "задачей"),
               6: ("Ревью — не задачи в работе", "по одному task_review на каждое"),
               8: ("создай одну групповую задачу: crew — сколько участников могут работать над ней одновременно, "
                   "включая её владельца; directions — её направления",)},
    }[lang]
    for number, parts in snippets.items():
        for part in parts:
            assert part in lines[number], (lang, number, part)
    # the rules end where they ended before: the crew text is added, nothing is moved
    assert lines[3].index("`task_claim`") < lines[3].index("crew 2/3")
    steps = {int(m.group(1)): m.group(2)
             for m in re.finditer(r"^(\d+)\. (.*)$", prompts.PLANNING_TASK[lang][2], re.M)}
    assert sorted(steps) == list(range(1, 10))
    step4, step9 = {"en": ("becomes one crew task: crew = how many members may work on it at once",
                           "A crew task you own and your own direction of it count as one task here"),
                    "ru": ("становится одной групповой задачей: crew — сколько участников могут работать над ней",
                           "Групповая задача, которой ты владеешь, и твоё собственное направление в ней считаются "
                           "здесь одной задачей")}[lang]
    assert step4 in steps[4] and step9 in steps[9]
    assert step4 in prompts.planning_task(lang, True)[2] and step9 in prompts.planning_task(lang, True)[2]
    # the plan's tasks are no subtasks of the card: a parent cannot be completed while a subtask is open
    no_parent = {"en": "Create them without parent_id: they are not parts of this task.",
                 "ru": "Создавай их без parent_id: они не части этой задачи."}[lang]
    assert no_parent in steps[4] and no_parent in prompts.planning_task(lang, True)[2]


# ------------------------------------------------------------------------- rendering & callers
def test_system_prompt_renders_all_fields():
    for lang in LANGS:
        text = prompts.system_prompt(lang, **SYSTEM_KW)
        for value in ("Ann Lee", "Swarm X", "Tester", "Checks things.", "Check {curly} things", "bash",
                      ".scratch/ann.lee/", "7", "95"):
            assert value in text, (lang, value)
        assert not re.search(r"\{[a-z_]+\}", text.replace("{curly}", ""))
        # the role's description follows the role line, before the member's own instructions
        first, second, third = text.splitlines()[:3]
        assert first.endswith("Tester.") and second == "Checks things."
        assert third == prompts.TEXTS[lang]["instructions"].format(text="Check {curly} things").strip()
        bare = prompts.system_prompt(lang, **{**SYSTEM_KW, "instructions": ""})
        assert prompts.TEXTS[lang]["instructions"].split("{")[0] not in bare
        # callers that pass neither a role description, a scratch folder nor the foreground cap (older callers,
        # tests) still render, with the default cap of [shell] foreground_max
        old = {k: v for k, v in SYSTEM_KW.items() if k not in ("role_description", "scratch", "foreground_max")}
        text = prompts.system_prompt(lang, **{**old, "instructions": ""})
        assert not re.search(r"\{[a-z_]+\}", text) and ".scratch/" in text
        assert str(ShellSettings.foreground_max) in text
        # no empty description line: the role line, one blank line, then the first section
        assert text.splitlines()[1:3] == ["", SYSTEMS[lang].splitlines()[2]]


def test_unknown_language_falls_back_to_english():
    # regression: system_prompt() raised KeyError for a language without texts while t() fell back
    assert prompts.system_prompt("de", **SYSTEM_KW) == prompts.system_prompt("en", **SYSTEM_KW)
    assert prompts.system_prompt(None, **SYSTEM_KW) == prompts.system_prompt("en", **SYSTEM_KW)
    assert prompts.t("de", "n_restart") == prompts.t("en", "n_restart")


# positional index of the prompt key in each notification / text call
_KEY_ARG = {"notify": 1, "notify_later": 2, "_notify_member": 2, "t": 1, "_announce": 0}
# placeholders a helper fills in itself: SessionSkills._announce computes `path` and `deps` per recipient
_IMPLIED = {"_announce": {"path", "deps"}}


def _string_values(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _string_values(node.body) + _string_values(node.orelse)
    return []


def _key_candidates(arg: ast.AST, func: ast.AST | None) -> list[str]:
    found = _string_values(arg)
    if not found and isinstance(arg, ast.Name) and func is not None:
        for node in ast.walk(func):  # key = "a" if x else "b"; notify(..., key, ...)
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == arg.id for t in node.targets):
                found += _string_values(node.value)
    return found


def _prompt_calls() -> list[tuple[str, str, set[str]]]:
    """(location, key, keyword names) for every call that renders a prompt text with a known key."""
    calls = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            name = node.func.attr
            if name not in _KEY_ARG or (name == "t" and not (isinstance(node.func.value, ast.Name)
                                                             and node.func.value.id == "prompts")):
                continue
            index = _KEY_ARG[name]
            if len(node.args) <= index or any(k.arg is None for k in node.keywords):
                continue  # pass-through helpers (**kw) are checked at their own call sites
            owner = next((f for f in funcs if f.lineno <= node.lineno <= (f.end_lineno or f.lineno)
                          and any(c is node for c in ast.walk(f))), None)
            kwargs = ({k.arg for k in node.keywords} - {"wake"}) | _IMPLIED.get(name, set())
            for key in _key_candidates(node.args[index], owner):
                calls.append((f"{path.relative_to(SRC)}:{node.lineno}", key, kwargs))
    return calls


def test_every_caller_passes_every_placeholder():
    calls = _prompt_calls()
    keys = {key for _, key, _ in calls}
    # the scan must really see the callers (guards against a silently broken AST walk)
    assert {"n_kickoff", "n_assigned", "n_review_approved", "n_review_rejected", "n_trigger", "nudge",
            "wake_header", "incoming_header", "compacted", "u_budget"} <= keys, keys
    assert len(calls) >= 30
    for where, key, kwargs in calls:
        assert key in prompts.TEXTS["en"], f"{where}: unknown prompt key {key!r}"
        for lang in LANGS:
            missing = placeholders(prompts.TEXTS[lang][key]) - kwargs
            assert not missing, f"{where}: {key} ({lang}) needs {missing}, the caller passes {sorted(kwargs)}"
            assert prompts.t(lang, key, **{k: 7.0 for k in kwargs})


# ---------------------------------------------------------------- guidance for self-organization
@pytest.mark.parametrize("lang", LANGS)
def test_texts_steer_agents_to_the_right_tools(lang):
    texts = prompts.TEXTS[lang]
    system = code_idents(SYSTEMS[lang])
    # the core protocol: claim before working, finish with a result, talk in task threads, end turns
    # with wait instead of polling, close the global goal, keep notes
    assert {"task_claim", "task_complete", "task_comment", "send_message", "wait", "goal_complete",
            "update_notes", "board", "task_get"} <= system
    expected = {
        "n_kickoff": {"wait"},          # non-planners wait instead of offering help / planning in parallel
        "n_ready": {"wait"},
        "n_hired": {"wait"},
        "nudge": {"task_complete", "waiting", "wait"},
        "n_stalled": {"waiting", "todo", "failed"},  # an idle holder of in_progress work must move or release it
        "n_all_done": {"goal_complete"},
        "n_goal_reopened": {"goal_complete"},
        "n_review_requested": {"task_review", "task_get"},
        "n_review_reminder": {"task_review"},  # an idle reviewer is reminded with the tool that settles it
        # a returned task: its ending (the plain one, or a crew task's) says to complete it again
        "review_rejected_plain": {"task_complete"},
        "review_rejected_crew": {"task_complete"},
        "n_assigned": {"task_claim"},
        "n_unblocked": {"task_claim"},
        "n_unblocked_removed": {"task_claim"},
        "n_mention": {"task_comment"},
    }
    for key, tools in expected.items():
        assert tools <= code_idents(texts[key]), (key, code_idents(texts[key]))
    assert texts["n_review_rejected"].endswith("\n{ending}")
    # the crew texts name their tools in parentheses, without backticks (docs/SPEC.md §4.2): the tool that settles
    # each one is there
    plain = {
        "ending_plain_creator": {"task_claim"},
        "ending_crew_failed": {"task_update"}, "ending_crew_failed_creator": {"task_update"},
        "ending_crew_cancelled": {"task_update", "task_complete"},
        "ending_crew_cancelled_creator": {"task_update", "task_claim", "task_complete"},
        "ending_crew_finished": {"task_complete"}, "ending_crew_finished_creator": {"task_claim", "task_complete"},
        "n_crew_review_requested": {"task_review"},
        "n_direction_failed": {"task_update"}, "n_direction_cancelled": {"task_update"},
        "n_parent_closed": {"task_update"}, "note_still_open": {"task_update"},
        "crew_owner_take": {"task_claim", "task_update", "task_complete"},
        "crew_owner_own": {"task_update", "task_complete"}, "crew_owner_all_taken": {"task_update", "task_complete"},
        "crew_owner_none": {"task_update", "task_complete"},
        "note_direction_card": {"task_update"}, "note_own_part": {"task_update"},
        "note_own_part_blocked": {"task_update", "task_claim"}, "mark_free_directions": {"task_claim", "task_update"},
        "mark_direction_to_take": {"task_claim", "task_update"},
    }
    for key, words in plain.items():
        for word in words:
            assert word in texts[key], (key, word)
    # a new member must not greet the hirer: a greeting is a message that wakes someone for nothing
    assert "send_message" not in code_idents(texts["n_hired"])
    # git hygiene: temporary files go to the member's own scratch folder, commits are made for the agents
    for snippet in ("`.scratch/`", "`{scratch}`", "`git add`", "`git commit`"):
        assert snippet in SYSTEMS[lang], snippet
    # independent review: the wake-up lists the reviews waiting, and reviews come before one's own work
    assert "task_review" in system and "{tasks}" in texts["my_reviews"]


# ------------------------------------------------------------------- end to end, in Russian
@pytest.fixture
def fast_scheduler(monkeypatch):
    monkeypatch.setattr(swarm_module, "SCHEDULER_SECONDS", 0.1)


async def _until(predicate, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached in time")


def _call(name: str, **arguments: Any) -> dict:
    return {"tool_calls": [{"name": name, "arguments": arguments}]}


async def test_russian_swarm_gets_russian_guidance_and_reaches_the_goal(tmp_path, fast_scheduler):
    raw = {
        "swarm": {"name": "Рой", "language": "ru", "start_agents": 3, "max_agents": 4, "max_cost_usd": 0},
        "agents": {"stagger_seconds": 0, "idle_nudge_seconds": 0.2, "max_steps_per_turn": 20},
        "mcp": {"library_dir": str(tmp_path / "library")},
        "models": {"default": {"provider": "scripted", "model": "scripted", "input_price": 0, "output_price": 0}},
    }
    systems: dict[str, set[str]] = {}
    seen: list[tuple[str, str]] = []
    stalled: set[int] = set()
    holder: dict[str, Swarm] = {}

    def policy(name: str, system: str, messages: list[dict]) -> Any:
        swarm = holder["swarm"]
        systems.setdefault(name, set()).add(system)
        if messages[-1]["role"] == "user":
            seen.append((name, messages[-1]["content"]))
        board = swarm.board
        for task in board.reviews_of(name):  # independent review: reviews come before one's own work
            return _call("task_review", task_id=task.id, approve=True, comment="Проверено")
        for task in board.open_tasks_of(name):
            if task.status != Status.IN_PROGRESS:
                continue
            if "planning" in task.tags:
                if len(board.list(include_closed=True)) == 1:
                    return {"tool_calls": [
                        {"name": "task_create", "arguments": {"title": "Парсер", "goal": "CSV читается"}},
                        {"name": "task_create", "arguments": {"title": "CLI", "goal": "команда работает"}},
                    ]}
                return _call("task_complete", task_id=task.id, result="План: #2, #3")
            if not stalled:  # a typical weak-model slip (once): announce the work instead of doing it
                stalled.add(task.id)
                return "Сейчас сделаю."
            return _call("task_complete", task_id=task.id, result="Готово")
        ready = board.ready_unassigned()
        if ready:
            return _call("task_claim", task_id=ready[0].id)
        tasks = board.list(include_closed=True)
        goal = swarm.goal()
        if goal and goal["status"] == "active" and len(tasks) == 3 and all(t.status == Status.DONE for t in tasks):
            return _call("goal_complete", summary="Всё сделано")
        return _call("wait")

    def factory(profile, agent: str) -> ScriptedLLM:
        return ScriptedLLM(lambda system, messages, tools: policy(agent, system, messages), profile)

    swarm = Swarm(config=Config.from_dict(tmp_path / "project", raw), llm_factory=factory)
    holder["swarm"] = swarm
    await swarm.start()
    try:
        swarm.set_goal(USER, "Конвертер CSV в JSON", "с тестами")
        await _until(lambda: (swarm.goal() or {}).get("status") == "done")
        planning = swarm.board.get(1)
        assert planning.title == prompts.PLANNING_TASK["ru"][0] and planning.status == Status.DONE
        # every task, the planning task included, was approved by a member who did not do it
        approved = {}
        for e in swarm.bus.recent(types=("task.updated",), limit=1000):
            if e["data"]["changes"].get("status") == [Status.REVIEW, Status.DONE]:
                approved[e["data"]["task"]["id"]] = (e["actor"], e["data"]["task"]["assignee"])
        assert sorted(approved) == [1, 2, 3]
        assert all(reviewer != executor and reviewer != USER for reviewer, executor in approved.values())
        assert not swarm.board.list(status=Status.REVIEW)
        # every agent ran on its own frozen Russian system prompt, in the catalog's generalist role
        for member in swarm.members():
            assert systems[member["name"]] == {member["system_prompt"]}
            assert member["system_prompt"].startswith(f"Ты — {member['name']}, участник роя ИИ-агентов «Рой». "
                                                      "Твоя роль: Универсал.")
        texts = [text for _, text in seen]
        wakes = [t for t in texts if t.startswith("===")]
        assert wakes and all(t.startswith(("=== Пробуждение", "=== Новые события")) for t in wakes)
        team = "; ".join(f"{m['name']} — Универсал" for m in swarm.members())
        kickoff = prompts.t("ru", "n_kickoff", by=USER, title="Конвертер CSV в JSON", description="с тестами",
                            team=team, task_id=1, task_title=planning.title)
        assert any(kickoff in t for t in wakes)
        assert any(f"Задача #1 «{planning.title}» ждёт твоего ревью" in t for t in wakes)  # n_review_requested
        nudges = [t for t in texts if t.startswith(prompts.TEXTS["ru"]["nudge"].split("{")[0])]
        assert nudges and all("`task_complete`" in t for t in nudges)
    finally:
        await swarm.stop()
        swarm.close()
