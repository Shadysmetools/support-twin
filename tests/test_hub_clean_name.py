"""``templates._clean_name``: the WhatsApp profile name is client-controlled and is greeted back in AUTOMATIC messages. It is
an ALLOWLIST (letters, combining marks, spaces, an apostrophe, a hyphen between letters, and a "." only in an exact title
form), so no link can survive however it is wrapped, split or joined. Every other character becomes a SPACE, never a
deletion, so removing one can never join two pieces into a domain."""
from __future__ import annotations

import re

import pytest

from twin_agent.hub import templates

c = templates._clean_name
_TITLE_DOT = re.compile(r"\b(Dr|Mr|Mrs|Ms|Eng|Prof|Sh|M|A)\.(?=[^\W\d_])")

LINKS = [
    # plain and wrapped
    "evil.de/x", "evil.ai/login", "evil.in/pay", "evil.gg/x", "evil.page/x", "evil.icu/x", "Principal evil.de/pay now",
    "x.com", "go.com", "go.com/x", "ab.shop", "evil.click", "evil.pro", "evil.ai", "evil.co", "evil.me", "bit.ly/x",
    "evil.su/x", "evil.com", "EVIL.COM/x", "sub.evil.co.uk/x", "t.me/evil", "wa.me/201234567890", "m.me", "m.me/evil",
    "(evil.com)", "evil.com,", "evil.com.", '"evil.com"', "evil.com:8080", "Principal,evil.com", "Principal:evil.com",
    "a@evil.example.org", "@evil.com", "evil.com!", "<evil.com>", "[evil.com]", "evil.com،", "«evil.com»",
    "evil.com…", "ms.com", "mr.com", "dr.com", "prof.ai", "sh.com", "eng.de", "m.facebook.com", "Dr.Principal.com",
    "evil。com", "evil｡com", "evil．com/x", "WWW.evil.com", "HTTPS://x", "HTTPS://evil.x Bob",
    "evil.com​/x", "evil.sexy", "evil.zip", "evil.zip/x", "evil.support?x", "evil.comics", "evil.com/path?x=1#y",
    # r5: empty brackets and joins
    "evil.()com", "evil.[]com", "evil.<>com", "evil.( )com", "evil.«»com", "Principal evil.()com/pay",
    "evil().com", "ev()il.com",
    # r5: separators outside the old list
    "{evil.com}", "|evil.com|", "evil.com-", "evil.com=", "evil.com+", "evil.com&", "evil.com$", "evil.com%",
    "evil.com؟", "evil.com؛", "evil.com、", "evil.com?", "evil.com#", "evil.com/", "evil.com¿",
    "¡evil.com", "evil.com”", "“evil.com”", "evil.com’", "evil.com—", "evil.com–",
    "evil.com۔", "evil.com।",
    # r5: truncation edges
    "Max Ross Leo Executive Xy evil.co m", "Max Ross Leo Executive X evil.co mics",
    "Max Ross Leo Executive Milo(evil.com)", "MaxRossLeoExecutiveMiloXYZWevil.comics",
    "Max Ross Leo Executive X evil.co。m", "Max Ross Leo Executive evil.community",
]


@pytest.mark.parametrize("raw", LINKS)
def test_nothing_link_shaped_survives(raw):
    out = c(raw)
    assert not any(ch in out.replace(". ", " ") for ch in "./@:?#\\|") and len(out) <= 40, (raw, out)
    assert not re.search(r"\.[^\W\d_]", out) and not re.search(r"[^\W\d_]+\.[^\W\d_]{2,}", out), (raw, out)


KEPT = ["Dr.Principal", "M.Salah", "Prof.Al", "Mr.Oscar", "Mrs.Hala", "Ms.Nora", "Eng.Max Leo", "Sh.Executive", "A.Max Leo",
        "Riley O'Brien", "O’Brien", "Jean-Luc", "Principal Cairo", "أليكس محمد",
        "محمد علي", "Zoë Muñoz"]


@pytest.mark.parametrize("name", KEPT)
def test_real_names_are_kept(name):
    assert c(name) == _TITLE_DOT.sub(r"\1. ", name)          # a title dot is rendered "Dr. Principal"


def test_the_title_dot_is_rendered_with_a_space_so_it_can_never_form_a_domain():
    assert c("Dr.Principal") == "Dr. Principal" and c("M.me") == "M. me" and c("Prof.ai") == "Prof. ai"
    assert c("M.Salah") == "M. Salah" and c("Prof.Al") == "Prof. Al" and c("A.io") == "A. io"


def test_arabic_diacritics_and_tatweel_are_kept_but_a_dot_mark_after_latin_is_not():
    import unicodedata
    harakat = "\u0645\u064f\u062d\u064e\u0645\u0651\u064e\u062f"
    assert c(harakat) == unicodedata.normalize("NFKC", harakat)      # NFKC only reorders the marks
    assert c("\u0639\u0628\u062f\u0627\u0644\u0644\u0647 \u0627\u0644\u0640\u0633\u064a\u062f") == \
        "\u0639\u0628\u062f\u0627\u0644\u0644\u0647 \u0627\u0644\u0640\u0633\u064a\u062f"
    assert c("evil\u0307com") == "evil com" and c("evil\ua4f8com") == "evil com"


def test_punctuation_becomes_a_space_not_a_deletion_so_no_join_can_form():
    assert c("Principal (Cairo)") == "Principal Cairo"
    assert c("Principal, Cairo") == "Principal Cairo" and c("Principal - Support") == "Principal Support"
    assert c("evil.()com") == "evil com" and c("Principal@Cairo") == "Principal Cairo"
    assert c("Principal,evil.com") == "Principal evil com"


def test_a_dot_is_kept_only_in_the_exact_title_form():
    assert c("Dr.Principal") == "Dr. Principal" and c("dr.principal") == "dr principal" and c("Principal S.Leo") == "Principal S Leo"
    assert c("Dr.Principal.com") == "Dr. Principal com" and c("Dr. Principal") == "Dr Principal"


def test_hidden_characters_formatting_digits_and_phone_numbers():
    assert c("Oscar­Executive") == "OscarExecutive" and c("ㅤ") == "there" and c("⠀⠀") == "there"
    assert c("Osc\U000e0041ar") == "Oscar" and c("؜Oscar") == "Oscar" and c("Osc​ar") == "Oscar"
    assert c("*bold* _it_ ~s~") == "bold it s"
    assert c("0100 123 4567") == "there" and c("Call 01001234567 now") == "Call now" and c("") == "there"
    assert c(None) == "there" and c("\U0001f642") == "there" and c("https://x.y") == "https x y"


def test_the_cut_to_40_happens_first_and_the_result_never_exceeds_it():
    assert len(c("a" * 90)) == 40
    out = c("Max Ross Leo Executive evil.comics")
    assert len(out) <= 40 and "." not in out


def test_template_1_ack_uses_the_cleaned_name():
    text = templates.ack("en", name="HTTPS://evil.x Bob‮", n=5, owner="Supervisor", next_step="proceed")
    assert "evil.x" not in text and "‮" not in text and "Bob" in text
