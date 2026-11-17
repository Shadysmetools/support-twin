"""Interview onboarding: the twin sits with the person and learns them from their own answers.

This is the missing "interview" step. The twin asks a fixed set of questions about who the person is and
how they actually work a ticket, then:
  - writes a profile file (data/profile/profile.json, gitignored data/), and
  - seeds the person's own rules into system-map memory as AMBER facts (source "interview"), so the twin
    acts like them from day one, before any real tickets exist.

Self-stated rules are a strong hint, not independent proof, so they are stored AMBER, never GREEN: a GREEN
fact still needs outside evidence (code file:line / record id). Answers are redacted on store like all memory.

Run it:
    python -I aa.py onboard                      # interactive, asks each question
    python -I aa.py onboard --all                # the full wizard: interview THEN setup (keys/logins)
    python -I aa.py onboard --answers me.json    # non-interactive, answers from a JSON {key: text}
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .. import paths
from ..memory import system_map
from ..memory.store import MemoryDB
from . import wizard


@dataclass(frozen=True)
class Question:
    key: str          # stable id, also the memory topic
    topic: str        # grouping / memory topic prefix
    prompt: str       # what the twin asks
    required: bool = True
    title: str = ""   # short step title shown in the progress line ("Step 3 of 12 - <title>")
    why: str = ""     # one-line plain-English reason this is asked (shown before the prompt)


# The interview. Order matters: identity first, then how they work, then the guardrails.
QUESTIONS: tuple[Question, ...] = (
    Question("name", "identity", "What is your full name, as it should appear to clients?",
             title="Your name", why="So the twin signs messages and introduces itself as you."),
    Question("role", "identity", "What is your role / title on the team?",
             title="Your role", why="So the twin frames its answers from your position on the team."),
    Question("owns", "responsibility", "What are you responsible for? What lands on your plate?",
             title="What you own", why="So the twin knows which tickets are yours to handle."),
    Question("channels", "intake", "Where do issues reach you? (email / Telegram / WhatsApp / Odoo)",
             title="Where work reaches you", why="So the twin watches the same places you do."),
    Question("classify", "classification",
             "How do you decide a ticket's track and type? "
             "(Functional vs Development; Bug / New Request / Configuration / Inquiry / Customization)",
             title="How you triage", why="So the twin sorts each ticket the way you would."),
    Question("bug_steps", "handling", "When it's a BUG, what are your steps before you reply?",
             title="Your bug playbook", why="So the twin reproduces and checks before answering, like you."),
    Question("inquiry_steps", "handling", "When it's just an INQUIRY, how do you answer it?",
             title="Your inquiry playbook", why="So the twin answers simple questions the way you do."),
    Question("voice", "voice", "How do you write to clients — tone, language, length? Give one example line.",
             title="Your voice", why="So replies sound like you, not a generic bot."),
    Question("gates", "gates", "What must you NEVER send or change without approval? And whose approval?",
             title="Approval gates", why="So the twin always pauses for a human before risky actions."),
    Question("redlines", "gates", "What are your hard red lines — things you never do?",
             title="Your red lines", why="So the twin refuses the things you would never do."),
    Question("escalate", "escalation", "When do you escalate, and to whom?",
             title="When to escalate", why="So the twin hands off instead of guessing when it's over its head."),
    Question("automation", "automation",
             "How much should the twin do on its own? Answer AUTOMATIC or MANUAL. "
             "AUTOMATIC = Claude runs each step by itself (advances the safe no-email stages and sends "
             "high-confidence replies on its own), still STOPPING at every client-facing gate. "
             "MANUAL = Claude prepares everything (draft, ticket, timesheet, proof) and waits for your tap on "
             "every step. You can change this any time, and set it per step later.",
             title="Automatic or manual", why="So the twin runs at the autonomy YOU want - it always stops at "
             "client-facing gates either way."),
    Question("example", "examples", "Walk me through one real ticket you handled and how you solved it.",
             required=False, title="A worked example", why="One real example teaches the twin more than rules alone."),
)


def profile_path() -> Path:
    return paths.data_dir() / "profile" / "profile.json"


def load_profile(path: Path | None = None) -> dict[str, str]:
    """The already-saved answers ({key: answer}), empty if none. Lets a re-run skip what's done."""
    p = path or profile_path()
    if p.exists():
        try:
            return dict(json.loads(p.read_text(encoding="utf-8")).get("profile", {}))
        except (json.JSONDecodeError, OSError, AttributeError):
            return {}
    return {}


def missing_required(answers: dict[str, str]) -> list[str]:
    """Keys of required questions with no (non-blank) answer."""
    return [q.key for q in QUESTIONS if q.required and not (answers.get(q.key) or "").strip()]


def seed_memory(mem: MemoryDB, answers: dict[str, str]) -> int:
    """Store each answered question as one AMBER system-map fact (source 'interview'). Returns count stored."""
    n = 0
    for q in QUESTIONS:
        ans = (answers.get(q.key) or "").strip()
        if not ans:
            continue
        system_map.add_fact(
            mem,
            topic=f"{q.topic}: {q.key}",
            fact=f"{q.prompt} -> {ans}",
            evidence="onboarding interview (self-stated)",
            trust="AMBER",
            source="interview",
        )
        n += 1
    return n


