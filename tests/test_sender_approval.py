"""'send it' from Principal = approval (HANDOFF correction #11). A direct owner command ("send"/"send it") satisfies
the send gate the SAME as a board Approve tap: both are Principal, on his own account, authorising this exact send.
Pure decision functions (no live Telegram), tested here; the live wiring note lives in sender_approval.py."""
from datetime import timedelta

from twin_agent.models import Channel, OutboundMessage, OutKind, SendTrigger
from twin_agent.sender_approval import approval_satisfied, is_owner_send_command, text_hash
from twin_agent.testing import fakes
from tests.sender_fakedb import FakeStateDB

PRINCIPAL, BACKUP, STRANGER = fakes.PRINCIPAL_ID, fakes.BACKUP_ID, 999999
CFG = {"people": {"principal_tg_user_id": PRINCIPAL}}
TEXT = "Yes, it is in stock - ships today."


def _msg():
    return OutboundMessage(request_id="REQ-0001", step="draft-1", kind=OutKind.answer,
                           channel=Channel.telegram_group, text=TEXT, draft_hash=text_hash(TEXT))


# ---------------------------------------------------------------- the owner command itself

def test_owner_send_command_recognised_only_from_principal_and_only_as_a_bare_command():
    assert is_owner_send_command("send it", PRINCIPAL, CFG) is True
    assert is_owner_send_command("Send", PRINCIPAL, CFG) is True              # case-insensitive, "send" alone counts
    assert is_owner_send_command("  send it  ", PRINCIPAL, CFG) is True       # trimmed
    assert is_owner_send_command("send it", BACKUP, CFG) is False         # a backup can NEVER approve as Principal
    assert is_owner_send_command("send it", STRANGER, CFG) is False       # unknown id is nobody
    assert is_owner_send_command("send it", 0, CFG) is False
    assert is_owner_send_command("send the PO to the client", PRINCIPAL, CFG) is False   # not a bare command
    assert is_owner_send_command("send it?", PRINCIPAL, CFG) is False         # a question is not a command


# ---------------------------------------------------------------- the gate decision

def test_owner_command_satisfies_the_gate_without_a_nonce_or_db(clock):
    # Principal typed "send it": approved, the same as a tap. The board-approve path (db) is never even reached.
    ok, why = approval_satisfied(None, CFG, _msg(), SendTrigger.approval,
                                 owner_command="send it", owner_user_id=PRINCIPAL)
    assert ok is True and "owner" in why.lower()


def test_backup_send_command_does_not_satisfy_the_gate(clock):
    db = FakeStateDB(clock)
    ok, why = approval_satisfied(db, CFG, _msg(), SendTrigger.approval, now=clock.now(),
                                 owner_command="send it", owner_user_id=BACKUP)
    assert ok is False                                                    # falls through to the board path and finds nothing


def test_board_approve_tap_still_satisfies_the_gate(clock):
    db = FakeStateDB(clock)
    nonce = db.issue_approval("REQ-0001", 1, "ap", PRINCIPAL, text_hash(TEXT), clock.now() + timedelta(hours=24))
    ok, why = approval_satisfied(db, CFG, _msg(), SendTrigger.approval,
                                 nonce=nonce, user_id=PRINCIPAL, now=clock.now())
    assert ok is True and why == ""


def test_no_command_and_no_valid_approval_is_blocked(clock):
    db = FakeStateDB(clock)
    ok, why = approval_satisfied(db, CFG, _msg(), SendTrigger.approval, nonce=None, user_id=PRINCIPAL, now=clock.now())
    assert ok is False and why                                            # fail-closed with a reason
