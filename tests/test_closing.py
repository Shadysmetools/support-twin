"""closing.py: requests close on their own (CLOSED_PROVEN / CLOSED_ANSWERED / CLOSED_REREAD_PROVEN), reopen still
works, and Principal gets no reminder cards for requests he already answered. Synthetic names and ids only."""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from twin_agent import closing, listener, paths, timers
from twin_agent.bus import Bus, Topics
from twin_agent.channels import telegram_in
from twin_agent.db import StateDB
from twin_agent.flow import Flow
from twin_agent.flow_timers import on_timer, scorecard_text
from twin_agent.models import CardKind, Event, IllegalTransition, State, TimerKind, can_transition, utc_iso
from twin_agent.router import Router
from twin_agent.testing import fakes
from twin_agent.testing.fakes import PRINCIPAL, STAFF_A, STAFF_B, make_event

S = State
K = TimerKind


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- ack text

@pytest.mark.parametrize("text", [
    "تمام", "تمام كده", "تمام كدا", "اشتغل", "اشتغلت", "done", "ok thanks", "Ok, thanks!", "👍", "👍👍", "تمااام",
    "شكرا يا باشمهندس", "تمام 👍", "وشكرا", "Thanks a lot", "tmam keda", "eshta8al", "اشتغل تمام شكرا",
    "ألف شكر", "تمام، شكراً", "works now, thanks", "okkk", "👌",
])
def test_ack_text_yes(text):
    assert closing.is_ack_text(text)


@pytest.mark.parametrize("text", [
    "", "   ", "مش تمام", "ما اشتغلش", "مشتغلش", "not done", "done?", "تمام؟", "still not working", "لسه",
    "ok but now the invoice fails", "WAVE/00123 done", "تمام بس فيه مشكلة تانية", "how do I print?",
    "please check SO10001", "?? ", "it worked yesterday but today the same error", "no", "ok " * 20,
    "cannot validate", "Send the screenshot please",
])
def test_ack_text_no(text):
    assert not closing.is_ack_text(text)


def test_ack_reaction_list_comes_from_config():
    assert closing.is_ack_reaction("👍", {}) and closing.is_ack_reaction("👍🏽", {})
    assert not closing.is_ack_reaction("❤", {}) and not closing.is_ack_reaction("", {})
    assert closing.is_ack_reaction("❤", {"close_rules": {"ack_reactions": ["❤️"]}})


def test_rule_defaults_and_legacy_key():
    assert closing.rule({}, "close_on_ack") is True and closing.rule({}, "close_silent_quickfix") is False
    assert closing.rule({"close_rules": {"reread_proven_accepted": True}}, "close_silent_quickfix") is True
    assert closing.rule({"close_rules": {"close_on_ack": False}}, "close_on_ack") is False


# ---------------------------------------------------------------- router level

@pytest.fixture
def env(tmp_home, clock, cfg, bus):
    db = StateDB(paths.state_db(), clock, cfg)
    pol = timers.TimerPolicy(cfg)
    r = Router(db, cfg, pol, bus, clock)
    cards = []
    bus.subscribe(Topics.CARD_OUT, lambda c: cards.append(c))
    yield db, r, pol, cards
    db.close()


def _open_and_triage(db, r, clock, text="how do I print the picking label?", msg_id=1, sender=STAFF_A, bus=None):
    rid = run(r.on_event(make_event(text, msg_id=msg_id, sender=sender, ts=clock.now())))
    clock.advance(minutes=3)
    run(r.close_burst(rid))
    return rid


def _principal_says(r, clock, text, msg_id, reply_to=None):
    return run(r.on_event(make_event(text, msg_id=msg_id, sender=PRINCIPAL, ts=clock.now(), reply_to=reply_to)))


def _say(r, clock, text, msg_id, sender=STAFF_A, reply_to=None):
    return run(r.on_event(make_event(text, msg_id=msg_id, sender=sender, ts=clock.now(), reply_to=reply_to)))


def _drain(bus):
    run(bus.drain())


def test_principal_answer_then_tamam_closes_answered(env, clock, bus):
    db, r, pol, cards = env
    rid = _open_and_triage(db, r, clock)
    clock.advance(minutes=10)
    _principal_says(r, clock, "Inventory > Print > Label", 2, reply_to=1)
    assert closing.human_answered(db, db.get_request(rid))
    clock.advance(hours=3)                                   # late: not "recent", no reply-to, still found
    assert _say(r, clock, "تمام كده", 3) == rid
    _drain(bus)
    req = db.get_request(rid)
    assert req["state"] == S.CLOSED_ANSWERED and "acked" in req["close_reason"]
    assert db.future_timers(rid) == []
    done = [c for c in cards if getattr(c, "kind", None) == CardKind.done]
    assert len(done) == 1 and done[0].silent and done[0].request_id == rid
    assert len(db.open_requests()) == 0                      # the "تمام" did not open a new request


