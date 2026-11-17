"""Unit tests for the REAL proof adapters (proof/pipeline.py), wired to the ShipShape kit.

Everything external is injected: a FAKE kit runner (no real subprocess, browser, ffmpeg, ElevenLabs) and a FAKE
Wiki ``http_fn`` (no network). The repo's autouse socket block still guarantees no connection can be made.
Each test asserts one of: the command/args/env-keys built for the kit, the parse of the kit's result into the
Fake's dict shape, or a guard/blocker path returning the right ``_blocked`` reason. The real vault is never touched
(a FakeKeyring backend is threaded into every adapter)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from twin_agent import creds
from twin_agent.proof import ocr, pipeline
from twin_agent.proof.pipeline import (KitResult, RealMuxer, RealNarrator, RealPublisher, RealReproducer)
from twin_agent.testing.fakes import FakeKeyring


@pytest.fixture(autouse=True)
def _no_real_ocr(monkeypatch):
    """No test in this file may call a real OCR engine: force ``resolve_engine`` to report none installed, so a
    RealReproducer that doesn't inject its own engine fails closed (frame_texts None) deterministically on ANY
    machine (incl. one with winsdk installed). Tests that want OCR inject ``ocr_engine=`` explicitly."""
    monkeypatch.setattr(ocr, "resolve_engine", lambda prefer="auto": None)


# ---------------------------------------------------------------- fakes

class RecordingRunner:
    """Stands in for ``_run_kit``: records each call (cmd, secret env-keys, extra_env, cwd) and returns a canned
    KitResult. ``on_call`` can write the files a real kit script would (e.g. record.py's results.json)."""

    def __init__(self, rc: int = 0, stdout: str = "", stderr: str = "", on_call=None):
        self.calls: list[dict] = []
        self.rc, self.stdout, self.stderr, self.on_call = rc, stdout, stderr, on_call

    def __call__(self, cmd, *, secrets=None, extra_env=None, cwd=None, purpose="", request_id=None,
                 timeout=None, backend=None):
        call = {"cmd": list(cmd), "secrets": dict(secrets or {}), "extra_env": dict(extra_env or {}),
                "cwd": cwd, "purpose": purpose, "request_id": request_id, "backend": backend}
        self.calls.append(call)
        if self.on_call:
            self.on_call(call)
        return KitResult(self.rc, self.stdout, self.stderr)


def _out_of(cmd: list[str]) -> Path:
    return Path(cmd[cmd.index("--out") + 1])


class FakeWiki:
    """A fake notify ``http_fn``: records (method, url) and returns the JSON the Wiki client expects, so
    send_wiki runs prepare -> PUT -> confirm (-> folder) (-> share) with no network."""

    def __init__(self, pid: str, share_url: str = "https://wiki.example.com/share/tok", raise_on=None):
        self.pid, self.share_url, self.raise_on = pid, share_url, raise_on
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, data=None, headers=None, timeout=120):
        self.calls.append((method, url))
        if self.raise_on and self.raise_on in url:
            raise RuntimeError("wiki boom")
        if url.endswith("/api/sources/prepare-upload"):
            return 200, json.dumps({"uploadUrl": "https://storage.example/put", "storagePath": "sp/1", "token": "t"})
        if url == "https://storage.example/put":
            return 200, ""
        if url.endswith("/api/sources/confirm-upload"):
            return 200, json.dumps({"source": {"id": "SID1", "project_id": self.pid}})
        if url.endswith("/sources/bulk"):
            return 200, "{}"
        if "/api/share/source/" in url:
            return 200, json.dumps({"url": self.share_url})
        return 200, "{}"

    def hit(self, needle: str) -> bool:
        return any(needle in u for _m, u in self.calls)


# ---------------------------------------------------------------- fixtures / helpers

@pytest.fixture
def kr() -> FakeKeyring:
    return FakeKeyring()


def _fake_kit(tmp_path: Path) -> Path:
    """A throwaway kit dir with the script files the adapters' ``available`` guards look for."""
    scripts = tmp_path / "kit" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("record.py", "tts.py", "video_trim.py"):
        (scripts / name).write_text("# stub\n", encoding="utf-8")
    return tmp_path / "kit"


