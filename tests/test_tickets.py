"""tickets / ticket_parse / ticket_readback: house style, TEST mark, dedupe, ack linking, never-open, read-back."""
from __future__ import annotations

from datetime import datetime

from twin_agent.channels import tickets
from twin_agent.channels.ticket_readback import (READBACK_1, READBACK_2, alert_cards, find_ack, readback_step,
                                                  resend_decision, resend_step, sent_query)
from twin_agent.channels.tickets import (build_ticket_email, count_sent_tickets, dedupe, is_auto_mail,
                                          normalize_subject, parse_ticket_mail, title_case)
from twin_agent.clock import CAIRO
from twin_agent.models import Channel, Event, OutKind, Sender

REQ = {"id": "REQ-0150", "company": "EG", "requester_name": "Staff A", "chat": 'Telegram "Odoo x Globex"',
       "topic": "Logistics", "created_at": "2026-11-02 14:40 Cairo", "refs": ["WAVE/00001"]}
PART = {"part": "a", "type": "functional", "subject": "block adding lines to PO receipts in waves",
        "body": "**Where:** Inventory > Receipts, WAVE/00001\n• Expected: no manual line — ever\n"
                "Actual: wrong variants added, call +20 100 000 0000\nref: REQ-9999"}
PACKET = {"class": "RB stray-in-line", "company": "EG", "proposed_action": {"records": ["IN/00001/26"]},
          "ticket_draft": {"parts": [PART]}}


def ev(text="", *, frm="info@example.com", subject="", headers=None, name="Staff | Globex") -> Event:
    h = {"from": f'"{name}" <{frm}>'}
    h.update(headers or {})
    return Event(event_id="gm:ticketmail:x", channel=Channel.ticket_mail, chat="ticketmail", chat_id="ticketmail",
                 topic=None, topic_id=None, sender=Sender(name=name, email=frm),
                 ts=datetime(2026, 11, 2, 15, 0, tzinfo=CAIRO), text=text, subject=subject, headers=h)


def test_ticket_email_shape_and_style():
    msg = build_ticket_email(REQ, PACKET, PART, {}, sent_count=5)
    assert msg.to == ["help@example.com"] and msg.cc == [] and msg.kind == OutKind.ticket_email
    assert msg.step == "ticket-a" and msg.channel == Channel.ticket_mail
    assert msg.subject == "[Globex EG] Block Adding Lines to PO Receipts in Waves"
    lines = msg.text.split("\n")
    assert lines[0] == "Request"
    assert all(x.startswith("- ") for x in lines[1:-2]) and lines[-1] == "ref: REQ-0150"
    assert "—" not in msg.text and "**" not in msg.text and "•" not in msg.text
    assert "REQ-9999" not in msg.text                         # worker's own ref line dropped
    assert "+20 100" not in msg.text                          # phone redacted
    assert lines[-3] == tickets.CLOSE_LINE
    assert len(msg.draft_hash) == 64


def test_first_three_are_test_and_unknown_is_test():
    for n, expect in [(None, True), (0, True), (2, True), (3, False)]:
        msg = build_ticket_email(REQ, PACKET, PART, {}, sent_count=n)
        assert msg.subject.startswith("[TEST - please close]") is expect
        assert ("[TEST - please close]" in msg.text) is expect
    rows = [{"kind": "ticket_email", "status": "sent"}, {"kind": "ticket_email", "status": "would_send"},
            {"kind": "holding", "status": "sent"}]
    assert count_sent_tickets(rows) == 1


def test_split_parts_get_letter_refs():
    pb = dict(PART, part="b", subject="question about waves")
    packet = dict(PACKET, ticket_draft={"parts": [PART, pb]})
    msg = build_ticket_email(REQ, packet, pb, {}, sent_count=9)
    assert msg.text.endswith("ref: REQ-0150-b") and msg.step == "ticket-b"


def test_title_case_keeps_ids():
    assert title_case("fix negative stock when QC fails on IN/00001/26") == \
        "Fix Negative Stock when QC Fails on IN/00001/26"


def test_dedupe():
    assert dedupe(REQ, PACKET, PART, [], {"Cc": "Helpdesk <help@example.com>"}).action == "skip"
    known = [{"ticket_id": "9001", "subject": "x", "cls": "stray-in-line", "records": ["IN/00001/26"], "open": True}]
    d = dedupe(REQ, PACKET, PART, known)
    assert d.action == "reply" and d.ticket["ticket_id"] == "9001"
    closed = [dict(known[0], open=False)]
    assert dedupe(REQ, PACKET, PART, closed).action == "new"
    by_subject = [{"ticket_id": "7", "subject": "Re: [Globex EG] Block Adding Lines to PO Receipts in Waves (#7)"}]
    assert dedupe(REQ, PACKET, PART, by_subject).action == "reply"


def test_is_auto_mail():
    assert is_auto_mail(ev(frm="info@example.com", name="Staff A | Globex"))   # relayed customer reply
    assert is_auto_mail(ev(frm="x@helpdesk.example.com"))
    assert is_auto_mail(ev(frm="a@example.org", headers={"auto-submitted": "auto-replied"}))
    assert is_auto_mail(ev(frm="a@example.org", headers={"x-autoreply": "yes"}))
    assert is_auto_mail(ev(frm="a@example.org", headers={"message-id": "<1.2-openerp-5-helpdesk.ticket@h>"}))
    assert not is_auto_mail(ev(frm="a@example.org", headers={"auto-submitted": "no"}))
    assert not is_auto_mail(ev(frm="a@example.com.evil.example.net", name="help@example.com"))


