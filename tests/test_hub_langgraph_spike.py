"""Stack spike test (plan §17): the minimal LangGraph RECEIVE->ANALYSE->CONFIRM graph with an approval interrupt
composes with twin_agent (nodes delegate to the same pure functions). SKIPS when langgraph is not installed, so
CI stays green without the optional dependency."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("langgraph", reason="langgraph is an optional Phase-2 dependency")

from twin_agent.hub import spike_langgraph   # noqa: E402

FIX = Path(__file__).parent.parent / "twin_agent" / "hub" / "fixtures"


def test_langgraph_spike_pauses_at_approval_then_resumes():
    payload = json.loads((FIX / "webhook_text.json").read_text(encoding="utf-8"))
    out = spike_langgraph.run_demo(payload, approve=True)
    assert out["interrupted"] is True                      # the graph paused at the owner-approval interrupt
    final = out["final"]
    assert final["module"] == "Inventory"                  # reused twin_agent analyse
    assert final["approved"] is True and final["sent"]      # resumed after approval, draft sent
    assert "Acme Support" in final["sent"]              # reused twin_agent §13 template


def test_langgraph_spike_not_sent_when_rejected():
    payload = json.loads((FIX / "webhook_text.json").read_text(encoding="utf-8"))
    out = spike_langgraph.run_demo(payload, approve=False, thread_id="spike-reject")
    assert out["final"]["approved"] is False and out["final"]["sent"] == ""
