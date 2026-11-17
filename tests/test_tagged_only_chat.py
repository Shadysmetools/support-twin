"""Acme Team (Principal 2026-11-11, "if anyone pings me there ... get me the request"): a chat with
tagged_only: true passes only messages that tag Principal (or his bot) or reply to him; holding: false sends no
automatic reply there (agents never message people in that group); auto_ticket: false opens no helpdesk ticket."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS

from twin_agent import timers
from twin_agent.bus import Topics
from twin_agent.channels.telegram_in import normalize_tg, raw_from_message
from twin_agent.config import chat_flag
from twin_agent.testing import fakes
from twin_agent.testing.fakes import PRINCIPAL_ID, GROUP_ID, TOPIC_LOGISTICS, make_event

TEAM_ID = -100777


def team_cfg(cfg):
    cfg["chats"].append({"name": "Acme Team", "chat_id": TEAM_ID, "kind": "group", "read": True,
                         "topics": [], "tagged_only": True, "tag_handles": ["@principaldoe", "@principaldoembot"],
                         "holding": False, "auto_ticket": False})
    return cfg


def raw(**kw):
    base = {"chat_id": TEAM_ID, "chat_title": "Acme Team", "topic_id": 3, "msg_id": 50, "sender_id": 3001,
            "sender_name": "Riley", "sender_is_bot": False, "out": False,
            "date": datetime(2026, 11, 11, 13, 0, tzinfo=timezone.utc), "text": "standup at 3", "media": [],
            "reply_to": None, "edited": False, "mentioned": False}
    base.update(kw)
    return base


def test_untagged_message_is_dropped(cfg):
    assert normalize_tg(raw(), team_cfg(cfg), PRINCIPAL_ID) is None


def test_telegram_mention_flag_passes(cfg):
    """Telethon sets `mentioned` for an @mention of Principal AND for a reply to one of his messages."""
    ev = normalize_tg(raw(mentioned=True), team_cfg(cfg), PRINCIPAL_ID)
    assert ev is not None and ev.chat == "Acme Team" and not ev.sender.is_principal


def test_handle_in_text_passes_case_insensitive(cfg):
    assert normalize_tg(raw(text="@PrincipaldoeMBot can you check #2629?"), team_cfg(cfg), PRINCIPAL_ID)
    assert normalize_tg(raw(text="hi @principaldoe"), team_cfg(cfg), PRINCIPAL_ID)


def test_handle_must_be_a_whole_handle(cfg):
    """'@principaldoex' is someone else; '@principaldoe' must not match inside it."""
    assert normalize_tg(raw(text="@principaldoex please"), team_cfg(cfg), PRINCIPAL_ID) is None


def test_principal_own_reply_passes_plain_message_does_not(cfg):
    c = team_cfg(cfg)
    assert normalize_tg(raw(out=True, sender_id=PRINCIPAL_ID, reply_to=49, text="done"), c, PRINCIPAL_ID) is not None
    assert normalize_tg(raw(out=True, sender_id=PRINCIPAL_ID, text="lunch?"), c, PRINCIPAL_ID) is None


def test_other_chats_unchanged(cfg):
    """Odoo x Globex has no tagged_only: every allowed message still passes."""
    r = raw(chat_id=GROUP_ID, topic_id=TOPIC_LOGISTICS, text="WAVE/00001")
    assert normalize_tg(r, team_cfg(cfg), PRINCIPAL_ID) is not None


def test_raw_from_message_reads_mentioned():
    msg = NS(chat_id=TEAM_ID, chat=NS(title="Acme Team"), is_private=False, reply_to=None, id=9, sender_id=1,
             sender=NS(first_name="Riley", last_name="", bot=False), out=False, date=None, message="hi",
             photo=None, action=None, mentioned=True)
    assert raw_from_message(msg)["mentioned"] is True


def test_chat_flag_defaults(cfg):
    c = team_cfg(cfg)
    assert chat_flag(c, TEAM_ID, "holding", True) is False
    assert chat_flag(c, GROUP_ID, "holding", True) is True          # not set = default
    assert chat_flag(c, -1, "holding", True) is True                # unknown chat = default


def test_no_holding_reply_in_team_chat(tmp_home, cfg, clock):
    from tests.test_listener import settle
    from twin_agent import paths, listener
    from twin_agent.bus import Bus
    from twin_agent.db import StateDB
    from twin_agent.flow import Flow
    team_cfg(cfg)
    db = StateDB(paths.state_db(), clock, cfg)
    bus = Bus()
    adapters = listener._dry_adapters()
    f = Flow(db, cfg, bus, clock, adapters, sender=listener._optional_sender(db, cfg, adapters, clock, bus),
             bot_ctl=listener._optional_bot_ctl(db, cfg, adapters, clock, bus))
    f.deadman = listener.DeadMan(cfg)
    f.spawner.retry_delay_s = 0
    f.wire()

    async def go():
        f.bus.publish(Topics.EVENT_IN, make_event("@principaldoe can you check #2629?", msg_id=60,
                                                  chat_id=TEAM_ID, topic_id=None, ts=clock.now()))
        await settle(f)
        rid = f.db.open_requests()[0]["id"]
        for _ in range(12):                                   # past any holding timer in the test config
            clock.advance(minutes=10)
            timers.tick(f.db, f.policy, clock.now(), f.bus)
            await settle(f)
        assert [t for t in db.timers_of(rid, "holding") if t["fired_at"]]     # the timer did fire
        assert db._all("SELECT * FROM events WHERE kind = 'holding_skipped' AND request_id = ?", (rid,))
        assert not db._all("SELECT * FROM sends WHERE request_id = ? AND kind = 'holding'", (rid,))
    try:
        asyncio.run(go())
    finally:
        db.close()


def test_no_auto_ticket_in_team_chat(cfg, clock, bus, tmp_home):
    """auto_ticket: false: a ping in Acme Team is internal, it never opens an Globex helpdesk ticket."""
    from tests.test_helpdesk import _desk, _live
    from twin_agent.testing.fakes import STAFF_A
    _live(cfg)
    team_cfg(cfg)
    cfg["helpdesk"].update(auto_create=True, auto_create_channels=["telegram_group", "telegram_dm"])
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    desk.wire()
    q = make_event("@principaldoe check this", msg_id=70, sender=STAFF_A, chat_id=TEAM_ID, topic_id=None,
                   ts=clock.now())
    rid = db.create_request(q, requester_id="2001", requester_name="Staff A", lane="L11_internal_diagnosis",
                            company="EG", title="team ping")
    db.save_packet(rid, __import__("tests.test_helpdesk", fromlist=["canned_packet"]).canned_packet(rid), False, [])
    asyncio.run(desk.on_triaged({"request_id": rid}))
    asyncio.run(desk.on_checks({"request_id": rid, "verified": False}))
    assert not [w for w in fake.writes if w["method"] == "create"]
    assert db._all("SELECT * FROM events WHERE kind = 'ticket_auto_skipped' AND request_id = ?", (rid,))


def _flow(tmp_home, cfg, clock):
    from twin_agent import paths, listener
    from twin_agent.bus import Bus
    from twin_agent.db import StateDB
    from twin_agent.flow import Flow
    db = StateDB(paths.state_db(), clock, cfg)
    bus = Bus()
    adapters = listener._dry_adapters()
    f = Flow(db, cfg, bus, clock, adapters, sender=listener._optional_sender(db, cfg, adapters, clock, bus),
             bot_ctl=listener._optional_bot_ctl(db, cfg, adapters, clock, bus))
    f.deadman = listener.DeadMan(cfg)
    f.spawner.retry_delay_s = 0
    f.wire()
    return f, db


def test_bot_reply_in_team_chat_is_an_info_card(tmp_home, cfg, clock):
    """Principal 2026-11-11: 'when sysadmin's bot replies you will get back to me': a bot message that passed the
    tagged_only filter (it tags his bot) becomes an info card; no request, no reply, no ticket."""
    from tests.test_listener import settle
    from twin_agent.models import Sender
    team_cfg(cfg)
    f, db = _flow(tmp_home, cfg, clock)
    cards = []
    f.bus.subscribe(Topics.CARD_OUT, lambda c: cards.append(c))
    bot = Sender(tg_id=4242, name="Sysadmin's Agents", is_bot=True)

    async def go():
        f.bus.publish(Topics.EVENT_IN, make_event("@principaldoembot [pm-manager] -> Principal: d5 now on 41661077",
                                                  msg_id=3300, sender=bot, chat_id=TEAM_ID, topic_id=20,
                                                  ts=clock.now()))
        await settle(f)
    try:
        asyncio.run(go())
        info = [c for c in cards if str(c.kind) == "info" and "d5 now on 41661077" in c.text]
        assert len(info) == 1 and "Sysadmin's Agents" in info[0].text and not info[0].buttons
        assert not db.open_requests()
    finally:
        db.close()


def test_bot_message_in_globex_group_still_silent(tmp_home, cfg, clock):
    from tests.test_listener import settle
    from twin_agent.models import Sender
    team_cfg(cfg)
    f, db = _flow(tmp_home, cfg, clock)
    cards = []
    f.bus.subscribe(Topics.CARD_OUT, lambda c: cards.append(c))

    async def go():
        f.bus.publish(Topics.EVENT_IN, make_event("order synced", msg_id=3301,
                                                  sender=Sender(tg_id=4243, name="Flow Bot", is_bot=True),
                                                  ts=clock.now()))
        await settle(f)
    try:
        asyncio.run(go())
        assert not cards and not db.open_requests()
    finally:
        db.close()


# ---- 11 Nov 2026, evening: the real Acme Team messages to Principal that the filter above dropped ------------
BOT_ID = 9000000004


def team_cfg_v2(cfg):
    team_cfg(cfg)
    cfg["chats"][-1].update(tag_user_ids=[BOT_ID], tag_names=["Principal", "اليكس", "أليكس"])
    return cfg


def test_reply_to_his_bot_passes_without_a_handle(cfg):
    """#3295: Sysadmin's pm-manager bot answered the message Principal's bot posted (#3292) without writing
    @principaldoembot. A reply to a message from Principal's bot is for Principal."""
    r = raw(sender_id=7001, sender_name="Sysadmin's Bot", sender_is_bot=True, reply_to=3292, text="[pm-manager] done",
            reply_to_sender_id=BOT_ID)
    assert normalize_tg(r, team_cfg_v2(cfg), PRINCIPAL_ID) is not None


