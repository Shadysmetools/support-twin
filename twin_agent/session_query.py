"""Ask-all-sessions / route-by-knowledge (MEETING-ANALYSIS §2.6, HANDOFF correction #12).

The always-on listener's core move: on each message it can query ALL open sessions - "who knows this?" - and route
the message to the session that HAS the knowledge, instead of landing it on a blind lane that lacks it (the bug
§2.6 calls out). The related session answers, and the answer comes back through Principal.

What is REAL here and now: the selection logic - which open sessions to ask, fail-closed asking, and picking the
best lane (by a session's own answer, else by the registry's knowledge tags). What is STUBBED for later: the actual
per-session query. Real inter-session RPC (prompting a running Claude tab and reading its reply) is a later wire-up,
so the query is an INJECTABLE callable `asker(lane, question) -> str | None`; the default stub returns None. Tests
pass a fake asker, so no real session or process is ever touched.
"""
from __future__ import annotations

from typing import Callable

# (lane, question) -> an answer the session gives, or None when it has nothing / cannot be reached.
Asker = Callable[[str, str], "str | None"]


def default_asker(lane: str, question: str) -> str | None:
    """The real inter-session query is not wired yet (prompting a live tab + reading its reply is a later step),
    so the default asker always answers None. The listener injects a real asker once that RPC exists."""
    return None


def _open_lanes(registry) -> list[str]:
    return [row["lane"] for row in registry.list_open()]


def ask_sessions(question: str, registry, *, asker: Asker | None = None) -> list[tuple[str, str | None]]:
    """Ask EVERY open session the question ("who knows this?"). Returns one (lane, answer-or-None) per open lane,
    newest-active first. Fail-closed per lane: if a session's asker raises (a wedged/closing tab), that lane
    answers None and the sweep continues - one bad session never blocks the rest."""
    asker = asker or default_asker
    out: list[tuple[str, str | None]] = []
    for lane in _open_lanes(registry):
        try:
            answer = asker(lane, question)
        except Exception:                      # noqa: BLE001 - a wedged session must not break the who-knows sweep
            answer = None
        out.append((lane, answer if answer else None))
    return out


def best_session_for(question: str, registry, *, asker: Asker | None = None) -> str | None:
    """Pick the open session that should handle `question` (route by knowledge, §2.6).

    1) Ask all open sessions; the first that gives a real answer HAS the knowledge - route there.
    2) No session answered (e.g. the default stub): fall back to the registry's knowledge hint - the first open
       lane whose tags match the topic (registry.knows, word-boundary).
    Returns None when nobody answers and no tag matches, so the caller opens a FRESH lane rather than dropping the
    message on a blind session (the bug §2.6 fixes)."""
    for lane, answer in ask_sessions(question, registry, asker=asker):
        if answer:
            return lane
    for lane in _open_lanes(registry):
        if registry.knows(lane, question):
            return lane
    return None
