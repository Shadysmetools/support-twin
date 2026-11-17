"""Deterministic FAKES for every hub seam. Injected into ``Hub`` by tests; the whole 11-stage pipeline runs with
no network, no credentials, no browser, no ffmpeg. Each fake RECORDS what it was asked to do, so tests can assert
things like "exactly one automatic client message" or "zero sends without owner approval".

These are NEVER a production fallback: ``adapters_real.py`` holds the real adapters, each of which blocks itself
with a plain reason when its key/dependency is missing.
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..channels import whatsapp_cloud
from .model import Owner


# ---------------------------------------------------------------- WhatsApp (outbound only; the hub is sole sender)

class FakeWhatsApp:
    """Records every outbound text. Enforces the 24h customer-service window via the REAL
    ``whatsapp_cloud.in_service_window`` (reuse, not a reimplementation). ``automatic`` marks the auto-ack so a
    test can assert the ack is the ONLY automatic client message."""

    def __init__(self, *, window_hours: float = 24):
        self.sent: list[dict] = []
        self.window_hours = window_hours

    def send_text(self, to: str, text: str, *, last_inbound_at, now=None, automatic: bool = False,
                  intake: bool = False) -> dict:
        now = now or datetime.now(timezone.utc)
        if not whatsapp_cloud.in_service_window(last_inbound_at, now, self.window_hours):
            raise whatsapp_cloud.WindowClosed("outside the 24h window: only an approved template may be sent")
        rec = {"to": to, "text": text, "automatic": bool(automatic), "ts": now,
               "msg_id": f"wamid.FAKE{len(self.sent) + 1:04d}"}      # like the real sender: Meta's id for delivery statuses
        self.sent.append(rec)
        return rec

    def send_interactive(self, to: str, interactive: dict, *, last_inbound_at, now=None, automatic: bool = False,
                         intake: bool = False) -> dict:
        now = now or datetime.now(timezone.utc)
        if not whatsapp_cloud.in_service_window(last_inbound_at, now, self.window_hours):
            raise whatsapp_cloud.WindowClosed("outside the 24h window: only an approved template may be sent")
        rec = {"to": to, "text": (interactive.get("body") or {}).get("text", ""), "interactive": interactive,
               "automatic": bool(automatic), "ts": now, "msg_id": f"wamid.FAKE{len(self.sent) + 1:04d}"}
        self.sent.append(rec)
        return rec

    def automatic_count(self) -> int:
        return sum(1 for s in self.sent if s["automatic"])


# ---------------------------------------------------------------- STT (Groq whisper-large-v3)

class FakeSTT:
    """Returns a recorded transcript fixture (plan §14.3: skip the live Groq call in CI, use a fixture). Marks the
    result as a PLACEHOLDER because the only audio on disk is synthetic TTS — real customer notes pending."""

    def __init__(self, transcripts: dict[str, str] | None = None):
        # media_id -> transcript. Default covers the voice fixture's media id.
        self.transcripts = transcripts or {
            "MEDIA_AUDIO_0001": "عندي مشكلة في المخزون، الكمية ظاهرة غلط بعد ما عملت تسليم",
        }
        self.calls: list[str] = []

    def transcribe(self, audio_id: str, *, lang_hint: str = "") -> dict:
        self.calls.append(audio_id)
        text = self.transcripts.get(audio_id, "(no transcript fixture for this clip)")
        return {"text": text, "lang": "ar" if any("؀" <= c <= "ۿ" for c in text) else "en",
                "provider": "groq-whisper-large-v3 (fixture)", "model": "whisper-large-v3",
                "placeholder": True, "audio_retained": True}


# ---------------------------------------------------------------- TicketWriter (translation + ticket words)

class FakeTicketWriter:
    """Deterministic stand-in for the ticket-writing LLM: NO model, NO network. ``translations`` maps an input message
    to its English; an English message is its own translation; anything else gets an obvious marker (never a made-up
    meaning). ``problem`` is the first few English words. Records its calls. Never a production fallback."""

    def __init__(self, translations: dict[str, str] | None = None, *, steps: dict[str, str] | None = None):
        self.translations = dict(translations or {})
        self.steps = dict(steps or {})
        self.calls: list[str] = []

    def write(self, text: str, *, lang: str, analysis, client: str, names=None) -> dict:
        self.calls.append(lang)
        english = self.translations.get(text) or (text if lang == "en" else f"[fake translation of {len(text)} characters]")
        return {"status": "ok", "english": english, "problem": " ".join(english.split()[:6]),
                "expect": "", "steps": self.steps.get(text, "")}


# ---------------------------------------------------------------- Telegram (per-ticket topic + approval channel)

class FakeTelegram:
    """Opens one topic per ticket and is the owner-approval channel. ``request_approval`` is ASYNCHRONOUS: it only
    RECORDS the request (and returns ``pending``). The owner's tap arrives later, as a separate call to
    ``Hub.on_owner_decision(nonce, approver_tg_id, ...)`` (tests use ``hub_helpers.approve``)."""

    def __init__(self):
        self.topics: dict[int, dict] = {}
        self.posts: list[dict] = []
        self.triage: list[str] = []
        self.approvals: list[dict] = []
        self._next = 5000

    def open_topic(self, tid: int, owner, brief: str) -> int:
        self._next += 1
        name = getattr(owner, "name", str(owner))
        self.topics[self._next] = {"tid": tid, "owner": name, "brief": brief}
        return self._next

    def post(self, topic_id: int, text: str) -> None:
        self.posts.append({"topic_id": topic_id, "text": text})

    def post_triage(self, text: str) -> None:
        self.triage.append(text)

    def request_approval(self, topic_id: int, *, ticket_id: int, kind: str, draft: str, nonce: str,
                         draft_hash: str, allowed_approvers: list[int], expires_at, offer_whatsapp: bool = False) -> dict:
        req = {"offer_whatsapp": offer_whatsapp, "topic_id": topic_id, "ticket_id": ticket_id, "kind": kind, "draft": draft, "nonce": nonce,
               "draft_hash": draft_hash, "allowed_approvers": list(allowed_approvers), "expires_at": expires_at}
        self.approvals.append(req)
        return {"status": "pending", "nonce": nonce}


# ---------------------------------------------------------------- Teamhub (team bus, post-only)

class FakeTeamhub:
    """A fake Teamhub inbound-webhook transport. Models the documented behaviour: each ``open`` is its OWN thread and
    ``_event_id`` dedupes. ``append=True`` models a transport that can add to a thread (an agent identity); ``fail``
    makes every call raise (an outage). ``threads`` is everything Teamhub would ever hold: tests assert no client data."""

    def __init__(self, *, append: bool = False, fail: Exception | None = None):
        self.supports_append = append
        self.fail = fail
        self.threads: list[dict] = []
        self._seen: set[str] = set()

    def open(self, title: str, content: str, event_id: str) -> str | None:
        if self.fail:
            raise self.fail
        for t in self.threads:
            if t["event_id"] == event_id:
                return t["id"]
        t = {"id": f"k{len(self.threads) + 1}", "event_id": event_id, "title": title, "content": content,
             "messages": []}
        self.threads.append(t)
        return t["id"]

    def append(self, thread_id: str, content: str, event_id: str) -> None:
        if self.fail:
            raise self.fail
        if event_id in self._seen:
            return
        self._seen.add(event_id)
        next(t for t in self.threads if t["id"] == thread_id)["messages"].append(content)


# ---------------------------------------------------------------- Knowledge sources (Odoo + Wiki), read-only

class FakeOdooKnowledge:
    """Stub of the Odoo customer-knowledge source: sender -> partner, plus client history used by routing."""

    def __init__(self, partners: dict[str, int] | None = None, context: dict[int, dict] | None = None):
        self.partners = partners if partners is not None else {"201000000001": 3001, "201000000002": 3002}
        self.context = context if context is not None else {}

    def partner_for(self, wa_id: str) -> int | None:
        return self.partners.get(str(wa_id))

    def client_context(self, partner_id: int) -> dict:
        return dict(self.context.get(int(partner_id), {"source": "odoo", "modules": [], "owner_uids": []}))


class FakeWikiKnowledge:
    """Stub of the Wiki 'Acme' org source: meetings/decisions/commitments/usual-collaborators per client
    (plan §14). Read-only; an OAuth MCP or org API key is the real headless path (tracked blocker)."""

    def __init__(self, context: dict[int, dict] | None = None):
        self.context = context or {
            3001: {"source": "wiki", "agreed": ["monthly inventory review"],
                   "open_commitments": ["enable lot tracking"], "usual_collaborators": ["Supervisor"],
                   "modules": ["Inventory"], "owner_uids": [778]},
        }

    def partner_for(self, wa_id: str) -> int | None:   # Wiki is keyed by partner, resolved via Odoo
        return None

    def client_context(self, partner_id: int) -> dict:
        return dict(self.context.get(int(partner_id), {"source": "wiki"}))


def merge_context(*contexts: dict) -> dict:
    """Merge the two knowledge sources into one brief dict (lists concatenated, scalars last-wins)."""
    out: dict = {"modules": [], "owner_uids": [], "agreed": [], "open_commitments": [], "usual_collaborators": [],
                 "type_owner": {}}
    for c in contexts:
        for k, v in (c or {}).items():
            if isinstance(v, list):
                out.setdefault(k, [])
                for item in v:
                    if item not in out[k]:
                        out[k].append(item)
            elif isinstance(v, dict):
                out.setdefault(k, {}).update(v)
            else:
                out[k] = v
    return out


# ---------------------------------------------------------------- Metrics connector (stage 11)

class FakeMetrics:
    """Records the KPI payload that WOULD be pushed to Metrics. No key, no network (stubbed)."""

    def __init__(self):
        self.pushed: list[dict] = []

    def push_kpis(self, outcome: dict) -> dict:
        rec = {"ok": True, "simulated": True, "outcome": dict(outcome)}
        self.pushed.append(rec)
        return rec


def owner(uid: int, name: str, tg_id: int = 0, role: str = "") -> Owner:
    return Owner(uid=uid, name=name, tg_id=tg_id, role=role)
