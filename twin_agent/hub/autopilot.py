"""TEST-CLIENT AUTOPILOT (Sysadmin 2026-11-17: "the full cycle as One, not human approval", for TEST CLIENTS ONLY).

When a ticket's sender is an ACTIVE test client (an open ``test_clients`` / ``test_numbers`` window, checked when the
draft is requested AND again at send time), the hub user ``one`` approves the client-facing draft through the EXISTING
approval book (``owner_flow.on_owner_decision`` -> ``approval_check.StoredApproval``): same bound nonce, same hash, same
single use, same send path. There is no new send path. A real client is never touched: with no open window the
autopilot does not exist for that ticket and the draft waits for a person, exactly as before.

The gate (any failure = a normal human approval request, plus a hold reason in the log and the audit):
  * the kind is ``confirm`` or ``reply``, the client writes English, the draft is English;
  * deterministic checks: not empty, length bounds, no leaked template field labels, not a raw paste of the client's
    message, and the persona wording lint (``wording_lint.lint``) finds nothing;
  * with a REAL judge: it scored the draft, not flagged, at or above the judge's threshold. The production judge is a
    fake today (``judge.real`` is False), so the deterministic checks are the gate there.

After a send to a test client the ticket is closed as Cancelled through the guard purpose ``test_client_close``.
Attribution everywhere: "Approved automatically by One (test client)". Audit role: "autopilot (test client)".
"""
from __future__ import annotations

import logging
import re

from .ticket_text import SECTION_ORDER

log = logging.getLogger("twin_agent.hub")

AUTOPILOT_USER = "one"
AUTOPILOT_ROLE = "autopilot (test client)"
AUTOPILOT_LABEL = "Approved automatically by One (test client)"
AUTOPILOT_KINDS = ("confirm", "reply")
MIN_LEN, MAX_LEN = 30, 1500
PASTE_RATIO = 0.6
LEAKED_LABELS = tuple(l.lower() for l in (*SECTION_ORDER, "What the client said", "What the client expects"))

from .wording_lint import lint as _lint                      # lint(text, *, client_text: str, lang) -> list[str]


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", str(text or "").lower()).strip()


def deterministic_problems(draft: str, client_message: str) -> list[str]:
    """Plain reasons a draft may not be auto-approved (pure; no store, no network). Reasons never contain the text."""
    out: list[str] = []
    text = str(draft or "")
    if not text.strip():
        return ["the draft is empty"]
    if not MIN_LEN <= len(text.strip()) <= MAX_LEN:
        out.append(f"the draft length is outside {MIN_LEN}..{MAX_LEN} characters")
    low = text.lower()
    if any(label in low for label in LEAKED_LABELS):
        out.append("the draft leaks a template field label")
    if "[[" in text or ("{" in text and "}" in text):
        out.append("the draft has an unfilled placeholder")
    if re.search(r"[؀-ۿ]", text):
        out.append("the draft is not English (client messages are English only)")
    d, c = _norm(text), _norm(client_message)
    if c and len(c) >= 12 and (d == c or (c in d and len(c) / max(len(d), 1) > PASTE_RATIO)):
        out.append("the draft is a raw paste of the client's message")
    return out


def lint_problems(draft: str, client_message: str = "") -> list[str]:
    try:
        found = _lint(draft, client_text=str(client_message or ""), lang="en")
    except Exception as e:                                   # noqa: BLE001 - a lint that cannot run is a human's call
        return [f"the wording lint could not run ({type(e).__name__})"]
    return [f"wording lint: {f}" for f in (found or [])]


def judge_problems(judge, j: dict | None) -> list[str]:
    """With a REAL judge the draft must have been scored, not flagged and at or above the threshold. A fake judge's
    score is never trusted (``real`` False): the deterministic checks stand in."""
    if not getattr(judge, "real", False):
        return []
    if not j or j.get("unjudged") or j.get("flag") or j.get("aggregate") is None:
        return ["the real judge did not clear the draft"]
    if float(j["aggregate"]) < float(getattr(judge, "threshold", 0.7)):
        return ["the judge score is below the threshold"]
    return []


def is_autopilot(doc: dict | None) -> bool:
    """True only for an approval ``autopilot_approve`` made. A human approving as the hub user ``one`` is NOT one: the
    flag is set inside ``_consume`` only when ``autopilot=True`` is passed, which no admin/HTTP path does."""
    return bool(doc) and doc.get("autopilot_approved") is True


