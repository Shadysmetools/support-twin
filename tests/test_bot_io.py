"""bot_io + BotController: one-time codes, Principal-only send buttons (red-team #3), plain reply = instruction and
only ForceReply Edit text replaces a draft (red-team #1), /stop /away /status, card cap, quiet hours."""
import asyncio
import secrets
from datetime import datetime

import pytest

from twin_agent.bus import Bus, Topics
from twin_agent.channels.bot_controller import BotController
from twin_agent.channels.bot_io import (build_card, make_callback, parse_callback, parse_duration, render_card,
                                         strip_for_backup)
from twin_agent.models import Button, Card, CardKind, parse_ts
from twin_agent.testing.fakes import PRINCIPAL_ID, BACKUP_ID, CAIRO, FakeBot, FakeClock

STRANGER = 4444


class MiniDB:
    """Just the StateDB methods the controller uses (CONTRACT section 8 semantics)."""

    def __init__(self, clock):
        self.clock, self.cards, self.approvals, self.events, self.cursors = clock, [], {}, [], {}

    def add_card(self, card):
        self.cards.append(card)
        return len(self.cards)

    def issue_approval(self, request_id, card_id, action, allowed_user_id, draft_hash, expires_at):
        nonce = secrets.token_urlsafe(8)
        self.approvals[nonce] = dict(nonce=nonce, request_id=request_id, card_id=card_id, action=action,
                                     allowed_user_id=allowed_user_id, draft_hash=draft_hash,
                                     expires_at=expires_at, used_at=None)
        return nonce

    def use_approval(self, nonce, user_id, action, request_id):
        row = self.approvals.get(nonce)
        if not row or row["used_at"] or row["action"] != action or row["request_id"] != request_id \
                or row["allowed_user_id"] != user_id or self.clock.now() >= row["expires_at"]:
            return None
        row["used_at"] = self.clock.now()
        return row

    def log_event(self, kind, request_id=None, actor="system", **detail):
        self.events.append((kind, request_id, actor, detail))

    def open_requests(self):
        return [{"id": "REQ-0001"}]

    def set_cursor(self, source, value):
        self.cursors[source] = value


@pytest.fixture
def rig(cfg, tmp_path):
    clock, bot, bus = FakeClock(), FakeBot(), Bus()
    db = MiniDB(clock)
    ctl = BotController(bot, db, cfg, bus, clock, stop_file=lambda: tmp_path / "STOP")
    asyncio.run(ctl.start())
    return ctl, bot, bus, db, clock, tmp_path


def draft_card(to="principal"):
    return Card(kind=CardKind.new_request, request_id="REQ-0142", to=to, text="Asked: WAVE/00001 will not validate",
                buttons=[[Button("Approve reply", "ap"), Button("Edit", "ed"), Button("Take over", "to")]])


def run(coro):
    return asyncio.run(coro)


def published(bus, topic):
    return [p for t, p in bus.published if t == topic]


# ------------------------------------------------------------ pure helpers

def test_callback_roundtrip_and_limits():
    data = make_callback("REQ-0142", "ap", "Xk3_9aQ1bZc")
    assert data == "REQ-0142|ap|Xk3_9aQ1bZc" and parse_callback(data) == ("REQ-0142", "ap", "Xk3_9aQ1bZc")
    for bad in ["REQ-0142|ap", "REQ-0142|zz|Xk3_9aQ1bZc", "x|ap|Xk3_9aQ1bZc", "REQ-1|ap|" + "a" * 60, ""]:
        with pytest.raises(ValueError):
            parse_callback(bad)


def test_render_card_strips_ticket_token_and_secrets():
    card = build_card(CardKind.ticket, "REQ-0150", headline="Needs Acme", need="tap Create ticket",
                      ticket_url="https://helpdesk.example.com/my/ticket/123/abcdef0123456789abcdef")
    text = render_card(card)
    assert text.startswith("REQ-0150") and "my/ticket/123" in text and "abcdef0123456789" not in text
    assert "What I need from you: tap Create ticket" in text
    assert "kc_abcdef" not in render_card(Card(kind=CardKind.info, text="key kc_abcdefabcdefabcdefabcdef1234"))
    assert len(render_card(Card(kind=CardKind.info, text="word " * 2000))) <= 3500


