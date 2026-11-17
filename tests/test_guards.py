"""Tests for checks/guards.py, guard_text.py and fixgate.py (CHECKS)."""
from __future__ import annotations

from datetime import timedelta

import pytest

from twin_agent.checks import guards
from twin_agent.checks.guards import check_outbound, only_mode_blocks
from twin_agent.checks.guards_selftest import (PRINCIPAL, HOLD, MARKER, NOW, MiniDB, approval_for, ctx, live_cfg,
                                                msg)
from twin_agent.models import Channel, GuardResult, OutKind, SendTrigger
from twin_agent.testing.fakes import canned_packet


def blocked(m, c):
    return check_outbound(m, c).blocked_by


def test_selftest_all_pass(tmp_home):
    results = guards.selftest()
    assert results and all(ok for _, ok in results), [n for n, ok in results if not ok]


def test_clean_holding_passes(tmp_home):
    r = check_outbound(msg(HOLD), ctx())
    assert r.ok, r.warnings


def test_stop_file_blocks(tmp_home):
    (tmp_home / "STOP").write_text("")
    assert "kill_switch" in blocked(msg(HOLD), ctx())


def test_default_config_is_observe_and_only_mode_blocks(tmp_home, cfg):
    cfg["people"]["principal_tg_user_id"] = PRINCIPAL
    r = check_outbound(msg(HOLD), ctx(cfg))
    assert not r.ok
    assert {"send_disabled", "consent", "dial", "notice_gate"} & set(r.blocked_by)
    assert only_mode_blocks(r), r.blocked_by          # sender records would_send, not blocked
    bad = check_outbound(msg("Sorry\n" + MARKER), ctx(cfg))
    assert not only_mode_blocks(bad)


def test_only_mode_blocks_empty():
    assert not only_mode_blocks(GuardResult(ok=True))


@pytest.mark.parametrize("text,guard", [
    ("", "system_text"),
    ("NO_REPLY", "system_text"),
    ('{"ok": true}', "system_text"),
    ("Error: auth failed", "system_text"),
    ("ValueError: bad", "system_text"),
    ("call mcp__kb_search__search", "system_text"),
    ("Try again now – ok", "persona"),
    ("Done \U0001F44D", "persona"),
    ("Done!", "persona"),
    ("Sorry for the delay", "persona"),
    ("• step one", "persona"),
    ("Regards,", "persona"),
    ("مرحبا", "persona"),
    ("call 01012345678", "personal_data"),
    ("mail staff.a@example.com", "personal_data"),
    ("open https://evil.example.com/x", "links"),
    ("https://helpdesk.example.com/my/ticket/12/abcdef123456", "secrets"),
    ("token=abcdef123456", "secrets"),
    ("See #2641", "ticket_refs"),
    ("It will be fixed by Sunday", "promises"),
    ("Done by 2026-11-05", "promises"),
    ("guaranteed", "promises"),
    ("```python\nx=1\n```", "no_writes"),
    ("UPDATE stock_move SET state='done'", "no_writes"),
    ("set stage_id to done", "no_stage_moves"),
])
def test_text_guards_block(tmp_home, text, guard):
    body = text + ("\n" + MARKER if text else "")
    assert guard in blocked(msg(body), ctx())


def test_validation_error_ask_is_not_system_text(tmp_home):
    r = check_outbound(msg("Send the validation error please.\n" + MARKER), ctx())
    assert r.ok, r.warnings


def test_record_refs_are_not_dates_or_phones(tmp_home):
    text = "WAVE/00001 and IN/10001/26 and SO10001 are checked.\n" + MARKER
    assert check_outbound(msg(text), ctx()).ok


def test_allowed_link_without_token_passes(tmp_home):
    text = "Raised to Acme: https://helpdesk.example.com/my/ticket/2641\n" + MARKER
    assert check_outbound(msg(text), ctx()).ok


def test_template_filled_date_is_allowed_but_other_dates_not(tmp_home):
    text = "Hi,\n\nYou will have an update by Tuesday 2026-11-03.\n\n" + MARKER
    m = msg(text, kind=OutKind.email_holding, channel=Channel.email, in_reply_to="<a@b>", is_template=True,
            filled_date="Tuesday 2026-11-03", chat_id=None, topic_id=None)
    assert "promises" not in blocked(m, ctx())
    m.filled_date = None
    assert "promises" in blocked(m, ctx())


