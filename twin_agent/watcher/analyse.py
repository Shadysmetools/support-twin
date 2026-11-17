"""Sysadmin's Watcher agent (runs on Sysadmin's device): bundles in -> fingerprint, match scores, persona addendum, digest.

    python -I aa.py watcher analyse --bundles <synced outbox clone> [--out DIR] [--addendum PATH] [--pull]
                                    [--judge] [--send-digest]

Writes `<out>/FINGERPRINT.md`, `<out>/fingerprint.json`, `<out>/scores.json` and updates the auto section of the
persona addendum (default `<out>/PERSONA-ADDENDUM.md`; never `07-PRINCIPAL-PERSONA.md` itself). Default out dir is
`data/watcher/fingerprint` (gitignored). The digest goes to Sysadmin through a DigestSender (fake unless live).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths
from . import bundle as bundle_mod
from .fingerprint import build_fingerprint, generic
from .similarity import judge, match_score

BEGIN, END = "<!-- watcher:fingerprint:begin -->", "<!-- watcher:fingerprint:end -->"


def default_out() -> Path:
    return paths.data_dir() / "watcher" / "fingerprint"


def load_bundles(bundle_dir: Path) -> list[dict]:
    d = Path(bundle_dir)
    files = sorted((d / "cases").glob("*.jsonl")) if (d / "cases").is_dir() else sorted(d.glob("*.jsonl"))
    out = []
    for p in files:
        try:
            out.append(bundle_mod.read_bundle(p))
        except (ValueError, OSError):
            continue                                        # one broken bundle never stops the analysis
    return out


def score_drafts(cases: list[dict], fp: dict, *, runner=None, cwd: Path | None = None) -> list[dict]:
    """Every agent draft vs Principal's real reply on the same case (+ his other replies to the same requester)."""
    by_req: dict[str, list[str]] = {}
    for c in cases:
        for r in c.get("records") or []:
            if r.get("kind") == "own_message":
                by_req.setdefault(str(r.get("requester") or c.get("requester")), []).append(r.get("text") or "")
    out = []
    for c in cases:
        for r in c.get("records") or []:
            if r.get("kind") != "edit" or not r.get("draft"):
                continue
            refs = ([r.get("final")] if not r.get("via_agent") or not r.get("unchanged") else []) + \
                by_req.get(str(c.get("requester")), [])[:8]
            s = match_score(r["draft"], [x for x in refs if x], fp)
            s.update(request_id=c.get("request_id"), judge=judge(r["draft"], refs, runner=runner, cwd=cwd))
            out.append(s)
    return out


def _table(rows: list[list], head: list[str]) -> list[str]:
    return ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)] + \
        ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]


def render_md(fp: dict, scores: list[dict] | None = None, *, public: bool = False, blocked: set[str] = frozenset(),
              title: str = "Principal's work fingerprint") -> str:
    """public=True: numbers and generic phrases only (seen with 3+ requesters, no digits, no name parts)."""
    o, d, e = fp.get("overall") or {}, fp.get("diagnosis") or {}, fp.get("edits") or {}
    L = [f"# {title}", "", f"Built from {fp.get('cases', 0)} case(s) and {fp.get('messages', 0)} of his own messages.",
         "", "## How he writes", "",
         f"- Language mix: {json.dumps(o.get('language_mix') or {}, ensure_ascii=False)} "
         "(franco = Egyptian Arabic in Latin letters)",
         f"- Length: median {o.get('median_words')} words, 90% under {o.get('p90_words')} words",
         f"- Emoji in {o.get('emoji_rate')} of messages; asks a question in {o.get('question_rate')}",
         f"- Reply time after the requester's message: median {o.get('median_reply_min')} min, "
         f"75% within {o.get('p75_reply_min')} min",
         f"- Messages by hour (Cairo): {json.dumps(o.get('hours') or {})}",
         f"- What his messages do: {json.dumps(o.get('moves') or {})}", "", "## How he diagnoses", "",
         f"- First move: {json.dumps(d.get('first_move') or {})}",
         f"- Escalates in {d.get('escalate_rate')} of cases; declines in {d.get('decline_rate')}", ""]
    L += _table([[" -> ".join(s["moves"]), s["count"]] for s in d.get("top_sequences") or []], ["First moves", "Cases"])
    L += ["", "## What he changes in drafts", "",
          f"- Drafts seen: {e.get('count')}; sent unchanged: {e.get('unchanged_rate')}; "
          f"median similarity draft->final: {e.get('median_similarity')}; length ratio: {e.get('length_ratio')}"]
    if not public:
        L += [f"- Words he adds: {', '.join(e.get('words_added') or [])}",
              f"- Words he removes: {', '.join(e.get('words_removed') or [])}"]
    bank = [p for p in fp.get("phrase_bank") or [] if not public or generic(p["phrase"], blocked, 3, p)]
    L += ["", "## Phrase bank" + (" (generic phrases only)" if public else ""), ""]
    L += _table([[p["phrase"], p["count"], p.get("requesters", "-")] for p in bank[:30]], ["Phrase", "Times", "Requesters"])
    if not public:
        L += ["", "Openers: " + "; ".join(f"{x['phrase']} ({x['count']})" for x in fp.get("openers") or []),
              "Closers: " + "; ".join(f"{x['phrase']} ({x['count']})" for x in fp.get("closers") or [])]
    L += ["", "## Per requester", ""]
    rows = []
    for i, (alias, r) in enumerate((fp.get("per_requester") or {}).items()):
        rows.append([f"R{i + 1}" if public else alias, r["messages"], json.dumps(r["language_mix"]),
                     r["median_words"], r["median_reply_min"], r["emoji_rate"], ", ".join(r["top_moves"])])
    L += _table(rows[:25], ["Requester", "Msgs", "Language", "Median words", "Median reply min", "Emoji", "Top moves"])
    if scores:
        L += ["", "## Fingerprint match of agent drafts", ""]
        L += _table([[s["request_id"], s["score"], json.dumps(s["parts"]), (s.get("judge") or {}).get("status")]
                     for s in scores], ["Request", "Score", "Parts", "Judge"])
    return "\n".join(L) + "\n"


