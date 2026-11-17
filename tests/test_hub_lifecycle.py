"""Review #5-#9, #11-#15: tickets keyed per ticket, owner-marked confirm, resolve routing, failure handling, holds,
batching, media, concurrency, the real judge. Driven through ``Hub.handle_webhook`` / ``Hub.on_owner_decision``."""
from __future__ import annotations

import copy
import threading
import time

import pytest

from twin_agent.hub import fakes, judge, proof_runner
from twin_agent.testing.fake_odoo import FakeOdoo
from hub_helpers import approve, build_hub, deliver, load, msg, pending, run_to_resolved

PHONE = "201000000001"


def _creates(hub) -> int:
    return sum(1 for w in hub.odoo.t.writes if w["method"] == "create")


def _now_ts(hub) -> int:
    return int(hub.clock().timestamp()) - 10


# ---------------------------------------------------------------- #5 state per ticket, reopen, attach

def test_new_issue_after_the_reopen_window_opens_a_new_ticket():
    hub = build_hub()
    run_to_resolved(hub)
    first = hub.latest_ticket(PHONE)
    assert first.stage == "resolved"
    hub.clock.advance(days=8)                                       # > reopen_days (default 7)
    res = deliver(hub, msg("n1", "New problem: payroll is broken", ts=_now_ts(hub)))
    second = hub.latest_ticket(PHONE)
    assert second.tid != first.tid and res["ticket_id"] == second.tid and _creates(hub) == 2
    assert hub.whatsapp.automatic_count() == 2                      # one ack PER ticket, never two for one
    assert len(hub.tickets_for(PHONE)) == 2 and hub.ticket(first.tid).stage == "resolved"


def test_reply_within_reopen_days_asks_the_owner_and_reopens_only_when_chosen():
    hub = build_hub()
    run_to_resolved(hub)
    tid = hub.latest_ticket(PHONE).tid
    hub.clock.advance(days=2)
    res = deliver(hub, msg("r1", "It is still wrong", ts=_now_ts(hub)))
    st = hub.latest_ticket(PHONE)
    assert res.get("reopen_asked") is True and st.tid == tid and st.stage == "resolved" and _creates(hub) == 1
    assert any("Reopen or keep closed" in p["text"] for p in hub.telegram.posts)    # owner-visible
    assert pending(hub, "reopen") is not None and hub.whatsapp.automatic_count() == 1
    approve(hub, "reopen")
    st = hub.latest_ticket(PHONE)
    assert st.stage == "in_progress" and st.reopen_count == 1 and st.tid == tid


def test_reopen_days_is_configurable():
    hub = build_hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "reopen_days": 1})
    run_to_resolved(hub)
    hub.clock.advance(days=2)
    deliver(hub, msg("n2", "Another thing", ts=_now_ts(hub)))
    assert len(hub.tickets_for(PHONE)) == 2


def test_messages_while_awaiting_owner_attach_to_the_ticket_not_dropped():
    hub = build_hub()
    first = deliver(hub, "webhook_text.json")
    res = deliver(hub, "webhook_confirm.json")                       # the customer writes before CONFIRM is approved
    st = hub.latest_ticket(PHONE)
    assert res.get("attached") is True and st.stage == "awaiting_owner" and st.tid == first["ticket_id"]
    assert len(st.replies) == 1 and "نعم" in st.replies[0]["text"]
    assert any("نعم" in p["text"] for p in hub.telegram.posts)       # owner sees it in the topic
    notes = [w for w in hub.odoo.t.writes if w["method"] == "message_post"]
    assert any("نعم" in (w["kwargs"].get("body") or "") for w in notes)    # and it is a note on the Odoo ticket
    assert hub.proof.calls == [] and _creates(hub) == 1


# ---------------------------------------------------------------- #6 negative reply, #15 no PROVE-IT rerun

def test_negative_reply_is_not_a_confirmation():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    approve(hub, "confirm")
    res = deliver(hub, msg("neg1", "No, that's not it at all, the problem is in invoices"))
    assert res.get("attached") is True and hub.latest_ticket(PHONE).stage == "awaiting_owner_advance"
    assert hub.proof.calls == [] and hub.metrics.pushed == [] and len(hub.whatsapp.sent) == 2
    out = approve(hub, "advance", approved=False)                    # the owner reads it: not a confirmation
    assert out["ok"] is True and hub.latest_ticket(PHONE).stage == "awaiting_customer"
    assert hub.proof.calls == [] and hub.metrics.pushed == []


