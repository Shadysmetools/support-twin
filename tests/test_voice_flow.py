"""Voice card end to end on fakes: draft -> voice card (text + preview path) -> `as` / `rr` / `nv` taps -> sender."""
import asyncio
import copy

import pytest
from voice_helpers import SHORT_EN, make_maker, voice_on

from twin_agent import listener, paths
from twin_agent.bus import Bus
from twin_agent.db import StateDB
from twin_agent.flow import Flow
from twin_agent.models import CardKind, State
from twin_agent.testing import fakes
from twin_agent.testing.voice_fakes import FakeTelegramVoice
from twin_agent.voice.desk import REC_PREFIX, VoiceDesk, build_voice_desk


def run(coro):
    return asyncio.run(coro)


def live_cfg(cfg):
    c = voice_on(copy.deepcopy(cfg))
    c["run_mode"], c["send_enabled"] = "live", True
    c["consent"]["channels"]["telegram"] = True
    c["consent"]["notice_gate"] = {"sysadmin_b1_ref": "B1-test", "executive_b2_ref": "B2-test"}
    c["dial"]["L3_t1_answer"] = "draft"
    c["people"]["principal_tg_user_id"] = fakes.PRINCIPAL_ID
    return c


@pytest.fixture
def vflow(tmp_home, cfg, clock):
    c = live_cfg(cfg)
    db = StateDB(paths.state_db(), clock, c)
    bus = Bus()
    adapters = listener._dry_adapters()
    tg = FakeTelegramVoice()
    adapters.tg_in = adapters.tg_send = tg
    sender = listener._optional_sender(db, c, adapters, clock, bus)
    bot_ctl = listener._optional_bot_ctl(db, c, adapters, clock, bus)
    f = Flow(db, c, bus, clock, adapters, sender=sender, bot_ctl=bot_ctl)
    f.wire()
    maker, tts, _ = make_maker(c, db, clock, tmp_home / "data" / "voice")
    f.voice = VoiceDesk(db, c, clock, maker, f.post_card, lambda rid, text: f.card(CardKind.info, text, rid))
    f.tts = tts
    yield f
    db.close()


async def settle(f):
    for _ in range(10):
        await f.bus.drain()
        if f.tasks:
            await asyncio.gather(*list(f.tasks), return_exceptions=True)
        else:
            break
    await f.bus.drain()


def setup_request(f, clock, text=SHORT_EN):
    ev = fakes.make_event("cannot validate the transfer", msg_id=5, ts=clock.now())
    rid = f.db.create_request(ev)
    f.db.add_message(ev, rid)
    for st in (State.TRIAGED, State.DIAGNOSED):
        f.db.transition(rid, st, actor="test")
    packet = fakes.canned_packet(rid)
    packet["draft_reply"] = {"channel": "telegram_group", "lang": "en", "text": text}
    packet["tier"] = "T1"
    f.db.save_packet(rid, packet, True, [])
    clock.advance(minutes=1)
    return rid, packet


def press_data(f, rid, action, nth=-1):
    """Callback data of the `action` button on the nth card of this request."""
    found = [d for _, card, datas in f.adapters.bot.cards if card.request_id == rid
             for row in datas for d in row if d.split("|")[1] == action]
    return found[nth]


async def tap(f, data):
    await f.bot_ctl.on_callback({"user_id": fakes.PRINCIPAL_ID, "data": data, "callback_id": "cb"})
    await settle(f)


def test_draft_gets_voice_card_and_as_sends_that_recording(vflow, clock):
    f = vflow

    async def go():
        rid, packet = setup_request(f, clock)
        await f._draft_card(rid, packet, True, [])
        assert f.tts.calls == [] and f.tasks               # synthesis runs as a task, never inside the bus handler
        await settle(f)
        voice_cards = [c for _, c, _ in f.adapters.bot.cards if c.attachments]
        assert len(voice_cards) == 1
        rec = f.voice.current(rid)
        assert SHORT_EN in voice_cards[0].text and rec["path"] in voice_cards[0].text
        await tap(f, press_data(f, rid, "as"))
        tg = f.adapters.tg_send
        assert len(tg.voice_sent) == 1 and tg.sent == [], f.db.events(rid, "send")
        assert tg.voice_sent[0]["path"] == rec["path"]
    run(go())


def test_old_card_after_rerecord_sends_nothing(vflow, clock):
    f = vflow

    async def go():
        rid, packet = setup_request(f, clock)
        await f._draft_card(rid, packet, True, [])
        await settle(f)
        first = f.voice.current(rid)
        old_as = press_data(f, rid, "as")
        clock.advance(minutes=1)
        await tap(f, press_data(f, rid, "rr"))
        second = f.voice.current(rid)
        assert second["sha256"] != first["sha256"] and len(f.tts.calls) == 2
        await tap(f, old_as)                                   # Principal taps the OLD card
        tg = f.adapters.tg_send
        assert tg.voice_sent == [] and tg.sent == []
        infos = [c.text for _, c, _ in f.adapters.bot.cards if c.kind == CardKind.info]
        assert any("voice note not sent" in t for t in infos)
        await tap(f, press_data(f, rid, "as"))                 # the new card sends the new take
        assert len(tg.voice_sent) == 1 and tg.voice_sent[0]["path"] == second["path"]
    run(go())


def test_nv_sends_text_without_voice(vflow, clock):
    f = vflow

    async def go():
        rid, packet = setup_request(f, clock)
        await f._draft_card(rid, packet, True, [])
        await settle(f)
        await tap(f, press_data(f, rid, "nv"))
        tg = f.adapters.tg_send
        assert len(tg.sent) == 1 and tg.sent[0]["text"] == SHORT_EN and tg.voice_sent == []
    run(go())


def test_numbers_heavy_draft_gets_no_voice_card(vflow, clock):
    f = vflow

    async def go():
        rid, packet = setup_request(f, clock, text="Moved 1250 units from bin 3340 to 4410.")
        await f._draft_card(rid, packet, True, [])
        await settle(f)
        assert not [c for _, c, _ in f.adapters.bot.cards if c.attachments] and f.tts.calls == []
        assert f.db.events(rid, "voice_skipped")
    run(go())


def test_no_voice_desk_in_dry_run_or_without_consent(tmp_home, cfg, clock):
    db = StateDB(paths.state_db(), clock, cfg)
    try:
        desk, why = build_voice_desk(cfg, db, clock, None, None, has_secret=lambda n: True)
        assert desk is None and "voice off" in why
        c = live_cfg(cfg)
        desk, why = build_voice_desk(c, db, clock, None, None, has_secret=lambda n: True, which=lambda n: None)
        assert desk is None and "ffmpeg" in why
        desk, why = build_voice_desk(c, db, clock, None, None, has_secret=lambda n: False, which=lambda n: "ffmpeg")
        assert desk is None and "refused" in why
        assert db.get_cursor(REC_PREFIX + "REQ-0001") is None
    finally:
        db.close()


def test_listener_has_no_voice_by_default(tmp_home, cfg, clock):
    db = StateDB(paths.state_db(), clock, cfg)
    try:
        f = Flow(db, cfg, Bus(), clock, listener._dry_adapters())
        assert listener._optional_voice(f) is None
    finally:
        db.close()
