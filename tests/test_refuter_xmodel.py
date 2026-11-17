"""Cross-model refuter + evidence gate (17 Adversarial verification; maturity dim 4).

The evidence for dim 4: the deterministic gate overrides a yes-man, a wrong claim is caught even with evidence,
a right claim passes, and the external path fails closed and never leaks a raw secret. Uses FakeRefuter - no
network (conftest blocks it anyway). The real Groq path is exercised only through an injected fake client.
"""
from __future__ import annotations

import importlib.util
import json

import pytest

from twin_agent.checks import refuter_xmodel as rx
from twin_agent.checks.refuter_xmodel import (Claim, FakeRefuter, GroqRefuter, RefuterReply, check_claim,
                                               extract_claims, make_refuter, verify_packet)
from twin_agent.redact import redact, value_hash
from twin_agent.testing.fakes import FakeKeyring, canned_packet

# A concrete record-id evidence item and an assertion-only ("note") item.
RECORD_EV = [{"kind": "odoo_read", "model": "stock.move", "id": "1", "field": "product_uom_qty", "value": 0}]
ASSERTION_EV = [{"kind": "note", "text": "I verified it works, tests pass"}]

RUNBOOK = {"stray-in-line": {"title": "Stray IN line (orphan product)", "status": "confirmed 2026-07-26"}}


def diag(evidence):
    return Claim("diagnosis", "IN/00001/26 has a 0-demand line with no PO line", list(evidence))


# ---------------------------------------------------------------- (a) no evidence is rejected even if "fine"

def test_a_no_evidence_rejected_even_when_refuter_says_fine():
    v = check_claim(diag([]), FakeRefuter(refuted=False))
    assert v.verified is False
    assert any("no concrete evidence" in r for r in v.reasons)


# ---------------------------------------------------------------- (b) a wrong claim is rejected even with evidence

def test_b_wrong_claim_rejected_despite_evidence():
    v = check_claim(diag(RECORD_EV), FakeRefuter(refuted=True, reasons=["the 0-qty line is a valid backorder"]))
    assert v.verified is False
    assert any("refuted by" in r and "backorder" in r for r in v.reasons)


# ---------------------------------------------------------------- (c) a right claim with evidence + no refutation passes

def test_c_right_claim_with_evidence_passes():
    rf = FakeRefuter(refuted=False)
    v = check_claim(diag(RECORD_EV), rf)
    assert v.verified is True and v.mode == "cross-model"
    assert rf.calls                                              # the refuter actually ran


# ---------------------------------------------------------------- (d) a seeded sycophancy case: yes-man overridden

@pytest.mark.parametrize("refuter", [FakeRefuter(yes_man=True), FakeRefuter(refuted=False)])
def test_d_sycophancy_overridden_by_evidence_gate(refuter):
    # The claim offers only an assertion ("I verified it works"); the refuter naively agrees. The evidence gate -
    # not the model - rejects it. This is the yes-man guard.
    v = check_claim(diag(ASSERTION_EV), refuter)
    assert v.verified is False
    assert any("no concrete evidence" in r for r in v.reasons)  # rejected by the gate, not by a refutation
    assert refuter.calls                                        # the yes-man was consulted and ignored


# ---------------------------------------------------------------- (e) degrade on error; fail-closed only on no-key

def test_e_refuter_backend_error_degrades_to_single_model():
    """Fix 4: a refuter backend ERROR (auth/runtime/transport/timeout) is NOT a refutation. It DEGRADES the claim to
    single-model so it can still pass on the evidence gate - it must NEVER blanket-refute (that was the outage)."""
    v = check_claim(diag(RECORD_EV), FakeRefuter(error="Timeout"))
    assert v.verified is True and v.mode == "single-model"
    assert any("degrad" in r.lower() for r in v.reasons)


def test_e_refuter_crash_degrades_to_single_model():
    v = check_claim(diag(RECORD_EV), FakeRefuter(raises=TimeoutError("slow")))
    assert v.verified is True and v.mode == "single-model"
    assert any("degrad" in r.lower() and "TimeoutError" in r for r in v.reasons)