def test_autopilot_needs_marker_and_approved_text_must_not_have_it(tmp_home):
    assert "persona" in blocked(msg("Received."), ctx())
    text = "Try again now.\n" + MARKER
    m = msg(text, kind=OutKind.answer, step="d1", draft_hash=guards.draft_hash(text))
    assert "persona" in blocked(m, ctx(trigger=SendTrigger.approval, approval=approval_for(text)))


def test_persona_is_warning_on_principal_edit(tmp_home):
    text = "Done — try again"
    m = msg(text, kind=OutKind.answer, step="e1")
    r = check_outbound(m, ctx(trigger=SendTrigger.principal_edit, approval=approval_for(text, action="sd")))
    assert r.ok, r.blocked_by
    assert any(w.startswith("persona") for w in r.warnings)


def test_secrets_still_block_on_principal_edit(tmp_home):
    text = "key kc_abcdefghijklmnop1234"
    m = msg(text, kind=OutKind.answer, step="e1")
    assert "secrets" in blocked(m, ctx(trigger=SendTrigger.principal_edit, approval=approval_for(text, action="sd")))


def test_vault_value_hash_blocks(tmp_home):
    from twin_agent.redact import value_hash
    m = msg("here plainword12 ok\n" + MARKER)
    assert "secrets" in blocked(m, ctx(vault_hashes=(value_hash("plainword12"),)))


def test_approval_binding(tmp_home):
    text = "Try again now."
    m = msg(text, kind=OutKind.answer, step="d1", draft_hash=guards.draft_hash(text))
    assert "approval_binding" in blocked(m, ctx(trigger=SendTrigger.approval))                  # no approval
    a = approval_for(text)
    a["request_id"] = "REQ-0002"
    assert "approval_binding" in blocked(m, ctx(trigger=SendTrigger.approval, approval=a))
    a = approval_for("other text")
    assert "approval_binding" in blocked(m, ctx(trigger=SendTrigger.approval, approval=a))     # hash mismatch
    a = approval_for(text, action="wy")
    assert "approval_binding" in blocked(m, ctx(trigger=SendTrigger.approval, approval=a))
    m.text = "Try again now please."                                                          # edited after judging
    assert "approval_binding" in blocked(m, ctx(trigger=SendTrigger.approval, approval=approval_for(text)))


def test_dial_observe_blocks_and_draft_needs_tap(tmp_home):
    c = live_cfg()
    c["dial"]["L1_holding"] = "observe"
    assert "dial" in blocked(msg(HOLD), ctx(c))
    c["dial"]["L3_t1_answer"] = "draft"
    text = "Try again now."
    m = msg(text, kind=OutKind.answer, step="d1", draft_hash=guards.draft_hash(text))
    assert "dial" not in blocked(m, ctx(c, trigger=SendTrigger.approval, approval=approval_for(text)))
    auto = msg(text + "\n" + MARKER, kind=OutKind.answer, step="a1")
    assert "dial" in blocked(auto, ctx(c, trigger=SendTrigger.timeout))


def test_db_dial_can_only_tighten(tmp_home):
    class DB(MiniDB):
        def get_dial(self, lane):
            return "observe"
    assert "dial" in blocked(msg(HOLD), ctx(db=DB()))


def test_notice_gate_and_signatory(tmp_home):
    c = live_cfg()
    c["consent"]["notice_gate"]["executive_b2_ref"] = None
    assert "notice_gate" in blocked(msg(HOLD), ctx(c))
    c = live_cfg()
    c["consent"]["signatory_must_approve"] = True
    assert "notice_gate" in blocked(msg(HOLD), ctx(c))


def test_chat_and_topic_allow_list_and_thread_binding(tmp_home):
    assert "chat_allow_list" in blocked(msg(HOLD, topic_id=99), ctx())
    assert "chat_allow_list" in blocked(msg(HOLD), ctx(requester_is_allowed=False))
    assert "thread_binding" in blocked(msg(HOLD, reply_to=None), ctx())