def test_holding_reply_only_plus_thanks_never_closes(env, clock):
    db, r, pol, _ = env
    rid = _open_and_triage(db, r, clock)
    db.update_request(rid, first_reply_at=clock.now(), first_reply_by="agent")   # the agent's holding reply went out
    clock.advance(minutes=20)
    _say(r, clock, "ok thanks", 2)
    assert db.get_request(rid)["state"] not in (S.CLOSED_ANSWERED, S.CLOSED_PROVEN)
    assert db.events(rid, "ack_no_close")


def test_ack_with_nothing_open_opens_nothing(env, clock):
    db, r, pol, _ = env
    assert _say(r, clock, "شكرا", 1) is None
    assert db.open_requests() == [] and db.get_message(f"tg:{fakes.GROUP_ID}:1")


def test_another_persons_thanks_does_not_close(env, clock):
    db, r, pol, _ = env
    rid = _open_and_triage(db, r, clock)
    _principal_says(r, clock, "Inventory > Print", 2, reply_to=1)
    _say(r, clock, "thanks", 3, sender=STAFF_B, reply_to=2)  # STAFF_B is not the requester
    assert db.get_request(rid)["state"] not in (S.CLOSED_ANSWERED,)


def test_requester_follow_up_question_reopens_the_ball(env, clock):
    db, r, pol, _ = env
    rid = _open_and_triage(db, r, clock)
    _principal_says(r, clock, "Inventory > Print", 2, reply_to=1)
    clock.advance(minutes=5)
    _say(r, clock, "and where is the barcode option?", 3, reply_to=2)
    assert not closing.human_answered(db, db.get_request(rid))
    clock.advance(minutes=5)
    _say(r, clock, "تمام", 4, reply_to=2)                    # ack of nothing new: Principal has not answered again
    assert db.get_request(rid)["state"] not in (S.CLOSED_ANSWERED,)
    _principal_says(r, clock, "Settings > Barcode", 5)           # no reply-to: lands on the request waiting for him
    assert closing.human_answered(db, db.get_request(rid))


def test_principal_after_holding_reply_still_counts_as_answer(env, clock):
    db, r, pol, _ = env
    rid = _open_and_triage(db, r, clock)
    db.update_request(rid, first_reply_at=clock.now(), first_reply_by="agent")
    clock.advance(minutes=30)
    assert _principal_says(r, clock, "Done, try again", 2) == rid      # not a reply-to, yet linked
    assert closing.human_answered(db, db.get_request(rid))


def test_reopen_after_closed_answered(env, clock):
    db, r, pol, _ = env
    rid = _open_and_triage(db, r, clock)
    _principal_says(r, clock, "Inventory > Print", 2, reply_to=1)
    _say(r, clock, "تمام", 3)
    assert db.get_request(rid)["state"] == S.CLOSED_ANSWERED
    clock.advance(days=2)
    assert _say(r, clock, "لسه نفس المشكلة", 4) == rid
    req = db.get_request(rid)
    assert req["state"] == S.TRIAGED and req["closed_at"] is None
    assert not closing.human_answered(db, req)              # the old answer is from before the reopen
    assert db.kpi_sum("reopen", request_id=rid) == 1


@pytest.mark.parametrize("state", [S.PROOF_SENT, S.FIXED_UNCONFIRMED])
def test_ack_after_proof_closes_proven(env, clock, state):
    db, r, pol, _ = env
    rid = _open_and_triage(db, r, clock, "WAVE/00001 cannot validate")
    for st in (S.DIAGNOSED, S.PROPOSED, S.FIX_PENDING, S.VERIFYING, S.PROOF_SENT):
        db.transition(rid, st, actor="test")
    if state == S.FIXED_UNCONFIRMED:
        db.transition(rid, S.FIXED_UNCONFIRMED, actor="test")
    clock.advance(hours=2)
    _say(r, clock, "اشتغل", 5)
    req = db.get_request(rid)
    assert req["state"] == S.CLOSED_PROVEN and "proof" in req["close_reason"]