def test_backup_card_loses_send_buttons():
    c = strip_for_backup(draft_card("backup"))
    assert [b.action for row in c.buttons for b in row] == ["to"]


def test_parse_duration():
    assert parse_duration("3h").total_seconds() == 3 * 3600 and parse_duration("90m").total_seconds() == 5400
    assert parse_duration("") is None and parse_duration("48h") is None and parse_duration("soon") is None


# ------------------------------------------------------------ approvals

def test_principal_approves_once(rig):
    ctl, bot, bus, db, clock, _ = rig
    run(ctl.post_card(draft_card(), draft_hash="h1"))
    ap = bot.cards[-1][2][0][0]
    assert ap.startswith("REQ-0142|ap|") and len(ap.encode()) <= 64
    run(bot.press(ap, user_id=PRINCIPAL_ID))
    run(bot.press(ap, user_id=PRINCIPAL_ID, callback_id="cb2"))          # second tap: used
    cbs = published(bus, Topics.CALLBACK_IN)
    assert len(cbs) == 1 and cbs[0]["action"] == "ap" and cbs[0]["request_id"] == "REQ-0142"
    assert bot.answered[-1] == ("cb2", "This button expired or was already used.")
    assert db.approvals[cbs[0]["nonce"]]["draft_hash"] == "h1"


def test_backup_cannot_approve_but_can_take_over(rig):
    ctl, bot, bus, db, clock, _ = rig
    run(ctl.post_card(draft_card()))
    ap, _, to = bot.cards[-1][2][0]
    run(bot.press(ap, user_id=BACKUP_ID))
    assert published(bus, Topics.CALLBACK_IN) == [] and "Only Principal" in bot.answered[-1][1]
    run(bot.press(to, user_id=BACKUP_ID))           # Principal's take-over code is bound to Principal's id
    assert published(bus, Topics.CALLBACK_IN) == []
    run(ctl.post_card(draft_card("backup")))        # the backup's own card: only Take over, bound to the backup
    assert len(bot.cards[-1][2]) == 1 and len(bot.cards[-1][2][0]) == 1
    run(bot.press(bot.cards[-1][2][0][0], user_id=BACKUP_ID))
    assert published(bus, Topics.CALLBACK_IN)[0]["action"] == "to"


def test_codes_expire_after_24h_and_strangers_are_ignored(rig):
    ctl, bot, bus, db, clock, _ = rig
    run(ctl.post_card(draft_card()))
    ap = bot.cards[-1][2][0][0]
    run(bot.press(ap, user_id=STRANGER))
    assert bot.answered == []                       # silence for strangers
    clock.advance(hours=24, minutes=1)
    run(bot.press(ap, user_id=PRINCIPAL_ID))
    assert published(bus, Topics.CALLBACK_IN) == [] and "expired" in bot.answered[-1][1]
    run(bot.press("REQ-0142|ap|forgedNonce1", user_id=PRINCIPAL_ID))
    assert published(bus, Topics.CALLBACK_IN) == []


# ------------------------------------------------------------ replies: instruction vs Edit

def test_plain_reply_is_instruction_never_text(rig):
    ctl, bot, bus, db, clock, _ = rig
    msg_id = run(ctl.post_card(draft_card()))
    run(bot.reply("ask him for the batch number", to_bot_msg_id=msg_id, force_reply=True))   # flag not trusted
    r = published(bus, Topics.REPLY_IN)
    assert r == [{"user_id": PRINCIPAL_ID, "request_id": "REQ-0142", "text": "ask him for the batch number",
                  "force_reply": False}]
    assert not any(c.kind == CardKind.edit_echo for _, c, _ in bot.cards)