def test_identity_human_active_already_answered(tmp_home):
    assert "identity" in blocked(msg(HOLD), ctx(requester_is_bot_or_staff_owner=True))
    assert "human_active" in blocked(msg(HOLD), ctx(principal_last_msg_at=NOW - timedelta(minutes=2)))
    assert "human_active" not in blocked(msg(HOLD), ctx(principal_last_msg_at=NOW - timedelta(minutes=6)))
    assert "already_answered" in blocked(msg(HOLD), ctx(thread_after_draft=({"is_principal": 1},)))


def test_rate_limits(tmp_home):
    def row(minutes_ago, rid="REQ-0001", kind="answer"):
        return {"ts": NOW - timedelta(minutes=minutes_ago), "status": "sent", "request_id": rid, "kind": kind,
                "chat_id": -100500}
    ans = msg("Try again now.\n" + MARKER, kind=OutKind.answer, step="a2")
    assert "rate_per_request_gap" in blocked(ans, ctx(trigger=SendTrigger.timeout, db=MiniDB([row(5)])))
    three = MiniDB([row(100), row(200), row(300)])
    assert "rate_per_request_day" in blocked(ans, ctx(trigger=SendTrigger.timeout, db=three))
    ten = MiniDB([row(i * 5, rid=f"REQ-{i:04d}") for i in range(2, 12)])
    assert "rate_per_chat_hour" in blocked(ans, ctx(trigger=SendTrigger.timeout, db=ten))
    many = MiniDB([row(i, rid=f"REQ-{i:04d}") for i in range(61, 101)])
    assert "rate_total_day" in blocked(ans, ctx(trigger=SendTrigger.timeout, db=many))


def test_ticket_email_guards(tmp_home):
    t = msg("Subject body", kind=OutKind.ticket_email, step="ticket-a", channel=Channel.email,
            to=["help@example.com"], chat_id=None, topic_id=None)
    assert check_outbound(t, ctx()).ok, check_outbound(t, ctx()).warnings
    t.to = ["someone@example.com"]
    assert "ticket_recipient" in blocked(t, ctx())
    t.to = ["help@example.com"]
    rows = [{"ts": NOW - timedelta(minutes=i), "status": "sent", "request_id": f"REQ-{i:04d}",
             "kind": "ticket_email", "send_key": f"REQ-{i:04d}:ticket-a"} for i in range(5)]
    assert "ticket_cap" in blocked(t, ctx(db=MiniDB(rows)))


def test_any_guard_crash_fails_closed(tmp_home):
    class Broken(MiniDB):
        def sends_since(self, *a, **k):
            raise RuntimeError("down")
    r = check_outbound(msg(HOLD), ctx(db=Broken()))
    assert not r.ok and "holding_once" in r.blocked_by
    assert all("down" not in w for w in r.warnings)


def test_scan_cli(tmp_path, capsys):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "ok.log").write_text("all fine\n", encoding="utf-8")
    assert guards.main(["--scan", str(logs)]) == 0
    (logs / "bad.jsonl").write_text('{"x": "bearer abcdefghijklmnopqrstuvwxyz123"}\n', encoding="utf-8")
    assert guards.main(["--scan", str(logs)]) == 1
    out = capsys.readouterr().out
    assert "bearer" in out and "abcdefghijklmnop" not in out


# ---------------------------------------------------------------- fix gate and verified

TEMPLATE = ('rec = env["stock.move"].browse({{move_id}})\n'
            'assert len(rec) == 1\n'
            'assert rec.product_uom_qty == 0\n'
            'rec.unlink()\n')
RUNBOOK = {"stray-in-line": {"title": "Stray IN line (orphan product)", "status": "confirmed 2026-07-26",
                             "template": TEMPLATE},
           "phantom-reservation": {"title": "Phantom reservation", "status": "provisional (drafted, unconfirmed)"}}


def fix_packet(records=("stock.move 1",)):
    p = canned_packet()
    p["proposed_action"]["records"] = list(records)
    return p


def test_exemption_happy_path():
    script = TEMPLATE.replace("{{move_id}}", "1")
    assert guards.recompute_exemption(fix_packet(), RUNBOOK, {"stray-in-line"}, script) == "exempt"


@pytest.mark.parametrize("change", ["not_b5", "two_records", "not_confirmed", "chained", "edited", "no_template",
                                    "injected_param", "no_script", "not_run_this"])
