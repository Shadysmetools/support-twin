"""§16 — golden-set eval gate (16a) + independent judge (16b/c). Acceptance tests from plan v2 §16."""
from __future__ import annotations

import json
from pathlib import Path

from twin_agent.hub import judge as judge_mod
from twin_agent.hub.evalharness import DefaultEvalHarness
from twin_agent.hub.model import Analysis
from hub_helpers import approve, build_hub, deliver, run_to_resolved

FIX = Path(__file__).parent.parent / "twin_agent" / "hub" / "fixtures"

DRAFTER_MODEL = "claude-drafter"   # production drafter; the judge must be a DIFFERENT model


# ---------------------------------------------------------------- §16a eval harness gate

def test_16a_harness_scores_golden_set_and_passes():
    res = DefaultEvalHarness().run_gate()
    assert res["gate_pass"] is True and res["score"] >= res["pass_threshold"]


def test_16a_injected_regression_fails_the_gate():
    good = DefaultEvalHarness().run()                         # baseline (clean classifier)

    def worse(text, client_context=None):                    # a deliberately degraded classifier
        a = Analysis(nature="issue", ticket_type="Inquiries", track="inquiry", module="unknown", summary=text)
        return a

    bad = DefaultEvalHarness(classifier=worse)
    gate = bad.run_gate(baseline=good)
    assert gate["regressed"] is True and gate["gate_pass"] is False


# ---------------------------------------------------------------- §16b independent judge

def test_16b_judge_flags_bad_draft_passes_good_one():
    j = judge_mod.FakeJudge()
    good = j.score("draft", {"text": "Hello Oscar, your ticket #7 is resolved. — Acme Support", "expect_lang": "en"})
    bad = j.score("draft", {"text": "obviously your fault, guaranteed 100% forever", "expect_lang": "en"})
    assert good["flag"] is False and set(good["scores"]) == set(judge_mod.DIMENSIONS)
    assert bad["flag"] is True


def test_16b_judge_is_a_distinct_model_from_the_drafter():
    assert judge_mod.FakeJudge().model != DRAFTER_MODEL


def test_16b_low_score_draft_routes_to_owner_review():
    # a judge that flags everything -> the confirm draft is marked owner-review (and still needs approval)
    class FlagAll(judge_mod.FakeJudge):
        def score(self, kind, item):
            r = super().score(kind, item)
            r["flag"] = True
            return r

    hub = build_hub(judge=FlagAll())
    deliver(hub, "webhook_text.json")
    drafts = [p["text"] for p in hub.telegram.posts if "confirm draft" in p["text"]]
    assert drafts and "LOW SCORE: owner-review" in drafts[0]
    assert hub.whatsapp.automatic_count() == 1 and len(hub.whatsapp.sent) == 1     # still needs the owner's approval


# ---------------------------------------------------------------- §16c feedback + agreement metric

def test_16c_judge_scores_feed_learning_and_kpi():
    hub = build_hub()
    run_to_resolved(hub)
    assert "video" in hub.learning.records[0]["judge_scores"]       # §15a learning signal
    assert "judge_video_score" in hub.metrics.pushed[0]["outcome"]   # stage-11 KPI value


def test_16c_agreement_metric():
    pairs = [(True, True), (False, False), (True, False), (True, True)]   # 3/4 agree
    assert judge_mod.agreement(pairs) == 0.75
    assert judge_mod.agreement([]) == 0.0


# ---------------------------------------------------------------- §16e concrete config + redaction

def test_16e_redaction_strips_name_and_phone_before_judge():
    red = judge_mod.redact_for_judge("Hello Oscar, call me on +201001234567 about ticket #7", names=["Oscar"])
    assert "Oscar" not in red and "+201001234567" not in red and "[NAME]" in red


def test_16e_config_shapes_scoring_and_eval():
    cfg = judge_mod.JUDGE_CONFIG
    assert cfg["primary"]["model"] == "gemini-3.6-flash" and cfg["primary"]["provider"] == "vertex"
    assert cfg["fallback"]["model"] == "gpt-6.1-terra"
    assert set(cfg["scored_by_exact_match"]) == {"type", "assignment"}       # NOT judged
    assert "classification" in cfg["scored_by_judge"] and "draft" in cfg["scored_by_judge"]
    assert cfg["eval_framework"]["name"] == "deepeval" and cfg["eval_framework"]["telemetry"] == "off"
    # judge model family differs from the drafter (Gemini vs Claude)
    assert "gemini" in cfg["primary"]["model"] and "claude" not in cfg["primary"]["model"]


def test_16e_real_judge_blocks_without_creds_but_redacts(monkeypatch):
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("VERTEX_PROJECT", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    j = judge_mod.resolve_judge()
    out = j.score("draft", {"text": "Hi Oscar +201001234567"}, names=["Oscar"])
    assert out["status"] == "blocked" and out["redacted"] is True and out["via"] == "fallback"


def test_16e_primary_fallback_routing():
    class Stub:
        def __init__(self, blocked, model):
            self.blocked, self.model = blocked, model

        def score(self, kind, item, names=None):
            if self.blocked:
                return {"status": "blocked", "model": self.model}
            return {"status": "ok", "model": self.model, "aggregate": 0.9}

    # primary works -> used
    p = judge_mod.PrimaryFallbackJudge(Stub(False, "gemini-3.6-flash"), Stub(False, "gpt-6.1-terra"))
    assert p.score("draft", {"text": "x"})["via"] == "primary"
    # primary blocked -> fallback (GPT-6.1 Terra, key-frames)
    f = judge_mod.PrimaryFallbackJudge(Stub(True, "gemini-3.6-flash"), Stub(False, "gpt-6.1-terra"))
    out = f.score("draft", {"text": "x"})
    assert out["via"] == "fallback" and out["model"] == "gpt-6.1-terra"


def test_16e_assignment_is_not_sent_to_the_judge_in_pipeline():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    kinds = {s["kind"] for s in hub.judge.scored}
    assert "assignment" not in kinds and "classification" in kinds and "draft" in kinds
