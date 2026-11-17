"""Teamhub ticket threads: one thread per ticket, privacy, approvals, close. Fakes only, no network."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from twin_agent.hub import odoo_ticket_watch as w
from twin_agent.hub.teamhub_threads import (ConfigError, OverlayStore, Settings, process_submissions, refusal_reason,
                                           sha)
from teamhub_helpers import (EXECUTIVE, SYSADMIN, STRANGER, TEST_TEXT, later, make_ctx, make_settings, press, ticket)

SECRET_WORDS = "Mr Kurt Fathy invoice 5521"        # stands in for a real client's words


def opened(tmp_path, **kw):
    """A ctx with one TEST ticket already opened (thread + 3 replies + card)."""
    ctx, agent, odoo, clock = make_ctx(tmp_path, [ticket(5)], **kw)
    assert w.watch_once(ctx)["opened"] == [5]
    return ctx, agent, odoo, clock, ctx.approvals.for_ticket(5)


# ------------------------------------------------------------------------------------------------ threads

def test_one_thread_per_ticket_across_reruns(tmp_path):
    ctx, agent, odoo, _ = make_ctx(tmp_path, [ticket(5)])
    w.watch_once(ctx)
    w.watch_once(ctx)
    ctx.store.put("teamhub_state", "watch", {"last_id": 0})          # even a lost watermark opens nothing new
    w.watch_once(ctx)
    assert len(agent.of("create_thread")) == 1
    assert [o["key"] for o in agent.of("reply")] == ["t5-analyst", "t5-functional", "t5-coordinator", "t5-draft"]
    assert len(agent.of("card")) == 1
    assert all(m == ("helpdesk.ticket", "search_read") or m[1] in ("fields_get", "search_read") for m in odoo.calls)
    assert odoo.writes == []


def test_crash_after_root_resumes_without_a_second_thread(tmp_path):
    ctx, agent, _, _ = make_ctx(tmp_path, [ticket(5)])
    ctx.queue.run([{"key": "t5-root", "op": "create_thread", "text": "x"}])      # the poster made it; we "crashed"
    w.watch_once(ctx)
    assert len(agent.of("create_thread")) == 1                                     # the result file was reused
    assert ctx.threads.get(5)["root_id"] == "root1"


def test_dry_run_writes_nothing(tmp_path):
    lines: list[str] = []
    ctx, agent, _, _ = make_ctx(tmp_path, [ticket(5)], dry_run=True)
    ctx.queue.out = lines.append
    ctx.store = OverlayStore()
    ctx.threads.store = ctx.approvals.store = ctx.store
    summary = w.watch_once(ctx)
    assert summary["dry_run"] and summary["opened"] == [5]
    assert not Path(ctx.st.queue_dir).exists() and not Path(ctx.st.results_dir).exists()
    assert not Path(ctx.st.state_dir).exists() and agent.triggers == 0
    assert any(l.startswith("WOULD QUEUE") for l in lines)


def test_poster_down_never_double_queues(tmp_path):
    ctx, agent, _, _ = make_ctx(tmp_path, [ticket(5)], down=99)
    s1 = w.watch_once(ctx)
    assert s1["retry"] == [5] and agent.triggers == 3                              # first try + two retries
    assert len(list(Path(ctx.st.queue_dir).glob("*.json"))) == 1                    # one queued file
    agent.down = 0
    agent.triggers = 0
    w.watch_once(ctx)
    assert len(agent.of("create_thread")) == 1 and ctx.threads.get(5)["root_id"]


def test_watch_refuses_with_no_filter(tmp_path):
    ctx, agent, odoo, _ = make_ctx(tmp_path, [ticket(5)], filter={})
    with pytest.raises(ConfigError):
        w.watch_once(ctx)
    assert odoo.calls == [] and agent.triggers == 0


def test_settings_need_room_and_workflow():
    with pytest.raises(ConfigError):
        Settings.from_cfg({"teamhub_tickets": {"workflow_id": "x"}})


# ------------------------------------------------------------------------------------------------ privacy

@pytest.mark.parametrize("text", ["server 10.20.30.40 is down", "key sk-abcdefghijklmnop1234", "call +20 100 123 4567",
                                  "mail a.person@personal.example.org please", "x" * 3600, "  "])
def test_pii_guard_refuses(tmp_path, text):
    ctx, agent, _, _ = make_ctx(tmp_path, [])
    res = ctx.queue.run([{"key": "t1-root", "op": "create_thread", "text": text}])
    assert res[0]["refused"] and not Path(ctx.st.queue_dir).exists() and agent.triggers == 0


def test_pii_guard_allows_team_email_and_ignores_button_values():
    assert refusal_reason({"op": "reply", "text": "ask info@example.com"}) is None
    card = {"op": "card", "fallback_text": "ok", "blocks": [], "actions": [{"value": "sk-abcdefghijklmnop1234"}]}
    assert refusal_reason(card) is None


def test_ticket_with_a_phone_number_opens_no_thread(tmp_path):
    ctx, agent, _, _ = make_ctx(tmp_path, [ticket(5, text=TEST_TEXT + " Call me on +20 100 123 4567")])
    assert w.watch_once(ctx)["refused"] == [5]
    assert agent.ops == [] and "t5-root" in ctx.threads.get(5)["refused"]
    w.watch_once(ctx)
    assert agent.ops == []                                       # not retried forever


def test_full_text_only_for_test_tickets(tmp_path):
    real = ticket(6, name="Initech invoice problem", text=TEST_TEXT + " " + SECRET_WORDS, partner=12)
    test = ticket(7)
    ctx, agent, _, _ = make_ctx(tmp_path, [real, test], filter={"team_id": 2})   # a widened filter
    w.watch_once(ctx)
    posted = {o["key"]: o for o in agent.ops}
    mine = json.dumps([o for o in agent.ops if o["key"].startswith("t6-")])
    assert not any(w_ in mine for w_ in ("5521", "Kurt", "Fathy", "quotation report", "print it"))
    assert "Summary:" in posted["t6-root"]["text"] and "Client #12" in posted["t6-root"]["text"]
    assert "t6-card" not in posted                                # real ticket: no card, so no client text in Teamhub
    assert TEST_TEXT in posted["t7-root"]["text"] and "t7-card" in posted


def test_card_is_simple_holds_no_client_text_and_the_draft_comes_just_before_it(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    card = agent.of("card")[0]
    assert card["target_user"] == SYSADMIN and "fingerprint" in card["fallback_text"]
    assert not any(b["type"] == "field_group" for b in card["blocks"])           # no form that could block the click
    assert doc["draft"] not in json.dumps(card, ensure_ascii=False)
    keys = [o["key"] for o in agent.ops]
    assert keys.index("t5-draft") == keys.index("t5-card") - 1                     # the draft reply is right before the card
    assert doc["draft"] in agent.of("reply")[3]["text"] and "fingerprint" in agent.of("reply")[3]["text"]
    assert [a["actionId"] for a in card["actions"]] == ["approve", "edit", "reject"]
    assert {a["value"] for a in card["actions"]} == {doc["nonce"]}
    mentions = [o.get("mentions") for o in agent.of("reply")]
    assert mentions == [None, None, [SYSADMIN], None]                # only the approver, only on the ask


def test_M1_no_queue_file_with_a_nonce_holds_any_ticket_text(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    _, _, edit = start_edit(ctx, agent, doc)
    repost_card(ctx, 5)
    nonces = [d["nonce"] for d in ctx.store.scan("teamhub_approvals")]
    body = [TEST_TEXT, "quotation", "print it", doc["draft"], doc["draft"].split(",")[1]]
    with_nonce = [o for o in agent.ops if any(n in json.dumps(o) for n in nonces)]
    assert len(with_nonce) >= 3 and all(o["op"] == "card" for o in with_nonce)         # cards, the edit card, the repost
    for o in with_nonce:
        assert not any(b_ in json.dumps(o, ensure_ascii=False) for b_ in body), o["key"]
    assert any("quotation" in json.dumps(o) for o in agent.of("reply"))                # the text is in nonce-free replies


# ------------------------------------------------------------------------------------------------ approvals

def run_press(ctx, agent, doc, action, **kw):
    mid = kw.pop("mid", doc["card_message_id"])
    press(ctx.st, "p", mid=mid, action=action, nonce=doc["nonce"], **kw)
    out = process_submissions(ctx)
    return out[-1], agent.of("reply")[-1]["text"]


def test_approve_records_and_never_sends(tmp_path):
    ctx, agent, odoo, _, doc = opened(tmp_path)
    out, reply = run_press(ctx, agent, doc, "approve", text=doc["draft"])
    d = ctx.approvals.get(doc["nonce"])
    assert out["reason"] is None and d["decision"] == "approved" and d["used_by"] == SYSADMIN
    assert d["approved_hash"] == sha(doc["draft"]) and "Approved by Sysadmin" in reply and "nothing was sent" in reply
    assert odoo.writes == []
    assert list(Path(ctx.st.results_dir, "processed").glob("submission-*")) and not list(
        Path(ctx.st.results_dir).glob("submission-*"))
    assert ctx.threads.get(5)["decision"]["decision"] == "approved"


def test_wrong_user_is_refused_and_does_not_burn_the_nonce(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    out, reply = run_press(ctx, agent, doc, "approve", who=STRANGER)
    assert out["reason"] == "not_authorised" and "Press refused" in reply
    assert ctx.approvals.get(doc["nonce"])["used_at"] is None
    out, _ = run_press(ctx, agent, doc, "approve", stamp="2")                 # the real approver can still press
    assert out["reason"] is None


def test_missing_submitter_is_refused_in_the_thread(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    out, reply = run_press(ctx, agent, doc, "approve", who=None)
    assert out["reason"] == "no_submitter" and "did not say who pressed" in reply
    assert ctx.approvals.get(doc["nonce"])["used_at"] is None


def test_replayed_nonce_is_refused(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    run_press(ctx, agent, doc, "approve")
    out, reply = run_press(ctx, agent, doc, "approve", who=EXECUTIVE, stamp="2")
    assert out["reason"] == "used" and "already used" in reply


def test_a_submission_file_is_handled_once(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    p = press(ctx.st, "p", mid=doc["card_message_id"], action="approve", nonce=doc["nonce"])
    raw = p.read_text(encoding="utf-8")
    process_submissions(ctx)
    n = len(agent.ops)
    p.write_text(raw, encoding="utf-8")                                        # the same file reappears
    out = process_submissions(ctx)
    assert out[0]["reason"] is None and len(agent.ops) == n                    # decided once, posted once
    assert ctx.approvals.get(doc["nonce"])["decision"] == "approved"


def test_expired_is_refused(tmp_path):
    ctx, agent, _, clock, doc = opened(tmp_path)
    later(clock, hours=25)
    out, reply = run_press(ctx, agent, doc, "approve")
    assert out["reason"] == "expired" and "expired" in reply


def start_edit(ctx, agent, doc, **kw):
    out, reply = run_press(ctx, agent, doc, "edit", **kw)
    edit = [d for d in ctx.store.scan("teamhub_approvals") if d.get("parent") == doc["nonce"]]
    return out, reply, (edit[0] if edit else None)


def test_edit_is_two_steps_and_the_edited_text_becomes_the_approved_text(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    out, _, edit = start_edit(ctx, agent, doc)
    replies = [o["text"] for o in agent.of("reply")]
    assert out["reason"] is None and any("Edit requested by Sysadmin" in t for t in replies)
    d = ctx.approvals.get(doc["nonce"])
    assert d["decision"] == "edit_requested" and d["used_by"] == SYSADMIN and d["approved_text"] is None
    card = agent.of("card")[-1]                                              # a NEW card, same approver, new nonce
    assert card["target_user"] == SYSADMIN and edit["nonce"] != doc["nonce"] and edit["allowed"] == [SYSADMIN]
    assert replies[-1].startswith("[Coordinator] Proposed reply") and doc["draft"] in replies[-1]    # text just before it
    area = [b for b in card["blocks"] if b["type"] == "field_group"][0]["fields"][0]
    assert area["fieldId"] == "text" and "defaultValue" not in area                # no client text in the nonce file
    assert [a["actionId"] for a in card["actions"]] == ["approve_edited"] and card["actions"][0]["value"] == edit["nonce"]
    new = "Hello, we got your request and will look at the quotation report today."
    out, reply = run_press(ctx, agent, edit, "approve_edited", text=new)
    e = ctx.approvals.get(edit["nonce"])
    assert out["reason"] is None and e["decision"] == "edited" and e["approved_text"] == new
    assert e["approved_hash"] == sha(new) != e["draft_hash"] and e["used_by"] == SYSADMIN and "Edited and approved" in reply
    assert ctx.threads.get(5)["decision"]["decision"] == "edited"


def test_edit_card_checks_the_text_and_the_approver(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    _, _, edit = start_edit(ctx, agent, doc)
    assert run_press(ctx, agent, edit, "approve_edited", text="call +20 100 123 4567")[0]["reason"] == "edit_sensitive"
    assert run_press(ctx, agent, edit, "approve_edited", text="  ", stamp="2")[0]["reason"] == "empty_text"
    assert run_press(ctx, agent, edit, "approve_edited", text="fill [[name]]", stamp="3")[0]["reason"] == "placeholder_left"
    assert run_press(ctx, agent, edit, "approve_edited", text="fine", who=EXECUTIVE, stamp="4")[0]["reason"] == "not_authorised"
    assert run_press(ctx, agent, edit, "approve", stamp="5")[0]["reason"] == "unknown_action"
    assert ctx.approvals.get(edit["nonce"])["used_at"] is None


def test_the_original_card_cannot_approve_an_edit(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    assert run_press(ctx, agent, doc, "approve_edited", text="x")[0]["reason"] == "unknown_action"


def test_approve_with_changed_text_must_use_edit(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    out, _ = run_press(ctx, agent, doc, "approve", text=doc["draft"] + " extra")
    assert out["reason"] == "text_changed_use_edit"


def test_press_from_another_card_is_refused(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    assert run_press(ctx, agent, doc, "approve", mid="some-other-card")[0]["reason"] == "wrong_card"


def test_reject_records_and_ignores_text(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    out, reply = run_press(ctx, agent, doc, "reject", text="whatever")
    d = ctx.approvals.get(doc["nonce"])
    assert out["reason"] is None and d["decision"] == "rejected" and d["approved_text"] is None and "Rejected by" in reply


def test_unknown_nonce_and_garbage_files_are_harmless(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    press(ctx.st, "p", mid="m", action="approve", nonce="nope")
    Path(ctx.st.results_dir, "submission-bad-1.json").write_text("{not json", encoding="utf-8")
    n = len(agent.ops)
    out = process_submissions(ctx)
    assert out[0]["reason"] == "unknown_approval" and len(agent.ops) == n


# ------------------------------------------------------------------------------------------------ close

def test_close_sequence_runs_once(tmp_path):
    ctx, agent, odoo, _, doc = opened(tmp_path)
    run_press(ctx, agent, doc, "approve")
    odoo.tickets[5]["stage_id"] = [3, "Solved"]
    s = w.watch_once(ctx)
    assert s["closed"] == [5]
    closed = [o for o in agent.ops if o["key"].startswith("t5-closed")]
    assert [o["op"] for o in closed] == ["reply", "edit_root", "mute"]               # in this order
    assert "[Closed]" in closed[0]["text"] and "approved by Sysadmin" in closed[0]["text"]
    assert closed[1]["text"].startswith("[CLOSED] Ticket #5") and closed[1]["message"] == "root1"
    assert ctx.threads.get(5)["closed"] is True
    n = len(agent.ops)
    assert w.watch_once(ctx)["closed"] == [] and len(agent.ops) == n               # not again


def test_a_late_press_after_close_is_refused(tmp_path):
    ctx, agent, odoo, _, doc = opened(tmp_path)
    odoo.tickets[5]["stage_id"] = [3, "Solved"]
    w.watch_once(ctx)
    assert "no reply draft was decided" in [o for o in agent.ops if o["key"] == "t5-closed"][0]["text"]
    out, _ = run_press(ctx, agent, doc, "approve")
    assert out["reason"] == "used"


def test_a_ticket_already_closed_when_first_seen_gets_no_thread(tmp_path):
    ctx, agent, _, _ = make_ctx(tmp_path, [ticket(5, stage=3)])
    assert w.watch_once(ctx)["skipped"] == [5] and agent.ops == []


def test_close_resumes_after_a_partial_failure(tmp_path):
    ctx, agent, odoo, _, _ = opened(tmp_path)
    odoo.tickets[5]["stage_id"] = [3, "Solved"]
    agent.down = agent.triggers + 99
    assert w.watch_once(ctx)["retry"] == [5] and not ctx.threads.get(5)["closed"]
    agent.down = 0
    assert w.watch_once(ctx)["closed"] == [5]
    assert len([o for o in agent.ops if o["key"] == "t5-closed"]) == 1


def test_owner_mapping_picks_the_card_target(tmp_path):
    ctx, agent, _, _ = make_ctx(tmp_path, [ticket(5, owner=9)], owner_teamhub_ids={"9": EXECUTIVE})
    w.watch_once(ctx)
    assert agent.of("card")[0]["target_user"] == EXECUTIVE
    assert "Owner: Owner (set in Odoo)" in agent.of("reply")[2]["text"]


# ------------------------------------------------------------------------------------------------ trust_scoped_card_target

def test_press_without_user_id_refused_when_flag_off(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    assert run_press(ctx, agent, doc, "approve", who=None)[0]["reason"] == "no_submitter"


def test_press_without_user_id_accepted_when_flag_on_and_recorded_as_scoping(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path, trust_scoped_card_target=True)
    out, reply = run_press(ctx, agent, doc, "approve", who=None)
    d = ctx.approvals.get(doc["nonce"])
    assert out["reason"] is None and d["used_by"] == SYSADMIN and d["identity"] == "teamhub_scoping"
    assert ctx.threads.get(5)["decision"]["identity"] == "teamhub_scoping" and "card scoping" in reply


def test_explicit_id_is_recorded_as_explicit(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path, trust_scoped_card_target=True)
    run_press(ctx, agent, doc, "approve")
    assert ctx.approvals.get(doc["nonce"])["identity"] == "explicit"


def test_flag_on_needs_the_target_on_the_allowlist(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path, trust_scoped_card_target=True, approver_allowlist=[EXECUTIVE])
    assert run_press(ctx, agent, doc, "approve", who=None)[0]["reason"] == "no_submitter"


# ------------------------------------------------------------------------------------------------ review round 1

from twin_agent.hub.teamhub_threads import Refused, _clear_failed, single_instance, stage_flag      # noqa: E402


def test_flag_on_an_allowlisted_other_approver_is_accepted_as_explicit(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path, trust_scoped_card_target=True)
    out, _ = run_press(ctx, agent, doc, "approve", who=EXECUTIVE)
    d = ctx.approvals.get(doc["nonce"])
    assert out["reason"] is None and d["used_by"] == EXECUTIVE and d["identity"] == "explicit"


def test_flag_on_a_stranger_is_refused(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path, trust_scoped_card_target=True)
    out, _ = run_press(ctx, agent, doc, "approve", who=STRANGER)
    assert out["reason"] == "not_authorised" and ctx.approvals.get(doc["nonce"])["used_at"] is None


@pytest.mark.parametrize("kw", [{"partner": 12}, {"partner": 11}])          # a real person / a real company
def test_B1_test_name_with_a_real_partner_shows_no_client_text(tmp_path, kw):
    ctx, agent, odoo, _ = make_ctx(tmp_path, [ticket(5, text=TEST_TEXT + " Nina Fathy", **kw)])
    w.watch_once(ctx)
    posted = json.dumps(agent.ops)
    assert "Nina" not in posted and "quotation" not in posted and "Some Person" not in posted
    assert "Summary:" in agent.of("create_thread")[0]["text"] and agent.of("card") == []


@pytest.mark.parametrize("field", ["email", "phone", "mobile"])
def test_B1_test_contact_with_contact_details_is_not_trusted(tmp_path, field):
    ctx, agent, odoo, _ = make_ctx(tmp_path, [ticket(5)])
    odoo.partners[10][field] = "x"
    w.watch_once(ctx)
    assert TEST_TEXT not in json.dumps(agent.ops) and agent.of("card") == []


def test_B1_configured_test_partner_id_must_match(tmp_path):
    ctx, agent, odoo, _ = make_ctx(tmp_path, [ticket(5)], test_partner_id=999)
    w.watch_once(ctx)
    assert TEST_TEXT not in json.dumps(agent.ops) and agent.of("card") == []


def test_H1_a_swallowed_post_is_never_posted_twice(tmp_path):
    ctx, agent, _, _ = make_ctx(tmp_path, [ticket(5)], agent_kw={"swallow": True})
    assert w.watch_once(ctx)["retry"] == [5]
    for _ in range(3):
        ctx.queue.down = False                                   # a new process each time
        w.watch_once(ctx)
    assert [o["key"] for o in agent.ops].count("t5-root") == 1
    assert "unknown" in ctx.threads.get(5)["failed"] and ctx.queue._marked("t5-root")
    ctx.queue.clear_marker("t5-root")                            # an operator checked: it was NOT posted
    _clear_failed(ctx, "t5-root")
    agent.swallow = False
    ctx.queue.down = False
    w.watch_once(ctx)
    assert ctx.threads.get(5)["root_id"]


def test_H2_failed_posts_are_capped_and_other_tickets_go_on(tmp_path):
    ctx, agent, _, _ = make_ctx(tmp_path, [ticket(5), ticket(6)], agent_kw={"fail_prefix": "t5-"})
    for _ in range(5):
        ctx.queue.down = False
        s = w.watch_once(ctx)
    keys = [o["key"] for o in agent.ops if o["key"].startswith("t5-")]
    assert keys == ["t5-root", "t5-root~1", "t5-root~2"]                   # the first try + two retries, then no more
    assert "failed 3 times" in ctx.threads.get(5)["failed"]
    assert ctx.threads.get(6)["root_id"] and len(agent.of("card")) == 1       # ticket 6 was not blocked


def test_L2_a_result_without_an_id_does_not_crash(tmp_path):
    ctx, agent, _, _ = make_ctx(tmp_path, [ticket(5)], agent_kw={"no_ids": True})
    s = w.watch_once(ctx)
    assert s["failed"] == [5] and "no root_id" in ctx.threads.get(5)["failed"]


def test_M5_dead_gateway_refuses_the_whole_run_at_once(tmp_path):
    ctx, agent, _, _ = make_ctx(tmp_path, [ticket(5)])
    agent.gateway_up = False
    with pytest.raises(Refused, match="gateway is down"):
        ctx.queue.check_gateway()
    assert agent.triggers == 0 and not Path(ctx.st.queue_dir).exists()


def test_M5_after_the_first_outage_the_rest_of_the_run_fails_fast(tmp_path):
    ctx, agent, _, _ = make_ctx(tmp_path, [ticket(5), ticket(6)], down=99)
    w.watch_once(ctx)
    assert agent.triggers == 3                                   # one ticket waited; the other did not wait again


def test_M5_only_one_watcher_at_a_time(tmp_path):
    with single_instance(str(tmp_path / "s")):
        with pytest.raises(Refused, match="already running"):
            with single_instance(str(tmp_path / "s")):
                pass
    with single_instance(str(tmp_path / "s")):                    # released afterwards
        pass


@pytest.mark.parametrize("text", ["call 0501234567", "010.1234.5678", "card 4111 1111 1111 1111", "a@example.com.evil.example.net",
                                  "Bearer abcdefghijklmnopqrstuvwxyz123456", "password: Hunter2!",
                                  "-----BEGIN RSA PRIVATE KEY-----x-----END RSA PRIVATE KEY-----", "id 29801011234567"])
def test_M4_every_listed_shape_is_refused(text):
    assert refusal_reason({"op": "reply", "text": text})


def test_M4_scanner_runs_on_card_blocks_and_fallback_and_team_email_still_passes():
    card = {"op": "card", "fallback_text": "ok", "blocks": [{"type": "markdown", "content": "call 0501234567"}], "actions": []}
    assert refusal_reason(card)
    assert refusal_reason({"op": "card", "fallback_text": "mail me at x@personal.example.org", "blocks": []})
    assert refusal_reason({"op": "reply", "text": "ask info@example.com. Expires 2026-11-16T10:30:00+00:00"}) is None


def test_M2_one_shared_closed_stage_rule(tmp_path):
    assert stage_flag({"is_close": {}, "fold": {}}) == "fold" and stage_flag({"is_close": {}}) == "is_close"
    ctx, _, odoo, _ = make_ctx(tmp_path, [ticket(5)])
    assert set(w.closed_stages(ctx.odoo)) == {3}                  # stage 4 is_close but not folded: not closed


def test_L1_an_expired_never_posted_card_gets_a_new_nonce(tmp_path):
    ctx, agent, _, clock = make_ctx(tmp_path, [ticket(5)], agent_kw={"fail_keys": ["t5-card"]})
    w.watch_once(ctx)
    old = ctx.threads.get(5)["card"]["nonce"]
    later(clock, hours=25)
    ctx.queue.down = False
    w.watch_once(ctx)
    new = ctx.threads.get(5)["card"]
    assert new["nonce"] != old and new["message_id"] and ctx.approvals.get(new["nonce"])["card_message_id"]


def test_L3_a_crash_after_deciding_still_reports_the_approval(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    p = press(ctx.st, "p", mid=doc["card_message_id"], action="approve", nonce=doc["nonce"])
    ctx.approvals.decide(doc["nonce"], SYSADMIN, "approve", None, doc["card_message_id"], "explicit", p.stem)   # then "crash"
    out = process_submissions(ctx)
    assert out[0]["reason"] is None and "Approved by Sysadmin" in agent.of("reply")[-1]["text"]


def test_existing_state_without_the_new_fields_still_works(tmp_path):
    """A thread/approval record written by the first version (no failed / press_id / identity) keeps working."""
    ctx, agent, odoo, _, doc = opened(tmp_path)
    ctx.store.update("teamhub_threads", "5", lambda d: {k: v for k, v in d.items() if k != "failed"})
    ctx.store.update("teamhub_approvals", doc["nonce"], lambda d: {k: v for k, v in d.items() if k not in ("identity", "press_id")})
    n = len(agent.ops)
    assert w.watch_once(ctx)["failed"] == [] and len(agent.ops) == n
    out, _ = run_press(ctx, agent, doc, "approve")
    assert out["reason"] is None
    odoo.tickets[5]["stage_id"] = [3, "Solved"]
    assert w.watch_once(ctx)["closed"] == [5]


def test_nothing_outside_the_approval_module_reads_the_approved_text():
    root = Path(__file__).parent.parent / "twin_agent"
    users = {p.name for p in root.rglob("*.py") if "approved_text" in p.read_text(encoding="utf-8")}
    assert users == {"teamhub_threads.py"}


# ------------------------------------------------------------------------------------------------ repost-card, rooms

from twin_agent.hub.teamhub_threads import repost_card      # noqa: E402


def test_repost_card_revokes_the_old_card_and_replaces_it(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    old_mid = doc["card_message_id"]
    r = repost_card(ctx, 5)
    new = ctx.approvals.get(ctx.threads.get(5)["card"]["nonce"])
    card = agent.of("card")[-1]
    assert r["reposted"] and card["key"].startswith("t5-card-r1-") and card["replaces"] == old_mid
    assert new["nonce"] != doc["nonce"] and new["card_message_id"] and new["expires_at"] >= doc["expires_at"]
    assert ctx.approvals.get(doc["nonce"])["used_at"] == "revoked"
    out, reply = run_press(ctx, agent, doc, "approve")                                # the old card is dead
    assert out["reason"] == "revoked" and "newest card" in reply
    out, _ = run_press(ctx, agent, new, "approve", stamp="2")                         # the new one works
    assert out["reason"] is None
    assert ctx.threads.get(5)["card"]["repost_n"] == 1


def test_repost_card_again_uses_the_next_key(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    repost_card(ctx, 5)
    repost_card(ctx, 5)
    keys = [o["key"] for o in agent.of("card")]
    assert keys[0] == "t5-card" and keys[1].startswith("t5-card-r1-") and keys[2].startswith("t5-card-r2-")
    assert len(set(keys)) == 3
    assert agent.of("card")[-1]["replaces"] == ctx.threads.get(5)["card"]["replaces"]


def test_repost_card_dry_run_writes_nothing(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    dctx, *_ = make_ctx(tmp_path / "x", [], dry_run=True)
    dctx.store = dctx.threads.store = dctx.approvals.store = OverlayStore(ctx.store)
    dctx.queue.out = lambda *_: None
    n = len(agent.ops)
    r = repost_card(dctx, 5)
    assert r["dry_run"] and len(agent.ops) == n and ctx.approvals.get(doc["nonce"])["used_at"] is None


def test_repost_card_refuses_a_decided_card(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    run_press(ctx, agent, doc, "approve")
    with pytest.raises(Refused):
        repost_card(ctx, 5)


def test_per_person_rooms_are_config_only(tmp_path):
    rooms = {"9": {"room_id": "principals-room", "workflow_id": "principals-wf", "approver": EXECUTIVE}}
    ctx, agent, odoo, _ = make_ctx(tmp_path, [ticket(5, owner=9), ticket(6)], owner_rooms=rooms)
    cmds = []
    inner = ctx.queue.runner
    ctx.queue.runner = lambda cmd, **kw: (cmds.append(cmd), inner(cmd, **kw))[1]
    w.watch_once(ctx)
    assert ctx.threads.get(5)["route"]["room_id"] == "principals-room" and ctx.threads.get(6)["route"] is None
    assert any("principals-wf" in c and "principals-room" in c for c in cmds) and any("wf1" in c for c in cmds)
    assert [o for o in agent.of("card") if o["key"] == "t5-card"][0]["target_user"] == EXECUTIVE
    odoo.tickets[5]["stage_id"] = [3, "Solved"]
    cmds.clear()
    w.watch_once(ctx)
    assert ctx.threads.get(5)["closed"] and all("principals-wf" in c for c in cmds)       # close goes to the same room


def test_real_tickets_in_a_person_room_still_get_no_card(tmp_path):
    rooms = {"9": {"room_id": "r", "workflow_id": "wf", "approver": EXECUTIVE}}
    ctx, agent, _, _ = make_ctx(tmp_path, [ticket(5, owner=9, partner=12)], owner_rooms=rooms)
    w.watch_once(ctx)
    assert agent.of("card") == [] and TEST_TEXT not in json.dumps(agent.ops)


# ------------------------------------------------------------------------------------------------ re-review round

def test_MA_no_repost_and_no_second_decision_after_an_approved_edit(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    _, _, edit = start_edit(ctx, agent, doc)
    run_press(ctx, agent, edit, "approve_edited", text="Hello, we will check the quotation report today.")
    with pytest.raises(Refused, match="final decision"):
        repost_card(ctx, 5)
    other = ctx.approvals.issue(5, doc["draft"], SYSADMIN)                    # even a stray live card cannot add a decision
    ctx.approvals.attach_card(other["nonce"], "stray")
    out, _ = run_press(ctx, agent, other, "reject", stamp="9")
    assert out["reason"] == "ticket_decided" and ctx.threads.get(5)["decision"]["decision"] == "edited"


def test_MB_a_repeated_repost_with_a_pending_first_one_is_refused(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    agent.swallow = True                                                     # the first repost is taken, no result yet
    repost_card(ctx, 5)
    ctx.queue.down = False
    agent.swallow = False
    with pytest.raises(Refused, match="previous re-post"):
        repost_card(ctx, 5)
    assert ctx.approvals.get(ctx.threads.get(5)["card"]["nonce"])["used_at"] is None   # the live nonce was not revoked


def test_MB_the_card_key_carries_the_approval_id(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    repost_card(ctx, 5)
    card, nonce = agent.of("card")[-1], ctx.threads.get(5)["card"]["nonce"]
    assert card["key"].endswith(nonce[:8]) and card["actions"][0]["value"] == nonce


def test_MC_a_failed_ticket_does_not_hold_the_watermark(tmp_path):
    tickets = [ticket(10), ticket(100), ticket(101), ticket(102), ticket(103)]
    ctx, agent, _, _ = make_ctx(tmp_path, tickets, agent_kw={"fail_prefix": "t10-"}, batch=3)
    for _ in range(6):
        ctx.queue.down = False
        w.watch_once(ctx)
    assert all(ctx.threads.get(t) and ctx.threads.get(t)["root_id"] for t in (100, 101, 102, 103))   # nobody starved
    assert ctx.threads.get(10)["failed"]


def test_MC_clearing_a_failed_ticket_looks_at_it_again(tmp_path):
    ctx, agent, _, _ = make_ctx(tmp_path, [ticket(5), ticket(6)], agent_kw={"swallow": True})
    w.watch_once(ctx)
    ctx.queue.down = False
    w.watch_once(ctx)                                                         # ticket 5 is now failed (unknown)
    assert ctx.store.get("teamhub_state", "watch")["last_id"] >= 5
    agent.swallow = False
    ctx.queue.clear_marker("t5-root")
    _clear_failed(ctx, "t5-root")
    assert ctx.store.get("teamhub_state", "watch")["last_id"] <= 4
    ctx.queue.down = False
    w.watch_once(ctx)
    assert ctx.threads.get(5)["root_id"]


def test_MD_presses_in_a_routed_rooms_own_results_dir_are_processed(tmp_path):
    rdir = str(tmp_path / "principal-out")
    rooms = {"9": {"room_id": "r", "workflow_id": "wf", "approver": EXECUTIVE, "results_dir": rdir,
                   "queue_dir": str(tmp_path / "principal-q")}}
    ctx, agent, _, _ = make_ctx(tmp_path, [ticket(5, owner=9)], owner_rooms=rooms)
    w.watch_once(ctx)
    doc = ctx.approvals.for_ticket(5)
    assert doc["card_message_id"] and doc["target"] == EXECUTIVE
    sub = {"source_message_id": doc["card_message_id"], "action_id": "approve", "value": doc["nonce"],
           "submitter_user_id": EXECUTIVE, "submitted_values": {}}
    Path(rdir, "submission-m-1.json").write_text(json.dumps(sub), encoding="utf-8")
    out = process_submissions(ctx)
    assert out and out[0]["reason"] is None and ctx.approvals.get(doc["nonce"])["decision"] == "approved"
    assert list(Path(rdir, "processed").glob("submission-*"))


def test_L6_a_capped_reply_is_given_up_not_retried_every_run(tmp_path):
    ctx, agent, _, _, doc = opened(tmp_path)
    p = press(ctx.st, "p", mid=doc["card_message_id"], action="approve", nonce=doc["nonce"])
    agent.fail_prefix = "t5-press"
    for _ in range(4):
        ctx.queue.down = False
        process_submissions(ctx)
    assert not list(Path(ctx.st.results_dir).glob("submission-*.json")) and p.name


# ------------------------------------------------------------------------------------------------ security round

def test_M3_without_a_configured_test_partner_id_it_fails_closed(tmp_path):
    ctx, agent, _, _ = make_ctx(tmp_path, [ticket(5)], test_partner_id=0)
    w.watch_once(ctx)
    assert TEST_TEXT not in json.dumps(agent.ops) and agent.of("card") == [] and "Summary:" in agent.ops[0]["text"]


@pytest.mark.parametrize("text", ["pw: Hunter22", "PWD=abc12345", "كلمة السر: 12345abc", "الباسورد هي abc123",
                                  "كلمة المرور = xyz789", "mail john at example dot com", "john (at) example (dot) com",
                                  "john[at]example[dot]com"])
def test_L1_more_secret_and_email_shapes_are_refused(text):
    assert refusal_reason({"op": "reply", "text": text})


def test_the_poster_instructions_are_committed_and_referenced():
    root = Path(__file__).parent.parent
    assert (root / "ops" / "teamhub-poster-instructions.md").exists()
    assert "ops/teamhub-poster-instructions.md" in (root / "twin_agent" / "hub" / "teamhub_threads.py").read_text(encoding="utf-8")
