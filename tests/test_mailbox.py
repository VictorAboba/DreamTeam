from __future__ import annotations

import pytest

from dreamteam.mailbox import MAX_TEXT, Mailbox
from dreamteam.models import USER, AgentKind, AgentStatus, ToolError

ANNA, BOB, CARL, OLD = "Anna Smith", "Bob Brown", "Иван Орлов", "Old Timer"


@pytest.fixture
def swarm(fake_swarm):
    fake_swarm.mail = Mailbox(fake_swarm)
    fake_swarm.add_member(ANNA)
    fake_swarm.add_member(BOB, kind=AgentKind.EXTERNAL)
    fake_swarm.add_member(CARL)
    fake_swarm.add_member(OLD, status=AgentStatus.RETIRED)
    return fake_swarm


@pytest.fixture
def mail(swarm) -> Mailbox:
    return swarm.mail


def sent_events(swarm) -> list[dict]:
    return [e for e in swarm.bus.recent(limit=1000) if e["type"] == "message.sent"]


def inbox(swarm, name: str) -> list[int]:
    return [r["message_id"] for r in swarm.db.all(
        "SELECT message_id FROM inbox WHERE recipient = ? ORDER BY message_id", (name,))]


# --- send --------------------------------------------------------------------------------------------

def test_send_to_one(swarm, mail):
    msg = mail.send(ANNA, "bob  brown", "  hello  ")
    assert set(msg) == {"id", "ts", "sender", "recipients", "text", "reply_to"}
    assert msg["sender"] == ANNA and msg["recipients"] == [BOB] and msg["text"] == "hello"
    assert msg["reply_to"] is None and msg["id"] == 1
    assert inbox(swarm, BOB) == [1]
    assert swarm.signals == [BOB]
    ev = sent_events(swarm)
    assert len(ev) == 1 and ev[0]["actor"] == ANNA and ev[0]["data"] == {"message": msg}
    assert mail.get(msg["id"]) == msg


def test_send_to_list_dedupes_and_signals_each(swarm, mail):
    msg = mail.send(ANNA, [BOB, "иван орлов", "BOB BROWN", " Bob  Brown "], "hi")
    assert msg["recipients"] == [BOB, CARL]
    assert swarm.signals == [BOB, CARL]
    assert inbox(swarm, BOB) == [1] and inbox(swarm, CARL) == [1]


def test_send_comma_separated_string(swarm, mail):
    assert mail.send(USER, "Anna Smith, bob brown", "hi")["recipients"] == [ANNA, BOB]


def test_send_to_all(swarm, mail):
    msg = mail.send(ANNA, "all", "team meeting")
    assert msg["recipients"] == [BOB, CARL]  # no sender, no retired, no USER
    assert USER not in msg["recipients"] and OLD not in msg["recipients"]
    msg = mail.send(USER, ["ALL"], "from the user")
    assert msg["recipients"] == [ANNA, BOB, CARL]


def test_send_all_plus_user(swarm, mail):
    msg = mail.send(ANNA, ["all", "user"], "everyone incl. human")
    assert msg["recipients"] == [BOB, CARL, USER]


def test_send_to_user(swarm, mail):
    for alias in ("user", "USER", "пользователь"):
        msg = mail.send(ANNA, alias, "report")
        assert msg["recipients"] == [USER]
    assert swarm.signals == [USER] * 3
    assert mail.unread_count(USER) == 3


def test_send_to_self(swarm, mail):
    with pytest.raises(ToolError, match="only to yourself"):
        mail.send(ANNA, "anna smith", "note to self")
    msg = mail.send(ANNA, [ANNA, BOB], "both")
    assert msg["recipients"] == [BOB]
    assert inbox(swarm, ANNA) == []


def test_send_all_with_nobody_else(make_fake_swarm):
    swarm = make_fake_swarm()
    swarm.mail = Mailbox(swarm)
    swarm.add_member(ANNA)
    with pytest.raises(ToolError, match="no other active members"):
        swarm.mail.send(ANNA, "all", "anyone?")


def test_send_validation(swarm, mail):
    with pytest.raises(ToolError, match="empty"):
        mail.send(ANNA, BOB, "   ")
    with pytest.raises(ToolError, match="too long"):
        mail.send(ANNA, BOB, "x" * (MAX_TEXT + 1))
    assert len(mail.send(ANNA, BOB, "x" * MAX_TEXT)["text"]) == MAX_TEXT
    with pytest.raises(ToolError, match="No recipients"):
        mail.send(ANNA, [], "hi")
    with pytest.raises(ToolError, match="No recipients"):
        mail.send(ANNA, "  ", "hi")
    with pytest.raises(ToolError, match="Unknown member 'Bobby Brown'"):
        mail.send(ANNA, [BOB, "Bobby Brown"], "hi")
    with pytest.raises(ToolError, match="Unknown member"):
        mail.send(ANNA, OLD, "are you retired?")
    assert len(sent_events(swarm)) == 1
    assert swarm.db.scalar("SELECT COUNT(*) FROM messages") == 1


