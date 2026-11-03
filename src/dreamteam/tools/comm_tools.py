"""Direct messages between members and the user."""

from __future__ import annotations

from typing import Any

from ..models import USER, ToolError
from . import ToolContext, tool
from .fmt import message_line

# The result notes of send_message(pause_swarm=true), by Swarm.ask_user's outcome ("asked" by the caller's kind: an
# external member is not paused itself). Each is appended to "Sent message #N to …." (docs/SPEC.md §3 "Clarify mode").
PAUSE_NOTES = {
    "asked_llm": " The swarm is paused until the user answers this question; the user's answer then reaches every "
                 "member with your question, and the swarm goes on.",
    "asked_ext": " The swarm is paused until the user answers this question; every member then gets the answer with "
                 "your question. Call wait_for_events: the answer comes there.",
    "off": " pause_swarm was ignored: this session does not pause for questions (the user's setting), so the others "
           "keep working. Go on with the work that doesn't depend on the answer.",
    "auto": " pause_swarm was ignored: the user is not available (questions get an automatic reply), so the swarm "
            "keeps working.",
    "pending": " pause_swarm was ignored: the swarm already waits for the user's answer to {asker}'s question #{id}; "
               "yours was sent as an ordinary message, which the user sees too. Don't send it again.",
    "own": " pause_swarm was ignored: the swarm already waits for the user's answer to your question #{id}; this one "
           "was sent as an ordinary message.",
    "paused": " pause_swarm was ignored: the swarm is already paused ({reason}); this was sent as an ordinary message.",
    "budget": " pause_swarm was ignored: the budget is exhausted, so the swarm pauses for that; this was sent as an "
              "ordinary message.",
    "no_user": " pause_swarm was ignored: it pauses the swarm only for a question to \"user\".",
    "user": " pause_swarm was ignored: only a member's question to the user pauses the swarm.",
}
# the user's message answered a member's question (Mailbox.send -> Swarm.question_answered)
ANSWER_NOTES = {
    "ok": " This answers {asker}'s question #{id}: every member got the question and your answer, and the swarm goes "
          "on.",
    "budget": " This answers {asker}'s question #{id}: every member got the question and your answer, but the budget "
              "is exhausted, so the swarm stays paused until you raise max_cost_usd.",
}


@tool(
    "send_message",
    en="Send a direct message to one or several members (array of names), to [\"all\"] (every member) or to "
       "[\"user\"] (the human). Use it to coordinate, ask, hand off and report: plain text replies reach nobody. "
       "Every message wakes its recipients: no greetings, thanks or acknowledgements. About a specific task, "
       "prefer task_comment. pause_swarm=true is only for a question to [\"user\"] whose answer decides how the "
       "team goes on, so that work already under way would otherwise be redone: the session may then pause the "
       "whole swarm until the user answers, and every member gets your question with the answer. Never use it for "
       "what you can decide yourself, for a question that concerns only your own task, or for a report. Ask "
       "everything in that one message and make it the only tool call of your response: the result says whether "
       "the swarm was paused; if it was not, go on with the work that doesn't depend on the answer.",
    ru="Отправить личное сообщение одному или нескольким участникам (массив имён), всем — [\"all\"], или "
       "пользователю — [\"user\"]. Используй для координации, вопросов, передачи работы и отчётов: обычный "
       "текстовый ответ никому не доставляется. Каждое сообщение будит получателей: без приветствий, "
       "благодарностей и подтверждений. О конкретной задаче лучше пиши через task_comment. pause_swarm=true — "
       "только для вопроса к [\"user\"], от ответа на который зависит, как команда продолжит работу, так что иначе "
       "уже начатую работу пришлось бы переделывать: тогда сессия может поставить весь рой на паузу до ответа "
       "пользователя, и твой вопрос вместе с ответом получат все участники. Никогда не используй его для того, что "
       "можешь решить сам, для вопроса только о своей задаче и для отчётов. Задай все вопросы в этом одном "
       "сообщении и сделай его единственным вызовом инструмента в ответе: результат скажет, встал ли рой на паузу; "
       "если нет, продолжай работу, которая не зависит от ответа.",
    properties={
        "to": {"type": "array", "items": {"type": "string"}, "description": "Recipient names, \"all\" or \"user\""},
        "text": {"type": "string"},
        "reply_to": {"type": "integer", "description": "Number of the message you answer"},
        # toolset revision 4 (tools/frozen.py): agents of r1-r3 never pass it (call_tool strips it)
        "pause_swarm": {"type": "boolean", "description":
                        "true: this question to \"user\" decides how the whole team goes on (work under way would "
                        "otherwise be redone). The session may then pause every member until the user answers and "
                        "give the answer to all of them; the result says whether the swarm was paused. Only with "
                        "\"user\" among the recipients, one such question at a time; never for what you can decide "
                        "yourself."},
    },
    required=["to", "text"],
)
async def send_message(ctx: ToolContext, args: dict[str, Any]) -> str:
    recipients: Any = args["to"]
    if len(recipients) == 1 and str(recipients[0]).strip().lower() in ("all", "все"):
        recipients = "all"
    swarm = ctx.swarm
    pending = getattr(swarm, "pending_question", None)  # (test doubles have none)
    asked = pending() if ctx.is_user and pending is not None else None
    msg = swarm.mail.send(ctx.actor, recipients, args["text"], args.get("reply_to") or None)
    if USER in msg["recipients"] and ctx.runtime is not None:
        ctx.runtime.note_sent_to_user()
    text = f"Sent message #{msg['id']} to {', '.join(msg['recipients'])}."
    if asked is not None and asked["asker"] in msg["recipients"]:
        text += _answer_note(swarm, asked)
    if args.get("pause_swarm"):  # agents of r1-r3 never get here with it (stripped, tools/__init__.py call_tool)
        text += _pause_note(ctx, msg)
    return text


def _answer_note(swarm: Any, asked: dict) -> str:
    """The user's message answered `asked` (the question that held the pause when it was sent)."""
    if swarm.pending_question() == asked:
        return ""  # the hook failed (logged): the swarm still waits
    key = "budget" if swarm.paused and swarm.pause_reason == "budget" else "ok"
    return ANSWER_NOTES[key].format(asker=asked["asker"], id=asked["message_id"])


def _pause_note(ctx: ToolContext, msg: dict) -> str:
    ask = getattr(ctx.swarm, "ask_user", None)  # (test doubles have none)
    outcome, info = ask(ctx.actor, msg) if ask is not None else ("off", {})
    if outcome == "asked":
        outcome = "asked_llm" if ctx.runtime is not None else "asked_ext"
    return PAUSE_NOTES[outcome].format(**info)


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
