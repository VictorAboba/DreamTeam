"""Tools for the swarm's skills (docs/SPEC.md §19). The user's (`skills`, `skill_attach`, `skill_detach`,
`skill_approve`, `skill_reinstall`; audience `user`, part of no frozen LLM tool set) reach the web /api/act, the TUI, the console and the
hub MCP. The members' `skill_propose` is an LLM tool of frozen revision 3 (tools/frozen.py)."""

from __future__ import annotations

import asyncio
from typing import Any

from .. import skills as skill_lib
from ..models import ToolError
from ..swarm_skills import files_text
from ..workspace import format_size
from . import ToolContext, tool


def _short(text: str, limit: int = 200) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _attached_lines(swarm, digests: dict[str, str | None]) -> list[str]:
    lines = []
    for s in swarm.skills.list():
        status = f", library: {s.library}" if s.library != "none" else ""
        owner = f" proposed by {s.owner}" if s.owner else f" by {s.attached_by}"
        role = f", brought by role {s.attached_via[5:]}" if s.attached_via.startswith("role:") else ""
        manual = ", only when the user asks for it" if s.manual else ""
        lines.append(f"- {s.name} [{s.frm}{status}{role}{manual}]{owner}: {_short(s.description)} "
                     f"({files_text(s.files)}, {format_size(s.size)}) → {swarm.skills.shown_path(s.name)[0]}")
        if s.frm != "agent":
            now = digests.get(s.name)
            if now is None:
                lines.append("  source gone: it is no longer where it was attached from")
            elif now != s.digest:
                lines.append(f"  source changed: skill_attach name=\"{s.frm}:{s.name}\" with replace updates it")
        packages = swarm.skills.deps_summary(s.name)
        if packages:
            lines.append(f"  {packages}")
    return lines


def _available_lines(swarm) -> list[str]:
    config = swarm.config
    attached = set(swarm.skills.names())
    rows: list[dict] = []
    rows += [{**e, "origin": "library"} for e in skill_lib.Library.from_config(config).list()]
    rows += skill_lib.host_skills(config)
    rows += skill_lib.claude_skills(config, swarm.workspace.root)
    lines = []
    for row in rows:
        if row["name"] in attached:
            continue
        origin = row["origin"]
        notes = []
        if row.get("changed_outside"):
            notes.append("changed outside DreamTeam")
        if row.get("agent_writable"):
            notes.append("from the workspace: agents can write here")
        if row.get("manual"):
            notes.append("only when the user asks for it")
        ref = row["name"] if origin == "library" else f"{origin}:{row['name']}"
        tail = f" (attach {ref})"
        if row.get("error"):
            lines.append(f"- {row['name']} [{origin}]: not attachable: {_short(row['error'])}")
            continue
        extra = f", {', '.join(notes)}" if notes else ""
        lines.append(f"- {row['name']} [{origin}{extra}]: {_short(row.get('description') or '')}{tail}")
    return lines


