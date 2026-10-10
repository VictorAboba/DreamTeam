"""Direct messages between members and the user."""

from __future__ import annotations

from typing import Any

from ..models import USER, ToolError
from . import ToolContext, tool
from .fmt import message_line


@tool(
    "send_message",
    en="Send a direct message to one or several members (array of names), to [\"all\"] (every member) or to "
       "[\"user\"] (the human). Use it to coordinate, ask, hand off and report: plain text replies reach nobody. "
       "Every message wakes its recipients: no greetings, thanks or acknowledgements. About a specific task, "
       "prefer task_comment.",
    ru="Отправить личное сообщение одному или нескольким участникам (массив имён), всем — [\"all\"], или "
       "пользователю — [\"user\"]. Используй для координации, вопросов, передачи работы и отчётов: обычный "
       "текстовый ответ никому не доставляется. Каждое сообщение будит получателей: без приветствий, "
       "благодарностей и подтверждений. О конкретной задаче лучше пиши через task_comment.",
    properties={
        "to": {"type": "array", "items": {"type": "string"}, "description": "Recipient names, \"all\" or \"user\""},
        "text": {"type": "string"},
        "reply_to": {"type": "integer", "description": "Number of the message you answer"},
    },
    required=["to", "text"],
)
async def send_message(ctx: ToolContext, args: dict[str, Any]) -> str:
    recipients: Any = args["to"]
    if len(recipients) == 1 and str(recipients[0]).strip().lower() in ("all", "все"):
        recipients = "all"
    msg = ctx.swarm.mail.send(ctx.actor, recipients, args["text"], args.get("reply_to") or None)
    if USER in msg["recipients"] and ctx.runtime is not None:
        ctx.runtime.note_sent_to_user()
    return f"Sent message #{msg['id']} to {', '.join(msg['recipients'])}."


@tool(
    "read_messages",
    en="Read messages. Without arguments returns your unread messages (they are also shown when you wake up). "
       "with=<name> shows your conversation with that member (or \"user\"); include_read=true shows your recent "
       "message history.",
    ru="Прочитать сообщения. Без аргументов — непрочитанные (они также показываются при пробуждении). "
       "with=<имя> — переписка с участником (или \"user\"); include_read=true — недавняя история сообщений.",
    properties={
        "with": {"type": "string", "description": "Member name or \"user\""},
        "limit": {"type": "integer", "description": "Max messages (default 30, max 200)"},
        "include_read": {"type": "boolean"},
    },
)
async def read_messages(ctx: ToolContext, args: dict[str, Any]) -> str:
    mail = ctx.swarm.mail
    limit = max(1, min(args.get("limit") or 30, 200))
    if args.get("with"):
        other = ctx.swarm.resolve_member(args["with"], allow_retired=True)
        if other == ctx.actor:
            raise ToolError("That is you; pass another member's name.")
        msgs = mail.conversation(ctx.actor, other, limit=limit)
        title = f"Conversation with {other}"
    elif args.get("include_read"):
        msgs = mail.history(ctx.actor, limit=limit)
        title = "Recent messages"
    else:
        msgs = mail.unread(ctx.actor, mark_read=True, limit=limit)
        title = "Unread messages"
    if not msgs:
        return f"{title}: none."
    # what was shown counts as read: otherwise it is delivered again at the next wake-up / wait_for_events
    mail.mark_read(ctx.actor, [m["id"] for m in msgs])
    return f"{title} ({len(msgs)}):\n" + "\n\n".join(message_line(m, ctx.actor) for m in msgs)
