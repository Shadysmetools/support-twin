"""Blank judge(s), understanding check and refuter (05 3.3 checks 2 to 5), all through a ClaudeRunner.

Each check is a fresh `claude -p` that never sees the worker's reasoning: judges get the request evidence, the
evidence list and the draft only (no `note`, `confidence`, `verified`, `questions_for_principal`). Every parse is
fail-closed: output we cannot read is a FAIL. Prompts live in worker/prompts/*.md with {{PLACEHOLDER}} fields.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..models import Channel, Tier
from . import injection
from .reply_checker import check_reply

PROMPTS = Path(__file__).resolve().parent.parent / "worker" / "prompts"
# What a judge may see of the packet. Builder reasoning stays out (05 1.1 "never sees the builder's reasoning").
JUDGE_KEYS = ("request_id", "class", "tier", "company", "understanding", "evidence", "draft_reply",
              "after_fix_reply", "proposed_action", "ticket_draft")
_REF = re.compile(r"\b(?:[A-Z]{2,6}(?:/[A-Za-z0-9]+){1,4}|(?:SO|PO|RO)\d{3,7})\b")


@dataclass
class Verdict:
    check: str
    passed: bool
    reasons: list[str] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------- prompts + parsing


def load_prompt(name: str, **fields: str) -> str:
    """worker/prompts/<name>.md with every {{KEY}} replaced (str.replace: braces in data are safe)."""
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    for k, v in fields.items():
        text = text.replace("{{" + k.upper() + "}}", v)
    return text


def extract_json(text: str) -> dict | None:
    """The last JSON object in the output: a ```json block, else the last {...} that parses."""
    if not text:
        return None
    for block in reversed(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)):
        try:
            return json.loads(block)
        except ValueError:
            pass
    starts = [m.start() for m in re.finditer(r"\{", text)]
    for s in reversed(starts):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[s:])
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    return None


def parse_verdict(text: str, check: str) -> Verdict:
    """{"pass": bool, "reasons": [...], "items": [{"item", "pass", "why"}]} -> Verdict. Fail-closed.

    Any item with pass false fails the verdict even if the top-level says pass. A refuter may answer
    {"refuted": bool} instead: refuted means FAIL.
    """
    obj = extract_json(text)
    if not isinstance(obj, dict):
        return Verdict(check, False, ["could not read the checker's output"])
    reasons = [str(r) for r in obj.get("reasons") or [] if str(r).strip()]
    items = [i for i in obj.get("items") or [] if isinstance(i, dict)]
    if "refuted" in obj:
        passed = obj.get("refuted") is False
    else:
        top = obj.get("pass", obj.get("passed"))
        passed = top is True
    failed_items = [i for i in items if i.get("pass", i.get("passed")) is False]
    if failed_items:
        passed = False
        reasons += [f"item failed: {i.get('item', '?')}: {i.get('why', '')}".strip() for i in failed_items]
    if not passed and not reasons:
        reasons = ["checker said fail without a reason"]
    return Verdict(check, passed, reasons, items)


def judge_view(packet: dict) -> dict:
    return {k: packet.get(k) for k in JUDGE_KEYS if k in packet}


def _call(runner, prompt: str, kind: str, workdir: Path, check: str) -> tuple[Verdict, dict | None]:
    """Run one checker session. Returns the parsed verdict and the raw JSON object (None if unreadable)."""
    try:
        res = runner.run(prompt, kind=kind, cwd=workdir)
    except Exception as exc:   # a crashed checker is a FAIL, never a pass
        return Verdict(check, False, [f"checker did not run: {type(exc).__name__}"]), None
    if not getattr(res, "ok", False):
        why = getattr(res, "error", "") or f"exit {getattr(res, 'exit_code', '?')}"
        return Verdict(check, False, [f"checker did not run: {why}"]), None
    return parse_verdict(res.text, check), extract_json(res.text)


def _run(runner, prompt: str, kind: str, workdir: Path, check: str) -> Verdict:
    return _call(runner, prompt, kind, workdir, check)[0]


# ---------------------------------------------------------------- the checks


def understanding_check(thread_text: str, packet: dict, runner, workdir: Path) -> Verdict:
    """A fresh session reads only the raw thread, writes its own reading, then compares with the packet's.

    Fails when the model says so, or when code sees its blind reading name a record the packet does not,
    or a different company.
    """
    prompt = load_prompt("understanding", thread=injection.wrap_untrusted(thread_text, "request thread"),
                         packet_understanding=json.dumps(packet.get("understanding", ""), ensure_ascii=False))
    v, obj = _call(runner, prompt, "understanding", workdir, "understanding")
    return _compare_understanding(v, obj, packet) if obj else v


def _compare_understanding(v: Verdict, obj: dict, packet: dict) -> Verdict:
    packet_text = json.dumps(judge_view(packet), ensure_ascii=False)
    extra = []
    for ref in obj.get("records") or []:
        if str(ref).strip() and str(ref) not in packet_text:
            extra.append(f"record {ref} is in the thread but not in the packet")
    theirs, ours = str(obj.get("company") or "UNKNOWN"), str(packet.get("company") or "UNKNOWN")
    if "UNKNOWN" not in (theirs, ours) and theirs != ours:
        extra.append(f"company differs: thread says {theirs}, packet says {ours}")
    items = v.items + ([{"blind_reading": obj["statement"]}] if obj.get("statement") else [])
    return Verdict("understanding", v.passed and not extra, v.reasons + extra, items)


def judge_names(tier: Tier | str) -> list[str]:
    """T1: 1 judge (correctness). T2a and up, or unknown: 2 judges (correctness, blast radius) (05 3.3)."""
    return ["correctness"] if str(tier) == Tier.T1 else ["correctness", "blast_radius"]


def run_judges(packet: dict, evidence_text: str, runner, workdir: Path, tier: Tier) -> list[Verdict]:
    return [_one_judge(packet, evidence_text, runner, workdir, name) for name in judge_names(tier)]


def refute(packet: dict, evidence_text: str, runner, workdir: Path) -> Verdict:
    """Tries to prove the draft wrong. Passed = it could not.

    Fix 4 (Claude path): this in-repo refuter runs with kind='refuter', so it inherits the refuter --model pin; an
    invalid pin (or any crash/timeout) fails the SESSION. A session that could not RUN is not a refutation - it
    DEGRADES (passed, noted) so it never forces judges_passed=False and strands the packet unverified (the outage).
    A session that ran and genuinely refuted still fails; an unreadable answer from a run that succeeded still fails.

    Isolation (review fix): the refuter's prompt is INLINE (packet + evidence), it reads no file from its folder
    (refuter.md: "You use no tools"), so it runs in a dedicated cwd OUTSIDE data/work. The effective-settings writer
    then denies it Read over data/work, so - unlike a run inside data/work/REQ-x - it cannot read a SIBLING
    request's request.json / NOTE.md / recall.md. A per-REQ subfolder avoids two refuter runs clashing on the
    effective-settings file.
    """
    prompt = load_prompt("refuter", packet=json.dumps(judge_view(packet), ensure_ascii=False, indent=1),
                         evidence=injection.wrap_untrusted(evidence_text, "evidence and thread"))
    cwd = _refuter_cwd(workdir)
    try:
        res = runner.run(prompt, kind="refuter", cwd=cwd)
    except Exception as exc:                                 # backend/session error: degrade, do not veto the packet
        return Verdict("refuter", True, [f"refuter could not run ({type(exc).__name__}); degraded to single-model "
                                         "(evidence gate + base judges)"])
    if not getattr(res, "ok", False):
        why = getattr(res, "error", "") or f"exit {getattr(res, 'exit_code', '?')}"
        return Verdict("refuter", True, [f"refuter could not run ({why}); degraded to single-model "
                                         "(evidence gate + base judges)"])
    return parse_verdict(res.text, "refuter")


def _refuter_cwd(workdir: Path) -> Path:
    """A dedicated cwd OUTSIDE data/work for the in-repo refuter, per-REQ so concurrent runs do not clash on the
    effective-settings file. Derived from the work folder's parent (the data dir) - production workdir
    data/work/REQ-x -> data/refuter/REQ-x, a sibling of data/work, never inside it. Deriving from the given
    workdir (not the global paths.data_dir()) keeps a test's throwaway workdir from touching the default home.
    Falls back to ``workdir`` only if the dedicated dir cannot be made."""
    workdir = Path(workdir)
    cwd = workdir.parent.parent / "refuter" / workdir.name
    try:
        cwd.mkdir(parents=True, exist_ok=True)
        return cwd
    except OSError:
        return workdir


def needs_refuter(tier: str, packet: dict) -> bool:
    """T2a and up, and any draft naming a fix (05 3.3 check 4)."""
    return str(tier) not in (Tier.T1,) or bool(packet.get("proposed_action"))


def _draft(packet: dict) -> dict | None:
    d = packet.get("draft_reply")
    return d if isinstance(d, dict) and str(d.get("text") or "").strip() else None


def run_checks(request: dict, packet: dict, thread_text: str, runner, budget, workdir: Path) -> list[Verdict]:
    """05 3.3 order: injection -> understanding -> judge(s) -> refuter -> reply checker (code + AI).

    Every AI call asks the spawn budget first; a refused call is a FAIL with the reason. Writes judge.json.
    """
    rid = request.get("id") or packet.get("request_id") or ""
    tier = packet.get("tier") or request.get("tier") or Tier.UNKNOWN
    verdicts: list[Verdict] = []
    inj = injection.scan(thread_text)
    verdicts.append(Verdict("injection", not inj.hit, [f"injection pattern: {p}" for p in inj.patterns]))

    def ai(kind: str, check: str, call):
        ok, why = budget.allow(rid, kind)
        if not ok:
            verdicts.append(Verdict(check, False, [f"spawn budget: {why}"]))
            return
        budget.record(rid, kind)
        got = call()
        verdicts.extend(got if isinstance(got, list) else [got])

    ai("understanding", "understanding", lambda: understanding_check(thread_text, packet, runner, workdir))
    for name in judge_names(tier):
        ai("judge", f"judge_{name}", lambda name=name: _one_judge(packet, thread_text, runner, workdir, name))
    if needs_refuter(tier, packet):
        ai("refuter", "refuter", lambda: refute(packet, thread_text, runner, workdir))
    draft = _draft(packet)
    if draft:
        rc = check_reply(draft["text"], channel=Channel(draft.get("channel") or request.get("channel") or "telegram_group"),
                         lang=str(draft.get("lang") or "en"))
        verdicts.append(Verdict("reply_rules", rc.ok, rc.problems, [{"warnings": rc.warnings}] if rc.warnings else []))
        prompt = load_prompt("reply_checker", draft=json.dumps(draft, ensure_ascii=False),
                             thread=injection.wrap_untrusted(thread_text, "request thread"))
        ai("reply_checker", "reply_checker", lambda: _run(runner, prompt, "reply_checker", workdir, "reply_checker"))
    write_judge_json(workdir, verdicts)
    return verdicts


def _one_judge(packet, evidence_text, runner, workdir, name) -> Verdict:
    prompt = load_prompt("judge", focus=name, packet=json.dumps(judge_view(packet), ensure_ascii=False, indent=1),
                         evidence=injection.wrap_untrusted(evidence_text, "evidence and thread"))
    return _run(runner, prompt, "judge", workdir, f"judge_{name}")


def all_passed(verdicts: list[Verdict]) -> bool:
    return bool(verdicts) and all(v.passed for v in verdicts)


def write_judge_json(workdir: Path, verdicts: list[Verdict]) -> None:
    try:
        Path(workdir).mkdir(parents=True, exist_ok=True)
        (Path(workdir) / "judge.json").write_text(
            json.dumps([asdict(v) for v in verdicts], ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass   # the verdicts still go back to the caller and onto the card
