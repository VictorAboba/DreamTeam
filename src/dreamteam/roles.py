"""The role catalog (docs/SPEC.md §17.3): the built-in roles of ROLE_PRESETS (ru/en) overlaid with the [[roles]]
entries of the settings. Used by the config (validation of [[roles]]), the swarm (starting team, hire_agent, the
`agents` listing), the web APIs and the TUI. A leaf module: it imports nothing from the package."""

from __future__ import annotations

import difflib
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable

# Built-in roles of the role catalog: id -> (name, description) per language; the dict order is the catalog order.
# The description goes into the system prompt of a new member right after "Your role: <name>."; both languages say
# the same thing. prompts.DEFAULT_ROLE is the generalist's name.
ROLE_PRESETS: dict[str, dict[str, tuple[str, str]]] = {
    "en": {
        "generalist": ("Generalist", (
            "You take whichever task is most useful now: code, texts, analysis, research or checks. You produce "
            "what the task goal asks for, in the workspace, following the contracts and conventions the team has "
            "already fixed. Before you finish, you check the result yourself: run it, reread it and compare it "
            "with the task goal. Your work is reviewed and tested by other members, never by you. When a task "
            "needs expertise the team lacks, say so in its discussion instead of guessing."
        )),
        "developer": ("Developer", (
            "You turn tasks into working code: features, fixes and refactorings. You follow the interfaces, file "
            "layout and code style fixed in the team's contracts (CONTRACTS.md); if a contract has to change, "
            "you propose it in the discussion of the planning task and wait for agreement instead of deviating "
            "silently. Before `task_complete` you run your code (a quick run or a smoke check; throwaway scripts "
            "go to `.scratch/`) and write in the result the exact commands and what they showed. You leave no "
            "half-done code, debug output or stray files behind. The tests and the review of your code are done "
            "by other members; you fix the defects they report."
        )),
        "tester": ("Tester", (
            "You check that what others built really works, against the task goals and the contracts rather than "
            "the author's explanations. You write automated tests in the project's test folder (normal cases, "
            "edge cases, bad input, the documented errors and formats) and run them; one-off checks go to "
            "`.scratch/`. You never test code you wrote yourself. For each defect you give the exact command, "
            "the expected and the actual result: in the discussion of the task that produced the code while it "
            "is open, otherwise in a new fix task for its author with you as the reviewer. Your result lists "
            "what is covered, the command that runs the tests and how many passed and failed; fixing the product "
            "code is left to its author."
        )),
        "reviewer": ("Reviewer", (
            "You review tasks other members have finished: you are the second pair of eyes before work counts as "
            "done, and reviews waiting for you come before anything else. For each review you read the task goal "
            "and result, open the changed files, run what can be run and compare everything with the goal and "
            "the contracts. You approve only what you have checked yourself, and you never review your own work. "
            "When you return a task, you list concrete, checkable fixes (file, place, what is wrong, what is "
            "expected) and leave the fixing to the assignee. You also watch the whole: names, formats and "
            "interfaces must not drift apart between tasks."
        )),
        "analyst": ("Analyst", (
            "You turn vague goals into clear requirements: what exactly must be delivered, for whom, with which "
            "inputs, outputs, constraints and acceptance criteria. You write them to a file in the workspace "
            "(requirements, scenarios, examples of input and expected output) that tasks can refer to. Every "
            "criterion you write must be checkable; questions only the user can answer go to the user in one "
            "message, the rest you decide and record as assumptions. When a task asks for it, you analyse data "
            "and results with numbers and sources. Design and implementation are left to others."
        )),
        "architect": ("Architect", (
            "You design the structure before the team builds it: modules and their responsibilities, interfaces "
            "and signatures, data and file formats, errors and exit codes, the directory layout and the code "
            "style. You write these contracts once to CONTRACTS.md in the workspace, short and precise enough "
            "for members to work against them in parallel without asking. You check a design by walking the main "
            "scenarios through it and by making sure every task of the plan can point to the part of the "
            "contract it needs. When a contract has to change later, you update the file and tell the members "
            "whose tasks depend on it. Implementing the modules is left to developers."
        )),
        "writer": ("Technical writer", (
            "You write the documentation people actually read: README, usage guides, references of APIs and file "
            "formats, reports for the user. You write from the real result, not from plans: you run the commands "
            "and examples you document and show their actual output. Your texts agree with the contracts and "
            "with each other and are short enough to be read. When the docs depend on tests or other tasks, you "
            "start once those are done, and you report contradictions to the author of the task instead of "
            "papering over them. Changing the code is left to developers."
        )),
        "researcher": ("Researcher", (
            "You find out what the team does not know yet: how a library or an API works, which approach or tool "
            "fits best, what the facts and numbers are. You check claims against primary sources (official "
            "documentation, the code itself, a quick experiment in `.scratch/`) and name the sources. You "
            "produce a short written answer in the workspace: the conclusion first, then the evidence, the "
            "options with their trade-offs and a recommendation. You keep what you verified apart from what you "
            "assume. Building the solution is left to others."
        )),
    },
    "ru": {
        "generalist": ("Универсал", (
            "Ты берёшь ту задачу, которая сейчас полезнее всего: код, тексты, анализ, исследование или проверку. "
            "Ты делаешь то, чего требует цель задачи, в workspace, по контрактам и соглашениям, которые команда "
            "уже приняла. Перед завершением ты сам проверяешь результат: запускаешь, перечитываешь и сверяешь с "
            "целью задачи. Твою работу проверяют и тестируют другие участники, а не ты сам. Если задаче нужна "
            "экспертиза, которой в команде нет, скажи об этом в её обсуждении, а не угадывай."
        )),
        "developer": ("Разработчик", (
            "Ты превращаешь задачи в работающий код: новые функции, исправления, рефакторинг. Ты следуешь "
            "интерфейсам, структуре файлов и стилю кода, зафиксированным в контрактах команды (CONTRACTS.md); "
            "если контракт нужно изменить, предложи изменение в обсуждении задачи планирования и дождись "
            "согласия, а не отступай от него молча. Перед `task_complete` запусти свой код (быстрый прогон или "
            "smoke-проверка; одноразовые скрипты — в `.scratch/`) и укажи в итоге точные команды и что они "
            "показали. Не оставляй недоделанный код, отладочный вывод и лишние файлы. Тесты и ревью твоего кода "
            "делают другие участники; ты исправляешь найденные ими дефекты."
        )),
        "tester": ("Тестировщик", (
            "Ты проверяешь, что сделанное другими действительно работает, — по целям задач и контрактам, а не по "
            "объяснениям автора. Ты пишешь автотесты в папке тестов проекта (обычные и граничные случаи, "
            "ошибочный ввод, описанные ошибки и форматы) и запускаешь их; разовые проверки клади в `.scratch/`. "
            "Код, который ты написал сам, ты не тестируешь. Для каждого дефекта приводи точную команду, "
            "ожидаемый и фактический результат: в обсуждении задачи, в которой сделан этот код, пока она "
            "открыта, иначе — в новой задаче на исправление для её автора, где ревьюер — ты. В итоге перечисли, "
            "что покрыто, команду запуска тестов и сколько тестов прошло и упало; исправлять код продукта оставь "
            "его автору."
        )),
        "reviewer": ("Ревьюер", (
            "Ты проверяешь задачи, которые завершили другие участники: ты — второй взгляд, прежде чем работа "
            "будет считаться сделанной, и ждущие тебя ревью важнее любой другой работы. Для каждого ревью "
            "прочитай цель и итог задачи, открой изменённые файлы, запусти то, что можно запустить, и сверь всё "
            "с целью и контрактами. Принимай только то, что проверил сам, и никогда не проверяй собственную "
            "работу. Возвращая задачу, перечисли конкретные проверяемые исправления (файл, место, что не так, "
            "что ожидается), а сами исправления оставь исполнителю. Следи и за целым: имена, форматы и "
            "интерфейсы не должны расходиться между задачами."
        )),
        "analyst": ("Аналитик", (
            "Ты превращаешь расплывчатые цели в ясные требования: что именно нужно получить, для кого, с какими "
            "входными и выходными данными, ограничениями и критериями приёмки. Ты записываешь их в файл в "
            "workspace (требования, сценарии, примеры входных данных и ожидаемого результата), на который могут "
            "ссылаться задачи. Каждый критерий должен быть проверяемым; вопросы, на которые может ответить "
            "только пользователь, задай ему одним сообщением, остальное реши сам и запиши как допущения. Если "
            "задача этого требует, ты анализируешь данные и результаты — с цифрами и источниками. Проектирование "
            "и реализацию оставь другим."
        )),
        "architect": ("Архитектор", (
            "Ты проектируешь структуру до того, как команда начнёт строить: модули и их ответственность, "
            "интерфейсы и сигнатуры, форматы данных и файлов, ошибки и коды выхода, структуру каталогов и стиль "
            "кода. Ты один раз записываешь эти контракты в CONTRACTS.md в workspace — коротко и настолько точно, "
            "чтобы участники могли параллельно работать по ним, не переспрашивая. Проверяй проект, проводя через "
            "него основные сценарии и убеждаясь, что каждая задача плана может сослаться на нужную ей часть "
            "контракта. Если контракт позже приходится менять, обнови файл и сообщи участникам, чьи задачи от "
            "него зависят. Реализацию модулей оставь разработчикам."
        )),
        "writer": ("Технический писатель", (
            "Ты пишешь документацию, которую действительно читают: README, руководства, справочники по API и "
            "форматам файлов, отчёты для пользователя. Ты пишешь по реальному результату, а не по планам: "
            "запускаешь команды и примеры, которые описываешь, и показываешь их фактический вывод. Твои тексты "
            "согласованы с контрактами и между собой и достаточно коротки, чтобы их прочли. Если документация "
            "зависит от тестов или других задач, берись за неё, когда они завершены, а найденные противоречия "
            "сообщай автору задачи, а не замазывай их. Менять код оставь разработчикам."
        )),
        "researcher": ("Исследователь", (
            "Ты выясняешь то, чего команда ещё не знает: как устроены библиотека или API, какой подход или "
            "инструмент подходит лучше, каковы факты и цифры. Ты проверяешь утверждения по первоисточникам "
            "(официальная документация, сам код, быстрый эксперимент в `.scratch/`) и называешь источники. "
            "Результат — короткий письменный ответ в workspace: сначала вывод, затем доказательства, варианты с "
            "их плюсами и минусами и рекомендация. Проверенное ты отделяешь от допущений. Строить решение оставь "
            "другим."
        )),
    },
}