def test_reply_to_someone_else_still_dropped(cfg):
    r = raw(reply_to=3290, text="ok", reply_to_sender_id=5555)
    assert normalize_tg(r, team_cfg_v2(cfg), PRINCIPAL_ID) is None


def test_his_name_as_a_word_passes(cfg):
    """#3296 '@Principal about the d5 link' (@Principal is NOT his username), #3299 'Principal, I'll handle', #3312 'Principal,
    Dispatch-5 is fixed'. His first name as a whole word, any case, Arabic too."""
    c = team_cfg_v2(cfg)
    for text in ("@Principal about the d5 link", "Principal, I'll handle the fix", "thanks principal", "يا اليكس شوف ده"):
        assert normalize_tg(raw(text=text), c, PRINCIPAL_ID) is not None, text


def test_name_inside_another_word_is_dropped(cfg):
    c = team_cfg_v2(cfg)
    for text in ("Springfield trip", "PrincipaldoeX", "@principal_bot please"):
        assert normalize_tg(raw(text=text), c, PRINCIPAL_ID) is None, text


def test_without_tag_names_a_name_is_not_a_tag(cfg):
    """Only the chats that list tag_names match names (no change for a chat that has none)."""
    assert normalize_tg(raw(text="Principal, check this"), team_cfg(cfg), PRINCIPAL_ID) is None


