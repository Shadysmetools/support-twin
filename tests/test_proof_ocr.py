"""Tests for the LOCAL frame/screenshot OCR seam (proof/ocr.py) and its wiring into RealReproducer + run().

No real OCR engine is ever called here: ``ocr_images`` takes an INJECTABLE ``engine`` (a callable image_path->str)
and ``RealReproducer`` takes an injectable ``ocr_engine``; both default to a real LOCAL engine resolved at call time.
The one test that exercises the real engine is guarded with ``skipif`` when none is installed. The fail-closed
contract of ``run()``'s privacy step is proven end to end: a clean OCR passes, a missing engine or an uncovered
screenshot blocks, and a secret in the OCR text blocks delivery.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from twin_agent.proof import ocr, pipeline
from twin_agent.proof.ocr import NoOCREngine, ocr_images, resolve_engine
from twin_agent.proof.pipeline import RealReproducer


# ---------------------------------------------------------------- ocr_images (the seam)

def test_ocr_images_uses_injected_engine_one_text_per_image():
    paths = ["/a/01.png", "/a/02.png", "/a/03.png"]
    canned = {"/a/01.png": "Odoo WAVE/00001 done", "/a/02.png": "status: Validated", "/a/03.png": "home"}
    out = ocr_images(paths, engine=lambda p: canned[p])
    assert out == ["Odoo WAVE/00001 done", "status: Validated", "home"]


def test_ocr_images_blank_string_when_image_yields_nothing():
    # An image the engine reads nothing from -> a blank string (never dropped, so coverage counts stay aligned).
    out = ocr_images(["/a/01.png", "/a/02.png"], engine=lambda p: "" if p.endswith("02.png") else "text")
    assert out == ["text", ""]


def test_ocr_images_empty_paths_returns_empty_without_resolving(monkeypatch):
    # No images -> nothing to OCR; must not even try to resolve an engine.
    monkeypatch.setattr(ocr, "resolve_engine", lambda prefer="auto": (_ for _ in ()).throw(AssertionError("resolved")))
    assert ocr_images([]) == []


def test_ocr_images_raises_no_engine_when_none_available(monkeypatch):
    # engine not injected AND no local engine resolvable -> signal (typed) so the pipeline stays FAIL-CLOSED.
    monkeypatch.setattr(ocr, "resolve_engine", lambda prefer="auto": None)
    with pytest.raises(NoOCREngine):
        ocr_images(["/a/01.png"])


def test_ocr_images_per_image_failure_propagates():
    # A read/engine failure on one image is not a blank: it propagates so the caller fails closed (no partial list).
    def boom(p):
        if p.endswith("02.png"):
            raise OSError("cannot read image")
        return "ok"
    with pytest.raises(OSError):
        ocr_images(["/a/01.png", "/a/02.png"], engine=boom)


def test_resolve_engine_unknown_preference_is_none(monkeypatch):
    # An unknown/garbage proof.ocr_engine value must fail closed (treated as 'none'), never fall back to 'auto'.
    assert resolve_engine("banana") is None
    assert resolve_engine("none") is None


# ---------------------------------------------------------------- RealReproducer OCR wiring

def _fake_kit(tmp_path: Path) -> Path:
    scripts = tmp_path / "kit" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("record.py", "tts.py", "video_trim.py"):
        (scripts / name).write_text("# stub\n", encoding="utf-8")
    return tmp_path / "kit"


def _repro_cfg(kit: Path, **over) -> dict:
    proof = {"kit_path": str(kit), "capture_base": "https://sandbox.example.test",
             "cleared": ["qa_user"], "plans": {"before": "b.json", "after": "a.json"}}
    proof.update(over)
    return {"run_mode": "live", "proof": proof, "consent": {}}


def _record_runner(n_shots: int = 1):
    """A fake kit runner that writes what record.py would: results.json, a video, and ``n_shots`` screenshots."""
    from tests.test_proof_adapters import RecordingRunner, _out_of

    def on_call(call):
        out = _out_of(call["cmd"])
        out.mkdir(parents=True, exist_ok=True)
        rows = [{"result": "FAIL"}] if out.name == "before" else [{"result": "PASS"}]
        (out / "results.json").write_text(json.dumps({"summary": f"{out.name} 1/1", "results": rows}),
                                          encoding="utf-8")
        (out / f"{out.name}.mp4").write_bytes(b"vid")
        for i in range(1, n_shots + 1):
            (out / f"shot-{i:02d}.png").write_bytes(b"png")
    return RecordingRunner(rc=0, on_call=on_call)


def _has_ffmpeg(monkeypatch):
    monkeypatch.setattr(pipeline.shutil, "which", lambda name: "/usr/bin/" + name)


def test_capture_ocrs_screenshots_with_injected_engine(tmp_home, tmp_path, monkeypatch):
    _has_ffmpeg(monkeypatch)
    rep = RealReproducer(runner=_record_runner(n_shots=2), ocr_engine=lambda p: "Odoo: order validated, status Done")
    out = rep.capture({"id": "REQ-1"}, {}, "narrated_video", _repro_cfg(_fake_kit(tmp_path)))
    assert out["status"] == "ok" and out["simulated"] is False
    assert len(out["screenshots"]) == 2
    # Real frame + screenshot texts, one non-blank per captured screenshot (no longer None).
    assert out["frame_texts"] == ["Odoo: order validated, status Done"] * 2
    assert out["screenshot_texts"] == ["Odoo: order validated, status Done"] * 2


def test_capture_missing_engine_keeps_frame_texts_none(tmp_home, tmp_path, monkeypatch):
    _has_ffmpeg(monkeypatch)
    monkeypatch.setattr(ocr, "resolve_engine", lambda prefer="auto": None)   # no local engine installed
    rep = RealReproducer(runner=_record_runner())                            # default engine -> resolve -> None
    out = rep.capture({"id": "REQ-1"}, {}, "narrated_video", _repro_cfg(_fake_kit(tmp_path)))
    assert out["status"] == "ok"
    assert out["frame_texts"] is None and out["screenshot_texts"] is None    # fail closed, not fabricated


def test_capture_engine_failure_keeps_frame_texts_none(tmp_home, tmp_path, monkeypatch):
    _has_ffmpeg(monkeypatch)

    def boom(p):
        raise OSError("decode failed")
    rep = RealReproducer(runner=_record_runner(n_shots=2), ocr_engine=boom)
    out = rep.capture({"id": "REQ-1"}, {}, "narrated_video", _repro_cfg(_fake_kit(tmp_path)))
    # An image failure must not yield a partial list: it fails closed to None.
    assert out["frame_texts"] is None and out["screenshot_texts"] is None


# ---------------------------------------------------------------- run() privacy step (fail-closed contract)

def _run(tmp_path, monkeypatch, rep):
    from twin_agent.proof.fakes import fake_adapters
    from twin_agent.testing.fakes import canned_packet
    _has_ffmpeg(monkeypatch)
    cfg = _repro_cfg(_fake_kit(tmp_path))
    return pipeline.run("REQ-1", cfg, request={"id": "REQ-1", "company": "EG"},
                        packet=canned_packet("REQ-1"), adapters=fake_adapters(reproduce=rep))


def test_run_privacy_passes_on_clean_ocr(tmp_home, tmp_path, monkeypatch):
    rep = RealReproducer(runner=_record_runner(n_shots=2), ocr_engine=lambda p: "Odoo: WAVE/00001 validated, Done")
    out = _run(tmp_path, monkeypatch, rep)
    assert out["status"] == "prepared"                 # a real capture now CLEARS the privacy step on clean text
    assert out["privacy"]["clean"] is True and out["privacy"]["findings"] == []
    assert out["share_link"] is not None


def test_run_blocks_no_ocr_when_engine_missing(tmp_home, tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "resolve_engine", lambda prefer="auto": None)
    rep = RealReproducer(runner=_record_runner())      # default engine, none resolvable -> frame_texts None
    out = _run(tmp_path, monkeypatch, rep)
    assert out["status"] == "blocked" and out["privacy"]["findings"] == ["no_ocr"]
    assert out["share_link"] is None and out["posted"] is False


def test_run_blocks_on_secret_in_ocr_text(tmp_home, tmp_path, monkeypatch):
    # A real portal token captured in a screenshot must block delivery (the privacy scanner catches it).
    secret = "open https://helpdesk.example.com/my/ticket/5/abcdef123456"
    rep = RealReproducer(runner=_record_runner(), ocr_engine=lambda p: secret)
    out = _run(tmp_path, monkeypatch, rep)
    assert out["status"] == "blocked"
    assert "portal_token" in out["privacy"]["findings"]
    assert out["share_link"] is None
    # The raw OCR token must NOT survive in the returned artifact (steps carry the capture dict): redacted there.
    assert "abcdef123456" not in json.dumps(out)


def test_run_blocks_screenshot_ocr_gap_on_blank(tmp_home, tmp_path, monkeypatch):
    # Two screenshots, one blank OCR: presence is not coverage -> a screenshot left unscanned fails closed.
    # The non-blank one carries a secret that the gap gate blocks before scanning; it must still not leak out.
    secret = "open https://helpdesk.example.com/my/ticket/5/abcdef123456"
    rep = RealReproducer(runner=_record_runner(n_shots=2),
                         ocr_engine=lambda p: "" if p.endswith("02.png") else secret)
    out = _run(tmp_path, monkeypatch, rep)
    assert out["status"] == "blocked"
    assert out["privacy"]["findings"] == ["screenshot_ocr_gap"]
    assert "abcdef123456" not in json.dumps(out)      # raw OCR never survives in the artifact, even on a gap block


# ---------------------------------------------------------------- real engine (guarded)

@pytest.mark.skipif(resolve_engine("auto") is None, reason="no local OCR engine installed (winsdk/winrt or tesseract)")
def test_real_engine_reads_text_from_an_image(tmp_path):
    """Smoke test the REAL local engine when one is actually installed. Renders text with Pillow, OCRs it back.
    Skipped on any machine (incl. this one and CI) without winsdk/winrt or a tesseract binary."""
    from PIL import Image, ImageDraw, ImageFont
    img = tmp_path / "hello.png"
    im = Image.new("RGB", (600, 120), "white")
    font = ImageFont.load_default(size=48)             # the ~10px default is too small for real OCR to read reliably
    ImageDraw.Draw(im).text((20, 30), "HELLO OCR", fill="black", font=font)
    im.save(img)
    texts = ocr_images([str(img)])
    assert isinstance(texts, list) and len(texts) == 1
    assert "HELLO" in texts[0].upper()
