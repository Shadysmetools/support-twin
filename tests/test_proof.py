"""Tests for proof/pipeline.py (BOARD): the orchestrator runs end to end on fakes, with no network, no browser,
no ffmpeg and no ElevenLabs. Covers the proof pair vs how-to-only split, the privacy block, the verify-first
guidance route, and the observe-safe "prepared, not posted" wire-in."""
from __future__ import annotations

from twin_agent.proof import pipeline
from twin_agent.proof.fakes import FakeReproducer, fake_adapters
from twin_agent.testing.fakes import canned_packet
from twin_agent.testing.fake_odoo import FakeOdoo


# ---------------------------------------------------------------- plan + preflight

def test_plan_picks_proof_kind():
    p = canned_packet()
    assert pipeline.plan({"id": "REQ-0142"}, p)["proof_kind"] == "quick_fix_reread"
    assert pipeline.plan({}, dict(p, tier="T3"))["proof_kind"] == "narrated_video"
    howto = pipeline.plan({}, dict(p, tier="T1", proposed_action=None))
    assert howto["proof_kind"] == "howto_library_or_walkthrough" and howto["is_fix"] is False


def test_preflight_lists_known_blockers(cfg):
    b = pipeline.preflight(cfg)
    assert any("Shopify" in x for x in b) and any("Voice Clone" in x for x in b)
    assert any("dry_run" in x for x in b)
    cfg2 = dict(cfg, proof={"cleared": list(pipeline.KNOWN_BLOCKERS)}, run_mode="live",
                consent={"d2_voice_ref": "D2"})
    assert pipeline.preflight(cfg2) == []


def test_privacy_scan_finds_tokens():
    r = pipeline.privacy_scan("open https://helpdesk.example.com/my/ticket/5/abcdef123456", ["call 01012345678"])
    assert r["status"] == "blocked" and not r["clean"] and "portal_token" in r["findings"]


# ---------------------------------------------------------------- observe: real adapters all block (no fakes)

def test_observe_blocks_everything_no_fabrication(cfg):
    """Production path in observe must NOT fabricate a proof: every real step blocks with a reason."""
    out = pipeline.run("REQ-0001", cfg)                       # real adapters, run_mode dry_run
    assert out["status"] == "blocked"
    assert out["share_link"] is None and out["posted"] is False and out["simulated"] is False
    assert out["why"]                                         # a plain reason that needs a live key/dep


# ---------------------------------------------------------------- end to end on fakes

def test_run_end_to_end_with_fakes(cfg):
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=canned_packet("REQ-0142"), adapters=fake_adapters())
    assert out["status"] == "prepared" and out["posted"] is False
    assert out["simulated"] is True
    assert out["share_link"].startswith("https://wiki.invalid/")   # unmistakably not a real Wiki URL
    assert [s["step"] for s in out["steps"]][:5] == list(pipeline.STEPS[2:7])


def test_proof_pair_for_fix_and_howto_only_for_inquiry(cfg):
    fix = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=canned_packet("REQ-0142"), adapters=fake_adapters())
    assert fix["route"] == "proof_pair"
    assert fix["before_path"] and fix["after_path"]            # BEFORE repro + AFTER fix = the proof pair

    inquiry = pipeline.run("REQ-0200", cfg, request={"id": "REQ-0200", "company": "EG"},
                           packet=dict(canned_packet("REQ-0200"), tier="T1", proposed_action=None),
                           adapters=fake_adapters())
    assert inquiry["route"] == "howto"
    assert inquiry["before_path"] is None and inquiry["after_path"]   # how-to = AFTER only


def test_secret_in_frame_blocks_delivery(cfg):
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=canned_packet("REQ-0142"),
                       adapters=fake_adapters(reproduce=FakeReproducer(secret_in_frame=True)))
    assert out["status"] == "blocked"
    assert "portal_token" in out["privacy"]["findings"] and out["privacy"]["clean"] is False
    assert out["share_link"] is None and out["note_draft"] is None and out["posted"] is False


def test_not_reproduced_routes_to_guidance_and_drops_dev_ticket(cfg):
    dev_packet = dict(canned_packet("REQ-0142"), ticket_draft={"parts": [{"type": "dev", "subject": "x"}]})
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=dev_packet, adapters=fake_adapters(reproduce=FakeReproducer("not_reproduced")))
    assert out["route"] == "guidance"
    assert out["guidance_reply"] and out["note_draft"] is None
    assert out["after_path"] and out["before_path"] is None               # guidance how-to is AFTER only
    # Verify-first is REAL, not self-certified: the dev part is dropped from the attached packet and the gate the
    # helpdesk flow consults returns False.
    attached = out["packet"]
    assert attached["proof"]["route"] == "guidance"
    assert all(p.get("type") != "dev" for p in attached["ticket_draft"]["parts"])
    assert pipeline.should_offer_dev_ticket(attached) is False


def test_user_error_also_routes_to_guidance(cfg):
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=canned_packet("REQ-0142"),
                       adapters=fake_adapters(reproduce=FakeReproducer("user_error")))
    assert out["route"] == "guidance" and out["dev_ticket"] is False
    assert pipeline.should_offer_dev_ticket(out["packet"]) is False


