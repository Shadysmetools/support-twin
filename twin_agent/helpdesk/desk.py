"""The helpdesk desk: ticket cards, Principal's taps, dedupe, create / link / note, stage moves and Supervisor's gate.

Flow of one ticket:
1. A request reaches CHECKS_DONE on a lane in `helpdesk.offer_on_lanes`, or Principal sends `/ticket REQ-x`.
2. The desk builds a TICKET-STYLE draft (conventions from the live database) and posts a card with
   [Create ticket] [Not a ticket]. The tap is bound to the sha256 of that exact draft.
3. On Principal's tap: dedupe (local links, then Odoo: the REQ ref in the description, the email Message-ID), then
   create in a SILENT stage + post the internal note, or link to the existing ticket with a note.
4. `/stage REQ-x <stage id>` (Principal's own words are the approval): a silent stage is written; a client-emailing
   stage becomes a card for Supervisor's tap [Approve move] [Not now], and only her tap moves it.
Every write is a Monitor `write` event (done / prepared / blocked) with its approvals. OBSERVE (the default):
nothing reaches Odoo, each write is recorded as "prepared". The kill switch blocks every write.
"""
from __future__ import annotations

import json
import logging

from .. import closing, paths
from ..bus import Topics
from ..models import Button, Card, CardKind
from . import analyst, drafting, guard, style, ticket_mail
from .conventions import AUTO_CLOSE_STAGE, Conventions, discover
from .ledger import DONE, Ledger, approval, instruction, log_write
from .odoo import writes_allowed

log = logging.getLogger(__name__)
DESK_ACTIONS = frozenset({"hk", "hx", "ag", "ax"})
DEFAULT_LANES = ("L4_t2a_fix_proposal", "L5_t2b_config")


WIKI_SHARE = "https://wiki.example.com/share/"   # only a genuine public Wiki share link is attached


def _video_url(draft) -> str | None:
    """The investigator's recorded proof-video link, if the draft carries one (future-proof: None today)."""
    for attr in ("proof_video", "proof_video_url", "video_url"):
        v = getattr(draft, attr, None)
        if v:
            return str(v)
    return None