def test_edit_force_reply_is_echoed_with_send_cancel(rig):
    ctl, bot, bus, db, clock, _ = rig
    run(ctl.post_card(draft_card()))
    ed = bot.cards[-1][2][0][1]
    run(bot.press(ed, user_id=PRINCIPAL_ID))
    prompt_id, _, rid = bot.force_replies[-1]
    assert rid == "REQ-0142"
    run(bot.reply("Send the batch number please", to_bot_msg_id=prompt_id))
    r = published(bus, Topics.REPLY_IN)[-1]
    assert r["force_reply"] is True and r["text"] == "Send the batch number please"
    _, echo, datas = bot.cards[-1]
    assert echo.kind == CardKind.edit_echo and "Send the batch number please" in echo.text
    assert [d.split("|")[1] for d in datas[0]] == ["sd", "cx"]
    run(bot.reply("other text", to_bot_msg_id=prompt_id, user_id=BACKUP_ID))   # backup: never an edit
    assert published(bus, Topics.REPLY_IN)[-1]["force_reply"] is False


# ------------------------------------------------------------ commands

def test_stop_writes_kill_file_and_resume_is_principal_only(rig):
    ctl, bot, bus, db, clock, home = rig
    run(bot.command("/stop", user_id=BACKUP_ID))
    assert (home / "STOP").exists() and published(bus, Topics.KILL_CHANGED) == [{"on": True, "by": BACKUP_ID}]
    run(bot.command("/resume", user_id=BACKUP_ID))
    assert (home / "STOP").exists() and "Only Principal" in bot.cards[-1][1].text
    run(bot.command("/resume", user_id=PRINCIPAL_ID))
    assert not (home / "STOP").exists() and published(bus, Topics.KILL_CHANGED)[-1]["on"] is False
    run(bot.command("/stop", user_id=STRANGER))
    assert not (home / "STOP").exists()


def test_away_and_status(rig):
    ctl, bot, bus, db, clock, _ = rig
    run(bot.command("/away 3h"))
    assert parse_ts(db.cursors["away_until"]) == datetime(2026, 11, 1, 17, 40, tzinfo=CAIRO)
    assert published(bus, Topics.COMMAND_IN)[-1] == {"user_id": PRINCIPAL_ID, "command": "away", "args": "3h"}
    run(bot.command("/away forever"))
    assert published(bus, Topics.COMMAND_IN)[-1]["args"] == "3h"          # bad input not passed on
    run(bot.command("/status"))
    assert "Open requests: 1" in bot.cards[-1][1].text and "Kill switch: off" in bot.cards[-1][1].text


# ------------------------------------------------------------ card cap, quiet hours

def test_cap_10_per_hour_then_digest(rig):
    ctl, bot, bus, db, clock, _ = rig
    for i in range(12):
        run(ctl.post_card(Card(kind=CardKind.reminder, text=f"reminder {i}", request_id=f"REQ-{i:04d}")))
    assert len(bot.cards) == 10 and len(ctl.held) == 2
    run(ctl.post_card(Card(kind=CardKind.holding_blocked, text="holding reply blocked")))   # urgent: goes now
    assert bot.cards[-1][1].kind == CardKind.holding_blocked
    clock.advance(hours=1)
    run(ctl.post_card(Card(kind=CardKind.reminder, text="later")))
    kinds = [c.kind for _, c, _ in bot.cards[-2:]]
    assert kinds == [CardKind.digest, CardKind.reminder] and "2 more updates" in bot.cards[-2][1].text


def test_quiet_hours_silent(rig):
    ctl, bot, bus, db, clock, _ = rig
    clock.set(datetime(2026, 11, 1, 23, 0, tzinfo=CAIRO))
    run(ctl.post_card(Card(kind=CardKind.reminder, text="r")))
    run(ctl.post_card(Card(kind=CardKind.holding_blocked, text="blocked")))
    assert bot.cards[0][1].silent is True and bot.cards[1][1].silent is False