def test_e_degraded_claim_still_rejected_without_evidence():
    """Degrade is not a free pass: with no concrete evidence the evidence gate still rejects the claim."""
    v = check_claim(diag(ASSERTION_EV), FakeRefuter(error="Timeout"))
    assert v.verified is False and any("no concrete evidence" in r for r in v.reasons)


def test_e_fail_closed_on_no_key_at_claim_level():
    # No refuter at all, cross-model required: even a well-evidenced claim cannot pass the independent check.
    v = check_claim(diag(RECORD_EV), None, require_cross_model=True)
    assert v.verified is False and any("no groq key" in r for r in v.reasons)


# ---------------------------------------------------------------- the external path redacts (no raw secret leaves)

class CapturingClient:
    """Stands in for groq.Groq: records the messages it is asked to send, returns a canned 'not refuted'."""

    def __init__(self):
        self.sent = []

        class _Completions:
            def create(inner, **kw):
                self.sent.append(kw["messages"])
                return type("R", (), {"choices": [type("C", (), {
                    "message": type("M", (), {"content": '{"refuted": false, "reasons": []}'})()})()]})()

        self.chat = type("Chat", (), {"completions": _Completions()})()


def test_external_path_minimal_no_names_or_secrets_leave():
    """The EXTERNAL (non-Claude) payload is minimised: the free-text diagnosis statement and free-text evidence
    fields (which can carry staff NAMES) are dropped; only redacted structural evidence leaves (fixes 1 + 5)."""
    secret_vault_value = "myvaultvalue12345xyz"
    claim = Claim(
        "diagnosis",
        "customer kc_abcdef0123456789abcd and Bearer tok_abcdefghij0123456789 for principal@globex.example.net "
        f"on +201012345678 with key {secret_vault_value} - Signatory reported it",
        [{"kind": "odoo_read", "model": "stock.move", "id": "1",
          "source": "https://helpdesk.example.com/my/ticket/5/abcdef123456",   # a secret in a STILL-sent field
          "observed": "Signatory approved it", "note": "staff said so"}],
    )
    cap = CapturingClient()
    rf = GroqRefuter(client=cap, model="unit-test", backend=FakeKeyring())   # empty vault, no real keyring read
    reply = rf.refute(claim, redact_fn=lambda t, hashes=(): redact(t, {value_hash(secret_vault_value)}))
    assert reply.refuted is False                               # the canned client said not refuted

    blob = json.dumps(cap.sent)
    # Secrets from the statement AND the structural field are redacted; names in free text are not forwarded.
    for leaked in ("kc_abcdef0123456789abcd", "tok_abcdefghij0123456789", "principal@globex.example.net",
                   "201012345678", secret_vault_value, "abcdef123456", "Signatory", "staff said so"):
        assert leaked not in blob, f"leaked to the external model: {leaked}"
    assert "[REDACTED:" in blob                                 # redaction visibly fired on the sent source field
    assert "IN/00001/26" in redact("IN/00001/26")              # record refs survive redaction


# ---------------------------------------------------------------- backend selection

def test_default_backend_is_independent_claude_session():
    """The DEFAULT refuter is an independent in-subscription Claude session built on our own runner (strong, free,
    no egress), NOT a weak local model and NOT the external groq path."""
    from twin_agent.checks.refuter_xmodel import ClaudeRefuter
    from twin_agent.testing.fakes import FakeClaude
    rf = make_refuter({}, runner=FakeClaude())
    assert isinstance(rf, ClaudeRefuter)


def test_make_refuter_none_without_runner():
    assert make_refuter({}) is None                             # no runner, default backend -> single-model


