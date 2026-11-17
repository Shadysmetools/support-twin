"""Principal 2026-11-12: every Telegram and email request becomes an Acme ticket that the Ticket Analyst routes at once
to Functional (2) or Development Requested (6), and every routed ticket emails Principal its status, description,
analysis and an "Open as a session in Claude Code" link."""
import asyncio

from tests.test_helpdesk import _desk, _live, _request_no_proof

PRINCIPAL_PARTNER = 908


def _cfg(cfg):
    _live(cfg)
    cfg["helpdesk"].update(auto_create=True, after_create_stage_id=2, principal_moves_without_supervisor=True,
                           analyst={"enabled": True, "notify_partner_id": PRINCIPAL_PARTNER,
                                    "open_url": "http://127.0.0.1:8766/open"})
    return cfg


def _checks(rid, packet, *, passed=8, total=8, verified=True):
    return {"request_id": rid, "verified": verified, "packet": packet,
            "verdicts": [{"check": f"c{i}", "passed": i < passed} for i in range(total)]}


READS = [{"kind": "odoo_read", "summary": "BATCH/13961 in progress"},
         {"kind": "odoo_read", "summary": "20 pickings Ready"},
         {"kind": "odoo_read", "summary": "quant -1 on Cars/Mall of Egypt"}]


def _routed(cfg, clock, bus, tmp_home, packet, **kw):
    db, fake, desk, cards = _desk(_cfg(cfg), clock, bus, tmp_home)
    rid = _request_no_proof(db, clock)
    db.save_packet(rid, packet, True, [])
    asyncio.run(desk.on_triaged({"request_id": rid}))
    tid = desk.ledger.ticket_of(rid)
    asyncio.run(desk.on_checks(_checks(rid, packet, **kw)))
    return db, fake, desk, rid, tid


def test_intake_ticket_waits_silent_until_the_analyst_routes_it(cfg, clock, bus, tmp_home):
    db, fake, desk, cards = _desk(_cfg(cfg), clock, bus, tmp_home)
    rid = _request_no_proof(db, clock)
    asyncio.run(desk.on_triaged({"request_id": rid}))
    tid = desk.ledger.ticket_of(rid)
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] == 14          # no move to Functional before routing


def test_functional_route_writes_three_fields_one_note_stage_2_and_emails_principal(cfg, clock, bus, tmp_home):
    packet = {"request_id": "x", "tier": "T2b", "track": "functional", "ticket_type": "configuration",
              "evidence": READS, "analysis": "The stock rule is missing for Mall of Egypt.",
              "draft_reply": {"text": "Please add the rule ..."}}
    db, fake, desk, rid, tid = _routed(cfg, clock, bus, tmp_home, packet)
    t = fake.data["helpdesk.ticket"][tid]
    assert t["stage_id"] == 2 and t["x_track"] == "functional" and t["x_type"] == 4      # 4 = Configuration
    assert 0.7 <= t["x_ai_confidence"] <= 1
    field_writes = [w for w in fake.writes if w["method"] == "write" and "x_track" in w["vals"]]
    assert len(field_writes) == 1 and set(field_writes[0]["vals"]) == {"x_track", "x_type", "x_ai_confidence"}
    posts = [w for w in fake.writes if w["method"] == "message_post" and w["ids"] == [tid]]
    notes = [w for w in posts if w["kwargs"]["subtype_xmlid"] == "mail.mt_note"]
    mails = [w for w in posts if w["kwargs"]["subtype_xmlid"] == "mail.mt_comment"]
    assert len(mails) == 1
    assert any("Ticket Analyst" in n["kwargs"]["body"] for n in notes)
    m = mails[0]["kwargs"]
    assert m["partner_ids"] == [PRINCIPAL_PARTNER] and m["context"] == {"mail_notify_author": True}
    body = m["body"]
    assert "Functional" in body and "Configuration" in body and "stock rule is missing" in body
    assert f'href="http://127.0.0.1:8766/open/{tid}"' in body and "Open as a session in Claude Code" in body
    assert "unsure" not in body.lower()
    assert db._all("SELECT * FROM events WHERE kind = 'ticket_routed' AND request_id = ?", (rid,))


