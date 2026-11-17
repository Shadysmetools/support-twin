"""§16a — the golden-set eval gate.

Loads a set of REDACTED past tickets (input -> ground-truth type/owner/answer), runs the pipeline's
classify/route/draft, scores the predictions against ground truth, and FAILS the gate on a score regression
(a drop below the baseline) — the CI gate on every prompt/routing/model change.

Redaction: the golden set carries no client PII — synthetic client ids and paraphrased inputs only. The schema
is documented in ``fixtures/golden_set.json`` (``_schema`` key). The classifier/assigner are injectable so a
test can simulate a regression (a deliberately worse classifier) and prove the gate fails.
"""
from __future__ import annotations

import html
import json
import re
from pathlib import Path

from . import analyse as analyse_mod
from . import ticket_text
from .fakes import FakeTicketWriter
from .judge import FakeJudge
from .model import Inbound, detect_lang
from .routing import RuleAssigner, default_team

GOLDEN = Path(__file__).parent / "fixtures" / "golden_set.json"


def load_golden(path: Path | None = None) -> list[dict]:
    data = json.loads((path or GOLDEN).read_text(encoding="utf-8"))
    return [row for row in data.get("items", [])]


class DefaultEvalHarness:
    """Runs classify + route + draft over the golden set and scores vs ground truth."""

    def __init__(self, *, golden: list[dict] | None = None, classifier=None, assigner=None,
                 pass_threshold: float = 0.6):
        self.golden = golden if golden is not None else load_golden()
        self.classifier = classifier or (lambda text, client_context=None: analyse_mod.classify(
            text, client_context=client_context))
        self.assigner = assigner or RuleAssigner(default_team())
        self.pass_threshold = pass_threshold

    def _score_item(self, item: dict) -> dict:
        ctx = item.get("client_context") or {}
        analysis = self.classifier(item["input"], client_context=ctx)
        type_ok = str(analysis.ticket_type) == str(item.get("gt_type"))
        pred = self.assigner.assign(analysis, ctx)
        owner_ok = int(pred["uid"]) == int(item.get("gt_owner_uid") or -1)
        return {"id": item.get("id"), "type_ok": type_ok, "owner_ok": owner_ok,
                "pred_type": analysis.ticket_type, "pred_owner": pred["uid"]}

    def run(self) -> dict:
        rows = [self._score_item(it) for it in self.golden]
        n = len(rows) or 1
        type_acc = sum(1 for r in rows if r["type_ok"]) / n
        owner_acc = sum(1 for r in rows if r["owner_ok"]) / n
        score = round((type_acc + owner_acc) / 2, 4)
        return {"n": len(rows), "type_accuracy": round(type_acc, 4), "owner_accuracy": round(owner_acc, 4),
                "score": score, "rows": rows}

    def run_gate(self, baseline: dict | None = None) -> dict:
        res = self.run()
        passed = res["score"] >= self.pass_threshold
        regressed = False
        if baseline is not None:
            base_score = float(baseline.get("score", 0.0))
            regressed = res["score"] < base_score - 1e-9
        gate_pass = passed and not regressed
        return {**res, "pass_threshold": self.pass_threshold, "passed_absolute": passed,
                "regressed": regressed, "gate_pass": gate_pass,
                "reason": ("regression vs baseline" if regressed else
                           ("below threshold" if not passed else "ok"))}


# ---------------------------------------------------------------- ticket quality (the Odoo title + description)

TICKET_GOLDEN = Path(__file__).parent / "fixtures" / "ticket_quality_golden.json"
_AR = re.compile("[؀-ۿݐ-ݿ]")


def load_ticket_golden(path: Path | None = None) -> list[dict]:
    return json.loads((path or TICKET_GOLDEN).read_text(encoding="utf-8"))["items"]


def _sections_in_order(desc: str) -> bool:
    pos = -1
    for label in ticket_text.SECTION_ORDER:
        i = desc.find(f"<b>{html.escape(label)}</b>", pos + 1)
        if i < 0:
            return False
        pos = i
    return desc.find("Original message", pos) > pos