def test_reproduced_fix_keeps_dev_ticket_gate_open(cfg):
    """Contrast: a reproduced fix with a dev part still offers the dev ticket (the guidance drop is meaningful)."""
    dev_packet = dict(canned_packet("REQ-0142"), ticket_draft={"parts": [{"type": "dev", "subject": "x"}]})
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=dev_packet, adapters=fake_adapters())
    assert out["route"] == "proof_pair"
    assert pipeline.should_offer_dev_ticket(out["packet"]) is True


class _Capture:
    """A minimal capture adapter with full control over the fields the privacy gate inspects."""

    def __init__(self, **fields):
        self.fields = {"step": "reproduce_capture", "status": "ok", "verdict": "reproduced",
                       "before_path": "b.mp4", "after_path": "a.mp4", "screenshots": [], "evidence": {},
                       "frame_texts": ["Odoo screen, all clean"], "simulated": False}
        self.fields.update(fields)

    def available(self, cfg):
        return None

    def capture(self, request, packet, kind, cfg):
        return dict(self.fields)


class _Muxer:
    def __init__(self, **fields):
        self.fields = {"step": "mux", "status": "ok", "mp4_path": "p.mp4", "captions_path": None, "simulated": False}
        self.fields.update(fields)

    def available(self, cfg):
        return None

    def combine(self, request_id, capture, narration, cfg):
        return dict(self.fields)


def test_empty_ocr_list_fails_closed(cfg):
    """An EMPTY OCR list (not just None) on a real capture must still block - the fail-open hole."""
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=canned_packet("REQ-0142"),
                       adapters=fake_adapters(reproduce=_Capture(frame_texts=[])))
    assert out["status"] == "blocked" and out["privacy"]["findings"] == ["no_ocr"]


def test_all_blank_ocr_fails_closed(cfg):
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=canned_packet("REQ-0142"),
                       adapters=fake_adapters(reproduce=_Capture(frame_texts=["", "   ", "\n"])))
    assert out["status"] == "blocked" and out["privacy"]["findings"] == ["no_ocr"]


def test_unreadable_captions_fail_closed_on_real_mux(cfg):
    """A missing/unreadable captions file on a REAL mux must block (it silently became '' before)."""
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=canned_packet("REQ-0142"),
                       adapters=fake_adapters(reproduce=_Capture(),
                                              mux=_Muxer(captions_path="does-not-exist.srt")))
    assert out["status"] == "blocked" and "captions" in out["why"].lower()


def test_missing_captions_path_fail_closed_on_real_mux(cfg):
    """A REAL mux that returns NO captions_path at all must also block - it used to silently pass with '' captions."""
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=canned_packet("REQ-0142"),
                       adapters=fake_adapters(reproduce=_Capture(), mux=_Muxer(captions_path=None)))
    assert out["status"] == "blocked" and "captions" in out["why"].lower()


def test_frame_ocr_coverage_gap_blocks(cfg):
    """Fix 5: presence was not coverage. A real capture with MORE frames than OCR texts (a frame left unscanned)
    must block - fail closed. Before, any non-empty frame_texts passed even if it covered only one of many frames."""
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=canned_packet("REQ-0142"),
                       adapters=fake_adapters(reproduce=_Capture(frames=["f1.png", "f2.png"],
                                                                 frame_texts=["only one frame scanned"])))
    assert out["status"] == "blocked" and "frame_ocr_gap" in out["privacy"]["findings"]


def test_screenshot_ocr_coverage_gap_blocks(cfg):
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=canned_packet("REQ-0142"),
                       adapters=fake_adapters(reproduce=_Capture(screenshots=["s1.png", "s2.png"],
                                                                 screenshot_texts=["only one"])))
    assert out["status"] == "blocked" and "screenshot_ocr_gap" in out["privacy"]["findings"]


def test_blank_item_in_coverage_blocks(cfg):
    """A frame count that matches but with a blank item still lacks text for that frame -> block."""
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=canned_packet("REQ-0142"),
                       adapters=fake_adapters(reproduce=_Capture(frames=["f1.png", "f2.png"],
                                                                 frame_texts=["real text", "   "])))
    assert out["status"] == "blocked" and "frame_ocr_gap" in out["privacy"]["findings"]


def test_empty_captions_file_blocks_on_real_mux(cfg, tmp_path):
    """Fix 5: an existing-but-empty/whitespace captions file on a REAL mux is a BLOCK, not a pass (it used to be
    read as '' and clear the scan)."""
    capp = tmp_path / "proof.srt"
    capp.write_text("   \n\t\n", encoding="utf-8")
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=canned_packet("REQ-0142"),
                       adapters=fake_adapters(reproduce=_Capture(), mux=_Muxer(captions_path=str(capp))))
    assert out["status"] == "blocked" and "captions" in out["why"].lower()