def test_development_route_goes_to_stage_6(cfg, clock, bus, tmp_home):
    packet = {"request_id": "x", "tier": "T3", "track": "development", "ticket_type": "customization",
              "evidence": READS, "analysis": "The batch validation code skips backorders."}
    db, fake, desk, rid, tid = _routed(cfg, clock, bus, tmp_home, packet)
    t = fake.data["helpdesk.ticket"][tid]
    assert t["stage_id"] == 6 and t["x_track"] == "development" and t["x_type"] == 3       # 3 = Customization


def test_weak_evidence_routes_anyway_and_says_unsure(cfg, clock, bus, tmp_home):
    packet = {"request_id": "x", "tier": "T2a", "track": "functional", "ticket_type": "bug", "evidence": []}
    db, fake, desk, rid, tid = _routed(cfg, clock, bus, tmp_home, packet, passed=2, verified=False)
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] == 2
    mail = [w for w in fake.writes if w["method"] == "message_post"
            and w["kwargs"]["subtype_xmlid"] == "mail.mt_comment"][0]
    assert "Unsure, please check" in mail["kwargs"]["body"]


def test_routes_once_per_analysis(cfg, clock, bus, tmp_home):
    packet = {"request_id": "x", "tier": "T2b", "track": "functional", "ticket_type": "configuration",
              "evidence": READS}
    db, fake, desk, rid, tid = _routed(cfg, clock, bus, tmp_home, packet)
    asyncio.run(desk.on_checks(_checks(rid, packet)))               # the same analysis again: no second email
    mails = [w for w in fake.writes if w["method"] == "message_post"
             and w["kwargs"]["subtype_xmlid"] == "mail.mt_comment"]
    assert len(mails) == 1


def test_linked_ticket_the_twin_did_not_create_is_never_routed(cfg, clock, bus, tmp_home):
    """Only tickets the twin creates. A request linked to someone else's ticket keeps the plain analysis note."""
    db, fake, desk, cards = _desk(_cfg(cfg), clock, bus, tmp_home)
    rid = _request_no_proof(db, clock)
    desk.ledger.set_link([rid], rid, 777, "linked")
    fake.data["helpdesk.ticket"][777] = {"id": 777, "stage_id": 11, "name": "someone else's"}
    packet = {"request_id": rid, "tier": "T3", "track": "development", "evidence": READS}
    asyncio.run(desk.on_checks(_checks(rid, packet)))
    assert fake.data["helpdesk.ticket"][777]["stage_id"] == 11
    assert not [w for w in fake.writes if w["method"] == "write" and w.get("ids") == [777]]
    assert not [w for w in fake.writes if w["method"] == "message_post"
                and w["kwargs"]["subtype_xmlid"] == "mail.mt_comment"]


def test_analyst_off_keeps_todays_flow(cfg, clock, bus, tmp_home):
    _live(cfg)
    cfg["helpdesk"].update(auto_create=True, after_create_stage_id=2, principal_moves_without_supervisor=True)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request_no_proof(db, clock)
    asyncio.run(desk.on_triaged({"request_id": rid}))
    assert fake.data["helpdesk.ticket"][desk.ledger.ticket_of(rid)]["stage_id"] == 2


def test_route_command_routes_a_twin_ticket_from_its_saved_analysis(cfg, clock, bus, tmp_home):
    """`route REQ-x` (Principal only): route a ticket the twin made before the analyst was on, from its latest packet."""
    from tests.test_helpdesk import PRINCIPAL_ID
    _cfg(cfg)
    cfg["helpdesk"]["analyst"]["enabled"] = False
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request_no_proof(db, clock)
    asyncio.run(desk.on_triaged({"request_id": rid}))           # made the old way: moved to Functional at once
    tid = desk.ledger.ticket_of(rid)
    packet = {"request_id": rid, "tier": "T3", "track": "development", "ticket_type": "customization",
              "evidence": READS, "analysis": "The validate code skips the backorder."}
    db.save_packet(rid, packet, True, [{"check": "c", "passed": True}])
    cfg["helpdesk"]["analyst"]["enabled"] = True
    db2, fake2, desk2 = db, fake, type(desk)(db, cfg, bus, clock, desk.odoo)
    asyncio.run(desk2.on_command({"user_id": PRINCIPAL_ID, "command": "route", "args": rid}))
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] == 6
    assert [w for w in fake.writes if w["method"] == "message_post" and w["kwargs"]["subtype_xmlid"] == "mail.mt_comment"]
    asyncio.run(desk2.on_command({"user_id": 999, "command": "route", "args": rid}))     # nobody else