def test_claude_refuter_passes_evidence_claim_and_fail_closed_on_session_error():
    from twin_agent.testing.fakes import FakeClaude
    good = FakeClaude()                                         # default refuter response = {"refuted": false}
    v = check_claim(diag(RECORD_EV), make_refuter({}, runner=good))
    assert v.verified is True and v.mode == "cross-model"
    assert good.calls and good.calls[-1]["kind"] == "refuter"

    broken = FakeClaude()
    broken.fail_kinds.add("refuter")                            # the refuter SESSION errors (e.g. invalid --model)
    v2 = check_claim(diag(RECORD_EV), make_refuter({}, runner=broken))
    # Fix 4: a failed refuter session DEGRADES to single-model (claims still pass on evidence), never refute-all.
    assert v2.verified is True and v2.mode == "single-model" and any("degrad" in r.lower() for r in v2.reasons)


def test_claude_refuter_veto_not_rescue():
    from twin_agent.testing.fakes import FakeClaude
    veto = FakeClaude()
    veto.responses["refuter"] = {"refuted": True, "reasons": ["a different root cause fits"]}
    v = check_claim(diag(RECORD_EV), make_refuter({}, runner=veto))
    assert v.verified is False and any("refuted by" in r for r in v.reasons)


def test_groq_backend_degrades_to_single_model_when_sdk_absent(monkeypatch):
    """Fix 2: groq backend selected + live + key present, but the groq SDK is NOT installed -> degrade to
    single-model (None), NEVER a blanket refute-all."""
    monkeypatch.setenv("GROQ_API_KEY", "k")
    # Simulate "SDK not installed" regardless of the venv: hide only `groq` from the real detector, so the test
    # holds whether or not requirements.txt (which lists groq) was installed.
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **k: None if name == "groq" else real_find_spec(name, *a, **k))
    assert rx._groq_sdk_present() is False
    cfg = {"run_mode": "live", "verify": {"backend": "groq"}}
    assert make_refuter(cfg, backend=FakeKeyring(), key_present=True) is None


def test_groq_backend_builds_refuter_when_sdk_present(monkeypatch):
    """Counterpart: live + key + SDK present -> a GroqRefuter. The client is lazy, so construction makes no
    network call and needs no real SDK; presence is forced so this also holds in a venv without groq."""
    monkeypatch.setattr(rx, "_groq_sdk_present", lambda: True)
    cfg = {"run_mode": "live", "verify": {"backend": "groq", "groq_model": "m"}}
    rf = make_refuter(cfg, backend=FakeKeyring(), key_present=True)
    assert isinstance(rf, GroqRefuter) and rf.model == "m"


def test_groq_backend_is_live_gated():
    """The EXTERNAL groq backend is refused in observe even with a key (fix 1: no third-party egress in observe)."""
    cfg = {"run_mode": "dry_run", "verify": {"backend": "groq"}}
    assert make_refuter(cfg, backend=FakeKeyring(), key_present=True) is None


def test_make_refuter_builds_groq_when_client_injected():
    rf = make_refuter({"verify": {"groq_model": "m"}}, client=CapturingClient())
    assert isinstance(rf, GroqRefuter) and rf.model == "m"


def test_make_refuter_panel_of_two_claude_models():
    from twin_agent.checks.refuter_xmodel import PanelRefuter
    from twin_agent.testing.fakes import FakeClaude
    rf = make_refuter({"verify": {"panel": ["opus", "sonnet"]}}, runner=FakeClaude())
    assert isinstance(rf, PanelRefuter) and len(rf.refuters) == 2
    assert [r.model for r in rf.refuters] == ["opus", "sonnet"]


def test_make_refuter_disabled_by_config():
    assert make_refuter({"verify": {"cross_model_enabled": False}}, client=CapturingClient()) is None


def test_build_command_passes_refuter_model_and_defaults_different_from_worker():
    from twin_agent.spawner import build_command
    # explicit refuter model is passed through as --model
    cmd = build_command("p", kind="refuter", session_id=None, cfg={"verify": {"refuter_model": "claude-x"}})
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "claude-x"
    # with no explicit refuter model but a pinned worker model, the refuter defaults to a DIFFERENT model
    cmd2 = build_command("p", kind="refuter", session_id=None, cfg={"claude": {"model": "opus"}})
    assert cmd2[cmd2.index("--model") + 1] != "opus"
    # with NEITHER set, no --model is pinned (CLI default) so a default install can't veto on a bad alias
    cmd3 = build_command("p", kind="refuter", session_id=None, cfg={})
    assert "--model" not in cmd3


