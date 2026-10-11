"""Shared workspace files and the console."""

from __future__ import annotations

from typing import Any

from ..shell import scrubbed_env
from ..workspace import agent_git_env, scratch_rel
from . import ToolContext, tool

_PATH = {"type": "string", "description": "Path relative to the workspace root, e.g. src/app.py"}


@tool(
    "read_file",
    en="Read a text file from the shared workspace. Large files are returned in chunks: use offset "
       "(first line, 1-based) and limit (number of lines, default 400).",
    ru="Прочитать текстовый файл из общего workspace. Большие файлы читаются частями: offset (первая строка, "
       "с 1) и limit (число строк, по умолчанию 400).",
    properties={"path": _PATH, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
    required=["path"],
)
async def read_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    return ctx.swarm.workspace.read(args["path"], offset=args.get("offset") or 1, limit=args.get("limit") or 400)


@tool(
    "write_file",
    en="Create or overwrite a whole file in the shared workspace (parent folders are created). The change is "
       "committed to git under your name. Fails if another member locked the file. For small changes use edit_file.",
    ru="Создать или целиком перезаписать файл в общем workspace (папки создаются автоматически). Изменение "
       "коммитится в git от твоего имени. Не сработает, если файл заблокирован другим участником. Для небольших "
       "правок используй edit_file.",
    properties={"path": _PATH, "content": {"type": "string", "description": "The full new file content"}},
    required=["path", "content"],
    audience=("llm", "external"),
    allow_empty=("content",),
)
async def write_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    return await ctx.swarm.workspace.write(ctx.actor, args["path"], args["content"])


@tool(
    "edit_file",
    en="Replace exact text in a workspace file. old_text must match exactly once (copy it from read_file) "
       "unless replace_all=true; new_text \"\" deletes it.",
    ru="Заменить точный фрагмент текста в файле workspace. old_text должен встречаться ровно один раз "
       "(скопируй его из read_file), если не указан replace_all=true; new_text \"\" удаляет фрагмент.",
    properties={
        "path": _PATH,
        "old_text": {"type": "string"},
        "new_text": {"type": "string"},
        "replace_all": {"type": "boolean"},
    },
    required=["path", "old_text", "new_text"],
    audience=("llm", "external"),
    allow_empty=("new_text",),
)
async def edit_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    return await ctx.swarm.workspace.edit(
        ctx.actor, args["path"], args["old_text"], args["new_text"], bool(args.get("replace_all"))
    )


@tool(
    "list_files",
    en="List files and folders of the shared workspace as a tree (path defaults to the root, depth 1-6, default 2).",
    ru="Показать файлы и папки общего workspace деревом (по умолчанию корень; depth от 1 до 6, по умолчанию 2).",
    properties={"path": _PATH, "depth": {"type": "integer"}},
)
async def list_files(ctx: ToolContext, args: dict[str, Any]) -> str:
    depth = max(1, min(args.get("depth") or 2, 6))
    return ctx.swarm.workspace.list(args.get("path") or ".", depth=depth)


@tool(
    "lock_file",
    en="Lock a shared file (or folder) so that only you can write it (for minutes, default from config), or "
       "unlock it with unlock=true. Lock shared files before long edits and unlock them afterwards.",
    ru="Заблокировать общий файл (или папку), чтобы писать в него мог только ты (на minutes минут, по "
       "умолчанию из настроек), или разблокировать через unlock=true. Блокируй общие файлы перед долгой "
       "правкой и разблокируй после.",
    properties={"path": _PATH, "minutes": {"type": "number"}, "unlock": {"type": "boolean"}},
    required=["path"],
    audience=("llm", "external"),
)
async def lock_file(ctx: ToolContext, args: dict[str, Any]) -> str:
    ws = ctx.swarm.workspace
    if args.get("unlock"):
        return ws.unlock(ctx.actor, args["path"])
    return ws.lock(ctx.actor, args["path"], args.get("minutes"))


@tool(
    "shell",
    en="Run a console command in the workspace directory and get its exit code and output (the result names "
       "the shell). Use it to run code, tests, package managers, git, etc. There is no input (stdin is closed). "
       "A command still running when its timeout expires is not killed: it continues as a background job, you get "
       "its output so far and you are notified when it ends. Start servers and other long commands with "
       "background=true: the call returns after 2 seconds with the job's id; `jobs` shows a job's output and "
       "`job_stop` stops it. Secrets from the environment are not available.",
    ru="Выполнить команду в консоли в папке workspace и получить код возврата и вывод (в результате указана "
       "оболочка). Подходит для запуска кода, тестов, пакетных менеджеров, git и т. п. Ввода нет (stdin "
       "закрыт). Команда, которая ещё работает, когда истёк её timeout, не убивается: она продолжает работать "
       "как фоновый процесс, ты получаешь её вывод на этот момент, а когда она завершится, придёт уведомление. "
       "Серверы и другие долгие команды запускай с background=true: вызов вернётся через 2 секунды с "
       "идентификатором процесса (например, j3); `jobs` покажет вывод процесса, а `job_stop` остановит его. "
       "Секреты из окружения недоступны.",
    properties={
        "command": {"type": "string"},
        "timeout": {"type": "integer", "description": "Seconds to wait for the command (default from config); a "
                                                      "command still running then continues in the background"},
        "background": {"type": "boolean",
                       "description": "Start the command as a background job and return after 2 seconds"},
    },
    required=["command"],
    audience=("llm", "external"),
)
async def shell(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    cfg = swarm.config.shell
    command = args["command"]
    # `jobs`, `jobs j3` or `kill j3` as the whole command line is answered by the hub (how agents whose tool set
    # has no `jobs` tool list and stop their jobs): nothing is spawned and nothing is committed
    answer = await swarm.jobs.console(ctx.actor, command, max_chars=ctx.output_cap)
    if answer is not None:
        return answer
    timeout = min(max(args.get("timeout") or cfg.timeout, 1), cfg.max_timeout)
    swarm.workspace.ensure_scratch(ctx.actor)  # .scratch/<slug>/ exists for redirects like `> .scratch/...`
    root = swarm.workspace.root
    # git in the shell: the workspace repository (GIT_DIR & co. dropped) and the member as author and committer
    env = agent_git_env(scrubbed_env(swarm.config, {"DREAMTEAM_AGENT": ctx.actor, "DREAMTEAM_WORKSPACE": str(root),
                                                    "DREAMTEAM_SCRATCH": str(root / scratch_rel(ctx.actor))}),
                        ctx.actor)
    # the command's result, or (still running at its timeout, background=true, processes it left running) a job
    outcome = await swarm.jobs.run(ctx.actor, command, timeout=timeout, env=env, max_output=ctx.output_cap,
                                   background=bool(args.get("background")))
    if outcome.state != "killed":  # killed: the session stops or the member leaves, so no git work may start now
        # one commit per call: the subject names the files it changed, the body keeps the command
        await swarm.workspace.autocommit(ctx.actor, kind="shell", detail=command)
    # the footer of a job points at the tools the caller has (`jobs`, `job_stop` or the console commands; `wait`,
    # or `wait_for_events` for an external member)
    return outcome.render(has_jobs_tool=ctx.has_tool("jobs"), cfg=cfg, has_wait=ctx.has_tool("wait"))
