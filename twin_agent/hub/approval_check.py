"""The gateway's own re-check of a stored owner approval (built INSIDE ``GuardedOdoo.send_client_email``, never handed in
by the caller). ``check`` re-reads the approval doc; ``claim`` marks it used so one approval can send one email."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from ..helpdesk import guard

CLIENT_KINDS = ("confirm", "resolve", "reply", "status")      # kinds that send to the customer (24h approval TTL)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _consumed(used_at) -> bool:
    if not isinstance(used_at, str):
        return False
    try:
        datetime.fromisoformat(used_at)
    except ValueError:                                       # "expired" / "superseded" / "request_failed" markers
        return False
    return True


class StoredApproval:
    def __init__(self, store, nonce: str, ticket_id: int):
        self.store, self.nonce, self.tid = store, str(nonce), int(ticket_id)

    def check(self, body: str) -> str:
        """Raise ``WriteRefused`` unless ``body`` is exactly what an allowed approver approved for THIS ticket and the
        approval was not used yet. Returns the approved hash. When the owner edited the draft only the edited text passes."""
        doc = self.store.get("approvals", self.nonce)
        if not doc:
            raise guard.WriteRefused("client email: no stored approval for this draft")
        if int(doc.get("ticket_id") or 0) != self.tid:
            raise guard.WriteRefused("client email: the approval belongs to another ticket")
        if not _consumed(doc.get("used_at")) or doc.get("approved") is not True:
            raise guard.WriteRefused("client email: the approval was not given (consumed and approved)")
        if int(doc.get("used_by") or 0) <= 0 or int(doc["used_by"]) not in [int(a) for a in doc.get("allowed") or []]:
            raise guard.WriteRefused("client email: the approval was not given by an allowed approver")
        if doc.get("kind") not in CLIENT_KINDS:
            raise guard.WriteRefused("client email: this approval is not for a client message")
        if doc.get("emailed_at"):
            raise guard.WriteRefused("client email: this approval was already used to send an email")
        want = doc.get("edited_hash") or doc.get("draft_hash")
        if not body.strip() or not want or sha(body) != want:
            raise guard.WriteRefused("client email: the text is not what the owner approved")
        return want

    def claim(self) -> None:
        verdict: dict = {}

        def fn(doc: dict) -> dict:
            if doc.get("emailed_at"):
                verdict["used"] = True
            else:
                doc["emailed_at"] = datetime.now(timezone.utc).isoformat()
            return doc

        if self.store.update("approvals", self.nonce, fn) is None or verdict.get("used"):
            raise guard.WriteRefused("client email: this approval was already used to send an email")
