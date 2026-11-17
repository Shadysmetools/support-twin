"""Review #16 (+ keyword boundaries): Arabic client messages must be Arabic end to end, and the fake judge must not
pass a mostly-English text just because it contains one Arabic character."""
from __future__ import annotations

import re

from twin_agent.hub import analyse, judge, templates

ALLOWED_LATIN = {"Acme"}


def _latin_words(text: str, extra: set[str] = frozenset()) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z]{2,}", text) if w not in ALLOWED_LATIN | set(extra)]


def test_ar_confirm_with_open_questions_has_no_english_fragments():
    a = analyse.classify("عندي مشكلة", client_context={"modules": ["Inventory"]})    # no module hint, no evidence
    assert a.open_questions, "this input should raise open questions (Template 2b)"
    text = templates.confirm("ar", name="عمر", issue=a.summary, module=a.module, outcome=a.desired_outcome,
                             questions=a.open_questions)
    assert _latin_words(text) == [], text


def test_ar_confirm_localises_the_module_name():
    a = analyse.classify("المخزون الكمية غلط")
    assert a.module == "Inventory"
    text = templates.confirm("ar", name="عمر", issue=a.summary, module=a.module, outcome=a.desired_outcome,
                             questions=a.open_questions)
    assert "Inventory" not in text and _latin_words(text) == []


def test_ar_feature_outcome_is_arabic():
    a = analyse.classify("عايز تقرير جديد للمبيعات")
    assert a.nature == "feature" and _latin_words(a.desired_outcome) == []


def test_en_questions_and_outcome_stay_english():
    a = analyse.classify("The report is broken")
    assert re.search(r"[A-Za-z]{4,}", a.desired_outcome) and "screenshot" in a.open_questions[0]


def test_ar_ack_next_step_is_arabic():
    for has_q in (True, False):
        step = templates.next_step("ar", has_questions=has_q)
        assert _latin_words(step) == [] and step
        assert templates.next_step("en", has_questions=has_q).isascii()


def test_ar_resolve_localises_module():
    text = templates.resolve("ar", name="عمر", n=5, what=f"{templates.module_label('ar', 'Inventory')}: x",
                             link="https://k.invalid/s", video="https://k.invalid/s")
    assert "Inventory" not in text


def test_fake_judge_language_is_a_ratio_not_any_arabic_character():
    j = judge.FakeJudge()
    mixed = "Hello, we will check this right away and update you on the ticket very soon. ع"
    bad = j.score("draft", {"text": mixed, "expect_lang": "ar"})
    assert bad["scores"]["language"] <= 0.3
    ar = templates.confirm("ar", name="Oscar", issue="مشكلة", module="Inventory", outcome="x", questions=[])
    good = j.score("draft", {"text": ar, "expect_lang": "ar"})              # Latin name + Acme tolerated
    assert good["scores"]["language"] >= 0.9
    en = j.score("draft", {"text": "Hello, all good here. — Acme Support", "expect_lang": "en"})
    assert en["scores"]["language"] >= 0.9


def test_keywords_match_whole_words_not_substrings():
    for text, bad_module in (("I went through the settings three times", "HR"),
                             ("What is the purpose of this?", "POS"),
                             ("Please add my address to the invoice", None)):
        a = analyse.classify(text)
        if bad_module:
            assert a.module != bad_module, text
    a = analyse.classify("Please add my address to the invoice")
    assert a.ticket_type == "New Request"                                  # "add" as a word, not inside "address"
    assert analyse.classify("What is the purpose of this?").ticket_type == "Inquiries"
    assert analyse.classify("Please add a new field").ticket_type == "New Request"
    assert analyse.classify("the hr module is broken").module == "HR"      # a genuine whole-word hit still works