def _has_ffmpeg(monkeypatch):
    monkeypatch.setattr(pipeline.shutil, "which", lambda name: "/usr/bin/" + name)


# ======================================================================= Reproducer

def _record_runner() -> RecordingRunner:
    def on_call(call):
        out = _out_of(call["cmd"])
        out.mkdir(parents=True, exist_ok=True)
        rows = [{"result": "FAIL"}] if out.name == "before" else [{"result": "PASS"}]
        (out / "results.json").write_text(json.dumps({"summary": f"{out.name} 1/1", "results": rows}),
                                          encoding="utf-8")
        (out / f"{out.name}.mp4").write_bytes(b"vid")
        (out / "shot-01.png").write_bytes(b"png")
    return RecordingRunner(rc=0, on_call=on_call)


def _repro_cfg(kit: Path, **over) -> dict:
    proof = {"kit_path": str(kit), "capture_base": "https://sandbox.example.test",
             "cleared": ["qa_user"], "plans": {"before": "b.json", "after": "a.json"}}
    proof.update(over)
    return {"run_mode": "live", "proof": proof, "consent": {}}


def test_reproducer_builds_record_command_pinned_to_capture_base(tmp_home, tmp_path, monkeypatch):
    _has_ffmpeg(monkeypatch)
    kit = _fake_kit(tmp_path)
    runner = _record_runner()
    rep = RealReproducer(runner=runner)
    out = rep.capture({"id": "REQ-1"}, {}, "narrated_video", _repro_cfg(kit))
    assert out["status"] == "ok" and out["simulated"] is False
    # Both the AFTER and BEFORE captures run record.py, each pinned to capture_base at the command level, never
    # --narrate, and each carrying no vault secret.
    plans_seen = set()
    for call in runner.calls:
        cmd = call["cmd"]
        assert cmd[1].endswith("record.py")
        assert cmd[cmd.index("--base-url") + 1] == "https://sandbox.example.test"
        assert cmd[cmd.index("--allow-host") + 1] == "sandbox.example.test"
        assert "--narrate" not in cmd
        assert call["secrets"] == {}                    # capture carries no vault secret
        plans_seen.update(x for x in cmd if x.endswith(".json"))
    assert plans_seen == {"a.json", "b.json"}           # AFTER (a.json) + BEFORE (b.json)


def test_reproducer_reproduced_verdict_and_proof_pair(tmp_home, tmp_path, monkeypatch):
    _has_ffmpeg(monkeypatch)
    rep = RealReproducer(runner=_record_runner())
    out = rep.capture({"id": "REQ-1"}, {}, "narrated_video", _repro_cfg(_fake_kit(tmp_path)))
    assert out["verdict"] == "reproduced"               # bug FAILs before, PASSes after
    assert out["before_path"] and out["after_path"]
    assert out["screenshots"] and out["frame_texts"] is None   # no local engine -> privacy fails closed in run()


