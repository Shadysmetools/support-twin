"""Opt-in per-ticket screen recording (stub-grade): ffmpeg gdigrab of ONE window, with visible indicators.

Off unless consent D4 + `watcher.enabled` + `watcher.screen_record.enabled` and not paused. It records one named
window (`window_title`, e.g. the browser running Odoo), never the whole desktop unless `allow_desktop: true` is set
on purpose, so personal chats in other windows stay out. Indicators while it runs:
- ffmpeg `-show_region 1` draws a border around what is being recorded;
- `data/watcher/RECORDING` holds the request id; the board and /status show "RECORDING SCREEN (REQ-...)".
The video goes to `data/proof/<REQ>/` so it can double as the proof video. It is NEVER put in a sync bundle
(a video cannot be redacted); the bundle gets only a note that a recording exists.
Command lines are argument lists (no shell). Tests inject `run` and never start ffmpeg.
"""
from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from .. import paths
from . import gate


class RecordingRefused(Exception):
    """Not allowed right now (no consent, switched off, paused, or no window named)."""


def _scfg(cfg: dict) -> dict:
    return (cfg.get("watcher") or {}).get("screen_record") or {}


def command(cfg: dict, out_path: Path, seconds: int) -> list[str]:
    s = _scfg(cfg)
    title = str(s.get("window_title") or "").strip()
    if title:
        source = f"title={title}"
    elif s.get("allow_desktop") is True:
        source = "desktop"
    else:
        raise RecordingRefused("no window_title set and allow_desktop is false: refusing to record the desktop")
    return [str(s.get("ffmpeg") or "ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "gdigrab", "-framerate", str(int(s.get("fps") or 5)), "-draw_mouse", "1", "-show_region", "1",
            "-i", source, "-t", str(int(seconds)), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(out_path)]


def record_until_paused(cmd: list[str], seconds: int, should_stop: Callable[[], bool], *,
                        popen: Callable[..., subprocess.Popen] = subprocess.Popen, poll_s: float = 1.0,
                        sleep: Callable[[float], None] = time.sleep) -> bool:
    """Run ffmpeg, checking `should_stop` (a /pause) every `poll_s`. On a pause ffmpeg gets "q" (it closes the file
    cleanly) or is killed. True = stopped by a pause. A check that crashes counts as a pause (fail closed)."""
    proc = popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    waited = 0.0
    while proc.poll() is None:
        try:
            stop = bool(should_stop())
        except Exception:                                   # noqa: BLE001 - cannot tell: stop recording
            stop = True
        if stop or waited > seconds + 60:
            try:
                proc.communicate(b"q", timeout=10)
            except Exception:                               # noqa: BLE001
                proc.kill()
            return stop
        sleep(poll_s)
        waited += poll_s
    return False


def record(cfg: dict, db, request_id: str, now: datetime, *, seconds: int = 300,
           run: Callable[..., object] | None = None, notify: Callable[[str], None] | None = None,
           popen: Callable[..., subprocess.Popen] = subprocess.Popen,
           now_fn: Callable[[], datetime] | None = None, poll_s: float = 1.0) -> Path:
    """Record one window for `seconds` (max 30 min) for this request. Returns the video path.

    Without `run` (the real path) a /pause sent while recording stops ffmpeg within about `poll_s` seconds.
    `run` (tests, old callers) runs the command blocking, with no pause check."""
    if not re.fullmatch(r"REQ-\d{4,}", request_id or ""):
        raise RecordingRefused("request id must look like REQ-0001")
    if not gate.allowed(cfg, db, "screen_record", now):
        raise RecordingRefused("screen recording is not allowed now (consent D4, watcher.screen_record, /pause)")
    seconds = max(1, min(int(seconds), 1800))
    out_dir = paths.data_dir() / "proof" / request_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"screen-{now:%Y%m%d-%H%M%S}.mp4"
    cmd = command(cfg, out, seconds)
    flag = gate.recording_file()
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(request_id, encoding="utf-8")
    if notify:
        notify(f"Recording your screen for {request_id} ({seconds // 60} min max). A border shows what is recorded. "
               "Send /pause to stop the recording and the Watcher.")
    try:
        db.log_event("watcher_recording", request_id, actor="watcher", seconds=seconds, file=out.name)
        if run is not None:
            run(cmd, check=False, stdin=subprocess.DEVNULL, timeout=seconds + 60)
        else:
            if now_fn is None:
                from ..clock import SystemClock
                now_fn = SystemClock().now
            if record_until_paused(cmd, seconds, lambda: gate.paused_now(db, now_fn()), popen=popen, poll_s=poll_s):
                db.log_event("watcher_recording_paused", request_id, actor="watcher", file=out.name)
                if notify:
                    notify(f"Recording for {request_id} stopped: the Watcher is paused.")
    finally:
        flag.unlink(missing_ok=True)
    return out
