"""Comms backbone router: classify + authorise (by account id) + lane routing. Pure, deterministic."""
from twin_agent.comms_router import CommsRouter, InboundMessage, classify, lane_for

PRINCIPAL, SUPERVISOR, RILEY, EXECUTIVE, STRANGER = 111, 222, 333, 444, 999
CFG = {"people": {"principal_tg_user_id": PRINCIPAL, "supervisor_tg_user_id": SUPERVISOR,
                  "riley_tg_user_id": RILEY, "executive_tg_user_id": EXECUTIVE}}


def r():
    return CommsRouter(CFG)


# ---------------------------------------------------------------- classify (word boundaries)
def test_classify_task_inquiry_chatter_gate_decision():
    assert classify("please investigate the stray-in bug") == "task"     # 'investigate' must NOT read as 'gate'
    assert classify("Done for today, thanks") == "chatter"               # 'done' must NOT read as task 'do'
    assert classify("how do I export the PO?") == "inquiry"
    assert classify("push to prod now") == "gate"
    assert classify("which option should we go with?") == "decision"
    assert classify("👍") == "chatter"


def test_classify_arabic():
    assert classify("عايز تظبط التقرير ده") == "task"
    assert classify("ليه مش شغال؟") == "inquiry"


# ---------------------------------------------------------------- authorise by ACCOUNT id, not name/seat
def test_task_from_authorised_person_launches_to_a_lane():
    d = r().route(InboundMessage("please fix the stray-in line", sender_id=SUPERVISOR, topic="Delivery"))
    assert d.action == "launch" and d.authorised and d.person == "supervisor"
    assert d.lane == "dept-delivery" and d.to_gate is False


def test_ticket_ref_makes_a_per_ticket_lane():
    d = r().route(InboundMessage("please update #2636", sender_id=SUPERVISOR, refs=("#2636",)))
    assert d.action == "launch" and d.lane == "ticket-2636"


def test_unauthorised_unknown_sender_is_rejected():
    d = r().route(InboundMessage("please fix prod data", sender_id=STRANGER))
    assert d.action == "reject" and d.authorised is False and d.person is None


def test_zero_id_is_never_authorised_even_with_a_matching_name():
    # A default Sender has tg_id=0 and the text can claim any name: neither grants authority (doc 15).
    d = r().route(InboundMessage("please fix it", sender_id=0, sender_name="Supervisor"))
    assert d.action == "reject" and d.person is None


def test_person_known_but_category_not_allowed_is_rejected():
    # Supervisor may order ticket work + gates, not a strategy DECISION (doc 10 who-can-order).
    d = r().route(InboundMessage("let's decide which option to go with", sender_id=SUPERVISOR))
    assert d.category == "decision" and d.action == "reject"


# ---------------------------------------------------------------- decision / gate go to the human gate
def test_decision_routes_to_human_gate_not_auto_execute():
    d = r().route(InboundMessage("we need to decide the Q4 budget approval", sender_id=RILEY))
    assert d.category == "decision" and d.action == "human_gate" and d.to_gate and d.lane is None


def test_gate_routes_to_human_gate():
    d = r().route(InboundMessage("approve the push and go live", sender_id=SUPERVISOR))
    assert d.category == "gate" and d.action == "human_gate" and d.to_gate and d.lane is None


def test_chatter_is_ignored_without_auth():
    d = r().route(InboundMessage("thanks a lot!", sender_id=STRANGER))
    assert d.action == "ignore" and d.category == "chatter"


def test_lane_for_falls_back_to_person_department():
    assert lane_for(InboundMessage("x", sender_id=RILEY), "riley") == "dept-sales"
    assert lane_for(InboundMessage("x"), None) == "general"
