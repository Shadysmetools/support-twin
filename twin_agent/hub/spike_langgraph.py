"""STACK SPIKE (plan v2 §17 / coordinator): a MINIMAL LangGraph graph for RECEIVE -> ANALYSE -> CONFIRM with a
human-in-the-loop approval INTERRUPT, built ALONGSIDE the existing twin_agent pipeline to answer one question:
does LangGraph compose cleanly with the engine, or does it fight it?

Finding (recorded in the PR): it composes cleanly. Every node is a thin wrapper that delegates to the SAME pure
functions the plain engine uses — ``webhook.parse``, ``analyse.analyse``, ``templates.confirm`` — and the
approval pause is a first-class ``interrupt()`` with a checkpointer, which maps directly onto the hub's
"pause after CONFIRM, resume on the customer's second webhook" state machine. guard.py and the interface+fake
pattern are untouched (nodes would call the same ``GuardedOdoo`` / fakes).

Decision for Phase 1: the plain ``Hub`` engine SHIPS as the deliverable (zero extra deps, CI stays
credential- and dependency-light, the two-webhook resume already works). LangGraph is adopted for the loop in
Phase 2 when multi-step interviews, retries and resumable long-running approvals justify its runtime — this spike
proves that migration is a node-wrapping exercise, not a rewrite. ``langgraph`` is therefore an OPTIONAL
dependency: this module imports it lazily and the spike test skips when it is absent.
"""
from __future__ import annotations

from typing import Any, TypedDict

from . import analyse as analyse_mod
from . import templates
from .webhook import parse


class SpikeState(TypedDict, total=False):
    payload: dict
    wa_id: str
    name: str
    lang: str
    text: str
    ticket_type: str
    module: str
    summary: str
    draft: str
    approved: bool
    sent: str


def build_graph(checkpointer: Any = None):
    """Build and compile the RECEIVE -> ANALYSE -> CONFIRM(interrupt) graph. Lazy LangGraph import."""
    from langgraph.graph import StateGraph, START, END
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import interrupt

    def receive(state: SpikeState) -> SpikeState:
        inbound = parse(state["payload"])
        if inbound is None:
            return {"text": ""}
        inbound.resolve_lang()
        return {"wa_id": inbound.wa_id, "name": inbound.name, "lang": inbound.lang, "text": inbound.text or ""}

    def analyse(state: SpikeState) -> SpikeState:
        a = analyse_mod.analyse(state.get("text", ""), lang=state.get("lang", "en"))
        return {"ticket_type": a.ticket_type, "module": a.module, "summary": a.summary}

    def confirm(state: SpikeState) -> SpikeState:
        draft = templates.confirm(state.get("lang", "en"), name=state.get("name") or "there",
                                  issue=state.get("summary", ""), module=state.get("module", ""),
                                  outcome="it resolved")
        # Human-in-the-loop: pause here until an owner approves (the same gate the plain engine runs in Telegram).
        decision = interrupt({"kind": "confirm", "draft": draft})
        approved = bool((decision or {}).get("approved"))
        return {"draft": draft, "approved": approved,
                "sent": ((decision or {}).get("text") or draft) if approved else ""}

    g = StateGraph(SpikeState)
    g.add_node("receive", receive)
    g.add_node("analyse", analyse)
    g.add_node("confirm", confirm)
    g.add_edge(START, "receive")
    g.add_edge("receive", "analyse")
    g.add_edge("analyse", "confirm")
    g.add_edge("confirm", END)
    return g.compile(checkpointer=checkpointer or MemorySaver())


def run_demo(payload: dict, *, approve: bool = True, thread_id: str = "spike-1") -> dict:
    """Drive the graph through the interrupt: invoke -> pause at approval -> resume with the owner's decision."""
    from langgraph.types import Command

    app = build_graph()
    cfg = {"configurable": {"thread_id": thread_id}}
    first = app.invoke({"payload": payload}, cfg)
    interrupted = bool(first.get("__interrupt__"))
    final = app.invoke(Command(resume={"approved": approve, "text": None}), cfg)
    return {"interrupted": interrupted, "final": final}