def test_failed_analysis_still_routes_to_functional_marked_unsure(cfg, clock, bus, tmp_home):
    """Principal 2026-11-12 ("Yes, fix it here"): when the worker fails (REQ-0068: subscription access refused), the
    ticket must not sit unrouted in the silent stage. It goes to Functional, confidence 0, unsure, with the reason."""
    db, fake, desk, cards = _desk(_cfg(cfg), clock, bus, tmp_home)
    rid = _request_no_proof(db, clock)
    asyncio.run(desk.on_triaged({"request_id": rid}))
    tid = desk.ledger.ticket_of(rid)
    asyncio.run(desk.on_worker_failed({"request_id": rid, "why": "Your organization has disabled Claude access"}))
    t = fake.data["helpdesk.ticket"][tid]
    assert t["stage_id"] == 2 and t["x_track"] == "functional" and t["x_ai_confidence"] == 0.0
    mail = [w for w in fake.writes if w["method"] == "message_post"
            and w["kwargs"]["subtype_xmlid"] == "mail.mt_comment"][0]["kwargs"]["body"]
    assert "Unsure, please check" in mail and "analysis failed" in mail and "disabled Claude access" in mail
    asyncio.run(desk.on_worker_failed({"request_id": rid, "why": "Your organization has disabled Claude access"}))
    assert len([w for w in fake.writes if w["method"] == "message_post"
                and w["kwargs"]["subtype_xmlid"] == "mail.mt_comment"]) == 1          # once per failure


def test_failed_analysis_on_an_email_opens_and_routes_its_ticket(cfg, clock, bus, tmp_home):
    """An email request gets its ticket only after the analysis; when that fails, it gets one anyway (the twin
    cannot tell an FYI from a request without the analysis, so it asks Principal to check)."""
    from twin_agent.models import Channel
    from twin_agent.testing.fakes import STAFF_A, make_event
    _cfg(cfg)
    cfg["helpdesk"]["auto_create_channels"] = ["telegram_group", "telegram_dm"]
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    q = make_event("Discuss Sahel Issue", msg_id=950, sender=STAFF_A, ts=clock.now(), channel=Channel.email)
    rid = db.create_request(q, requester_id="2001", requester_name="Lamia", lane="L12_email_reply", company="EG",
                            title="Discuss Sahel Issue")
    asyncio.run(desk.on_worker_failed({"request_id": rid, "why": "worker timeout"}))
    tid = desk.ledger.ticket_of(rid)
    assert tid and fake.data["helpdesk.ticket"][tid]["stage_id"] == 2


def test_failed_analysis_with_analyst_off_does_nothing(cfg, clock, bus, tmp_home):
    _live(cfg)
    cfg["helpdesk"].update(auto_create=True, after_create_stage_id=2, principal_moves_without_supervisor=True)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request_no_proof(db, clock)
    n = len(fake.writes)
    asyncio.run(desk.on_worker_failed({"request_id": rid, "why": "x"}))
    assert len(fake.writes) == n


def test_failed_reanalysis_never_moves_an_already_routed_ticket(cfg, clock, bus, tmp_home):
    packet = {"request_id": "x", "tier": "T3", "track": "development", "ticket_type": "customization",
              "evidence": READS, "analysis": "Needs code."}
    db, fake, desk, rid, tid = _routed(cfg, clock, bus, tmp_home, packet)
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] == 6
    asyncio.run(desk.on_worker_failed({"request_id": rid, "why": "timeout on the re-run"}))
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] == 6


def test_route_command_on_a_failed_request_routes_it_as_failed(cfg, clock, bus, tmp_home):
    """`route REQ-x` on a request whose analysis failed (no packet, maybe no ticket): it routes like a failure, with
    the last logged reason (REQ-0068, 11 Nov)."""
    from tests.test_helpdesk import PRINCIPAL_ID
    from twin_agent.testing.fakes import STAFF_A, make_event
    db, fake, desk, cards = _desk(_cfg(cfg), clock, bus, tmp_home)
    q = make_event("Discuss Sahel Issue", msg_id=960, sender=STAFF_A, ts=clock.now())
    rid = db.create_request(q, requester_id="2001", requester_name="Lamia", lane="L12_email_reply", company="EG",
                            title="Discuss Sahel Issue")
    db.log_event("worker_failed", rid, actor="spawner", attempt=2, why="subscription access disabled")
    asyncio.run(desk.on_command({"user_id": PRINCIPAL_ID, "command": "route", "args": rid}))
    tid = desk.ledger.ticket_of(rid)
    assert tid and fake.data["helpdesk.ticket"][tid]["stage_id"] == 2
    mail = [w for w in fake.writes if w["method"] == "message_post"
            and w["kwargs"]["subtype_xmlid"] == "mail.mt_comment"][0]["kwargs"]["body"]
    assert "subscription access disabled" in mail


