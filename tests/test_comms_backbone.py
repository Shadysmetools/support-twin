"""The thin comms path: router -> (ack) -> maybe_launch -> registry, and the listener wire (wire_comms).
OBSERVE-safe; never sends to the world; never opens a real tab in a test (spawn is injected)."""
from types import SimpleNamespace

from twin_agent.bus import Bus, Topics
from twin_agent.comms_backbone import Backbone, wire_comms
from twin_agent.comms_router import CommsRouter, InboundMessage
from twin_agent.registry import PREPARED, LaneRegistry

PRINCIPAL, SUPERVISOR, RILEY, STRANGER = 111, 222, 333, 999
GROUP = -100777
CFG = {"people": {"principal_tg_user_id": PRINCIPAL, "supervisor_tg_user_id": SUPERVISOR, "riley_tg_user_id": RILEY}}


def _backbone(clock, *, live=False, spawn=None, wake=None):
    notes = []
    reg = LaneRegistry(clock=clock)
    bb = Backbone(CommsRouter(CFG), reg, live_launch=live, spawn=spawn, wake=wake,
                  notify=lambda tag, text: notes.append((tag, text)), clock=clock)
    return bb, reg, notes


def test_task_prepares_lane_acks_and_registers_without_sending(tmp_home, clock):
    bb, reg, notes = _backbone(clock)                     # observe
    out = bb.handle(InboundMessage("please fix the stray-IN line", sender_id=SUPERVISOR, topic="Delivery"))
    assert out["action"] == "launch" and out["launched"] is False and out["cmd"][0] == "cmd.exe"
    row = reg.get("dept-delivery")
    assert row and row["status"] == PREPARED              # observe prepares; it does NOT claim a real open tab
    assert any(tag == "ack" and "Received" in text for tag, text in notes)
    inbox = tmp_home / "data" / "lanes" / "dept-delivery" / "INBOX.md"
    assert inbox.exists() and "stray-IN line" in inbox.read_text(encoding="utf-8")


def test_observe_prepared_lane_still_launches_when_live(tmp_home, clock):
    # Regression: an observe-prepared lane must NOT lock out the later live launch (cf. helpdesk prepared-link bug).
    obs, reg, _ = _backbone(clock)
    obs.handle(InboundMessage("please fix A", sender_id=SUPERVISOR, topic="Delivery"))
    assert reg.is_open("dept-delivery") is False          # prepared, not open
    calls = []
    live, _reg2, _ = _backbone(clock, live=True, spawn=lambda cmd: calls.append(cmd) or True)
    out = live.handle(InboundMessage("please fix B now", sender_id=SUPERVISOR, topic="Delivery"))
    assert len(calls) == 1 and out["launched"] is True and reg.is_open("dept-delivery")


def test_second_task_to_an_OPEN_lane_is_inbox_only(tmp_home, clock):
    calls = []
    bb, reg, _ = _backbone(clock, live=True, spawn=lambda cmd: calls.append(cmd) or True)
    bb.handle(InboundMessage("please fix A", sender_id=SUPERVISOR, topic="Delivery"))     # launches a real tab
    out = bb.handle(InboundMessage("please also fix B", sender_id=SUPERVISOR, topic="Delivery"))
    assert out["launch_action"] == "inbox_only" and out["cmd"] is None
    assert len(calls) == 1                                 # no second tab opened on the same session


def test_inbox_only_live_wakes_the_open_session_and_refreshes_last_task(tmp_home, clock):
    woke = []
    bb, reg, _ = _backbone(clock, live=True, spawn=lambda cmd: True, wake=lambda lane: woke.append(lane) or True)
    bb.handle(InboundMessage("please fix A", sender_id=SUPERVISOR, topic="Delivery"))          # opens a real tab
    out = bb.handle(InboundMessage("please also fix the stray-IN line now", sender_id=SUPERVISOR, topic="Delivery"))
    assert out["launch_action"] == "inbox_only" and out["woken"] is True
    assert woke == ["dept-delivery"]                                                       # the open session woke
    assert "stray-IN line" in reg.get("dept-delivery")["last_task"]                        # board shows the latest task


