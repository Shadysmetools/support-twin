"""Principal 2026-11-15 ("make the telegram replies go through the twin"): Supervisor's Digital Twin kit works the ticket,
but a Telegram requester must get the reply on Telegram, in his thread, as Principal. The kit hands the text Principal
approved in its session to the twin with `say REQ-x <text>`; the twin posts it through its normal send guards (an
email request gets a Gmail draft). In label mode the twin never closes or moves the ticket: the kit owns it."""
import asyncio

from twin_agent.bus import Topics
from twin_agent.sender_approval import check_approval
from twin_agent.testing import fakes
from tests.test_closing import _flow_request, flow  # noqa: F401  (fixture)


def run(c):
    return asyncio.run(c)


def _published(f):
    out = []
    f.bus.publish = lambda topic, p=None: out.append((topic, p))
    return out


def test_say_posts_the_approved_text_as_an_principal_edit(flow, clock):  # noqa: F811
    f = flow
    rid = _flow_request(f, clock)
    pub = _published(f)
    text = "Dear Principal,\n\nThe batch is now validated.\n\nBest regards,"
    run(f.on_command({"user_id": fakes.PRINCIPAL_ID, "command": "say", "args": f"{rid}\n{text}"}))
    sends = [p for t, p in pub if t == Topics.SEND_REQUEST]
    assert len(sends) == 1
    p = sends[0]
    msg = p["msg"]
    assert msg.request_id == rid and msg.text == text and str(p["trigger"]).endswith("principal_edit")
    row, why = check_approval(f.db, f.cfg, msg, p["trigger"], p["nonce"], p["user_id"], clock.now())
    assert row, why                                          # the sender will accept it as Principal's own approval
    assert f.pending_close.get(msg.send_key) == (rid, fakes.PRINCIPAL_ID)


def test_say_also_accepts_the_text_on_the_same_line(flow, clock):  # noqa: F811
    f = flow
    rid = _flow_request(f, clock)
    pub = _published(f)
    run(f.on_command({"user_id": fakes.PRINCIPAL_ID, "command": "say", "args": f"{rid.lower()} Thank you, done."}))
    sends = [p for t, p in pub if t == Topics.SEND_REQUEST]
    assert sends and sends[0]["msg"].text == "Thank you, done."


def test_say_from_anyone_else_or_empty_does_nothing(flow, clock):  # noqa: F811
    f = flow
    rid = _flow_request(f, clock)
    pub = _published(f)
    run(f.on_command({"user_id": 999, "command": "say", "args": f"{rid}\nhello"}))
    run(f.on_command({"user_id": fakes.PRINCIPAL_ID, "command": "say", "args": f"{rid}\n   "}))
    run(f.on_command({"user_id": fakes.PRINCIPAL_ID, "command": "say", "args": "REQ-9999\nhello"}))
    assert not [p for t, p in pub if t == Topics.SEND_REQUEST]


def test_label_mode_close_never_moves_the_ticket(cfg, clock, bus, tmp_home):
    """The kit owns the ticket: closing or reopening the request leaves its stage alone."""
    from tests.test_helpdesk import _desk, _request_no_proof
    from tests.test_ticket_routing import _label_cfg
    _label_cfg(cfg)
    cfg["helpdesk"]["principal_close_stage_id"] = 3
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request_no_proof(db, clock)
    asyncio.run(desk.on_triaged({"request_id": rid}))
    tid = desk.ledger.ticket_of(rid)
    fake.data["helpdesk.ticket"][tid]["stage_id"] = 11                   # the kit moved it to Client Testing
    db.update_request(rid, ticket_id=str(tid))
    desk.close_by_principal(rid, [])
    asyncio.run(desk.on_reopened({"request_id": rid}))
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] == 11


# ---- 15 Nov 2026 (Principal, "Yes, build both"): a follow-up reply reaches the ticket the kit is working ------------

def _handed_over(cfg, clock, bus, tmp_home):
    from tests.test_helpdesk import _desk, _request_no_proof
    from tests.test_ticket_routing import READS, _checks, _label_cfg
    _label_cfg(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request_no_proof(db, clock)
    asyncio.run(desk.on_triaged({"request_id": rid}))
    tid = desk.ledger.ticket_of(rid)
    db.update_request(rid, ticket_id=str(tid))
    packet = {"request_id": rid, "tier": "T2a", "track": "functional", "ticket_type": "bug", "evidence": READS}
    asyncio.run(desk.on_checks(_checks(rid, packet)))
    fake.data["helpdesk.ticket"][tid]["stage_id"] = 5                    # the kit asked; Customer Feedback
    return db, fake, desk, rid, tid, packet


def _notes(fake, tid):
    return [w["kwargs"]["body"] for w in fake.writes if w["method"] == "message_post" and w["ids"] == [tid]
            and "Client reply via the twin" in w["kwargs"]["body"]]


def test_follow_up_message_is_copied_onto_the_ticket(cfg, clock, bus, tmp_home):
    from twin_agent.testing.fakes import STAFF_A, make_event
    db, fake, desk, rid, tid, _ = _handed_over(cfg, clock, bus, tmp_home)
    ev = make_event("It came back on the Le Marche car", msg_id=901, sender=STAFF_A, ts=clock.now())
    db.add_message(ev, rid)
    asyncio.run(desk.on_request_updated({"request_id": rid, "event_id": ev.event_id}))
    notes = _notes(fake, tid)
    assert len(notes) == 1 and notes[0].startswith("<p>[Client reply via the twin]")
    assert "It came back on the Le Marche car" in notes[0] and rid in notes[0]
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] == 5                  # the kit moves it, never the twin