def test_exemption_routes_to_supervisor(change):
    packet, runbook, b5 = fix_packet(), {k: dict(v) for k, v in RUNBOOK.items()}, {"stray-in-line"}
    script = TEMPLATE.replace("{{move_id}}", "1")
    if change == "not_b5":
        b5 = set()
    elif change == "two_records":
        packet = fix_packet(("stock.move 1", "stock.move 2"))
    elif change == "not_confirmed":
        runbook["stray-in-line"]["status"] = "provisional"
    elif change == "chained":
        script = script + "rec.picking_id.write({'state': 'done'})\n"
    elif change == "edited":
        script = script.replace("== 0", "== 5")
    elif change == "no_template":
        runbook["stray-in-line"].pop("template")
    elif change == "injected_param":
        script = TEMPLATE.replace("{{move_id}}", "1); env.cr.execute('x'")
    elif change == "no_script":
        script = None
    elif change == "not_run_this":
        packet["proposed_action"]["type"] = "ui_steps"
    assert packet["proposed_action"]["approver"].startswith("exempt")      # the packet claims exempt
    assert guards.recompute_exemption(packet, runbook, b5, script) == "supervisor"


def test_fix_hash_reviewed_equals_run():
    script = TEMPLATE.replace("{{move_id}}", "1")
    sha = guards.fix_block_hash(script)
    assert guards.fix_hash_matches(script, sha)
    assert not guards.fix_hash_matches(script.replace("\n", "\r\n"), sha)   # bytes, no normalisation
    assert not guards.fix_hash_matches(script + " ", sha)
    assert not guards.fix_hash_matches(script, "")


def test_recompute_verified():
    p = canned_packet()
    p["evidence"] = [{"kind": "runbook", "id": "stray-in-line", "status": "confirmed"}]
    assert guards.recompute_verified(p, RUNBOOK, read_user_exists=False, judges_passed=True)
    assert not guards.recompute_verified(p, RUNBOOK, read_user_exists=False, judges_passed=False)
    p2 = canned_packet()                                   # has an odoo_read item, no read user yet
    assert not guards.recompute_verified(p2, RUNBOOK, read_user_exists=False, judges_passed=True)
    p3 = dict(p, evidence=[{"kind": "runbook", "id": "phantom-reservation", "status": "provisional"}])
    assert not guards.recompute_verified(p3, RUNBOOK, read_user_exists=False, judges_passed=True)
    p4 = dict(p, tier="T3")
    assert not guards.recompute_verified(p4, RUNBOOK, read_user_exists=False, judges_passed=True)
    p5 = dict(p, evidence=[{"kind": "past_answer"}], **{"class": "unknown"})
    assert not guards.recompute_verified(p5, RUNBOOK, read_user_exists=False, judges_passed=True)
    p6 = dict(p5, evidence=[{"kind": "past_answer"}, {"kind": "past_answer"}])
    assert guards.recompute_verified(p6, RUNBOOK, read_user_exists=False, judges_passed=True)
    assert not guards.recompute_verified({"garbage": 1}, RUNBOOK, read_user_exists=True, judges_passed=True)


def test_scan_ignores_session_uuids(tmp_path):
    import uuid
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "listener.log").write_text(chr(10).join(f"session {uuid.uuid4()} ok" for _ in range(3000)), encoding="utf-8")
    assert guards.scan_dir(logs) == []


def test_approve_lets_a_past_date_through_but_not_a_promise(tmp_home):
    """2026-11-10: "DLV/49549/26 already shows it delivered on 2 Nov" was blocked on Principal's Approve."""
    text = "BATCH/13918 stops: DLV/49549/26 already shows it delivered on 2 Nov."
    m = msg(text, kind=OutKind.answer, step="d1", draft_hash=guards.draft_hash(text))
    assert "promises" not in blocked(m, ctx(trigger=SendTrigger.approval, approval=approval_for(text)))
    assert "promises" in blocked(msg(text + chr(10) + MARKER), ctx())          # autopilot: dates still blocked
    promise = "It will be fixed by Sunday."
    m2 = msg(promise, kind=OutKind.answer, step="d1", draft_hash=guards.draft_hash(promise))
    assert "promises" in blocked(m2, ctx(trigger=SendTrigger.approval, approval=approval_for(promise)))
