"""The thin comms path: inbound message -> router -> (ack) -> maybe_launch -> registry update.

This is the glue, kept deliberately small. It NEVER sends to the world and never writes state.db: it routes
(comms_router), prepares/launches a visible lane session (session_launch), records the lane (registry), and
emits an ACK + gate/reject notice through an injected `notify` callback. In the listener that callback posts a
CARD to Principal (not a world send), so the single Sender stays the only thing that posts. The real seat-thread ack
(via the Sender) is the next step - see STATUS.
"""
from __future__ import annotations

from typing import Callable

from .bus import Topics
from .comms_router import CommsRouter, InboundMessage
from .registry import LaneRegistry
from .session_launch import maybe_launch

Notify = Callable[[str, str], None]        # (tag, text) -> None; tag in {"ack","gate","reject"}


class Backbone:
    def __init__(self, router: CommsRouter, registry: LaneRegistry, *, live_launch: bool = False,
                 notify: Notify | None = None, seat: str = "principal-twin", clock=None, spawn=None, wake=None):
        self.router, self.registry = router, registry
        self.live_launch = bool(live_launch)
        self.notify = notify
        self.seat = seat
        self.clock = clock
        self.spawn = spawn                     # injectable so a test (or a non-Windows box) never opens a real tab
        self.wake = wake                       # injectable auto-wake hook (correction #10); live-only, no-op default

    def _notify(self, tag: str, text: str) -> None:
        if self.notify is not None:
            self.notify(tag, text)

    def handle(self, msg: InboundMessage) -> dict:
        d = self.router.route(msg)
        out = {"action": d.action, "category": d.category, "person": d.person, "lane": d.lane,
               "authorised": d.authorised, "reason": d.reason}
        who = d.person or (msg.sender_name or "someone")
        if d.action == "ignore":
            return out
        if d.action == "reject":
            self._notify("reject", f"[{self.seat}] ignored a {d.category} from {who}: {d.reason}")
            return out
        if d.action == "human_gate":
            # A decision/gate is relayed to the human, never auto-executed (doc 15).
            self._notify("gate", f"[{self.seat}] a {d.category} from {who} needs a human: "
                                 f"\"{(msg.text or '').strip()[:160]}\" - routing to the gate, not acting on it.")
            return out
        # launch
        res = maybe_launch(d.lane, msg.text, live_launch=self.live_launch, title=d.lane,
                           registry=self.registry, source=f"{who}", clock=self.clock, spawn=self.spawn,
                           wake=self.wake)
        if res.action == "inbox_only":
            # keep the open tab's lifecycle status; just refresh the last_task hint on Principal's board.
            self.registry.note_task(d.lane, msg.text)
        else:
            self.registry.add(d.lane, title=d.lane, cmd=res.cmd_str, launched=res.launched,
                              category=d.category, person=d.person or "", last_task=msg.text)
        verb = {"launched": "opened a visible session for",
                "inbox_only": "appended the task to the INBOX of the open session for (it waits to be prompted)",
                "prepared": "prepared (observe) a session for"}.get(res.action, "routed")
        self._notify("ack", f"[{self.seat}] Received. {verb} lane {d.lane}"
                            + (f"\n{res.cmd_str}" if res.cmd else ""))
        out.update(launch_action=res.action, launched=res.launched, cmd=res.cmd,
                   inbox=str(res.inbox_path), resume=res.resume, woken=res.woken)
        return out


def wire_comms(flow):
    """Listener wiring (OBSERVE-safe, OFF by default): subscribe the backbone to inbound events when
    comms.enabled. It only acts on messages from a KNOWN internal account id; live_launch needs run_mode live
    AND comms.live_launch (apply_mode('observe') forces it off). Returns the Backbone, or None when disabled."""
    cfg = flow.cfg
    comms = cfg.get("comms") or {}
    if not comms.get("enabled"):
        return None
    router = CommsRouter(cfg)
    registry = LaneRegistry(clock=flow.clock)
    live = cfg.get("run_mode") == "live" and bool(comms.get("live_launch"))
    # The backbone acts ONLY in explicitly listed comms chats (the Acme Team group / topics). Empty = nowhere,
    # even when enabled - so it never fires on a client channel (Odoo x Globex) where Principal/Supervisor reply to clients.
    comms_chats = {str(c) for c in (comms.get("chat_ids") or [])}

    def notify(tag: str, text: str) -> None:
        from .models import CardKind
        flow.card(CardKind.info, text)

    bb = Backbone(router, registry, live_launch=live, notify=notify, clock=flow.clock)

    async def on_event_comms(ev) -> None:
        if str(getattr(ev, "chat_id", "")) not in comms_chats:   # not a comms chat: leave it to the support flow
            return
        msg = InboundMessage.from_event(ev, cfg)
        if router.person_of(msg.sender_id) is None:       # not an internal account we know: leave it alone
            return
        bb.handle(msg)

    flow.bus.subscribe(Topics.EVENT_IN, on_event_comms)
    flow.comms = bb
    return bb