ACK = ("Dear Principal, Your request [Globex EG] Block Adding Lines has been received and is being reviewed by our "
       "Helpdesk Support team. The reference of your ticket is 9001.\n"
       "View: https://helpdesk.example.com/my/ticket/9001/00000000-aaaa-bbbb-cccc-000000000000")


def test_ack_parsed_token_stripped_and_linked_by_ref():
    upd = parse_ticket_mail(ev(ACK + "\nref: REQ-0150", subject="[Globex EG] Block Adding Lines (#9001)"))
    assert (upd.request_id, upd.ticket_id, upd.kind) == ("REQ-0150", "9001", "ack")
    assert upd.ticket_url == "https://helpdesk.example.com/my/ticket/9001"
    assert "00000000-aaaa" not in upd.text


def test_ack_linked_by_subject_or_references():
    pending = [{"request_id": "REQ-0150", "part": None, "subject": "[TEST - please close] [Globex EG] Block Adding Lines",
                "message_id": "<ours-1@mail.example.org>"}]
    upd = parse_ticket_mail(ev(ACK, subject="[Globex EG] Block Adding Lines (#9001)"), pending=pending)
    assert upd.request_id == "REQ-0150"
    upd2 = parse_ticket_mail(ev("New message", subject="Something else",
                                headers={"references": "<ours-1@mail.example.org> <9-openerp-9001-helpdesk.ticket@x>"}),
                             pending=pending)
    assert (upd2.request_id, upd2.ticket_id, upd2.kind) == ("REQ-0150", "9001", "message")
    assert parse_ticket_mail(ev(ACK, subject="Other (#9001)")).request_id is None   # number only, no link


def test_ticket_id_sources():
    enc = ("View Helpdesk Ticket https://redirect-url.email/?link=https%3A%2F%2Fhelpdesk.example.com%2Fmail%2Fview"
           "%3Fmodel%3Dhelpdesk.ticket%26res_id%3D9002")
    assert parse_ticket_mail(ev(enc)).ticket_id == "9002"
    assert parse_ticket_mail(ev("x", headers={"message-id": "<1-openerp-9003-helpdesk.ticket@h>"})).ticket_id == "9003"
    assert parse_ticket_mail(ev("hello there")) is None


def test_stage_change_and_close():
    upd = parse_ticket_mail(ev("The stage of your ticket changed to Development Requsted", subject="T (#9004)"),
                            stage_ids={"Development Requsted": 12})
    assert (upd.kind, upd.stage, upd.stage_id) == ("stage", "Development Requsted", 12)
    assert parse_ticket_mail(ev("Stage: Solved", subject="T (#9004)")).kind == "closed"


def test_never_parses_non_auto_mail():
    # Principal's or a staff member's own mail is not a ticket notification, even with a ticket number in it.
    assert parse_ticket_mail(ev("about (#9001) ref: REQ-0150", frm="staff@example.org")) is None


def test_normalize_subject():
    assert normalize_subject("Re: RE: [TEST - please close] [Globex EG]  Fix X (#12)") == "fix x"


def test_readback_flow():
    pending = {"request_id": "REQ-0150", "part": None, "subject": "[Globex EG] Block Adding Lines", "message_id": ""}
    assert readback_step(READBACK_1, None) == "recheck"
    assert readback_step(READBACK_2, None) == "alert"
    ack = find_ack([ev("unrelated (#1)"), ev(ACK, subject="[Globex EG] Block Adding Lines (#9001)")], pending)
    assert ack.ticket_id == "9001" and readback_step(READBACK_1, ack) == "linked"
    cards = alert_cards(REQ, "[Globex EG] Block Adding Lines")
    assert [c.to for c in cards] == ["principal", "backup"]
    assert [b.action for b in cards[0].buttons[0]] == ["rs", "hb"] and cards[1].buttons == []


def test_resend_never_blind():
    assert resend_decision("maybe_sent", None, None) == "readback_first"
    assert resend_decision("sent", None, None) == "readback_first"
    late = tickets.TicketUpdate("REQ-0150", "9001", None, "ack", None, "")
    assert resend_decision("maybe_sent", late, True) == "linked"
    assert resend_decision("maybe_sent", None, False) == "resend_ok"
    assert resend_step("ticket-a", 1) == "ticket-a-r1"
    assert sent_query("<abc@x>") == "in:sent rfc822msgid:abc@x"


def test_link_back_text():
    assert tickets.link_back_text("https://helpdesk.example.com/my/ticket/9001") == \
        "Raised to Acme: https://helpdesk.example.com/my/ticket/9001. I will update you here."


def test_ticket_body_keeps_record_refs_and_source_link():
    part = dict(PART, body="Source: https://wiki.example.com/share/AbC123xYz789QwE456\n"
                           "Example: IN/00001/26 on WAVE/00001, stock.move 1")
    msg = build_ticket_email(REQ, PACKET, part, {}, sent_count=9)
    for keep in ("https://wiki.example.com/share/AbC123xYz789QwE456", "IN/00001/26", "WAVE/00001"):
        assert keep in msg.text
