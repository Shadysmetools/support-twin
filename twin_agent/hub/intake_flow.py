"""Intake v2 (spec INTAKE-FLOW-v2-2026-11-17): the PRE-TICKET conversation, before any Odoo ticket exists.

  awaiting_company -> awaiting_type -> the ticket is created -> ONE confirmation (``pipeline._ack``)

State is one doc per sender in the store (``intake`` namespace), saved BEFORE every send (the production sender checks for
an open flow), so it survives a restart. The rules:
  - identity is ALWAYS ``partner_for`` (it fails closed on staff, ambiguous and test numbers). A sender that is exactly ONE
    saved contact WITH a company skips the company question;
  - the company answer is linked ONLY on a single EXACT (case-insensitive, whole-name) match with an Odoo company. Anything
    else (no match, several, a near name) is "Pending company": the contact is saved with no company, the answer is
    recorded on the ticket and the owner is alerted. NO existing company name is ever shown to the client;
  - the sender is always saved as a contact (guard purpose ``client_contact``: name, phone, company link only);
  - re-asks are bounded (ONE, spec §3), then the owner is alerted and the flow moves on (company: pending; type: Question). The
    ticket is ALWAYS created: after silence too (``tick``: one re-ask, then the same defaults);
  - a message mid-flow that is not the answer is KEPT in the pending context (voice notes and photos as references) and
    carried onto the ticket, never opens a second ticket and never counts as a failed answer;
  - a TEST sender (a "Acme Test Client - " contact, or any sender with an open test window: ``test_clients`` or ``test_numbers``) runs the whole flow but
    is NEVER linked to a company.
Everything the client sees is rendered from ``persona/messages.yaml`` (the type buttons from ``intake_buttons``), and every
message goes through the real ``wording_lint``: ANY finding, or any error in the lint, blocks the send and tells the owner.
Questions are intake messages: only inside the 24h window, only to a sender with an open flow.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from ..channels import whatsapp_cloud
from ..helpdesk import guard
from . import events, intake_buttons, persona
from .model import Inbound
from .wording_lint import lint

log = logging.getLogger("twin_agent.hub")

NS, CONTACTS_NS = "intake", "intake_contacts"
OPEN_STEPS = ("awaiting_company", "awaiting_type")
MAX_REASKS = 1                                          # spec §3: re-ask ONCE, then file it
TYPE_FIELDS = {"bug": ("Bug", "issue", "functional"), "feature": ("New Request", "feature", "development"),
               "question": ("Inquiries", "issue", "inquiry")}
_GREETING = {"hi", "hello", "hey", "thanks", "thank", "you", "salam", "ok", "okay", "السلام", "عليكم", "مرحبا", "اهلا", "أهلا"}
_REQUESTY = {"not", "error", "issue", "problem", "wrong", "cannot", "can't", "cant", "need", "want", "please", "help", "how",
             "why", "what", "when", "fail", "failed", "crash", "broken", "doesn't", "don't", "isn't", "won't", "unable"}
_LEAD = re.compile(r"^(?:hi|hello|hey)[,!. ]+|^(?:i\s*(?:'m|am)?\s*)?(?:work(?:ing)?|from|with|at)\s+(?:at\s+|for\s+|with\s+)?"
                   r"|^we\s*(?:are|'re)\s+(?:from\s+|at\s+|with\s+)?|^(?:my|our|the)\s+company\s+(?:name\s+)?is\s+", re.IGNORECASE)
_MEDIA_LABEL = {"image": "Photo", "video": "Video", "document": "Document", "voice": "Voice note"}


def _iso(d: datetime) -> str:
    return d.isoformat()


def _digits(s) -> str:
    return "".join(c for c in str(s or "") if c.isdigit())


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _company_candidates(said: str) -> list[str]:
    """The client's own words, whitespace-tidied, then with a leading "I work at" / "we are from" taken off."""
    raw = re.sub(r"\s+", " ", said).strip().strip(".!,؟ ")
    stripped = _LEAD.sub("", raw, count=1).strip().strip(".!,؟ ")
    return [c for c in dict.fromkeys([raw, stripped]) if c]


