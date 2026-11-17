"""State machine, enums and data shapes (ARCHITECT foundation)."""
from datetime import datetime

import pytest

from twin_agent.clock import CAIRO
from twin_agent.models import (
    LANE_FOR_KIND, OVERLAY, TERMINAL, TRANSITIONS, Channel, Dial, Event, Lane, OutboundMessage, OutKind,
    State, can_transition, is_open, parse_ts, utc_iso,
)
from twin_agent.testing.fakes import make_event


def test_every_state_has_a_transition_row():
    assert set(TRANSITIONS) == set(State)


def test_happy_path_t2a():
    path = [State.NEW, State.TRIAGED, State.AWAITING_FIRST_REPLY, State.DIAGNOSED, State.PROPOSED,
            State.FIX_PENDING, State.VERIFYING, State.PROOF_SENT, State.CLOSED_PROVEN]
    for a, b in zip(path, path[1:]):
        assert can_transition(a, b), (a, b)


def test_t3_ticket_path_and_reopen():
    path = [State.TRIAGED, State.DIAGNOSED, State.PROPOSED, State.TICKETED, State.VERIFYING, State.PROOF_SENT,
            State.FIXED_UNCONFIRMED, State.REOPENED, State.TRIAGED]
    for a, b in zip(path, path[1:]):
        assert can_transition(a, b), (a, b)


def test_illegal_moves():
    assert not can_transition(State.NEW, State.PROOF_SENT)
    assert not can_transition(State.FIX_PENDING, State.CLOSED_PROVEN)   # never "assumed fixed"
    assert not can_transition(State.CLOSED_PROVEN, State.TRIAGED)       # must go through REOPENED
    assert not can_transition(State.TRIAGED, State.TRIAGED)


def test_overlay_enter_from_any_open_state_and_return_to_prev():
    for s in State:
        if s in TERMINAL or s in OVERLAY:
            continue
        assert can_transition(s, State.TAKEN_OVER)
        assert can_transition(s, State.CLOSED_BY_PRINCIPAL)
    assert can_transition(State.TAKEN_OVER, State.PROPOSED, prev_state=State.PROPOSED)
    assert not can_transition(State.TAKEN_OVER, State.CLOSED_PROVEN, prev_state=State.PROPOSED)
    assert can_transition(State.PAUSED, State.CLOSED_BY_PRINCIPAL)


def test_terminal_states_only_reopen():
    for s in TERMINAL:
        assert TRANSITIONS[s] == {State.REOPENED}
        assert not can_transition(s, State.TAKEN_OVER)
        assert not is_open(s)


def test_failed_worker_exists():
    assert can_transition(State.AWAITING_FIRST_REPLY, State.FAILED_WORKER)
    assert can_transition(State.FAILED_WORKER, State.TRIAGED)


def test_dial_order():
    assert Dial.auto.at_least(Dial.auto_on_timeout)
    assert Dial.draft.at_least(Dial.observe)
    assert not Dial.observe.at_least(Dial.draft)
    assert Dial.off.rank() == 0


def test_every_outkind_has_a_lane():
    assert set(LANE_FOR_KIND) == set(OutKind)
    assert set(LANE_FOR_KIND.values()) <= set(Lane)


def test_outbound_send_key_and_lane():
    m = OutboundMessage(request_id="REQ-0001", step="holding", kind=OutKind.holding,
                        channel=Channel.telegram_group, text="Received.")
    assert m.send_key == "REQ-0001:holding"
    assert m.lane == Lane.L1_holding


def test_event_roundtrip():
    ev = make_event("WAVE/00001", photo=True)
    back = Event.from_dict(ev.to_dict())
    assert back == ev


def test_time_helpers_keep_cairo_and_reject_naive():
    dt = datetime(2026, 7, 1, 12, 0, tzinfo=CAIRO)        # summer time
    assert parse_ts(utc_iso(dt)) == dt
    assert parse_ts(utc_iso(dt)).tzinfo == CAIRO
    assert utc_iso(dt) == "2026-07-01T09:00:00+00:00"                    # stored as UTC
    assert utc_iso(datetime(2026, 1, 1, 12, 0, tzinfo=CAIRO)) == "2026-01-01T10:00:00+00:00"
    # text order == time order even across the DST change (SQLite compares strings)
    a, b = datetime(2026, 10, 29, 23, 30, tzinfo=CAIRO), datetime(2026, 10, 30, 0, 10, tzinfo=CAIRO)
    assert (utc_iso(a) < utc_iso(b)) == (a < b)
    with pytest.raises(ValueError):
        utc_iso(datetime(2026, 1, 1))