def test_reproducer_not_reproduced_when_before_passes(tmp_home, tmp_path, monkeypatch):
    _has_ffmpeg(monkeypatch)

    def on_call(call):
        out = _out_of(call["cmd"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "results.json").write_text(json.dumps({"summary": "1/1", "results": [{"result": "PASS"}]}),
                                          encoding="utf-8")
        (out / "v.mp4").write_bytes(b"vid")
    rep = RealReproducer(runner=RecordingRunner(rc=0, on_call=on_call))
    out = rep.capture({"id": "REQ-1"}, {}, "narrated_video", _repro_cfg(_fake_kit(tmp_path)))
    assert out["verdict"] == "not_reproduced"


def test_reproducer_guard_paths(tmp_home, tmp_path, monkeypatch):
    _has_ffmpeg(monkeypatch)
    kit = _fake_kit(tmp_path)
    rep = RealReproducer(runner=_record_runner())
    # not live
    assert rep.capture({"id": "R"}, {}, "narrated_video", dict(_repro_cfg(kit), run_mode="dry_run"))["status"] == "blocked"
    # flow safety: empty capture_base
    assert "capture" in rep.capture({"id": "R"}, {}, "narrated_video", _repro_cfg(kit, capture_base=""))["why"].lower()
    # flow safety: the Globex flow tenant
    aric = rep.capture({"id": "R"}, {}, "narrated_video", _repro_cfg(kit, capture_base="https://globex.odoo.com"))
    assert aric["status"] == "blocked" and "FLOW" in aric["why"]
    # qa_user not cleared
    assert rep.capture({"id": "R"}, {}, "narrated_video", _repro_cfg(kit, cleared=[]))["status"] == "blocked"
    # no AFTER plan
    noplan = rep.capture({"id": "R"}, {}, "narrated_video", _repro_cfg(kit, plans={}))
    assert noplan["status"] == "blocked" and "plan" in noplan["why"].lower()


def test_reproducer_ffmpeg_and_kit_missing(tmp_home, tmp_path, monkeypatch):
    kit = _fake_kit(tmp_path)
    monkeypatch.setattr(pipeline.shutil, "which", lambda name: None)
    rep = RealReproducer(runner=_record_runner())
    assert "ffmpeg" in rep.capture({"id": "R"}, {}, "narrated_video", _repro_cfg(kit))["why"]
    _has_ffmpeg(monkeypatch)
    missing = rep.capture({"id": "R"}, {}, "narrated_video", _repro_cfg(tmp_path / "nope"))
    assert "kit not found" in missing["why"]


def test_reproducer_record_failure_blocks(tmp_home, tmp_path, monkeypatch):
    _has_ffmpeg(monkeypatch)
    rep = RealReproducer(runner=RecordingRunner(rc=2, stderr="playwright crashed"))
    out = rep.capture({"id": "R"}, {}, "narrated_video", _repro_cfg(_fake_kit(tmp_path)))
    assert out["status"] == "blocked" and "record.py" in out["why"]


# ======================================================================= Narrator

def _narr_cfg(**over) -> dict:
    proof = {"voice_id": "VOICE123"}
    proof.update(over)
    return {"run_mode": "live", "proof": proof, "consent": {"d2_voice_ref": "D2"}}


def test_narrator_stays_blocked_until_clone_cleared(tmp_home, kr):
    nar = RealNarrator(backend=kr)
    # not live
    assert nar.available(dict(_narr_cfg(), run_mode="dry_run"))
    # live but voice blocker NOT cleared -> the voice blocker, verbatim
    cfg = dict(_narr_cfg(), proof={"voice_id": "V"})
    cfg["proof"].setdefault("cleared", [])
    assert nar.available(cfg) == pipeline.KNOWN_BLOCKERS["voice"]


def test_narrator_available_requires_key_consent_voiceid(tmp_home, kr):
    nar = RealNarrator(backend=kr)
    base = _narr_cfg(cleared=["voice"])
    # cleared but no ElevenLabs key
    assert "ElevenLabs key" in nar.available(base)
    creds.set_secret("elevenlabs", "EL-KEY", backend=kr)
    # key but no D2 consent
    assert "D2" in nar.available(dict(base, consent={}))
    # key + consent but no voice id
    assert "voice id" in nar.available(_narr_cfg(cleared=["voice"], voice_id=""))
    # all present -> available
    assert nar.available(base) is None


def test_narrator_synth_builds_edge_tts_command_no_secret(tmp_home, kr):
    creds.set_secret("elevenlabs", "EL-KEY", backend=kr)
    runner = RecordingRunner(rc=0, stdout="C:/cache/narr.mp3 3.20s new")
    nar = RealNarrator(runner=runner, backend=kr)
    out = nar.synth({"request_id": "REQ-1", "script": "مرحبا", "lang": "ar"},
                    _narr_cfg(cleared=["voice"], tts_voice="ar-EG-SalmaNeural"))
    assert out["status"] == "ok" and out["simulated"] is False
    assert out["audio_path"] == "C:/cache/narr.mp3" and out["text"] == "مرحبا"
    cmd = runner.calls[0]["cmd"]
    assert cmd[1].endswith("tts.py") and "مرحبا" in cmd
    assert cmd[cmd.index("--voice") + 1] == "ar-EG-SalmaNeural"
    # tts.py is edge-tts only: the ElevenLabs vault value must NOT be handed to it.
    assert runner.calls[0]["secrets"] == {} and runner.calls[0]["extra_env"] == {}


def test_narrator_synth_failure_blocks(tmp_home, kr):
    creds.set_secret("elevenlabs", "EL-KEY", backend=kr)
    nar = RealNarrator(runner=RecordingRunner(rc=1, stderr="tts crashed"), backend=kr)
    out = nar.synth({"request_id": "R", "script": "x"}, _narr_cfg(cleared=["voice"]))
    assert out["status"] == "blocked" and "synthesis failed" in out["why"]


# ======================================================================= Muxer

def _mux_cfg(kit: Path, **over) -> dict:
    proof = {"kit_path": str(kit)}
    proof.update(over)
    return {"run_mode": "live", "proof": proof}


def test_muxer_combines_with_relative_paths_and_writes_srt(tmp_home, tmp_path, monkeypatch):
    _has_ffmpeg(monkeypatch)
    kit = _fake_kit(tmp_path)
    (tmp_path / "after.mp4").write_bytes(b"v")
    (tmp_path / "narr.mp3").write_bytes(b"a")
    runner = RecordingRunner(rc=0)
    mux = RealMuxer(runner=runner)
    cap = {"after_path": str(tmp_path / "after.mp4")}
    narr = {"audio_path": str(tmp_path / "narr.mp3")}
    out = mux.combine("REQ-1", cap, narr, _mux_cfg(kit))
    assert out["status"] == "ok" and out["simulated"] is False
    assert out["mp4_path"].endswith("proof.mp4") and not out["mp4_path"].endswith(".original.mp4")
    assert Path(out["captions_path"]).read_text(encoding="utf-8").strip()        # real, non-empty .srt
    call = runner.calls[-1]
    assert call["cmd"][0] == "ffmpeg" and call["cmd"][-1] == "proof.mp4"          # RELATIVE output name
    assert call["cwd"] == str(Path(out["mp4_path"]).parent)                       # ffmpeg runs in the out dir
    assert "0:v" in call["cmd"] and "1:a" in call["cmd"]
    assert "apad" in call["cmd"]                                                  # video, not narration, sets length


def test_muxer_trim_runs_on_capture_before_ffmpeg(tmp_home, tmp_path, monkeypatch):
    _has_ffmpeg(monkeypatch)
    kit = _fake_kit(tmp_path)
    (tmp_path / "after.mp4").write_bytes(b"v")
    (tmp_path / "narr.mp3").write_bytes(b"a")
    runner = RecordingRunner(rc=0)
    mux = RealMuxer(runner=runner)
    mux.combine("REQ-1", {"after_path": str(tmp_path / "after.mp4")},
                {"audio_path": str(tmp_path / "narr.mp3")}, _mux_cfg(kit, trim=True))
    assert runner.calls[0]["cmd"][1].endswith("video_trim.py")                    # trim first
    assert runner.calls[1]["cmd"][0] == "ffmpeg"                                   # mux second


def test_muxer_guard_paths(tmp_home, tmp_path, monkeypatch):
    kit = _fake_kit(tmp_path)
    mux = RealMuxer(runner=RecordingRunner(rc=0))
    monkeypatch.setattr(pipeline.shutil, "which", lambda name: None)
    assert "ffmpeg" in mux.combine("R", {"after_path": "a.mp4"}, {"audio_path": "n.mp3"}, _mux_cfg(kit))["why"]
    _has_ffmpeg(monkeypatch)
    assert "capture video" in mux.combine("R", {}, {"audio_path": "n.mp3"}, _mux_cfg(kit))["why"]
    assert "narration audio" in mux.combine("R", {"after_path": "a.mp4"}, {}, _mux_cfg(kit))["why"]


def test_muxer_ffmpeg_failure_blocks(tmp_home, tmp_path, monkeypatch):
    _has_ffmpeg(monkeypatch)
    (tmp_path / "after.mp4").write_bytes(b"v")
    (tmp_path / "narr.mp3").write_bytes(b"a")
    mux = RealMuxer(runner=RecordingRunner(rc=1, stderr="ffmpeg error"))
    out = mux.combine("R", {"after_path": str(tmp_path / "after.mp4")},
                      {"audio_path": str(tmp_path / "narr.mp3")}, _mux_cfg(_fake_kit(tmp_path)))
    assert out["status"] == "blocked" and "ffmpeg" in out["why"]


# ======================================================================= Publisher

_KIT = Path(r"C:\twin-dev\proof-kit")
# The publisher reuses the kit's real notify.py; skip where the kit checkout is absent (e.g. CI) rather than fail.
needs_kit = pytest.mark.skipif(not (_KIT / "scripts" / "notify.py").exists(),
                               reason="ShipShape kit not available (notify.py reused for Wiki)")


def _pub_cfg(**over) -> dict:
    proof = {"kit_path": str(_KIT), "projects": {"EG": "P-EG"}}
    proof.update(over)
    return {"run_mode": "live", "proof": proof}


def _mp4(tmp_path: Path) -> str:
    p = tmp_path / "proof.mp4"
    p.write_bytes(b"\x00\x00fake mp4 bytes")
    return str(p)


def test_publisher_guard_paths(tmp_home, tmp_path, kr):
    # not live
    pub = RealPublisher(backend=kr)
    assert pub.upload("R", _mp4(tmp_path), "EG", "T-1", dict(_pub_cfg(), run_mode="dry_run"))["status"] == "blocked"
    # no wiki key
    assert pub.upload("R", _mp4(tmp_path), "EG", "T-1", _pub_cfg())["why"] == pipeline.KNOWN_BLOCKERS["wiki_key"]
    # key present but no project mapped
    creds.set_secret("wiki", "KC-KEY", backend=kr)
    nomap = pub.upload("R", _mp4(tmp_path), "ZZ", "T-1", _pub_cfg())
    assert nomap["status"] == "blocked" and "no Wiki project" in nomap["why"]


@needs_kit
def test_publisher_refuses_live_client_data(tmp_home, tmp_path, kr):
    creds.set_secret("wiki", "KC-KEY", backend=kr)
    http = FakeWiki("P-EG")
    pub = RealPublisher(http_fn=http, backend=kr)
    out = pub.upload("REQ-1", _mp4(tmp_path), "EG", "T-1", _pub_cfg(capture_is_live=True))
    assert out["status"] == "blocked" and "live" in out["why"].lower()
    assert http.calls == []                                   # refused before any API call


@needs_kit
def test_publisher_private_upload_no_share(tmp_home, tmp_path, kr):
    creds.set_secret("wiki", "KC-KEY", backend=kr)
    http = FakeWiki("P-EG")
    pub = RealPublisher(http_fn=http, backend=kr)
    out = pub.upload("REQ-1", _mp4(tmp_path), "EG", "T-1", _pub_cfg(share_public=False))
    assert out["status"] == "ok" and out["simulated"] is False
    assert out["public"] is False
    assert out["share_link"] == "https://wiki.example.com/project/P-EG/meeting/SID1"
    assert out["project"] == "client:EG" and out["folder"] == "project-root"
    assert http.hit("/api/sources/prepare-upload") and http.hit("/api/sources/confirm-upload")
    assert not http.hit("/api/share/source/")                 # share NOT asked -> never requested
    assert "KC-KEY" not in json.dumps(out)                    # the API key never leaks into the result


def _write_capture_result(rid, security_count=None):
    """Write a per-request after/results.json like record.py would, with an optional security findings count."""
    from twin_agent import paths
    after = paths.data_dir() / "proof" / rid / "after"
    after.mkdir(parents=True, exist_ok=True)
    body = {"summary": "1/1", "results": [{"result": "PASS"}]}
    if security_count is not None:
        body["security"] = {"count": security_count}
    (after / "results.json").write_text(json.dumps(body), encoding="utf-8")


@needs_kit
def test_publisher_public_share_when_asked_and_clean(tmp_home, tmp_path, kr):
    creds.set_secret("wiki", "KC-KEY", backend=kr)
    _write_capture_result("REQ-1", security_count=0)          # a clean capture result -> share allowed
    http = FakeWiki("P-EG", share_url="https://wiki.example.com/share/XYZ")
    pub = RealPublisher(http_fn=http, backend=kr)
    out = pub.upload("REQ-1", _mp4(tmp_path), "EG", "T-1", _pub_cfg(share_public=True))
    assert out["status"] == "ok" and out["public"] is True
    assert out["share_link"] == "https://wiki.example.com/share/XYZ"
    assert http.hit("/api/share/source/")


@needs_kit
def test_publisher_share_refused_on_security_findings(tmp_home, tmp_path, kr):
    creds.set_secret("wiki", "KC-KEY", backend=kr)
    _write_capture_result("REQ-1", security_count=2)          # capture found security issues -> no public share
    http = FakeWiki("P-EG")
    pub = RealPublisher(http_fn=http, backend=kr)
    out = pub.upload("REQ-1", _mp4(tmp_path), "EG", "T-1", _pub_cfg(share_public=True))
    assert out["status"] == "ok" and out["public"] is False
    assert not http.hit("/api/share/source/")


@needs_kit
def test_publisher_share_fails_closed_without_capture_result(tmp_home, tmp_path, kr):
    """Fail closed: share_public asked but NO capture result present -> no public link (the share gate must not
    fail open on a missing/inconclusive run)."""
    creds.set_secret("wiki", "KC-KEY", backend=kr)
    http = FakeWiki("P-EG", share_url="https://wiki.example.com/share/XYZ")
    pub = RealPublisher(http_fn=http, backend=kr)
    out = pub.upload("REQ-1", _mp4(tmp_path), "EG", "T-1", _pub_cfg(share_public=True))
    assert out["status"] == "ok" and out["public"] is False
    assert not http.hit("/api/share/source/")                 # no capture result -> never shared


@needs_kit
def test_publisher_uses_existing_folder_when_mapped(tmp_home, tmp_path, kr):
    creds.set_secret("wiki", "KC-KEY", backend=kr)
    http = FakeWiki("P-EG")
    pub = RealPublisher(http_fn=http, backend=kr)
    out = pub.upload("REQ-1", _mp4(tmp_path), "EG", "T-1", _pub_cfg(folders={"T-1": "FID9"}))
    assert out["folder"] == "folder:FID9"
    assert http.hit("/folders/FID9/sources/bulk")


@needs_kit
def test_publisher_upload_failure_blocks(tmp_home, tmp_path, kr):
    creds.set_secret("wiki", "KC-KEY", backend=kr)
    http = FakeWiki("P-EG", raise_on="/api/sources/prepare-upload")
    pub = RealPublisher(http_fn=http, backend=kr)
    out = pub.upload("REQ-1", _mp4(tmp_path), "EG", "T-1", _pub_cfg())
    assert out["status"] == "blocked" and "upload failed" in out["why"].lower()


# ======================================================================= run() integration (fail-closed)

def test_run_real_capture_fails_closed_on_no_ocr(tmp_home, tmp_path, monkeypatch):
    """End-to-end through run(): a REAL record.py capture (no OCR engine) yields frame_texts=None, so run()'s
    privacy step fails closed with 'no_ocr' and NOTHING is uploaded. Narrate/mux/publish are fakes so the block is
    provably the privacy gate, not an earlier blocker."""
    from twin_agent.proof.fakes import fake_adapters
    from twin_agent.testing.fakes import canned_packet
    _has_ffmpeg(monkeypatch)
    kit = _fake_kit(tmp_path)
    rep = RealReproducer(runner=_record_runner())
    cfg = _repro_cfg(kit)
    out = pipeline.run("REQ-1", cfg, request={"id": "REQ-1", "company": "EG"},
                       packet=canned_packet("REQ-1"), adapters=fake_adapters(reproduce=rep))
    assert out["status"] == "blocked" and out["privacy"]["findings"] == ["no_ocr"]
    assert out["share_link"] is None and out["posted"] is False
