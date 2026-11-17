"""Sort a send error into what the sender must do about it (05 2.5, red-team #17).

- flood_wait: Telegram said "wait N seconds". Nothing went out. Wait, then try again (never faster).
- peer_flood: Telegram flagged the account (PEER_FLOOD or a spam notice). Stop ALL sends from the account.
- refused: an adapter's own safety check said no before any network call (TgSendRefused, RecipientRefused).
  Nothing went out: record failed, the key stays reusable.
- unknown: anything else, e.g. a network drop. It may or may not have gone out: never blind-retry.

Works with Telethon's errors (FloodWaitError has .seconds; PeerFloodError) and with the test fakes,
which raise FakeSendError("FloodWait" | "PeerFlood" | "NetworkDrop"). No Telethon import needed.
"""
from __future__ import annotations

import re

DEFAULT_FLOOD_SECONDS = 60


def classify_error(e: BaseException) -> tuple[str, int]:
    """Returns (kind, seconds). kind is "flood_wait", "peer_flood", "refused" or "unknown"."""
    name = type(e).__name__
    if "refused" in name.lower():
        return "refused", 0
    text = f"{name} {e}"
    low = text.lower()
    if "peerflood" in low.replace("_", "") or "spam" in low:
        return "peer_flood", 0
    if "floodwait" in low.replace("_", "") or "slowmode" in low.replace("_", ""):
        seconds = getattr(e, "seconds", None)
        if not isinstance(seconds, int):
            m = re.search(r"(\d+)\s*(?:s\b|sec|seconds)", text) or re.search(r"FLOOD_WAIT_(\d+)", text)
            seconds = int(m.group(1)) if m else DEFAULT_FLOOD_SECONDS
        return "flood_wait", max(1, int(seconds))
    return "unknown", 0