PRESET_IDS: tuple[str, ...] = tuple(ROLE_PRESETS["en"])
DEFAULT_ROLE_ID = "generalist"
# a role name (at most), a description or instructions, [[roles]] entries, agents per /team item, agents per team
NAME_LIMIT, TEXT_LIMIT, MAX_ROLES, TEAM_ITEM_MAX, TEAM_MAX = 60, 4000, 100, 50, 200
MAX_ROLE_SKILLS = 20  # skill names one role lists (docs/SPEC.md §19)

# casefolded id or name (either language) of a preset -> its id
_PRESET_OF = {key.casefold(): rid for rid in PRESET_IDS
              for key in (rid, ROLE_PRESETS["en"][rid][0], ROLE_PRESETS["ru"][rid][0])}
_SENTENCE_END = re.compile(r"[.!?…](?=\s|$)")


@dataclass(frozen=True)
class Role:
    """A role of the catalog in one language."""

    key: str                    # preset id, or the casefolded name of an own role
    name: str                   # in the requested language (own roles: as written)
    description: str
    instructions: str = ""
    model: str | None = None
    builtin: bool = False
    customized: bool = False    # a built-in role with a [[roles]] entry
    hidden: bool = False
    skills: tuple[str, ...] = ()  # skill names its agents get attached at hire (built-in presets list none)

    def to_dict(self) -> dict:
        return asdict(self)


