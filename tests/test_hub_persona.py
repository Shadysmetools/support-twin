"""The persona (single source of client wording), English-only default, and the pre-send wording lint."""
from __future__ import annotations

import copy

import pytest
import yaml

from twin_agent.hub import persona, slots, templates, wording_lint
from twin_agent.hub.persona import PersonaError


LIVE = "TEST: Hi, my invoice export to PDF shows the wrong total for discounted customers."


def doc():
    return yaml.safe_load((persona.HERE / "messages.yaml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- loading

def test_the_default_is_english_only_and_the_real_file_validates():
    assert persona.client_languages() == ["en"] and doc()["client_languages"] == ["en"]
    assert persona.validate(doc())


def test_every_arabic_entry_is_marked_for_native_review():
    d = doc()
    for k in ("sign", "issue_fallback"):
        assert d[k]["needs_native_review"] is True
    assert all(m["needs_native_review"] is True for m in d["messages"].values())


@pytest.mark.parametrize("mutate", [
    lambda d: d["messages"].pop("confirmation"),
    lambda d: d["messages"]["ack" if "ack" in d["messages"] else "confirm"].pop("ar"),
    lambda d: d["messages"]["confirm"].update(en="Hello {who}, {nope}"),
    lambda d: d["messages"]["confirm"].update(needs_native_review=False),
    lambda d: d.update(client_languages=[]),
    lambda d: d.update(client_languages=["fr"]),
    lambda d: d.pop("sign"),
])
def test_a_broken_messages_file_fails_closed(mutate):
    d = copy.deepcopy(doc())
    mutate(d)
    with pytest.raises(PersonaError):
        persona.validate(d)


def test_an_unreadable_file_fails_closed(tmp_path):
    with pytest.raises(PersonaError):
        persona.load(tmp_path / "missing.yaml")


def test_an_arabic_client_gets_english_until_arabic_is_enabled():
    ar = "السلام عليكم فاتورتي غلط"
    d = templates.confirm("ar", name="منى", issue=ar, module="Accounting")
    assert wording_lint.written_language(d) == "en" and "Acme Support" in d
    persona.set_client_languages(["en", "ar"])
    assert wording_lint.written_language(templates.confirm("ar", name="منى", issue=ar, module="Accounting")) == "ar"


ARABIC_CLIENT = "السلام عليكم، عندي مشكلة في الفواتير مش بتتحفظ"


def test_every_test_here_runs_with_the_production_default():
    assert persona.client_languages() == ["en"]


def test_an_arabic_client_gets_an_english_draft_with_no_arabic_and_no_pasted_words():
    from twin_agent.hub import analyse
    a = analyse.analyse(ARABIC_CLIENT, lang="ar")
    assert a.open_questions and any("؀" <= c <= "ۿ" for q in a.open_questions for c in q)   # the analyser asks in Arabic
    d = templates.confirm("ar", name="Sam", issue=a.summary, module=a.module, outcome=a.desired_outcome,
                          questions=a.open_questions, n=42)
    assert not any("؀" <= c <= "ۿ" for c in d), d
    assert "a problem" in d and "screenshot" in d.lower() or "screen" in d.lower()          # fallback issue + the English question
    assert d.count("?") == 1 and wording_lint.lint(d, client_text=ARABIC_CLIENT, lang="ar") == []


def test_an_untranslatable_arabic_question_is_dropped_not_pasted():
    d = templates.confirm("ar", name="Sam", issue=ARABIC_CLIENT, module="Accounting", questions=["سؤال غير معروف"])
    assert not any("؀" <= c <= "ۿ" for c in d) and "Is that correct?" in d and "Before we start" not in d


def test_a_voice_note_placeholder_summary_is_not_client_text():
    d = templates.confirm("en", name="Sam", issue="(voice note / empty)", module="unknown")
    assert "voice note" not in d and "on unknown" not in d and "you're seeing a problem." in d


def test_the_module_is_left_out_when_unknown_and_the_question_reads_naturally():
    d = templates.confirm("en", name="Sam", issue="my export fails", module="unknown",
                          questions=["a screenshot with the page URL, or a short screen recording, so we can reproduce it"])
    assert " on unknown" not in d and "could you please share a screenshot" in d


def test_an_introduction_is_not_turned_into_an_issue():
    s = slots.issue_phrase("Hello I am Max from Initech, accounting is broken")
    assert s == ""


def test_the_config_flag_is_refused_when_invalid():
    with pytest.raises(PersonaError):
        persona.set_client_languages(["xx"])


# ---------------------------------------------------------------- the drafter (the 2714 case)

def test_the_live_2714_draft_is_a_one_line_summary_one_question_and_passes_the_lint():
    d = templates.confirm("en", name="Mia", issue=LIVE, module="Accounting")
    assert "TEST" not in d and "Hi," not in d and "my invoice" not in d
    assert d.count("?") == 1 and d.endswith("— Acme Support")
    assert "an issue with your invoice export to PDF" in d
    assert wording_lint.lint(d, client_text=LIVE, lang="en") == []


def test_a_draft_with_open_questions_asks_only_the_first():
    d = templates.confirm("en", name="Mia", issue=LIVE, module="Accounting",
                          questions=["a screenshot with the page URL, so we can reproduce it", "which screen this is about"])
    assert d.count("?") == 1 and "screenshot" in d and "which screen" not in d
    assert wording_lint.lint(d, client_text=LIVE, lang="en") == []


def test_the_ack_resolve_and_fallback_come_from_the_persona_and_pass_the_lint():
    ack = templates.ack("en", name="Mia", n=2714, subject=templates.request_subject("en", "Accounting"))
    assert ack == ("Thank you, Mia. We've opened ticket #2714 – your Accounting request for you. Our team will reach out "
                   "if we have any questions about your request. — Acme Support")
    res = templates.resolve("en", name="Mia", n=2714, what=templates.resolved_what("en", "Accounting"),
                            link="https://x.example/s/abc", video="https://x.example/v/abc")
    fb = templates.fallback_ack("en", name="Mia")
    for text in (ack, res, fb):
        assert wording_lint.lint(text, client_text=LIVE, lang="en") == [], text
    assert LIVE.split(",")[1].strip()[:10] not in res


@pytest.mark.parametrize("key", sorted(persona.REQUIRED))
def test_every_standard_message_passes_the_lint(key):
    m = doc()["messages"][key]
    fields = {f: "Mia" if f in ("name", "who") else "x" for f in m["fields"]}
    fields.update(n="1", link="https://x.example/a", video="https://x.example/v", issue="an issue where your export fails",
                  place=" on Accounting", need="the page URL", what="the issue", step="Open the video and verify the step")
    out = persona.text(key, "en", **fields)
    bad = [r for r in wording_lint.lint(out, client_text="", lang="en") if r not in ("too_short",)]
    assert bad == [], (key, out)


# ---------------------------------------------------------------- the lint rules, one by one

OK = "Hello Mia, we are looking into your request and our team will get back to you soon. — Acme Support"


def test_a_clean_message_passes():
    assert wording_lint.lint(OK, client_text="my export fails", lang="en") == []


@pytest.mark.parametrize("bad", [
    "Hello Mia, One has opened this for you and our team will follow up soon.",
    "Hello Mia, Claude reviewed your request and our team will follow up soon.",
    "Hello Mia, the hub received your request and our team will follow up soon.",
    "Hello Mia, Teamhub passed your request on and our team will follow up soon.",
    "Hello Mia, our automation received your request and the team will follow up.",
    "Hello Mia, an agent is looking at your request and the team will follow up.",
    "Hello Mia, your request is now awaiting_owner and the team will follow up.",
])
def test_internal_names_are_refused(bad):
    assert "internal_name" in wording_lint.lint(bad, client_text="", lang="en")


@pytest.mark.parametrize("fine", ["One moment please while our team checks your request, thank you.",
                                  "Hello Mia, one of our team will get back to you soon, thank you."])
def test_the_english_word_one_is_not_an_internal_name(fine):
    assert "internal_name" not in wording_lint.lint(fine, client_text="", lang="en")


@pytest.mark.parametrize("bad", [
    "What the client said: the export fails. Our team will look into it soon, thank you.",
    "Hello Mia, we are on {{2}} and our team will get back to you soon, thank you.",
    "TEST: Hello Mia, our team will get back to you soon about your request, thank you.",
    "Hello Mia, [[placeholder]] our team will get back to you soon, thank you.",
    "Hello Mia, Steps to reproduce: our team will get back to you soon, thank you.",
])
def test_template_labels_are_refused(bad):
    assert "template_label" in wording_lint.lint(bad, client_text="", lang="en")


def test_a_verbatim_paste_of_eight_words_is_refused():
    client = "the invoice export to pdf shows the wrong total for discounted customers every single time"
    pasted = "Hello Mia, you said the invoice export to pdf shows the wrong total for customers. Is that right?"
    assert "client_text_pasted" in wording_lint.lint(pasted, client_text=client, lang="en")


def test_a_high_overlap_without_an_eight_word_run_is_refused():
    client = "export invoice pdf wrong total discount customers"
    echo = "export invoice pdf wrong total discount customers please"
    assert "client_text_pasted" in wording_lint.lint(echo, client_text=client, lang="en")


def test_a_short_client_message_cannot_trigger_the_paste_rule():
    assert "client_text_pasted" not in wording_lint.lint(OK, client_text="export fails", lang="en")


def test_length_bounds():
    assert "too_short" in wording_lint.lint("Hello.", client_text="", lang="en")
    assert "too_long" in wording_lint.lint("Thank you for your patience. " * 40, client_text="", lang="en")


def test_more_than_one_question_is_refused():
    two = "Hello Mia, which company are you with? And which module is this about, please?"
    assert "more_than_one_question" in wording_lint.lint(two, client_text="", lang="en")
    assert "more_than_one_question" not in wording_lint.lint("Hello Mia, which company are you with, please?",
                                                            client_text="", lang="en")


def test_the_language_must_be_one_the_persona_enables():
    ar = "أهلاً منى، شكراً لتواصلك معنا وسيتواصل معك فريقنا قريباً"
    assert "language_mismatch" in wording_lint.lint(ar, client_text="مرحبا", lang="ar")       # Arabic client, Arabic reply: off
    assert wording_lint.lint(OK, client_text="مرحبا فاتورتي غلط", lang="ar") == []              # Arabic client, English reply: fine
    persona.set_client_languages(["en", "ar"])
    assert "language_mismatch" not in wording_lint.lint(ar, client_text="مرحبا", lang="ar")
    assert "language_mismatch" in wording_lint.lint(OK, client_text="مرحبا", lang="ar")        # enabled: must match the client


def test_reasons_never_quote_the_text():
    secret = "Hello Mia, One saw 201001112233 and the hub knows. Our team will reply soon, thank you."
    assert all(" " not in r and "2010" not in r for r in wording_lint.lint(secret, client_text="", lang="en"))


def test_issue_phrase_uses_the_persona_prefix():
    assert slots.issue_phrase("my export fails").startswith(persona.phrase("issue_prefix", "en"))
