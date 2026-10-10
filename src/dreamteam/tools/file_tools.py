"""Shared workspace files and the console."""

from __future__ import annotations

from typing import Any

from ..shell import resolve_shell, run_shell, scrubbed_env
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
       "the shell). Use it to run code, tests, package managers, git, etc. There is no input (stdin is closed); "
       "start long-running servers in the background. Secrets from the environment are not available.",
    ru="Выполнить команду в консоли в папке workspace и получить код возврата и вывод (в результате указана "
       "оболочка). Подходит для запуска кода, тестов, пакетных менеджеров, git и т. п. Ввода нет (stdin "
       "закрыт); долгоживущие серверы запускай в фоне. Секреты из окружения недоступны.",
    properties={
        "command": {"type": "string"},
        "timeout": {"type": "integer", "description": "Seconds (default from config)"},
    },
    required=["command"],
    audience=("llm", "external"),
)
async def shell(ctx: ToolContext, args: dict[str, Any]) -> str:
    swarm = ctx.swarm
    cfg = swarm.config.shell
    timeout = min(max(args.get("timeout") or cfg.timeout, 1), cfg.max_timeout)
    env = scrubbed_env(swarm.config, {"DREAMTEAM_AGENT": ctx.actor, "DREAMTEAM_WORKSPACE": str(swarm.workspace.root)})
    code, output = await run_shell(
        args["command"], cwd=swarm.workspace.root, timeout=timeout, program=cfg.program,
        env=env, max_output=ctx.output_cap,
    )
    await swarm.workspace.autocommit(ctx.actor, f"shell: {args['command'][:72]}")
    name = resolve_shell(cfg.program)[0]
    head = f"TIMEOUT after {timeout}s (process tree killed)" if code is None else f"exit code: {code}"
    return f"{head} [{name}]\n" + (output if output.strip() else "(no output)")