def _language(language: Any) -> str:
    return "ru" if language == "ru" else "en"


def _field(entry: Any, key: str) -> Any:
    """A field of a [[roles]] entry: a config.RoleSettings or a dict of the same keys."""
    return entry.get(key) if isinstance(entry, dict) else getattr(entry, key, None)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _skills(entry: Any) -> tuple[str, ...]:
    value = _field(entry, "skills")
    return tuple(s for s in value if isinstance(s, str)) if isinstance(value, (list, tuple)) else ()


def normalize(name: Any) -> str:
    """A role name with its whitespace collapsed ("" for None)."""
    return " ".join(str(name or "").split())


def preset_id(name: Any) -> str | None:
    """The id of the built-in role named `name` (its id or its name in either language, any case), else None."""
    return _PRESET_OF.get(normalize(name).casefold())


def role_key(name: Any) -> str:
    """The catalog key of a role name: a preset id, else the casefolded normalized name."""
    return preset_id(name) or normalize(name).casefold()


def catalog(entries: Iterable[Any] | None, language: str, *, include_hidden: bool = False) -> list[Role]:
    """The catalog in `language`: the presets in PRESET_IDS order with their entries applied (a non-empty
    description replaces the built-in one in both languages; instructions, model and hidden come from the entry),
    then the own roles in entry order. Hidden roles only with `include_hidden`."""
    language = _language(language)
    by_key: dict[str, Any] = {}
    own: list[tuple[str, Any]] = []
    for entry in entries or ():
        name = normalize(_field(entry, "name"))
        key = role_key(name)
        if not key or key in by_key:      # settings validation forbids both; the first entry wins
            continue
        by_key[key] = entry
        if key not in ROLE_PRESETS["en"]:
            own.append((key, entry))
    out: list[Role] = []
    for rid in PRESET_IDS:
        name, description = ROLE_PRESETS[language][rid]
        entry = by_key.get(rid)
        if entry is None:
            out.append(Role(rid, name, description, builtin=True))
            continue
        out.append(Role(rid, name, _text(_field(entry, "description")) or description,
                        instructions=_text(_field(entry, "instructions")),
                        model=_text(_field(entry, "model")) or None, builtin=True, customized=True,
                        hidden=bool(_field(entry, "hidden")), skills=_skills(entry)))
    for key, entry in own:
        out.append(Role(key, normalize(_field(entry, "name")), _text(_field(entry, "description")),
                        instructions=_text(_field(entry, "instructions")),
                        model=_text(_field(entry, "model")) or None, hidden=bool(_field(entry, "hidden")),
                        skills=_skills(entry)))
    return out if include_hidden else [role for role in out if not role.hidden]