def test_email_has_an_orange_open_button_and_no_odoo_link():
    """Principal 2026-11-12 (screenshot of #2689): 'Open as a session in Claude Code' must be a button, orange, like
    Odoo's blue 'View Helpdesk Ticket' one; 'Open the ticket in Odoo' goes, that blue button already does it."""
    from twin_agent.helpdesk import analyst, ticket_mail
    r = analyst.failed("x")
    body = ticket_mail.email_html(2689, "REQ-0068", {"requester_name": "Lamia"}, r, {}, "Discuss Sahel Issue",
                                  "http://127.0.0.1:8766/open", "https://helpdesk.example.com/odoo/helpdesk/2689")
    # Principal (2nd screenshot): right UNDER Odoo's blue button, same size and brightness. Odoo's own button is a
    # table cell (background #5a6ed3, radius 3px) holding <a> 12px bold white, padding 8px 12px 11px.
    assert body.startswith("<table")
    cell = body.split("<td", 1)[1].split(">", 1)[0]
    assert "background-color: #d48c59" in cell and "border-radius: 3px" in cell     # not the shorthand
    assert "background-color: #d48c59" in body.split('<a ', 1)[1].split(">", 1)[0]
    a = body.split('<a ', 1)[1].split("</a>", 1)[0]
    assert 'href="http://127.0.0.1:8766/open/2689"' in a and "Open as a session in Claude Code" in a
    assert "font-size: 12px" in a and "padding: 8px 12px 11px" in a and "color: #FFFFFF" in a and "bold" in a
    assert "Open the ticket in Odoo" not in body and "/odoo/helpdesk/2689" not in body
    assert body.count("<a ") == 1


def _ts_cfg(cfg):
    _cfg(cfg)
    cfg["helpdesk"]["analyst"]["timesheet"] = {"enabled": True, "hours": 0.25, "employee_id": 26, "project_id": 107}
    return cfg


def test_routing_logs_one_quarter_hour_timesheet_line(cfg, clock, bus, tmp_home):
    """Principal 2026-11-12 ("Yes, 0.25 h each"), as his ticket-router rule: one line per ticket routed, employee 26,
    project 107, the ticket, today, 'Routing: <stage> - <reason>'; checked first so nothing is logged twice."""
    _ts_cfg(cfg)
    packet = {"request_id": "x", "tier": "T2b", "track": "functional", "ticket_type": "configuration",
              "evidence": READS, "analysis": "The stock rule is missing."}
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request_no_proof(db, clock)
    asyncio.run(desk.on_triaged({"request_id": rid}))
    tid = desk.ledger.ticket_of(rid)
    asyncio.run(desk.on_checks(_checks(rid, packet)))
    lines = [w for w in fake.writes if w["model"] == "account.analytic.line" and w["method"] == "create"]
    assert len(lines) == 1
    v = lines[0]["vals"][0] if isinstance(lines[0]["vals"], list) else lines[0]["vals"]
    assert v["unit_amount"] == 0.25 and v["employee_id"] == 26 and v["project_id"] == 107
    assert v["helpdesk_ticket_id"] == tid and v["date"] == clock.now().astimezone(__import__("twin_agent.clock", fromlist=["CAIRO"]).CAIRO).date().isoformat()
    assert v["name"].startswith("Routing: Functional - ")
    assert db._all("SELECT * FROM events WHERE kind = 'ticket_timesheet' AND request_id = ?", (rid,))
    # a second routing of the same ticket the same day (reopened, re-analysed) logs nothing more
    packet2 = dict(packet, analysis="Second look.")
    asyncio.run(desk.on_checks(_checks(rid, packet2)))
    assert len([w for w in fake.writes if w["model"] == "account.analytic.line" and w["method"] == "create"]) == 1


