"""MCP workshop: agents build MCP servers, share them and call their tools."""

from __future__ import annotations

import json
from typing import Any

from ..models import ToolError
from . import ToolContext, tool
from .fmt import trim

_TEMPLATE = '''from mcp.server.mcpserver import MCPServer

mcp = MCPServer("my_tools")

@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

if __name__ == "__main__":
    mcp.run()'''


@tool(
    "mcp_create",
    en="Build a new MCP server (a reusable tool set) from Python code using the mcp 2.x SDK, test it and "
       "register it for the swarm. The code must follow this template (type hints and docstrings become the "
       "tool schema; any library from `requirements` can be imported):\n" + _TEMPLATE + "\n"
       "name: lowercase snake_case. scope: swarm (everyone, default; also saved to the cross-run library), "
       "task (people of task_id) or private. Calling it again with the same name replaces your server. "
       "If the smoke test fails you get the error log: fix the code and call again. Build one only for a "
       "capability that will be reused; for one-off work use shell.",
    ru="Создать новый MCP-сервер (переиспользуемый набор инструментов) из кода на Python с SDK mcp 2.x, "
       "проверить его и зарегистрировать в рое. Код должен следовать шаблону (аннотации типов и docstring "
       "становятся схемой инструмента; можно импортировать библиотеки из `requirements`):\n" + _TEMPLATE + "\n"
       "name: латиница snake_case. scope: swarm (все, по умолчанию; сохраняется и в общую библиотеку между "
       "запусками), task (участники задачи task_id) или private. Повторный вызов с тем же name заменяет твой "
       "сервер. Если проверка не прошла, вернётся лог ошибки: исправь код и вызови снова. Создавай сервер "
       "только для переиспользуемой возможности; для разовой работы используй shell.",
    properties={
        "name": {"type": "string", "description": "e.g. csv_tools"},
        "description": {"type": "string", "description": "What the tools are for (one sentence)"},
        "code": {"type": "string", "description": "Full server.py source"},
        "scope": {"type": "string", "enum": ["swarm", "task", "private"]},
        "task_id": {"type": "integer", "description": "For scope=task"},
        "requirements": {"type": "array", "items": {"type": "string"}, "description": "pip packages, e.g. [\"httpx\"]"},
    },
    required=["name", "description", "code"],
    audience=("llm", "external"),
)
async def mcp_create(ctx: ToolContext, args: dict[str, Any]) -> str:
    forge = ctx.swarm.forge
    report = await forge.create(
        ctx.actor, args["name"], args["description"], args["code"],
        scope=args.get("scope") or "swarm", task_id=args.get("task_id") or None,
        requirements=args.get("requirements") or None,
    )
    if (forge.get(args["name"]) or {}).get("status") == "failed":
        raise ToolError(report)  # an error result, so the model sees that the server is not usable
    return report


@tool(
    "mcp_list",
    en="List MCP servers available to you: built in this swarm, configured by the user, and from the "
       "library of earlier runs. Optional query filters by name/description/tool names.",
    ru="Список доступных тебе MCP-серверов: созданных в этом рое, настроенных пользователем и из "
       "библиотеки прошлых запусков. query фильтрует по имени, описанию и названиям инструментов.",
    properties={"query": {"type": "string"}},
)
async def mcp_list(ctx: ToolContext, args: dict[str, Any]) -> str:
    servers = ctx.swarm.forge.list(ctx.actor, args.get("query") or "")
    if not servers:
        if args.get("query"):
            return f"No MCP servers match '{args['query']}'. mcp_list without query shows all."
        return "No MCP servers found. Build one with mcp_create."
    lines = []
    for s in servers:
        tools = ", ".join(s.get("tools") or []) or "tools discovered on first use"
        line = f"{s['name']} [{s['status']}, {s['origin']}, scope={s['scope']}] by {s.get('owner') or '-'}: {s.get('description') or ''}"
        line += f"\n    tools: {tools}"
        if s.get("error") and s["status"] != "active":
            line += f"\n    error: {trim(s['error'], 200)}"
        lines.append(line)
    return "\n".join(lines)


@tool(
    "mcp_tools",
    en="Show the tools of an MCP server with their input schemas (starts the server if needed).",
    ru="Показать инструменты MCP-сервера с их входными схемами (при необходимости запускает сервер).",
    properties={"server": {"type": "string"}},
    required=["server"],
)
async def mcp_tools(ctx: ToolContext, args: dict[str, Any]) -> str:
    tools = await ctx.swarm.forge.tools(ctx.actor, args["server"])
    if not tools:
        return f"Server {args['server']} has no tools."
    out = []
    for t in tools:
        schema = json.dumps(t.get("input_schema") or {}, ensure_ascii=False)
        out.append(f"- {t['name']}: {t.get('description') or ''}\n  input: {schema}")
    return f"Tools of {args['server']}:\n" + "\n".join(out)


@tool(
    "mcp_call",
    en="Call a tool of an MCP server: server name, tool name and arguments object (see mcp_tools for schemas).",
    ru="Вызвать инструмент MCP-сервера: имя сервера, имя инструмента и объект arguments (схемы — в mcp_tools).",
    properties={"server": {"type": "string"}, "tool": {"type": "string"},
                "arguments": {"type": "object", "description": "Arguments of the MCP tool, e.g. {\"a\": 1}"}},
    required=["server", "tool"],
    audience=("llm", "external"),
)
async def mcp_call(ctx: ToolContext, args: dict[str, Any]) -> str:
    text, is_error = await ctx.swarm.forge.call(ctx.actor, args["server"], args["tool"], args.get("arguments") or {},
                                                max_chars=ctx.output_cap)
    if is_error:
        raise ToolError(f"{args['server']}.{args['tool']} returned an error:\n{text}")
    return text


@tool(
    "mcp_approve",
    en="Approve or reject an MCP server that waits for the user's approval (status pending in mcp_list).",
    ru="Одобрить или отклонить MCP-сервер, ожидающий одобрения пользователя (статус pending в mcp_list).",
    properties={"name": {"type": "string"}, "approve": {"type": "boolean"},
                "comment": {"type": "string", "description": "Shown to the owner (e.g. why it was rejected)"}},
    required=["name", "approve"],
    audience=("user",),
)
async def mcp_approve(ctx: ToolContext, args: dict[str, Any]) -> str:
    return await ctx.swarm.forge.approve(ctx.actor, args["name"], args["approve"], args.get("comment") or "")