def test_send_reply_to(swarm, mail):
    first = mail.send(ANNA, BOB, "question")
    answer = mail.send(BOB, ANNA, "answer", reply_to=first["id"])
    assert answer["reply_to"] == first["id"]
    assert mail.send(BOB, ANNA, "again", reply_to=str(first["id"]))["reply_to"] == first["id"]
    with pytest.raises(ToolError, match="Message #99 does not exist"):
        mail.send(BOB, ANNA, "answer", reply_to=99)
    with pytest.raises(ToolError, match="message id"):
        mail.send(BOB, ANNA, "answer", reply_to="abc")


def test_get_missing(mail):
    with pytest.raises(ToolError, match="Message #5 not found"):
        mail.get(5)


# --- unread ------------------------------------------------------------------------------------------

def test_unread_marks_read(swarm, mail):
    m1 = mail.send(ANNA, BOB, "one")
    m2 = mail.send(CARL, [BOB, ANNA], "two")
    assert mail.unread_count(BOB) == 2 and mail.unread_count(ANNA) == 1
    assert mail.unread(BOB, mark_read=False) == [m1, m2]
    assert mail.unread_count(BOB) == 2
    assert mail.unread(BOB) == [m1, m2]
    assert mail.unread_count(BOB) == 0 and mail.unread(BOB) == []
    assert mail.unread_count(ANNA) == 1  # read state is per recipient
    assert not swarm.has_pending(BOB)


def test_unread_limit_oldest_first(mail):
    msgs = [mail.send(ANNA, BOB, f"m{i}") for i in range(5)]
    assert mail.unread(BOB, limit=2) == msgs[:2]
    assert mail.unread_count(BOB) == 3
    assert mail.unread(BOB) == msgs[2:]


# --- history / conversation / recent -------------------------------------------------------------------

def test_history(swarm, mail):
    m1 = mail.send(ANNA, BOB, "a->b")
    m2 = mail.send(BOB, ANNA, "b->a")
    m3 = mail.send(CARL, BOB, "c->b")
    m4 = mail.send(ANNA, CARL, "a->c")
    m5 = mail.send(USER, "all", "broadcast")
    assert mail.history(BOB) == [m1, m2, m3, m5]
    assert mail.history(ANNA) == [m1, m2, m4, m5]
    assert mail.history(ANNA, with_name="bob brown") == [m1, m2]
    assert mail.history(ANNA, with_name="user") == [m5]
    assert mail.history(BOB, limit=2) == [m3, m5]
    assert mail.history(BOB, before_id=m3["id"]) == [m1, m2]
    assert mail.history(BOB, before_id=m5["id"], limit=1) == [m3]
    assert mail.history(ANNA, with_name="Somebody Unknown") == []
    assert mail.history(BOB, with_name=CARL) == [m3]


def test_history_does_not_mark_read(mail):
    mail.send(ANNA, BOB, "x")
    mail.history(BOB)
    assert mail.unread_count(BOB) == 1


def test_conversation(swarm, mail):
    m1 = mail.send(USER, ANNA, "hi Anna")
    m2 = mail.send(ANNA, USER, "hi user")
    mail.send(ANNA, BOB, "side chat")
    m4 = mail.send(USER, [ANNA, BOB], "both of you")
    m5 = mail.send(ANNA, [USER, CARL], "cc carl")
    assert mail.conversation(USER, ANNA) == [m1, m2, m4, m5]
    assert mail.conversation(ANNA, USER) == [m1, m2, m4, m5]
    assert mail.conversation(USER, ANNA, since_id=m2["id"]) == [m4, m5]
    assert mail.conversation(USER, ANNA, since_id=m1["id"], limit=2) == [m2, m4]
    assert mail.conversation(USER, ANNA, limit=2) == [m4, m5]
    assert mail.conversation(USER, CARL) == []


def test_recent(mail):
    msgs = [mail.send(ANNA, BOB, f"m{i}") for i in range(5)]
    assert mail.recent() == msgs
    assert mail.recent(limit=2) == msgs[-2:]
    assert mail.recent(since_id=msgs[1]["id"]) == msgs[2:]
    assert mail.recent(since_id=msgs[1]["id"], limit=1) == [msgs[2]]
    assert mail.recent(since_id=msgs[-1]["id"]) == []


def test_malformed_recipients_and_limits_are_tool_errors(swarm, mail):
    # these used to escape as TypeError / ValueError ("Internal error" for the caller)
    with pytest.raises(ToolError, match='Invalid recipients 5: use a member name, a list of names, "all"'):
        mail.send(ANNA, 5, "hi")
    assert mail.send(ANNA, (BOB, None), "tuple")["recipients"] == [BOB]
    for bad in ("abc", None, True):
        with pytest.raises(ToolError, match="limit must be a positive integer"):
            mail.recent(limit=bad)
        with pytest.raises(ToolError, match="limit must be a positive integer"):
            mail.unread(BOB, limit=bad)
        with pytest.raises(ToolError, match="limit must be a positive integer"):
            mail.history(BOB, limit=bad)
    assert len(mail.recent(limit=" 1 ")) == 1 and mail.unread_count(BOB) == 1


def test_unicode_roundtrip(swarm, mail):
    msg = mail.send(CARL, ANNA, "Привет, как дела? ✓")
    assert mail.get(msg["id"])["text"] == "Привет, как дела? ✓"
    assert mail.get(msg["id"])["recipients"] == [ANNA]
    assert sent_events(swarm)[0]["data"]["message"]["text"] == "Привет, как дела? ✓"
