"""Interview onboarding: validates required answers, writes the profile file, and seeds the person's
self-stated rules into system-map memory as AMBER facts (source 'interview')."""
from __future__ import annotations

import json

from twin_agent import creds
from twin_agent.memory import system_map
from twin_agent.memory.store import MemoryDB
from twin_agent.onboarding import interview, setup


def _full_answers() -> dict[str, str]:
    return {q.key: f"answer for {q.key}" for q in interview.QUESTIONS}


def test_missing_required_flags_blanks_not_optional():
    answers = _full_answers()
    answers["role"] = "   "        # required, blanked
    answers["example"] = ""        # optional, blanked
    missing = interview.missing_required(answers)
    assert "role" in missing
    assert "example" not in missing


def test_run_interview_saves_profile_and_seeds_memory(tmp_home, clock):
    answers = _full_answers()
    mem = MemoryDB(clock=clock)
    try:
        summary = interview.run_interview(answers, mem=mem)

        p = interview.profile_path()
        assert p.exists(), "profile file was not written"
        saved = json.loads(p.read_text(encoding="utf-8"))["profile"]
        assert len(saved) == len(interview.QUESTIONS)
        assert saved["name"]

        facts = [f for f in system_map.all_facts(mem.conn) if f["source"] == "interview"]
        assert len(facts) == len(interview.QUESTIONS)
        assert all(f["trust_level"] == "AMBER" for f in facts)
        assert summary["facts_stored"] == len(interview.QUESTIONS)
        assert summary["missing"] == []
    finally:
        mem.close()


def test_run_interview_strict_raises_on_missing_required(tmp_home, clock):
    answers = _full_answers()
    del answers["gates"]           # required, absent
    mem = MemoryDB(clock=clock)
    try:
        raised = False
        try:
            interview.run_interview(answers, mem=mem, strict=True)
        except ValueError:
            raised = True
        assert raised, "strict run should refuse an incomplete interview"
    finally:
        mem.close()


def test_interactive_collects_answers_and_saves(tmp_home, clock):
    answers = _full_answers()
    feed = iter(answers[q.key] for q in interview.QUESTIONS)
    out: list[str] = []
    mem = MemoryDB(clock=clock)
    try:
        summary = interview.interactive(ask=lambda _prompt: next(feed), out=out.append, mem=mem)
        assert summary["facts_stored"] == len(interview.QUESTIONS)
        assert summary["missing"] == []
        assert interview.profile_path().exists()
        assert any("learned" in line for line in out)
        # numbered progress + the next-step bridge to setup are shown
        assert any("Step 1 of" in line for line in out)
        assert any("aa.py setup" in line for line in out)
    finally:
        mem.close()


def test_save_profile_merges_and_does_not_wipe_on_rerun(tmp_home):
    interview.save_profile({"name": "Principal", "role": "Support"})
    # a second run that only fills one more field must keep the earlier answers
    interview.save_profile({"owns": "tickets"})
    saved = json.loads(interview.profile_path().read_text(encoding="utf-8"))["profile"]
    assert saved["name"] == "Principal"
    assert saved["role"] == "Support"
    assert saved["owns"] == "tickets"


def test_interactive_rerun_only_asks_unanswered(tmp_home, clock):
    interview.save_profile({q.key: f"old {q.key}" for q in interview.QUESTIONS if q.key != "escalate"})
    asked_prompts: list[str] = []

    def ask(prompt):
        asked_prompts.append(prompt)
        return "escalate to Sysadmin"

    out: list[str] = []
    mem = MemoryDB(clock=clock)
    try:
        summary = interview.interactive(ask=ask, out=out.append, mem=mem)
        # only the one unanswered question was asked
        assert len(asked_prompts) == 1
        assert "escalate" in asked_prompts[0].lower()
        assert summary["missing"] == []
        saved = json.loads(interview.profile_path().read_text(encoding="utf-8"))["profile"]
        assert saved["name"] == "old name"          # preserved, not wiped
        assert saved["escalate"] == "escalate to Sysadmin"
        assert any("[have]" in line for line in out)
    finally:
        mem.close()


def test_run_wizard_runs_interview_then_setup_in_one_sitting(tmp_home, clock, fake_keyring):
    """The combined `aa.py onboard --all` wizard: one ask(prompt, is_secret) drives both halves."""
    setup_feed = {
        "odoo-sys-principal": '{"login":"a","api_key":"k"}',
        "tg-bot-token": "123456:AAbbcc",
        "wiki": "kc", "elevenlabs": "el",
        "google-oauth-client": '{"client":"x"}', "groq": "gq",
        "d2_voice": "C:/r.wav", "avatar_consent": "yes",
        "qa_user": "qa", "shopify_sandboxed": "confirmed",
    }
    feed = [f"answer for {q.key}" for q in interview.QUESTIONS]          # interview, in order
    feed += [setup_feed[r.key] for r in setup.REQUIREMENTS]             # setup, in order
    it = iter(feed)
    out: list[str] = []
    mem = MemoryDB(clock=clock)
    try:
        result = interview.run_wizard(ask=lambda _p, _s: next(it, ""), out=out.append,
                                      mem=mem, backend=fake_keyring)
        # interview half seeded the profile + memory
        assert result["interview"]["facts_stored"] == len(interview.QUESTIONS)
        assert interview.profile_path().exists()
        # setup half stored the secrets in the vault
        assert creds.has_secret("wiki", backend=fake_keyring)
        assert result["setup"]["missing_required"] == []
        # one smooth run: both section headers and the final checklist are shown
        assert any("Part 2" in line for line in out)
        assert any("setup checklist" in line.lower() for line in out)
        # the chained interview does NOT tell the user to go run setup (it runs right after)
        assert not any("Next step" in line and "aa.py setup" in line for line in out)
    finally:
        mem.close()