# ---------------------------------------------------------------- claims out of a packet + packet-level decision

def test_extract_claims_gets_diagnosis_and_fix():
    kinds = [c.kind for c in extract_claims(canned_packet())]
    assert kinds == ["diagnosis", "fix"]


def test_verify_packet_cross_model_pass_and_veto():
    p = canned_packet()                                         # runbook class + odoo_read + past_answer evidence
    ok = verify_packet(p, runbook=RUNBOOK, judges_passed=True, read_user_exists=True, refuter=FakeRefuter(refuted=False))
    assert ok.verified is True and ok.mode == "cross-model"

    veto = verify_packet(p, runbook=RUNBOOK, judges_passed=True, read_user_exists=True,
                         refuter=FakeRefuter(refuted=True, reasons=["wrong root cause"]))
    assert veto.verified is False


def test_verify_packet_cannot_rescue_failed_claude_judges():
    p = canned_packet()
    r = verify_packet(p, runbook=RUNBOOK, judges_passed=False, read_user_exists=True, refuter=FakeRefuter(refuted=False))
    assert r.verified is False                                  # cross-model never rescues; it only vetoes


def test_verify_packet_single_model_degraded_when_no_key():
    p = canned_packet()
    r = verify_packet(p, runbook=RUNBOOK, judges_passed=True, read_user_exists=True, refuter=None)
    assert r.verified is True and r.mode == "single-model"
    assert any("single-model" in x for x in r.reasons)
    assert rx.mode_label(r.verified, r.mode) == "verified (single-model)"


def test_fix_claim_rejected_when_records_unbacked():
    p = canned_packet()
    p["proposed_action"]["records"] = ["stock.move 999"]       # no evidence item reads 999
    r = verify_packet(p, runbook=RUNBOOK, judges_passed=True, read_user_exists=True, refuter=FakeRefuter(refuted=False))
    assert r.verified is False and any("no evidence item reads" in x for x in r.reasons)


# ---------------------------------------------------------------- flow wiring (on_packet uses the gate)

def test_verify_packet_degrades_to_single_model_on_refuter_error():
    """Fix 4 at the packet level: a raised auth/runtime error on the refuter degrades the WHOLE packet to
    single-model and the well-evidenced claims still pass - NOT all-refuted (the T1 timer can still schedule)."""
    p = canned_packet()
    r = verify_packet(p, runbook=RUNBOOK, judges_passed=True, read_user_exists=True,
                      refuter=FakeRefuter(raises=ConnectionError("401 auth")))
    assert r.verified is True and r.mode == "single-model"
    assert any("degrad" in x.lower() for x in r.reasons)


def test_groq_runtime_error_degrades_not_refute_all():
    """The EXTERNAL groq path: a client that raises (auth/network) degrades to single-model, never refute-all."""
    class _Raising:
        def __init__(self):
            outer = self

            class _Completions:
                def create(self, **kw):
                    raise RuntimeError("401 Unauthorized")
            self.chat = type("Chat", (), {"completions": _Completions()})()

    v = check_claim(diag(RECORD_EV), GroqRefuter(client=_Raising(), model="m"))
    assert v.verified is True and v.mode == "single-model"


def test_unreadable_refuter_output_still_vetoes():
    """Guard: an UNREADABLE/non-JSON refuter answer (which injected claim text could steer) is NOT a degrade -
    it stays a fail-closed veto, so a steered 'blank' answer can never pass a claim."""
    class _Garbage:
        def __init__(self):
            class _Completions:
                def create(self, **kw):
                    return type("R", (), {"choices": [type("C", (), {
                        "message": type("M", (), {"content": "not json at all"})()})()]})()
            self.chat = type("Chat", (), {"completions": _Completions()})()

    v = check_claim(diag(RECORD_EV), GroqRefuter(client=_Garbage(), model="m"))
    assert v.verified is False and v.mode == "cross-model"


