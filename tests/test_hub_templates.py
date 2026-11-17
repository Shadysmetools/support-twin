"""§13 reply templates — bilingual EN/AR, verbatim Arabic, signed 'Acme Support', language follows sender."""
from __future__ import annotations

from twin_agent.hub import templates
from twin_agent.hub.model import detect_lang


def test_ack_template1_both_languages_signed():
    en = templates.ack("en", name="Oscar", n=42, owner="Principal", next_step="confirm our understanding")
    assert en.startswith("Thank you, Oscar. We've opened ticket #42") and "Our team will reach out" in en
    assert en.endswith("— Acme Support")
    ar = templates.ack("ar", name="Oscar", n=42, owner="أليكس", next_step="نأكد الفهم")
    assert ar.startswith("شكراً Oscar. تم فتح التذكرة رقم #42") and "#42" in ar
    assert ar.endswith("— دعم Acme")


def test_confirm_template2_and_2b_open_questions():
    t2 = templates.confirm("en", name="Oscar", issue="a crash", module="POS", outcome="it fixed")
    assert "Is that correct?" in t2 and "Before we start" not in t2
    t2b = templates.confirm("en", name="Oscar", issue="a crash", module="POS", outcome="it fixed",
                            questions=["the page URL", "a screenshot"])
    assert "Before we start" in t2b and "the page URL" in t2b and "a screenshot" not in t2b     # ONE question per message
    assert t2b.count("?") == 1
    # AR 2b uses Arabic-Indic numerals and the verbatim lead-in
    t2b_ar = templates.confirm("ar", name="Oscar", issue="تعطل", module="المخزون", outcome="يشتغل",
                               questions=["اللينك", "صورة"])
    assert "اللينك" in t2b_ar and "صورة" not in t2b_ar and t2b_ar.endswith("— دعم Acme")


def test_resolve_template3_and_3b_video():
    t3 = templates.resolve("en", name="Oscar", n=7, what="cleared the cache", link="https://x/verify")
    assert "resolved" in t3 and "short video" not in t3 and t3.endswith("— Acme Support")
    t3b = templates.resolve("en", name="Oscar", n=7, what="cleared the cache", link="https://x/verify",
                            video="https://wiki.invalid/s/c/7")
    assert "short video showing it working" in t3b and "wiki.invalid" in t3b
    t3b_ar = templates.resolve("ar", name="Oscar", n=7, what="اتمسح الكاش", link="https://x",
                               video="https://wiki.invalid/s/c/7")
    assert "فيديو قصير بيوضّح إنها شغّالة" in t3b_ar


def test_language_follows_sender():
    assert detect_lang("مرحبا كيف الحال") == "ar"
    assert detect_lang("hello there") == "en"
    assert detect_lang("the مخزون is wrong") == "ar"   # any Arabic char -> ar