class AutopilotMixin:
    """Mixed into ``ProductionHub`` ahead of ``Hub``. Needs ``self.windows`` (test-client windows), ``self.prod`` and the
    ``OwnerFlow`` surface. Without windows every method is a no-op: nothing here can touch a real client."""

    # ---- eligibility, called by OwnerFlow._request_approval
    def _autopilot_check(self, state, kind: str, draft: str, j: dict | None) -> dict | None:
        """None = not a test client (the autopilot does not exist for this ticket). Else {"ok": bool, "reason": str}."""
        w = self._autopilot_window(state)
        if w is None:
            return None
        reasons: list[str] = []
        if kind not in AUTOPILOT_KINDS:
            reasons.append(f"a {kind} message always needs a person")
        if str(getattr(state, "lang", "en")) != "en":
            reasons.append("the client does not write English")
        reasons += deterministic_problems(draft, getattr(state.inbound, "text", "") or "")
        reasons += lint_problems(draft, getattr(state.inbound, "text", "") or "") + judge_problems(self.judge, j)
        if not self._autopilot_contact_ok(state, w):
            reasons.append("the ticket's client is not the test client's own contact")
        if reasons:
            self._autopilot_audit(state.tid, "held", "held", "; ".join(reasons))
            log.warning("autopilot held ticket %s for a person: %s", state.tid, "; ".join(reasons))
        return {"ok": not reasons, "reason": "; ".join(reasons)}

    def _autopilot_window(self, state):
        wins = getattr(self, "windows", None)
        return wins.active(state.wa_id) if wins is not None else None

    def _autopilot_contact_ok(self, state, w) -> bool:
        try:
            return bool(state.partner_id) and int(state.partner_id) == self.windows.contact_id(self.k_odoo.odoo, w)
        except Exception as e:                               # noqa: BLE001 - fail closed: a person decides
            log.warning("autopilot could not confirm the test contact: %s", type(e).__name__)
            return False

    def _autopilot_audit(self, tid: int, method: str, outcome: str, reason: str = "") -> None:
        from .store import audit_append
        try:
            audit_append(self.store, {"acting": "hub", "role": AUTOPILOT_ROLE, "on_behalf_of": AUTOPILOT_USER,
                                      "model": "helpdesk.ticket", "method": method, "ticket_id": int(tid),
                                      "outcome": outcome, "reason": reason[:300]}, self.clock())
        except Exception as e:                               # noqa: BLE001 - the audit must never break the flow
            log.error("autopilot audit write failed: %s", type(e).__name__)

    # ---- the approval itself (through the existing approval book)
    def autopilot_sweep(self) -> int:
        """Approve every live autopilot-flagged draft. Safe to call often. Returns how many were approved."""
        n = 0
        for doc in list(self.store.scan("approvals")):
            if doc.get("autopilot") is True and not doc.get("used_at"):
                try:
                    n += 1 if self.autopilot_approve(str(doc["nonce"])).get("ok") else 0
                except Exception as e:                       # noqa: BLE001 - one bad ticket never blocks the others
                    log.error("autopilot failed on ticket %s: %s", doc.get("ticket_id"), type(e).__name__)
        return n

    def _autopilot_revoke(self, doc: dict, why: str) -> dict:
        """The flag comes off, so it is never retried: the draft waits for a person."""
        self.store.update("approvals", doc["nonce"], lambda d: {**d, "autopilot": False})
        self._autopilot_audit(doc["ticket_id"], "refused", "refused", why)
        log.warning("autopilot refused ticket %s: %s", doc["ticket_id"], why)
        return {"ok": False, "reason": why}

    def autopilot_approve(self, nonce: str) -> dict:
        doc = self.store.get("approvals", nonce)
        state = self.ticket(doc["ticket_id"]) if doc else None
        if not doc or state is None or doc.get("used_at") or doc.get("kind") not in AUTOPILOT_KINDS \
                or state.pending_nonce != nonce:
            return {"ok": False, "reason": "not_live"}
        w = self._autopilot_window(state)
        if w is None:
            return self._autopilot_revoke(doc, "the test client window is closed")
        if self.prod.staff_approver_id not in [int(a) for a in doc.get("allowed") or []]:
            return self._autopilot_revoke(doc, "no configured approver id to act through")
        if str(getattr(state, "lang", "en")) != "en" or not self._autopilot_contact_ok(state, w):
            return self._autopilot_revoke(doc, "the ticket is no longer the test client's own English ticket")
        again = deterministic_problems(doc["draft"], state.inbound.text or "") + lint_problems(doc["draft"], state.inbound.text or "")
        if again:
            return self._autopilot_revoke(doc, "; ".join(again))
        res = self.on_owner_decision(nonce, self.prod.staff_approver_id, approved=True, by=AUTOPILOT_USER,
                                     by_name=AUTOPILOT_LABEL, autopilot=True)
        self._autopilot_audit(state.tid, "approve", "ok" if res.get("ok") else "refused", str(res.get("reason") or ""))
        if res.get("ok") and res.get("sent"):
            try:
                self._post_static(state, AUTOPILOT_LABEL)       # best effort: a failed post never skips the close
            except Exception as e:                           # noqa: BLE001
                log.error("autopilot attribution post failed on ticket %s: %s", state.tid, type(e).__name__)
            self._autopilot_close(state.tid)
        return res

    # ---- the window must still be open when the send happens
    def _send_approved(self, state, doc: dict, text, now):
        if doc.get("autopilot_approved") is True and self._autopilot_window(state) is None:
            self._autopilot_audit(state.tid, "refused", "refused", "the test client window closed before the send")
            log.warning("autopilot send refused on ticket %s: the window closed", state.tid)
            self._safe_ensure(state.tid)                     # a person gets the draft again
            return {"ok": False, "reason": "test_window_closed"}
        return super()._send_approved(state, doc, text, now)

    # ---- the close rail
    def _autopilot_close(self, tid: int) -> None:
        stage = int(getattr(self.prod, "test_client_close_stage", 0) or 0)
        if stage <= 0:
            log.info("autopilot: no close stage configured, ticket %s stays open", tid)
            return
        try:
            self.odoo.close_test_client_ticket(int(tid))
        except Exception as e:                               # noqa: BLE001 - the reply is already out; never re-send
            self._autopilot_audit(tid, "close", "refused", type(e).__name__)
            log.warning("autopilot could not close ticket %s: %s", tid, type(e).__name__)
            return
        state = self.ticket(tid)
        if state is not None:                               # the hub ticket is closed too: the next message opens a NEW ticket
            state.history.append("AUTOPILOT-CLOSE")
            state.stage, state.resolved_at, state.keep_closed = "resolved", self.clock().isoformat(), True
            state.pending_nonce, state.pending_kind, state.needs_owner_text = "", "", False
            self._save(state)
        self._autopilot_audit(tid, "close", "ok", f"moved to stage {stage}")
        self._note(int(tid), "[Hub] Test client: the reply was approved and the ticket closed automatically by One.")
