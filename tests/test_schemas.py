"""Packet and card validation (ARCHITECT foundation)."""
import json
from pathlib import Path

from twin_agent import schemas
from twin_agent.models import Button, Card, CardKind
from twin_agent.testing.fakes import canned_packet

WORKER = Path(schemas.__file__).parent / "worker"


def test_canned_packet_is_valid():
    assert schemas.validate_packet(canned_packet(), "REQ-0142") == []


def test_wrong_request_id_and_missing_keys():
    p = canned_packet()
    assert schemas.validate_packet(p, "REQ-0001")
    del p["evidence"]
    assert "missing key: evidence" in schemas.validate_packet(p)


def test_bad_values_are_reported():
    p = canned_packet()
    p.update(tier="T9", company="US", confidence=1.5, note="\n".join(["x"] * 16))
    p["draft_reply"]["lang"] = "de"
    p["evidence"].append({"kind": "guess"})
    errs = schemas.validate_packet(p)
    assert len(errs) == 6, errs


def test_ticket_draft_parts_capped_at_three():
    p = canned_packet()
    part = {"part": "a", "type": "dev", "subject": "[Globex EG] X", "body": "b"}
    p["ticket_draft"] = {"parts": [part] * 4}
    assert schemas.validate_packet(p)
    p["ticket_draft"] = {"parts": [part]}
    assert schemas.validate_packet(p) == []


def test_normalize_drops_model_decisions():
    p = schemas.normalize_packet(canned_packet())
    assert p["verified"] is False
    assert p["proposed_action"]["approver"] is None
    assert p["proposed_action"]["script"] is None
    assert p["runbook_occurrence"]["status"] == "provisional"
    assert canned_packet()["verified"] is True       # the original is untouched


def test_normalize_strips_worker_authored_proof():
    """Fix 1(a): the dev-ticket gate trusts packet['proof']; a prompt-injected worker could forge it. normalize_packet
    strips any worker-supplied proof / proof_link so only the proof pipeline can set them."""
    p = canned_packet()
    p["proof"] = {"status": "prepared", "route": "proof_pair", "verdict": "reproduced"}
    p["proof_link"] = "https://wiki.invalid/x"
    q = schemas.normalize_packet(p)
    assert "proof" not in q and "proof_link" not in q
    assert "proof" in p                                  # the original dict is untouched


def test_card_roundtrip_and_validation():
    card = Card(kind=CardKind.new_request, text="REQ-0142 Odoo x Test / Logistics", request_id="REQ-0142",
                buttons=[[Button("Approve reply", "ap"), Button("Edit", "ed")], [Button("Take over", "to")]])
    d = schemas.card_to_dict(card)
    assert schemas.validate_card(d) == []
    assert schemas.card_from_dict(d) == card
    d["buttons"][0][0]["action"] = "APPROVE!"
    assert schemas.validate_card(d)


def test_schema_files_are_json():
    for name in ("packet.schema.json", "card.schema.json"):
        json.loads((WORKER / name).read_text(encoding="utf-8"))
