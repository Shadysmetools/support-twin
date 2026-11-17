"""Ticket Analyst inside the twin (Principal 2026-11-12: every Telegram / email request becomes a ticket, routed at once
to Functional or Development Requested). Built on 25-TICKET-ANALYST.md: the classifier taxonomy and rules, with
confidence from CHECKED evidence (records read, judges passed), never a bare self-reported number. Inquiry stays a
Functional type (MASTER-PROMPT), not its own track."""
from twin_agent.helpdesk import analyst


def _packet(**kw):
    p = {"tier": "T2a", "evidence": [{"kind": "odoo_read", "summary": "BATCH/13961 state in_progress"},
                                     {"kind": "odoo_read", "summary": "20 pickings Ready"},
                                     {"kind": "odoo_read", "summary": "quant -1 on Cars/Mall of Egypt"}],
         "ticket_draft": None}
    p.update(kw)
    return p


def _verdicts(passed, total):
    return [{"check": f"c{i}", "passed": i < passed, "reasons": []} for i in range(total)]


def test_worker_track_and_type_with_strong_evidence_is_green():
    v = analyst.classify(_packet(track="functional", ticket_type="configuration", tier="T2b"),
                         _verdicts(8, 8), verified=True)
    assert (v.track, v.type, v.stage_id) == ("functional", "configuration", 2)
    assert v.level == "GREEN" and not v.unsure and v.confidence >= 0.7
    assert v.type_name == "Configuration"


def test_development_goes_to_development_requested():
    v = analyst.classify(_packet(track="development", ticket_type="customization", tier="T3"),
                         _verdicts(7, 8), verified=True)
    assert (v.track, v.type, v.stage_id) == ("development", "customization", 6)


def test_inquiry_is_a_functional_type_not_a_track():
    for given in ({"track": "inquiry"}, {"track": "functional", "ticket_type": "inquiries"}, {"tier": "T1"}):
        v = analyst.classify(_packet(**given), _verdicts(8, 8), verified=True)
        assert (v.track, v.type, v.stage_id) == ("functional", "inquiries", 2), given


def test_no_records_read_is_unsure_whatever_the_worker_says():
    """25-TICKET-ANALYST: their analyst scored blind. Ours never reports confidence it did not check."""
    v = analyst.classify(_packet(track="functional", ticket_type="bug", evidence=[], confidence=0.95),
                         _verdicts(8, 8), verified=True)
    assert v.unsure and v.level == "RED" and v.confidence <= 0.3
    assert any("no records" in r for r in v.reasons)


def test_failed_checks_lower_confidence():
    strong = analyst.classify(_packet(track="functional", ticket_type="bug"), _verdicts(8, 8), verified=True)
    weak = analyst.classify(_packet(track="functional", ticket_type="bug"), _verdicts(2, 8), verified=False)
    assert weak.confidence < strong.confidence and weak.unsure


def test_worker_and_tier_disagree_caps_confidence():
    v = analyst.classify(_packet(track="functional", ticket_type="configuration", tier="T3"), _verdicts(8, 8),
                         verified=True)
    assert v.track == "functional" and v.unsure and v.confidence <= 0.5
    assert any("disagree" in r for r in v.reasons)


def test_missing_worker_track_falls_back_to_the_tier():
    assert analyst.classify(_packet(tier="T3"), _verdicts(8, 8), verified=True).track == "development"
    assert analyst.classify(_packet(tier="T2b"), _verdicts(8, 8), verified=True).type == "configuration"
    dev_part = _packet(tier="T2a", ticket_draft={"parts": [{"type": "dev"}]})
    assert analyst.classify(dev_part, _verdicts(8, 8), verified=True).track == "development"


def test_invalid_values_never_pass():
    v = analyst.classify(_packet(track="sales", ticket_type="whatever", tier="T2a"), _verdicts(8, 8), verified=True)
    assert v.track in analyst.TRACKS and v.type in analyst.TYPES[v.track]


def test_type_wrong_for_its_track_is_replaced():
    """customization exists only on the development track; configuration and inquiries only on functional."""
    v = analyst.classify(_packet(track="functional", ticket_type="customization", tier="T2b"), _verdicts(8, 8),
                         verified=True)
    assert v.track == "functional" and v.type in analyst.TYPES["functional"]


def test_unverified_or_tier_only_is_never_green():
    """REQ-0069 (12 Nov): 3 records read, 5 of 8 checks, not verified, no worker track -> it scored GREEN. It must
    say unsure: green needs an independent check AND the worker's own classification."""
    v = analyst.classify(_packet(tier="UNKNOWN"), _verdicts(5, 8), verified=False)
    assert v.unsure and v.level != "GREEN"
    v2 = analyst.classify(_packet(tier="T2b"), _verdicts(8, 8), verified=True)            # tier only
    assert v2.unsure and v2.source == "tier"
    v3 = analyst.classify(_packet(track="functional", ticket_type="bug"), _verdicts(8, 8), verified=False)
    assert v3.unsure