def test_adapter_looks_up_who_a_reply_answers_only_when_needed():
    """The live adapter asks Telegram for the replied-to message only in a tagged_only chat, only for a reply
    that is not already tagged; it fills reply_to_sender_id."""
    from twin_agent.channels.telegram_in import fill_reply_sender
    calls = []

    class Msg:
        def __init__(self, reply_to):
            self.reply_to_id = reply_to

        async def get_reply_message(self):
            calls.append(1)
            return NS(sender_id=BOT_ID)

    chat = {"tagged_only": True, "tag_user_ids": [BOT_ID]}
    r = raw(reply_to=3292, text="[pm-manager] done")
    asyncio.run(fill_reply_sender(Msg(3292), r, chat))
    assert r["reply_to_sender_id"] == BOT_ID and calls == [1]
    r2 = raw(reply_to=None, text="hi")
    asyncio.run(fill_reply_sender(Msg(None), r2, chat))                 # not a reply: no lookup
    r3 = raw(reply_to=3292, text="x", mentioned=True)
    asyncio.run(fill_reply_sender(Msg(3292), r3, chat))                 # already tagged: no lookup
    r4 = raw(reply_to=3292, text="x")
    asyncio.run(fill_reply_sender(Msg(3292), r4, {"tagged_only": False}))   # other chats: no lookup
    assert calls == [1]