def _is_answer(said: str) -> bool:
    """A company name is short and is not a question or a greeting; anything else is the client's REQUEST, kept as context."""
    words = {w.lower().strip(".,!") for w in said.split()}
    return bool(words) and len(said.split()) <= 6 and "?" not in said and "؟" not in said \
        and not words <= _GREETING and not words & _REQUESTY


class IntakeFlow:
    def __init__(self, hub):
        self.h = hub

    # ---------------------------------------------------------------- switches and lookups

    def enabled_for(self, wa_id: str) -> bool:
        """``intake_v2``: false (default, the old behaviour) | true | "test_only" (test numbers and open test-client
        windows only). A sender with an open flow always continues it."""
        mode = self.h.cfg.get("intake_v2", False)
        if mode is True or str(mode).lower() in ("on", "true"):
            return True
        return str(mode).lower() == "test_only" and self._is_test_sender(wa_id)

    def _is_test_sender(self, wa_id: str) -> bool:
        wins = getattr(self.h.k_odoo, "windows", None)      # the test-client windows (test_clients AND test_numbers)
        return bool(wins is not None and wins.active(_digits(wa_id)))

    def has_flow(self, wa_id: str) -> bool:
        doc = self.h.store.get(NS, str(wa_id))
        return bool(doc and doc.get("step") in OPEN_STEPS)

    def saved_partner(self, wa_id: str) -> int:
        """The contact this flow just saved, for a SHORT time only (``intake_contact_ttl_minutes``, default 10), and only if
        Odoo still has it as a person. After that ``partner_for`` (which fails closed) is the only identity."""
        d = self.h.store.get(CONTACTS_NS, str(wa_id))
        if not d:
            return 0
        ttl = timedelta(minutes=float(self.h.cfg.get("intake_contact_ttl_minutes", 10)))
        if self.h.clock() - datetime.fromisoformat(d["at"]) > ttl:
            return 0
        info = self._contact(d["partner_id"])
        return int(d["partner_id"]) if info and not info["is_company"] else 0

    # ---------------------------------------------------------------- entry from the pipeline

    def route(self, inbound: Inbound, partner, state) -> dict | None:
        """None = not an intake case (the normal pipeline handles it)."""
        doc = self.h.store.get(NS, inbound.wa_id)
        if doc and doc["step"] in OPEN_STEPS:
            return self._advance(doc, inbound)
        if doc and doc["step"] == "refused" and self.h.clock() - datetime.fromisoformat(doc["updated_at"]) < timedelta(hours=24):
            return {"held": "intake_refused", "message_id": inbound.message_id}      # a human has it: no loop, no new alert
        if self._would_open(inbound, partner, state):
            return self._start(inbound, partner)
        return None

    def _would_open(self, inbound: Inbound, partner, state) -> bool:
        h = self.h
        if state is None or not partner or h._other_partner(state, partner):
            return True
        return state.stage == "resolved" and (state.keep_closed or not h._within_reopen(state))

    # ---------------------------------------------------------------- timers (tick): silence never drops a client

    def timer_pass(self, now: datetime) -> int:
        """One polite re-ask after ``intake_reask_after_minutes`` (30) of silence; after ``intake_close_after_minutes`` (120)
        the defaults apply (company: Pending company, type: Question) and the ticket is CREATED. Never waits for a busy
        sender (a non-blocking lock), and re-reads the doc under it."""
        n = 0
        for doc in self.h.store.scan(NS):
            if doc.get("step") not in OPEN_STEPS:
                continue
            lock = self.h._wa_lock(doc["wa_id"])
            if not lock.acquire(blocking=False):
                continue
            try:
                cur = self.h.store.get(NS, doc["wa_id"])
                n += bool(cur and cur.get("step") in OPEN_STEPS and self._timer_step(cur, now))
            finally:
                lock.release()
        return n

    def _timer_step(self, doc: dict, now: datetime) -> bool:
        idle = now - datetime.fromisoformat(doc["updated_at"])
        if idle >= timedelta(minutes=float(self.h.cfg.get("intake_close_after_minutes", 120))):
            self._force(doc)
            return True
        if idle >= timedelta(minutes=float(self.h.cfg.get("intake_reask_after_minutes", 30))) and not doc.get("timer_reasked"):
            doc["timer_reasked"] = True
            self._save(doc)
            self._send_step_reask(doc, self._last(doc))
            return True
        return False

    def _force(self, doc: dict) -> None:
        inbound = self._last(doc)
        if not doc.get("forced"):                              # the defaults and the owner alerts happen ONCE, even if Odoo is down
            events.event("intake_forced", step=doc["step"])
            doc["forced"] = True
            if doc["step"] == "awaiting_company":
                doc["pending_company"] = True
                self.h._hold("intake_pending_company", inbound=inbound,
                             detail="no company answer in time; the contact is saved as Pending company. Please confirm it in Odoo")
            if not doc["type"]:
                doc.update(type="question", defaulted=True)
                self.h._hold("intake_type_defaulted", inbound=inbound,
                             detail="the client did not pick a request type in time; opened as Question, please check")
            self._save(doc)
        if self._ensure_contact(doc, inbound):
            self._save(doc)                                    # keep the saved contact id for a retry
            self._open(doc, inbound)

    def _last(self, doc: dict) -> Inbound:
        """The first inbound, stamped with the time of the client's LATEST message (the 24h window runs from it)."""
        first = Inbound.from_dict(doc["first"])
        first.ts = datetime.fromisoformat(doc["last_ts"])
        return first

    # ---------------------------------------------------------------- start

    def _start(self, inbound: Inbound, partner) -> dict:
        h, now, d = self.h, self.h.clock(), _digits(inbound.wa_id)
        staff, wins = getattr(h.k_odoo, "staff", None), getattr(h.k_odoo, "windows", None)
        if staff is not None and staff.contains(d) and not (wins is not None and wins.active(d)):
            return h._hold("unknown_sender", inbound=inbound, detail="a staff number is not a client: no intake questions")
        info = self._contact(partner) if partner else None
        doc = {"wa_id": inbound.wa_id, "step": "awaiting_company", "started_at": _iso(now), "updated_at": _iso(now),
               "last_ts": inbound.ts.isoformat(), "first": inbound.to_dict(), "context": [], "handled": [inbound.message_id],
               "reasks": 0, "media_prompted": False, "partner_id": info["id"] if info else 0,
               "is_test": bool(info and info["is_test"]) or self._is_test_sender(inbound.wa_id),
               "partner_is_company": bool(info and info["is_company"]), "company_id": 0, "company_name": "",
               "company_said": "", "pending_company": False, "type": "", "defaulted": False}
        events.event("intake_started", known=bool(info), test=doc["is_test"])
        if info and info["parent_id"] and not doc["is_test"]:
            doc["company_id"], doc["company_name"] = info["parent_id"], info["parent_name"]
            return self._after_company(doc, inbound)
        self._save(doc)                                       # BEFORE the send: the sender gate checks for an open flow
        self._send(inbound, "company_question", "company")
        return {"intake": "awaiting_company"}

    def _contact(self, partner) -> dict | None:
        """The partner behind ``partner_for`` and its company (a company record is its own company). None = unreadable."""
        row = self.h.odoo.read_partner(int(partner)) if partner else None
        if not row:
            return None
        par = row.get("parent_id")
        pid = int(par[0] if isinstance(par, (list, tuple)) and par else par or 0)
        pname = str(par[1]) if isinstance(par, (list, tuple)) and len(par) > 1 else ""
        is_co = bool(row.get("is_company"))
        return {"id": int(partner), "is_company": is_co, "parent_id": int(partner) if is_co else pid,
                "parent_name": str(row.get("name") or "") if is_co else pname,
                "is_test": str(row.get("name") or "").startswith(guard.TEST_CLIENT_PREFIX)}

    # ---------------------------------------------------------------- advance an open flow

    def _advance(self, doc: dict, inbound: Inbound) -> dict:
        if inbound.message_id in doc["handled"]:
            return {"duplicate": True, "message_id": inbound.message_id}
        doc["updated_at"], doc["last_ts"], doc["timer_reasked"] = _iso(self.h.clock()), inbound.ts.isoformat(), False
        res = (self._on_company if doc["step"] == "awaiting_company" else self._on_type)(doc, inbound)
        cur = self.h.store.get(NS, doc["wa_id"]) or doc
        cur["handled"] = list(cur.get("handled", [])) + [inbound.message_id]
        self._save(cur)
        return res

    def _append(self, doc: dict, inbound: Inbound) -> None:
        doc["context"].append(inbound.to_dict())

    @staticmethod
    def _is_media(inbound: Inbound) -> bool:
        return inbound.kind in ("voice", "media") and not inbound.button_id

    def _on_media(self, doc: dict, inbound: Inbound) -> dict:
        """A voice note or a photo while a question is open: KEPT for the ticket. It is not a wrong answer and spends no
        re-ask; the question is repeated at most once per step."""
        self._append(doc, inbound)
        if not doc["media_prompted"]:
            doc["media_prompted"] = True
            self._save(doc)
            self._send_step_reask(doc, inbound)
        self._save(doc)
        return {"intake": "media_kept"}

    def _send_step_reask(self, doc: dict, inbound: Inbound) -> None:
        if doc["step"] == "awaiting_company":
            self._send(inbound, "company_reask", "reask")
        else:
            self._send_text(inbound, intake_buttons.reask_text(persona.reply_lang(inbound.lang), 1,
                                                               persona.client_languages()), "type_reask")

    def _reask(self, doc: dict, inbound: Inbound) -> bool:
        """One bounded re-ask. False = the bound is used up (the caller moves on and the owner is alerted)."""
        if doc["reasks"] >= MAX_REASKS:
            return False
        doc["reasks"] += 1
        self._save(doc)
        self._send_step_reask(doc, inbound)
        return True

    def _on_company(self, doc: dict, inbound: Inbound) -> dict:
        if self._is_media(inbound):
            return self._on_media(doc, inbound)
        said = (inbound.text or "").strip()
        if not _is_answer(said):                              # a greeting or the request itself: kept, then ask again
            self._append(doc, inbound)
            if self._reask(doc, inbound):
                return {"intake": "company_reask"}
            return self._pending_company(doc, inbound, said="", why="no usable company answer after the re-ask")
        match = self.match_company(said)
        if match:
            doc["company_id"], doc["company_name"], doc["company_said"] = match["id"], match["name"], said
            return self._after_company(doc, inbound)
        return self._pending_company(doc, inbound, said=said, why="no single exact company with that name in Odoo")

    def _pending_company(self, doc: dict, inbound: Inbound, *, said: str, why: str) -> dict:
        doc.update(pending_company=True, company_said=said)
        self.h._hold("intake_pending_company", inbound=inbound,
                     detail=f"company not confirmed ({why}); the contact is saved as Pending company. Please confirm it in Odoo")
        return self._after_company(doc, inbound)

    # ---------------------------------------------------------------- company -> contact -> type

    def _after_company(self, doc: dict, inbound: Inbound) -> dict:
        if not self._ensure_contact(doc, inbound):
            return {"held": "intake_refused"}
        doc.update(step="awaiting_type", reasks=0, media_prompted=False)
        self._save(doc)
        self._send_buttons(inbound)
        return {"intake": "awaiting_type"}

    def _ensure_contact(self, doc: dict, inbound: Inbound) -> bool:
        """True = go on. A refused save never loops: with a known partner the ticket still opens as Pending company; with
        none it stops here (a human has it, one alert, no client message)."""
        try:
            self._save_contact(doc, inbound)
            return True
        except guard.WriteRefused as e:
            self.h._hold("intake_contact_refused", inbound=inbound, detail=f"the contact could not be saved: {e}")
        if doc["partner_id"]:
            doc["pending_company"] = True
            return True
        doc.update(step="refused", updated_at=_iso(self.h.clock()))
        self._save(doc)
        return False

    def _save_contact(self, doc: dict, inbound: Inbound) -> None:
        """ALWAYS save the sender (guard purpose ``client_contact``) so the next message is recognised. A test sender and a
        pending company are saved with NO company."""
        gw, wa = self.h.odoo, inbound.wa_id
        link = bool(doc["company_id"]) and not doc["is_test"]
        if doc["company_id"] and doc["is_test"]:
            events.event("contact_link_skipped", reason="test_client")      # test data is never tied to a real company
        if not doc["partner_id"]:
            cat = self.h.cfg.get("pending_company_category_id") if doc["pending_company"] else None
            name = (str(doc["first"].get("name") or "").strip() or f"WhatsApp {wa}")[:80]
            doc["partner_id"] = gw.create_client_contact(name, "+" + _digits(wa), doc["company_id"] if link else None, cat)
        elif link and not doc["partner_is_company"]:
            info = self._contact(doc["partner_id"])
            if info is None:
                raise guard.WriteRefused("the contact can no longer be read")
            if not info["parent_id"]:
                gw.link_client_contact(doc["partner_id"], doc["company_id"])
        self.h.store.put(CONTACTS_NS, wa, {"partner_id": doc["partner_id"], "at": _iso(self.h.clock())})

    def _on_type(self, doc: dict, inbound: Inbound) -> dict:
        if self._is_media(inbound):
            return self._on_media(doc, inbound)
        event = ({"interactive": {"button_reply": {"id": inbound.button_id, "title": inbound.text or ""}}}
                 if inbound.button_id else inbound.text)
        choice = intake_buttons.parse_type_reply(event)
        if choice in TYPE_FIELDS:
            doc["type"] = choice
            return self._open(doc, inbound)
        self._append(doc, inbound)                             # an unknown button id is just an unrecognised answer
        if doc["reasks"] < MAX_REASKS:
            doc["reasks"] += 1
            self._save(doc)
            self._send_text(inbound, intake_buttons.reask_text(persona.reply_lang(inbound.lang), doc["reasks"],
                                                               persona.client_languages()), "type_reask")
            return {"intake": "type_reask"}
        doc.update(type="question", defaulted=True)
        self.h._hold("intake_type_defaulted", inbound=inbound,
                     detail="the client did not pick a request type after the re-ask; opened as Question, please check")
        return self._open(doc, inbound)

    # ---------------------------------------------------------------- open the ticket

    def _open(self, doc: dict, inbound: Inbound) -> dict:
        h = self.h
        first = Inbound.from_dict(doc["first"])
        existing = h._ticket_by_source(first.message_id)
        if existing is not None:                               # a retry: resume, never a second ticket
            res = h._continue_first_contact(existing, [])
        else:
            res = h._first_contact(first, intake=doc)
        slim = {k: doc[k] for k in ("wa_id", "partner_id", "handled", "last_ts")}   # no client text kept after the ticket
        self._save({**slim, "step": "done", "updated_at": _iso(h.clock())})
        return res

    # ---------------------------------------------------------------- the company directory (read-only)

    def match_company(self, said: str) -> dict | None:
        """The ONE Odoo company whose whole name equals the answer (case-insensitive), else None. A near name, a partial
        name or several companies with the same name are NOT a match: the owner decides (Pending company)."""
        for cand in _company_candidates(said):
            rows = self.h.odoo.search_read("res.partner", [["is_company", "=", True], ["name", "=ilike", _escape_like(cand)]],
                                           ["name"], limit=3)
            if len(rows) == 1:
                return {"id": int(rows[0]["id"]), "name": str(rows[0]["name"])}
            if rows:
                return None
        return None

    # ---------------------------------------------------------------- sending (intake messages, 24h window only)

    def _save(self, doc: dict) -> None:
        self.h.store.put(NS, doc["wa_id"], doc)

    @staticmethod
    def _lint_ok(text: str, lang: str) -> bool:
        """Fail CLOSED: any finding, an error, or a result that is not a list blocks the message."""
        try:
            findings = lint(text, client_text="", lang=lang)
        except Exception:                                      # noqa: BLE001
            return False
        return isinstance(findings, list) and not findings

    def _send(self, inbound: Inbound, key: str, kind: str) -> None:
        self._send_text(inbound, persona.text(key, inbound.lang), kind)

    def _send_text(self, inbound: Inbound, text: str, kind: str) -> None:
        h, now = self.h, self.h.clock()
        if not self._lint_ok(text, persona.reply_lang(inbound.lang)):
            self._blocked(inbound, kind, "the wording check refused this message")
            return
        try:
            sent = h.whatsapp.send_text(inbound.wa_id, text, last_inbound_at=min(inbound.ts, now), now=now,
                                        automatic=True, intake=True)
        except whatsapp_cloud.WhatsAppRefused as e:           # window closed, sending off, STOP file, staff, not allowed
            self._blocked(inbound, kind, self._why(e))
            return
        events.record_send(h.store, sent, tid=0, kind="intake", msg_kind=kind, source_mid=inbound.message_id, now=now)

    def _send_buttons(self, inbound: Inbound) -> None:
        h, now = self.h, self.h.clock()
        payload = intake_buttons.build_type_buttons(persona.reply_lang(inbound.lang), persona.client_languages())["interactive"]
        if not self._lint_ok(str((payload.get("body") or {}).get("text", "")), persona.reply_lang(inbound.lang)):
            self._blocked(inbound, "type_buttons", "the wording check refused this message")
            return
        try:
            sent = h.whatsapp.send_interactive(inbound.wa_id, payload, last_inbound_at=min(inbound.ts, now), now=now,
                                               automatic=True, intake=True)
        except whatsapp_cloud.WhatsAppRefused as e:
            self._blocked(inbound, "type_buttons", self._why(e))
            return
        events.record_send(h.store, sent, tid=0, kind="intake", msg_kind="type_buttons", source_mid=inbound.message_id, now=now)

    @staticmethod
    def _why(e: Exception) -> str:
        return ("the client's 24h window is closed" if isinstance(e, whatsapp_cloud.WindowClosed)
                else "the sender refused it (sending is off or not allowed)")

    def confirmation(self, state) -> str | None:
        """The ONE message after the ticket exists. None = the wording check refused it (the owner is told)."""
        from .templates import _clean_name
        name = _clean_name(state.inbound.name or "")
        for subject in (state.intake_subject, "your request"):   # the client's own words may not pass the check: then neutral
            if name == "there":                                # no usable profile name: no "Thank you, there."
                text = persona.text("confirmation_anon", state.lang, n=state.tid, subject=subject)
            else:
                text = persona.text("confirmation", state.lang, name=name, n=state.tid, subject=subject)
            if self._lint_ok(text, persona.reply_lang(state.lang)):
                return text
        self._blocked(state.inbound, "confirmation", "the wording check refused it")
        return None

    def _blocked(self, inbound: Inbound, kind: str, why: str) -> None:
        self.h._hold("intake_send_blocked", inbound=inbound, detail=f"intake message '{kind}' not sent: {why}")