def test_fix_in_flight_ok_does_not_close(env, clock):
    db, r, pol, _ = env
    rid = _open_and_triage(db, r, clock, "WAVE/00001 cannot validate")
    _principal_says(r, clock, "I will fix it, wait", 2, reply_to=1)
    for st in (S.DIAGNOSED, S.PROPOSED, S.FIX_PENDING):
        db.transition(rid, st, actor="test")
    _say(r, clock, "ok", 3)
    assert db.get_request(rid)["state"] == S.FIX_PENDING


def test_close_on_ack_switch_off(env, clock, cfg):
    db, r, pol, _ = env
    cfg.setdefault("close_rules", {})["close_on_ack"] = False
    rid = _open_and_triage(db, r, clock)
    _principal_says(r, clock, "Inventory > Print", 2, reply_to=1)
    _say(r, clock, "تمام", 3)
    assert db.get_request(rid)["state"] != S.CLOSED_ANSWERED


def test_taken_over_request_can_close_on_ack(env, clock):
    db, r, pol, _ = env
    rid = _open_and_triage(db, r, clock)
    db.transition(rid, S.TAKEN_OVER, actor="principal")
    _principal_says(r, clock, "Inventory > Print", 2, reply_to=1)
    _say(r, clock, "thanks", 3)
    assert db.get_request(rid)["state"] == S.CLOSED_ANSWERED


def test_overlay_auto_close_follows_prev_state():
    assert can_transition(S.TAKEN_OVER, S.CLOSED_ANSWERED, S.DIAGNOSED)
    assert can_transition(S.ESCALATED, S.CLOSED_PROVEN, S.PROOF_SENT)
    assert not can_transition(S.TAKEN_OVER, S.CLOSED_ANSWERED, S.FIX_PENDING)
    assert not can_transition(S.PAUSED, S.CLOSED_PROVEN, S.DIAGNOSED)
    assert can_transition(S.CLOSED_ANSWERED, S.REOPENED) and not can_transition(S.CLOSED_ANSWERED, S.TRIAGED)


# ---------------------------------------------------------------- reactions

def _reaction(rid_msg: int, uid: int, emoji: str, clock, *, is_principal=False) -> Event:
    return Event(event_id=f"tg:{fakes.GROUP_ID}:{rid_msg}:react:{uid}:{emoji}", channel=fakes.Channel.telegram_group,
                 chat="Test Group", chat_id=fakes.GROUP_ID, topic="Logistics", topic_id=fakes.TOPIC_LOGISTICS,
                 sender=fakes.Sender(tg_id=uid, is_principal=is_principal), ts=clock.now(), text="", reply_to=rid_msg,
                 reaction=emoji)


def test_thumbs_up_reaction_by_requester_closes(env, clock):
    db, r, pol, _ = env
    rid = _open_and_triage(db, r, clock)
    _principal_says(r, clock, "Inventory > Print", 2, reply_to=1)
    assert run(r.on_event(_reaction(2, STAFF_B.tg_id, "👍", clock))) is None   # not the requester
    assert run(r.on_event(_reaction(2, fakes.PRINCIPAL_ID, "👍", clock, is_principal=True))) is None
    assert run(r.on_event(_reaction(2, STAFF_A.tg_id, "❤", clock))) is None    # not an ack emoji
    assert db.get_request(rid)["state"] != S.CLOSED_ANSWERED
    assert run(r.on_event(_reaction(2, STAFF_A.tg_id, "👍", clock))) == rid
    assert db.get_request(rid)["state"] == S.CLOSED_ANSWERED
    assert db.get_message(f"tg:{fakes.GROUP_ID}:2:react:{STAFF_A.tg_id}:👍") is None    # never stored as text
    assert len(db.requests_in([S.NEW, S.TRIAGED])) == 0


def test_telethon_reaction_update_to_events(cfg):
    peer = SimpleNamespace(channel_id=int(str(fakes.GROUP_ID)[4:]))
    rec = [SimpleNamespace(peer_id=SimpleNamespace(user_id=STAFF_A.tg_id), reaction=SimpleNamespace(emoticon="👍"),
                           date=datetime(2026, 11, 1, 12, 0, tzinfo=fakes.CAIRO)),
           SimpleNamespace(peer_id=SimpleNamespace(channel_id=5), reaction=SimpleNamespace(emoticon="👍"), date=None),
           SimpleNamespace(peer_id=SimpleNamespace(user_id=fakes.PRINCIPAL_ID), reaction=SimpleNamespace(emoticon="👍"),
                           date=None)]
    upd = SimpleNamespace(peer=peer, msg_id=42, top_msg_id=fakes.TOPIC_LOGISTICS, reactions=SimpleNamespace(
        recent_reactions=rec))
    evs = telegram_in.reaction_events(upd, cfg, fakes.PRINCIPAL_ID)
    assert [(e.sender.tg_id, e.reaction, e.reply_to, e.text) for e in evs] == \
        [(STAFF_A.tg_id, "👍", 42, ""), (fakes.PRINCIPAL_ID, "👍", 42, "")]
    assert evs[1].sender.is_principal and not evs[0].sender.is_principal and evs[0].chat_id == fakes.GROUP_ID
    other = SimpleNamespace(peer=SimpleNamespace(channel_id=999999), msg_id=1, top_msg_id=None,
                            reactions=SimpleNamespace(recent_reactions=rec))
    assert telegram_in.reaction_events(other, cfg, fakes.PRINCIPAL_ID) == []         # not allow-listed
    cfg.setdefault("close_rules", {})["close_on_ack"] = False
    assert telegram_in.reaction_events(upd, cfg, fakes.PRINCIPAL_ID) == []


