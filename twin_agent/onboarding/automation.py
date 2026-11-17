"""set-automation: translate the wizard's AUTOMATIC / MANUAL choice into the twin's autopilot PREFERENCE flags.

It edits config.yaml IN PLACE, changing only the `autopilot:` lines and preserving every other line and every
inline comment (a plain YAML round-trip would wipe the comments). It toggles PREFERENCES only - they still fire
only inside their own gates:
  - voice.autopilot needs voice consent (D2) + run_mode live,
  - timesheets.autopilot needs its earned eligibility (14 days, >90% unchanged approvals).
It NEVER touches run_mode or send_enabled: going live still needs consent D1 + the guard self-test. So
"automatic" means "the twin runs on its own WITHIN the safety gates", never "send unproven things on day one".

CLI:
    python -I aa.py set-automation auto|manual [--config PATH] [--dry-run]
    python -I aa.py set-automation            # read the AUTOMATIC/MANUAL answer from the onboarding profile
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# The 'autopilot:' lines we flip. Both voice.autopilot and timesheets.autopilot are 'autopilot:' keys; AUTOMATIC
# and MANUAL set both the same way, so one line-level rule covers them without needing section context.
_AUTOPILOT_LINE = re.compile(r"(?m)^(?P<pre>\s*autopilot:\s*)(?:true|false)(?P<post>[^\n]*)$")


def answer_to_mode(answer: str | None) -> str | None:
    """'auto' / 'manual' / None from a free-text answer (AUTOMATIC, manual, 'a', ...)."""
    a = (answer or "").strip().lower()
    if a.startswith("auto") or a == "a":
        return "auto"
    if a.startswith("man") or a == "m":
        return "manual"
    return None


def apply_to_text(text: str, mode: str) -> tuple[str, int]:
    """Return (new_text, n_changed). mode 'auto' -> autopilot: true; 'manual' -> false. Comments preserved."""
    value = "true" if mode == "auto" else "false"
    n = 0

    def repl(m: re.Match) -> str:
        nonlocal n
        n += 1
        return f"{m.group('pre')}{value}{m.group('post')}"

    return _AUTOPILOT_LINE.sub(repl, text), n


def _config_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    from .. import paths
    return paths.config_dir() / "config.yaml"


def _profile_answer() -> str | None:
    from .interview import load_profile
    return load_profile().get("automation")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="set-automation")
    ap.add_argument("mode", nargs="?", choices=["auto", "manual", "automatic"], default=None,
                    help="auto(matic) or manual; omit to read the onboarding answer")
    ap.add_argument("--config", default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)

    mode = "auto" if a.mode in ("auto", "automatic") else a.mode
    if mode is None:
        mode = answer_to_mode(_profile_answer())
        if mode is None:
            print("no automation choice given and none in the onboarding profile - run `aa.py onboard` or pass "
                  "auto|manual.")
            return 2

    path = _config_path(a.config)
    if not path.is_file():
        print(f"config not found at {path} - copy profiles/<name>/config.*.yaml to it first.")
        return 2
    text = path.read_text(encoding="utf-8")
    new, n = apply_to_text(text, mode)
    label = "AUTOMATIC (autopilot preferences ON)" if mode == "auto" else "MANUAL (autopilot OFF)"
    if n == 0:
        print(f"no 'autopilot:' lines found in {path} - nothing to change.")
        return 1
    if a.dry_run or new == text:
        print(f"{label}: would set {n} autopilot flag(s)" if new != text else f"{label}: already set ({n} flags).")
        return 0
    path.write_text(new, encoding="utf-8")
    print(f"{label}: set {n} autopilot flag(s) in {path}.")
    print("Note: this is a PREFERENCE. Sending still needs consent D1 + the guard self-test + run_mode: live; "
          "timesheet autopilot still needs its earned eligibility. The twin always stops at client-facing gates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
