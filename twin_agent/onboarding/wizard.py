"""Shared wizard plumbing for the onboarding flow (interview + setup).

A non-technical person completes onboarding by answering prompts. This module is the small, testable
seam under both wizards:

  - an injectable prompt loop (``ask_value``) that validates input, re-asks on invalid (bounded, so a
    scripted/empty stdin can never hang it), and lets optional items be skipped with a blank line,
  - a handful of stdlib-only validators that return a plain-English error that NEVER echoes the input
    (so a mistyped secret is not reflected back), and
  - progress/formatting helpers (``step_line``) used to show "Step 3 of 7".

Nothing here imports a heavy dependency, prints a secret, or touches the vault; it only moves strings
between the caller's injected ``ask``/``say`` and the caller's own store.
"""
from __future__ import annotations

import json
from typing import Callable
from urllib.parse import urlsplit

# Answers that mean "I don't have this / skip it", for every kind of item (secret, consent, resource).
# A non-technical user often types one of these instead of pressing Enter.
SKIP_WORDS = {"", "no", "n", "skip", "none", "n/a", "na", "not yet", "later", "-"}

MAX_ATTEMPTS = 3  # bounded re-ask: after this many invalid/blank tries we leave the item unset, never loop.


def is_skip(value: str) -> bool:
    """True if the answer means 'skip / I don't have this' (case-insensitive)."""
    return (value or "").strip().lower() in SKIP_WORDS


# ---------------------------------------------------------------- validators
# Each validator: str -> (ok, error_message). The error message is a plain-English hint that never
# contains the value the user typed (so a secret is never reflected back, even on a bad entry).

def valid_int(value: str) -> tuple[bool, str]:
    return (value.strip().isdigit(), "that should be digits only (a number, like 1234567).")


def valid_email(value: str) -> tuple[bool, str]:
    v = value.strip()
    ok = "@" in v and "." in v.split("@")[-1] and not v.startswith("@") and not v.endswith("@")
    return (ok, "that doesn't look like an email address (example: name@example.com).")


def valid_url(value: str) -> tuple[bool, str]:
    parts = urlsplit(value.strip())
    ok = parts.scheme in ("http", "https") and bool(parts.netloc)
    return (ok, "that should be a full web address starting with https:// (example: https://app.example.com).")


def valid_json(value: str) -> tuple[bool, str]:
    try:
        json.loads(value)
        return (True, "")
    except (ValueError, TypeError):
        return (False, "that isn't valid JSON — paste the whole block including the { and } braces.")


def valid_json_object(value: str) -> tuple[bool, str]:
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return (False, "that isn't valid JSON — paste the whole block including the { and } braces.")
    return (isinstance(data, dict), "that should be a JSON object in { ... } braces.")


def valid_bot_token(value: str) -> tuple[bool, str]:
    head, sep, tail = value.strip().partition(":")
    ok = bool(sep) and head.isdigit() and len(tail) >= 3
    return (ok, "a bot token looks like 123456789:AA... - copy the whole line from @BotFather.")


# ---------------------------------------------------------------- prompt loop

def ask_value(read: Callable[[str], str], prompt: str, *, optional: bool = False,
              validate: Callable[[str], tuple[bool, str]] | None = None,
              say: Callable[[str], None] = print, max_attempts: int = MAX_ATTEMPTS) -> str:
    """Ask once, validate, and re-ask up to ``max_attempts`` times on invalid/blank input.

    ``read(prompt_text) -> str`` is the injected input source (so tests drive it with scripted answers and
    the real CLI binds it to input()/getpass). Returns the accepted, stripped value, or "" when the item
    was skipped (optional + blank) or the user never gave a valid value within ``max_attempts`` (so a
    scripted or empty stdin can never hang the wizard). On invalid input it says a plain-English hint that
    never contains what the user typed.
    """
    for attempt in range(1, max_attempts + 1):
        raw = (read(prompt) or "").strip()
        if not raw:
            if optional or attempt >= max_attempts:
                return ""
            say("  (this one is needed) please type an answer, or press Enter again to skip for now.")
            continue
        if validate is not None:
            ok, err = validate(raw)
            if not ok:
                if attempt >= max_attempts:
                    say(f"  still not valid — leaving this blank for now ({err})")
                    return ""
                say(f"  {err} let's try that again.")
                continue
        return raw
    return ""


# ---------------------------------------------------------------- formatting

def step_line(n: int, total: int, title: str) -> str:
    """'Step 3 of 7 - <title>'."""
    return f"Step {n} of {total} - {title}"