@tool(
    "skills",
    en="List the skills attached to the swarm (folders with a SKILL.md that the members read before work they fit): "
       "name, source, who attached it, description and path. With available=true, also the skills of the skills "
       "library and of Claude Code that are not attached.",
    ru="Показать скиллы, подключённые к рою (папки с SKILL.md, которые участники читают перед подходящей работой): "
       "имя, источник, кто подключил, описание и путь. С available=true — также не подключённые скиллы библиотеки "
       "скиллов и Claude Code.",
    properties={"available": {"type": "boolean",
                              "description": "Also list the library's and Claude Code's skills that are not attached"}},
    audience=("user",),
)
async def list_skills(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    attached = swarm.skills.list()
    digests = await asyncio.to_thread(lambda: {s.name: swarm.skills.source_digest(s) for s in attached})
    limit = swarm.config.skills.max_attached
    if attached:
        lines = [f"Skills attached to the swarm ({len(attached)} of max {limit}):", *_attached_lines(swarm, digests)]
    else:
        lines = ["No skills are attached to the swarm."]
    if args.get("available"):
        available = await asyncio.to_thread(_available_lines, swarm)
        lines += ["Not attached (skill_attach name=...):", *available] if available else \
            [f"Nothing else to attach: the skills library ({swarm.config.skills_library_dir}) and Claude Code's "
             "skills folders hold no other skill."]
    return "\n".join(lines)


def _result(swarm, skill, status: str) -> str:
    if status == "unchanged":
        return f"Skill \"{skill.name}\" is already attached (unchanged)."
    packages = swarm.skills.deps_result(skill.name)
    if status == "updated":
        return (f"Skill \"{skill.name}\" updated from its source ({files_text(skill.files)}); members are "
                f"told.{packages}")
    return f"Skill \"{skill.name}\" attached ({files_text(skill.files)}); members are told.{packages}"


@tool(
    "skill_attach",
    en="Attach a skill to the swarm: every member sees it and is told. Either name: a skill of the skills library or "
       "of Claude Code (library:NAME or claude-user:NAME picks the source; claude-project:NAME is the workspace's "
       ".claude/skills, which agents can write), or source: a folder or .zip path on the hub's machine, or a Git URL "
       "(a subfolder: URL#ref:path), imported into the skills library first; pick names the skills to take from a "
       "source that holds several. With replace=true a skill of the same name that has other content is replaced "
       "(the attached one, and the library's when importing).",
    ru="Подключить скилл к рою: его видят все участники, им об этом сообщат. Либо name — скилл из библиотеки скиллов "
       "или Claude Code (library:NAME или claude-user:NAME выбирает источник; claude-project:NAME — .claude/skills "
       "папки workspace, куда могут писать агенты), либо source — путь к папке или .zip на машине хаба или Git-URL "
       "(подпапка: URL#ref:path), который сначала импортируется в библиотеку скиллов; pick называет скиллы, которые "
       "взять из источника с несколькими скиллами. С replace=true заменяется скилл с тем же именем и другим "
       "содержимым (подключённый, а при импорте и библиотечный).",
    properties={
        "name": {"type": "string",
                 "description": "A skill reference: NAME, library:NAME, claude-user:NAME or claude-project:NAME"},
        "source": {"type": "string", "description": "A folder or .zip path on the hub's machine, or a Git URL"},
        "pick": {"type": "array", "items": {"type": "string"},
                 "description": "The skills to take from a source that holds several"},
        "replace": {"type": "boolean", "description": "Replace a skill of the same name that has other content"},
    },
    audience=("user",),
)
async def skill_attach(ctx: ToolContext, args: dict[str, Any]) -> str:
    name, source = (args.get("name") or "").strip(), (args.get("source") or "").strip()
    if bool(name) == bool(source):
        raise ToolError("Give exactly one of name (a skill of the library or of Claude Code) and source (a folder, "
                        "a .zip path or a Git URL).")
    replace = bool(args.get("replace"))
    if name:
        skill, status = await ctx.swarm.skills.attach(ctx.actor, name, replace=replace, via=ctx.via)
        return _result(ctx.swarm, skill, status)
    report = await ctx.swarm.skills.attach_source(ctx.actor, source, pick=args.get("pick") or None,
                                                  replace=replace, via=ctx.via)
    if report["need_pick"]:
        listed = "; ".join(f"{c['name']}" + (f" ({_short(c['description'], 100)})" if c.get("description") else "")
                           + (f" [{_short(c['error'], 100)}]" if c.get("error") else "")
                           for c in report["candidates"])
        return (f"{source} holds several skills: {listed}. Call skill_attach again with pick, the names to "
                "attach.")
    lines = []
    for item in report["attached"]:
        skill = ctx.swarm.skills.get(item["name"])
        lines.append(_result(ctx.swarm, skill, item["status"]) if skill is not None else f"Skill \"{item['name']}\" attached.")
    for item in report.get("failed", ()):
        lines.append(f"Skill \"{item['name']}\" was imported into the skills library but not attached: "
                     f"{item['error']}")
    renamed = [f"\"{e['name']}\" (from \"{e['renamed_from']}\")" for e in report["imported"] if e.get("renamed_from")]
    if renamed:
        lines.append("Named " + ", ".join(renamed) + ".")
    if report["skipped"]:
        lines.append("Left out: " + "; ".join(f"{s['path']} ({s['error']})" for s in report["skipped"][:20]) + ".")
    if report.get("failed") and not report["attached"]:
        raise ToolError("\n".join(lines))
    return "\n".join(lines) or "Nothing was attached."


@tool(
    "skill_detach",
    en="Detach a skill from the swarm: its copy is removed and every member is told not to use it any more. The "
       "skills library keeps its own copy.",
    ru="Отключить скилл от роя: его копия удаляется, а всем участникам сообщают, что пользоваться им больше нельзя. "
       "В библиотеке скиллов остаётся её собственная копия.",
    properties={"name": {"type": "string", "description": "The attached skill's name"}},
    required=["name"],
    audience=("user",),
)
async def skill_detach(ctx: ToolContext, args: dict[str, Any]) -> str:
    skill = await ctx.swarm.skills.detach(ctx.actor, args["name"].strip(), via=ctx.via)
    return f"Skill \"{skill.name}\" detached; members are told."


# The members' tool (frozen tool set revision 3, tools/frozen.py): its schema is part of every new agent's prefix.
@tool(
    "skill_propose",
    en="Propose a skill you made: a folder with a SKILL.md, which starts with front matter between two '---' lines "
       "(name: lowercase letters, digits and hyphens, up to 64 characters; description: what it does and when to use "
       "it, up to 1024 characters), followed by the instructions, plus any scripts or files it uses. Write the folder "
       "in the workspace (your scratch folder is best), then call this with its path. Use it for a procedure that "
       "worked and will be needed again. It is checked and attached to the swarm at once, so every member sees it; "
       "the user decides whether it is saved to the skills library for future runs. Proposing the same name again "
       "replaces your earlier version.",
    ru="Предложить сделанный тобой скилл: папку с SKILL.md, который начинается с front matter между двумя строками "
       "'---' (name: строчные латинские буквы, цифры и дефисы, до 64 символов; description: что он делает и когда им "
       "пользоваться, до 1024 символов), за которым идут инструкции, и с нужными ему скриптами и файлами. Запиши "
       "папку в workspace (лучше в свою черновую папку) и вызови этот инструмент с её путём. Подходит для приёма, "
       "который сработал и ещё понадобится. Скилл проверяется и сразу подключается к рою, его видят все участники; "
       "сохранить ли его в библиотеку скиллов для будущих запусков, решает пользователь. Повторное предложение с тем "
       "же именем заменяет твою прежнюю версию.",
    properties={"path": {"type": "string",
                         "description": "The skill's folder (or its SKILL.md), relative to the workspace root"}},
    required=["path"],
    audience=("llm", "external"),
)
async def skill_propose(ctx: ToolContext, args: dict[str, Any]) -> str:
    return await ctx.swarm.skills.propose(ctx.actor, args["path"])


@tool(
    "skill_approve",
    en="Decide on a skill a member proposed (library status pending in skills): approve=true saves it to the skills "
       "library for future runs (replace=true overwrites a library skill of the same name that has other content); "
       "approve=false does not save it, and it stays attached to this swarm (detach it to remove it). comment is "
       "shown to the member.",
    ru="Решить судьбу скилла, предложенного участником (статус библиотеки pending в skills): approve=true сохраняет "
       "его в библиотеку скиллов для будущих запусков (replace=true заменяет библиотечный скилл с тем же именем и "
       "другим содержимым); approve=false не сохраняет, и он остаётся подключённым к этому рою (чтобы убрать его, "
       "отключите его). comment показывается участнику.",
    properties={
        "name": {"type": "string", "description": "The proposed skill's name"},
        "approve": {"type": "boolean", "description": "true: save it to the skills library; false: don't"},
        "comment": {"type": "string", "description": "Shown to the member (e.g. why it is not saved)"},
        "replace": {"type": "boolean",
                    "description": "Overwrite a library skill of the same name that has other content"},
    },
    required=["name", "approve"],
    audience=("user",),
)
async def skill_approve(ctx: ToolContext, args: dict[str, Any]) -> str:
    return await ctx.swarm.skills.decide(ctx.actor, args["name"].strip(), args["approve"],
                                         comment=args.get("comment") or "", replace=bool(args.get("replace")),
                                         via=ctx.via)


@tool(
    "skill_reinstall",
    en="Install the packages of an attached skill again (its requirements.txt, pyproject.toml or package.json): "
       "after a failure, or when its environment seems damaged. ecosystem picks python or node (default: both). "
       "An environment that other running sessions use is not reinstalled.",
    ru="Заново установить пакеты подключённого скилла (из его requirements.txt, pyproject.toml или package.json): "
       "после ошибки или если его окружение, похоже, повреждено. ecosystem выбирает python или node (по умолчанию "
       "оба). Окружение, которым пользуются другие работающие сессии, не переустанавливается.",
    properties={"name": {"type": "string", "description": "The attached skill's name"},
                "ecosystem": {"type": "string", "enum": ["python", "node"],
                              "description": "Only these packages (default: both)"}},
    required=["name"],
    audience=("user",),
)
async def skill_reinstall(ctx: ToolContext, args: dict[str, Any]) -> str:
    return await ctx.swarm.skills.reinstall(ctx.actor, args["name"].strip(), args.get("ecosystem") or None)