def test_real_capture_with_screenshots_but_no_screenshot_ocr_blocks(cfg):
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=canned_packet("REQ-0142"),
                       adapters=fake_adapters(reproduce=_Capture(screenshots=["shot-01.png"],
                                                                 screenshot_texts=None)))
    assert out["status"] == "blocked" and out["privacy"]["findings"] == ["no_ocr_screenshot"]


def test_screenshot_text_is_scanned_for_secrets(cfg):
    """Screenshots must be scanned too: a token in a screenshot's OCR text blocks delivery."""
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=canned_packet("REQ-0142"),
                       adapters=fake_adapters(reproduce=_Capture(
                           simulated=True, screenshots=["shot.png"],
                           screenshot_texts=["open https://helpdesk.example.com/my/ticket/5/abcdef123456"])))
    assert out["status"] == "blocked" and "portal_token" in out["privacy"]["findings"]


def test_unknown_verdict_routes_to_needs_human_not_proof_pair(cfg):
    """A verdict the pipeline does not recognise must NOT silently proceed to a dev ticket."""
    dev_packet = dict(canned_packet("REQ-0142"), ticket_draft={"parts": [{"type": "dev", "subject": "x"}]})
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=dev_packet, adapters=fake_adapters(reproduce=FakeReproducer("weird_new_verdict")))
    assert out["route"] == "needs_human"
    assert out["dev_ticket"] is False
    assert pipeline.should_offer_dev_ticket(out["packet"]) is False
    assert all(p.get("type") != "dev" for p in out["packet"]["ticket_draft"]["parts"])


def test_proof_cleared_rejects_forged_proof_requires_provenance(tmp_home):
    """Fix 1(b): a worker-forged proof dict (right fields, no pipeline signature) must NOT clear the gate. Only a
    proof the pipeline signed (attach) for THIS request clears it; a signed proof pasted into another request's
    packet is rejected (the signature binds the request id)."""
    forged = {"request_id": "REQ-0142", "proof": {"status": "prepared", "route": "proof_pair",
                                                   "verdict": "reproduced"}}
    assert pipeline.proof_cleared(forged) is False
    signed = pipeline.attach({"request_id": "REQ-0142"},
                             {"request_id": "REQ-0142", "status": "prepared", "route": "proof_pair",
                              "verdict": "reproduced"})
    assert pipeline.proof_cleared(signed) is True
    # the key now EXISTS (attach created it), so these exercise the HMAC comparison, not just the no-key path:
    assert pipeline.proof_cleared(forged) is False                     # no sig at all, key present
    garbage = {"request_id": "REQ-0142", "proof": dict(signed["proof"], sig="0" * 64)}
    assert pipeline.proof_cleared(garbage) is False                    # wrong sig fails compare_digest
    moved = dict(signed, request_id="REQ-0999")           # a valid signature cannot be moved to another request
    assert pipeline.proof_cleared(moved) is False


def test_should_offer_dev_ticket_fail_safe_when_proof_absent():
    """No proof artifact at all (the runtime reality) -> never auto-offer a dev ticket."""
    dev_packet = dict(canned_packet("REQ-0142"), ticket_draft={"parts": [{"type": "dev", "subject": "x"}]})
    assert "proof" not in dev_packet
    assert pipeline.should_offer_dev_ticket(dev_packet) is False
    assert pipeline.should_offer_dev_ticket(dict(dev_packet, proof={"status": "blocked"})) is False


def test_real_capture_without_ocr_fails_closed(cfg):
    """A non-simulated capture that yields no OCR frame text must block delivery (privacy fails closed)."""
    class _NoOcrCapture:
        def available(self, cfg):
            return None

        def capture(self, request, packet, kind, cfg):
            return {"step": "reproduce_capture", "status": "ok", "verdict": "reproduced",
                    "before_path": "b.mp4", "after_path": "a.mp4", "screenshots": [], "evidence": {},
                    "frame_texts": None, "simulated": False}

    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=canned_packet("REQ-0142"), adapters=fake_adapters(reproduce=_NoOcrCapture()))
    assert out["status"] == "blocked" and out["privacy"]["findings"] == ["no_ocr"]
    assert out["share_link"] is None and out["posted"] is False


# ---------------------------------------------------------------- observe-safe wire-in

def test_wiki_link_prepared_into_note_but_not_posted(cfg):
    odoo = FakeOdoo()
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=canned_packet("REQ-0142"), adapters=fake_adapters(), odoo=odoo)
    nd = out["note_draft"]
    assert nd["has_link"] and out["share_link"] in nd["note"]      # link prepared into the draft
    assert nd["posted"] is False and out["posted"] is False
    assert odoo.writes == []                                       # nothing created / written / message_posted


def test_everything_redacted(cfg):
    leaky = dict(canned_packet("REQ-0142"),
                 understanding="open /my/ticket/5/abcdef123456zzzz and call 01012345678")
    out = pipeline.run("REQ-0142", cfg, request={"id": "REQ-0142", "company": "EG"},
                       packet=leaky, adapters=fake_adapters())
    assert out["status"] == "prepared"
    assert "abcdef123456" not in out["narration_script"]           # token redacted out of the script
    assert "abcdef123456" not in out["note_draft"]["note"]
    assert out["privacy"]["clean"] is True