def addendum_block(fp: dict) -> str:
    o, d = fp.get("overall") or {}, fp.get("diagnosis") or {}
    mix = o.get("language_mix") or {}
    top = max(mix.items(), key=lambda kv: kv[1])[0] if mix else "en"
    first = next(iter(d.get("first_move") or {}), "reply")
    return "\n".join([BEGIN, "## Fingerprint addendum (auto, from the Watcher; do not edit inside the markers)", "",
                      f"- Write mostly in {top}; keep it near {o.get('median_words')} words.",
                      f"- His first move on a new issue is usually: {first.replace('_', ' ')}.",
                      f"- Emoji rate {o.get('emoji_rate')}: use emoji only if he would.",
                      f"- He escalates in {d.get('escalate_rate')} of cases; do not escalate more than he does.",
                      END])


def update_addendum(path: Path, fp: dict) -> None:
    """Replace only the text between the markers (append the block the first time)."""
    path = Path(path)
    old = path.read_text(encoding="utf-8") if path.exists() else "# Principal persona addendum\n"
    block = addendum_block(fp)
    if BEGIN in old and END in old:
        new = old[:old.index(BEGIN)] + block + old[old.index(END) + len(END):]
    else:
        new = old.rstrip() + "\n\n" + block + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")


@dataclass
class AnalyseReport:
    cases: int = 0
    new_cases: int = 0
    scores: list[dict] = field(default_factory=list)
    digest: str = ""
    files: list[str] = field(default_factory=list)


def analyse(bundle_dir: Path, out_dir: Path | None = None, *, addendum: Path | None = None, runner=None,
            sender=None) -> AnalyseReport:
    from .digest import digest_text
    out = Path(out_dir or default_out())
    out.mkdir(parents=True, exist_ok=True)
    cases = load_bundles(bundle_dir)
    state_p = out / "state.json"
    prev = json.loads(state_p.read_text(encoding="utf-8")) if state_p.exists() else {}
    seen = set(prev.get("seen") or [])
    fp = build_fingerprint(cases)
    scores = score_drafts(cases, fp, runner=runner, cwd=out)
    rep = AnalyseReport(cases=len(cases), new_cases=sum(c.get("request_id") not in seen for c in cases), scores=scores)
    (out / "fingerprint.json").write_text(json.dumps(fp, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "scores.json").write_text(json.dumps(scores, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "FINGERPRINT.md").write_text(render_md(fp, scores), encoding="utf-8")
    update_addendum(addendum or out / "PERSONA-ADDENDUM.md", fp)
    rep.files = ["fingerprint.json", "scores.json", "FINGERPRINT.md", str(addendum or "PERSONA-ADDENDUM.md")]
    rep.digest = digest_text(fp, scores, new_cases=rep.new_cases, prev=prev)
    if sender is not None:
        sender.send(rep.digest)
    avg = round(sum(s["score"] for s in scores) / len(scores), 1) if scores else prev.get("avg_score")
    state_p.write_text(json.dumps({"seen": sorted(seen | {c.get("request_id") for c in cases}), "avg_score": avg}),
                       encoding="utf-8")
    return rep