def test_telethon_in_routes_reactions(cfg, monkeypatch):
    got = []

    async def on_event(ev):
        got.append(ev)
    tin = telegram_in.TelethonIn.__new__(telegram_in.TelethonIn)
    tin.cfg, tin.principal_user_id, tin._on_event = cfg, fakes.PRINCIPAL_ID, on_event
    upd = SimpleNamespace(peer=SimpleNamespace(channel_id=int(str(fakes.GROUP_ID)[4:])), msg_id=7, top_msg_id=None,
                          reactions=SimpleNamespace(recent_reactions=[SimpleNamespace(
                              peer_id=SimpleNamespace(user_id=2001), reaction=SimpleNamespace(emoticon="👍"),
                              date=None)]))
    assert len(run(tin.handle_reactions(upd))) == 1 and got[0].reaction == "👍"
    assert run(tin.handle_reactions(object())) == []          # a malformed update never raises


# ---------------------------------------------------------------- timers and flow

@pytest.fixture
def flow(tmp_home, cfg, clock):
    db = StateDB(paths.state_db(), clock, cfg)
    bus = Bus()
    adapters = listener._dry_adapters()
    f = Flow(db, cfg, bus, clock, adapters, sender=listener._optional_sender(db, cfg, adapters, clock, bus),
             bot_ctl=listener._optional_bot_ctl(db, cfg, adapters, clock, bus))
    f.deadman = listener.DeadMan(cfg)
    f.spawner.retry_delay_s = 0
    f.wire()
    yield f
    db.close()


async def settle(f):
    for _ in range(10):
        await f.bus.drain()
        if not f.tasks:
            break
        await asyncio.gather(*list(f.tasks), return_exceptions=True)
    await f.bus.drain()


def _flow_request(f, clock, tier="T1", text="how do I print the label?"):
    rid = run(f.router.on_event(make_event(text, msg_id=1, ts=clock.now())))
    clock.advance(minutes=3)
    f.db.update_request(rid, tier=tier)
    f.db.transition(rid, S.TRIAGED, actor="test")
    timers.schedule_for_state(f.db, f.policy, rid, S.TRIAGED, clock.now())
    f.db.cancel_timers(rid, [K.burst_close.value])
    return rid


def _cards(f):
    return [c for _, c, _ in f.adapters.bot.cards]


def test_answered_request_plans_no_reminders(flow, clock):
    f = flow
    rid = _flow_request(f, clock)
    run(f.router.on_event(make_event("Inventory > Print", msg_id=2, sender=PRINCIPAL, ts=clock.now(), reply_to=1)))
    kinds = {t["kind"] for t in f.db.future_timers(rid)}
    assert {"answered_close", "weekly_recheck"} <= kinds
    assert not kinds & {"reminder", "holding"}
    # timers planned before the answer may still fire: they must not card Principal
    for kind in (K.sla_warning, K.escalate_backup, K.reminder, K.weekly_recheck):
        run(on_timer(f, {"timer_id": 0, "request_id": rid, "kind": kind.value, "payload": {}}))
    run(settle(f))
    assert [c for c in _cards(f) if c.request_id == rid and c.kind in (CardKind.reminder, CardKind.escalation)] == []
    assert f.db.events(rid, "weekly_answered_open")
    assert rid in scorecard_text(f)                          # the one weekly "worked?" card lists it


def test_unanswered_request_still_gets_sla_card(flow, clock):
    f = flow
    rid = _flow_request(f, clock, tier="T2a", text="WAVE/00001 cannot validate")
    run(on_timer(f, {"timer_id": 0, "request_id": rid, "kind": "sla_warning", "payload": {}}))
    run(settle(f))
    assert any(c.kind == CardKind.reminder and "75%" in c.text for c in _cards(f))