def find_role(entries: Iterable[Any] | None, name: Any, language: str) -> Role | None:
    """The catalog role named `name` (a preset id, a preset name in either language or an own role's name; any case
    and spacing), hidden roles included (hidden = not offered). None: a new role."""
    key = role_key(name)
    if not key:
        return None
    return next((role for role in catalog(entries, language, include_hidden=True) if role.key == key), None)


def close_roles(entries: Iterable[Any] | None, name: Any, language: str, n: int = 3) -> list[str]:
    """Names (in `language`) of up to `n` visible catalog roles that look like `name` (a typo, the other language's
    name or a preset id)."""
    shown: dict[str, str] = {}
    for role in catalog(entries, language):
        spellings = [role.name]
        if role.builtin:
            spellings += [role.key, *(ROLE_PRESETS[lang][role.key][0] for lang in ("en", "ru"))]
        for spelling in spellings:
            shown.setdefault(spelling.casefold(), role.name)
    found: list[str] = []
    for match in difflib.get_close_matches(normalize(name).casefold(), list(shown), n=len(shown), cutoff=0.6):
        if shown[match] not in found:
            found.append(shown[match])
    return found[:n]


def summary(text: Any, limit: int = 200) -> str:
    """The first sentence of a description (it ends with . ! ? or …), else the text cut at a word with "…"; at most
    `limit` characters."""
    text = normalize(text)
    end = _SENTENCE_END.search(text)
    if end and end.end() <= limit:
        return text[:end.end()]
    if len(text) <= limit:
        return text
    space = text.rfind(" ", 0, limit)
    cut = space if space >= limit // 4 else limit - 1
    return text[:cut].rstrip(" ,;:-–—") + "…"