def test_unset_principal_id_hears_nobody(cfg, tmp_path):
    cfg["people"]["principal_tg_user_id"] = 0
    bot, bus, clock = FakeBot(), Bus(), FakeClock()
    ctl = BotController(bot, MiniDB(clock), cfg, bus, clock, stop_file=lambda: tmp_path / "STOP")
    run(ctl.start())
    run(bot.command("/stop", user_id=0))
    assert not (tmp_path / "STOP").exists() and run(ctl.post_card(draft_card())) is None


# ------------------------------------------------------------ joins with other components

def test_card_out_on_the_bus_is_sent(rig):
    ctl, bot, bus, db, clock, _ = rig
    bus.publish(Topics.CARD_OUT, Card(kind=CardKind.health, text="Gmail token expires tomorrow"))
    bus.publish(Topics.CARD_OUT, {"card": draft_card(), "draft_hash": "h9"})
    run(bus.drain())
    assert [c.kind for _, c, _ in bot.cards] == [CardKind.health, CardKind.new_request]
    assert {r["draft_hash"] for r in db.approvals.values()} == {"h9"}


def test_capped_approval_cards_keep_their_buttons(rig):
    ctl, bot, bus, db, clock, _ = rig
    for i in range(10):
        run(ctl.post_card(Card(kind=CardKind.reminder, text=f"r{i}")))
    run(ctl.post_card(draft_card(), draft_hash="h1"))             # over the cap: held, not folded away
    assert len(bot.cards) == 10 and len(ctl.held) == 1
    clock.advance(hours=1)
    run(ctl.flush_digest())                                       # the listener's tick
    _, card, datas = bot.cards[-1]
    assert card.kind == CardKind.new_request and datas[0][0].startswith("REQ-0142|ap|")


def test_render_keeps_record_refs():
    text = render_card(Card(kind=CardKind.new_request, request_id="REQ-0142", text=(
        "REQ-0142  Odoo x Globex / Logistics  14:40  (waited 3 min)\nAsked: WAVE/06733 will not validate.\n"
        "- IN/12306/26 has 1 line with demand 0\n- Fix: delete stock.move 5608784\nsha256: 3f9a...c21")))
    for ref in ("REQ-0142", "WAVE/06733", "IN/12306/26", "stock.move 5608784", "14:40"):
        assert ref in text


def test_hash_matches_guards_and_sender():
    from twin_agent.channels.bot_io import text_hash
    from twin_agent.checks.fixgate import draft_hash
    from twin_agent.sender_approval import text_hash as sender_hash
    assert text_hash("Checking it, 10 min.") == draft_hash("Checking it, 10 min.") == sender_hash("Checking it, 10 min.")


def test_end_to_end_with_real_statedb_and_sender_approval(cfg, tmp_path):
    """Principal taps Approve (the bot consumes the code); the sender's approval binding still accepts it once,
    and a backup's tap on the same kind of button never authorises a send."""
    from twin_agent.channels.bot_io import text_hash
    from twin_agent.db import StateDB
    from twin_agent.models import Channel, OutKind, OutboundMessage, SendTrigger
    from twin_agent.sender_approval import check_approval
    clock, bot, bus = FakeClock(), FakeBot(), Bus()
    db = StateDB(tmp_path / "state.db", clock)
    ctl = BotController(bot, db, cfg, bus, clock, stop_file=lambda: tmp_path / "STOP")
    run(ctl.start())
    text = "Checking it, 10 min."
    run(ctl.post_card(draft_card(), draft_hash=text_hash(text)))
    ap = bot.cards[-1][2][0][0]
    run(bot.press(ap, user_id=PRINCIPAL_ID))
    nonce = published(bus, Topics.CALLBACK_IN)[-1]["nonce"]
    msg = OutboundMessage(request_id="REQ-0142", step="draft-1", kind=OutKind.holding, channel=Channel.telegram_group,
                          text=text, draft_hash=text_hash(text))
    row, why = check_approval(db, cfg, msg, SendTrigger.approval, nonce, PRINCIPAL_ID, clock.now())
    assert row is not None, why
    row2, _ = check_approval(db, cfg, msg, SendTrigger.approval, nonce, BACKUP_ID, clock.now())
    assert row2 is None
    db.close()
