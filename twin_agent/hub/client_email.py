"""Client follow-ups by EMAIL from the Helpdesk team's alias (Sysadmin, 15 Nov 2026), mixed into ``OwnerFlow``.

Every ticket follow-up to the client (confirm, status, question, resolution) goes by email, never from a personal mailbox
and never over WhatsApp. The single instant ack stays the ONLY automatic WhatsApp message. The owner-approval flow is
unchanged: this only swaps the channel and the sender of ``OwnerFlow._send_approved``, after the approval was consumed.

Config (the hub dict), choosing the channel and the sender:
    client_channel: email                  # "email" (default) | "whatsapp" (the legacy path)
    email_from_by_team: {2: "help@example.com"}      # helpdesk team id -> sender alias
    email_from_default: "info@example.com"           # a team with no alias (e.g. team 11 "Customer Inquiries")
The configured addresses are also the ONLY senders the guard accepts (``guard._check_client_email``). When Odoo's
``helpdesk.team`` has ``use_alias`` and an ``alias_email``, it is used only if it equals THIS team's configured alias (or
the default): a team can never send from another team's alias, and Odoo data never widens the configured set.
In Odoo the email is ``message_post`` (comment, ``mail.mt_comment``) with ``email_from`` = the alias, ``partner_ids`` = the
ticket's own client only, and ``email_add_signature=False`` (no personal signature).

No email in Odoo (``NoClientEmail``): the follow-up is HELD ("client has no email in Odoo; add it, then re-approve"),
nothing is marked sent and the stage does not move. The owner is asked again, and that approval carries
``whatsapp_offer``: the owner may answer it with ``on_owner_decision(..., channel="whatsapp")``, an explicit, separate
choice for THAT one follow-up. It uses the same approval binding (nonce + hash) and the existing WhatsApp send path
(inside the 24h window, never ``automatic``), and is audited (approval doc + an Odoo note). Never automatic.

Safety (``odoo_gateway.GuardedOdoo.send_client_email``): the gateway RE-READS the stored approval (consumed by an allowed
approver, the right kind, the text equals the approved draft or the owner's edited text, not used before) and marks it
used (one approval, one email); and it re-reads the LIVE ticket: its client must still be the expected partner and no other
follower may be external. Internal followers (Odoo users) are not refused and may receive the email per their own Odoo
notification setting.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone

from ..helpdesk import guard
from .model import Inbound, detect_lang
from ..helpdesk.conventions import m2o
from .odoo_gateway import NoClientEmail, team_id_for

log = logging.getLogger("twin_agent.hub")


class ClientEmail:
    def _email_channel(self) -> bool:
        return str(self.cfg.get("client_channel", "email")).lower() == "email"

    def _email_aliases(self) -> set[str]:
        by_team = {str(k): str(v).strip() for k, v in (self.cfg.get("email_from_by_team") or {}).items()}
        out = {v for v in by_team.values() if v}
        default = str(self.cfg.get("email_from_default") or "").strip()
        return out | ({default} if default else set())

    def _email_from(self, state) -> str:
        """The sender alias for this ticket's team ('' = none configured: nothing is sent)."""
        team = team_id_for(state.analysis.track, self.conv)
        by_team = {str(k): str(v).strip() for k, v in (self.cfg.get("email_from_by_team") or {}).items()}
        own = by_team.get(str(team)) or str(self.cfg.get("email_from_default") or "").strip()
        try:
            rows = self.odoo.search_read("helpdesk.team", [["id", "=", team]], ["alias_email", "use_alias"], limit=1)
            live = str((rows[0].get("alias_email") or "")).strip() if rows and rows[0].get("use_alias") else ""
        except Exception:                                    # noqa: BLE001 - reading the alias is best effort
            live = ""
        return live if live and live.lower() == own.lower() else own

    def _send_email(self, state, doc: dict, final: str, kind: str) -> dict | None:
        """Send the APPROVED text by email. None = sent; a dict = the failure to return to the owner. Only called from
        ``_send_approved`` after ``_consume`` accepted the owner's tap; the gateway re-verifies that from the store."""
        sender = self._email_from(state)
        if not sender:
            self._hold("email_not_configured", ticket_id=state.tid,
                       detail=f"{kind} approved but no sender alias is configured (email_from_by_team / email_from_default)")
            return {"ok": False, "reason": "no_email_alias", "stage": state.stage}
        kw = dict(partner_id=state.partner_id, email_from=sender, aliases=self._email_aliases(), store=self.store,
                  nonce=doc["nonce"])
        if getattr(self.odoo, "acting", False):
            kw["approver_tg_id"] = doc.get("used_by")
        state.attempting_kind, state.replies_at_attempt = kind, len(state.replies)
        self._save(state)                                    # persisted BEFORE the send, as the WhatsApp path does
        try:
            self.odoo.send_client_email(state.tid, final, **kw)
        except guard.WriteRefused as e:                      # refused by the guard / no key / wrong identity: NOT sent
            state.attempting_kind = ""
            if isinstance(e, NoClientEmail):                 # default = HOLD; the re-asked approval offers WhatsApp
                state.email_missing = True
            self._save(state)
            log.warning("client email refused on ticket %s: %s", state.tid, e)
            self._hold("email_blocked", ticket_id=state.tid,
                       detail=f"the {kind} email was NOT sent and nothing moved: {e}")
            self._safe_ensure(state.tid)                     # ask the owner again, to approve once it is fixed
            return {"ok": False, "reason": "email_blocked", "stage": state.stage}
        except Exception as e:                               # noqa: BLE001 - outcome unknown: never auto re-sent
            state.send_unknown, state.attempting_kind = kind, ""
            self._save(state)
            self._hold("send_outcome_unknown", ticket_id=state.tid,
                       detail=f"the {kind} email ended with {type(e).__name__}: check Odoo manually; NOT resent")
            return {"ok": False, "reason": "send_unknown", "stage": state.stage}
        state.attempting_kind, state.email_missing = "", False
        return None

    # ---------------------------------------------------------------- the client's EMAIL replies come back in

    def _email_replies_pass(self) -> int:
        """Tick pass: read new Odoo chatter messages on open hub tickets and feed the CLIENT's own email replies in.
        Read-only on Odoo. Each message goes to ITS OWN ticket by id (``on_email_reply``), never through the WhatsApp
        router: it can never join another client's ticket, open a ticket, or cause an ack. Returns how many were fed."""
        if not self._email_channel():
            return 0
        fed = 0
        for st in self.all_tickets():
            pollable = (not st.keep_closed and self._within_reopen(st)) if st.stage == "resolved" else bool(st.tid)
            if pollable and st.partner_id:
                fed += self._poll_ticket_emails(st)
        return fed

    def _poll_ticket_emails(self, st) -> int:
        lock = self._wa_lock(st.wa_id)
        if not lock.acquire(blocking=False):                 # a webhook / owner tap is busy on this sender: next tick
            return 0
        try:
            return self._poll_locked(st.tid)
        finally:
            lock.release()

    def _poll_locked(self, tid: int) -> int:
        st = self.ticket(tid)
        if st is None:
            return 0
        cur = self.store.get("email_cursor", tid) or {"last": 0}
        rows = self.odoo.search_read(
            "mail.message", [["model", "=", "helpdesk.ticket"], ["res_id", "=", tid], ["id", ">=", int(cur["last"]) + 1],
                             ["message_type", "in", ["email", "comment"]]],
            ["id", "body", "message_type", "date", "subject", "email_from", "author_id"], order="id asc", limit=30)
        if not rows:
            return 0
        live = self._live_partner(tid)
        external = self._external_authors(rows, {live, int(st.partner_id)})
        fed = 0
        for r in rows:
            try:
                fed += self.on_email_reply(tid, r, live_partner=live, external=external)
            except Exception as e:                           # noqa: BLE001 - one bad message never blocks the rest
                log.error("email reply %s on ticket %s failed: %s", r.get("id"), tid, type(e).__name__)
                self._hold("email_reply_failed", ticket_id=tid, message_id=f"odoo-mail-{r.get('id')}",
                           detail=f"an email reply could not be processed ({type(e).__name__}); read it in the Odoo chatter")
            self.store.put("email_cursor", tid, {"last": int(r["id"])})
        return fed

    def _live_partner(self, tid: int):
        rows = self.odoo.search_read("helpdesk.ticket", [["id", "=", int(tid)]], ["partner_id"], limit=1)
        return m2o(rows[0].get("partner_id")) if rows else None

    def _external_authors(self, rows: list, known: set) -> set:
        """Authors (other than the ticket's client) that are EXTERNAL people: a portal/customer partner, or unreadable."""
        ids = {m2o(r.get("author_id")) for r in rows} - known - {None}
        if not ids:
            return set()
        found = {int(x["id"]): x for x in self.odoo.search_read("res.partner", [["id", "in", sorted(ids)]], ["partner_share"])}
        return {i for i in ids if i not in found or found[i].get("partner_share") is not False}

    def _held_email(self, tid: int, mid, why: str) -> int:
        self._hold("email_reply_held", ticket_id=tid, message_id=f"odoo-mail-{mid}", detail=why)
        return 0

    def on_email_reply(self, tid: int, row: dict, *, live_partner=None, external: set | None = None) -> int:
        """Feed ONE Odoo chatter message into ITS OWN ticket. 1 = attached, 0 = ignored or held (the owner is told).
        Accepted only when the author is that ticket's live client. Never opens a ticket, never acks, never touches the
        WhatsApp window (``Inbound.channel == "email"``)."""
        st = self.ticket(tid)
        if st is None or not st.partner_id:
            return 0
        author, mid = m2o(row.get("author_id")), row.get("id")
        if author is not None and author != int(st.partner_id) and author not in (external or set()):
            return 0                                         # our own staff message: not the client's
        if _auto_reply(row):
            return 0
        if live_partner is not None and live_partner != int(st.partner_id):
            return self._held_email(tid, mid, "the ticket's client changed in Odoo; an email reply was NOT attached: read the chatter")
        if author != int(st.partner_id):
            return self._held_email(tid, mid, "an email reply came from another address on the ticket; NOT attached: read the chatter")
        text = _plain(row.get("body"))
        if not text:
            return self._held_email(tid, mid, "the client's email has no readable text (image or attachment only): read the chatter")
        inbound = Inbound(message_id=f"odoo-mail-{int(mid)}", wa_id=st.wa_id, name=st.inbound.name, kind="text", text=text,
                          lang=detect_lang(text), ts=self._mail_time(row), channel="email")
        if st.stage == "resolved":
            if st.keep_closed or not self._within_reopen(st):
                return 0
            self._post_resolve(st, inbound)
        else:
            self._attach(st, inbound)
        return 1

    def _mail_time(self, row: dict) -> datetime:
        now = self.clock()
        try:
            t = datetime.fromisoformat(str(row.get("date")).replace(" ", "T")).replace(tzinfo=timezone.utc)
        except ValueError:
            return now
        return min(t, now)


_AUTO_SUBJECT = re.compile(r"(?i)out of office|automatic reply|auto[- ]?reply|autoreply|undeliver(able|ed)|delivery status "
                           r"notification|mail delivery (failed|subsystem)|returned mail|vacation|away from (the )?office")
_AUTO_SENDER = re.compile(r"(?i)mailer-daemon|postmaster|no-?reply|do-?not-?reply")


def _auto_reply(row: dict) -> bool:
    """Out-of-office / bounce / no-reply mail is not a client answer (the Auto-Submitted header is not on mail.message,
    so subject and sender patterns are used)."""
    return bool(_AUTO_SUBJECT.search(str(row.get("subject") or "")) or _AUTO_SENDER.search(str(row.get("email_from") or "")))


def _plain(body) -> str:
    """An Odoo HTML message body as plain text (tags dropped, entities decoded, blank lines collapsed, capped)."""
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", str(body or ""))
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n\s*\n+", "\n", t).strip()[:2000]