def starting_team(starting_roles: Iterable[Any] | None, start_agents: int) -> list[str]:
    """The roles of the members a session starts with: member i gets starting_roles[i]; the members beyond the list,
    and blank entries, are generalists; roles beyond start_agents are ignored."""
    n = max(0, int(start_agents or 0))
    team = [normalize(role) or DEFAULT_ROLE_ID for role in list(starting_roles or [])[:n]]
    return team + [DEFAULT_ROLE_ID] * (n - len(team))


def _team_role(role: Any) -> str:
    """A role of a starting team as stored and shown in rows: a preset by its id, an own role as written."""
    return preset_id(role) or normalize(role) or DEFAULT_ROLE_ID


def team_rows(starting_roles: Iterable[Any] | None, start_agents: int) -> list[tuple[str, int]]:
    """The starting team as rows [(role, count)] in member order, consecutive equal roles grouped (the same role may
    appear in two rows). A row's role is a preset id or an own role's name as written (role_key() of it is the
    catalog key)."""
    rows: list[tuple[str, int]] = []
    for role in map(_team_role, starting_team(starting_roles, start_agents)):
        if rows and rows[-1][0] == role:
            rows[-1] = (role, rows[-1][1] + 1)
        else:
            rows.append((role, 1))
    return rows


def team_value(rows: Iterable[tuple[Any, Any]]) -> tuple[list[str], int]:
    """(starting_roles, start_agents) of rows: the expanded list with the trailing generalists left implicit, so an
    untouched default never changes the settings ([("generalist", 3)] -> ([], 3))."""
    roles: list[str] = []
    for role, count in rows:
        roles += [_team_role(role)] * max(0, int(count or 0))
    total = len(roles)
    while roles and roles[-1] == DEFAULT_ROLE_ID:
        roles.pop()
    return roles, total


def parse_team(text: Any) -> list[str]:
    """The roles of a team typed as "architect, developer*2, tester" (`--team`, `/team`): items split on ",", blank
    items skipped, ROLE*N repeats a role (N from 1 to TEAM_ITEM_MAX). Names are kept as typed (normalized): presets
    are resolved when the members are created, unknown names are new roles. ValueError with the reason."""
    team: list[str] = []
    for raw in str(text or "").split(","):
        item = normalize(raw)
        if not item:
            continue
        name, count = item, 1
        if "*" in item:
            name, _, number = item.rpartition("*")
            name, number = normalize(name), number.strip()
            if not (number.isascii() and number.isdigit()) or not 1 <= int(number) <= TEAM_ITEM_MAX or "*" in name:
                raise ValueError(f"'{item}': the count must be a whole number from 1 to {TEAM_ITEM_MAX}")
            if not name:
                raise ValueError(f"'{item}': the role name is empty")
            count = int(number)
        if len(name) > NAME_LIMIT:
            raise ValueError(f"'{item}': a role name cannot be longer than {NAME_LIMIT} characters")
        team += [name] * count
        if len(team) > TEAM_MAX:
            raise ValueError(f"A team has at most {TEAM_MAX} agents")
    if not team:
        raise ValueError("The team is empty")
    return team


def presets_view() -> list[dict]:
    """The built-in roles in both languages, in catalog order (GET /api/settings "role_presets")."""
    return [{"id": rid,
             "name": {lang: ROLE_PRESETS[lang][rid][0] for lang in ("ru", "en")},
             "description": {lang: ROLE_PRESETS[lang][rid][1] for lang in ("ru", "en")}}
            for rid in PRESET_IDS]


def catalog_view(entries: Iterable[Any] | None) -> list[dict]:
    """The whole catalog, hidden roles included, in both languages (GET /api/settings "role_catalog"); own roles carry
    the same text under both languages."""
    entries = list(entries or ())
    ru = catalog(entries, "ru", include_hidden=True)
    en = catalog(entries, "en", include_hidden=True)
    return [{"key": r.key, "builtin": r.builtin, "customized": r.customized, "hidden": r.hidden,
             "name": {"ru": r.name, "en": e.name}, "description": {"ru": r.description, "en": e.description},
             "instructions": r.instructions, "model": r.model, "skills": list(r.skills)}
            for r, e in zip(ru, en)]
