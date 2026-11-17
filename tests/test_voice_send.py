"""Voice notes through the SAME sender path: kill switch, dial, consent D2, approval, live re-read, binding."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from sender_fakedb import FakeStateDB
from test_sender import ANSWER, answer_msg, cards, guard_ok, hold_msg, make_sender  # noqa: F401 (fixture)
from test_telegram_send import FakeClient
from voice_helpers import make_maker, voice_on

from twin_agent import paths
from twin_agent.channels.telegram_send import TelethonSend, TgSendRefused
from twin_agent.models import CardKind, Dial, Lane, SendStatus, SendTrigger
from twin_agent.sender import text_hash
from twin_agent.testing import fakes
from twin_agent.testing.voice_fakes import FakeTelegramVoice


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def tgv():
    return FakeTelegramVoice()


def voiced(msg, cfg, clock, tmp_home, *, text=None):
    """Record a voice note of msg.text and attach it (what VoiceDesk.attach does after an `as` tap)."""
    maker, _, _ = make_maker(cfg, FakeStateDB(clock), clock, tmp_home / "data" / "voice")
    note = maker.make(text or msg.text, lang="en", request_id=msg.request_id)
    msg.voice_path, msg.voice_sha256, msg.voice_lang = str(note.path), note.sha256, "en"
    return msg


def approve(db, msg, action="as"):
    return db.issue_approval(msg.request_id, 1, action, fakes.PRINCIPAL_ID, msg.draft_hash,
                             db.clock.now().replace(year=2027))


def test_approved_voice_note_goes_out_as_voice_with_disclosure(cfg, clock, bus, tgv, tmp_home, guard_ok):
    on = voice_on(cfg)
    s, db = make_sender(on, clock, bus, tgv, tmp_home, dial=Dial.draft)
    msg = voiced(answer_msg(clock), on, clock, tmp_home)
    r = run(s.send(msg, SendTrigger.approval, approval_nonce=approve(db, msg), approver_user_id=fakes.PRINCIPAL_ID))
    assert r.status == SendStatus.sent, r
    assert tgv.sent == [] and len(tgv.voice_sent) == 1                       # a voice note, not text
    v = tgv.voice_sent[0]
    assert v["caption"] == "AI voice of Principal's assistant" and v["reply_to"] == 5
    assert v["topic_id"] == fakes.TOPIC_LOGISTICS and v["path"] == msg.voice_path
    audit = (tmp_home / "data" / "logs" / "actions.jsonl").read_text(encoding="utf-8")
    assert msg.voice_sha256 in audit and ANSWER not in audit
    assert guard_ok and guard_ok[0].thread_after_draft == []              # live re-read happened, guards ran


def test_kill_switch_blocks_voice(cfg, clock, bus, tgv, tmp_home, guard_ok):
    on = voice_on(cfg)
    s, db = make_sender(on, clock, bus, tgv, tmp_home, dial=Dial.draft)
    paths.stop_file().write_text("stop", encoding="utf-8")
    msg = voiced(answer_msg(clock), on, clock, tmp_home)
    r = run(s.send(msg, SendTrigger.approval, approval_nonce=approve(db, msg), approver_user_id=fakes.PRINCIPAL_ID))
    assert r.status == SendStatus.blocked and r.guard.blocked_by == ["kill_switch"] and not tgv.voice_sent


def test_no_voice_consent_blocks_live_and_shadows_in_observe(cfg, clock, bus, tgv, tmp_home, guard_ok):
    on = voice_on(cfg)
    msg = voiced(answer_msg(clock), on, clock, tmp_home)
    s, db = make_sender(cfg, clock, bus, tgv, tmp_home, dial=Dial.draft)          # no D2 in this config
    r = run(s.send(msg, SendTrigger.approval, approval_nonce=approve(db, msg), approver_user_id=fakes.PRINCIPAL_ID))
    assert r.status == SendStatus.blocked and "voice_consent" in r.guard.blocked_by and not tgv.voice_sent
    s2, db2 = make_sender(cfg, clock, bus, tgv, tmp_home, live=False, dial=Dial.draft)   # dry_run
    r2 = run(s2.send(msg, SendTrigger.approval, approval_nonce=approve(db2, msg), approver_user_id=fakes.PRINCIPAL_ID))
    assert r2.status == SendStatus.would_send and not tgv.voice_sent
    shown = cards(bus, CardKind.observe_would_send)[-1].text
    assert "voice note" in shown and msg.voice_path in shown


def test_lane_dial_off_and_observe_apply_to_voice(cfg, clock, bus, tgv, tmp_home, guard_ok):
    on = voice_on(cfg)
    msg = voiced(answer_msg(clock), on, clock, tmp_home)
    s, db = make_sender(on, clock, bus, tgv, tmp_home, dial=Dial.off)
    r = run(s.send(msg, SendTrigger.approval, approval_nonce=approve(db, msg), approver_user_id=fakes.PRINCIPAL_ID))
    assert r.status == SendStatus.blocked and r.guard.blocked_by == ["dial"]
    s, db = make_sender(on, clock, bus, tgv, tmp_home, dial=Dial.observe)
    r = run(s.send(msg, SendTrigger.approval, approval_nonce=approve(db, msg), approver_user_id=fakes.PRINCIPAL_ID))
    assert r.status == SendStatus.would_send and not tgv.voice_sent


def test_approval_is_still_required_and_bound(cfg, clock, bus, tgv, tmp_home, guard_ok):
    on = voice_on(cfg)
    s, db = make_sender(on, clock, bus, tgv, tmp_home, dial=Dial.draft)
    msg = voiced(answer_msg(clock), on, clock, tmp_home)
    r = run(s.send(msg, SendTrigger.approval))
    assert r.status == SendStatus.blocked and r.guard.blocked_by == ["approval_binding"]
    r = run(s.send(msg, SendTrigger.approval, approval_nonce=approve(db, msg), approver_user_id=fakes.BACKUP_ID))
    assert r.status == SendStatus.blocked and not tgv.voice_sent


def test_swapped_audio_is_never_sent(cfg, clock, bus, tgv, tmp_home, guard_ok):
    """Re-record overwrote the preview, or anyone changed the file: the sender re-hashes it (voice_binding)."""
    on = voice_on(cfg)
    s, db = make_sender(on, clock, bus, tgv, tmp_home, dial=Dial.draft)
    msg = voiced(answer_msg(clock), on, clock, tmp_home)
    with open(msg.voice_path, "ab") as f:
        f.write(b"tampered")
    r = run(s.send(msg, SendTrigger.approval, approval_nonce=approve(db, msg), approver_user_id=fakes.PRINCIPAL_ID))
    assert r.status == SendStatus.blocked and "voice_binding" in r.guard.blocked_by and not tgv.voice_sent
    assert db.sends[msg.send_key]["status"] == "blocked"                     # key stays reusable (nv can follow)


def test_live_reread_still_blocks_stale_voice_draft(cfg, clock, bus, tgv, tmp_home, guard_ok):
    on = voice_on(cfg)
    s, db = make_sender(on, clock, bus, tgv, tmp_home, dial=Dial.draft)
    msg = voiced(answer_msg(clock), on, clock, tmp_home)
    tgv.thread.append(fakes.make_event("still broken", msg_id=9, ts=clock.now()))
    r = run(s.send(msg, SendTrigger.approval, approval_nonce=approve(db, msg), approver_user_id=fakes.PRINCIPAL_ID))
    assert r.status == SendStatus.blocked and "stale_draft" in r.guard.blocked_by and not tgv.voice_sent


def test_autopilot_voice_blocked_by_default_never_downgraded_to_text(cfg, clock, bus, tgv, tmp_home, guard_ok):
    on = voice_on(cfg)
    s, db = make_sender(on, clock, bus, tgv, tmp_home)
    msg = voiced(hold_msg(), on, clock, tmp_home)
    r = run(s.send(msg, SendTrigger.holding_floor))
    assert r.status == SendStatus.blocked and "voice_autopilot" in r.guard.blocked_by
    assert not tgv.voice_sent and not tgv.sent                               # no silent text fallback
    assert {c.to for c in cards(bus, CardKind.holding_blocked)} == {"principal", "backup"}
    on2 = voice_on(cfg, autopilot=True)
    s2, _ = make_sender(on2, clock, bus, tgv, tmp_home)
    assert run(s2.send(msg, SendTrigger.holding_floor)).status == SendStatus.sent and len(tgv.voice_sent) == 1


def test_adapter_without_voice_is_no_adapter(cfg, clock, bus, fake_tg, tmp_home, guard_ok):
    on = voice_on(cfg)
    s, db = make_sender(on, clock, bus, fake_tg, tmp_home, dial=Dial.draft)
    msg = voiced(answer_msg(clock), on, clock, tmp_home)
    r = run(s.send(msg, SendTrigger.approval, approval_nonce=approve(db, msg), approver_user_id=fakes.PRINCIPAL_ID))
    assert r.status == SendStatus.blocked and r.guard.blocked_by == ["no_adapter"] and not fake_tg.sent


def test_nv_approves_the_text_only(cfg, clock, bus, tgv, tmp_home, guard_ok):
    on = voice_on(cfg)
    s, db = make_sender(on, clock, bus, tgv, tmp_home, dial=Dial.draft)
    msg = answer_msg(clock)
    r = run(s.send(msg, SendTrigger.approval, approval_nonce=approve(db, msg, "nv"), approver_user_id=fakes.PRINCIPAL_ID))
    assert r.status == SendStatus.sent and len(tgv.sent) == 1 and not tgv.voice_sent


# ------------------------------------------------------------ TelethonSend.send_voice

class VoiceClient(FakeClient):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.files = []

    async def send_file(self, chat_id, file, voice_note=False, caption="", reply_to=None):
        if self.raise_exc:
            raise self.raise_exc
        self.files.append({"chat_id": chat_id, "file": file, "voice_note": voice_note, "caption": caption,
                           "reply_to": reply_to})
        from_id = None if self.anonymous else NS(user_id=fakes.PRINCIPAL_ID)
        return NS(id=778, chat_id=chat_id, from_id=from_id)


def _tele(cfg, tmp_path, client, **over):
    c = dict(cfg, run_mode="live", send_enabled=True, **over)
    return TelethonSend(c, client, has_secret=lambda n: True, stop_file=lambda: tmp_path / "STOP")


def test_telethon_send_voice(cfg, tmp_path):
    client = VoiceClient()
    s = _tele(voice_on(cfg), tmp_path, client)
    r = run(s.send_voice(fakes.GROUP_ID, str(tmp_path / "a.ogg"), caption="AI voice of Principal's assistant",
                         reply_to=5, topic_id=fakes.TOPIC_LOGISTICS))
    assert r == {"msg_id": 778, "from_id": fakes.PRINCIPAL_ID, "from_id_ok": True}
    f = client.files[0]
    assert f["voice_note"] is True and f["reply_to"] == 5 and f["caption"].startswith("AI voice")
    anon = _tele(voice_on(cfg), tmp_path, VoiceClient(anonymous=True))
    assert run(anon.send_voice(fakes.GROUP_ID, "a.ogg", caption="x", reply_to=5, topic_id=7))["from_id_ok"] is False


def test_telethon_send_voice_refuses_without_consent_or_with_stop(cfg, tmp_path):
    with pytest.raises(TgSendRefused):
        run(_tele(cfg, tmp_path, VoiceClient()).send_voice(fakes.GROUP_ID, "a.ogg", caption="x", reply_to=5,
                                                             topic_id=7))
    (tmp_path / "STOP").write_text("x", encoding="utf-8")
    with pytest.raises(TgSendRefused):
        run(_tele(voice_on(cfg), tmp_path, VoiceClient()).send_voice(fakes.GROUP_ID, "a.ogg", caption="x",
                                                                       reply_to=5, topic_id=7))


def test_sends_row_keeps_voice_hash_in_events(cfg, clock, bus, tgv, tmp_home, guard_ok):
    on = voice_on(cfg)
    s, db = make_sender(on, clock, bus, tgv, tmp_home, dial=Dial.draft)
    msg = voiced(answer_msg(clock), on, clock, tmp_home)
    run(s.send(msg, SendTrigger.approval, approval_nonce=approve(db, msg), approver_user_id=fakes.PRINCIPAL_ID))
    ev = [e for e in db.events if e["kind"] == "send"][-1]
    assert ev["voice_sha256"] == msg.voice_sha256 and "text" not in json.dumps(ev).lower().replace("text_sha256", "")
    assert text_hash(msg.text) == ev["text_sha256"] and str(Lane.L3_t1_answer) == ev["lane"]