def test_informational_answer_closes_after_48_working_hours(flow, clock):
    f = flow
    rid = _flow_request(f, clock)                            # Sunday 14:43 Cairo
    run(f.router.on_event(make_event("Inventory > Print", msg_id=2, sender=PRINCIPAL, ts=clock.now(), reply_to=1)))
    due = timers.parse_ts([t for t in f.db.future_timers(rid) if t["kind"] == "answered_close"][0]["due_at"])
    assert due == f.policy.add_working_hours(clock.now(), 48)   # 6 working days later, Fridays skipped
    clock.set(due - timedelta(minutes=1))
    timers.tick(f.db, f.policy, clock.now(), f.bus)
    run(settle(f))
    assert f.db.get_request(rid)["state"] != S.CLOSED_ANSWERED
    clock.set(due)
    timers.tick(f.db, f.policy, clock.now(), f.bus)
    run(settle(f))
    req = f.db.get_request(rid)
    assert req["state"] == S.CLOSED_ANSWERED and "silent" in req["close_reason"]
    assert any(c.kind == CardKind.done for c in _cards(f))


def test_silence_close_skips_non_informational_and_follow_ups(flow, clock):
    f = flow
    rid = _flow_request(f, clock, tier="T2a", text="WAVE/00001 cannot validate")
    run(f.router.on_event(make_event("fixed, try again", msg_id=2, sender=PRINCIPAL, ts=clock.now(), reply_to=1)))
    kinds = {t["kind"] for t in f.db.future_timers(rid)}
    assert "answered_close" not in kinds and "weekly_recheck" in kinds    # weekly "worked?" check only
    assert not closing.on_answered_close(f, rid)
    rid2 = run(f.router.on_event(make_event("how to export?", msg_id=10, sender=STAFF_B, ts=clock.now())))
    f.db.update_request(rid2, tier="T1")
    f.db.transition(rid2, S.TRIAGED, actor="test")
    run(f.router.on_event(make_event("File > Export", msg_id=11, sender=PRINCIPAL, ts=clock.now(), reply_to=10)))
    clock.advance(minutes=5)
    run(f.router.on_event(make_event("and to excel too?", msg_id=12, sender=STAFF_B, ts=clock.now(), reply_to=11)))
    clock.advance(days=14)
    assert not closing.on_answered_close(f, rid2)           # requester asked again: never closed by silence
    assert f.db.get_request(rid2)["state"] == S.ACKED


def test_answered_close_waits_for_the_latest_answer(flow, clock):
    f = flow
    rid = _flow_request(f, clock)
    run(f.router.on_event(make_event("Inventory > Print", msg_id=2, sender=PRINCIPAL, ts=clock.now(), reply_to=1)))
    first_due = f.policy.add_working_hours(clock.now(), 48)
    clock.advance(hours=20)
    run(f.router.on_event(make_event("Also check Settings", msg_id=3, sender=PRINCIPAL, ts=clock.now(), reply_to=1)))
    later_due = f.policy.add_working_hours(clock.now(), 48)
    clock.set(first_due)
    assert not closing.on_answered_close(f, rid)
    assert any(timers.parse_ts(t["due_at"]) == later_due for t in f.db.future_timers(rid)
               if t["kind"] == "answered_close")


def _to_proof(f, rid):
    for st in (S.DIAGNOSED, S.PROPOSED, S.FIX_PENDING, S.VERIFYING, S.PROOF_SENT):
        f.db.transition(rid, st, actor="test")


def _sent_chasers(f, rid, n, clock):
    from twin_agent.models import Channel, OutboundMessage, OutKind, SendStatus
    for i in range(1, n + 1):
        clock.advance(hours=2)
        msg = OutboundMessage(request_id=rid, step=f"worked-{i}", kind=OutKind.worked_chaser,
                              channel=Channel.telegram_group, text="worked?", chat_id=fakes.GROUP_ID)
        f.db.finish_send(msg.send_key, SendStatus.sent, msg=msg, sent_at=utc_iso(clock.now()))


def test_worked_chaser_2_default_is_fixed_unconfirmed(flow, clock):
    f = flow
    rid = _flow_request(f, clock, tier="T2a", text="WAVE/00001 cannot validate")
    _to_proof(f, rid)
    _sent_chasers(f, rid, 2, clock)
    run(on_timer(f, {"timer_id": 0, "request_id": rid, "kind": "worked_chaser_2", "payload": {}}))
    assert f.db.get_request(rid)["state"] == S.FIXED_UNCONFIRMED
    assert "weekly_recheck" in {t["kind"] for t in f.db.future_timers(rid)}


