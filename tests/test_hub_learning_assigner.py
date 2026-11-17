"""§15 — self-learning (15a) + team-aware assigner (15b). Acceptance tests from plan v2 §15."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from twin_agent.hub import routing
from twin_agent.hub.learning import FakeLearningLog, LearningRefused
from twin_agent.hub.model import Analysis, Owner
from hub_helpers import approve, build_hub, deliver, run_to_resolved

FIX = Path(__file__).parent.parent / "twin_agent" / "hub" / "fixtures"


def _analysis(ttype="Bug", track="functional", module="Inventory"):
    return Analysis(nature="issue", ticket_type=ttype, track=track, module=module, summary="x")


# ---------------------------------------------------------------- §15a self-learning

def test_15a_close_writes_one_record_with_all_signals():
    hub = build_hub()
    run_to_resolved(hub)
    assert len(hub.learning.records) == 1
    r = hub.learning.records[0]
    for key in ("final_type", "final_module", "final_owner_uid", "resolution", "proof_outcome", "judge_scores"):
        assert key in r


def test_15a_reassignment_before_close_is_the_final_owner():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    hub.reassign("201000000001", Owner(uid=777, name="Principal", tg_id=1001), actor_tg_id=1003)      # override before close
    approve(hub, "confirm", as_tg=1001)
    deliver(hub, "webhook_confirm.json")
    for kind in ("advance", "fix_ready", "resolve"):
        approve(hub, kind, as_tg=1001)
    assert hub.learning.records[0]["final_owner_uid"] == 777 and hub.learning.records[0]["reassigned"] is True


def test_15a_batch_shifts_routing_weight_toward_owner():
    log = FakeLearningLog()
    for _ in range(4):
        log.record({"client": "C", "final_type": "Bug", "final_owner_uid": 778})
    log.record({"client": "C", "final_type": "Bug", "final_owner_uid": 777})
    rw = log.routing_weight("C", "Bug")
    assert rw["weights"][778] > rw["weights"][777]                 # weight shifted toward 778


def test_15a_review_emits_change_proposal_never_auto_applies():
    log = FakeLearningLog()
    log.record({"client": "C", "final_type": "Bug", "final_owner_uid": 778,
                "resolution": "fixed it", "client_rating": 5})
    out = log.review()
    assert out["status"] == "proposal" and out["auto_applied"] is False and out["proposals"]
    assert any(p["kind"] == "routing" for p in out["proposals"])


def test_15a_record_refuses_fields_outside_ticket_data():
    with pytest.raises(LearningRefused):
        FakeLearningLog().record({"client": "C", "ssn": "123-45-6789"})   # non-ticket field -> refused


# ---------------------------------------------------------------- §15b team-aware assigner

def test_15b_seeded_team_picks_expected_owner_with_rationale():
    asg = routing.RuleAssigner(routing.default_team())
    ctx = {"owner_uids": [778], "modules": ["Inventory"]}
    out = asg.assign(_analysis(ttype="New Request", track="development", module="Inventory"), ctx)
    assert out["uid"] == 778 and out["factors"] and "because" in out["rationale"]


def test_15b_raising_load_flips_to_next_best():
    principal = Owner(uid=777, name="Principal", role="support")
    supervisor = Owner(uid=778, name="Supervisor", role="lead")
    light = [routing.Candidate(principal, skills=("POS",), handles_tracks=("functional",), open_load=0),
             routing.Candidate(supervisor, skills=("POS",), handles_tracks=("functional",), open_load=0)]
    a = _analysis(module="POS")
    # both tie on skills+track; tie-breaks to the lower uid (Principal)
    assert routing.RuleAssigner(light).assign(a, {})["uid"] == 777
    # now pile load onto Principal -> the choice flips to Supervisor
    heavy = [routing.Candidate(principal, skills=("POS",), handles_tracks=("functional",), open_load=10),
             routing.Candidate(supervisor, skills=("POS",), handles_tracks=("functional",), open_load=0)]
    assert routing.RuleAssigner(heavy).assign(a, {})["uid"] == 778


def test_15b_stubbed_sources_still_deterministic():
    asg = routing.RuleAssigner(routing.default_team())
    out1 = asg.assign(_analysis(), {})      # empty (stubbed) context
    out2 = asg.assign(_analysis(), {})
    assert out1["uid"] == out2["uid"] and out1["rationale"] == out2["rationale"]


def test_15b_one_tap_override_produces_reassignment_and_learning_signal():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    out = hub.reassign("201000000001", Owner(uid=777, name="Principal"), actor_tg_id=1003)
    assert out["from"] == "Supervisor" and out["to"] == "Principal"
    assert hub.learning.signals[0]["kind"] == "reassignment_override"
    assert hub.learning.signals[0]["final_owner_uid"] == 777
