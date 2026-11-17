"""Ask-all-sessions / route-by-knowledge (MEETING §2.6, correction #12): on each message Digital Twin can query
ALL open sessions ("who knows this?") and route to the one that HAS the knowledge, not a blind lane. The per-session
query is INJECTABLE, so no real session/process is ever touched in a test."""
from twin_agent.registry import LaneRegistry
from twin_agent.session_query import ask_sessions, best_session_for, default_asker


def _reg(clock):
    reg = LaneRegistry(clock=clock)
    reg.add("dept-delivery", launched=True, tags=["po", "delivery", "stray-in"])
    reg.add("dept-sales", launched=True, tags=["quote", "pricing"])
    reg.add("dept-content", launched=False, tags=["blog"])     # prepared, NOT open
    return reg


def test_ask_sessions_queries_only_open_lanes_with_injected_asker(tmp_home, clock):
    seen = []
    def asker(lane, question):
        seen.append(lane)
        return f"{lane} says: I can look at '{question}'" if lane == "dept-delivery" else None
    answers = ask_sessions("the PO total is wrong", _reg(clock), asker=asker)
    lanes = {lane for lane, _ in answers}
    assert lanes == {"dept-delivery", "dept-sales"}            # only OPEN lanes, prepared dept-content skipped
    assert "dept-content" not in seen
    answered = {lane: ans for lane, ans in answers}
    assert "PO total" in answered["dept-delivery"] and answered["dept-sales"] is None


def test_ask_sessions_is_fail_closed_per_lane(tmp_home, clock):
    def asker(lane, question):
        if lane == "dept-sales":
            raise RuntimeError("that session is wedged")
        return "ok"
    answers = dict(ask_sessions("q", _reg(clock), asker=asker))
    assert answers["dept-sales"] is None                      # a wedged session yields None, never crashes
    assert answers["dept-delivery"] == "ok"                   # and the others are still queried


def test_default_asker_is_a_documented_stub_returning_none(tmp_home, clock):
    assert default_asker("dept-delivery", "anything") is None
    # With no real inter-session RPC wired yet, ask_sessions over the default stub answers None everywhere.
    answers = ask_sessions("q", _reg(clock))
    assert answers and all(ans is None for _, ans in answers)


def test_best_session_for_picks_the_session_that_answers(tmp_home, clock):
    def asker(lane, question):
        return "yes, that's my ticket" if lane == "dept-sales" else None
    assert best_session_for("what's the quote for Globex?", _reg(clock), asker=asker) == "dept-sales"


def test_best_session_for_falls_back_to_knowledge_tags(tmp_home, clock):
    # No asker answer (default stub): route by the knowledge hint - the lane whose tags match the topic.
    assert best_session_for("the stray-in line on the PO", _reg(clock)) == "dept-delivery"


def test_best_session_for_is_none_when_nobody_knows(tmp_home, clock):
    # Nobody answers and no tag matches: return None so the caller opens a fresh lane instead of a blind one.
    assert best_session_for("quarterly board offsite logistics", _reg(clock)) is None