@pytest.mark.parametrize("key", ["close_silent_quickfix", "reread_proven_accepted"])
def test_silent_quickfix_closes_when_switched_on(flow, clock, cfg, key):
    f = flow
    cfg.setdefault("close_rules", {})[key] = True
    rid = _flow_request(f, clock, tier="T2a", text="WAVE/00001 cannot validate")
    _to_proof(f, rid)
    _sent_chasers(f, rid, 2, clock)
    run(on_timer(f, {"timer_id": 0, "request_id": rid, "kind": "worked_chaser_2", "payload": {}}))
    assert f.db.get_request(rid)["state"] == S.CLOSED_REREAD_PROVEN


@pytest.mark.parametrize("case", ["pings_not_sent", "video_case", "no_reread", "requester_spoke"])
def test_silent_quickfix_needs_every_condition(flow, clock, cfg, case):
    f = flow
    cfg.setdefault("close_rules", {})["close_silent_quickfix"] = True
    rid = _flow_request(f, clock, tier="T3" if case == "video_case" else "T2a", text="WAVE/00001 cannot validate")
    if case == "no_reread":
        for st in (S.DIAGNOSED, S.PROOF_SENT):
            f.db.transition(rid, st, actor="test")
    else:
        _to_proof(f, rid)
    _sent_chasers(f, rid, 1 if case == "pings_not_sent" else 2, clock)
    if case == "requester_spoke":
        run(f.router.on_event(make_event("I will check tomorrow", msg_id=9, ts=clock.now(), reply_to=1)))
    run(on_timer(f, {"timer_id": 0, "request_id": rid, "kind": "worked_chaser_2", "payload": {}}))
    assert f.db.get_request(rid)["state"] == S.FIXED_UNCONFIRMED
    assert f.db.events(rid, "reread_close_skipped")


def test_reread_proven_still_refused_by_db_when_off(flow, clock):
    f = flow
    rid = _flow_request(f, clock, tier="T2a", text="WAVE/00001")
    _to_proof(f, rid)
    with pytest.raises(IllegalTransition):
        f.db.transition(rid, S.CLOSED_REREAD_PROVEN, actor="test")


def test_stale_approve_tap_after_close_sends_nothing(flow, clock):
    f = flow
    rid = _flow_request(f, clock)
    sends = []
    f.bus.subscribe(Topics.SEND_REQUEST, lambda p: sends.append(p))
    f.db.transition(rid, S.CLOSED_BY_PRINCIPAL, actor="test")
    run(f.on_callback({"request_id": rid, "action": "ap", "user_id": fakes.PRINCIPAL_ID, "nonce": "x"}))
    run(settle(f))
    assert sends == [] and f.db.events(rid, "callback_after_close")


def test_packet_after_close_posts_no_draft(flow, clock):
    f = flow
    rid = _flow_request(f, clock)
    f.db.transition(rid, S.CLOSED_BY_PRINCIPAL, actor="test")
    before = len(_cards(f))
    run(f.on_packet({"request_id": rid, "packet": fakes.canned_packet(rid)}))
    run(settle(f))
    assert len(_cards(f)) == before and f.db.get_request(rid)["state"] == S.CLOSED_BY_PRINCIPAL
    assert f.db.latest_packet(rid) and f.db.events(rid, "packet_after_close")


def test_close_command_closes(flow, clock):
    f = flow
    rid = _flow_request(f, clock)
    run(f.on_command({"command": "close", "args": rid.lower(), "user_id": fakes.PRINCIPAL_ID}))
    assert f.db.get_request(rid)["state"] == S.CLOSED_BY_PRINCIPAL


def test_digest_lists_rule_closes(flow, clock):
    from twin_agent.flow_timers import digest_text
    f = flow
    rid = _flow_request(f, clock)
    run(f.router.on_event(make_event("Inventory > Print", msg_id=2, sender=PRINCIPAL, ts=clock.now(), reply_to=1)))
    run(f.router.on_event(make_event("تمام شكرا", msg_id=3, ts=clock.now())))
    assert "1 closed by the closing rules" in digest_text(f) and rid in digest_text(f)