def test_only_the_owner_marked_confirm_moves_forward_and_proveit_waits_for_fix_ready():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    approve(hub, "confirm")
    deliver(hub, "webhook_confirm.json")
    approve(hub, "advance")
    st = hub.latest_ticket(PHONE)
    assert st.stage == "in_progress" and hub.proof.calls == []       # the fix step is not skipped
    approve(hub, "fix_ready")
    assert len(hub.proof.calls) == 1 and hub.latest_ticket(PHONE).stage == "awaiting_owner_resolve"


def test_unapproved_resolve_does_not_rerun_proveit_on_every_customer_message():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    approve(hub, "confirm")
    deliver(hub, "webhook_confirm.json")
    approve(hub, "advance")
    approve(hub, "fix_ready")                                        # PROVE-IT once; RESOLVE draft awaits the owner
    for i in range(3):
        deliver(hub, msg(f"x{i}", "any update?"))
    resolves = [a for a in hub.telegram.approvals if a["kind"] == "resolve"]
    assert len(hub.proof.calls) == 1 and len(resolves) == 1
    assert hub.latest_ticket(PHONE).stage == "awaiting_owner_resolve" and len(hub.latest_ticket(PHONE).replies) == 4


# ---------------------------------------------------------------- #7 resolve routing by type + verdict

