"""Proof-video pipeline (15-SUPPORT-PIPELINE.md steps 4, 6, 7, 8). An ORCHESTRATOR over five adapters.

Flow: reproduce + CAPTURE on staging (verify-first) -> narrate in Principal's Arabic voice -> mux (ffmpeg) ->
privacy scan (block delivery on any secret) -> upload to Wiki, get a share link -> wire the link into the
ticket note DRAFT (observe-safe: prepared, never posted here).

Each adapter has:
  - a REAL implementation that activates ONLY when ``run_mode == live`` AND its key / dependency / staging target is
    present; otherwise that step is ``blocked`` with a plain reason. Those reasons ARE the "needs live keys" list.
  - a deterministic FAKE (``twin_agent/proof/fakes.py``) used only by tests via ``run(adapters=...)``. Fakes are
    NEVER the production fallback: an observe-mode run with no keys returns a blocked artifact, not a fabricated one.

Reuse, don't fork (see ..\\02n-SHIPSHAPE-ANALYSIS.md): the REAL adapters shell out to the ShipShape kit
(``proof-kit/scripts``): ``record.py`` (Playwright capture + trace), ``tts.py`` (edge-tts) with an
ElevenLabs path, ffmpeg (mux + ``video_trim.py``), ``privacy.py`` (text) + OCR for frames, and ``notify.py``'s
Wiki uploader. Secrets reach those subprocesses through ``creds.run`` (child env), never printed or returned.

Verify-first gate (step 7): a ``not_reproduced`` or ``user_error`` verdict routes to a GUIDANCE how-to (reply +
after-only video), never a dev ticket. Only a reproduced / data-only fix prepares a dev-or-config ticket note.

Safety: ``run_mode`` defaults to ``dry_run`` (observe). In observe every REAL step is blocked, so nothing is
captured, synthesised or uploaded, and the Wiki link is only ever PREPARED into a draft - a real post still goes
through the gated helpdesk path (desk.py) + Supervisor.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

from . import ocr
from .. import creds, paths
from ..redact import redact, scan

# ---------------------------------------------------------------- shared vocabulary

STEPS = ("plan", "preflight", "reproduce_capture", "narration", "mux", "privacy_scan", "publish", "wire_in")

# A proof pair (BEFORE repro + AFTER fix) is produced for these; an inquiry is AFTER-only (a how-to).
FIX_KINDS = ("quick_fix_reread", "narrated_video")
HOWTO_KINDS = ("howto_library_or_walkthrough",)
VERDICTS = ("reproduced", "not_reproduced", "user_error", "data_only")
GUIDANCE_VERDICTS = ("not_reproduced", "user_error")   # verify-first: these route to guidance, not a dev ticket

# What still needs a live key / dependency before a step can run for real (15 step 7.2 "Blockers today").
# Each stays until Sysadmin or Principal records it cleared in ``config.proof.cleared``.
KNOWN_BLOCKERS = {
    "voice": "no ElevenLabs Professional Voice Clone for Principal yet (D2 consent + recording pending)",
    "wiki_key": "Wiki per-bot API key (vault 'wiki') not issued",
    "qa_user": "no non-admin QA user on stagelatest",
    "shopify_keys": "stagelatest holds live Shopify write keys; connector must be removed or sandboxed",
    "shipshape_gaps": "ShipShape gaps G1, G2, G5, G10, G12, G13, G14 open",
    "flow": "capture target is the Globex Flow-shared Odoo tenant; set proof.capture_base to a non-Globex sandbox first",
}


def _kit_path(cfg: dict) -> Path:
    """Where the ShipShape kit lives (reuse, do not fork). From ``proof.kit_path`` or the sibling checkout."""
    p = ((cfg or {}).get("proof") or {}).get("kit_path")
    if p:
        return Path(p)
    return Path(r"C:\twin-dev\proof-kit")


def _cleared(cfg: dict, key: str) -> bool:
    return key in set(((cfg or {}).get("proof") or {}).get("cleared") or [])


def _flow_blocked(cfg: dict) -> str | None:
    """HARD Flow safety, NOT clearable via proof.cleared: capturing a repro on the Globex tenant (production OR
    stagelatest) shares the LIVE Flow warehouse + Shopify keys, so a BEFORE/AFTER capture can push a real
    fulfilment. Cleared ONLY by pointing proof.capture_base at a genuinely non-Globex sandbox host."""
    from urllib.parse import urlsplit
    base = str(((cfg or {}).get("proof") or {}).get("capture_base") or "").strip()
    if not base:
        return KNOWN_BLOCKERS["flow"]
    host = (urlsplit(base).hostname or base).lower()
    if "globex" in host and "odoo" in host:
        return "FLOW SAFETY: proof.capture_base is the Globex Flow-shared tenant; capture refused"
    from ..browser.attach import is_odoo_host
    if is_odoo_host(host):
        return ("ODOO API-ONLY: proof.capture_base is an Odoo host; Odoo is reached by API / JSON-RPC only, never a "
                "browser capture (S7). Point proof.capture_base at a non-Odoo sandbox host.")
    return None


# ---------------------------------------------------------------- plan + preflight (never run anything)

def plan(request: dict, packet: dict) -> dict:
    """Which proof this request needs (15 step 5) and its acceptance outline. Pure; opens nothing."""
    tier = (packet or {}).get("tier") or (request or {}).get("tier") or "UNKNOWN"
    action = ((packet or {}).get("proposed_action") or {}).get("type")
    if tier in ("T2b", "T3", "T4"):
        kind = "narrated_video"
    elif tier == "T1":
        kind = "howto_library_or_walkthrough"
    elif action == "run_this" or tier == "T2a":
        kind = "quick_fix_reread"
    else:
        kind = "unknown"
    return {
        "request_id": (request or {}).get("id") or (packet or {}).get("request_id"),
        "proof_kind": kind,
        "is_fix": kind in FIX_KINDS,
        "closes_when": {"quick_fix_reread": "re-read after 'I ran it' + requester says it worked",
                        "narrated_video": "video sent + requester confirms",
                        "howto_library_or_walkthrough": "requester confirms, or FIXED_UNCONFIRMED after 2 chasers",
                        "unknown": "Principal decides"}[kind],
        "acceptance": {"document": None, "expected": (packet or {}).get("understanding", "")[:300]},
        "status": "planned",
    }


def preflight(cfg: dict) -> list[str]:
    """Blockers that still stop a real proof-video run (15 step 7.2). ``proof.cleared`` removes a cleared one."""
    out = [why for key, why in KNOWN_BLOCKERS.items() if not _cleared(cfg, key)]
    if (cfg or {}).get("run_mode") != "live":
        out.append("run_mode is dry_run (observe): real capture / upload is off")
    if not ((cfg or {}).get("consent") or {}).get("d2_voice_ref"):
        out.append("no D2 voice consent reference")
    return out


# ---------------------------------------------------------------- narration script (Principal's Arabic, SHORT)

def narration_script(packet: dict, kind: str = "") -> dict:
    """A SHORT (30-90s) Arabic narration outline. The full Arabic script is written live from the fix summary; the
    specifics stay in the captions, and the English ``understanding`` is NEVER read aloud (per Supervisor) - so the
    spoken line is Arabic only. Redacted for safety."""
    lead = "دي خطوات الحل بالتفصيل" if kind in FIX_KINDS else "دي طريقة عمل الخطوة بالتفصيل"
    script = redact(lead)[:600]
    return {"step": "narration_script", "status": "ok", "request_id": (packet or {}).get("request_id"),
            "script": script, "lang": "ar", "words_target": [40, 120],
            "caption_footer": "AI voice of Principal's assistant",
            "note": "short Arabic; specifics live in captions; never reads documents or the English summary aloud"}


# ---------------------------------------------------------------- privacy scan (REAL + local; fails closed)

def privacy_scan(transcript: str = "", frame_texts: list[str] | None = None, *, vault_hashes=()) -> dict:
    """Run the narration, captions and every OCR'd frame/screenshot text through the real local secrets scanner. ANY
    finding (tokens, keys, phones, leaked vault values) blocks delivery. This is the real scan; the frame OCR itself
    (``ocr.ocr_images``) needs a local engine, and a real capture that yields no frame text fails closed in ``run``
    (never passes by default)."""
    texts = [transcript, *(frame_texts or [])]
    kinds = sorted({f.kind for t in texts for f in scan(t or "", vault_hashes)})
    clean = not kinds
    return {"step": "privacy_scan", "status": "ok" if clean else "blocked", "clean": clean, "findings": kinds,
            "why": "" if clean else f"secret/personal-data in the video: {', '.join(kinds)}"}


# ---------------------------------------------------------------- REAL adapters (live-only; blocked otherwise)

class KitResult:
    """The outcome of one ShipShape-kit subprocess: exit code + captured text. The child's environment (which held
    any vault value) is NEVER part of this - only what the kit wrote to its own stdout/stderr and to disk."""

    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = int(returncode)
        self.stdout = stdout or ""
        self.stderr = stderr or ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _run_kit(cmd: list[str], *, secrets: dict | None = None, extra_env: dict | None = None, cwd: str | None = None,
             purpose: str = "proof kit", request_id: str | None = None, timeout: int = 900,
             backend=None) -> KitResult:
    """Run a kit script as a child process, the established ``creds.child_env`` pattern (see browser/broker.py).

    ``secrets`` is a ``{ENV_VAR: vault-name}`` map; those values reach the child ONLY through its environment and are
    never printed, logged or returned. Non-secret knobs (voice id, provider) go in ``extra_env``. stdin is closed and
    a timeout is enforced so a kit prompt can never hang the worker. Returns the exit code and captured text, not the
    env. ``creds.run`` itself returns only an int and cannot pass extra env or capture output, so we build on
    ``child_env`` (its own foundation) instead."""
    env = creds.child_env(secrets or {}, purpose=purpose, request_id=request_id, backend=backend)
    for k, v in (extra_env or {}).items():
        env[str(k)] = str(v)
    try:
        p = subprocess.run(list(cmd), env=env, cwd=cwd, shell=False, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return KitResult(124, "", f"kit timed out after {timeout}s")
    except OSError as e:
        return KitResult(127, "", f"could not start kit: {type(e).__name__}")
    return KitResult(p.returncode, p.stdout or "", p.stderr or "")


class _Real:
    """Base: a REAL adapter is available only in live mode with its key/dep present. ``available`` names the gap.

    Each adapter takes an injectable ``runner`` (the ShipShape-kit subprocess, default ``_run_kit``) and a keyring
    ``backend`` so tests exercise the exact command/args/env-keys and the parse path with NO real network, browser,
    ffmpeg, ElevenLabs or Wiki call - and never touch the real vault."""

    step = "step"

    def __init__(self, *, runner=None, backend=None):
        self._runner = runner or _run_kit
        self._backend = backend

    def _blocked(self, why: str, **extra) -> dict:
        return {"step": self.step, "status": "blocked", "why": why, "simulated": False, **extra}

    def _ok(self, **fields) -> dict:
        """A REAL ok result. ``simulated`` is ALWAYS False: a True here would silently switch off the run()
        fail-closed OCR/captions checks (which only fire on a non-simulated step)."""
        return {"step": self.step, "status": "ok", "simulated": False, **fields}


def _py() -> str:
    """The interpreter to run kit scripts with (the current venv's Python)."""
    return sys.executable or "python"


def _host_of(base_url: str) -> str:
    return (urlsplit(base_url).hostname or "").strip()


def _proof_dir(request_id: str) -> Path:
    """Per-request working dir for proof artifacts (under data/, the worker-denied tree)."""
    return paths.data_dir() / "proof" / str(request_id or "REQ-0000")


class RealReproducer(_Real):
    """ShipShape capture on a sandbox host via ``scripts/record.py`` (Playwright). BEFORE = repro, AFTER = fix.

    Flow safety and ``proof.capture_base`` are enforced at the COMMAND level: record.py is always given
    ``--base-url <capture_base>`` and ``--allow-host <host>`` ("the target you name wins"), so a stray plan base_url
    can never redirect the capture onto the Globex Flow tenant. We never pass ``--narrate`` - narration is a separate
    single-voice step. The captured screenshots are OCR'd through the LOCAL engine seam (``ocr.ocr_images``) so
    ``frame_texts``/``screenshot_texts`` carry real text for the privacy scan; if no local engine is installed (or an
    image fails) ``frame_texts`` stays None and the privacy step fails closed on a real capture (an honest remaining
    blocker, not a silent pass). NOTE: only the AFTER screenshots are OCR'd, not sampled video frames - ``frame_texts``
    here means screenshot OCR."""

    step = "reproduce_capture"

    def __init__(self, *, runner=None, backend=None, ocr_engine=None):
        super().__init__(runner=runner, backend=backend)
        # Injectable LOCAL OCR engine (``image_path -> str``): tests pass a fake so no real OCR binary/network runs.
        # None => ``_ocr_screenshots`` resolves a real local engine from ``proof.ocr_engine`` (or fails closed).
        self._ocr_engine = ocr_engine

    def _ocr_screenshots(self, paths: list[str], cfg: dict) -> list[str] | None:
        """OCR the captured screenshots to one text each, or None to FAIL CLOSED (no engine, or any image failed).
        Never fabricates text; a None here makes ``run()``'s privacy step block the video."""
        if not paths:
            return None                 # a real capture with no screenshots cannot be scanned: fail closed
        engine = self._ocr_engine
        try:
            if engine is None:
                prefer = str(((cfg or {}).get("proof") or {}).get("ocr_engine") or "auto")
                engine = ocr.resolve_engine(prefer)
                if engine is None:
                    return None          # no local OCR engine installed: fail closed (an honest remaining blocker)
            return ocr.ocr_images(paths, engine=engine)
        except Exception:                # noqa: BLE001 - NoOCREngine or a per-image failure => fail closed, never partial
            return None

    def available(self, cfg: dict) -> str | None:
        if (cfg or {}).get("run_mode") != "live":
            return "run_mode is not live (observe): staging capture is off"
        flow = _flow_blocked(cfg)
        if flow:
            return flow
        if not (_kit_path(cfg) / "scripts" / "record.py").exists():
            return "ShipShape kit not found (set proof.kit_path)"
        if not _cleared(cfg, "qa_user"):
            return KNOWN_BLOCKERS["qa_user"]
        if shutil.which("ffmpeg") is None:
            return "ffmpeg not on PATH"
        return None

    @staticmethod
    def _counts(summary: dict) -> tuple[int, int, int]:
        rows = (summary or {}).get("results") or []
        passed = sum(1 for r in rows if (r or {}).get("result") == "PASS")
        failed = sum(1 for r in rows if (r or {}).get("result") == "FAIL")
        blocked = sum(1 for r in rows if (r or {}).get("result") == "BLOCKED")
        return passed, failed, blocked

    @staticmethod
    def _find_video(out_dir: Path) -> str | None:
        vids = [p for p in sorted(out_dir.glob("*.mp4")) if not p.name.endswith(".original.mp4")]
        return str(vids[0]) if vids else None

    def _record(self, plan: str, out_dir: Path, base_url: str, cfg: dict, request_id: str) -> KitResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        cmd = [_py(), str(_kit_path(cfg) / "scripts" / "record.py"), str(plan), "--out", str(out_dir),
               "--base-url", base_url, "--allow-host", _host_of(base_url), "--total-timeout", "600"]
        qa_config = ((cfg or {}).get("proof") or {}).get("qa_config")   # record.py's --config default is cwd/qa.config.yaml
        if qa_config:
            cmd += ["--config", str(qa_config)]
        return self._runner(cmd, secrets={}, cwd=str(_kit_path(cfg)), purpose="proof capture (record.py)",
                            request_id=request_id, timeout=900, backend=self._backend)

    def _read_results(self, out_dir: Path) -> dict | None:
        rp = out_dir / "results.json"
        if not rp.exists():
            return None
        try:
            return json.loads(rp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def capture(self, request: dict, packet: dict, kind: str, cfg: dict) -> dict:
        why = self.available(cfg)
        if why:
            return self._blocked(why)
        proof_cfg = (cfg or {}).get("proof") or {}
        base_url = str(proof_cfg.get("capture_base") or "").strip()
        plans = proof_cfg.get("plans") or {}
        rid = request.get("id") or (packet or {}).get("request_id") or "REQ-0000"
        base = _proof_dir(rid)
        is_fix = kind in FIX_KINDS

        # Validate BOTH plans up front so a missing BEFORE plan blocks before any live capture has run.
        after_plan = plans.get("after")
        before_plan = plans.get("before")
        if not after_plan:
            return self._blocked("no AFTER QA plan for the capture (set proof.plans.after to a record.py plan)")
        if is_fix and not before_plan:
            return self._blocked("no BEFORE QA plan for the fix (set proof.plans.before to a record.py plan)")
        after_dir = base / "after"
        res_after = self._record(after_plan, after_dir, base_url, cfg, rid)
        if not res_after.ok:
            return self._blocked(f"record.py (AFTER) failed: {redact((res_after.stderr or res_after.stdout)[:200])}")
        sum_after = self._read_results(after_dir)
        if sum_after is None:
            return self._blocked("record.py (AFTER) wrote no results.json: capture inconclusive")
        a_pass, a_fail, a_blocked = self._counts(sum_after)
        after_ok = a_pass > 0 and a_fail == 0 and a_blocked == 0
        after_path = self._find_video(after_dir)

        before_path = None
        verdict = "capture_inconclusive"       # outside VERDICTS -> run() routes to needs_human (never a dev ticket)
        if is_fix:
            before_dir = base / "before"
            res_before = self._record(before_plan, before_dir, base_url, cfg, rid)
            if not res_before.ok:
                return self._blocked(
                    f"record.py (BEFORE) failed: {redact((res_before.stderr or res_before.stdout)[:200])}")
            sum_before = self._read_results(before_dir)
            if sum_before is None:
                return self._blocked("record.py (BEFORE) wrote no results.json: capture inconclusive")
            _bp, b_fail, _bb = self._counts(sum_before)
            before_path = self._find_video(before_dir)
            if after_ok and b_fail > 0:
                verdict = "reproduced"          # bug shown BEFORE, gone AFTER = a real proof pair
            elif after_ok and b_fail == 0:
                verdict = "not_reproduced"       # could not reproduce the bug -> verify-first guidance
        else:
            if after_ok:
                verdict = "reproduced"           # inquiry / how-to: a clean AFTER walkthrough

        screenshots = [str(p) for p in sorted(after_dir.glob("*.png"))]
        # OCR the captured screenshots through the LOCAL engine. ``texts`` is None (no engine / image failed) ->
        # frame_texts stays None and run()'s privacy step fails closed; a clean list clears it (one text per shot).
        texts = self._ocr_screenshots(screenshots, cfg)
        return self._ok(verdict=verdict, before_path=before_path, after_path=after_path,
                        screenshots=screenshots, frame_texts=texts, screenshot_texts=texts,
                        evidence={"case": "record.py", "note": "sandbox capture (real)",
                                  "summary": str(sum_after.get("summary") or "")})


class RealNarrator(_Real):
    """Short Arabic narration AUDIO via the ShipShape kit TTS (``scripts/tts.py``), with the ElevenLabs key carried
    to the child through the vault/env.

    STAYS BLOCKED until Principal's ElevenLabs Professional Voice Clone exists: ``available`` returns the ``voice``
    blocker unless the clone is RECORDED cleared AND the key, D2 consent and a configured voice id are all present -
    code-complete is not the same as clearing the blocker, and nothing here fabricates Principal's voice. If the kit TTS
    silently falls back (no key / no voice), we return blocked rather than mislabel the provider."""

    step = "narration"

    def available(self, cfg: dict) -> str | None:
        if (cfg or {}).get("run_mode") != "live":
            return "run_mode is not live (observe): synthesis is off"
        if not _cleared(cfg, "voice"):
            return KNOWN_BLOCKERS["voice"]
        if not creds.has_secret("elevenlabs", backend=self._backend):
            return KNOWN_BLOCKERS["voice"] + " (no ElevenLabs key in the vault)"
        if not ((cfg or {}).get("consent") or {}).get("d2_voice_ref"):
            return "no D2 voice consent reference"
        if not ((cfg or {}).get("proof") or {}).get("voice_id"):
            return "no ElevenLabs voice id configured (set proof.voice_id)"
        return None

    def synth(self, script: dict, cfg: dict) -> dict:
        why = self.available(cfg)
        if why:
            return self._blocked(why)
        rid = script.get("request_id") or "REQ-0000"
        text = (script.get("script") or "").strip() or "."
        voice = str(((cfg or {}).get("proof") or {}).get("tts_voice") or "ar-EG-ShakirNeural")
        # NOTE: tts.py is edge-tts only - it does NOT use the ElevenLabs key, so we do NOT hand the vault value to
        # it (and ``child_env`` would pass the whole ``elevenlabs`` JSON blob, not a bare key, anyway). The
        # ElevenLabs clone path is a REMAINING blocker: the kit exposes no single-clip ElevenLabs CLI, and the
        # JSON ``elevenlabs`` secret would need ``creds.get_fields`` in-process. Clearing the ``voice`` blocker
        # today therefore narrates with the edge voice ``tts_voice``, NOT Principal's clone - do not clear it until a
        # real ElevenLabs path exists. The ``available`` gate above keeps this blocked in every real config.
        cmd = [_py(), str(_kit_path(cfg) / "scripts" / "tts.py"), text, "--voice", voice]
        res = self._runner(cmd, secrets={}, cwd=str(_kit_path(cfg)), purpose="proof narration (tts.py)",
                           request_id=rid, timeout=180, backend=self._backend)
        if not res.ok:
            return self._blocked(f"narration synthesis failed: {redact((res.stderr or res.stdout)[:200])}")
        audio_path = (res.stdout.strip().split() or [""])[0]
        if not audio_path:
            return self._blocked("narration produced no audio path")
        return self._ok(audio_path=audio_path, provider="kit-tts", text=text,
                        lang=script.get("lang") or "ar",
                        narration_note="AI narration via kit edge-TTS (ElevenLabs clone path is a remaining blocker)")


class RealMuxer(_Real):
    """ffmpeg: join the capture video + narration audio into one short MP4, with a real sidecar ``.srt``
    (ShipShape ``video_trim.py`` trims the capture first when ``proof.trim`` is set; record.py already trims, so it
    is off by default). ffmpeg runs with ``cwd`` = the output dir and RELATIVE names, so a repo path with spaces or
    a drive-letter colon never breaks an ffmpeg filter/arg. ``mp4_path`` is always the final file, never
    ``*.original.mp4``."""

    step = "mux"

    def available(self, cfg: dict) -> str | None:
        if (cfg or {}).get("run_mode") != "live":
            return "run_mode is not live (observe): mux is off"
        if shutil.which("ffmpeg") is None:
            return "ffmpeg not on PATH"
        return None

    def combine(self, request_id: str, capture: dict, narration: dict, cfg: dict) -> dict:
        why = self.available(cfg)
        if why:
            return self._blocked(why)
        proof_cfg = (cfg or {}).get("proof") or {}
        video = (capture or {}).get("after_path")
        audio = (narration or {}).get("audio_path")
        if not video:
            return self._blocked("no capture video to mux (after_path missing)")
        if not audio:
            return self._blocked("no narration audio to mux (audio_path missing)")
        out_dir = _proof_dir(request_id)
        out_dir.mkdir(parents=True, exist_ok=True)

        # Optional trim of the CAPTURE (never the muxed output) via the kit, before narration is added.
        if proof_cfg.get("trim"):
            trim = self._runner([_py(), str(_kit_path(cfg) / "scripts" / "video_trim.py"), str(video)],
                                secrets={}, cwd=str(_kit_path(cfg)), purpose="proof trim (video_trim.py)",
                                request_id=str(request_id), timeout=600, backend=self._backend)
            if not trim.ok:
                return self._blocked(f"video_trim.py failed: {redact((trim.stderr or trim.stdout)[:200])}")

        # Real, non-empty captions (sidecar .srt) - the SPOKEN narration text, never a dev note (this ships to the
        # client). Falls back to the disclosure footer if the narrator gave no text. run() fails closed on a
        # missing/empty captions file.
        srt_name = "proof.srt"
        spoken = str((narration or {}).get("text") or "").strip()
        caption = f"{spoken}\n" if spoken else ""
        caption += "AI voice of Principal's assistant"
        (out_dir / srt_name).write_text(f"1\n00:00:00,000 --> 00:00:05,000\n{caption}\n\n", encoding="utf-8")

        # -af apad + -shortest: pad the (short) narration audio with silence so the VIDEO, not the narration, sets
        # the clip length (the kit's narrator.mix_cmd pads for the same reason); otherwise the proof is truncated.
        out_name = "proof.mp4"
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", os.path.abspath(video), "-i", os.path.abspath(audio),
               "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-af", "apad", "-shortest", out_name]
        res = self._runner(cmd, secrets={}, cwd=str(out_dir), purpose="proof mux (ffmpeg)",
                           request_id=str(request_id), timeout=600, backend=self._backend)
        if not res.ok:
            return self._blocked(f"ffmpeg mux failed: {redact((res.stderr or res.stdout)[:200])}")
        return self._ok(mp4_path=str(out_dir / out_name), captions_path=str(out_dir / srt_name))


def _capture_security_count(run_dir) -> int | None:
    """Security findings the capture recorded, read from after/results.json. Returns None when the capture result
    is missing or unreadable, so the public-share decision FAILS CLOSED (no public link on an inconclusive run)."""
    try:
        data = json.loads((run_dir / "after" / "results.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sec = data.get("security") if isinstance(data, dict) else None
    if isinstance(sec, dict):
        return int(sec.get("count") or 0)
    if isinstance(sec, list):
        return len(sec)
    return 0                                             # a present capture result with no security section = clean


class RealPublisher(_Real):
    """Wiki upload via the kit's ALREADY-written client (``scripts/notify.py``): per-client PROJECT (resolved from
    ``proof.projects``), an existing per-ticket sub-folder when configured, and a public share link only when asked.

    Reuse, don't reinvent: ``notify.send_wiki`` does prepare -> PUT -> confirm -> move -> folder (+ dedupe) and
    derives the link; ``notify.wiki_guard`` refuses ``live: true`` client data; ``notify.wiki_share_allowed``
    allows a public share only when the channel asks AND there are no security findings. The Wiki API key is read
    in-process with ``creds._get`` AFTER the guards pass and used only as a header - never printed or returned. The
    kit has no folder-CREATE endpoint, so an unmapped ticket folder uploads to the project root (noted, never a
    crash). ``http_fn`` is injectable: the real one is ``notify.http``; tests pass a fake, so nothing hits the net."""

    step = "publish"

    def __init__(self, *, http_fn=None, runner=None, backend=None):
        super().__init__(runner=runner, backend=backend)
        self._http_fn = http_fn

    def available(self, cfg: dict) -> str | None:
        if (cfg or {}).get("run_mode") != "live":
            return "run_mode is not live (observe): upload is off"
        if not creds.has_secret("wiki", backend=self._backend):
            return KNOWN_BLOCKERS["wiki_key"]
        return None

    def _notify(self, cfg: dict):
        """Import the kit's notify.py (reuse its Wiki client). Returns the module or None if unavailable."""
        scripts = str(_kit_path(cfg) / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        try:
            import notify  # type: ignore
            return notify
        except Exception:   # noqa: BLE001 - a missing kit must block, never crash the run
            return None

    def upload(self, request_id: str, mp4_path: str, client: str, ticket_ref: str, cfg: dict) -> dict:
        why = self.available(cfg)
        if why:
            return self._blocked(why)
        proof_cfg = (cfg or {}).get("proof") or {}
        projects = proof_cfg.get("projects") or {}
        pid = projects.get(client)
        if not pid:
            return self._blocked(f"no Wiki project mapped for client {client!r} (set proof.projects)")
        notify = self._notify(cfg)
        if notify is None:
            return self._blocked("ShipShape kit notify.py not importable (set proof.kit_path)")

        folders = proof_cfg.get("folders") or {}
        folder_id = folders.get(ticket_ref)
        share_asked = bool(proof_cfg.get("share_public"))
        ch = {"project_id": pid, "share": share_asked}
        if proof_cfg.get("wiki_base"):
            ch["api_base"] = proof_cfg["wiki_base"]
        if folder_id:
            ch["folder_id"] = folder_id

        run_dir = _proof_dir(request_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        # Public share ONLY when asked AND the capture positively shows ZERO security findings. A missing or
        # inconclusive capture result -> no public link (FAIL CLOSED). The kit's wiki_share_allowed reads a
        # ledger.json this pipeline never writes, so it fails OPEN on a missing file; we decide from the real
        # capture result instead. The live + privacy guards below still gate the upload itself.
        sec = _capture_security_count(run_dir)
        share = bool(share_asked and sec == 0)
        ch["share"] = share
        http_fn = self._http_fn or notify.http
        env = {"SMEKIT_STATE_DIR": str(paths.data_dir() / "state" / "wiki")}
        try:
            # Live refusal (notify's own guard): never upload live client production data. run_mode:live != live:true.
            refusal = notify.wiki_guard(str(run_dir), {"live": bool(proof_cfg.get("capture_is_live"))})
            if refusal:
                return self._blocked(refusal)
            key = creds._get("wiki", backend=self._backend, caller="proof_publish", purpose="wiki upload")
            got = notify.send_wiki(str(run_dir), "wiki", ch, [("video", mp4_path)], share, key, http_fn, env)
        except Exception as e:   # noqa: BLE001 - upload/guard/key failure blocks delivery; the reason is redacted
            return self._blocked(f"Wiki upload failed: {redact(str(e))[:200]}")
        return self._ok(share_link=got.get("link"), project=f"client:{client}",
                        folder=(f"folder:{folder_id}" if folder_id else "project-root"), public=bool(share))


def resolve_adapters(cfg: dict) -> dict:
    """Production adapters. These are REAL; each blocks itself (with a reason) when its key/dep is missing. Fakes
    are never returned here - tests pass them explicitly to ``run(adapters=...)``."""
    return {"reproduce": RealReproducer(), "narrate": RealNarrator(), "mux": RealMuxer(), "publish": RealPublisher()}


# ---------------------------------------------------------------- routing + wire-in

def _route(kind: str, verdict: str | None) -> str:
    """Verify-first, ALLOW-LISTED: only an explicit verdict routes anywhere. A not-reproduced / user-error
    verdict is a guidance how-to; a reproduced / data-only verdict is a proof pair (or a how-to for an inquiry).
    An unknown or missing verdict must NEVER silently default to a dev-ticket proof_pair -> it needs a human."""
    if verdict in GUIDANCE_VERDICTS:
        return "guidance"
    if verdict not in VERDICTS:                       # unknown / None: fail-safe, do not proceed
        return "needs_human"
    if kind in HOWTO_KINDS:
        return "howto"
    return "proof_pair"


def _has_dev_part(packet: dict) -> bool:
    parts = ((packet or {}).get("ticket_draft") or {}).get("parts") or []
    return any((p or {}).get("type") == "dev" for p in parts)


# ---------------------------------------------------------------- proof provenance (only the pipeline can sign)

def _proof_key_path() -> Path:
    """The per-install HMAC key for proof provenance. It lives under data/state, which is on the worker's DENY
    list (spawner.private_dirs), so a worker cannot read it to forge a signature."""
    return paths.data_dir() / "state" / "proof.key"


def _proof_key(*, create: bool) -> bytes | None:
    """Read the provenance key; ``create`` makes one (only the signing side does). None if absent/unreadable."""
    p = _proof_key_path()
    try:
        if p.exists():
            return p.read_bytes()
        if not create:
            return None
        p.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(key)
        os.replace(tmp, p)
        return key
    except OSError:
        return None


def _proof_material(proof: dict, rid: str) -> bytes:
    """The exact fields the gate trusts, bound to the request id so a signed proof cannot be moved to a sibling."""
    return "|".join([str(rid or ""), str((proof or {}).get("status") or ""),
                     str((proof or {}).get("route") or ""),
                     str((proof or {}).get("verdict") or "")]).encode("utf-8")


def _sign_proof(proof: dict, rid: str) -> str:
    key = _proof_key(create=True)
    if key is None:
        return ""
    return hmac.new(key, _proof_material(proof, rid), hashlib.sha256).hexdigest()


def attach(packet: dict, artifact: dict) -> dict:
    """Attach the proof artifact to the packet (15 step 6/8). On a GUIDANCE or NEEDS_HUMAN route (verify-first),
    drop any ``dev`` ticket part so no development ticket is drafted. Returns a NEW packet; never mutates the input.

    The stored proof is a COPY without its own ``packet`` key, so packet->proof->packet is not a cycle (a cycle
    breaks json.dumps if the packet is ever persisted, and makes ``status`` readable only through aliasing).

    The pipeline SIGNS the proof here (HMAC over request_id|status|route|verdict with the worker-denied key), so
    ``proof_cleared`` can tell a real pipeline artifact from a worker-forged ``proof`` dict."""
    out = dict(packet or {})
    art_copy = {k: v for k, v in (artifact or {}).items() if k != "packet"}
    rid = str(art_copy.get("request_id") or out.get("request_id") or "")
    art_copy.setdefault("request_id", rid or None)
    art_copy["sig"] = _sign_proof(art_copy, rid)
    out["proof"] = art_copy
    if art_copy.get("share_link"):
        out["proof_link"] = art_copy["share_link"]
    if art_copy.get("route") in ("guidance", "needs_human"):
        td = out.get("ticket_draft")
        if isinstance(td, dict):
            parts = [p for p in (td.get("parts") or []) if (p or {}).get("type") != "dev"]
            out["ticket_draft"] = dict(td, parts=parts)
    return out


def proof_cleared(packet: dict) -> bool:
    """FAIL-SAFE: a proof artifact actually ran, finished ``prepared``, took the ``proof_pair`` route on a
    reproduced / data-only verdict. This is the precondition the helpdesk desk's verify-first gate checks before it
    offers to CREATE a development ticket - independent of whether the packet carries an explicit dev PART, because
    a tier-derived development ticket (type Bug / x_track development) can have ``ticket_draft`` parts=[].
    Proof absent / blocked / needs-human, or a guidance verdict, all return False, so the gate can never fail open.

    PROVENANCE (review fix): the proof must carry a valid pipeline HMAC signature bound to THIS request. A worker
    cannot sign (the key is on its DENY list) and cannot copy a sibling's signed proof (the id is in the material),
    so a forged ``proof`` dict - even one that was not stripped by normalize_packet - can never clear the gate."""
    proof = (packet or {}).get("proof") or {}
    if not proof:                                       # no proof ran (the runtime reality): do not auto-offer
        return False
    if proof.get("status") != "prepared":               # blocked / needs_human / anything else: fail-safe
        return False
    if proof.get("route") != "proof_pair":              # only an allow-listed proof-pair route may offer
        return False
    if proof.get("verdict") not in ("reproduced", "data_only"):
        return False
    rid = str(packet.get("request_id") or proof.get("request_id") or "")
    if proof.get("request_id") and packet.get("request_id") \
            and str(proof.get("request_id")) != str(packet.get("request_id")):
        return False                                    # a signed proof from a different request was pasted in
    key = _proof_key(create=False)
    if key is None:                                     # no key = nothing was ever signed here = never cleared
        return False
    expected = hmac.new(key, _proof_material(proof, rid), hashlib.sha256).hexdigest()
    sig = str(proof.get("sig") or "")
    return bool(sig) and hmac.compare_digest(sig, expected)


def should_offer_dev_ticket(packet: dict) -> bool:
    """The verify-first gate for an EXPLICIT dev ticket PART: proof cleared AND the packet carries a dev part."""
    return proof_cleared(packet) and _has_dev_part(packet)


def _guidance_reply(packet: dict, verdict: str, share_link: str | None) -> str:
    """A short Arabic how-to reply for a not-a-bug / user-error outcome. No dev ticket."""
    why = {"not_reproduced": "جربنا نعيد المشكلة على بيئة الاختبار وما ظهرتش",
           "user_error": "المشكلة في خطوة الاستخدام مش في النظام"}.get(verdict, "")
    tail = f" فيديو الشرح: {share_link}" if share_link else ""
    return (f"{why}. بعتنالك شرح بالخطوات.{tail}").strip()


def prepare_note_draft(request: dict, packet: dict, cfg: dict, artifact: dict, *, verified: bool = False,
                       conv=None, odoo=None) -> dict:
    """OBSERVE-safe wire-in: build the ticket's internal note DRAFT with the Wiki link prepared in, via the real
    helpdesk drafting path (drafting.build(links=...)). It never posts: a real post goes through desk.py + Supervisor.

    ``verified`` is the packet's RECOMPUTED verified flag (passed by the caller), NOT the capture's self-reported
    verdict: the note must not label a draft 'verified' on the capture's own say-so."""
    from ..helpdesk import drafting, style
    from ..helpdesk.conventions import Conventions
    link = artifact.get("share_link")
    draft = drafting.build(request, packet, conv or Conventions(), cfg, odoo=odoo, verified=verified,
                           links=[link] if link else None, ref=request.get("id"))
    note = "\n".join(style.note_lines(draft))
    return {"note": note, "links": list(draft.links), "posted": False,
            "has_link": bool(link and link in note)}


# ---------------------------------------------------------------- orchestrator

def run(request_id: str, cfg: dict, *, request: dict | None = None, packet: dict | None = None,
        adapters: dict | None = None, odoo=None, vault_hashes=()) -> dict:
    """Walk the pipeline end to end and return a proof artifact. Backward compatible: ``run(request_id, cfg)`` works.

    With production (REAL) adapters in observe mode every step blocks and the artifact is ``blocked`` with the
    reasons that need live keys. Tests pass fakes via ``adapters`` to exercise the green path. Nothing is ever
    sent, posted or uploaded from here; the Wiki link is only PREPARED into the note draft."""
    request = dict(request or {"id": request_id})
    request.setdefault("id", request_id)
    packet = dict(packet or {})
    packet.setdefault("request_id", request_id)
    pl = plan(request, packet)
    kind = pl["proof_kind"]
    A = adapters or resolve_adapters(cfg)
    art: dict = {"request_id": request_id, "proof_kind": kind, "route": None, "verdict": None,
                 "before_path": None, "after_path": None, "screenshots": [], "evidence": {},
                 "share_link": None, "note_draft": None, "guidance_reply": None, "dev_ticket": None,
                 "ticket_draft": None, "posted": False, "simulated": False, "status": "blocked",
                 "blockers": preflight(cfg), "steps": [], "why": ""}

    def record(step: dict):
        art["steps"].append(step)
        art["simulated"] = art["simulated"] or bool(step.get("simulated"))
        return step

    # 1) Reproduce + CAPTURE (verify-first).
    cap = record(A["reproduce"].capture(request, packet, kind, cfg))
    if cap.get("status") != "ok":
        art["why"] = cap.get("why") or "capture blocked"
        return art
    verdict = cap.get("verdict")
    route = _route(kind, verdict)
    art.update(verdict=verdict, route=route, before_path=cap.get("before_path"),
               after_path=cap.get("after_path"), screenshots=cap.get("screenshots") or [],
               evidence=cap.get("evidence") or {})
    if route == "needs_human":           # verify-first: an unrecognised verdict never auto-drafts a dev ticket
        art["dev_ticket"] = False
        art["ticket_draft"] = None
        art["status"] = "needs_human"
        art["why"] = f"capture verdict {verdict!r} not recognised; needs a human decision (no auto dev ticket)"
        record({"step": "route", "status": "needs_human", "verdict": verdict})
        art["packet"] = attach(packet, art)
        return art

    # 2) Narration (short Arabic, from the fix summary).
    script = narration_script(packet, kind)
    script["request_id"] = request_id
    narr = record(A["narrate"].synth(script, cfg))
    art["narration_script"] = script.get("script")
    if narr.get("status") != "ok":
        art["why"] = narr.get("why") or "narration blocked"
        return art

    # 3) Mux (video + narration + captions -> one short MP4).
    mux = record(A["mux"].combine(request_id, cap, narr, cfg))
    if mux.get("status") != "ok":
        art["why"] = mux.get("why") or "mux blocked"
        return art

    # 4) Privacy scan (REAL, local; FAILS CLOSED). Scans narration, caption CONTENTS, evidence and every OCR'd
    #    frame AND screenshot text. A REAL capture that returned no usable OCR text (None, [], or all-blank)
    #    cannot be cleared -> block (never pass by default); a screenshot with no OCR text blocks too.
    def _no_ocr(texts) -> bool:
        return texts is None or not [t for t in (texts or []) if str(t).strip()]

    def _coverage_gap(texts, items) -> bool:
        """Presence is not coverage: block unless OCR text covers EVERY captured item (one non-blank text each).
        A count mismatch or any blank item is a gap -> fail closed. No items = nothing to cover (not a gap here)."""
        items = items or []
        if not items:
            return False
        if texts is None or len(texts) != len(items):
            return True
        return any(not str(t).strip() for t in texts)

    def _block_privacy(finding: str, why: str) -> dict:
        priv = record({"step": "privacy_scan", "status": "blocked", "clean": False, "findings": [finding],
                       "why": why})
        art["privacy"] = {"clean": False, "findings": [finding]}
        art["why"] = priv["why"]
        return art

    real_cap = not cap.get("simulated")
    frame_texts = cap.get("frame_texts")
    shot_texts = cap.get("screenshot_texts")
    # The capture dict is recorded into art["steps"] (and copied onto the packet), so its raw OCR text would travel
    # out with the artifact - including any secret the scan is about to block on. Redact the STORED copies now; the
    # locals above still hold the raw lists, so the gates and the real scan below are unchanged (redacting before
    # the scan would make it fail OPEN: scan("[REDACTED:...]") finds nothing).
    for _k in ("frame_texts", "screenshot_texts"):
        if cap.get(_k) is not None:
            cap[_k] = [redact(str(t)) for t in cap[_k]]
    if real_cap and _no_ocr(frame_texts):
        return _block_privacy("no_ocr", "a real capture produced no OCR frame text: cannot clear the video "
                              "(fail closed)")
    if real_cap and _coverage_gap(frame_texts, cap.get("frames")):
        return _block_privacy("frame_ocr_gap", "OCR text does not cover every captured frame (a frame was left "
                              "unscanned): cannot clear the video (fail closed)")
    if real_cap and (cap.get("screenshots") or []) and _no_ocr(shot_texts):
        return _block_privacy("no_ocr_screenshot", "a real capture has screenshots but no OCR text for them: "
                              "cannot clear (fail closed)")
    if real_cap and _coverage_gap(shot_texts, cap.get("screenshots")):
        return _block_privacy("screenshot_ocr_gap", "OCR text does not cover every screenshot (one was left "
                              "unscanned): cannot clear (fail closed)")
    caption_text = ""
    capp = mux.get("captions_path")
    real_mux = not mux.get("simulated")
    unreadable = False
    if capp:
        if os.path.exists(capp):
            try:
                caption_text = Path(capp).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                unreadable = True
        else:
            unreadable = True
    else:
        unreadable = True            # a real narrated video always carries burned captions; none = cannot scan them
    # Missing / unreadable OR existing-but-empty/whitespace captions on a REAL mux: fail closed. An empty captions
    # file used to be read as "" and silently clear the scan (fix 5).
    blank_caps = bool(capp) and not unreadable and not caption_text.strip()
    if real_mux and (unreadable or blank_caps):
        finding = "captions_unreadable" if unreadable else "captions_empty"
        return _block_privacy(finding, "captions are missing, unreadable or empty: cannot scan them (fail closed)")
    texts = [script.get("script") or "", str(cap.get("evidence") or ""), narr.get("narration_note") or "", caption_text]
    ocr_texts = list(frame_texts or []) + list(shot_texts or [])      # scan screenshots too, not just frames
    vh = vault_hashes
    if adapters is None and not vh:                 # production: catch a leaked real vault value too (lazy; no key read in tests)
        try:
            vh = creds.vault_hashes()
        except Exception:                           # noqa: BLE001 - vault unavailable must not crash the scan
            vh = ()
    priv = record(privacy_scan("\n".join(texts), ocr_texts, vault_hashes=vh))
    art["privacy"] = {"clean": priv["clean"], "findings": priv["findings"]}
    if not priv["clean"]:
        art["why"] = priv["why"]
        art["status"] = "blocked"
        return art   # delivery blocked: nothing is uploaded or wired in

    # 5) Publish to Wiki (per-client project, per-ticket sub-folder, share link).
    client = str(request.get("company") or packet.get("company") or "UNKNOWN")
    ticket_ref = str(request.get("ticket_ref") or request_id)
    pub = record(A["publish"].upload(request_id, mux.get("mp4_path"), client, ticket_ref, cfg))
    if pub.get("status") != "ok":
        art["why"] = pub.get("why") or "publish blocked"
        return art
    art["share_link"] = pub.get("share_link")

    # 6) Wire-in (OBSERVE-safe): the link is PREPARED, never posted.
    if route == "guidance":                 # verify-first: not a bug / user error -> guidance, no dev ticket
        art["dev_ticket"] = False
        art["ticket_draft"] = None
        art["guidance_reply"] = _guidance_reply(packet, verdict, art["share_link"])
    else:
        nd = prepare_note_draft(request, packet, cfg, art, verified=bool(packet.get("verified")), odoo=odoo)
        art["note_draft"] = nd
        art["dev_ticket"] = _has_dev_part(packet)
    # Status is set BEFORE attach so the copy stored on the packet carries the real status (no aliasing).
    record({"step": "wire_in", "status": "ok", "posted": False, "route": route})
    art["posted"] = False
    art["status"] = "prepared"
    # Attach the artifact (a copy, no cycle) to the packet; drops the dev part on a guidance/needs-human route.
    # The helpdesk desk calls should_offer_dev_ticket(packet) before offering a dev ticket (now wired in desk.py).
    art["packet"] = attach(packet, art)
    return art