def test_wire_received_ack_from_a_bot_is_not_a_card(tmp_home, cfg, clock):
    """#3295 '[pm-manager] @executiveagentsbot Received' is agent_wire's automatic ack, not a message for Principal."""
    from tests.test_listener import settle
    from twin_agent.models import Sender
    team_cfg(cfg)
    f, db = _flow(tmp_home, cfg, clock)
    cards = []
    f.bus.subscribe(Topics.CARD_OUT, lambda c: cards.append(c))
    bot = Sender(tg_id=4242, name="Sysadmin's Bot", is_bot=True)

    async def go():
        for i, text in enumerate(("[pm-manager] @executiveagentsbot Received", "[pm-manager] Relayed")):
            f.bus.publish(Topics.EVENT_IN, make_event(text, msg_id=3400 + i, sender=bot, chat_id=TEAM_ID,
                                                      topic_id=20, ts=clock.now(), reply_to=3292))
        await settle(f)
    try:
        asyncio.run(go())
        assert not [c for c in cards if "Bot message for you" in c.text]
    finally:
        db.close()


def test_his_seat_name_passes(cfg):
    """#3293: Sysadmin's bot wrote '@executiveagentsbot [pm-manager] → digital-twin' (his seat, Executive's bot handle)."""
    c = team_cfg_v2(cfg)
    c["chats"][-1]["tag_names"].append("digital-twin")
    r = raw(sender_is_bot=True, text="@executiveagentsbot [pm-manager] → digital-twin\nd5 status")
    assert normalize_tg(r, c, PRINCIPAL_ID) is not None
    assert normalize_tg(raw(text="digital-twin-old is gone"), c, PRINCIPAL_ID) is None


def test_his_own_bot_post_is_not_a_card(tmp_home, cfg, clock):
    """#3292: Principal's own bot (tag_user_ids) asked Sysadmin; its own post is not a message FOR Principal."""
    from tests.test_listener import settle
    from twin_agent.models import Sender
    team_cfg_v2(cfg)
    f, db = _flow(tmp_home, cfg, clock)
    cards = []
    f.bus.subscribe(Topics.CARD_OUT, lambda c: cards.append(c))

    async def go():
        f.bus.publish(Topics.EVENT_IN, make_event("@sysadmins_agents_bot [digital-twin] -> Sysadmin: two questions",
                                                  msg_id=3292, sender=Sender(tg_id=BOT_ID, name="Digital Twin",
                                                                             is_bot=True),
                                                  chat_id=TEAM_ID, topic_id=20, ts=clock.now()))
        await settle(f)
    try:
        asyncio.run(go())
        assert not [c for c in cards if "Bot message for you" in c.text]
    finally:
        db.close()