def test_launch_records_last_task_on_the_board(tmp_home, clock):
    bb, reg, _ = _backbone(clock)                                                          # observe
    bb.handle(InboundMessage("please fix the PO total", sender_id=SUPERVISOR, topic="Delivery"))
    assert "PO total" in reg.get("dept-delivery")["last_task"]


def test_decision_routes_to_gate_and_opens_no_lane(tmp_home, clock):
    bb, reg, notes = _backbone(clock)
    out = bb.handle(InboundMessage("we need to decide the budget approval", sender_id=RILEY))
    assert out["action"] == "human_gate" and out["lane"] is None
    assert reg.list_open() == [] and any(tag == "gate" for tag, _ in notes)


def test_unauthorised_sender_rejected_no_lane(tmp_home, clock):
    bb, reg, notes = _backbone(clock)
    out = bb.handle(InboundMessage("please push to prod", sender_id=STRANGER))
    assert out["action"] == "reject" and reg.list_open() == []
    assert any(tag == "reject" for tag, _ in notes)


def test_chatter_is_silently_ignored(tmp_home, clock):
    bb, reg, notes = _backbone(clock)
    out = bb.handle(InboundMessage("thanks!", sender_id=SUPERVISOR))
    assert out["action"] == "ignore" and reg.list_open() == [] and notes == []


# ---------------------------------------------------------------- from_event + wire_comms
def test_from_event_strips_department_suffix_and_reads_account_id():
    ev = SimpleNamespace(text="please fix X", channel="telegram_group", topic="Delivery Department",
                         sender=SimpleNamespace(tg_id=SUPERVISOR, name="Supervisor"), refs=())
    msg = InboundMessage.from_event(ev, CFG)
    assert msg.topic == "Delivery" and msg.sender_id == SUPERVISOR


class _FakeFlow:
    def __init__(self, cfg, clock):
        self.cfg, self.clock, self.bus = cfg, clock, Bus()
        self.cards = []

    def card(self, kind, text, request_id=None, **kw):
        self.cards.append(text)


def _event(chat_id, sender_id, text="please fix the PO", topic="Delivery Department"):
    return SimpleNamespace(text=text, channel="telegram_group", topic=topic, chat_id=chat_id,
                           sender=SimpleNamespace(tg_id=sender_id, name="x"), refs=())


async def _publish(flow, ev):
    flow.bus.publish(Topics.EVENT_IN, ev)
    await flow.bus.drain()


def test_wire_comms_disabled_returns_none(tmp_home, clock):
    flow = _FakeFlow({**CFG, "comms": {"enabled": False}}, clock)
    assert wire_comms(flow) is None


def test_wire_comms_ignores_a_chat_not_in_comms_chat_ids(tmp_home, clock):
    import asyncio
    flow = _FakeFlow({**CFG, "comms": {"enabled": True, "chat_ids": [GROUP]}}, clock)
    assert wire_comms(flow) is not None
    asyncio.run(_publish(flow, _event(chat_id=-100999, sender_id=SUPERVISOR)))   # a client channel, not comms
    assert flow.cards == [] and not (tmp_home / "data" / "lanes" / "dept-delivery").exists()


def test_wire_comms_acts_in_a_comms_chat_from_a_known_id(tmp_home, clock):
    import asyncio
    flow = _FakeFlow({**CFG, "comms": {"enabled": True, "chat_ids": [GROUP]}}, clock)
    wire_comms(flow)
    asyncio.run(_publish(flow, _event(chat_id=GROUP, sender_id=SUPERVISOR)))
    assert flow.cards and "Received" in flow.cards[0]
    assert (tmp_home / "data" / "lanes" / "dept-delivery" / "INBOX.md").exists()