def test_failed_analysis_routing_logs_its_line_too(cfg, clock, bus, tmp_home):
    _ts_cfg(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request_no_proof(db, clock)
    asyncio.run(desk.on_triaged({"request_id": rid}))
    asyncio.run(desk.on_worker_failed({"request_id": rid, "why": "timeout"}))
    lines = [w for w in fake.writes if w["model"] == "account.analytic.line" and w["method"] == "create"]
    assert len(lines) == 1


def test_no_timesheet_when_its_switch_is_off(cfg, clock, bus, tmp_home):
    packet = {"request_id": "x", "tier": "T2b", "track": "functional", "ticket_type": "configuration",
              "evidence": READS}
    db, fake, desk, rid, tid = _routed(cfg, clock, bus, tmp_home, packet)
    assert not [w for w in fake.writes if w["model"] == "account.analytic.line"]


# ---- 15 Nov 2026: Supervisor's Digital Twin kit works the tickets; the twin is only the intake and the labeller ------

def _label_cfg(cfg):
    _cfg(cfg)
    cfg["helpdesk"]["analyst"].update(mode="label", label_stage_id=1)
    cfg["helpdesk"]["analyst"]["timesheet"] = {"enabled": True, "hours": 0.25, "employee_id": 26, "project_id": 107}
    return cfg


def test_label_mode_writes_the_label_and_one_note_then_hands_to_new(cfg, clock, bus, tmp_home):
    """Principal 2026-11-15 ("yes do the plan"): the kit picks tickets in New that carry the Ticket Analyst's label.
    The twin labels (x_track, x_type, x_ai_confidence + one note) and puts the ticket in New. No routing to 2/6, no
    email to Principal, no timesheet: the kit's own session does those."""
    _label_cfg(cfg)
    packet = {"request_id": "x", "tier": "T3", "track": "development", "ticket_type": "customization",
              "evidence": READS, "analysis": "Needs code."}
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request_no_proof(db, clock)
    asyncio.run(desk.on_triaged({"request_id": rid}))
    tid = desk.ledger.ticket_of(rid)
    assert fake.data["helpdesk.ticket"][tid]["stage_id"] == 14            # silent until labelled
    asyncio.run(desk.on_checks(_checks(rid, packet)))
    t = fake.data["helpdesk.ticket"][tid]
    assert t["stage_id"] == 1 and t["x_track"] == "development" and t["x_type"] == 3
    assert not [w for w in fake.writes if w["method"] == "message_post"
                and w["kwargs"]["subtype_xmlid"] == "mail.mt_comment"]
    assert not [w for w in fake.writes if w["model"] == "account.analytic.line"]
    notes = [w for w in fake.writes if w["method"] == "message_post" and w["ids"] == [tid]
             and "Ticket Analyst" in w["kwargs"]["body"]]
    assert len(notes) == 1 and "Handed to" in notes[0]["kwargs"]["body"]


def test_label_mode_failed_analysis_still_hands_to_new_unsure(cfg, clock, bus, tmp_home):
    _label_cfg(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    rid = _request_no_proof(db, clock)
    asyncio.run(desk.on_triaged({"request_id": rid}))
    tid = desk.ledger.ticket_of(rid)
    asyncio.run(desk.on_worker_failed({"request_id": rid, "why": "timeout"}))
    t = fake.data["helpdesk.ticket"][tid]
    assert t["stage_id"] == 1 and t["x_ai_confidence"] == 0.0


def test_route_command_opens_the_missing_ticket_then_labels_it(cfg, clock, bus, tmp_home):
    """REQ-0072 / REQ-0077 (15 Nov): analysed, but no ticket (intake was off). `route REQ-x` opens it, then labels."""
    from tests.test_helpdesk import PRINCIPAL_ID
    from twin_agent.testing.fakes import STAFF_A, make_event
    _label_cfg(cfg)
    db, fake, desk, cards = _desk(cfg, clock, bus, tmp_home)
    q = make_event("OMS App walkthrough session", msg_id=970, sender=STAFF_A, ts=clock.now())
    rid = db.create_request(q, requester_id="2001", requester_name="Ross", lane="L11_internal_diagnosis",
                            company="EG", title="OMS App walkthrough")
    db.save_packet(rid, {"request_id": rid, "tier": "T1", "track": "functional", "ticket_type": "inquiries",
                         "evidence": READS, "analysis": "A walkthrough request."}, True, [{"check": "c", "passed": True}])
    asyncio.run(desk.on_command({"user_id": PRINCIPAL_ID, "command": "route", "args": rid}))
    tid = desk.ledger.ticket_of(rid)
    assert tid and fake.data["helpdesk.ticket"][tid]["stage_id"] == 1
    assert fake.data["helpdesk.ticket"][tid]["x_type"] == 5                  # Inquiries