def test_done_cards_do_not_use_up_the_hourly_cap(flow, clock):
    from twin_agent.models import Card
    ctl = flow.bot_ctl
    for i in range(12):
        run(ctl.post_card(Card(kind=CardKind.done, text=f"REQ-{i:04d} closed", request_id=f"REQ-{i:04d}", silent=True)))
    run(ctl.post_card(Card(kind=CardKind.new_request, text="REQ-0100 new", request_id="REQ-0100")))
    sent = [c for _, c, _ in flow.adapters.bot.cards]
    assert any(c.kind == CardKind.new_request for c in sent)       # the real card went out, not into the digest


def test_scorecard_says_how_many_more(flow, clock, monkeypatch):
    rows = [{"id": f"REQ-{i:04d}", "title": "t"} for i in range(30)]
    monkeypatch.setattr(flow.db, "open_requests", lambda: rows)
    monkeypatch.setattr(closing, "human_answered", lambda db, r, **k: True)
    assert "and 5 more" in scorecard_text(flow)


def test_take_over_always_confirms_to_principal(flow, clock):
    """Principal 2026-11-09: 'I pressed Take over, nothing happened'. Take over now always answers with a card:
    taken over / already yours / already closed."""
    f = flow
    rid = _flow_request(f, clock)
    run(f.on_callback({"request_id": rid, "action": "to", "user_id": fakes.PRINCIPAL_ID, "nonce": "x"}))
    run(settle(f))
    texts = [c.text for c in _cards(f)]
    assert f.db.get_request(rid)["state"] == S.TAKEN_OVER and any("taken over: it is yours now" in t for t in texts)
    run(f.on_callback({"request_id": rid, "action": "to", "user_id": fakes.PRINCIPAL_ID, "nonce": "y"}))
    run(settle(f))
    assert any("already yours" in c.text for c in _cards(f))
    f.db.transition(rid, S.CLOSED_BY_PRINCIPAL, actor="test")
    run(f.on_callback({"request_id": rid, "action": "to", "user_id": fakes.PRINCIPAL_ID, "nonce": "z"}))
    run(settle(f))
    assert any("already closed" in c.text for c in _cards(f))


def test_why_explains_the_reply_with_buttons(flow, clock):
    """Principal 2026-11-10: 'I sent Why on REQ-0057, it should tell me why you will answer this'. Why? used to do
    nothing. Now it answers with the reasoning: reply, understanding, records checked, open questions, and the
    Approve / Edit / Take over buttons on the same card."""
    f = flow
    rid = _flow_request(f, clock)
    packet = {"request_id": rid, "tier": "T1", "understanding": "He cannot validate BATCH/13918.",
              "draft_reply": {"text": "Set the Alma line to 0, then validate BATCH/13918."},
              "evidence": [{"kind": "odoo_read", "model": "stock.quant", "summary": "Cars/Drivers holds 0"}],
              "questions_for_principal": ["Is the armchair in the warehouse?"]}
    f.db.save_packet(rid, packet, False, [])
    run(f.on_callback({"request_id": rid, "action": "wy", "user_id": fakes.PRINCIPAL_ID, "nonce": "w"}))
    run(settle(f))
    why = [c for c in _cards(f) if "why this reply" in (c.text or "")]
    assert why, [c.text for c in _cards(f)]
    t = why[-1].text
    assert "Set the Alma line to 0" in t and "Cars/Drivers holds 0" in t and "Is the armchair" in t
    assert {b.action for row in why[-1].buttons for b in row} == {"ap", "ed", "to"}


def test_approve_and_close_closes_only_after_the_reply_is_sent(flow, clock):
    """Principal 2026-11-10: 'Approve + close' posts the reply, then closes the request and its ticket - but only when
    the post really went out. Blocked: nothing closes and Principal is told."""
    from twin_agent.models import SendResult, SendStatus
    f = flow
    rid = _flow_request(f, clock)
    f.db.save_packet(rid, {"request_id": rid, "tier": "T1", "draft_reply": {"text": "Inventory > Print."}}, False, [])
    f.pending_close.clear()
    f.bus.publish = lambda *a, **k: None                  # keep the real send out of this unit test
    run(f.on_callback({"request_id": rid, "action": "ac", "user_id": fakes.PRINCIPAL_ID, "nonce": "c"}))
    assert len(f.pending_close) == 1
    key = next(iter(f.pending_close))
    published = []
    f.bus.publish = lambda topic, p=None: published.append((topic, p))
    run(f.on_send_done(SendResult(SendStatus.blocked, key, "promises")))
    assert not any(p and p.get("command") == "close" for _, p in published if isinstance(p, dict))
    assert any("stay open" in getattr(p, "text", "") for _, p in published)
    f.pending_close[key] = (rid, fakes.PRINCIPAL_ID)
    published.clear()
    run(f.on_send_done(SendResult(SendStatus.sent, key)))
    assert ("command.in" in [str(t) for t, _ in published] or
            any(isinstance(p, dict) and p.get("command") == "close" and p.get("args") == rid for _, p in published))