def save_profile(answers: dict[str, str], *, path: Path | None = None) -> Path:
    """Write the profile JSON (the person's own words; local, gitignored data/). Returns the path.

    Merges with anything already saved so a re-run (where blank answers mean 'leave as-is') never wipes
    earlier answers — onboarding is safe to run again to fill the rest in."""
    p = path or profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = dict(load_profile(path=p))
    for q in QUESTIONS:
        val = (answers.get(q.key) or "").strip()
        if val:
            clean[q.key] = val
    p.write_text(json.dumps({"profile": clean}, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def run_interview(answers: dict[str, str], *, mem: MemoryDB | None = None,
                  path: Path | None = None, strict: bool = True) -> dict:
    """Core, non-interactive. Validate -> save profile -> seed memory. Returns a summary dict.

    strict=True raises ValueError if a required answer is missing (so the CLI can refuse to finish half-done)."""
    missing = missing_required(answers)
    if strict and missing:
        raise ValueError(f"interview incomplete, missing required answers: {', '.join(missing)}")
    saved = save_profile(answers, path=path)
    own = mem or MemoryDB()
    try:
        stored = seed_memory(own, answers)
    finally:
        if mem is None:
            own.close()
    return {"profile_path": str(saved), "facts_stored": stored, "missing": missing,
            "answered": [q.key for q in QUESTIONS if (answers.get(q.key) or "").strip()]}


def interactive(ask=input, out=print, *, mem: MemoryDB | None = None, path: Path | None = None,
                chained: bool = False) -> dict:
    """Walk the person through the interview with numbered steps and a one-line 'why' per question.

    ask/out are injectable for tests. Each prompt validates/re-asks through wizard.ask_value (bounded, so
    scripted input never hangs). Idempotent: a re-run shows what's already answered and asks only the rest.
    chained=True means setup runs straight after (run_wizard), so the "now run setup" hand-off is skipped."""
    existing = load_profile(path=path)
    total = len(QUESTIONS)
    out("Welcome - let's teach your digital twin to work like you. I'll ask a few questions in plain words;")
    out("answer however you'd say it. There are no wrong answers, and you can re-run this anytime to add more.\n")
    if existing:
        out(f"Picking up where you left off - {len(existing)} of {total} already answered, I'll only ask the rest.\n")

    answers: dict[str, str] = dict(existing)
    for i, q in enumerate(QUESTIONS, 1):
        if (existing.get(q.key) or "").strip():
            out(f"[have] {wizard.step_line(i, total, q.title or q.key)} (already answered)")
            continue
        out(f"\n{wizard.step_line(i, total, q.title or q.key)}")
        if q.why:
            out(f"  Why: {q.why}")
        tag = "  (optional - press Enter to skip)" if not q.required else ""
        answers[q.key] = wizard.ask_value(
            lambda text: ask(text), f"{q.prompt}{tag}\n> ", optional=not q.required, say=out)

    summary = run_interview(answers, mem=mem, path=path, strict=False)
    out(f"\nThanks. Saved your profile to {summary['profile_path']} and learned {summary['facts_stored']} "
        f"things about how you work.")
    if summary["missing"]:
        out(f"Still blank (re-run anytime to fill): {', '.join(summary['missing'])}")
    if not chained:
        out("\nNext step - give the twin its keys and logins:  python -I aa.py setup")
    return summary


def run_wizard(ask: Callable[[str, bool], str], out=print, *, mem: MemoryDB | None = None,
               backend=None, path: Path | None = None) -> dict:
    """The one smooth wizard: interview, then setup, then the setup checklist - all from a single run.

    ``ask(prompt, is_secret) -> str`` is adapted to the interview (which never needs a hidden prompt) and
    passed straight to setup (which does). Returns {"interview": ..., "setup": ...}."""
    from . import setup
    out("=== Welcome to Digital Twin onboarding ===")
    out("Two short parts: first a few questions about how you work, then your keys and logins.\n")
    interview_summary = interactive(ask=lambda prompt: ask(prompt, False), out=out,
                                    mem=mem, path=path, chained=True)
    out("\n=== Part 2: your keys and logins ===")
    setup_summary = setup.interactive(ask=ask, out=out, backend=backend, path=path)
    return {"interview": interview_summary, "setup": setup_summary}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="onboard", description="Interview the person to build their twin's profile.")
    ap.add_argument("--answers", default=None, help="JSON file of {question_key: answer} for non-interactive use")
    ap.add_argument("--all", action="store_true",
                    help="run the full wizard: interview then setup (keys/logins) in one sitting")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if a.answers:
        answers = json.loads(Path(a.answers).read_text(encoding="utf-8"))
        try:
            summary = run_interview(answers, strict=True)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if a.all:
        import getpass
        ask = lambda prompt, is_secret: (getpass.getpass(prompt + " ") if is_secret else input(prompt + " "))
        run_wizard(ask)
        return 0

    interactive()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