class HelpdeskDesk:
    def __init__(self, db, cfg: dict, bus, clock, odoo=None, *, conv: Conventions | None = None):
        self.db, self.cfg, self.bus, self.clock, self.odoo = db, cfg, bus, clock, odoo
        self.hd = cfg.get("helpdesk") or {}
        self.ledger = Ledger(db)
        self._conv = conv
        self._search_failed = False
        people = cfg.get("people") or {}
        self.principal_id = int(people.get("principal_tg_user_id") or 0)
        self.supervisor_id = int(people.get("supervisor_tg_user_id") or 0)
        self.new_stage_id = int(self.hd.get("new_stage_id") or 1)        # "New" (emails the client)
        self.working_stage_id = int(self.hd.get("create_stage_id") or 14)  # "Functional" (silent, being worked)
        # Principal 2026-11-09: open the ticket by itself when a request is triaged (an INTAKE ticket, type Inquiry,
        # silent stage), then add the analysis as an internal note when checks finish - no [Create ticket] tap.
        # A development ticket still needs a passing proof (verify-first): intake tickets are never development.
        self.auto_create = bool(self.hd.get("auto_create"))
        # 2026-11-10: auto-tickets are for Telegram requests (Principal's flow). Mail from the same people is often a
        # thread follow-up, not a new issue: with this list set, other channels get a card but no ticket by itself.
        chans = self.hd.get("auto_create_channels")
        self.auto_create_channels = {str(c) for c in chans} if chans else None
        # Principal 2026-11-09: "I can close a ticket normally, so when I tell you to close it you close it". His own
        # /close (or the card's Close tap) moves the linked ticket to this stage directly; no Supervisor gate for HIM.
        # Other emailing stages, and anyone else's request, still wait for Supervisor. 0/None = off.
        self.principal_close_stage = int(self.hd.get("principal_close_stage_id") or 0) or None
        # Principal 2026-11-09 (rule): "moving my tickets to different stages doesn't need Supervisor's approval". When on,
        # ANY stage HE orders (/stage, /close, Close tap) is written directly; the twin's own automatic moves stay
        # silent-only, and nobody else can move a ticket into a client-emailing stage without Supervisor.
        self.principal_moves_ungated = bool(self.hd.get("principal_moves_without_supervisor"))
        # Principal 2026-11-09: "you should have made it Functional, not Pending Internal Review". After auto_create
        # opens a ticket (in the silent create stage), it is moved to this stage as Principal's standing rule.
        self.after_create_stage = int(self.hd.get("after_create_stage_id") or 0) or None
        # Principal 2026-11-12: the Ticket Analyst routes every ticket the twin creates (Functional 2 / Development
        # Requested 6) once the analysis is done, and emails him the result. While it is on, a new ticket waits in
        # the silent create stage instead of moving to after_create_stage at once.
        self.an = self.hd.get("analyst") or {}
        self.analyst_on = bool(self.an.get("enabled"))
        self.notify_partner = int(self.an.get("notify_partner_id") or 0) or None

    @property
    def write_on(self) -> bool:
        return writes_allowed(self.cfg, "helpdesk") and bool(getattr(self.odoo, "writes_on", False))

    def _wctx(self) -> dict:
        """The authenticated uid and odoo url stamped on every Monitor write event (the Monitor classifies them)."""
        return {"uid": getattr(self.odoo, "uid", None),
                "url": str((self.cfg.get("odoo_sys") or {}).get("url") or "") or None}

    def wire(self) -> None:
        self.bus.subscribe(Topics.CALLBACK_IN, self.on_callback)
        self.bus.subscribe(Topics.COMMAND_IN, self.on_command)
        self.bus.subscribe(Topics.CHECKS_DONE, self.on_checks)
        self.bus.subscribe(Topics.WORKER_FAILED, self.on_worker_failed)
        if self.auto_create:
            self.bus.subscribe(Topics.REQUEST_TRIAGED, self.on_triaged)
        self.bus.subscribe(Topics.REQUEST_REOPENED, self.on_reopened)
        self.bus.subscribe(Topics.REQUEST_UPDATED, self.on_request_updated)

    async def on_reopened(self, p: dict) -> None:
        """Principal 2026-11-11: "if the request came again the ticket should be open again". The requester wrote
        again after Principal's Approve closed it: the linked ticket goes back to the after-create stage (Functional),
        Principal's own rule, so no Supervisor step. No ticket linked: nothing to do."""
        rid = p.get("request_id")
        req = self.db.get_request(rid) if rid else None
        tid = str((req or {}).get("ticket_id") or "")
        stage = self.after_create_stage or self.working_stage_id
        if not tid.isdigit() or not stage:              # writes off: the Odoo layer records it as prepared
            return
        if self._kit_owns_tickets():
            self.db.log_event("ticket_reopen_skipped", rid, actor="helpdesk", why="label mode: the kit owns the ticket")
            msgs = [m for m in self.db.thread(rid) if not m.get("is_principal") and not m.get("is_bot")]
            if msgs:
                self._copy_client_reply(rid, int(tid), msgs[-1])
            return
        self.move_stage(rid, int(tid), int(stage),
                        [approval("principal_rule", self.principal_id, self.clock.now(),
                                  rule="reopened request: its ticket goes back to the after-create stage")],
                        by_principal=True)
        self.db.log_event("ticket_reopened", rid, actor="helpdesk", ticket_id=tid, stage=int(stage))

    def _auto_channel_ok(self, req: dict) -> bool:
        """helpdesk.auto_create_channels: only these channels get a ticket by themselves (mail gets a card only)."""
        from ..config import chat_flag
        chat_ok = chat_flag(self.cfg, req.get("chat_id"), "auto_ticket", True)   # Acme Team: internal, no ticket
        if chat_ok and (self.auto_create_channels is None or str(req.get("channel")) in self.auto_create_channels):
            return True
        self.db.log_event("ticket_auto_skipped", req.get("id"), actor="helpdesk", channel=str(req.get("channel")))
        return False

    def _auto_approval(self) -> list:
        return [approval("auto_create", "config", self.clock.now(), setting="helpdesk.auto_create")]

    def _auto_apply(self, rid: str, h: str | None) -> int | None:
        d = self.ledger.draft_by_hash(rid, h) if h else None
        return self.apply(rid, d, self._auto_approval()) if d else None

    async def on_triaged(self, p: dict) -> None:
        """auto_create: the first triage of a request opens its intake ticket (never a second one)."""
        rid = p.get("request_id")
        if not self.auto_create or not rid or self.ledger.find([rid]) or self.ledger.latest_draft(rid):
            return
        if not self._auto_channel_ok(self.db.get_request(rid) or {}):
            return
        tid = self._auto_apply(rid, self.offer(rid, auto=True, intake=True))
        if tid and self.after_create_stage and not self.analyst_on:
            self.move_stage(rid, int(tid), self.after_create_stage,
                            [approval("principal_rule", self.principal_id, self.clock.now(),
                                      rule="helpdesk.after_create_stage_id: new tickets go to this stage")],
                            by_principal=True)

    def conventions(self) -> Conventions:
        if self._conv is None:
            self._conv = discover(self.odoo, self.cfg) if self.odoo is not None else Conventions(problems=["no Odoo"])
            for p in self._conv.problems:
                self.db.log_event("helpdesk_convention", None, actor="helpdesk", problem=p)
        return self._conv

    def card(self, text: str, rid: str | None, buttons=None, *, to: str = "principal", draft_hash: str = "") -> None:
        c = Card(kind=CardKind.ticket if buttons else CardKind.info, text=style.redact(text)[:3400], request_id=rid,
                 to=to, buttons=buttons or [])
        self.bus.publish(Topics.CARD_OUT, {"card": c, "draft_hash": draft_hash} if draft_hash else c)

    def _stopped(self) -> bool:
        try:
            return bool(self.db.kill_on()) or paths.stop_file().exists()
        except Exception:                                  # noqa: BLE001 - fail closed
            return True

    # ---------------------------------------------------------------- 1-2: offer a draft

    async def on_checks(self, p: dict) -> None:
        rid = p.get("request_id")
        req = self.db.get_request(rid) if rid else None
        if req and self.auto_create:
            link = self.ledger.find([rid])
            if link and link.get("ticket_id") and link.get("status") in DONE:
                if self.analyst_on and link.get("status") == "created":
                    self.route(rid, int(link["ticket_id"]), p)       # one note, three fields, stage, email
                    return
                # the intake ticket exists: add the finished analysis to it as an internal note, no tap
                self._note(rid, int(link["ticket_id"]),
                           ["[Digital Twin] Analysis finished", *self._analysis_lines(rid, bool(p.get("verified")))],
                           self._auto_approval())
                return
            if not link and not self.ledger.latest_draft(rid):
                mail_ticket = str(req.get("channel")) == "email" and self.hd.get("mail_tickets", True) \
                    and closing.mail_is_actionable(req, closing.latest_packet(self.db, rid))
                if not mail_ticket and not self._auto_channel_ok(req):
                    return              # 2026-11-11: an FYI mail gets a card, never a ticket (REQ-0061)
                # Telegram, or (Principal 2026-11-11) a mail that is a request / inquiry: a Functional ticket, decided
                # here because only the analysis tells a real request from an FYI.
                tid = self._auto_apply(rid, self.offer(rid, verified=bool(p.get("verified")), auto=True, intake=True))
                if tid and self.analyst_on:
                    self.route(rid, int(tid), p)
                    return
                if tid and mail_ticket and self.after_create_stage:
                    self.move_stage(rid, int(tid), self.after_create_stage,
                                    [approval("principal_rule", self.principal_id, self.clock.now(),
                                              rule="a mail request gets a Functional ticket")], by_principal=True)
                return
        lanes = self.hd.get("offer_on_lanes", DEFAULT_LANES) or ()
        if req and req.get("lane") in lanes and not self.ledger.latest_draft(rid) and not self.ledger.find([rid]):
            self.offer(rid, verified=bool(p.get("verified")))

    # ---------------------------------------------------------------- Ticket Analyst (Principal 2026-11-12)

    async def on_worker_failed(self, p: dict) -> None:
        """Principal 2026-11-12: a failed analysis still routes its ticket (Functional, confidence 0, unsure, the
        reason). An email request has no ticket yet: it gets one now, since only the analysis tells an FYI apart."""
        rid = p.get("request_id")
        req = self.db.get_request(rid) if rid else None
        if not self.analyst_on or not self.auto_create or not req:
            return
        link = self.ledger.find([rid])
        if link and link.get("status") in DONE and link.get("status") != "created":
            return                                     # someone else's ticket: never routed
        if self.db._one("SELECT 1 AS x FROM events WHERE kind = 'ticket_routed' AND request_id = ?", (rid,)):
            return                                     # already routed: a failed re-run never moves it back
        self._route_failed(rid, str(p.get("why") or "unknown"))

    def _route_failed(self, rid: str, why: str) -> None:
        link = self.ledger.find([rid])
        if link and link.get("status") in DONE and link.get("status") != "created":
            return                                     # someone else's ticket: never routed
        tid = self.ledger.ticket_of(rid) if link else None
        if not tid:
            tid = self._auto_apply(rid, self.offer(rid, auto=True, intake=True))
        if tid:
            self.route(rid, int(tid), {"request_id": rid, "verified": False, "verdicts": [],
                                       "packet": {"analysis": f"The twin's analysis failed: {why}", "failed": why}},
                       forced=analyst.failed(why))

    def route(self, rid: str, tid: int, p: dict, *, forced=None) -> None:
        """Classify a ticket the twin created, write x_track / x_type / x_ai_confidence and ONE internal note, move
        it to Functional or Development Requested, and email Principal. Once per analysis (a reopened request's new
        analysis routes again)."""
        packet = p.get("packet")
        if not isinstance(packet, dict):
            pk = self.db.latest_packet(rid) or {}
            packet = json.loads(pk["packet_json"]) if pk.get("packet_json") else {}
        key = __import__("hashlib").sha256(
            json.dumps([tid, packet.get("analysis"), packet.get("understanding"), packet.get("track"),
                        packet.get("ticket_type"), packet.get("tier"), packet.get("failed")], sort_keys=True,
                       default=str).encode()
        ).hexdigest()[:16]
        if self.db._one("SELECT 1 AS x FROM events WHERE kind = 'ticket_routed' AND request_id = ? "
                        "AND detail_json LIKE ?", (rid, f'%"key": "{key}"%')):
            return
        if self._kit_owns_tickets() and self.db._one(
                "SELECT 1 AS x FROM events WHERE kind = 'ticket_routed' AND request_id = ?", (rid,)):
            # Label mode: the ticket was handed to the kit already. A re-analysis (a reopened request) never
            # re-labels or moves it; the follow-up message itself reaches the ticket as a note.
            self.db.log_event("label_skipped", rid, actor="helpdesk", ticket=tid, why="already handed to the kit")
            return
        r = forced or analyst.classify(packet, p.get("verdicts"), bool(p.get("verified")))
        appr = [approval("principal_rule", self.principal_id, self.clock.now(),
                         rule="Ticket Analyst routes every ticket the twin creates (Principal 2026-11-12)")]
        conv = self.conventions()
        vals: dict = {}
        if conv.has("x_track") and r.track in conv.track_values:
            vals["x_track"] = r.track
        type_id = conv.types.get(r.type_name.lower())
        if conv.has("x_type") and type_id:
            vals["x_type"] = int(type_id)
        if conv.has("x_ai_confidence"):
            vals["x_ai_confidence"] = r.confidence
        if vals:
            self._write_fields(rid, tid, vals, appr)
        # Principal 2026-11-15: in "label" mode Supervisor's Digital Twin kit works the ticket. The twin only labels it and
        # hands it to New, where the kit's hourly job picks labelled tickets; no 2/6 routing, email or timesheet here.
        label_only = str(self.an.get("mode") or "route") == "label"
        stage = int(self.an.get("label_stage_id") or 1) if label_only else r.stage_id
        lines = ["[Digital Twin] Ticket Analyst",
                 (f"Handed to New (stage {stage}) for the Digital Twin ticket check; suggested route: {r.stage_name}"
                  if label_only else f"Routed to: {r.stage_name} (stage {r.stage_id})"),
                 f"Track / type: {r.track} / {r.type_name}",
                 f"Confidence from checked evidence: {r.confidence} ({r.level})"]
        if r.unsure:
            lines.append("Unsure, please check: " + ("; ".join(r.reasons) or "weak evidence"))
        lines += ["", "Analysis:", *([packet["analysis"]] if forced is not None
                                         else self._analysis_lines(rid, bool(p.get("verified"))))]
        self._note(rid, tid, lines, appr)
        self.move_stage(rid, tid, stage, appr, by_principal=True)
        if not label_only:
            self._log_routing_time(rid, tid, r, appr)
            self._email_principal(rid, tid, r, packet, appr)
        self.db.log_event("ticket_routed", rid, actor="helpdesk", key=key, ticket=tid, track=r.track,
                          type=r.type, confidence=r.confidence, level=r.level, stage=r.stage_id, source=r.source,
                          reasons=r.reasons)

    def route_saved(self, rid: str) -> None:
        """`route REQ-x`: route a ticket the twin made from the request's latest saved analysis."""
        if not self.analyst_on:
            self.card(f"route {rid}: the Ticket Analyst is off (helpdesk.analyst.enabled).", rid)
            return
        link = self.ledger.find([rid])
        tid = self.ledger.ticket_of(rid)
        pk = self.db.latest_packet(rid) or {}
        if not pk.get("packet_json"):
            fail = self.db._one("SELECT detail_json FROM events WHERE kind = 'worker_failed' AND request_id = ? "
                                "ORDER BY id DESC LIMIT 1", (rid,))
            if fail:                                   # the analysis failed: route it as a failure (REQ-0068)
                why = json.loads(fail["detail_json"] or "{}").get("why") or "unknown"
                self._route_failed(rid, str(why))
                return
            self.card(f"route {rid}: no saved analysis yet.", rid)
            return
        if not link and self.auto_create:              # analysed but never ticketed (intake was off): open it now
            tid = self._auto_apply(rid, self.offer(rid, verified=bool(pk.get("verified")), auto=True, intake=True))
            link = self.ledger.find([rid])
        if not tid or not link or link.get("status") != "created":
            self.card(f"route {rid}: only a ticket the twin created can be routed.", rid)
            return
        self.route(rid, int(tid), {"request_id": rid, "packet": json.loads(pk["packet_json"]),
                                   "verdicts": json.loads(pk.get("verdicts_json") or "[]"),
                                   "verified": bool(pk.get("verified"))})

    def _write_fields(self, rid: str, tid: int, vals: dict, appr: list) -> None:
        conv = self.conventions()
        args = [[tid], vals]
        status, reason = "done", ""
        try:
            guard.check("helpdesk", "helpdesk.ticket", "write", args, safe_stages=conv.safe_stages)
            if self._stopped():
                status, reason = "blocked", "kill switch is on"
            elif not self.write_on:
                status = "prepared"
            else:
                self.odoo.call("helpdesk", "helpdesk.ticket", "write", args, safe_stages=conv.safe_stages)
        except Exception as e:                              # noqa: BLE001
            status, reason = "blocked", str(e) if isinstance(e, guard.WriteRefused) else type(e).__name__
        log_write(self.db, rid, op="write", model="helpdesk.ticket", status=status, approvals=appr,
                  purpose="helpdesk", record_id=tid, fields=sorted(vals), reason=reason, **self._wctx())

    def _log_routing_time(self, rid: str, tid: int, r, appr: list) -> None:
        """Principal 2026-11-12 ("Yes, 0.25 h each"), his ticket-router rule: one timesheet line per ticket routed
        (employee 26, project 107, the ticket, today, 'Routing: <stage> - <reason>'). Existing routing lines on the
        ticket for today are checked first, so nothing is logged twice; the new line is read back."""
        ts = self.an.get("timesheet") or {}
        if not ts.get("enabled"):
            return
        from ..clock import CAIRO
        emp, project = int(ts.get("employee_id") or 0), int(ts.get("project_id") or 0)
        hours = float(ts.get("hours") or 0.25)
        day = self.clock.now().astimezone(CAIRO).date().isoformat()
        why = (r.reasons[0] if r.unsure and r.reasons else f"{r.track} / {r.type_name}, confidence {r.confidence}")
        vals = {"name": f"Routing: {r.stage_name} - {why}"[:200], "unit_amount": hours, "date": day,
                "employee_id": emp, "project_id": project, "helpdesk_ticket_id": int(tid)}
        status, reason, line = "done", "", None
        try:
            guard.check("timesheet", "account.analytic.line", "create", [vals])
            if self._stopped():
                status, reason = "blocked", "kill switch is on"
            elif not self.write_on:
                status = "prepared"
            else:
                old = self.odoo.search_read("account.analytic.line",
                                            [["helpdesk_ticket_id", "=", int(tid)], ["employee_id", "=", emp],
                                             ["date", "=", day], ["name", "=like", "Routing:%"]], ["id"])
                if old:
                    status, reason = "skipped", "a routing line is already logged on this ticket today"
                else:
                    line = int(self.odoo.call("timesheet", "account.analytic.line", "create", [vals]))
                    back = self.odoo.read("account.analytic.line", [line], ["unit_amount", "helpdesk_ticket_id"])
                    if not back:
                        status, reason = "blocked", "the new line could not be read back"
        except Exception as e:                              # noqa: BLE001
            status, reason = "blocked", str(e) if isinstance(e, guard.WriteRefused) else type(e).__name__
        if status != "skipped":
            log_write(self.db, rid, op="create", model="account.analytic.line", status=status, approvals=appr,
                      purpose="timesheet", record_id=line, fields=sorted(vals), reason=reason, **self._wctx())
        self.db.log_event("ticket_timesheet", rid, actor="helpdesk", ticket=int(tid), status=status, line=line,
                          hours=hours, reason=reason)

    def _request_text(self, rid: str) -> str:
        msgs = [m for m in self.db.thread(rid) if not m.get("is_principal") and (m.get("text") or "").strip()]
        return " / ".join(str(m["text"]).strip() for m in msgs) or "(photo or attachment only)"

    def _email_principal(self, rid: str, tid: int, r, packet: dict, appr: list) -> None:
        """The routed-ticket email: a comment to Principal's partner only (guard: COMMENT_KWARGS)."""
        if not self.notify_partner:
            self.db.log_event("ticket_email_skipped", rid, actor="helpdesk", why="no notify_partner_id")
            return
        req = self.db.get_request(rid) or {"id": rid}
        url = str((self.cfg.get("odoo_sys") or {}).get("url") or "").rstrip("/")
        body = ticket_mail.email_html(tid, rid, req, r, packet, self._request_text(rid),
                                      str(self.an.get("open_url") or "http://127.0.0.1:8766/open"),
                                      f"{url}/odoo/helpdesk/{tid}" if url else None)
        kw = {"body": body, "subtype_xmlid": "mail.mt_comment", "message_type": "comment", "body_is_html": True,
              "partner_ids": [self.notify_partner], "context": {"mail_notify_author": True}}
        status, reason = "done", ""
        try:
            guard.check("helpdesk", "helpdesk.ticket", "message_post", [[tid]], kw, notify_partner=self.notify_partner)
            if self._stopped():
                status, reason = "blocked", "kill switch is on"
            elif not self.write_on:
                status = "prepared"
            else:
                self.odoo.call("helpdesk", "helpdesk.ticket", "message_post", [[tid]], kw,
                               notify_partner=self.notify_partner)
        except Exception as e:                              # noqa: BLE001
            status, reason = "blocked", str(e) if isinstance(e, guard.WriteRefused) else type(e).__name__
        log_write(self.db, rid, op="message_post", model="helpdesk.ticket", status=status, approvals=appr,
                  purpose="helpdesk", record_id=tid, fields=["body"], reason=reason,
                  **self._wctx())

    def _analysis_lines(self, rid: str, verified: bool) -> list[str]:
        req = self.db.get_request(rid) or {"id": rid}
        pk = self.db.latest_packet(rid) or {}
        packet = json.loads(pk["packet_json"]) if pk.get("packet_json") else {}
        d = drafting.build(req, packet, self.conventions(), self.cfg, odoo=self.odoo,
                           verified=verified or bool(pk.get("verified")), ref=self.ledger.ref(rid))
        return style.note_lines(d)[1:]

    def offer(self, rid: str, *, verified: bool = False, words: str = "", auto: bool = False,
              intake: bool = False) -> str | None:
        req = self.db.get_request(rid)
        if not req:
            return None
        conv = self.conventions()
        pk = self.db.latest_packet(rid) or {}
        packet = json.loads(pk["packet_json"]) if pk.get("packet_json") else {}
        d = drafting.build(req, packet, conv, self.cfg, odoo=self.odoo, verified=verified or bool(pk.get("verified")),
                           ref=self.ledger.ref(rid))
        if intake:                                          # an intake ticket is an Inquiry, never development
            d.type = "Inquiry"
            d.x_track = "functional" if "functional" in conv.track_values else None
        problems = style.lint(d)
        stage = int(self.hd.get("create_stage_id") or 14)
        existing = self.ledger.find(d.dedupe_keys)
        # Verify-first gate (proof pipeline): a DEVELOPMENT ticket is CREATED only when a proof artifact cleared it.
        # Dev-ness is read from the DERIVED draft (type Bug / an explicit dev part), not just ticket_draft parts -
        # a T2a packet derives type Bug with parts=[], and used to be drafted ungated. With no passing proof (the
        # runtime reality) the create action is WITHHELD and flagged for a human. Linking a note to a ticket that
        # already exists is not a create, so it is never gated. Non-development tickets are unaffected.
        from ..proof.pipeline import proof_cleared
        link_only = bool(existing and existing["status"] in DONE)
        withheld = (not link_only) and (not intake) and drafting.is_development(req, packet) \
            and not proof_cleared(packet)
        dump = {**d.__dict__, "stage_id": stage, "words": words, "gated": withheld}
        draft_id, h = self.ledger.save_draft(rid, dump)
        if auto and not withheld:                           # auto_create: no card with buttons, apply() reports
            self.db.log_event("ticket_auto", rid, actor="helpdesk", draft_id=draft_id, link_only=link_only)
            return h
        head = style.plain_ticket(d)
        sname = (conv.stages.get(stage) or {}).get("name") or f"stage {stage}"
        if link_only:
            text = f"{rid} is already ticket #{existing['ticket_id']}. Tap to add this request as a linked note.\n\n{head}"
            buttons = [[Button("Add note to ticket", "hk"), Button("Not now", "hx")]]
        elif withheld:
            self.db.log_event("dev_ticket_withheld", rid, actor="helpdesk",
                              why="verify-first: no passing proof; a development ticket needs a human")
            cls = (f"Team {d.team_id or '?'} | {sname} | priority {d.priority} | track {d.x_track or '-'} | "
                   f"type {d.type} | client id {d.partner_id or 'not found'}")
            note = "\n".join(style.note_lines(d))
            text = (f"Development ticket draft for {rid} is WITHHELD: no passing proof, so a dev ticket needs a "
                    f"human (verify-first). Nothing will be created from this card.\n\n{head}\n\n{cls}\n\n"
                    f"Internal note:" f"\n{note}")
            buttons = [[Button("Not a ticket", "hx")]]
        else:
            safe = "silent" if stage in conv.safe_stages else "NOT confirmed silent: create will be refused"
            cls = (f"Team {d.team_id or '?'} | {sname} ({safe}) | priority {d.priority} | track {d.x_track or '-'} | "
                   f"type {d.type} | assignee Principal | client id {d.partner_id or 'not found'}")
            note = "\n".join(style.note_lines(d))
            warn = ("\nStyle problems: " + "; ".join(problems)) if problems else ""
            text = f"New ticket draft for {rid}:\n\n{head}\n\n{cls}\n\nInternal note:" f"\n{note}{warn}"
            buttons = [[Button("Create ticket", "hk"), Button("Not a ticket", "hx")]]
        self.card(text, rid, buttons, draft_hash=h)
        self.db.log_event("ticket_offered", rid, actor="helpdesk", draft_id=draft_id, problems=len(problems))
        return h

    # ---------------------------------------------------------------- 3: taps

    async def on_callback(self, p: dict) -> None:
        action, rid, uid, nonce = p.get("action"), p.get("request_id"), int(p.get("user_id") or 0), p.get("nonce")
        if (action not in DESK_ACTIONS and action != "ck") or not rid:
            return
        row = self.db.get_approval(nonce) if nonce else None
        if not row or not row.get("used_at") or int(row.get("used_by") or 0) != uid:
            return                                      # the bot controller consumes the code; no code, no write
        if action == "ck":                              # Principal's Close tap on a card: close the ticket too
            if uid == self.principal_id:
                self.close_by_principal(rid, [approval("principal_tap", uid, self.clock.now(), card_id=row.get("card_id"),
                                                   action="close")])
            return
        if action in ("hk", "hx"):
            if uid != self.principal_id:
                return
            d = self.ledger.draft_by_hash(rid, row.get("draft_hash") or "")
            if not d:
                self.card(f"{rid}: that card is out of date. Use the newest ticket card.", rid)
                return
            if action == "hx":
                self.ledger.decide_draft(d["id"], "declined")
                self.db.log_event("ticket_declined", rid, actor=str(uid))
                return
            appr = [approval("principal_tap", uid, self.clock.now(), card_id=row.get("card_id"),
                             draft_sha256=d["draft_hash"])]
            if (json.loads(d["draft_json"]).get("words")):
                appr.append(instruction(uid, self.clock.now(), json.loads(d["draft_json"])["words"]))
            self.apply(rid, d, appr)
        elif action in ("ag", "ax") and self.supervisor_id and uid == self.supervisor_id:
            gate = self.ledger.open_gate(rid)
            if not gate or row.get("draft_hash") != f"gate:{gate['ticket_id']}:{gate['stage_id']}":
                return                                  # the tap must be for THIS move, not an older card
            if action == "ax":
                self.ledger.close_gate(gate["id"], "declined", str(uid))
                self.card(f"Supervisor said not now: #{gate['ticket_id']} stays in its stage.", rid)
                return
            appr = json.loads(gate["approvals_json"] or "[]") + [approval("supervisor_gate", uid, self.clock.now(),
                                                                           card_id=row.get("card_id"))]
            self.ledger.close_gate(gate["id"], "approved", str(uid))
            self._write_stage(rid, int(gate["ticket_id"]), int(gate["stage_id"]), appr, gated=True)

    def apply(self, rid: str, d: dict, appr: list) -> int | None:
        if json.loads(d["draft_json"]).get("gated"):
            # Verify-first: this development ticket was withheld (no passing proof). Even a (manufactured or stale)
            # tap must not create it - the create action is gated to a human. In production no [Create ticket]
            # button exists on a withheld card, so no nonce is ever issued for it.
            self.db.log_event("dev_ticket_withheld", rid, actor="helpdesk",
                              why="create refused: withheld development ticket (no passing proof)")
            self.card(f"{rid}: this development ticket needs a human (no passing proof). Not created.", rid)
            return None
        draft = style.TicketDraft(**{k: v for k, v in json.loads(d["draft_json"]).items()
                                     if k in style.TicketDraft.__dataclass_fields__})
        keys = draft.dedupe_keys or [rid]
        conv = self.conventions()
        existing = self.ledger.find(keys)
        # Bug fix (observe -> live): a `prepared` link owns no ticket (ticket_id IS NULL). It was written only to
        # record the observe-time intent. Once writes are on, a later tap must be able to actually create the
        # ticket, so release this request's own prepared keys and re-read, letting claim() succeed and
        # (a `claimed` row with no ticket is a crash mid-create: never released here, to avoid a double create.)
        # the Odoo dedupe search run again (a ticket may have appeared meanwhile).
        if self.write_on and existing and not existing.get("ticket_id") \
                and existing.get("status") == "prepared" and existing.get("request_id") == rid:
            for k in keys:
                self.ledger.release(k)
            existing = self.ledger.find(keys)
        if existing and existing["status"] in DONE or (existing and existing["status"] == "maybe_created"):
            tid = existing["ticket_id"] or self._search_odoo(draft)
            if tid:
                return self._link(rid, int(tid), draft, appr, keys, d)
        if existing is None:
            tid = self._search_odoo(draft)
            if tid:
                return self._link(rid, tid, draft, appr, keys, d)
        if self._search_failed and self.write_on:
            self.card(f"{rid}: I could not search Odoo for an existing ticket, so I did not create one (no "
                      "duplicates). Tap again later.", rid)
            return None
        if self._stopped():
            log_write(self.db, rid, op="create", model="helpdesk.ticket", status="blocked", approvals=appr,
                      purpose="helpdesk", reason="kill switch is on", **self._wctx())
            return None
        if not self.ledger.claim(keys[0], rid) and (existing or {}).get("status") != "maybe_created":
            self.card(f"{rid}: a ticket for this request is already being made (or prepared). Nothing new.", rid)
            return None
        stage = int(json.loads(d["draft_json"]).get("stage_id") or 14)
        desc = style.html_lines(style.body_lines(draft))
        vals = drafting.create_vals(draft, conv, stage, desc)
        try:
            guard.check("helpdesk", "helpdesk.ticket", "create", [vals], safe_stages=conv.safe_stages)
        except guard.WriteRefused as e:
            self.ledger.release(keys[0])
            log_write(self.db, rid, op="create", model="helpdesk.ticket", status="blocked", approvals=appr,
                      purpose="helpdesk", fields=list(vals), stage_to=stage, reason=str(e), **self._wctx())
            self.card(f"{rid}: ticket not created: {e}", rid)
            return None
        if not self.write_on:
            self.ledger.set_link(keys, rid, None, "prepared")
            self.ledger.decide_draft(d["id"], "prepared")
            for op, fields in (("create", list(vals)), ("message_post", ["body"])):
                log_write(self.db, rid, op=op, model="helpdesk.ticket", status="prepared", approvals=appr,
                          purpose="helpdesk", fields=fields, stage_to=stage if op == "create" else None,
                          **self._wctx())
            self.card(f"Observe: ticket for {rid} NOT created (writes are off). It would be created in stage "
                      f"{stage} with: {', '.join(sorted(vals))}, plus the internal note.", rid)
            return None
        try:
            tid = int(self.odoo.call("helpdesk", "helpdesk.ticket", "create", [vals], safe_stages=conv.safe_stages))
        except Exception as e:                              # noqa: BLE001 - maybe created: search before retrying
            self.ledger.set_link(keys, rid, None, "maybe_created")
            log_write(self.db, rid, op="create", model="helpdesk.ticket", status="blocked", approvals=appr,
                      purpose="helpdesk", reason=f"maybe created: {type(e).__name__}", **self._wctx())
            self.card(f"{rid}: Odoo did not confirm the ticket ({type(e).__name__}). It may exist: I will search "
                      "for it before any retry.", rid)
            return None
        self.ledger.set_link(keys, rid, tid, "created")
        self.ledger.decide_draft(d["id"], "created", tid)
        log_write(self.db, rid, op="create", model="helpdesk.ticket", status="done", approvals=appr,
                  purpose="helpdesk", record_id=tid, fields=list(vals), stage_to=stage, **self._wctx())
        self._note(rid, tid, style.note_lines(draft), appr)
        self._remember(rid, tid)
        self.on_ticket_ready(rid, tid, appr, created_stage=stage, video_url=self._proof_share_link(rid) or _video_url(draft))
        self.card(f"Ticket #{tid} created for {rid} in a silent stage, with the internal note.", rid)
        return tid

    def _search_odoo(self, draft) -> int | None:
        """Find a ticket this request already made, scoped to THIS client and to tickets the twin or Supervisor created.

        Bug fix: the REQ ref and the email Message-ID both live in the client-visible description and are
        attacker-controlled, so they cannot be the sole dedupe token. Without a known client (partner_id) we
        never link by ref (fail closed) - a stranger could plant the ref to bind us to their ticket.
        """
        self._search_failed = False
        if self.odoo is None:
            return None
        partner = getattr(draft, "partner_id", None)
        if not partner:
            return None
        conv = self.conventions()
        owners = [int(u) for u in (conv.principal_uid, (conv.supervisor or {}).get("uid")) if u]
        try:
            dom = [["partner_id", "=", int(partner)],
                   ["description", "ilike", f"ref {self.ledger.ref(draft.request_id)})"]]
            if owners:
                dom.append(["create_uid", "in", owners])
            rows = self.odoo.search_read("helpdesk.ticket", dom, ["name"], limit=1)
            if rows:
                return int(rows[0]["id"])
            for k in draft.dedupe_keys:
                if k.startswith("msg:<"):                   # an email Message-ID: the alias may have made a ticket
                    m = self.odoo.search_read("mail.message", [["message_id", "=", k[4:]],
                                                               ["model", "=", "helpdesk.ticket"]], ["res_id"], limit=1)
                    if m:
                        tid = int(m[0]["res_id"])            # confirm it is THIS client's ticket before linking
                        own = self.odoo.search_read("helpdesk.ticket", [["id", "=", tid],
                                                    ["partner_id", "=", int(partner)]], ["id"], limit=1)
                        if own:
                            return tid
        except Exception:                                   # noqa: BLE001 - cannot search = cannot dedupe
            self._search_failed = True
            return None
        return None

    def _link(self, rid, tid, draft, appr, keys, d) -> int:
        self.ledger.set_link(keys, rid, tid, "linked")
        self.ledger.decide_draft(d["id"], "linked", tid)
        self._note(rid, tid, [f"[Digital Twin] Linked request {rid} ({draft.source}) to this ticket.",
                              *style.note_lines(draft)[1:]], appr)
        self._remember(rid, tid)
        self.on_ticket_ready(rid, tid, appr, video_url=self._proof_share_link(rid) or _video_url(draft))
        self.card(f"{rid} linked to existing ticket #{tid} (no second ticket).", rid)
        return tid

    def _note(self, rid: str, tid: int, lines: list[str], appr: list) -> None:
        # body_is_html: without it Odoo 17+/18 escapes the body over RPC and the note shows raw "<p>" tags
        # (Principal, 2026-11-09). The lines are html.escape()d by style.html_lines, so this stays injection-safe.
        kw = {"body": style.html_lines(lines), "subtype_xmlid": "mail.mt_note", "message_type": "comment",
              "body_is_html": True}
        status = "done"
        if self._stopped():
            status = "blocked"
        elif not self.write_on:
            status = "prepared"
        else:
            try:
                self.odoo.call("helpdesk", "helpdesk.ticket", "message_post", [[tid]], kw)
            except Exception as e:                          # noqa: BLE001
                status = "blocked"
                self.db.log_event("helpdesk_error", rid, actor="helpdesk", error=type(e).__name__)
        log_write(self.db, rid, op="message_post", model="helpdesk.ticket", status=status, approvals=appr,
                  purpose="helpdesk", record_id=tid, fields=["body"], **self._wctx())

    def _remember(self, rid: str, tid: int) -> None:
        url = str((self.cfg.get("odoo_sys") or {}).get("url") or "").rstrip("/")
        self.db.update_request(rid, ticket_id=str(tid), ticket_url=f"{url}/odoo/helpdesk/{tid}" if url else None)

    # ---------------------------------------------------------------- 4: stage moves

    async def on_command(self, p: dict) -> None:
        uid, cmd, args = int(p.get("user_id") or 0), p.get("command"), str(p.get("args") or "").split()
        if uid != self.principal_id or cmd not in ("ticket", "stage", "close", "route") or not args:
            return
        rid = args[0].upper()
        if cmd == "route":
            self.route_saved(rid)
            return
        if cmd == "close":
            self.close_by_principal(rid, [instruction(uid, self.clock.now(), f"/close {' '.join(args)}")])
            return
        if cmd == "ticket":
            self.offer(rid, words=f"/ticket {' '.join(args)}")
            return
        tid = self.ledger.ticket_of(rid)
        if not tid or len(args) < 2 or not args[1].isdigit():
            self.card(f"/stage {rid}: needs a ticket made by me and a stage id, e.g. /stage {rid} 14", rid)
            return
        self.move_stage(rid, tid, int(args[1]), [instruction(uid, self.clock.now(), f"/stage {' '.join(args)}")],
                        by_principal=True)

    async def on_request_updated(self, p: dict) -> None:
        """Label mode (Principal 2026-11-15, "if it is a reply to the same issue it will be in the same ticket"): a new
        message from the requester on a request whose ticket the kit is working is copied onto that ticket as an
        internal note, so the kit's client-followup hands it to a session. The ticket's stage is never touched."""
        rid = p.get("request_id")
        if not rid or not self._kit_owns_tickets():
            return
        req = self.db.get_request(rid) or {}
        tid = str(req.get("ticket_id") or "")
        msg = self.db.get_message(p.get("event_id")) if p.get("event_id") else None
        if not tid.isdigit() or not msg or msg.get("is_principal") or msg.get("is_bot") or msg.get("edited_at"):
            return
        self._copy_client_reply(rid, int(tid), msg)

    def _copy_client_reply(self, rid: str, tid: int, msg: dict) -> None:
        link = self.ledger.find([rid])
        if not link or link.get("status") != "created":
            return
        if not self.db._one("SELECT 1 AS x FROM events WHERE kind = 'ticket_routed' AND request_id = ?", (rid,)):
            return                                       # not handed to the kit yet: its first analysis covers it
        if self.db._one("SELECT 1 AS x FROM events WHERE kind = 'client_reply_copied' AND request_id = ? "
                        "AND detail_json LIKE ?", (rid, f'%"event_id": "{msg.get("event_id")}"%')):
            return
        req = self.db.get_request(rid) or {}
        where = " / ".join(x for x in (req.get("chat") or "", req.get("topic") or "") if x)
        channel = "Email" if str(req.get("channel")) == "email" else "Telegram"
        text = str(msg.get("text") or "").strip() or "(photo or attachment only)"
        lines = [f"[Client reply via the twin] {rid}, {channel}" + (f" ({where})" if where else "")
                 + f", from {msg.get('sender_name') or req.get('requester_name') or 'the requester'}:", text]
        self._note(rid, tid, lines, [approval("principal_rule", self.principal_id, self.clock.now(),
                                              rule="label mode: a requester's follow-up reaches the kit's ticket")])
        self.db.log_event("client_reply_copied", rid, actor="helpdesk", ticket=tid, event_id=msg.get("event_id"))

    def _kit_owns_tickets(self) -> bool:
        """Label mode (Principal 2026-11-15): Supervisor's Digital Twin kit works the ticket, so the twin never moves it."""
        return self.analyst_on and str(self.an.get("mode") or "route") == "label"

    def close_by_principal(self, rid: str, appr: list) -> None:
        """Principal's own close: the linked ticket goes to principal_close_stage_id now (only when that setting is on)."""
        if not self.principal_close_stage:
            return
        if self._kit_owns_tickets():
            self.db.log_event("ticket_close_skipped", rid, actor="helpdesk", why="label mode: the kit owns the ticket")
            return
        tid = self.ledger.ticket_of(rid)
        if not tid:
            self.card(f"{rid} closed here; it has no ticket made by me, so nothing to close in Odoo.", rid)
            return
        self.move_stage(rid, int(tid), self.principal_close_stage, appr, by_principal=True)

    def move_stage(self, rid: str, tid: int, stage_id: int, appr: list, *, by_principal: bool = False) -> None:
        conv = self.conventions()
        if stage_id in conv.safe_stages:
            self._write_stage(rid, tid, stage_id, appr, gated=False)
            return
        if by_principal and (self.principal_moves_ungated or (self.principal_close_stage and stage_id == self.principal_close_stage)):
            if stage_id not in conv.stages:
                self.card(f"#{tid}: stage {stage_id} does not exist in the help desk. Nothing moved.", rid)
                return
            self._write_stage(rid, tid, stage_id, appr, gated=True)   # his own words are the approval
            return
        name = (conv.stages.get(stage_id) or {}).get("name") or f"stage {stage_id}"
        self.ledger.add_gate(rid, tid, stage_id, appr)
        self.db.log_event("write_gate", rid, actor="helpdesk", ticket=tid, stage_to=stage_id, waiting="supervisor")
        if self.supervisor_id:
            self.card(f"Supervisor: Principal asks to move ticket #{tid} ({rid}) to '{name}'. Entering this stage EMAILS THE "
                      "CLIENT. Only your tap moves it.", rid, [[Button("Approve move", "ag"), Button("Not now", "ax")]],
                      to="supervisor", draft_hash=f"gate:{tid}:{stage_id}")
        self.card(f"#{tid} -> '{name}' emails the client, so it waits for Supervisor's tap"
                  + ("" if self.supervisor_id else " (people.supervisor_tg_user_id is not set: no card could reach her)") + ".", rid)

    def _write_stage(self, rid: str, tid: int, stage_id: int, appr: list, *, gated: bool) -> None:
        conv = self.conventions()
        emails = conv.emails_client(stage_id)
        args = [[tid], {"stage_id": stage_id}]
        kw = dict(safe_stages=conv.safe_stages, gated_stage=stage_id if gated else None)
        status, reason = "done", ""
        try:
            guard.check("helpdesk", "helpdesk.ticket", "write", args, **kw)
            if self._stopped():
                status, reason = "blocked", "kill switch is on"
            elif not self.write_on:
                status = "prepared"
            else:
                self.odoo.call("helpdesk", "helpdesk.ticket", "write", args, **kw)
        except Exception as e:                              # noqa: BLE001 - refused or failed = blocked
            status, reason = "blocked", str(e) if isinstance(e, guard.WriteRefused) else type(e).__name__
        log_write(self.db, rid, op="write", model="helpdesk.ticket", status=status, approvals=appr,
                  purpose="helpdesk", record_id=tid, fields=["stage_id"], stage_to=stage_id, emails_client=emails,
                  reason=reason, auto_close=stage_id == AUTO_CLOSE_STAGE, **self._wctx())
        tail = " Live starts the 7-day auto-close clock." if stage_id == AUTO_CLOSE_STAGE else ""
        self.card(f"#{tid} stage -> {stage_id}: {status}{(' (' + reason + ')') if reason else ''}.{tail}", rid)

    # ------------------------------------------------- 5: automatic start / identify / proof (per-ticket lifecycle)

    def on_ticket_ready(self, rid: str, tid: int, appr: list, *, created_stage: int | None = None,
                        video_url: str | None = None) -> None:
        """Fired automatically the moment a ticket is created or linked - the twin does what the developers do:
        log the START time, auto-advance the status New -> Functional (on identify), and attach the proof-video
        link if the investigator recorded one. Every step is guarded + OBSERVE-safe and never emails the client."""
        self.log_start(rid, tid, appr)
        self.advance_on_identify(rid, tid, appr, current_stage=created_stage)
        if video_url:
            self.attach_proof_video(rid, tid, video_url, appr)

    def log_start(self, rid: str, tid: int, appr: list) -> None:
        """Start time: record when work began on the ticket (the internal-note equivalent of pressing Start)."""
        now = self.clock.now()
        self._note(rid, tid, [f"[Digital Twin] Started work on this ticket at {now:%Y-%m-%d %H:%M}."], appr)

    def _current_stage(self, tid: int) -> int | None:
        if self.odoo is None:
            return None
        try:
            rows = self.odoo.search_read("helpdesk.ticket", [["id", "=", tid]], ["stage_id"], limit=1)
        except Exception:                                   # noqa: BLE001 - cannot read = do not move (fail closed)
            return None
        from .conventions import m2o
        return m2o(rows[0].get("stage_id")) if rows else None

    def advance_on_identify(self, rid: str, tid: int, appr: list, *, current_stage: int | None = None) -> None:
        """Automatic status move on identify: a ticket sitting in New is moved to the working (Functional) stage.
        Only a ticket actually in New is moved, and only into a SAFE (non-client-emailing) stage - never a gate."""
        cur = current_stage if current_stage is not None else self._current_stage(tid)
        if cur is None or int(cur) != self.new_stage_id:
            return
        if self.working_stage_id not in self.conventions().safe_stages:
            return                                          # fail closed: never auto-move into a client-emailing stage
        self.move_stage(rid, tid, self.working_stage_id, appr)

    def _proof_share_link(self, rid: str) -> str | None:
        """The REAL Wiki share link from this request's proof artifact (the proof pipeline uploaded it to the
        client's project + the ticket sub-folder). Only a genuine wiki.example.com/share link is used; a fake or
        blocked one (e.g. wiki.invalid) is ignored."""
        try:
            pk = self.db.latest_packet(rid) or {}
            packet = json.loads(pk["packet_json"]) if pk.get("packet_json") else {}
            if not isinstance(packet, dict):
                return None
            proof = packet.get("proof")
            link = packet.get("proof_link") or (proof.get("share_link") if isinstance(proof, dict) else None)
        except Exception:                                   # noqa: BLE001 - no/garbled packet = no link (fail safe)
            return None
        return link if isinstance(link, str) and link.startswith(WIKI_SHARE) else None

    def attach_proof_video(self, rid: str, tid: int, url: str, appr: list) -> None:
        """Write the investigator's recorded Wiki proof-video link onto the ticket (internal note).
        Only a genuine public wiki.example.com/share link is ever written (defence in depth)."""
        if not url or not str(url).startswith(WIKI_SHARE):
            return
        self._note(rid, tid, [f"[Digital Twin] Proof video (Wiki, client project / ticket folder): {url}"], appr)
