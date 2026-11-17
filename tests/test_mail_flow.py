"""Email flow, Principal 2026-11-11 ("fix email"): one request per Gmail thread, his sent reply closes it, a new mail
in the thread reopens it, Approve builds a reply draft for the same thread, quotes are stripped, one reminder."""
import asyncio
import json
from datetime import datetime

import pytest

from twin_agent import paths, router, timers
from twin_agent.channels.gmail_common import strip_quoted
from twin_agent.db import StateDB
from twin_agent.models import Channel, Event, OutKind, Sender, State
from tests.test_closing import flow  # noqa: F401  (the Flow fixture)

SIGNATORY = Sender(name="Signatory Kate", email="kate@globex.example.net")
PRINCIPAL = Sender(name="Principal Doe", email="principal.doe@globex.example.net", is_principal=True)


def run(coro):
    return asyncio.run(coro)


def mail(clock, n, text, sender=SIGNATORY, thread="T1", subject="Purchase to Pay Process Streamline"):
    return Event(event_id=f"gm:ticketmail:m{n}", channel=Channel.email, chat="ticketmail", chat_id="ticketmail",
                 topic=None, topic_id=None, sender=sender, ts=clock.now(), text=text, msg_id=f"m{n}",
                 subject=subject, thread_id=thread, untrusted=not sender.is_principal,
                 headers={"message-id": f"<id{n}@globex.example.net>", "subject": subject})


@pytest.fixture
def env(tmp_home, clock, cfg, bus):
    db = StateDB(paths.state_db(), clock, cfg)
    r = router.Router(db, cfg, timers.TimerPolicy(cfg), bus, clock)
    r.today_only, r.reopen_days = True, 7
    yield db, r
    db.close()


def test_one_request_per_thread_and_principals_reply_closes_it(env, clock):
    db, r = env

    async def go():
        rid = await r.on_event(mail(clock, 1, "Let us know the next step."))
        await r.close_burst(rid)
        clock.advance(hours=5)
        assert await r.on_event(mail(clock, 2, "Basem, start the pilot asap.")) == rid   # same thread, same request
        info = json.loads(db.get_cursor(f"gmreq:{rid}"))
        assert info["thread_id"] == "T1" and info["last_message_id"] == "<id2@globex.example.net>"
        assert await r.on_event(mail(clock, 3, "Dear Signatory, next step is ...", sender=PRINCIPAL)) == rid
        assert db.get_request(rid)["state"] == State.CLOSED_BY_PRINCIPAL                       # his sent reply closes
        clock.advance(hours=20)
        assert await r.on_event(mail(clock, 4, "Ross, please send the names.")) == rid
        assert db.get_request(rid)["state"] == State.TRIAGED                               # reopened
        other = await r.on_event(mail(clock, 5, "A different subject", thread="T2", subject="Other"))
        assert other != rid
    run(go())


def test_outlook_quoted_history_is_stripped():
    text = ("Principal,\n\nLet us know the next step.\n\nFrom: ben.hale@globex.example.net <ben.hale@globex.example.net>\n"
            "Date: Sunday, 8 November 2026 at 4:43 PM\nTo: 'Principal Doe'\nSubject: RE: Purchase\n\nReminder.")
    assert strip_quoted(text) == "Principal,\n\nLet us know the next step."
    text2 = "Thanks Principal,\n\nBasem, start the pilot.\n________________________________\nFrom: Principal\nSent: Monday"
    assert strip_quoted(text2) == "Thanks Principal,\n\nBasem, start the pilot."
    assert strip_quoted("From: me, to the team: hello") == "From: me, to the team: hello"   # not a header block


def test_reply_draft_goes_to_the_same_thread(flow, clock):
    f = flow
    rid = run(f.router.on_event(mail(clock, 1, "Let us know the next step.")))
    f.db.save_packet(rid, {"request_id": rid, "tier": "T1", "draft_reply": {"text": "Hi Signatory, next step ..."}},
                     False, [])
    msg = f.outbound_for(rid)
    assert msg.kind == OutKind.email_reply and msg.to == ["kate@globex.example.net"]
    assert msg.subject == "Re: Purchase to Pay Process Streamline" and msg.thread_id == "T1"
    assert msg.in_reply_to == "<id1@globex.example.net>"
    from twin_agent.listener import known_mail_threads
    assert known_mail_threads(f.db) == {"T1": ["kate@globex.example.net"]}


def test_email_reminder_only_once_and_only_in_working_hours(flow, clock):
    from twin_agent import flow_timers
    from twin_agent.models import TimerKind
    f = flow
    rid = run(f.router.on_event(mail(clock, 1, "Let us know the next step.")))
    req = f.db.get_request(rid)
    cards = []
    f.card = lambda kind, text, rid_=None, **kw: cards.append(text)
    f.policy.is_working_time = lambda now: False
    run(flow_timers._request_timer(f, TimerKind.reminder, rid, req))
    assert cards == []                                                  # night: nothing
    f.policy.is_working_time = lambda now: True
    run(flow_timers._request_timer(f, TimerKind.reminder, rid, req))
    run(flow_timers._request_timer(f, TimerKind.reminder, rid, req))
    assert len(cards) == 1                                              # one reminder, ever


def test_principals_mail_reply_closes_the_ticket_but_not_a_development_request(env, clock, bus):
    from twin_agent.bus import Topics
    db, r = env
    seen = []

    async def on_cmd(p):
        seen.append(p)
    bus.subscribe(Topics.COMMAND_IN, on_cmd)

    async def go():
        rid = await r.on_event(mail(clock, 1, "How do I print the PO?"))
        await r.close_burst(rid)
        db.update_request(rid, ticket_id="2680")
        db.save_packet(rid, {"request_id": rid, "tier": "T1"}, False, [])
        await r.on_event(mail(clock, 2, "Open the PO, then Print.", sender=PRINCIPAL))
        assert db.get_request(rid)["state"] == State.CLOSED_BY_PRINCIPAL
        dev = await r.on_event(mail(clock, 3, "We need a new report.", thread="T9", subject="Report"))
        await r.close_burst(dev)
        db.save_packet(dev, {"request_id": dev, "tier": "T3"}, False, [])
        await r.on_event(mail(clock, 4, "Raised with development.", sender=PRINCIPAL, thread="T9", subject="Report"))
        assert db.get_request(dev)["state"] != State.CLOSED_BY_PRINCIPAL              # stays open until delivered
        await bus.drain()
        return rid
    rid = run(go())
    assert [p for p in seen if p.get("command") == "close"] == [
        {"user_id": seen[0]["user_id"], "command": "close", "args": rid}]
