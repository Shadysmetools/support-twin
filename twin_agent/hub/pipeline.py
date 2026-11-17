"""The Hub — the 11-stage support pipeline orchestrator (plan §4, baton + coordinator stage 11).

  1 RECEIVE -> 2 STT -> 3 ANALYSE -> 4 CREATE -> 5 ASSIGN -> 6 TELEGRAM -> 7 ACK -> 8 CONFIRM (owner approval, async)
  -> customer reply (attached, never auto-advances) -> owner marks confirmed -> fix -> owner taps fix-ready
  -> 9 PROVE-IT -> 10 RESOLVE (owner approval, async) -> 11 METRICS-KPIs -> (close -> §15a learning)

The owner side (approvals, resolve routing, holds) lives in ``owner_flow.py``. State is per TICKET and durable
(``store.py``); a message is marked DONE only after it was processed, a failed one stays retryable, and the first
contact saves the ticket right after CREATE so a retry resumes instead of creating an orphan twin.

Gates and safety:
  - The ACK (Template 1) is the ONLY automatic client message, at most one per ticket. CONFIRM and RESOLVE are
    owner-approved first (bound approvals), and never sent outside the 24h window (checked against the real now).
  - The §16 Judge scores each client-reply DRAFT before the owner sees it; a blocked/failed judge means UNJUDGED.
  - Odoo writes stay inside ``helpdesk/guard.py`` with mail suppressed on create (``GuardedOdoo``).
  - An unknown sender (no res.partner) is HELD for a human (triage topic + hold queue): no ticket, no message, and
    no audio sent to STT (A4).
  - A PER-SENDER lock serialises everything that touches one customer's tickets (webhooks, owner taps, PROVE-IT), so
    a text + voice note a second apart make ONE ticket while other customers are never blocked.

DEPLOY CONSTRAINT: ONE hub process per SQLite file. The locks are in-process; a second process (a separate
scheduler, a blue/green twin) could race the check-then-create and send two acks. Run ``python -m
twin_agent.hub.tick`` only as the SAME process' timer thread or accept that constraint explicitly.
"""
from __future__ import annotations

import hashlib
import html
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

from ..channels import whatsapp_cloud
from ..config import TEAM_BUSES
from . import analyse as analyse_mod
from . import intake_buttons, templates, ticket_text
from .fakes import merge_context
from .intake_flow import TYPE_FIELDS, IntakeFlow
from .model import Analysis, Inbound, Owner, TicketState
from .odoo_people import ActorRefused
from . import events
from .odoo_gateway import GuardedOdoo, mail_suppress_context, team_id_for, x_track_value, x_type_id
from .owner_flow import OwnerFlow
from .store import MemoryStore
from .team_bus import TelegramTeamBus

log = logging.getLogger("twin_agent.hub")
_MEDIA_LABEL = {"image": "Photo", "video": "Video", "document": "Document", "voice": "Voice note"}

OPEN_STAGES = {"new", "awaiting_owner", "awaiting_customer", "awaiting_owner_advance", "in_progress",
               "awaiting_owner_resolve"}


ACK_DONE = ("7-ACK", "7-ACK(skipped)", "7-ACK(unknown)", "7-ACK(fallback)")


def _ref(message_id: str) -> str:
    """A short hash for logs and triage: a wamid encodes the sender's phone number, so it is never printed."""
    return hashlib.sha256(str(message_id).encode("utf-8")).hexdigest()[:8]