def ticket_rubric(item: dict, analysis, title: str, desc: str) -> dict:
    """The rubric as booleans (deterministic, no model): the judge then scores these."""
    module = item["gt_module"] if item["gt_module"] != "unknown" else "Module not identified"
    parts = title.split(" · ")
    escaped = [html.escape(ln) for ln in item["input"].splitlines() if ln.strip()]
    steps = item.get("steps") or []
    atts = item.get("attachments") or []
    follow = f"Follows up #{item['follow_up']}" if item.get("follow_up") else "Follows up #"
    invented = [] if atts else ["Link:", "Voice note", "received on WhatsApp"]
    return {
        "title_ok": len(parts) == 3 and parts[0] == item["client"] and parts[1] == module and bool(parts[2].strip())
                    and not _AR.search(title),
        "sections_ok": _sections_in_order(desc) and (follow in desc) == bool(item.get("follow_up")),
        "original_ok": all(e in desc for e in escaped),
        "no_invention": ("<script" not in desc and all(a in desc for a in atts) and not any(x in desc for x in invented)
                         and (all(s in desc for s in steps) if steps else ticket_text.STEPS_NONE in desc)),
        "type_ok": str(analysis.ticket_type) == item["gt_type"],
        "urgency_ok": str(analysis.urgency) == item["gt_urgency"] and f"<p>{analysis.urgency}</p>" in desc,
    }


class TicketQualityEval:
    """Builds each golden message into the real Odoo ticket (``ticket_text.compose``) and scores the rubric. CI uses a
    deterministic FAKE writer and FAKE judge, so the result says ``judged_by: fake`` and NEVER claims a model judged it;
    the real judge (Vertex / fallback) is blocked without keys and returns ``blocked``, not a score."""

    def __init__(self, *, cases: list[dict] | None = None, judge=None, pass_threshold: float = 0.9):
        self.cases = cases if cases is not None else load_ticket_golden()
        self.judge = judge or FakeJudge()
        self.pass_threshold = pass_threshold

    def _ticket(self, item: dict):
        text = item["input"]
        inbound = Inbound(message_id="eval", wa_id="eval", name="", kind=item["kind"], text=text, lang=detect_lang(text),
                          media_type=item.get("media_type"), transcript_placeholder=item["kind"] == "voice")
        writer = FakeTicketWriter({text: item["translation"]} if item.get("translation") else None,
                                  steps={text: "\n".join(item.get("steps") or [])})
        analysis = analyse_mod.classify(text, client_context={"modules": item.get("context_modules") or []})
        title, desc = ticket_text.compose(analysis, inbound, client=item["client"], partner_id=item["partner_id"],
                                          writer=writer, previous=int(item.get("follow_up") or 0))
        return analysis, title, desc

    def run(self) -> dict:
        rows = []
        for item in self.cases:
            analysis, title, desc = self._ticket(item)
            checks = ticket_rubric(item, analysis, title, desc)
            scored = self.judge.score("ticket", {"checks": checks})
            rows.append({"id": item["id"], "title": title, "checks": checks, "judge": scored})
        n = len(rows) or 1
        score = round(sum(sum(r["checks"].values()) / len(r["checks"]) for r in rows) / n, 4)
        return {"n": len(rows), "score": score, "rows": rows, "pass_threshold": self.pass_threshold,
                "gate_pass": score >= self.pass_threshold, "judged_by": self._judged_by(rows)}

    def _judged_by(self, rows: list[dict]) -> str:
        """Honest label: a model only when a REAL judge actually scored every row; a blocked real judge is 'none'."""
        if not getattr(self.judge, "real", False):
            return "fake (deterministic rubric, no model)"
        if any(r["judge"].get("status") == "blocked" for r in rows):
            return "none (the real judge is blocked: no key); rubric checks only"
        return f"model:{getattr(self.judge, 'model', '?')}"
