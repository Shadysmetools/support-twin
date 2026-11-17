"""Plain-English Watcher digest for Sysadmin, and the adapters that deliver it.

This is an internal report to Sysadmin (not client traffic and never "as Principal"), so it does not go through the
Sender. It still refuses to post text that shows a secret or personal-data finding.
`FakeDigestSender` (tests, dry runs) keeps the text in memory. `BotDigestSender` posts to Sysadmin's Telegram via the
Bot API, only when `run_mode: live`, `watcher.digest.sysadmin_tg_user_id` is set and the vault holds the bot token.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..redact import scan


class DigestRefused(Exception):
    pass


class DigestSender(Protocol):
    def send(self, text: str) -> bool: ...


@dataclass
class FakeDigestSender:
    sent: list[str] = field(default_factory=list)

    def send(self, text: str) -> bool:
        if scan(text):
            raise DigestRefused("digest text has a secret or personal-data finding")
        self.sent.append(text)
        return True


class BotDigestSender:
    """Telegram Bot API sendMessage to Sysadmin only. `post` and `get_secret` are injected in tests."""

    def __init__(self, cfg: dict, *, has_secret: Callable[[str], bool], get_secret: Callable[[str], str] | None = None,
                 post: Callable[..., object] | None = None):
        d = (cfg.get("watcher") or {}).get("digest") or {}
        self.chat_id = int(d.get("sysadmin_tg_user_id") or 0)
        self.vault_name = str(d.get("vault_name") or "tg-bot-token")
        if cfg.get("run_mode") != "live":
            raise DigestRefused("run_mode is not live")
        if not self.chat_id:
            raise DigestRefused("watcher.digest.sysadmin_tg_user_id is not set")
        if not has_secret(self.vault_name):
            raise DigestRefused(f"vault name {self.vault_name} is missing")
        if get_secret is None:
            from .. import creds
            get_secret = creds._get
        self._get, self._post = get_secret, post

    def __repr__(self) -> str:                               # never show the token
        return "BotDigestSender()"

    def send(self, text: str) -> bool:
        if scan(text):
            raise DigestRefused("digest text has a secret or personal-data finding")
        post = self._post
        if post is None:
            import httpx                                     # lazy: tests never import it
            post = httpx.post
        token = self._get(self.vault_name)
        try:
            r = post(f"https://api.telegram.org/bot{token}/sendMessage",
                     json={"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True}, timeout=30)
        except Exception as e:                               # noqa: BLE001 - never echo the URL (it holds the token)
            raise DigestRefused(f"post failed: {type(e).__name__}") from None
        return getattr(r, "status_code", 0) == 200


def _pct(x) -> str:
    return f"{round(100 * x)}%" if isinstance(x, (int, float)) else "-"


def digest_text(fp: dict, scores: list[dict], *, new_cases: int, prev: dict | None = None) -> str:
    """A short message Sysadmin can read on his phone. Numbers and aliases only, no message text."""
    o = fp.get("overall") or {}
    mix = ", ".join(f"{k} {_pct(v)}" for k, v in (o.get("language_mix") or {}).items()) or "no messages yet"
    lines = [f"Watcher digest: {new_cases} case(s) in, {fp.get('cases', 0)} total, {fp.get('messages', 0)} of "
             f"Principal's own messages learned from.",
             f"- He writes: {mix}; usually {o.get('median_words', '-')} words; replies in about "
             f"{o.get('median_reply_min', '-')} min (slowest quarter: over {o.get('p75_reply_min', '-')} min)."]
    d = fp.get("diagnosis") or {}
    first = next(iter(d.get("first_move") or {}), None)
    if first:
        lines.append(f"- His usual first move: {first.replace('_', ' ')}. Escalates in {_pct(d.get('escalate_rate'))} "
                     f"of cases, says no in {_pct(d.get('decline_rate'))}.")
    e = fp.get("edits") or {}
    if e.get("count"):
        lines.append(f"- Drafts: {e['count']} seen, {_pct(e.get('unchanged_rate'))} sent unchanged; when he edits, "
                     f"the result is {e.get('length_ratio', '-')}x the draft length.")
    if scores:
        avg = round(sum(s["score"] for s in scores) / len(scores), 1)
        low = sorted(scores, key=lambda s: s["score"])[:3]
        lines.append(f"- Fingerprint match (would Principal write this?): {avg}/100 over {len(scores)} draft(s). "
                     "Lowest: " + ", ".join(f"{s['request_id']} {s['score']}" for s in low) + ".")
        if prev and prev.get("avg_score") is not None:
            lines.append(f"- Change since last digest: {round(avg - prev['avg_score'], 1):+} points.")
    else:
        lines.append("- No agent drafts to score yet.")
    lines.append("Details: FINGERPRINT.md on your device.")
    return "\n".join(lines)
