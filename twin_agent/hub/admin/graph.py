"""The Meta Graph seam for ``whatsapp_business_management``: WABAs, their numbers and approved templates (reads), plus
two explicit WRITE actions (subscribe the app to a WABA's webhooks, start the coexistence data sync).

``GraphClient`` is a structural Protocol (like every seam in ``hub/interfaces.py``). Tests and the demo use
``FakeGraphClient``; ``graph_real.RealGraphClient`` is the real one (only built when the config says ``real``).
``BlockedGraphClient`` is the safe default and says so in plain words. The writes are never called from a read path:
only the two POST routes in ``app.py``, which are off unless ``allow_meta_writes`` is set.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


class GraphError(Exception):
    """A Graph call failed. The message is shown to staff as-is, so it must never carry a token or a response body.
    A plain GraphError (timeout, network) is UNCERTAIN for a write: it may or may not have gone through."""


class LocalRefusal(GraphError):
    """Refused here, before anything was sent to Meta (bad id, business not connected, sync type not allowed)."""


class MetaRefused(GraphError):
    """Meta answered with a definite refusal (HTTP 4xx/5xx, or ``success: false``)."""


class AmbiguousAssets(GraphError):
    """The token grants several WABAs and nothing tells us which one this sign-up is for (yet)."""


@runtime_checkable
class GraphClient(Protocol):
    def list_wabas(self) -> list[dict]:
        """[{business, id, name, phone_numbers: [{id, display_phone_number, verified_name, status, quality_rating,
        webhooks_subscribed}]}] for the WABAs each business shared with the app. One business failing must not hide
        the others: its entry is ``{business, id: "", name, phone_numbers: [], error: "<fixed sentence>"}``."""

    def list_templates(self, waba_id: str, business: str = "") -> list[dict]:
        """[{name, language, category, status, body}] message templates of one WABA."""


SYNC_TYPES = ("history", "smb_app_state_sync")      # coexistence: chat history, and contacts / app state


@runtime_checkable
class GraphWriter(Protocol):
    def subscribe_app(self, waba_id: str, business: str = "") -> None:
        """POST /{waba_id}/subscribed_apps. One attempt, never retried. Raises GraphError."""

    def start_sync(self, phone_number_id: str, sync_type: str, business: str = "") -> None:
        """POST /{phone_number_id}/smb_app_data. One attempt, never retried. Raises GraphError."""


class BlockedGraphClient:
    """Default when no Graph credentials are wired: every read blocks with a plain reason (never a fake)."""

    def list_wabas(self) -> list[dict]:
        raise GraphError("The Meta Graph client is not configured on this server.")

    def list_templates(self, waba_id: str, business: str = "") -> list[dict]:
        raise GraphError("The Meta Graph client is not configured on this server.")

    def subscribe_app(self, waba_id: str, business: str = "") -> None:
        raise GraphError("The Meta Graph client is not configured on this server.")

    def start_sync(self, phone_number_id: str, sync_type: str, business: str = "") -> None:
        raise GraphError("The Meta Graph client is not configured on this server.")


class FakeGraphClient:
    """Fictional demo data only: 'Demo Bakery' and a 555-01xx test number."""

    def __init__(self, wabas: list[dict] | None = None, templates: dict[str, list[dict]] | None = None,
                 *, fail: bool = False):
        self.fail = fail
        self.wabas = wabas if wabas is not None else [{
            "business": "demo-bakery", "id": "1000000000001", "name": "Demo Bakery",
            "selected_phone": "1000000000002",
            "phone_numbers": [{"id": "1000000000002", "display_phone_number": "+1 555 010 4321",
                               "verified_name": "Demo Bakery", "status": "CONNECTED", "quality_rating": "GREEN",
                               "webhooks_subscribed": True}]}]
        self.templates = templates if templates is not None else {"1000000000001": [
            {"name": "order_update", "language": "en", "category": "UTILITY", "status": "APPROVED",
             "body": "Hello {{1}}, your order {{2}} has an update."},
            {"name": "support_followup", "language": "en", "category": "UTILITY", "status": "APPROVED",
             "body": "Hello {{1}}, we are following up on your support request."},
            {"name": "seasonal_offer", "language": "en", "category": "MARKETING", "status": "PENDING",
             "body": "Not approved yet: must not be listed."}]}
        self.calls: list[str] = []

    def list_wabas(self) -> list[dict]:
        self.calls.append("list_wabas")
        if self.fail:
            raise GraphError("Could not read from Meta.")
        return [dict(w) for w in self.wabas]

    def list_templates(self, waba_id: str, business: str = "") -> list[dict]:
        self.calls.append(f"list_templates:{waba_id}")
        if self.fail:
            raise GraphError("Could not read from Meta.")
        return [dict(t) for t in self.templates.get(waba_id, [])]

    def subscribe_app(self, waba_id: str, business: str = "") -> None:
        self.calls.append(f"subscribe_app:{waba_id}:{business}")
        if self.fail:
            raise GraphError("Could not write to Meta.")

    def start_sync(self, phone_number_id: str, sync_type: str, business: str = "") -> None:
        self.calls.append(f"start_sync:{phone_number_id}:{sync_type}:{business}")
        if self.fail:
            raise GraphError("Could not write to Meta.")