def _to_in_progress(hub, text=None):
    p = load("webhook_text.json")
    if text:
        p["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = text
    deliver(hub, p)
    approve(hub, "confirm")
    deliver(hub, "webhook_confirm.json")
    return approve(hub, "advance")


def test_resolution_with_a_reproduced_verdict_uses_template_3b_with_the_video():
    hub = build_hub()
    _to_in_progress(hub)
    approve(hub, "fix_ready")
    draft = pending(hub, "resolve")["draft"]
    assert "wiki.invalid" in draft and "short video" in draft
    assert approve(hub, "resolve")["ok"] is True and hub.latest_ticket(PHONE).stage == "resolved"


def test_inquiry_needs_no_proof_video_and_waits_for_the_owner_to_answer():
    hub = build_hub()
    _to_in_progress(hub, "How do I print a delivery slip in Inventory?")
    st = hub.latest_ticket(PHONE)
    assert st.analysis.ticket_type == "Inquiries" and hub.proof.calls == []
    assert pending(hub, "fix_ready") is None and pending(hub, "resolve") is None
    assert [h["reason"] for h in hub.holds()] == ["guidance_needed"]   # TODO(§13): no guidance template exists yet


@pytest.mark.parametrize("verdict", ["not_reproduced", "user_error"])
def test_guidance_verdicts_are_not_drafted_as_resolved(verdict):
    hub = build_hub(proof=proof_runner.FakeProofRunner(verdict=verdict))
    _to_in_progress(hub)
    approve(hub, "fix_ready")
    assert pending(hub, "resolve") is None and hub.metrics.pushed == []
    assert [h["reason"] for h in hub.holds()] == ["guidance_needed"]
    assert hub.latest_ticket(PHONE).stage == "in_progress"


def test_blocked_proof_holds_and_lets_the_owner_retry():
    class Blocked:
        calls = 0

        def prove(self, request, packet, cfg):
            self.calls += 1
            return {"status": "blocked", "why": "no staging"}

    hub = build_hub(proof=Blocked())
    _to_in_progress(hub)
    approve(hub, "fix_ready")
    assert [h["reason"] for h in hub.holds()] == ["proof_blocked"]
    assert pending(hub, "fix_ready") is not None and pending(hub, "resolve") is None


def test_resolved_ticket_pushes_kpi_and_a_learning_record_with_partner_id():
    hub = build_hub()
    run_to_resolved(hub)
    assert len(hub.metrics.pushed) == 1 and hub.metrics.pushed[0]["outcome"]["resolved"] is True
    rec = hub.learning.records[0]
    assert rec["partner_id"] == 3001 and "wa_id" not in rec and rec["reassigned"] is False


def test_reassign_is_recorded_on_the_learning_record():
    from twin_agent.hub.model import Owner
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    hub.reassign(hub.latest_ticket(PHONE).tid, Owner(uid=777, name="Principal", tg_id=1001), actor_tg_id=1003)
    approve(hub, "confirm", as_tg=1001)                              # the NEW owner approves
    deliver(hub, "webhook_confirm.json")
    approve(hub, "advance", as_tg=1001)
    approve(hub, "fix_ready", as_tg=1001)
    approve(hub, "resolve", as_tg=1001)
    assert hub.learning.records[0]["reassigned"] is True and hub.learning.records[0]["final_owner_uid"] == 777


# ---------------------------------------------------------------- #8 failure handling, no orphans

class FlakyTelegram(fakes.FakeTelegram):
    def __init__(self, fail_times=1):
        super().__init__()
        self.fail_times = fail_times

    def open_topic(self, tid, owner, brief):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("telegram down")
        return super().open_topic(tid, owner, brief)


def test_failure_after_create_leaves_no_orphan_and_retry_resumes_without_a_second_ticket():
    hub = build_hub(telegram=FlakyTelegram())
    res = deliver(hub, "webhook_text.json")
    assert res.get("error") and res.get("retryable") is True and _creates(hub) == 1     # ticket exists ...
    assert hub.latest_ticket(PHONE).tid and hub.whatsapp.sent == []                    # ... and is tracked, no ack yet
    out = hub.retry_failed()                                          # the re-drive path (Meta won't retry a 200)
    assert len(out) == 1 and out[0].get("ticket_id")
    assert _creates(hub) == 1 and hub.whatsapp.automatic_count() == 1
    assert hub.latest_ticket(PHONE).stage == "awaiting_owner" and pending(hub, "confirm")
    assert hub.retry_failed() == []                                   # nothing left to retry


def test_redelivery_of_a_failed_message_resumes_too():
    hub = build_hub(telegram=FlakyTelegram())
    deliver(hub, "webhook_text.json")
    again = deliver(hub, "webhook_text.json")                          # Meta/replay re-sends the same wamid
    assert again.get("duplicate") is not True and _creates(hub) == 1 and hub.whatsapp.automatic_count() == 1


def test_a_done_message_is_still_a_duplicate():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    assert deliver(hub, "webhook_text.json").get("duplicate") is True and _creates(hub) == 1


def test_a_failed_ack_send_is_never_retried_so_the_client_cannot_get_two_acks():
    class OnceBroken(fakes.FakeWhatsApp):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def send_text(self, *a, **kw):
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError("network error: outcome unknown")
            return super().send_text(*a, **kw)

    hub = build_hub(whatsapp=OnceBroken())
    deliver(hub, "webhook_text.json")
    hub.retry_failed()
    assert hub.whatsapp.attempts == 1 and hub.whatsapp.automatic_count() == 0
    assert any("outcome unknown" in p["text"].lower() for p in hub.telegram.posts)
    assert hub.latest_ticket(PHONE).stage == "awaiting_owner" and pending(hub, "confirm")


# ---------------------------------------------------------------- #9 concurrency

def test_concurrent_text_and_voice_note_make_one_ticket_and_one_ack():
    class SlowOdoo(FakeOdoo):
        def execute_kw(self, model, method, args, kwargs):
            if method == "create":
                time.sleep(0.15)                                       # widen the check-then-act window
            return super().execute_kw(model, method, args, kwargs)

    hub = build_hub(transport=SlowOdoo())
    hub.clock.advance(minutes=5)
    voice = load("webhook_voice.json")
    barrier = threading.Barrier(2)
    results = []

    def go(payload):
        barrier.wait()
        results.append(hub.handle_webhook(payload))

    ts = [threading.Thread(target=go, args=(p,)) for p in (load("webhook_text.json"), voice)]
    [t.start() for t in ts]
    [t.join(timeout=10) for t in ts]
    assert len(results) == 2 and _creates(hub) == 1
    assert hub.whatsapp.automatic_count() == 1 and len(hub.tickets_for(PHONE)) == 1
    assert len(hub.latest_ticket(PHONE).replies) == 1                  # the second message attached to the first ticket


# ---------------------------------------------------------------- #11 real judge inside the hub

def test_real_judge_that_is_blocked_does_not_crash_stage_3(monkeypatch):
    for v in ("GOOGLE_APPLICATION_CREDENTIALS", "VERTEX_PROJECT", "OPENAI_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    hub = build_hub(judge=judge.resolve_judge())
    res = deliver(hub, "webhook_text.json")
    assert res["ticket_id"] and res["stage"] == "awaiting_owner"
    posted = " ".join(p["text"] for p in hub.telegram.posts)
    assert "UNJUDGED" in posted and "owner-review" in posted.lower()
    assert approve(hub, "confirm")["ok"] is True                       # the owner can still decide


def test_judge_that_raises_is_treated_as_unjudged():
    class Boom:
        def score(self, kind, item):
            raise RuntimeError("judge down")

    hub = build_hub(judge=Boom())
    assert deliver(hub, "webhook_text.json")["stage"] == "awaiting_owner"


# ---------------------------------------------------------------- #12 held for a human has a human

def test_unknown_sender_is_held_with_a_triage_notification_and_no_stt():
    hub = build_hub(knowledge_odoo=fakes.FakeOdooKnowledge(partners={}))
    res = deliver(hub, "webhook_voice.json")
    assert res["held"] == "unknown_sender" and hub.stt.calls == []        # strangers' audio never goes to Groq
    assert len(hub.telegram.triage) == 1 and "unknown_sender" in hub.telegram.triage[0]
    assert PHONE not in hub.telegram.triage[0]                           # PII minimised: masked number only
    assert [h["reason"] for h in hub.holds()] == ["unknown_sender"] and _creates(hub) == 0


def test_hold_can_be_released_after_the_partner_is_added():
    hub = build_hub(knowledge_odoo=fakes.FakeOdooKnowledge(partners={}),
                    cfg={"phone_number_id": "PHONE_NUMBER_ID", "approver_tg_ids": [4242]})
    deliver(hub, "webhook_text.json")
    hub.k_odoo.partners[PHONE] = 3001                                    # a human created the partner
    out = hub.release_hold(hub.holds()[0]["id"], actor_tg_id=4242)
    assert out.get("ticket_id") and hub.holds() == [] and hub.whatsapp.automatic_count() == 1


def test_stt_failure_is_held_and_triaged():
    class DeadSTT:
        calls = []

        def transcribe(self, audio_id, *, lang_hint=""):
            return {"status": "blocked", "why": "no key", "text": None}

    hub = build_hub(stt=DeadSTT())
    res = deliver(hub, "webhook_voice.json")
    assert res["held"] == "stt_unavailable" and hub.telegram.triage and _creates(hub) == 0


def test_media_only_first_contact_is_held_for_a_human():
    p = load("webhook_text.json")
    m = p["entry"][0]["changes"][0]["value"]["messages"][0]
    m.pop("text")
    m.update({"type": "image", "image": {"id": "IMG1"}})
    hub = build_hub()
    assert deliver(hub, p)["held"] == "empty_message" and _creates(hub) == 0 and hub.telegram.triage


# ---------------------------------------------------------------- #13 batching, #14 media replies

def test_a_batched_webhook_processes_every_message():
    p = load("webhook_text.json")
    v = p["entry"][0]["changes"][0]["value"]
    other = copy.deepcopy(v["messages"][0])
    other.update({"id": "wamid.B", "from": "201000000002"})
    v["messages"].append(other)
    v["contacts"].append({"profile": {"name": "Riley Client"}, "wa_id": "201000000002"})
    hub = build_hub()
    res = deliver(hub, p)
    assert len(res["batch"]) == 2 and _creates(hub) == 2
    assert {t.wa_id for t in hub.all_tickets()} == {PHONE, "201000000002"}


def test_media_reply_is_attached_and_asks_the_owner_to_confirm():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    approve(hub, "confirm")
    p = load("webhook_confirm.json")
    m = p["entry"][0]["changes"][0]["value"]["messages"][0]
    m.pop("text")
    m.update({"id": "img1", "type": "image", "image": {"id": "MEDIA_IMG_1", "caption": "here is the screenshot",
                                                       "mime_type": "image/png"}})
    res = deliver(hub, p)
    st = hub.latest_ticket(PHONE)
    assert res.get("attached") is True and st.stage == "awaiting_owner_advance"
    assert st.replies[0]["kind"] == "media" and "MEDIA_IMG_1" in st.replies[0]["text"]
    notes = [w for w in hub.odoo.t.writes if w["method"] == "message_post"]
    assert any("image" in (w["kwargs"].get("body") or "") for w in notes)


def test_voice_reply_is_transcribed_and_attached():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    approve(hub, "confirm")
    p = load("webhook_voice.json")
    p["entry"][0]["changes"][0]["value"]["messages"][0]["id"] = "voice-reply-1"
    deliver(hub, p)
    st = hub.latest_ticket(PHONE)
    assert st.stage == "awaiting_owner_advance" and "المخزون" in st.replies[0]["text"] and hub.stt.calls


# ---------------------------------------------------------------- minor: escaping + language

def test_client_text_is_html_escaped_in_the_odoo_ticket():
    p = load("webhook_text.json")
    p["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = "stock wrong <script>alert(1)</script>"
    hub = build_hub()
    deliver(hub, p)
    create = [w for w in hub.odoo.t.writes if w["method"] == "create"][0]
    assert "<script>" not in create["vals"]["description"] and "&lt;script&gt;" in create["vals"]["description"]


def test_arabic_flow_has_no_english_fragments_in_client_messages():
    import re
    hub = build_hub()
    deliver(hub, "webhook_voice.json")
    approve(hub, "confirm")
    for s in hub.whatsapp.sent:
        words = [w for w in re.findall(r"[A-Za-z]{2,}", s["text"]) if w not in ("Acme", "Oscar", "Client", "Supervisor")]  # proper names allowed
        assert words == [], s["text"]


# ---------------------------------------------------------------- #8: a failed Telegram approval request is retryable

class FlakyApprovalTelegram(fakes.FakeTelegram):
    def __init__(self, fail_times=1):
        super().__init__()
        self.fail_times = fail_times

    def request_approval(self, topic_id, **kw):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("telegram down")
        return super().request_approval(topic_id, **kw)


def test_first_contact_with_a_failed_approval_request_resumes_and_asks_the_owner():
    hub = build_hub(telegram=FlakyApprovalTelegram())
    res = deliver(hub, "webhook_text.json")
    assert res.get("error") and _creates(hub) == 1
    assert pending(hub, "confirm") is None
    out = hub.retry_failed()
    st = hub.latest_ticket(PHONE)
    assert len(out) == 1 and out[0].get("ticket_id") == st.tid
    assert pending(hub, "confirm") is not None and st.stage == "awaiting_owner" and st.replies == []
    assert _creates(hub) == 1 and hub.whatsapp.automatic_count() == 1


def test_reply_with_a_failed_approval_request_is_retried_and_the_owner_is_asked_to_advance():
    tg = FlakyApprovalTelegram(fail_times=0)
    hub = build_hub(telegram=tg)
    deliver(hub, "webhook_text.json")
    approve(hub, "confirm")
    tg.fail_times = 1
    res = deliver(hub, msg("rep1", "yes that is right"))
    assert res.get("error") and pending(hub, "advance") is None
    hub.retry_failed()
    st = hub.latest_ticket(PHONE)
    assert pending(hub, "advance") is not None and st.stage == "awaiting_owner_advance"
    assert [r["message_id"] for r in st.replies] == ["rep1"]                   # attached exactly once


# ---------------------------------------------------------------- #9 / F1: persist first, process later

def test_accepted_message_is_durable_before_it_is_processed():
    hub = build_hub()
    assert hub.accept(load("webhook_text.json")) == 1                       # persisted ... no worker has run
    assert hub.store.get("messages", "wamid.TEXT0001")["status"] == "queued"
    assert _creates(hub) == 0 and hub.whatsapp.sent == []
    out = hub.retry_failed()                                                # the process "restarted"; drain the queue
    assert len(out) == 1 and out[0].get("ticket_id") and _creates(hub) == 1
    assert hub.whatsapp.automatic_count() == 1
    assert hub.accept(load("webhook_text.json")) == 0                       # a replay is not queued again
