"""BUILD 1: helpdesk tickets "like Supervisor" (guard, conventions, style, desk, dedupe, stage gate, Monitor rule)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from twin_agent.bus import Topics
from twin_agent.db import StateDB
from twin_agent.helpdesk import guard, style
from twin_agent.helpdesk.conventions import discover
from twin_agent.helpdesk.desk import HelpdeskDesk
from twin_agent.helpdesk.odoo import OdooAdapter, open_odoo
from twin_agent.monitor import alerts as al
from twin_agent.testing.fake_odoo import PRINCIPAL_EMPLOYEE, PRINCIPAL_UID, SUPERVISOR_UID, FakeOdoo
from twin_agent.testing.fakes import PRINCIPAL_ID, STAFF_A, canned_packet, make_event

SUPERVISOR_TG = 5550001
SAFE = {12, 14, 6, 18, 16, 9}


# ---------------------------------------------------------------- guard

@pytest.mark.parametrize("model,method,args,kw,why", [
    ("helpdesk.ticket", "unlink", [[1]], {}, "never"),
    ("ir.model.fields", "create", [{"name": "x_track"}], {}, "never"),
    ("ir.model.fields", "search_read", [[]], {}, "never"),
    ("helpdesk.ticket", "create", [{"name": "A", "stage_id": 14, "x_type_code": "1"}], {}, "x_type_code"),
    ("helpdesk.ticket", "write", [[1], {"x_type_code": "9"}], {}, "x_type_code"),
    ("helpdesk.ticket", "create", [{"name": "A", "stage_id": 14, "kanban_state": "done"}], {}, "allowlist"),
    ("helpdesk.ticket", "create", [{"name": "A"}], {}, "stage_id is required"),
    ("helpdesk.ticket", "create", [{"name": "A", "stage_id": 1}], {}, "emails the client"),
    ("helpdesk.ticket", "write", [[1], {"stage_id": 2}], {}, "emails the client"),
    ("helpdesk.ticket", "write", [[1, 2], {"stage_id": 14}], {}, "exactly one"),
    ("helpdesk.ticket", "write", [[1], {"partner_id": 5}], {}, "partner"),
    ("helpdesk.ticket", "write", [[1], {"team_id": 2}], {}, "team_id"),   # bug i: team move resets stage -> emails
    ("helpdesk.ticket", "write", [[1], {"x_task_ids": [(0, 0, {"name": "t"})]}], {}, "link commands"),
    ("helpdesk.ticket", "write", [[1], {"tag_ids": [(6, 0, [1])]}], {}, "link commands"),
    ("helpdesk.ticket", "message_post", [[1]], {"body": "x", "subtype_xmlid": "mail.mt_comment"}, "internal note"),
    ("helpdesk.ticket", "message_post", [[1]], {"body": "x", "subtype_xmlid": "mail.mt_note", "partner_ids": [3]},
     "internal notes only"),
    ("account.analytic.line", "create", [{"name": "x", "unit_amount": 1.0}], {}, "not allowed for purpose"),
    ("stock.move", "search_read", [[]], {}, "not on the allowlist"),
    ("project.task", "write", [[1], {"x_helpdesk_ticket_id": 1}], {}, "not allowed"),
])
def test_guard_refuses(model, method, args, kw, why):
    with pytest.raises(guard.WriteRefused, match=why):
        guard.check("helpdesk", model, method, args, kw, safe_stages=SAFE)


def test_guard_allows_the_narrow_paths():
    guard.check("helpdesk", "helpdesk.ticket", "create", [{"name": "Fix X", "stage_id": 14, "x_track": "functional",
                                                           "tag_ids": [(6, 0, [31])]}], safe_stages=SAFE)
    guard.check("helpdesk", "helpdesk.ticket", "write", [[5], {"stage_id": 9}], safe_stages=SAFE)
    guard.check("helpdesk", "helpdesk.ticket", "write", [[5], {"stage_id": 2}], safe_stages=SAFE, gated_stage=2)
    guard.check("helpdesk", "helpdesk.ticket", "write", [[5], {"x_task_ids": [(4, 77)]}], safe_stages=SAFE)
    guard.check("helpdesk", "helpdesk.ticket", "message_post", [[5]],
                {"body": "<p>n</p>", "subtype_xmlid": "mail.mt_note", "message_type": "comment"})
    guard.check("timesheet", "account.analytic.line", "create",
                [{"name": "#5 diagnosed x", "unit_amount": 0.75, "employee_id": 1, "date": "2026-11-03"}])
    with pytest.raises(guard.WriteRefused, match="0.25"):
        guard.check("timesheet", "account.analytic.line", "create",
                    [{"name": "x", "unit_amount": 0.3, "employee_id": 1, "date": "2026-11-03"}])


def test_adapter_in_observe_refuses_writes_even_when_the_guard_passes():
    fake = FakeOdoo()
    odoo = OdooAdapter(fake, writes_on=False)
    with pytest.raises(guard.WriteRefused, match="observe"):
        odoo.call("helpdesk", "helpdesk.ticket", "create", [{"name": "Fix X", "stage_id": 14}], safe_stages=SAFE)
    assert fake.writes == [] and odoo.search_read("helpdesk.stage", [], ["name"])


def test_open_odoo_refuses_outside_live_and_without_the_key(cfg, fake_keyring):
    from twin_agent.helpdesk.odoo import OdooUnavailable
    with pytest.raises(OdooUnavailable, match="live"):
        open_odoo(cfg, backend=fake_keyring)
    cfg.update(run_mode="live", odoo_sys={"url": "https://sys.example.test", "db": "x"})
    with pytest.raises(OdooUnavailable, match="odoo-sys-principal"):
        open_odoo(cfg, backend=fake_keyring)


# ---------------------------------------------------------------- monitor identity rule

def test_monitor_alerts_on_admin_uid_and_off_host_sme_writes():
    """Bug e: the Monitor classifies the authenticated uid / host recorded on each Acme write. A write
    authenticated as an admin uid, or to a host other than the pinned one, raises an alert; Principal's own uid on
    the pinned host does not."""
    cfg = {"monitor": {"odoo": {"admin_uids": [1], "pinned_host": "helpdesk.example.com"}},
           "odoo_sys": {"url": "https://helpdesk.example.com"}}
    base = {"system": "odoo", "env": "sme_helpdesk", "op": "write", "model": "helpdesk.ticket", "status": "done",
            "approvals": [{"kind": "principal_instruction", "by": "principal"}]}
    admin = {k for _s, k, _t in al.judge_write({**base, "uid": 1, "url": "https://helpdesk.example.com"}, cfg)}
    assert "admin_uid_write" in admin
    off = {k for _s, k, _t in al.judge_write({**base, "uid": 42, "url": "https://evil.example.test"}, cfg)}
    assert "off_host_write" in off
    clean = {k for _s, k, _t in al.judge_write({**base, "uid": 42, "url": "https://helpdesk.example.com"}, cfg)}
    assert not ({"admin_uid_write", "off_host_write"} & clean)


# ---------------------------------------------------------------- conventions

def test_conventions_are_discovered_and_safe_stages_confirmed_at_runtime():
    fake = FakeOdoo()
    fake.data["helpdesk.stage"][14]["template_id"] = 999          # someone added a template to stage 14
    c = discover(OdooAdapter(fake), {"helpdesk": {}})
    assert c.safe_stages == SAFE - {14, 9}                       # 14 got a template; 9 (Live) auto-closes -> gated
    assert any("stage 14" in p for p in c.problems)
    assert 9 not in c.safe_stages and any("stage 9" in p for p in c.problems)   # stage 9 never silently safe
    assert c.emails_client(2) and not c.emails_client(12) and c.emails_client(4242)   # unknown = emails
    assert c.supervisor["uid"] == SUPERVISOR_UID and c.supervisor["team_id"] == 2 and c.supervisor["priority"] == "3"
    assert c.supervisor["x_track"] == "functional" and c.principal_uid == PRINCIPAL_UID and c.principal_employee_id == PRINCIPAL_EMPLOYEE
    assert c.types["configuration"] == 4 and "x_type_code" in c.ticket_fields
    assert ("ir.model.fields", "create") not in fake.calls and fake.writes == []


def test_stage_read_failure_means_nothing_is_safe():
    fake = FakeOdoo()
    fake.fail_on.add(("helpdesk.stage", "search_read"))
    assert discover(OdooAdapter(fake), {}).safe_stages == set()


# ---------------------------------------------------------------- style

def test_ticket_style():
    d = style.TicketDraft(request_id="REQ-0001", title="stock problem when inbound qc fails on returns — WAVE",
                          type="Bug", requester="Staff A (Stores)", source="Telegram, Odoo x Globex (ref REQ-0001)",
                          bullets=["Return receipt **stays** blocked", "- Expected: it validates"])
    text = style.plain_ticket(d)
    lines = text.splitlines()
    assert lines[0] == "Fix Stock Problem When Inbound Qc Fails on Returns - WAVE"
    assert lines[1:5] == ["Requester: Staff A (Stores)", "Type: Bug", "Source: Telegram, Odoo x Globex (ref REQ-0001)",
                          "Request"]
    assert lines[5:] == ["- Return receipt stays blocked", "- Expected: it validates"]
    assert "—" not in text and "**" not in text and style.lint(d) == []
    assert any("Request needs 2 to 4" in p for p in style.lint(style.TicketDraft(request_id="R", title="Fix A", bullets=["one"])))
    assert style.title_case("fix the x_track field on ID 2607") == "Fix the x_track Field on ID 2607"
    note = style.note_lines(style.TicketDraft(request_id="REQ-1", title="t", runbook_trust="AMBER",
                                              links=["PR #12"], evidence=["Read stock.move 1"]))
    assert note[0].startswith("[Digital Twin]") and "Verification: unverified" in note[5] and "a hint" in note[6]


# ---------------------------------------------------------------- desk

def _live(cfg):
    cfg["run_mode"] = "live"
    cfg["helpdesk"].update(enabled=True, write_enabled=True, partner_ids={"EG": 3001})
    cfg["odoo_sys"] = {"url": "https://sys.example.test", "db": "x"}
    cfg["people"]["supervisor_tg_user_id"] = SUPERVISOR_TG
    return cfg


def _request(db, clock, lane="L4_t2a_fix_proposal"):
    n = db._one("SELECT COUNT(*) AS n FROM requests")["n"] + 1
    q = make_event("WAVE/00001 cannot be validated", msg_id=n, sender=STAFF_A, ts=clock.now())
    rid = db.create_request(q, requester_id="2001", requester_name="Staff A", lane=lane, company="EG",
                            title="Receipt blocked by a stray line")
    # A T2a packet derives type=Bug/x_track=development, so creating its ticket is now gated on a passing proof
    # (verify-first). The create-path tests mean to exercise the CREATE, so give them a cleared proof artifact -
    # signed through the real pipeline.attach so it carries valid provenance (a bare dict no longer clears the gate).
    from twin_agent.proof import pipeline
    pkt = pipeline.attach(canned_packet(rid), {"request_id": rid, "status": "prepared",
                                               "route": "proof_pair", "verdict": "reproduced"})
    db.save_packet(rid, pkt, True, [])
    return rid


def _request_no_proof(db, clock, lane="L4_t2a_fix_proposal"):
    """A default T2a request with NO proof artifact - the dominant runtime path (canned_packet, ticket_draft null)."""
    n = db._one("SELECT COUNT(*) AS n FROM requests")["n"] + 1
    q = make_event("WAVE/00001 cannot be validated", msg_id=n, sender=STAFF_A, ts=clock.now())
    rid = db.create_request(q, requester_id="2001", requester_name="Staff A", lane=lane, company="EG",
                            title="Receipt blocked by a stray line")
    db.save_packet(rid, canned_packet(rid), False, [])
    return rid


def _desk(cfg, clock, bus, tmp_home, *, writes=True):
    db = StateDB(tmp_home / "data" / "state" / "state.db", clock, cfg)
    fake = FakeOdoo()
    desk = HelpdeskDesk(db, cfg, bus, clock, OdooAdapter(fake, writes_on=writes))
    cards = []
    bus.subscribe(Topics.CARD_OUT, lambda c: cards.append(c))
    return db, fake, desk, cards


def _tap(db, rid, action, h, user=PRINCIPAL_ID):
    nonce = db.issue_approval(rid, 1, action, user, h, datetime(2099, 1, 1, tzinfo=timezone.utc))
    assert db.use_approval(nonce, user, action, rid)
    return {"action": action, "request_id": rid, "user_id": user, "nonce": nonce}


def _writes(db):
    return [json.loads(e["detail_json"]) | {"kind": e["kind"]} for e in db._all(
        "SELECT * FROM events WHERE kind IN ('write', 'write_blocked') ORDER BY id")]


def test_dev_ticket_part_withheld_without_proof(cfg, clock, bus, tmp_home):
    """Verify-first wired into the desk: a dev ticket part with no passing proof is WITHHELD (needs human),
    never silently drafted. Non-dev tickets are unaffected (the e2e's offers are unchanged)."""
    _live(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    n = db._one("SELECT COUNT(*) AS n FROM requests")["n"] + 1
    q = make_event("WAVE/00002 feature is broken", msg_id=n, sender=STAFF_A, ts=clock.now())
    rid = db.create_request(q, requester_id="2001", requester_name="Staff A", lane="L4_t2a_fix_proposal",
                            company="EG", title="Feature is broken")
    pkt = dict(canned_packet(rid), ticket_draft={"parts": [{"type": "dev", "subject": "fix the bug",
                                                            "body": "the save button throws"}]})
    db.save_packet(rid, pkt, False, [])
    desk.offer(rid)
    asyncio.run(bus.drain())
    withheld = db._all("SELECT * FROM events WHERE kind = 'dev_ticket_withheld'")
    assert len(withheld) == 1
    assert "withheld" in cards[-1]["card"].text.lower()


def test_t2a_dev_packet_with_forged_proof_still_withheld(cfg, clock, bus, tmp_home):
    """Fix 1 (HIGH): a worker-forged proof (right fields, no pipeline signature) must NOT unlock the dev-ticket
    create gate. The packet is saved RAW (skipping normalize) to exercise the gate itself: the draft stays gated -
    no [Create ticket] button, and a manufactured tap creates nothing."""
    _live(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    # Create the provenance key (a real signed artifact for an unrelated request), so the forged-proof rejection
    # below exercises the HMAC comparison with a key PRESENT, not just the no-key fail-safe.
    from twin_agent.proof import pipeline
    pipeline.attach({"request_id": "REQ-9999"}, {"request_id": "REQ-9999", "status": "prepared",
                                                 "route": "proof_pair", "verdict": "reproduced"})
    n = db._one("SELECT COUNT(*) AS n FROM requests")["n"] + 1
    q = make_event("WAVE/00003 feature is broken", msg_id=n, sender=STAFF_A, ts=clock.now())
    rid = db.create_request(q, requester_id="2001", requester_name="Staff A", lane="L4_t2a_fix_proposal",
                            company="EG", title="Feature is broken")
    pkt = dict(canned_packet(rid), proof={"status": "prepared", "route": "proof_pair", "verdict": "reproduced"})
    db.save_packet(rid, pkt, False, [])                  # RAW: a forged proof that never passed through the pipeline
    h = desk.offer(rid)
    asyncio.run(bus.drain())
    assert "hk" not in _card_actions(cards[-1])          # the forged proof did NOT add a [Create ticket] button
    assert len(db._all("SELECT * FROM events WHERE kind = 'dev_ticket_withheld'")) == 1
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", h)))
    asyncio.run(bus.drain())
    assert not [w for w in fake.writes if w["method"] == "create"]


def _card_actions(entry) -> list:
    card = entry["card"] if isinstance(entry, dict) else entry
    return [b.action for row in card.buttons for b in row]


def test_t2a_dev_packet_without_proof_is_not_offered_ungated(cfg, clock, bus, tmp_home):
    """Fix 1 (verify-first): a T2a packet derives type=Bug/x_track=development even with ticket_draft parts=[].
    With no passing proof the CREATE action is WITHHELD - the card may show, but there is NO [Create ticket] button
    and a manufactured tap creates nothing. Both desk.offer and /ticket go through the same gate. An inquiry flows."""
    _live(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request_no_proof(db, clock)                       # canned_packet T2a, ticket_draft null, no proof
    h = desk.offer(rid)
    asyncio.run(bus.drain())
    assert "hk" not in _card_actions(cards[-1])              # no ungated [Create ticket]
    assert len(db._all("SELECT * FROM events WHERE kind = 'dev_ticket_withheld'")) == 1
    # a manufactured hk tap must also create nothing: apply refuses a gated draft
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", h)))
    asyncio.run(bus.drain())
    assert not [w for w in fake.writes if w["method"] == "create"]
    # /ticket goes through the SAME gate
    asyncio.run(desk.on_command({"user_id": PRINCIPAL_ID, "command": "ticket", "args": rid}))
    asyncio.run(bus.drain())
    assert "hk" not in _card_actions(cards[-1])
    # an INQUIRY (T1) is not a development ticket, so it still gets its [Create ticket] offer
    q = make_event("how do I export a report?", msg_id=777, sender=STAFF_A, ts=clock.now())
    rid2 = db.create_request(q, requester_id="2001", requester_name="Staff A", lane="L4_t2a_fix_proposal",
                             company="EG", title="How to export")
    inq = dict(canned_packet(rid2), tier="T1", proposed_action=None,
               ticket_draft={"parts": [{"type": "inquiry", "subject": "How to export a report", "body": "steps"}]})
    db.save_packet(rid2, inq, True, [])
    desk.offer(rid2)
    asyncio.run(bus.drain())
    assert "hk" in _card_actions(cards[-1])


def test_create_ticket_like_supervisor_then_dedupe(cfg, clock, bus, tmp_home):
    _live(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request(db, clock)
    h = desk.offer(rid)
    asyncio.run(bus.drain())
    card = cards[-1]["card"]
    assert "Fix " in card.text and "Request\n- " in card.text and [b.action for b in card.buttons[0]] == ["hk", "hx"]
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", h)))
    created = [w for w in fake.writes if w["method"] == "create"]
    assert len(created) == 1
    vals = created[0]["vals"]
    assert vals["stage_id"] == 14 and vals["team_id"] == 2 and vals["priority"] == "3" and vals["user_id"] == PRINCIPAL_UID
    assert vals["x_track"] == "development" and vals["x_type"] == 1 and vals["x_ai_confidence"] == 0.9
    assert "x_type_code" not in vals and f"(ref {desk.ledger.ref(rid)})" in vals["description"]
    note = [w for w in fake.writes if w["method"] == "message_post"][0]
    assert note["kwargs"]["subtype_xmlid"] == "mail.mt_note" and "partner_ids" not in note["kwargs"]
    assert db.get_request(rid)["ticket_id"] == str(created[0]["id"])
    # the same request again: never a second ticket, a linked note instead
    h2 = desk.offer(rid)
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", h2)))
    assert len([w for w in fake.writes if w["method"] == "create"]) == 1
    ws = _writes(db)
    assert all(w["env"] == "sme_helpdesk" and w["approvals"][0]["kind"] == "principal_tap" for w in ws)
    assert all(al.judge_write({**w, "approvals": [{**a, "by": "principal"} for a in w["approvals"]]}) == [] for w in ws)


def test_dedupe_finds_a_ticket_made_elsewhere_and_maybe_created_searches_first(cfg, clock, bus, tmp_home):
    _live(cfg)
    db, fake, desk, _ = _desk(cfg, clock, bus, tmp_home)
    rid = _request(db, clock)
    fake.data["helpdesk.ticket"][9500] = {"id": 9500, "name": "Supervisor made it", "partner_id": 3001,
                                          "create_uid": SUPERVISOR_UID, "description": f"<p>(ref {desk.ledger.ref(rid)})</p>"}
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", desk.offer(rid))))
    assert not [w for w in fake.writes if w["method"] == "create"]
    assert desk.ledger.ticket_of(rid) == 9500
    rid2 = _request(db, clock)
    fake.fail_on.add(("helpdesk.ticket", "create"))
    asyncio.run(desk.on_callback(_tap(db, rid2, "hk", desk.offer(rid2))))
    assert desk.ledger.find([rid2])["status"] == "maybe_created"
    fake.fail_on.clear()
    fake.data["helpdesk.ticket"][9600] = {"id": 9600, "name": "x", "partner_id": 3001, "create_uid": SUPERVISOR_UID,
                                          "description": f"<p>(ref {desk.ledger.ref(rid2)})</p>"}
    asyncio.run(desk.on_callback(_tap(db, rid2, "hk", desk.offer(rid2))))
    assert not [w for w in fake.writes if w["method"] == "create"] and desk.ledger.ticket_of(rid2) == 9600


def test_dedupe_ignores_a_ticket_under_a_foreign_partner(cfg, clock, bus, tmp_home):
    """Bug d: the REQ ref lives in the client-visible description, so a ticket under a DIFFERENT client that
    carries our ref must never be matched. Dedupe is scoped by partner_id; a stranger's ref binds nothing."""
    _live(cfg)
    db, fake, desk, _ = _desk(cfg, clock, bus, tmp_home)
    rid = _request(db, clock)
    fake.data["helpdesk.ticket"][9800] = {"id": 9800, "name": "stranger's ticket", "partner_id": 3999,
                                          "create_uid": SUPERVISOR_UID,
                                          "description": f"<p>(ref {desk.ledger.ref(rid)})</p>"}
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", desk.offer(rid))))
    asyncio.run(bus.drain())
    assert desk.ledger.ticket_of(rid) != 9800                      # the foreign-partner ref is not matched
    assert [w for w in fake.writes if w["method"] == "create"]     # so the twin makes its own ticket


def test_observe_records_prepared_writes_only(cfg, clock, bus, tmp_home):
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home, writes=False)   # run_mode dry_run, write off
    rid = _request(db, clock)
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", desk.offer(rid))))
    asyncio.run(bus.drain())
    assert fake.writes == []
    ws = _writes(db)
    assert {w["op"] for w in ws} == {"create", "message_post"} and {w["status"] for w in ws} == {"prepared"}
    assert "Observe" in cards[-1].text
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", desk.offer(rid))))   # second observe tap: still one prepared
    assert len([w for w in _writes(db) if w["op"] == "create"]) == 1
    # the prepared link must NOT permanently block creation: once writes are on, a later tap really creates it
    _live(cfg)
    desk.odoo.writes_on = True
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", desk.offer(rid))))
    asyncio.run(bus.drain())
    assert [w for w in fake.writes if w["method"] == "create"] and desk.ledger.ticket_of(rid)


def test_stale_card_and_strangers_never_write(cfg, clock, bus, tmp_home):
    _live(cfg)
    db, fake, desk, _ = _desk(cfg, clock, bus, tmp_home)
    rid = _request(db, clock)
    desk.offer(rid)
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", "not-the-draft-hash")))
    asyncio.run(desk.on_callback({"action": "hk", "request_id": rid, "user_id": PRINCIPAL_ID, "nonce": "forged"}))
    assert fake.writes == []


def test_stage_moves_silent_vs_supervisor_gate(cfg, clock, bus, tmp_home):
    _live(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request(db, clock)
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", desk.offer(rid))))
    tid = desk.ledger.ticket_of(rid)
    # a confirmed-silent stage (6) is written straight away, no gate
    asyncio.run(desk.on_command({"user_id": PRINCIPAL_ID, "command": "stage", "args": f"{rid} 6"}))
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] == 6
    assert _writes(db)[-1]["approvals"][0]["kind"] == "principal_instruction"
    # stage 9 (Live) starts the 7-day auto-close that emails "Solved": it now needs Supervisor's gate, no direct write
    n = len(fake.writes)
    asyncio.run(desk.on_command({"user_id": PRINCIPAL_ID, "command": "stage", "args": f"{rid} 9"}))
    asyncio.run(bus.drain())
    assert len(fake.writes) == n and fake.data["helpdesk.ticket"][tid]["stage_id"] == 6   # waits for Supervisor
    assert desk.ledger.open_gate(rid)["stage_id"] == 9
    asyncio.run(desk.on_callback(_tap(db, rid, "ag", f"gate:{tid}:9", user=SUPERVISOR_TG)))
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] == 9
    w9 = _writes(db)[-1]
    assert w9["auto_close_clock"] and w9["emails_client"] \
        and {a["kind"] for a in w9["approvals"]} == {"principal_instruction", "supervisor_gate"}
    # a plain client-emailing stage (Functional, 2) also needs Supervisor
    n = len(fake.writes)
    asyncio.run(desk.on_command({"user_id": PRINCIPAL_ID, "command": "stage", "args": f"{rid} 2"}))
    asyncio.run(bus.drain())
    assert len(fake.writes) == n                                    # Functional emails the client: no write yet
    supervisor_card = [c["card"] for c in cards if isinstance(c, dict) and c["card"].to == "supervisor"][-1]
    assert [b.action for b in supervisor_card.buttons[0]] == ["ag", "ax"]
    gate = desk.ledger.open_gate(rid)
    asyncio.run(desk.on_callback(_tap(db, rid, "ag", f"gate:{tid}:2", user=PRINCIPAL_ID)))   # Principal cannot gate
    assert len(fake.writes) == n
    asyncio.run(desk.on_callback(_tap(db, rid, "ag", f"gate:{tid}:2", user=SUPERVISOR_TG)))
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] == 2 and desk.ledger.open_gate(rid) is None
    w = _writes(db)[-1]
    assert w["emails_client"] and {a["kind"] for a in w["approvals"]} == {"principal_instruction", "supervisor_gate"}
    assert gate["stage_id"] == 2


def test_kill_switch_blocks_writes(cfg, clock, bus, tmp_home):
    _live(cfg)
    db, fake, desk, _ = _desk(cfg, clock, bus, tmp_home)
    rid = _request(db, clock)
    db.set_kill(True, by="test")
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", desk.offer(rid))))
    assert fake.writes == [] and _writes(db)[-1]["kind"] == "write_blocked"


# ---------------------------------------------------------------- Monitor rule

TAP = {"kind": "principal_tap", "by": "principal", "at": "2026-11-03T10:00:00+00:00"}
GATE = {"kind": "supervisor_gate", "by": "supervisor", "at": "2026-11-03T10:05:00+00:00"}


@pytest.mark.parametrize("w,expect", [
    ({"system": "odoo", "op": "create", "model": "helpdesk.ticket", "env": "sme_helpdesk", "approvals": [TAP]}, set()),
    ({"system": "odoo", "op": "create", "model": "helpdesk.ticket", "env": "sme_helpdesk", "approvals": []},
     {"write_without_instruction"}),
    ({"system": "odoo", "op": "create", "model": "helpdesk.ticket", "env": "sme_helpdesk",
      "approvals": [{**TAP, "by": "backup"}]}, {"write_without_instruction"}),
    ({"system": "odoo", "op": "unlink", "model": "helpdesk.ticket", "env": "sme_helpdesk", "approvals": [TAP]},
     {"odoo_write"}),
    ({"system": "odoo", "op": "write", "model": "project.task", "env": "sme_helpdesk", "approvals": [TAP]},
     {"odoo_write"}),
    ({"system": "odoo", "op": "write", "model": "helpdesk.ticket", "env": "sme_helpdesk", "approvals": [TAP],
      "emails_client": True, "stage_to": 2}, {"client_email_stage_unapproved"}),
    ({"system": "odoo", "op": "write", "model": "helpdesk.ticket", "env": "sme_helpdesk", "approvals": [TAP, GATE],
      "emails_client": True, "stage_to": 2}, set()),
    ({"system": "odoo", "op": "create", "model": "account.analytic.line", "env": "sme_helpdesk",
      "approvals": [{"kind": "timesheet_autopilot", "by": "autopilot", "at": "2026-11-03T10:00:00+00:00"}]}, set()),
    ({"system": "odoo", "op": "write", "model": "helpdesk.ticket", "env": "sme_helpdesk",
      "approvals": [{"kind": "timesheet_autopilot", "by": "autopilot", "at": "x"}]}, {"write_without_instruction"}),
])
def test_monitor_rule_for_sme_helpdesk_writes(w, expect):
    assert {k for _s, k, _t in al.judge_write(w)} == expect


# ---------------------------------------------------------------- BUILD: automatic start / identify / proof

def test_create_logs_start_time_and_no_advance_when_already_working(cfg, clock, bus, tmp_home):
    """On create the twin automatically logs the START time (internal note), and does NOT move the stage because
    the ticket is created directly in the working (Functional) stage, not in New."""
    _live(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request(db, clock)
    h = desk.offer(rid)
    asyncio.run(bus.drain())
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", h)))
    asyncio.run(bus.drain())
    creates = [w for w in fake.writes if w["method"] == "create" and w["model"] == "helpdesk.ticket"]
    assert len(creates) == 1 and creates[0]["vals"]["stage_id"] == 14
    assert any("Started work" in (w["kwargs"].get("body") or "") for w in fake.writes if w["method"] == "message_post")
    assert not [w for w in fake.writes if w["method"] == "write" and "stage_id" in (w.get("vals") or {})]


def test_advance_on_identify_moves_new_to_working_only(cfg, clock, bus, tmp_home):
    """Automatic status move: a ticket sitting in New (stage 1) is advanced to Functional (14, a SAFE stage);
    a ticket already past New is left alone."""
    _live(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    fake.data["helpdesk.ticket"][4100] = {"id": 4100, "name": "New one", "stage_id": 1, "partner_id": 3001,
                                          "team_id": 2, "create_uid": SUPERVISOR_UID}
    desk.advance_on_identify("REQ-N", 4100, [])
    moves = [w for w in fake.writes if w["method"] == "write" and w["ids"] == [4100]]
    assert moves and moves[-1]["vals"]["stage_id"] == 14
    fake.writes.clear()
    fake.data["helpdesk.ticket"][4101] = {"id": 4101, "name": "WIP", "stage_id": 2, "partner_id": 3001,
                                          "team_id": 2, "create_uid": SUPERVISOR_UID}
    desk.advance_on_identify("REQ-W", 4101, [])
    assert not [w for w in fake.writes if w["method"] == "write" and w["ids"] == [4101]]


def test_advance_never_moves_into_a_client_emailing_stage(cfg, clock, bus, tmp_home):
    """Fail closed: if the working stage is not confirmed SAFE at runtime, the auto-advance does nothing
    (it never emails the client on its own)."""
    _live(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    fake.data["helpdesk.stage"][14]["template_id"] = 999      # stage 14 now emails -> not safe
    fake.data["helpdesk.ticket"][4102] = {"id": 4102, "name": "New", "stage_id": 1, "partner_id": 3001,
                                          "team_id": 2, "create_uid": SUPERVISOR_UID}
    desk.advance_on_identify("REQ-X", 4102, [])
    assert not [w for w in fake.writes if w["method"] == "write" and w["ids"] == [4102]]


def test_attach_proof_video_writes_the_link_as_a_note(cfg, clock, bus, tmp_home):
    """The investigator's recorded proof-video link is written onto the ticket as an internal note."""
    _live(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    desk.attach_proof_video("REQ-V", 4200, "https://wiki.example.com/share/in12306", [])
    notes = [w for w in fake.writes if w["method"] == "message_post" and w["ids"] == [4200]]
    assert notes and "wiki.example.com/share/in12306" in (notes[-1]["kwargs"]["body"] or "")
    desk.attach_proof_video("REQ-V", 4200, "", [])                               # empty url -> no note
    desk.attach_proof_video("REQ-V", 4200, "https://helpdesk.example.com/x.mp4", [])  # non-Wiki -> no note
    assert len([w for w in fake.writes if w["method"] == "message_post" and w["ids"] == [4200]]) == 1


def test_proof_share_link_fails_safe_on_malformed_packet(cfg, clock, bus, tmp_home):
    """Blocker fix: a garbled packet_json (list/str/broken) or a non-dict proof must return None, never raise -
    so apply()/_link() never crash after the ticket is already created."""
    _live(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    stub = lambda raw: type("D", (), {"latest_packet": staticmethod(lambda rid, r=raw: {"packet_json": r})})()
    for raw in ("[1, 2]", '"a string"', "{bad", json.dumps({"proof": "x"}), json.dumps({"proof": ["a"]}),
                json.dumps({"proof_link": "https://wiki.invalid/s/x"})):
        desk.db = stub(raw)
        assert desk._proof_share_link("REQ-X") is None
    desk.db = stub(json.dumps({"proof_link": "https://wiki.example.com/share/ok"}))     # a real one still works
    assert desk._proof_share_link("REQ-X") == "https://wiki.example.com/share/ok"


def test_observe_prepares_start_and_advance_without_touching_odoo(cfg, clock, bus, tmp_home):
    """OBSERVE: start-note and New->Functional are recorded as 'prepared' write events; nothing reaches Odoo."""
    _live(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home, writes=False)
    fake.data["helpdesk.ticket"][4300] = {"id": 4300, "name": "New", "stage_id": 1, "partner_id": 3001,
                                          "team_id": 2, "create_uid": SUPERVISOR_UID}
    desk.log_start("REQ-O", 4300, [])
    desk.advance_on_identify("REQ-O", 4300, [])
    assert fake.writes == []
    ev = _writes(db)
    assert any(e.get("op") == "message_post" and e.get("status") == "prepared" for e in ev)
    assert any(e.get("op") == "write" and e.get("status") == "prepared" and e.get("stage_to") == 14 for e in ev)


def test_create_attaches_real_wiki_share_link_to_ticket(cfg, clock, bus, tmp_home):
    """End-to-end: a real wiki.example.com/share link on the proof packet is written onto the ticket as a note."""
    _live(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request(db, clock)
    pk = db.latest_packet(rid); pkt = json.loads(pk["packet_json"])
    pkt["proof_link"] = "https://wiki.example.com/share/abc123XYZ"       # as the pipeline sets after a public upload
    db.save_packet(rid, pkt, True, [])
    h = desk.offer(rid); asyncio.run(bus.drain())
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", h))); asyncio.run(bus.drain())
    notes = [w["kwargs"].get("body", "") for w in fake.writes if w["method"] == "message_post"]
    assert any("wiki.example.com/share/abc123XYZ" in b for b in notes)


def test_fake_wiki_link_is_not_attached(cfg, clock, bus, tmp_home):
    """A fake/blocked link (wiki.invalid) is never attached to the ticket."""
    _live(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request(db, clock)
    pk = db.latest_packet(rid); pkt = json.loads(pk["packet_json"])
    pkt["proof_link"] = "https://wiki.invalid/s/x/y"
    db.save_packet(rid, pkt, True, [])
    h = desk.offer(rid); asyncio.run(bus.drain())
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", h))); asyncio.run(bus.drain())
    notes = [w["kwargs"].get("body", "") for w in fake.writes if w["method"] == "message_post"]
    assert not any("wiki.invalid" in b for b in notes)


# ---------------------------------------------------------------- auto_create (Principal 2026-11-09)

def test_auto_create_opens_an_intake_ticket_at_triage_then_notes_the_analysis(cfg, clock, bus, tmp_home):
    """helpdesk.auto_create: triage opens ONE intake ticket (Inquiry, silent stage) with no tap; the finished
    analysis is added to it as an internal note; a second triage never makes a second ticket."""
    _live(cfg)
    cfg["helpdesk"]["auto_create"] = True
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    desk.wire()
    rid = _request_no_proof(db, clock)                    # T2a, no proof: would be WITHHELD as a dev ticket
    asyncio.run(desk.on_triaged({"request_id": rid}))
    asyncio.run(bus.drain())
    created = [w for w in fake.writes if w["method"] == "create"]
    assert len(created) == 1
    vals = created[0]["vals"]
    assert vals["stage_id"] == 14 and vals.get("x_track") != "development"
    assert not db._all("SELECT * FROM events WHERE kind = 'dev_ticket_withheld'")
    assert not any("hk" in _card_actions(c) for c in cards if isinstance(c, dict) or getattr(c, "buttons", None))
    assert db.get_request(rid)["ticket_id"] == str(created[0]["id"])
    ws = _writes(db)
    assert ws and all(w["approvals"][0]["kind"] == "auto_create" for w in ws)
    notes_before = len([w for w in fake.writes if w["method"] == "message_post"])
    asyncio.run(desk.on_checks({"request_id": rid, "verified": False}))
    notes = [w for w in fake.writes if w["method"] == "message_post"]
    assert len(notes) == notes_before + 1 and notes[-1]["kwargs"]["subtype_xmlid"] == "mail.mt_note"
    asyncio.run(desk.on_triaged({"request_id": rid}))     # reopened / re-triaged: no second ticket
    assert len([w for w in fake.writes if w["method"] == "create"]) == 1


def test_auto_create_off_keeps_the_tap(cfg, clock, bus, tmp_home):
    """Without helpdesk.auto_create nothing is created at triage; the [Create ticket] card flow is unchanged."""
    _live(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    desk.wire()
    rid = _request(db, clock)
    asyncio.run(desk.on_triaged({"request_id": rid}))
    assert not [w for w in fake.writes if w["method"] == "create"]


def test_auto_create_in_observe_only_prepares(cfg, clock, bus, tmp_home):
    """Writes off (observe): auto_create records a prepared write, Odoo is never touched."""
    _live(cfg)
    cfg["helpdesk"]["auto_create"] = True
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home, writes=False)
    rid = _request_no_proof(db, clock)
    asyncio.run(desk.on_triaged({"request_id": rid}))
    assert not fake.writes or not [w for w in fake.writes if w["method"] in ("create", "message_post")]


def test_principal_close_moves_the_ticket_to_closed_without_supervisor(cfg, clock, bus, tmp_home):
    """helpdesk.principal_close_stage_id (Principal 2026-11-09): HIS /close or Close tap moves the linked ticket to Closed
    directly, his words being the approval. Other emailing stages still wait for Supervisor; strangers do nothing."""
    _live(cfg)
    cfg["helpdesk"]["principal_close_stage_id"] = 3
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request(db, clock)
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", desk.offer(rid))))
    tid = desk.ledger.ticket_of(rid)
    n = len(fake.writes)
    asyncio.run(desk.on_command({"user_id": 999, "command": "close", "args": rid}))          # a stranger
    assert len(fake.writes) == n
    asyncio.run(desk.on_command({"user_id": PRINCIPAL_ID, "command": "stage", "args": f"{rid} 2"}))
    assert len(fake.writes) == n and desk.ledger.open_gate(rid)["stage_id"] == 2              # still Supervisor's gate
    asyncio.run(desk.on_command({"user_id": PRINCIPAL_ID, "command": "close", "args": rid}))
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] == 3
    w = _writes(db)[-1]
    assert w["emails_client"] and [a["kind"] for a in w["approvals"]] == ["principal_instruction"]


def test_principal_close_tap_closes_the_ticket_and_off_by_default(cfg, clock, bus, tmp_home):
    _live(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request(db, clock)
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", desk.offer(rid))))
    tid = desk.ledger.ticket_of(rid)
    asyncio.run(desk.on_command({"user_id": PRINCIPAL_ID, "command": "close", "args": rid}))
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] != 3                               # setting off: no close
    desk.principal_close_stage = 3
    asyncio.run(desk.on_callback(_tap(db, rid, "ck", "", user=999)))                         # stranger's tap
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] != 3
    asyncio.run(desk.on_callback(_tap(db, rid, "ck", "")))
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] == 3
    assert _writes(db)[-1]["approvals"][0]["kind"] == "principal_tap"


def test_principal_moves_without_supervisor_covers_every_stage_he_orders(cfg, clock, bus, tmp_home):
    """helpdesk.principal_moves_without_supervisor (Principal's rule 2026-11-09): any stage HE orders is written directly, even a
    client-emailing one; a stranger cannot, an unknown stage id moves nothing, and the setting is off by default."""
    _live(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request(db, clock)
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", desk.offer(rid))))
    tid = desk.ledger.ticket_of(rid)
    n = len(fake.writes)
    asyncio.run(desk.on_command({"user_id": PRINCIPAL_ID, "command": "stage", "args": f"{rid} 2"}))
    assert len(fake.writes) == n                                          # off by default: Supervisor's gate
    desk.principal_moves_ungated = True
    asyncio.run(desk.on_command({"user_id": 999, "command": "stage", "args": f"{rid} 2"}))
    assert len(fake.writes) == n                                          # a stranger: nothing
    asyncio.run(desk.on_command({"user_id": PRINCIPAL_ID, "command": "stage", "args": f"{rid} 4242"}))
    assert len(fake.writes) == n                                          # no such stage: nothing
    asyncio.run(desk.on_command({"user_id": PRINCIPAL_ID, "command": "stage", "args": f"{rid} 2"}))
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] == 2
    w = _writes(db)[-1]
    assert w["emails_client"] and [a["kind"] for a in w["approvals"]] == ["principal_instruction"]


def test_notes_are_posted_as_html_and_render_lists():
    """Principal 2026-11-09: notes showed raw '<p>' tags. The note body is real HTML (body_is_html) with escaped text,
    '- x' lines as one list and section words in bold; record codes in titles keep their case."""
    from twin_agent.helpdesk import style
    body = style.html_lines(["[Digital Twin] Internal note for REQ-1", "Evidence", "- read <b>x</b>", "- y",
                             "Verification: unverified"])
    assert "<ul><li>read &lt;b&gt;x&lt;/b&gt;</li><li>y</li></ul>" in body
    assert "<p><b>Evidence</b></p>" in body and "&lt;p&gt;" not in body
    assert style.imperative_title("BATCH/13502", "Inquiry").split(" ", 1)[1] == "BATCH/13502"
    assert style.imperative_title("Receipt blocked by a stray line", "Bug").split(" ")[1] == "Receipt"


def test_note_call_sets_body_is_html(cfg, clock, bus, tmp_home):
    _live(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request(db, clock)
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", desk.offer(rid))))
    notes = [w for w in fake.writes if w["method"] == "message_post"]
    assert notes and all(n["kwargs"].get("body_is_html") is True for n in notes)


def test_auto_created_ticket_moves_to_the_after_create_stage(cfg, clock, bus, tmp_home):
    """helpdesk.after_create_stage_id (Principal 2026-11-09: 'make it Functional'): the auto intake ticket is created
    silently, then moved to that stage under Principal's rule; with principal_moves_without_supervisor off it waits for Supervisor."""
    _live(cfg)
    cfg["helpdesk"].update(auto_create=True, after_create_stage_id=2, principal_moves_without_supervisor=True)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request_no_proof(db, clock)
    asyncio.run(desk.on_triaged({"request_id": rid}))
    tid = desk.ledger.ticket_of(rid)
    created = [w for w in fake.writes if w["method"] == "create"][0]
    assert created["vals"]["stage_id"] == 14 and fake.data["helpdesk.ticket"][tid]["stage_id"] == 2
    assert _writes(db)[-1]["approvals"][0]["kind"] == "principal_rule"
    # without Principal's no-gate rule the move waits for Supervisor (no direct write)
    cfg["helpdesk"]["principal_moves_without_supervisor"] = False
    db2, fake2, desk2, _ = _desk(cfg, clock, bus, tmp_home / "b")
    rid2 = _request_no_proof(db2, clock)
    asyncio.run(desk2.on_triaged({"request_id": rid2}))
    assert fake2.data["helpdesk.ticket"][desk2.ledger.ticket_of(rid2)]["stage_id"] == 14
    assert desk2.ledger.open_gate(rid2)["stage_id"] == 2


def test_auto_create_channels_skips_mail(cfg, clock, bus, tmp_home):
    """2026-11-10: with auto_create_channels = Telegram only, an email request gets no ticket by itself."""
    from twin_agent.models import Channel
    _live(cfg)
    cfg["helpdesk"]["auto_create"] = True
    cfg["helpdesk"]["auto_create_channels"] = ["telegram_group", "telegram_dm"]
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    desk.wire()
    q = make_event("Purchase to Pay: next step?", msg_id=900, sender=STAFF_A, ts=clock.now(), channel=Channel.email)
    rid = db.create_request(q, requester_id="2001", requester_name="Staff A", lane="L11_internal_diagnosis",
                            company="EG", title="P2P next step")
    asyncio.run(desk.on_triaged({"request_id": rid}))
    assert not [w for w in fake.writes if w["method"] == "create"]
    assert db._all("SELECT * FROM events WHERE kind = 'ticket_auto_skipped'")
    asyncio.run(desk.on_checks({"request_id": rid, "verified": False}))     # 2026-11-11: nor when checks finish
    assert not [w for w in fake.writes if w["method"] == "create"]
    tg = _request_no_proof(db, clock)                      # a Telegram request still gets its ticket
    asyncio.run(desk.on_triaged({"request_id": tg}))
    assert len([w for w in fake.writes if w["method"] == "create"]) == 1


def test_reopened_request_moves_its_ticket_back_to_functional(cfg, clock, bus, tmp_home):
    """2026-11-11: 'if the request came again the ticket should be open again'."""
    _live(cfg)
    cfg["helpdesk"]["after_create_stage_id"] = 2
    cfg["helpdesk"]["principal_moves_without_supervisor"] = True
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    desk.wire()
    rid = _request(db, clock)
    asyncio.run(desk.on_callback(_tap(db, rid, "hk", desk.offer(rid))))
    tid = desk.ledger.ticket_of(rid)
    db.update_request(rid, ticket_id=str(tid))
    fake.data["helpdesk.ticket"][tid]["stage_id"] = 3            # closed by Principal's Approve
    asyncio.run(desk.on_reopened({"request_id": rid}))
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] == 2
    assert db._all("SELECT * FROM events WHERE kind = 'ticket_reopened'")


def test_mail_request_gets_a_functional_ticket_after_analysis_fyi_does_not(cfg, clock, bus, tmp_home):
    """Principal 2026-11-11: an email that is a request / inquiry gets a Functional ticket once analysed; an FYI does
    not."""
    from twin_agent.models import Channel
    _live(cfg)
    cfg["helpdesk"].update(auto_create=True, auto_create_channels=["telegram_group"], after_create_stage_id=2,
                           principal_moves_without_supervisor=True)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    desk.wire()
    q = make_event("What is the next step?", msg_id=910, sender=STAFF_A, ts=clock.now(), channel=Channel.email)
    rid = db.create_request(q, requester_id="2001", requester_name="Staff A", lane="L12_email_reply",
                            company="EG", title="Next step")
    db.save_packet(rid, {"request_id": rid, "tier": "T1", "draft_reply": {"text": "The next step is ..."}}, False, [])
    asyncio.run(desk.on_triaged({"request_id": rid}))
    assert not [w for w in fake.writes if w["method"] == "create"]           # not at arrival
    asyncio.run(desk.on_checks({"request_id": rid, "verified": False}))
    created = [w for w in fake.writes if w["method"] == "create"]
    assert len(created) == 1 and fake.data["helpdesk.ticket"][created[0]["id"]]["stage_id"] == 2
    fyi = db.create_request(make_event("Basem, start the pilot.", msg_id=911, sender=STAFF_A, ts=clock.now(),
                                       channel=Channel.email),
                            requester_id="2001", requester_name="Staff A", lane="L12_email_reply", company="EG",
                            title="FYI")
    db.save_packet(fyi, {"request_id": fyi, "tier": "T4"}, False, [])
    asyncio.run(desk.on_checks({"request_id": fyi, "verified": False}))
    assert len([w for w in fake.writes if w["method"] == "create"]) == 1     # no ticket for the FYI