def test_email_card_has_no_approve_while_the_twin_cannot_send_email(flow, clock):
    from twin_agent.models import Channel
    f = flow
    rid = _flow_request(f, clock)
    f.db.update_request(rid, channel=str(Channel.email))
    f.adapters.gmail_send = None
    assert f._mail_only(rid)
    f.db.save_packet(rid, {"request_id": rid, "tier": "T1", "draft_reply": {"text": "Dear Signatory, next step."}},
                     False, [])
    run(f._why_card(rid))
    run(settle(f))
    why = [c for c in _cards(f) if "why this reply" in (c.text or "")][-1]
    assert {b.action for row in why.buttons for b in row} == {"ck", "to"}     # Close + Take over, no Approve


def test_first_card_uses_principals_four_part_format(flow, clock):
    """Principal 2026-11-11: the first card (before the analysis) follows his format too."""
    f = flow
    rid = _flow_request(f, clock)
    run(f.on_triaged({"request_id": rid}))
    run(settle(f))
    first = [c for c in _cards(f) if c.request_id == rid and "Checking it now" in (c.text or "")]
    assert first, [c.text for c in _cards(f)]
    t = first[-1].text
    assert "Message: " in t and "Analysis: Checking it now" in t and "NO DRAFT REPLY YET" in t
    assert {b.action for row in first[-1].buttons for b in row} == {"to", "tn"}


def test_plain_approve_and_edit_send_close_after_the_send(flow, clock):
    """2026-11-11: Approve (and Principal's own Edit + Send) close the request and its ticket once sent."""
    f = flow
    rid = _flow_request(f, clock)
    f.db.save_packet(rid, {"request_id": rid, "tier": "T1", "draft_reply": {"text": "Inventory > Print."}}, False, [])
    f.pending_close.clear()
    f.bus.publish = lambda *a, **k: None
    run(f.on_callback({"request_id": rid, "action": "ap", "user_id": fakes.PRINCIPAL_ID, "nonce": "a"}))
    assert list(f.pending_close.values()) == [(rid, fakes.PRINCIPAL_ID)]
    f.pending_close.clear()
    f.pending_edit[rid] = "My own words."
    run(f.on_callback({"request_id": rid, "action": "sd", "user_id": fakes.PRINCIPAL_ID, "nonce": "s"}))
    assert list(f.pending_close.values()) == [(rid, fakes.PRINCIPAL_ID)]


def test_duplicate_approve_of_an_already_posted_reply_still_closes(flow, clock):
    """2026-11-11 (REQ-0062): a second card's Approve is refused as a duplicate (never posted twice), but the reply
    is out, so the request and its ticket close."""
    from twin_agent.models import SendResult, SendStatus
    f = flow
    rid = _flow_request(f, clock)
    f.db.save_packet(rid, {"request_id": rid, "tier": "T1", "draft_reply": {"text": "Inventory > Print."}}, False, [])
    msg = f.outbound_for(rid)
    f.db.finish_send(msg.send_key, SendStatus.sent, msg=msg, sent_at="2026-11-11T09:04:09+00:00", remote_id="6697")
    f.pending_close[msg.send_key] = (rid, fakes.PRINCIPAL_ID)
    published = []
    f.bus.publish = lambda topic, p=None: published.append(p)
    run(f.on_send_done(SendResult(SendStatus.duplicate, msg.send_key, "send key already used")))
    assert any(isinstance(p, dict) and p.get("command") == "close" and p.get("args") == rid for p in published)


def test_development_request_stays_open_after_approve(flow, clock):
    """Principal 2026-11-11: a development request (T3 / dev part) is not finished when the reply goes out."""
    from twin_agent.models import SendResult, SendStatus
    f = flow
    rid = _flow_request(f, clock)
    f.db.save_packet(rid, {"request_id": rid, "tier": "T3", "draft_reply": {"text": "Raised with development."}},
                     False, [])
    f.pending_close["k1"] = (rid, fakes.PRINCIPAL_ID)
    published = []
    f.bus.publish = lambda topic, p=None: published.append(p)
    run(f.on_send_done(SendResult(SendStatus.sent, "k1")))
    assert not any(isinstance(p, dict) and p.get("command") == "close" for p in published)
    assert any("development request" in getattr(p, "text", "") for p in published)