def test_reopened_request_copies_the_latest_client_message(cfg, clock, bus, tmp_home):
    from twin_agent.testing.fakes import STAFF_A, make_event
    db, fake, desk, rid, tid, _ = _handed_over(cfg, clock, bus, tmp_home)
    db.add_message(make_event("Still blocked", msg_id=902, sender=STAFF_A, ts=clock.now()), rid)
    asyncio.run(desk.on_reopened({"request_id": rid}))
    assert len(_notes(fake, tid)) == 1 and "Still blocked" in _notes(fake, tid)[0]
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] == 5


def test_principal_own_message_is_not_copied(cfg, clock, bus, tmp_home):
    from twin_agent.testing.fakes import PRINCIPAL, make_event
    db, fake, desk, rid, tid, _ = _handed_over(cfg, clock, bus, tmp_home)
    ev = make_event("Please check the car", msg_id=903, sender=PRINCIPAL, ts=clock.now())
    db.add_message(ev, rid)
    asyncio.run(desk.on_request_updated({"request_id": rid, "event_id": ev.event_id}))
    assert not _notes(fake, tid)


def test_re_analysis_after_a_reopen_never_moves_a_handed_over_ticket(cfg, clock, bus, tmp_home):
    from tests.test_ticket_routing import _checks
    db, fake, desk, rid, tid, packet = _handed_over(cfg, clock, bus, tmp_home)
    asyncio.run(desk.on_checks(_checks(rid, dict(packet, analysis="second look"))))
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] == 5


# ---- 15 Nov 2026 (REQ-0069 / ticket 2684): the client's answer to a `say` reply landed on another request ----------
# The twin posted REQ-0069's approved reply as Telegram msg 6727; the client answered it as a Telegram reply (6728).
# Our own sent message is in `sends` (remote_id 6727) but not in `messages`, so the router fell back to the same
# person's newest open request (REQ-0080) instead of reopening REQ-0069.

import pytest  # noqa: E402

from twin_agent.models import TERMINAL, SendResult, SendStatus, State  # noqa: E402
from twin_agent.testing.fakes import STAFF_A, STAFF_B, make_event  # noqa: E402
from tests.test_closing import settle  # noqa: E402

SENT_ID = 6727


def _posts_as_telegram_would(f, remote_id):
    """The sender with every guard passed: the row it records and the SEND_DONE it publishes. The guards have their
    own tests; this one is about what happens to the client's answer afterwards."""
    async def send(msg, trigger, approval_nonce=None, approver_user_id=None):
        assert f.db.claim_send(msg, trigger)
        f.db.finish_send(msg.send_key, SendStatus.sent, remote_id=str(remote_id))
        res = SendResult(SendStatus.sent, msg.send_key, remote_id=str(remote_id))
        f.bus.publish(Topics.SEND_DONE, res)
        return res
    f.sender.send = send


@pytest.mark.parametrize("replier", [STAFF_A, STAFF_B], ids=["same-requester", "other-sender"])
def test_reply_to_a_say_message_reopens_its_request_not_the_newest_open_one(flow, clock, replier):  # noqa: F811
    f = flow
    _posts_as_telegram_would(f, SENT_ID)
    rid_a = _flow_request(f, clock, text="where do I set the night stand price?")
    run(f.on_command({"user_id": fakes.PRINCIPAL_ID, "command": "say", "args": f"{rid_a}\nWhich product do you mean?"}))
    run(settle(f))
    assert f.db.get_request(rid_a)["state"] == State.CLOSED_BY_PRINCIPAL          # say = Approve + close
    assert f.db.send_by_remote(fakes.GROUP_ID, SENT_ID)["request_id"] == rid_a
    assert f.db.get_message(f"tg:{fakes.GROUP_ID}:{SENT_ID}") is None         # no echo stored: the real-life gap

    clock.advance(minutes=40)                                                 # past the burst / "recent" window
    rid_b = run(f.router.on_event(make_event("BATCH/13999 cannot validate", msg_id=60, sender=replier,
                                             ts=clock.now())))
    assert rid_b and rid_b != rid_a and f.db.get_request(rid_b)["state"] not in TERMINAL

    clock.advance(minutes=2)
    got = run(f.router.on_event(make_event("THE NIGHT STAND", msg_id=SENT_ID + 1, sender=replier, ts=clock.now(),
                                           reply_to=SENT_ID)))
    assert got == rid_a                                                       # traced through `sends`
    assert f.db.get_request(rid_a)["state"] not in TERMINAL                  # and reopened
    assert f.db.get_message(f"tg:{fakes.GROUP_ID}:{SENT_ID + 1}")["request_id"] == rid_a
    assert "THE NIGHT STAND" not in [m["text"] for m in f.db.thread(rid_b)]