def test_panel_refuter_propagates_transient_error():
    """Fix 4: a PANEL member whose BACKEND errored must propagate transient=True, so the gate degrades instead of
    letting one member's dead session refute every claim."""
    from twin_agent.checks.refuter_xmodel import PanelRefuter
    reply = PanelRefuter([FakeRefuter(error="Timeout"), FakeRefuter(refuted=False)]).refute(diag(RECORD_EV))
    assert reply.error and reply.transient is True


def test_mode_label_is_honest_about_model_diversity():
    """Fix 3: the card-facing label must not imply a different model when none is configured (same-model session)."""
    assert rx.mode_label(True, "cross-model", diverse=False) == "verified (independent refuter, same model)"
    assert rx.mode_label(True, "cross-model", diverse=True) == "verified (cross-model)"
    assert rx.mode_label(False, "single-model") == "not verified (single-model)"


def test_refuter_different_model_claim_is_honest():
    """Fix 3: the default refuter must not UNCONDITIONALLY claim a different model (true only when a model is
    pinned). The instructions say 'when configured', and make_refuter reflects the real pin on the object."""
    from twin_agent.spawner import refuter_model
    from twin_agent.testing.fakes import FakeClaude
    assert "on a different model" not in rx.REFUTER_INSTRUCTIONS            # the old unconditional claim is gone
    assert "when" in rx.REFUTER_INSTRUCTIONS and "configured" in rx.REFUTER_INSTRUCTIONS
    assert refuter_model({}) is None                                        # unpinned: no forced model pin
    assert refuter_model({"claude": {"model": "opus"}}) not in (None, "opus")   # pinned worker -> different refuter
    # unpinned install: the ClaudeRefuter carries no model (same model as the worker), honestly
    assert rx.make_refuter({}, runner=FakeClaude()).model is None
    assert rx.make_refuter({"claude": {"model": "opus"}}, runner=FakeClaude()).model not in (None, "opus")


def test_same_model_config_card_is_not_labelled_cross_model(cfg, bus, clock, tmp_home):
    """Fix 3: an explicit verify.refuter_model EQUAL to claude.model is the SAME model. The draft card must NOT say
    'cross-model' (it did, because the old code keyed on bool(refuter_model), presence not difference)."""
    import asyncio

    from twin_agent import listener, paths
    from twin_agent.bus import Topics
    from twin_agent.db import StateDB
    from twin_agent.flow import Flow
    from twin_agent.testing.fakes import make_event
    cfg["claude"] = dict(cfg.get("claude") or {}, model="opus")
    cfg["verify"] = dict(cfg.get("verify") or {}, refuter_model="opus")
    db = StateDB(paths.state_db(), clock, cfg)
    cards: list = []
    bus.subscribe(Topics.CARD_OUT, lambda c: cards.append(c))
    try:
        f = Flow(db, cfg, bus, clock, listener._dry_adapters())
        rid = db.create_request(make_event("WAVE/00001 cannot validate", msg_id=1))
        db.save_packet(rid, canned_packet(rid), True, [])
        asyncio.run(f._draft_card(rid, canned_packet(rid), True, [{"passed": True}], mode="cross-model"))
        asyncio.run(bus.drain())
        text = cards[-1]["card"].text if isinstance(cards[-1], dict) else cards[-1].text
        assert "cross-model" not in text
        assert "same model" in text
    finally:
        db.close()


def test_flow_verify_wires_to_cross_model_gate(cfg, bus, clock, tmp_home):
    from twin_agent import listener, paths
    from twin_agent.db import StateDB
    from twin_agent.flow import Flow
    db = StateDB(paths.state_db(), clock, cfg)
    try:
        f = Flow(db, cfg, bus, clock, listener._dry_adapters())
        res = f._verify(canned_packet(), judges_passed=True)    # _verify is what on_packet calls
        # Default backend is the independent in-subscription Claude session built on the (fake) runner.
        assert res.mode == "cross-model" and hasattr(res, "verified")
        assert any(c["kind"] == "refuter" for c in f.adapters.claude.calls)
    finally:
        db.close()