class Hub(OwnerFlow):
    """One hub instance owns the pipeline across webhooks. Inject every seam; the fakes make it CI-safe."""

    def __init__(self, *, odoo: GuardedOdoo, whatsapp, stt, telegram, knowledge_odoo, knowledge_wiki,
                 assigner, proof, metrics_connector, judge, learning, cfg: dict | None = None, store=None,
                 clock=None, team_bus=None, ticket_writer=None):
        self.odoo = odoo
        self.whatsapp = whatsapp
        self.stt = stt
        self.telegram = telegram                     # the APPROVAL gateway (always Telegram), never the team bus
        self.k_odoo = knowledge_odoo
        self.k_wiki = knowledge_wiki
        self.assigner = assigner
        self.proof = proof
        self.metrics = metrics_connector
        self.judge = judge
        self.learning = learning
        self.ticket_writer = ticket_writer           # optional LLM words for the Odoo ticket (None = deterministic)
        self.cfg = cfg or {}
        if self.cfg.get("client_languages"):                 # config flag: English only unless "ar" is added (needs native review)
            from . import persona
            persona.set_client_languages(self.cfg["client_languages"])
        self.team_bus = self._pick_team_bus(team_bus, telegram)
        self.conv = odoo.conv
        if store is None and self.cfg.get("run_mode") not in ("test", "dry_run"):
            raise ValueError("Hub needs an explicit store (SqliteStore); MemoryStore only when run_mode is test/dry_run")
        self.store = store if store is not None else MemoryStore()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        lock = getattr(self.store, "lock_exclusive", None)
        if lock:
            lock()                                   # one hub process per SQLite file (refuses a second one)
        self.intake = IntakeFlow(self)               # intake v2 (flag ``intake_v2``, off by default)
        if hasattr(whatsapp, "intake_sender"):
            whatsapp.intake_sender = self.intake.has_flow
        self._locks: dict[str, threading.RLock] = {}
        self._fb_neg: dict[str, datetime] = {}      # message id -> when it was last found NOT a fallback candidate
        self._tick_err: dict[str, datetime] = {}
        self._locks_guard = threading.Lock()
        safe = set(self.conv.safe_stages or set())
        self.working_stage = 14 if 14 in safe else (sorted(safe)[0] if safe else 14)

    def _pick_team_bus(self, team_bus, telegram):
        """``team_bus`` in cfg names the channel; teamhub must be INJECTED (``team_bus.resolve_team_bus``), and a
        mismatch is an error: never a silent fallback to Telegram."""
        mode = self.cfg.get("team_bus", "telegram")
        if mode not in TEAM_BUSES:
            raise ValueError(f"team_bus must be one of {TEAM_BUSES}, got {mode!r}")
        if team_bus is None:
            if mode != "telegram":
                raise ValueError(f"team_bus {mode!r} needs an injected bus (team_bus.resolve_team_bus)")
            return TelegramTeamBus(telegram)
        if team_bus.name != mode:
            raise ValueError(f"team_bus is {mode!r} in the config but the injected bus is {team_bus.name!r}")
        return team_bus

    def _wa_lock(self, wa_id: str) -> threading.RLock:
        with self._locks_guard:
            return self._locks.setdefault(str(wa_id), threading.RLock())

    # ---------------------------------------------------------------- ticket access

    def _save(self, state: TicketState) -> None:
        self.store.put("tickets", state.tid, state.to_dict())

    def ticket(self, tid: int) -> TicketState | None:
        d = self.store.get("tickets", tid)
        return TicketState.from_dict(d) if d else None

    def all_tickets(self) -> list[TicketState]:
        return [TicketState.from_dict(d) for d in self.store.scan("tickets")]

    def tickets_for(self, wa_id: str) -> list[TicketState]:
        return [t for t in self.all_tickets() if t.wa_id == str(wa_id)]

    def latest_ticket(self, wa_id: str) -> TicketState | None:
        mine = self.tickets_for(wa_id)
        return mine[-1] if mine else None

    def _ticket_by_source(self, message_id: str) -> TicketState | None:
        for t in self.all_tickets():
            if t.source_message_id == message_id:
                return t
        return None

    # ---------------------------------------------------------------- entry

    def handle_webhook(self, payload: dict) -> dict:
        from .webhook import parse_all
        inbounds = parse_all(payload, phone_number_id=str(self.cfg.get("phone_number_id") or ""))
        self._ingest_statuses(payload)
        if not inbounds:
            events.event("inbound_dropped", reason="status_callback" if events.has_statuses(payload) else "no_message_for_hub_number")
            return {"ignored": "no inbound message for this hub number (status callback, other number or "
                               "unsupported type)"}
        results = [self._handle_one(i) for i in inbounds]
        return results[0] if len(results) == 1 else {"batch": results}

    def _ingest_statuses(self, payload: dict) -> int:
        """Meta's delivery statuses (sent / delivered / read / failed) for OUR number are stored against the send they
        belong to. Idempotent (a status never moves a message backwards), so the 200-path and the worker may both call it."""
        try:
            return events.ingest_statuses(self.store, payload, str(self.cfg.get("phone_number_id") or ""), self.clock())
        except Exception as e:                                  # noqa: BLE001 - a bad status must never lose a message
            log.error("delivery status not stored: %s", type(e).__name__)
            return 0

    def accept(self, payload: dict) -> int:
        """Persist every inbound message as ``queued`` BEFORE the webhook answers 200 (Meta won't resend after a 200),
        so a crash before processing loses nothing: ``retry_failed`` drains what is left. Returns how many NEW."""
        from .webhook import parse_all
        self._ingest_statuses(payload)
        n = 0                           # NO hub lock: put_if_absent is atomic, and the lock is held across slow STT/PROVE-IT
        for i in parse_all(payload, phone_number_id=str(self.cfg.get("phone_number_id") or "")):
            i.message_id = i.message_id or f"nomid-{id(i)}-{self.clock().timestamp()}"
            n += bool(self.store.put_if_absent("messages", i.message_id, {
                "status": "queued", "since": self.clock().isoformat(), "attempts": 0, "inbound": i.to_dict(),
                "error": "", "received_at": self.clock().isoformat()}))
        return n

    BACKOFF_S = (30, 120, 600, 3600, 21600)     # 30s, 2m, 10m, 1h, 6h, then the message goes DEAD (+ triage)

    def _handle_one(self, inbound: Inbound) -> dict:
        """Claim -> route -> mark. A message is DONE only after it was processed; an exception leaves it FAILED with a
        backoff (``tick`` re-drives it), tells a human once, and never loses it."""
        mid = inbound.message_id or f"nomid-{id(inbound)}-{self.clock().timestamp()}"
        inbound.message_id = mid
        with self._wa_lock(inbound.wa_id):
            if not self._claim(inbound):
                events.event("inbound_dropped", reason="duplicate", wamid=events.wamid_tail(mid))
                return {"duplicate": True, "message_id": mid}      # idempotency (A2)
            events.event("inbound_received", kind=inbound.kind, wamid=events.wamid_tail(mid))
            try:
                res = self._route(inbound)
            except Exception as e:                                 # noqa: BLE001
                log.error("message %s failed: %s", _ref(mid), type(e).__name__)
                self._failed(mid, type(e).__name__)
                return {"error": type(e).__name__, "retryable": True, "message_id": mid}
            self._finish(mid, "held" if res.get("held") else "done")
            self._close_holds(message_id=mid, reason="processing_failed")
            return res

    def _failed(self, mid: str, err: str) -> None:
        now = self.clock()
        doc = self.store.get("messages", mid) or {}
        n = int(doc.get("attempts", 1))
        src = self._ticket_by_source(mid)
        tid = src.tid if src else 0
        if n > len(self.BACKOFF_S):
            self._finish(mid, "dead", err)
            self._hold("processing_dead", ticket_id=tid, message_id=mid,
                       detail=f"message dead after {n} attempts ({err}); needs a human")
            return
        self._finish(mid, "failed", err, next_at=(now + timedelta(seconds=self.BACKOFF_S[n - 1])).isoformat())
        if n == 1 or not any(h["reason"] == "processing_failed" and h.get("message_id") == mid for h in self.holds()):
            self._hold("processing_failed", ticket_id=tid, message_id=mid,
                       detail=f"processing error {err}; automatic retry is scheduled")

    def _claim(self, inbound: Inbound) -> bool:
        now = self.clock()
        doc = {"status": "processing", "since": now.isoformat(), "attempts": 1, "inbound": inbound.to_dict(),
               "error": "", "received_at": now.isoformat()}
        if self.store.put_if_absent("messages", inbound.message_id, doc):
            return True
        took: list[int] = []
        timeout = float(self.cfg.get("processing_timeout_s", 300))

        def fn(d: dict) -> dict:
            stale = d["status"] == "processing" and now - datetime.fromisoformat(d["since"]) > timedelta(seconds=timeout)
            if d["status"] in ("failed", "queued") or stale:
                d.update(status="processing", since=now.isoformat(), attempts=d["attempts"] + 1)
                took.append(1)
            return d

        self.store.update("messages", inbound.message_id, fn)
        return bool(took)

    def _finish(self, mid: str, status: str, error: str = "", next_at: str = "") -> None:
        self.store.update("messages", mid, lambda d: {**d, "status": status, "error": error, "next_at": next_at})

    def retry_failed(self, *, due_only: bool = False, now: datetime | None = None) -> list[dict]:
        """Re-drive every FAILED / queued / stale in-flight message once (``dead`` ones are left for a human).
        ``due_only`` honours each message's backoff (``tick``); a manual call retries immediately."""
        now = now or self.clock()
        out, blocked = [], set()
        for doc in self.store.scan("messages"):
            if doc.get("status") not in ("failed", "queued", "processing"):
                continue
            if doc.get("status") == "processing" and now - datetime.fromisoformat(doc["since"]) <= timedelta(
                    seconds=float(self.cfg.get("processing_timeout_s", 300))):
                blocked.add(doc["inbound"]["wa_id"])          # in flight on a worker: its sender's next message waits
                continue
            if due_only and doc.get("next_at") and datetime.fromisoformat(doc["next_at"]) > now:
                blocked.add(doc["inbound"]["wa_id"])          # an earlier message of this sender is still waiting
                continue
            if doc["inbound"]["wa_id"] in blocked:            # keep each sender's order: later ones wait for it
                continue
            lock = self._wa_lock(doc["inbound"]["wa_id"])
            if not lock.acquire(blocking=False):              # a busy sender: skip until the next tick, never wait
                blocked.add(doc["inbound"]["wa_id"])
                continue
            try:
                res = self._handle_one(Inbound.from_dict(doc["inbound"]))
            finally:
                lock.release()
            if res.get("error"):
                blocked.add(doc["inbound"]["wa_id"])
            out.append(res)
        return [r for r in out if not r.get("duplicate")]

    def _tick_pass(self, name: str, fn, default):
        """Each tick pass is isolated: one failing pass (e.g. Odoo down in the fallback's partner lookup) is logged and
        reported to triage (at most every 10 minutes per pass), and never stops the others."""
        try:
            return fn()
        except Exception as e:                                 # noqa: BLE001
            log.error("tick pass %s failed: %s", name, type(e).__name__)
            now = self.clock()
            last = self._tick_err.get(name)
            if last is None or now - last > timedelta(minutes=10):
                self._tick_err[name] = now
                self._triage_static(f"tick pass {name} failed: {type(e).__name__}; the other passes still ran")
            return default

    def tick(self, now: datetime | None = None) -> dict:
        """The scheduler calls this every minute: the fallback ack, retries with backoff, approval expiry (+ triage)
        and re-asks of INTERNAL owner steps a ticket lost. Client-facing drafts are never re-asked on a timer. The tick
        never waits on a busy sender's lock (it skips that sender until the next tick)."""
        self._tick_pass("intake", lambda: self.intake.timer_pass(now or self.clock()), 0)
        fallback = self._tick_pass("fallback", lambda: self._fallback_pass(now or self.clock()), 0)
        retried = self._tick_pass("retry", lambda: len(self.retry_failed(due_only=True, now=now)), 0)
        expired, reasked = self._tick_pass("expiry", self._expiry_pass, (0, 0))
        out = {"retried": retried, "expired": expired, "reasked": reasked, "fallback_acks": fallback}
        if self._email_channel():                            # the client's email replies join the loop
            out["email_replies"] = self._tick_pass("email_replies", self._email_replies_pass, 0)
        if getattr(self.odoo, "acting", False):             # per-person Odoo: replay notes that waited for a key
            out["notes_replayed"] = self._tick_pass("notes", lambda: self.odoo.replay_notes(self._current_owner_uid), 0)
        return out

    def _expiry_pass(self) -> tuple[int, int]:
        expired = reasked = 0
        for t in self.all_tickets():
            if t.stage in ("resolved", "new", "awaiting_customer"):
                continue
            lock = self._wa_lock(t.wa_id)
            if not lock.acquire(blocking=False):
                continue
            try:
                st = self.ticket(t.tid)
                if st is None:
                    continue
                if st.pending_nonce and not self._live_pending(st):
                    doc = self.store.get("approvals", st.pending_nonce) or {}
                    if not doc.get("used_at"):
                        self.store.update("approvals", st.pending_nonce, lambda d: {**d, "used_at": "expired"})
                        self._triage_static(f"approval expired: ticket={st.tid} kind={st.pending_kind}: use re-ask")
                        expired += 1
                    st.pending_nonce, st.pending_kind = "", ""
                    self._save(st)
                try:
                    reasked += bool(self._ensure_pending(st, internal_only=True))
                except Exception as e:                             # noqa: BLE001
                    log.error("tick re-ask failed on ticket %s: %s", st.tid, type(e).__name__)
            finally:
                lock.release()
        return expired, reasked

    # ---------------------------------------------------------------- the 60s fallback ack

    # The automatic-message rule is per SENDER EPISODE, not per message: ``fcclaim/<wa_id>`` holds the ONE automatic
    # message (fallback or normal) for a sender's first-contact episode. It is claimed atomically, bound to the ticket
    # the first-contact flow creates, and closed when that ticket is resolved (or, if no ticket ever appears, after the
    # 24h window). A later fallback or normal ack for that sender is refused while the claim is open.

    def _msg_time(self, mid: str) -> datetime | None:
        """When the hub accepted this message (None if unknown)."""
        doc = self.store.get("messages", mid) if mid else None
        raw = (doc or {}).get("received_at") or (doc or {}).get("since")
        return datetime.fromisoformat(raw) if raw else None

    def _claim_closed(self, cur: dict, now: datetime, mid: str = "") -> bool:
        if cur.get("tid"):
            t = self.ticket(cur["tid"])
            if t is not None:
                return t.stage == "resolved"
            # the bound ticket is GONE (store loss): fail closed. The claim is kept (outcome unknown) and, like an
            # unbound one, only ages out after the 24h window, so it can never block the sender forever.
        end = datetime.fromisoformat(cur["at"]) + timedelta(hours=float(self.cfg.get("window_hours", 24)))
        if now <= end:
            return False
        # Aged out. A message that arrived INSIDE the old episode (stuck, queued or failed since) belongs to it and can
        # never trigger an ack now: only a NEW inbound message after the age-out opens a new first contact.
        arrived = self._msg_time(mid)
        return not (arrived is not None and arrived <= end)

    def _claim_superseded(self, cur: dict, mid: str, tid: int) -> bool:
        """The open claim does not own THIS message's automatic reply: an unbound fallback that was SKIPPED (nothing
        went out) for a DIFFERENT (older) message, or a claim bound to another partner's ticket (a reassigned number).
        An ``error`` claim is NEVER superseded: the send may have been delivered (a read timeout after Meta accepted
        it), so it stays "maybe sent" until the window closes and the next ticket reports an unknown outcome."""
        if cur.get("mid") == mid:
            return False
        if not cur.get("tid"):
            return cur.get("state") == "skipped"
        if tid:
            a, b = self.ticket(cur["tid"]), self.ticket(tid)
            return bool(a and b and int(a.partner_id) != int(b.partner_id))
        return False

    def _episode_open(self, wa_id: str, mid: str = "") -> bool:
        cur = self.store.get("fcclaim", wa_id)
        return bool(cur) and not (self._claim_closed(cur, self.clock(), mid) or self._claim_superseded(cur, mid, 0))

    def _claim_auto(self, wa_id: str, mid: str, kind: str, tid: int = 0) -> bool:
        now = self.clock()
        doc = {"mid": mid, "kind": kind, "state": "attempting", "tid": tid, "at": now.isoformat()}
        if self.store.put_if_absent("fcclaim", wa_id, doc):
            return True
        cur = self.store.get("fcclaim", wa_id)
        if not cur or not (self._claim_closed(cur, now, mid) or self._claim_superseded(cur, mid, tid)):
            return False
        won: list[int] = []

        def fn(d: dict) -> dict:                               # the previous episode is over: take it over atomically
            if d.get("at") == cur["at"] and d.get("mid") == cur["mid"]:
                won.append(1)
                return doc
            return d

        self.store.update("fcclaim", wa_id, fn)
        if won and cur.get("tid") and self.ticket(cur["tid"]) is None:
            self._triage_static(f"the automatic-message claim was bound to ticket {cur['tid']}, which no longer exists: "
                         "closed, so the sender's next ticket can get its own ack")
        return bool(won)

    def _claim_set(self, wa_id: str, mid: str, **fields) -> None:
        self.store.update("fcclaim", wa_id, lambda d: {**d, **fields} if d.get("mid") == mid else d)

    def _fallback_candidate(self, inbound: Inbound) -> bool:
        """Would this message open (or has it opened) a NEW ticket whose normal ack has not gone out? A message that
        attaches to an open ticket, or comes from an unknown sender, never gets an automatic reply."""
        if self.intake.has_flow(inbound.wa_id):               # the intake questions ARE the reply: no extra fallback ack
            return False
        partner = self.k_odoo.partner_for(inbound.wa_id)
        if not partner:
            return False
        src = self._ticket_by_source(inbound.message_id)
        if src is not None:
            return not any(x in src.history for x in ACK_DONE)
        latest = self.latest_ticket(inbound.wa_id)
        if latest is None or self._other_partner(latest, partner):
            return True
        return latest.stage == "resolved" and (latest.keep_closed or not self._within_reopen(latest))

    def _fallback_pass(self, now: datetime) -> int:
        secs = self.cfg.get("fallback_ack_seconds", 60)
        if not secs:                                           # 0 / None disables the fallback (old behaviour)
            return 0
        oldest: dict[str, dict] = {}                           # only a sender's OLDEST unfinished message counts
        for doc in self.store.scan("messages"):
            if doc.get("status") not in ("queued", "failed", "processing"):
                continue
            wa, ts = doc["inbound"]["wa_id"], doc.get("received_at") or doc["since"]
            if wa not in oldest or ts < (oldest[wa].get("received_at") or oldest[wa]["since"]):
                oldest[wa] = doc
        sent = 0
        recheck = float(self.cfg.get("fallback_recheck_seconds", 300))
        self._fb_neg = {m: t for m, t in self._fb_neg.items() if m in {d["inbound"]["message_id"] for d in oldest.values()}}
        for wa, doc in oldest.items():
            try:
                since = datetime.fromisoformat(doc.get("received_at") or doc["since"])
                mid = doc["inbound"]["message_id"]
                if (now - since).total_seconds() < float(secs) or self._episode_open(wa, mid):
                    continue
                seen = self._fb_neg.get(mid)                   # tick cost: a message that is NOT a candidate is not
                if seen is not None and (now - seen).total_seconds() < recheck:    # re-checked (Odoo + ticket scan)
                    continue                                                       # every minute; a due one never waits
                inbound = Inbound.from_dict(doc["inbound"])
                if self._fallback_candidate(inbound):
                    self._fb_neg.pop(mid, None)
                    sent += self._send_fallback(inbound, now)
                else:
                    self._fb_neg[mid] = now
            except Exception as e:                             # noqa: BLE001 - one bad message never stops the pass
                log.error("fallback check failed: %s", type(e).__name__)
                self._tick_pass("fallback", lambda e=e: (_ for _ in ()).throw(e), 0)
        return sent

    def _send_fallback(self, inbound: Inbound, now: datetime) -> int:
        """ONE templated ack with no ticket number, claimed per sender episode. A crash leaves ``attempting`` (never
        re-sent); a raised send error is recorded as ``error`` so the normal path reports it as UNKNOWN."""
        wa, mid = inbound.wa_id, inbound.message_id
        if not self._claim_auto(wa, mid, "fallback", 0):
            return 0
        last = min(inbound.ts, now)
        if not whatsapp_cloud.in_service_window(last, now, float(self.cfg.get("window_hours", 24))):
            self._claim_set(wa, mid, state="skipped")
            return 0
        lang = analyse_mod.detect_lang(inbound.text) if inbound.text else inbound.lang
        text = templates.fallback_ack(lang, name=inbound.name or "there")
        try:
            sent = self.whatsapp.send_text(wa, text, last_inbound_at=last, now=now, automatic=True)
        except whatsapp_cloud.WindowClosed:
            self._claim_set(wa, mid, state="skipped")
            return 0
        except Exception as e:                                 # noqa: BLE001
            log.error("fallback ack failed for %s: %s", _ref(mid), type(e).__name__)
            self._claim_set(wa, mid, state="error")
            self._triage_static(f"fallback ack send FAILED (message ref {_ref(mid)}): outcome unknown, the client may or may not "
                         "have been acknowledged. Check WhatsApp (it is NOT retried).")
            return 0
        self._claim_set(wa, mid, state="sent")
        tail = events.record_send(self.store, sent, tid=0, kind="ack", msg_kind="fallback", source_mid=mid, now=now)
        events.event("ack_sent", tid="-", kind="fallback", wamid=tail)
        return 1

    # ---------------------------------------------------------------- routing: per TICKET, not per phone

    def _route(self, inbound: Inbound) -> dict:
        resume = self._ticket_by_source(inbound.message_id)
        if resume is not None:       # reprocessing the message that OPENED a ticket == its first contact failed midway
            return self._continue_first_contact(resume, [])      # (idempotent: every stage is guarded by history)
        state = self.latest_ticket(inbound.wa_id)
        if inbound.button_id and not self.intake.has_flow(inbound.wa_id):
            return {"ignored": "stale_button"}                  # a tap on an old button: never a message, never a ticket
        partner = self.k_odoo.partner_for(inbound.wa_id)
        if self.intake.enabled_for(inbound.wa_id) or self.intake.has_flow(inbound.wa_id):
            partner = partner or self.intake.saved_partner(inbound.wa_id) or None   # short-lived, re-verified
            handled = self.intake.route(inbound, partner, state)
            if handled is not None:
                return handled
        if state is None or not partner or self._other_partner(state, partner):
            # first contact: a number with NO partner is held as an unknown sender, and a reassigned number never joins
            # another client's ticket (fail closed; the real partner_for must raise on an Odoo error, not return None)
            return self._first_contact(inbound)
        if state.stage == "resolved":
            if state.keep_closed or not self._within_reopen(state):
                return self._first_contact(inbound)                 # kept closed / too late for a reply: a NEW ticket
            return self._post_resolve(state, inbound)
        return self._attach(state, inbound)

    @staticmethod
    def _other_partner(state: TicketState, partner) -> bool:
        """The number now belongs to a DIFFERENT (known) partner than the ticket's. (No partner at all is handled
        before this, in ``_route``: it never reaches an old ticket.)"""
        return bool(partner and state.partner_id and int(partner) != int(state.partner_id))

    def _link_fresh(self, prev: TicketState) -> bool:
        """Only link a follow-up to a previous ticket resolved within ``follow_up_max_days`` (default 90)."""
        if not prev.resolved_at:
            return True
        age = self.clock() - datetime.fromisoformat(prev.resolved_at)
        return age <= timedelta(days=float(self.cfg.get("follow_up_max_days") or 90))

    def _within_reopen(self, state: TicketState) -> bool:
        if not state.resolved_at:
            return False
        age = self.clock() - datetime.fromisoformat(state.resolved_at)
        return age <= timedelta(days=float(self.cfg.get("reopen_days", 7)))

    # ---------------------------------------------------------------- stages 1-8 (first contact)

    def _transcribe(self, inbound: Inbound, log_: list) -> bool:
        if inbound.kind == "voice":
            tr = self.stt.transcribe(inbound.audio_id or "", lang_hint=inbound.lang)
            if tr.get("status") == "blocked" or not tr.get("text"):
                log_.append({"stage": "2-STT", "status": "blocked", "why": tr.get("why")})
                return False
            inbound.text = tr["text"]
            inbound.lang = tr.get("lang") or inbound.lang
            inbound.transcript_placeholder = bool(tr.get("placeholder"))
            log_.append({"stage": "2-STT", "status": "ok", "provider": tr.get("provider"),
                         "placeholder": inbound.transcript_placeholder, "audio_retained": tr.get("audio_retained")})
        else:
            inbound.resolve_lang()
            log_.append({"stage": "2-STT", "status": "skipped", "reason": f"{inbound.kind} message"})
        return True

    def _ctx(self, partner_id: int) -> dict:
        return merge_context(self.k_odoo.client_context(partner_id), self.k_wiki.client_context(partner_id))

    def _first_contact(self, inbound: Inbound, *, intake: dict | None = None) -> dict:
        log_: list[dict] = [{"stage": "1-RECEIVE", "status": "ok", "kind": inbound.kind,
                             "message_id": inbound.message_id}]
        # identity FIRST (A4 + data minimisation): a stranger's voice note must never reach the STT provider
        # TODO(PDPL sign-off, OPEN): audio retention is still undefined; a KNOWN sender's voice note does go to Groq
        # (cross-border processor); and the proof share_link carries the client name (kept away from the judge).
        partner_id = intake["partner_id"] if intake else self.k_odoo.partner_for(inbound.wa_id)
        if not partner_id:
            return self._hold("unknown_sender", inbound=inbound, log_=log_,
                              detail="no res.partner for this sender; no ticket, no message, no STT")
        if not self._transcribe(inbound, log_):
            return self._hold("stt_unavailable", inbound=inbound, log_=log_, detail="transcription unavailable")
        said = ""
        if intake:                                         # what the client sent while the questions were open joins it
            inbound.text, said = self._intake_text(inbound, intake)
        if not (inbound.text or "").strip():
            return self._hold("empty_message", inbound=inbound, log_=log_, detail="no text to open a ticket from")

        ctx = self._ctx(partner_id)
        client = str(ctx.get("name") or ctx.get("client") or f"partner:{partner_id}")
        analysis = analyse_mod.analyse(inbound.text or "", lang=inbound.lang, client_context=ctx)
        if intake:
            self._apply_intake_type(analysis, intake)
        jcls = self._judge("classification", {"ticket_type": analysis.ticket_type, "module": analysis.module,
                                              "confidence": analysis.confidence})
        log_.append({"stage": "3-ANALYSE", "status": "ok", "nature": analysis.nature,
                     "ticket_type": analysis.ticket_type, "track": analysis.track, "module": analysis.module,
                     "judge": jcls["aggregate"]})

        prev = self.latest_ticket(inbound.wa_id)          # a new ticket after a closed one FOLLOWS UP that ticket
        previous = prev.tid if prev and int(prev.partner_id) == int(partner_id) and self._link_fresh(prev) else 0   # same client, not stale
        subject = self._intake_subject(inbound, said, analysis, intake) if intake else ""
        tid = self._create(analysis, inbound, int(partner_id), previous, client=client, ctx=ctx, intake=intake,
                           subject=subject)
        log_.append({"stage": "4-CREATE", "status": "ok", "ticket_id": tid, "mail_suppressed": True})
        events.event("ticket_created", tid=tid, kind=inbound.kind)
        state = TicketState(wa_id=inbound.wa_id, inbound=inbound, analysis=analysis, partner_id=int(partner_id),
                            tid=tid, lang=inbound.lang, client=client, source_message_id=inbound.message_id,
                            last_inbound_at=inbound.ts.isoformat(), previous_ticket_id=previous)
        if intake:
            state.last_inbound_at = intake["last_ts"]
            state.intake_subject, state.intake_type = subject, intake["type"]
            state.intake_company_id = int(intake["company_id"] or 0)
        state.history = ["1-RECEIVE", "2-STT", "3-ANALYSE", "4-CREATE"]
        self._save(state)                                           # right after CREATE: no orphan on a later failure
        if intake:
            self._intake_voice_holds(intake, tid)
        return self._continue_first_contact(state, log_)

    @staticmethod
    def _apply_intake_type(analysis: Analysis, intake: dict) -> None:
        """The type the client chose sets the Odoo type and the routing track (an unknown value is a Question)."""
        analysis.ticket_type, analysis.nature, analysis.track = TYPE_FIELDS.get(intake["type"], TYPE_FIELDS["question"])

    @staticmethod
    def _media_ref(item: dict) -> str:
        """A voice note or a file the client sent, as a reference line (the file itself is attached by the media step)."""
        if item.get("kind") == "voice":
            return f"[voice note {item.get('audio_id') or ''}]".replace(" ]", "]")
        return f"[{item.get('media_type') or 'file'} {item.get('media_id') or ''}]".replace(" ]", "]")

    def _intake_text(self, inbound: Inbound, intake: dict) -> tuple[str, str]:
        """(the full text for the ticket, the client's own words only). Photos and voice notes sent during the questions
        are kept as references; they are never dropped."""
        items = [inbound.to_dict()] + list(intake["context"])
        said = [str(i.get("text") or "") for i in items if i.get("text")]
        refs = [self._media_ref(i) for i in items if i.get("kind") == "media" or (i.get("kind") == "voice" and i is not items[0])]
        return "\n".join(said + refs), "\n".join(said)

    def _intake_subject(self, inbound: Inbound, said: str, analysis: Analysis, intake: dict) -> str:
        """The short subject from the client's words; with none (a photo or voice note only) a plain label."""
        subject = re.sub(r"[{}\[\]]", "", intake_buttons.short_subject(said, analysis)).strip() if said.strip() else ""
        if subject:
            return subject
        kind = "voice" if inbound.kind == "voice" else (inbound.media_type or "")
        label = _MEDIA_LABEL.get(kind)
        return f"{label} from client" if label else "Support request"

    def _intake_voice_holds(self, intake: dict, tid: int) -> None:
        """A voice note sent during the questions is not transcribed: a person must listen (one alert per note)."""
        for item in intake["context"]:
            if item.get("kind") == "voice":
                self._hold("voice_note_pending", ticket_id=tid, inbound=Inbound.from_dict(item),
                           detail="voice note sent during the intake questions: a person must listen")

    def _pending_tag(self, intake: dict | None) -> int:
        """The "Pending company" ticket tag by name (read-only). 0 = no such tag: the hold and the description line remain."""
        if not (intake and intake.get("pending_company")):
            return 0
        try:
            rows = self.odoo.search_read("helpdesk.tag", [["name", "=", "Pending company"]], ["id"], limit=1)
        except Exception:                                    # noqa: BLE001 - a missing tag must never stop the ticket
            return 0
        return int(rows[0]["id"]) if rows else 0

    def _intake_description(self, intake: dict) -> str:
        said, name = str(intake.get("company_said") or ""), str(intake.get("company_name") or "")
        bits = []
        if said:
            bits.append(f"Company (client said): {html.escape(said)}")
        if name:
            bits.append(f"Company (matched in Odoo): {html.escape(name)} #{int(intake['company_id'])}")
        if intake.get("pending_company"):
            bits.append("Pending company: the owner must confirm the client's company")
        if intake.get("defaulted"):
            bits.append("Request type defaulted to Question: the client did not choose")
        return "".join(f"<p>{b}</p>" for b in bits)

    def _suggest_owner(self, analysis: Analysis, ctx: dict) -> str:
        """"Name: why" for the ticket description, from a PEEK at the assigner (the real assignment is stage 5, after
        create, unchanged). Any failure just leaves the description's "set at assignment" text."""
        try:
            c = self.assigner.assign(analysis, ctx)
            return f"{c['owner'].name}: {c['rationale']}"
        except Exception:                                    # noqa: BLE001
            return ""

    def _create(self, analysis: Analysis, inbound: Inbound, partner_id: int, previous: int = 0, *,
                client: str = "", ctx: dict | None = None, intake: dict | None = None, subject: str = "") -> int:
        """The title/description are for the Odoo ticket ONLY (they carry the client's name and words): they never
        reach the team bus, which gets structured fields from ``_brief_safe``."""
        title, description = ticket_text.compose(analysis, inbound, client=client, partner_id=partner_id,
                                                 writer=self.ticket_writer, previous=previous,
                                                 suggested=self._suggest_owner(analysis, ctx or {}))
        if intake:                                           # a SHORT subject as the name; the full text stays in the description
            title, description = subject, description + self._intake_description(intake)
        # a TEST sender's ticket stays in team 2 whatever the type (the autopilot close only works there); real clients
        # keep the routing of the type
        track = "functional" if intake and intake.get("is_test") else analysis.track
        vals = {"name": title, "description": description, "team_id": team_id_for(track, self.conv),
                "stage_id": self.working_stage, "partner_id": partner_id, "priority": "1"}
        tag = self.cfg.get("pending_company_tag_id") or self._pending_tag(intake)
        if intake and intake.get("pending_company") and tag:
            vals["tag_ids"] = [(4, int(tag))]
        if self.conv.has("x_track"):
            vals["x_track"] = x_track_value(analysis.track, self.conv)
        tid_type = x_type_id(analysis.ticket_type, self.conv)
        if self.conv.has("x_type") and tid_type:
            vals["x_type"] = tid_type
        if self.conv.has("x_ai_confidence"):
            vals["x_ai_confidence"] = analysis.confidence
        return self.odoo.create_ticket(vals, context=mail_suppress_context())

    def _continue_first_contact(self, state: TicketState, log_: list) -> dict:
        """Stages 5-8, each guarded by ``history`` so a retry resumes exactly where the failure happened."""
        if "5-ASSIGN" not in state.history:
            choice = self.assigner.assign(state.analysis, self._ctx(state.partner_id))
            state.owner = choice["owner"]
            state.rationale = choice["rationale"]
            self._on_assigned(state, choice)
            self.odoo.assign(state.tid, state.owner.uid)          # intake: automation (per-person Odoo) or the one account
            state.history.append("5-ASSIGN")
            self._save(state)
            log_.append({"stage": "5-ASSIGN", "status": "ok", "owner": state.owner.name, "uid": state.owner.uid})
        if not self._thread(state):
            self._ensure_thread(state)
            state.history.append("6-TELEGRAM")
            self._save(state)
            log_.append({"stage": "6-TELEGRAM", "status": "ok", "topic_id": state.topic_id,
                         "thread": state.thread_ref})
        self._ack(state, log_)
        if "8-CONFIRM(drafted)" not in state.history:
            draft = self._confirm_draft(state)
            state.history.append("8-CONFIRM(drafted)")
            state.stage = "awaiting_owner"
            self._request_approval(state, "confirm", draft)          # ASYNC: returns pending; the owner's tap resumes
            log_.append({"stage": "8-CONFIRM", "status": "awaiting_owner", "sent": False})
        return {"ticket_id": state.tid, "owner": state.owner.name, "stage": state.stage, "log": log_,
                "awaiting": "owner approval of the CONFIRM draft (nothing sent to the customer yet)"}

    def _on_assigned(self, state: TicketState, choice: dict) -> None:
        """Hook at the real (not the peek) intake assignment; a subclass may audit a fallback choice."""

    def _ack(self, state: TicketState, log_: list) -> None:
        """Stage 7 — the ONLY automatic client message, at most ONE per ticket, never outside the window. It shares
        the per-sender episode claim with the 60s fallback ack (see ``_claim_auto``): whichever claims first wins and
        the other never sends; this ticket binds to a fallback that already went out (``7-ACK(fallback)``). A claim
        left ``attempting`` by a crashed normal send, a failed fallback send, or the pre-#54 ``7-ACK(attempting)``
        marker is an UNKNOWN outcome: reported to the owner, never re-sent."""
        h = state.history
        if "7-ACK(attempting)" in h:                               # legacy marker from before the claim existed
            h[h.index("7-ACK(attempting)")] = "7-ACK(unknown)"
            self._ack_unknown(state)
            return
        if any(x in h for x in ACK_DONE):
            return
        wa, mid, now = state.wa_id, state.source_message_id, self.clock()
        if not self._claim_auto(wa, mid, "normal", state.tid):
            self._ack_refused(state, wa)
            return
        if not self._window_open(state, now):
            self._claim_set(wa, mid, state="skipped")
            h.append("7-ACK(skipped)")
            events.event("ack_skipped", tid=state.tid, reason="window_closed")
            self._post_static(state, "Ack skipped: the customer's 24h window is already closed.")
            self._save(state)
            log_.append({"stage": "7-ACK", "status": "skipped", "why": "window closed"})
            return
        text = self._ack_text(state)
        if text is None:                                     # the wording check refused it: a human is told, nothing is sent
            self._claim_set(wa, mid, state="skipped")
            h.append("7-ACK(skipped)")
            self._save(state)
            events.event("ack_skipped", tid=state.tid, reason="wording_refused")
            return
        last = min(datetime.fromisoformat(state.last_inbound_at), now)
        try:
            sent = self.whatsapp.send_text(state.wa_id, text, last_inbound_at=last, now=now, automatic=True)
        except whatsapp_cloud.WindowClosed:
            self._claim_set(wa, mid, state="skipped")
            h.append("7-ACK(skipped)")
            self._save(state)
            events.event("ack_skipped", tid=state.tid, reason="window_closed")
            return
        self._claim_set(wa, mid, state="sent")
        h.append("7-ACK")
        state.acked = True
        self._save(state)
        tail = events.record_send(self.store, sent, tid=state.tid, kind="ack", msg_kind="normal", source_mid=mid, now=now)
        events.event("ack_sent", tid=state.tid, kind="normal", wamid=tail)
        self._note(state.tid, "[Hub] Auto-ack sent to client (Template 1).")
        log_.append({"stage": "7-ACK", "status": "sent", "automatic": True})

    def _ack_text(self, state: TicketState) -> str | None:
        """Intake v2: ONE confirmation with the ticket number and the short subject. Otherwise Template 1."""
        if state.intake_subject:
            return self.intake.confirmation(state)
        a = state.analysis
        return templates.ack(state.lang, name=state.inbound.name or "there", n=state.tid,
                             subject=templates.request_subject(state.lang, a.module))

    def _ack_unknown(self, state: TicketState) -> None:
        self._post_static(state, "Ack send outcome unknown (an earlier attempt failed); NOT retried, so the client "
                                 "cannot get two acks. Check WhatsApp manually.")
        self._save(state)

    def _ack_refused(self, state: TicketState, wa: str) -> None:
        """The sender's episode claim is held by someone else: bind to a fallback, or report an unknown outcome."""
        cur = self.store.get("fcclaim", wa) or {}
        h = state.history
        if cur.get("kind") == "fallback" and cur.get("tid") in (0, None, state.tid):
            self._claim_set(wa, cur["mid"], tid=state.tid)         # the ticket now owns the fallback that went out
            if cur.get("state") == "error":
                h.append("7-ACK(unknown)")
                self._ack_unknown(state)
                return
            if cur.get("state") == "skipped":
                h.append("7-ACK(skipped)")
            else:
                h.append("7-ACK(fallback)")
                if cur.get("state") == "sent":
                    state.acked = True
                    events.bind_fallback_ack(self.store, cur["mid"], state.tid)
                    self._note(state.tid, "[Hub] Fallback auto-ack sent to client (no ticket number).")
        elif cur.get("tid") == state.tid and cur.get("state") == "sent":
            h.append("7-ACK")                                      # crash after the send, before the save: it DID go out
            state.acked = True
        elif cur.get("tid") == state.tid:                          # an earlier normal send of this ticket died mid-way
            h.append("7-ACK(unknown)")
            self._ack_unknown(state)
            return
        else:                                                      # another open episode owns the automatic message
            h.append("7-ACK(skipped)")
            why = ("which no longer exists: the claim is KEPT until the 24h window ends (fail closed)"
                   if self.ticket(cur.get("tid") or 0) is None else "which is still open")
            self._triage_static(f"ticket {state.tid}: no ack sent because another episode (ticket {cur.get('tid')}, {why}) "
                         "owns this sender's automatic message; this should not normally happen")
        self._save(state)

    def _brief(self, state: TicketState) -> str:
        ctx = self._ctx(state.partner_id)
        a = state.analysis
        agreed = ", ".join(ctx.get("agreed") or []) or "—"
        commits = ", ".join(ctx.get("open_commitments") or []) or "—"
        collabs = ", ".join(ctx.get("usual_collaborators") or []) or "—"
        linked = f" (linked to #{state.previous_ticket_id})" if state.previous_ticket_id else ""
        return (f"Ticket #{state.tid}{linked} → @{state.owner.name}\n"
                f"{state.rationale}\n"
                f"Request: {a.summary}\n"
                f"Type {a.ticket_type} · track {a.track} · module {a.module}\n"
                f"Wiki: agreed[{agreed}] open[{commits}] usually-with[{collabs}]\n"
                f"Open questions: {'; '.join(a.open_questions) or 'none'}")

    # ---------------------------------------------------------------- customer messages on an existing ticket

    def _reply_text(self, state: TicketState, inbound: Inbound) -> tuple[str, bool]:
        """(text, ok). ``ok`` False = a voice note that could not be transcribed yet (held for a human)."""
        if inbound.kind == "voice":
            tr = self.stt.transcribe(inbound.audio_id or "", lang_hint=inbound.lang)
            if tr.get("status") == "blocked" or not tr.get("text"):
                if not any(h.get("message_id") == inbound.message_id for h in self.holds()):
                    self._hold("stt_unavailable", inbound=inbound, ticket_id=state.tid,
                               detail="voice reply could not be transcribed")
                return "(voice note: transcription unavailable)", False
            return str(tr["text"]), True
        if inbound.kind == "media":
            cap = f" {inbound.text}" if inbound.text else ""
            return f"[{inbound.media_type} {inbound.media_id}]{cap}", True
        return inbound.text or "", True

    def _record_reply(self, state: TicketState, inbound: Inbound) -> tuple[dict, str]:
        """Store the customer's message on the ticket. Returns (reply, "new" | "updated" | "dup"); a reply whose STT
        was down is RE-TRANSCRIBED when the message is re-driven (hold released)."""
        for r in state.replies:
            if r["message_id"] == inbound.message_id:
                if r.get("pending_stt"):
                    text, ok = self._reply_text(state, inbound)
                    if ok:
                        r["text"] = text
                        r.pop("pending_stt")
                        return r, "updated"
                return r, "dup"
        text, ok = self._reply_text(state, inbound)
        r = {"message_id": inbound.message_id, "kind": inbound.kind, "text": text, "ts": inbound.ts.isoformat()}
        if not ok:
            r["pending_stt"] = True
        state.replies.append(r)
        if inbound.channel == "email":                       # an email reply must NEVER reopen the WhatsApp 24h window
            state.last_email_at = max(state.last_email_at, inbound.ts.isoformat())
        else:
            state.last_inbound_at = max(state.last_inbound_at, inbound.ts.isoformat())
        return r, "new"

    def _attach(self, state: TicketState, inbound: Inbound) -> dict:
        """A message on an OPEN ticket is never dropped and never reprocessed as a new request: it is stored on the
        ticket, mirrored as an Odoo note and a topic post. Nothing moves forward by itself: a reply while we wait for
        the customer (or a newer one) asks the OWNER to mark it confirmed (an "advance" approval, superseding a stale
        one), and any other stage re-asks the owner if it has lost its approval (expiry, decline, window closed)."""
        r, status = self._record_reply(state, inbound)
        if status == "dup":                       # a RETRY of a reply whose re-ask failed must still re-ask
            self._ensure_pending(state, force=True)
            return {"ticket_id": state.tid, "stage": state.stage, "attached": True}
        label = inbound.media_type if inbound.kind == "media" else inbound.kind
        self._note(state.tid, f"[Hub] Customer reply ({label}): {html.escape(r['text'])}")
        self._post(state, f"Customer reply ({label}): {r['text']}",
                   safe=f"Ticket #{state.tid}: customer reply #{len(state.replies)} ({label}); the text is in Odoo")
        if state.stage in ("awaiting_customer", "awaiting_owner_advance"):
            state.stage = "awaiting_owner_advance"
            self._request_approval(state, "advance", r["text"], judged=False)
        else:
            self._save(state)
            self._ensure_pending(state, force=True)
        return {"ticket_id": state.tid, "stage": state.stage, "attached": True}

    def _post_resolve(self, state: TicketState, inbound: Inbound) -> dict:
        """A message on a RESOLVED ticket within ``reopen_days``: attach it and ask the OWNER "reopen or keep closed?".
        The ticket stays resolved until the owner chooses; more messages join the same open question."""
        r, status = self._record_reply(state, inbound)
        if status == "dup":
            return {"ticket_id": state.tid, "stage": state.stage, "attached": True}
        if inbound.message_id not in state.post_resolve_ids:
            state.post_resolve_ids.append(inbound.message_id)
        self._note(state.tid, f"[Hub] Customer message after resolve: {html.escape(r['text'])}")
        self._post(state, f"Customer wrote after the ticket was resolved: {r['text']}\nReopen or keep closed?",
                   safe=f"Ticket #{state.tid}: the customer wrote after it was resolved (message #{len(state.replies)}); "
                        "the owner decides reopen or keep closed")
        if self._live_pending(state) and state.pending_kind == "reopen":
            self._save(state)
        else:
            self._request_approval(state, "reopen", r["text"], judged=False)
        return {"ticket_id": state.tid, "stage": state.stage, "attached": True, "reopen_asked": True}

    # ---------------------------------------------------------------- owner override (§15b)

    def _current_owner_uid(self, tid: int) -> int | None:
        st = self.ticket(int(tid))
        return st.owner.uid if st and st.owner else None

    def _assign_as(self, tid: int, uid: int, actor_tg_id, old_uid: int) -> None:
        """A reassign runs AS the person who tapped (``hub.odoo_tg_people``); unmapped or no tapper = automation."""
        if getattr(self.odoo, "acting", False):
            self.odoo.assign(tid, uid, tapper=True, actor_tg_id=actor_tg_id, old_owner_uid=old_uid)
        else:
            self.odoo.assign(tid, uid)

    def reassign(self, ref, new_owner: Owner, *, actor_tg_id=None) -> dict:
        """One-tap owner override: write the reassignment and a learning SIGNAL. ``ref`` is a ticket id (int) or a
        sender id (str, latest ticket). A pending approval is re-issued to the NEW owner."""
        state = self.ticket(int(ref)) if isinstance(ref, int) else self.latest_ticket(str(ref))
        if state is None:
            raise KeyError("no such ticket")
        with self._wa_lock(state.wa_id):
            state = self.ticket(state.tid)
            if not self._actor_ok(actor_tg_id, state):
                log.warning("reassign refused on ticket %s: actor not authorised", state.tid)
                return {"ok": False, "reason": "not_authorised"}
            old = state.owner
            try:
                self._assign_as(state.tid, new_owner.uid, actor_tg_id, old.uid if old else 0)
            except ActorRefused as e:                          # no key / wrong identity: blocked, nothing changed
                log.warning("reassign blocked on ticket %s: %s", state.tid, e.reason)
                return {"ok": False, "reason": f"odoo_{e.reason}"}
            state.owner, state.reassigned = new_owner, True
            self._save(state)
            self._reissue_pending(state)
            sig = self.learning.signal("reassignment_override", {
                "ticket_id": state.tid, "client": state.client, "final_type": state.analysis.ticket_type,
                "from_uid": old.uid if old else 0, "final_owner_uid": new_owner.uid})
            return {"ok": True, "ticket_id": state.tid, "from": old.name if old else None, "to": new_owner.name, "signal": sig}
