import types

import pytest

from twin_agent.hub import intake_buttons as ib


def test_buttons_shape_and_titles():
    for lang in ("en", "ar"):
        for allowed in (None, ["en", "ar"]):
            p = ib.build_type_buttons(lang, allowed)
            assert p["type"] == "interactive" and p["interactive"]["type"] == "button"
            b = p["interactive"]["action"]["buttons"]
            assert [x["reply"]["id"] for x in b] == ["type_bug", "type_feature", "type_question"]
            assert all(x["type"] == "reply" and 0 < len(x["reply"]["title"]) <= 20 for x in b)
            assert p["interactive"]["body"]["text"]


def test_english_only_by_default_and_ar_when_enabled():
    en = ib.build_type_buttons("en")
    assert ib.build_type_buttons("ar") == en
    assert ib.build_type_buttons("ar", ["en", "ar"]) != en
    assert [x["reply"]["title"] for x in en["interactive"]["action"]["buttons"]] == [
        "Bugs & issues", "Features & requests", "Question"]
    assert ib.reask_text("ar", 1) == ib.reask_text("en", 1)


@pytest.mark.parametrize("bid,kind", [("type_bug", "bug"), ("type_feature", "feature"), ("type_question", "question")])
def test_button_reply_event(bid, kind):
    ev = {"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": bid, "title": "x"}}}
    assert ib.parse_type_reply(ev) == kind
    assert ib.parse_type_reply({"interactive": {"button_reply": {"id": bid}}}) == kind


@pytest.mark.parametrize("text,kind", [
    ("1", "bug"), ("2", "feature"), ("3", "question"), ("Bug", "bug"), ("bugs", "bug"), (" Issue. ", "bug"),
    ("feature", "feature"), ("Features", "feature"), ("request", "feature"), ("Question", "question"), ("?", "question"),
    ("مشكلة", "bug"), ("خطأ", "bug"), ("ميزة", "feature"), ("طلب", "feature"), ("سؤال", "question"), ("؟", "question"),
    ("١", "bug"), ("٢", "feature"), ("٣", "question"), ("Bugs & issues", "bug"),
])
def test_typed(text, kind):
    assert ib.parse_type_reply(text) == kind


@pytest.mark.parametrize("bad", ["", "hello", "4", "٤", "my invoice is wrong", None, 5, {"text": {"body": "hi"}}])
def test_unknown(bad):
    assert ib.parse_type_reply(bad) is None


def test_typed_text_event():
    assert ib.parse_type_reply({"type": "text", "text": {"body": "2"}}) == "feature"


def test_subject():
    s = ib.short_subject("TEST: Hello, the invoice PDF total is wrong for discounted customers when printing from the "
                         "sales order screen. Call me on +20 100 123 4567 or a@b.example.org", None)
    assert s.lower().startswith("the invoice pdf")
    assert len(s.split()) <= 10 and len(s) <= 80
    assert "TEST" not in s and "@" not in s and "100" not in s


def test_subject_no_contact_details_even_when_first():
    s = ib.short_subject("a@b.example.org +20 100 123 4567 cannot log in to the portal", None)
    assert "@" not in s and "123" not in s and "log in" in s


def test_subject_arabic_and_fallback():
    s = ib.short_subject("مرحبا، الفاتورة بتظهر الإجمالي غلط للعملاء اللي عندهم خصم وده بيتكرر كل يوم في الشغل", None)
    assert s and len(s.split()) <= 10 and "غلط" in s
    assert ib.short_subject("", types.SimpleNamespace(summary="Login fails on mobile")) == "Login fails on mobile"
    assert ib.short_subject("", None) == ""


def test_reask():
    assert ib.reask_text("en", 1) != ib.reask_text("en", 2)
    assert ib.reask_text("ar", 1, ["ar"]) != ib.reask_text("en", 1)


def test_a_forged_id_is_not_a_type():
    assert ib.parse_type_reply("id:zzz") is None and ib.parse_type_reply("id:bug") == "bug"
