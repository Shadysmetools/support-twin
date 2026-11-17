"""Reflection loop: deterministic trust changes + lessons, and the LLM hook."""
from __future__ import annotations

from twin_agent.memory import cases, corrections, reflect
from twin_agent.memory.store import MemoryDB


def _trust_by_ref(mem):
    return {r["request_ref"]: r["trust_level"] for r in cases.all_cases(mem.conn)}


def test_two_independent_sources_reach_green(tmp_home, clock):
    mem = MemoryDB(clock=clock)
    root = "custom guard reads only the first move PO so a stray line blocks validate"
    cases.add(mem, cases.Case(request_ref="A", client="EG", symptom="s1 receipt validate",
                              verified_root_cause=root, trust_level="RED", source="runbook"))
    cases.add(mem, cases.Case(request_ref="B", client="EG", symptom="s2 receipt validate",
                              verified_root_cause=root, trust_level="RED", source="rca"))
    cases.add(mem, cases.Case(request_ref="C", client="KSA", symptom="pos badge",
                              verified_root_cause="unsynced pos order", trust_level="RED", source="runbook"))
    rep = reflect.reflect(mem, min_cases=1)
    trust = _trust_by_ref(mem)
    assert trust["A"] == "GREEN" and trust["B"] == "GREEN"       # two distinct sources, one independent
    assert trust["C"] == "RED"                                   # runbook-only single source never GREEN
    assert rep["trust_upgrades"] >= 2 and rep["lessons_written"] >= 1
    assert reflect.lessons(mem)
    mem.close()


def test_lone_independent_source_reaches_amber(tmp_home, clock):
    mem = MemoryDB(clock=clock)
    cases.add(mem, cases.Case(request_ref="E", client="X", symptom="odd thing",
                              verified_root_cause="root one", trust_level="RED", source="rca"))
    reflect.reflect(mem, min_cases=1)
    assert _trust_by_ref(mem)["E"] == "AMBER"
    mem.close()


def test_correction_downgrades(tmp_home, clock):
    mem = MemoryDB(clock=clock)
    cases.add(mem, cases.Case(request_ref="F", client="Z", symptom="login link reset password expired",
                              verified_root_cause="", trust_level="AMBER", source="rca"))
    corrections.add_rule(mem, context="login link reset", what_changed="w",
                         rule="prefer the password reset flow link")
    rep = reflect.reflect(mem, min_cases=1)
    assert _trust_by_ref(mem)["F"] == "RED"
    assert rep["trust_downgrades"] >= 1
    mem.close()


def test_lessons_idempotent_and_llm_hook(tmp_home, clock):
    mem = MemoryDB(clock=clock)
    cases.add(mem, cases.Case(request_ref="A", client="EG", symptom="s1",
                              verified_root_cause="root", trust_level="RED", source="rca"))
    reflect.reflect(mem, min_cases=1)
    n1 = len(reflect.lessons(mem))
    reflect.reflect(mem, min_cases=1)
    assert len(reflect.lessons(mem)) == n1       # re-running does not duplicate lessons
    rep = reflect.reflect(mem, min_cases=1, reflector=lambda ctx: ["an extra distilled lesson"])
    assert rep["lessons_written"] >= 1
    mem.close()
