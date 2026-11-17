"""`python -I aa.py voice <sub>`: status | check. Offline only: nothing here calls ElevenLabs or sends anything.

status: is voice on, and if not, why (consent D1 + D2, voice.enabled, live, ffmpeg, vault entry name only).
check:  would this text be allowed as a voice note (the persona rule in policy.py)? Prints reasons, not the text.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from ..models import OutKind
from .policy import detect_lang, voice_cfg, voice_consent_ok, voice_text_problems


def status_lines(cfg: dict, *, has_secret=None, which=shutil.which) -> list[str]:
    v = voice_cfg(cfg)
    consent = cfg.get("consent") or {}
    if has_secret is None:
        def has_secret(name):                               # noqa: E306 - keyring missing = "unknown", never a crash
            try:
                from .. import creds
                return creds.has_secret(name)
            except Exception:                               # noqa: BLE001
                return None
    secret = has_secret(str(v["vault_name"]))
    blockers = [why for ok, why in (
        (bool(consent.get("d1_ref")), "consent.d1_ref not set"),
        (bool(consent.get("d2_voice_ref")), "consent.d2_voice_ref (written voice consent) not set"),
        (v["enabled"] is True, "voice.enabled is false"),
        (cfg.get("run_mode") == "live", "run_mode is not live"),
        (which(str(v["ffmpeg"])) is not None, "ffmpeg not found"),
        (secret is True, f"vault entry {v['vault_name']} {'unknown (no keyring)' if secret is None else 'not set'}"),
    ) if not ok]
    return [f"Voice notes: {'ON' if not blockers else 'off'}",
            *(f"- blocked: {b}" for b in blockers),
            f"- consent D1 + D2 + switch: {'yes' if voice_consent_ok(cfg) else 'no'}",
            f"- autopilot voice: {'on' if v['autopilot'] is True else 'off (only Principal tapping Send as voice)'}",
            f"- lanes: {', '.join(v['lanes'])}; languages: {', '.join(v['langs'])}",
            f"- caption: {v['caption']}; monthly character cap: {v['monthly_char_cap']}"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="voice", description=__doc__.splitlines()[0])
    ap.add_argument("--config", type=Path, default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    c = sub.add_parser("check")
    c.add_argument("text")
    c.add_argument("--kind", default=OutKind.answer.value, choices=[k.value for k in OutKind])
    c.add_argument("--lang", default=None, choices=["en", "ar", "franco"])
    args = ap.parse_args(argv)
    from ..config import load_config
    cfg = load_config(args.config)
    if args.cmd == "status":
        print("\n".join(status_lines(cfg)))
        return 0
    problems = voice_text_problems(args.text, kind=args.kind, lang=args.lang, cfg=cfg)
    print(f"language: {args.lang or detect_lang(args.text)}")
    print("OK as a voice note" if not problems else "not as a voice note: " + "; ".join(problems))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
