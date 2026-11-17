"""Retrieval API: recall(request) -> the top relevant cases + system-map facts + correction rules for a new
request, as a compact context block the worker can use.

The block is wrapped as UNTRUSTED data (same posture the spawner uses for thread text) and labelled HINTS
ONLY: recalled memory is client-derived and trust-graded, so the worker must verify it independently and may
never cite it as proof on its own. recall never writes packet evidence, so it cannot make a packet "verified".

Wiring: spawner.prepare_workdir calls maybe_inject(), which (behind cfg memory.recall_enabled, default True)
writes recall.md into the request's work folder. It is a pure read of memory.db (mode=ro) and OBSERVE-safe:
no send, no write, and it never creates memory.db as a side effect.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import cases as cases_store
from . import corrections as corrections_store
from . import system_map as system_map_store
from .store import db_path, open_ro
from ..checks.injection import wrap_untrusted


def _request_text(request) -> tuple[str, str]:
    """(symptom_text, client) from a request dict or a plain string."""
    if isinstance(request, str):
        return request, ""
    if isinstance(request, dict):
        text = " ".join(str(request.get(k)) for k in ("title", "symptom", "understanding", "class")
                        if request.get(k))
        client = str(request.get("company") or request.get("client") or "").strip()
        return text, client
    return str(request), ""


def recall(conn: sqlite3.Connection, request, k: int = 3) -> dict:
    """Top cases + facts + rules for a new request, plus a rendered markdown `block` (hints, untrusted)."""
    text, client = _request_text(request)
    cs = cases_store.similar(conn, text, client or None, k)
    fs = system_map_store.facts(conn, text, k)
    rs = corrections_store.rules(conn, text, k)
    return {"cases": cs, "facts": fs, "rules": rs, "block": render_block(cs, fs, rs)}


def render_block(cs: list, fs: list[dict], rs: list[dict]) -> str:
    """Compact, wrapped, hints-only context. Returns "" when there is nothing relevant."""
    lines: list[str] = []
    if cs:
        lines.append("## Similar past cases")
        for c in cs:
            who = "/".join(x for x in (c.client, c.track) if x)
            tail = f" | fix: {c.fix}" if c.fix else ""
            cause = c.verified_root_cause or "(root cause not verified)"
            lines.append(f"- [{c.trust_level}] ({who}) {c.symptom} -> {cause}{tail}"
                         + (f"  [refs: {c.evidence_refs}]" if c.evidence_refs else ""))
    if fs:
        lines.append("## System-map facts")
        for f in fs:
            lines.append(f"- [{f['trust_level']}] {f['topic']}: {f['fact']}"
                         + (f"  (evidence: {f['evidence']})" if f.get("evidence") else ""))
    if rs:
        lines.append("## Correction rules (how Principal changed past drafts)")
        for r in rs:
            lines.append(f"- {r['context']}: {r['rule']}")
    if not lines:
        return ""
    header = ("# Recalled memory (HINTS ONLY - each line carries a trust level GREEN/AMBER/RED; "
              "treat as leads to VERIFY independently, never as proof on their own, and do not cite as evidence "
              "without your own check).")
    body = header + "\n" + "\n".join(lines)
    # Route through the shared neutralising wrapper (checks.injection.wrap_untrusted) rather than hand-rolling
    # the tag: a case symptom carrying a </untrusted> tag (from a client ticket title via bootstrap) is
    # neutralised and cannot break out to inject the worker.
    return wrap_untrusted(body, "memory-recall") + "\n"


def maybe_inject(request_id: str, request, cfg: dict | None, workdir, clock=None) -> Path | None:
    """Write recall.md into the work folder when recall is enabled and memory has something relevant.

    Fully guarded: any problem (missing DB, bad config, read error) returns None without raising, so a memory
    hiccup can never break prepare_workdir or the pipeline. Default is ON and read-only (OBSERVE-safe)."""
    try:
        mem_cfg = (cfg or {}).get("memory", {}) if isinstance(cfg, dict) else {}
        if not mem_cfg.get("recall_enabled", True):
            return None
        if not db_path().exists():
            return None
        con = open_ro()
        try:
            result = recall(con, request, k=int(mem_cfg.get("recall_k", 3)))
        finally:
            con.close()
        block = result["block"]
        if not block.strip():
            return None
        out = Path(workdir) / "recall.md"
        out.write_text(block, encoding="utf-8")
        return out
    except Exception:
        return None
