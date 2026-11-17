"""The confirm-understanding draft must never leak the ticket description's section labels or a word cut in half.

The bug (live, 2026-11-16): the draft read "you're seeing What the client said (English) Hi, my invoice export... What the
client expects The proble... on Accounting". The slots must hold ONE clean phrase about the problem."""
from __future__ import annotations

import re

import pytest

from twin_agent.hub import analyse as analyse_mod, teamhub_threads as kt, odoo_ticket_watch as otw, slots, templates, ticket_text
from twin_agent.hub.model import Inbound

from test_hub_production import build, payload

LABELS = ("What the client said", "What the client expects", "(English)", "Steps to reproduce", "Attachments and evidence",
          "Suggested owner", "Open questions", "Original message", "Urgency")
EN = ("Hi Support team, my invoice export to PDF is showing the wrong total for customers who have a discount. "
      "It started yesterday after the update, and we need it fixed before the month-end closing. Please check.")
AR = "السلام عليكم، تقرير الفواتير بيظهر الإجمالي غلط للعملاء اللي عليهم خصم. ده بدأ امبارح بعد التحديث وعايزينه يتصلح قبل قفل الشهر."
LONG = ("my invoice export to PDF is showing the wrong total for customers who have a very large discount applied on "
        "the second page of the report and nobody on the accounting team can find where the figure comes from")


def description_text(msg: str, lang: str = "en") -> str:
    """A real hub ticket description, flattened the way ``odoo_ticket_watch.plain_text`` reads it from Odoo."""
    inbound = Inbound(message_id="w1", wa_id="201001112233", name="Mia", kind="text", text=msg, lang=lang)
    a = analyse_mod.analyse(msg, lang=lang)
    html = ticket_text.build_description(a, inbound, ticket_text.deterministic(a, inbound), suggested="Riley: default owner")
    return otw.plain_text(html)


def assert_clean(draft: str, *, arabic: bool = False) -> str:
    for label in LABELS:
        assert label not in draft, label
    assert "…" not in draft and "..." not in draft and "  " not in draft and "\n" not in draft
    slot = re.search(r"seeing (.+?)(?: on [^.]+)?\. (?:Is that correct|Before we start)", draft) if not arabic else re.search(r"بتواجه (.+?)(?: في [^.]+)?\. (?:صح|قبل)", draft)
    assert slot, draft
    issue = slot.group(1)
    assert not re.search(r"[.!?؟]", issue)                       # the problem is a phrase, not a second sentence
    assert issue == issue.strip() and len(issue) <= 140
    return issue


# ---------------------------------------------------------------- the live-bug shape

def test_the_exact_leaked_text_gives_a_clean_draft():
    leaked = ("What the client said (English) Hi, my invoice export… What the client expects The proble… "
              "Steps to reproduce Not given yet")
    draft = templates.confirm("en", name="Mia", issue=leaked, module="Accounting", outcome="What the client expects The proble…", n=7)
    issue = assert_clean(draft)
    assert issue == "an issue where your invoice export"
    assert draft.count("?") == 1 and draft.endswith("— Acme Support")


def test_a_whole_multi_section_description_gives_a_natural_one_sentence_draft():
    desc = description_text(EN)
    assert "What the client said (English)" in desc and "What the client expects" in desc          # it IS the real shape
    a = analyse_mod.analyse(desc)
    draft = templates.confirm("en", name="Mia", issue=a.summary, module=a.module, outcome=a.desired_outcome, n=9)
    issue = assert_clean(draft)
    assert issue == "an issue with your invoice export to PDF"
    assert draft.startswith("Hello Mia (ticket #9), to make sure we solve the right thing: you're seeing an issue with your invoice export")
    assert draft.count("?") == 1 and draft.endswith("— Acme Support")


def test_the_teamhub_watch_path_feeds_the_clients_own_words_not_the_description():
    v = {"id": 7, "client": "Mia", "text": description_text(EN), "is_test": True, "owner": None, "name": "TEST · x"}
    st = kt.Settings(room_id="r", workflow_id="w", teamhub_exe="k", workdir="w", state_dir="s", default_approver="a1")
    steps = otw.build_steps(st, v, "https://odoo.example/7")
    assert steps["draft"] and assert_clean(steps["draft"]).startswith("an issue with your invoice export to PDF")
    assert "What the client" not in steps["analyst"] and "Understanding: Hi Support team, my invoice export" in steps["analyst"]


def test_the_hub_confirm_draft_from_a_real_whatsapp_message_is_clean():
    hub, _wa = build()
    r = hub.handle_webhook(payload(text=EN))
    state = hub.ticket(r["ticket_id"])
    assert_clean(hub._confirm_draft(state))
    assert state.analysis.summary.startswith("Hi Support team, my invoice export to PDF") and "…" not in state.analysis.summary


def test_an_already_stored_dirty_analysis_is_cleaned_when_the_draft_is_built():
    hub, _wa = build()
    r = hub.handle_webhook(payload(text=EN))
    state = hub.ticket(r["ticket_id"])
    state.analysis.summary = "What the client said (English) Hi, my invoice export… What the client expects The proble…"
    state.analysis.desired_outcome = "What the client expects The proble…"
    assert_clean(hub._confirm_draft(state))


# ---------------------------------------------------------------- word boundaries

@pytest.mark.parametrize("limit", [20, 40, 60, 100])
def test_a_long_problem_is_cut_on_a_word_boundary_with_no_ellipsis(limit):
    out = slots.plain_slot(LONG, limit)
    assert out and "…" not in out and "..." not in out and len(out) <= limit
    assert LONG.startswith(out) and (len(out) == len(LONG) or LONG[len(out)] == " ")        # a whole word ends it
    assert out.split()[-1] not in slots._DANGLING


def test_the_default_summary_for_a_long_message_ends_on_a_whole_word():
    a = analyse_mod.analyse(LONG * 2)
    assert "…" not in a.summary and len(a.summary) <= 160 and (LONG * 2).startswith(a.summary)
    assert (LONG * 2)[len(a.summary)] == " "


def test_a_single_very_long_word_is_kept_whole():
    word = "x" * 200
    assert slots.plain_slot(word, 50) == word


@pytest.mark.parametrize("text,want", [
    ("Hi, my invoice is wrong.", "my invoice is wrong"),
    ("Hello team,\nthe stock count is off by 3 units. We counted twice.", "the stock count is off by 3 units"),
    ("hey there! the POS closes by itself", "the POS closes by itself"),
    ("Invoice export fails... again and again", "Invoice export fails"),
    ("Good morning, can't print the delivery note", "can't print the delivery note"),
    ("", ""), ("   ", ""), ("Hi", ""),
])
def test_plain_slot_cases(text, want):
    assert slots.plain_slot(text) == want


def test_a_missing_slot_gets_a_plain_phrase_not_a_blank():
    d = templates.confirm("en", name="Mia", issue="", module="Sales", outcome="", n=1)
    assert "you're seeing a problem on Sales. Is that correct?" in d


# ---------------------------------------------------------------- Arabic keeps working

def test_arabic_message_gives_a_clean_arabic_draft():
    hub, _wa = build()
    r = hub.handle_webhook(payload(text=AR))
    state = hub.ticket(r["ticket_id"])
    assert state.lang == "ar" and state.analysis.summary.startswith("السلام عليكم، تقرير الفواتير")
    issue = assert_clean(hub._confirm_draft(state), arabic=True)
    assert issue.startswith("تقرير الفواتير") and not re.search("[A-Za-z]", issue)


def test_an_arabic_ticket_description_gives_the_original_arabic_not_the_not_translated_note():
    desc = description_text(AR, "ar")
    assert "Not translated yet" in desc
    msg = slots.client_message(desc)
    assert msg.startswith("السلام عليكم") and "Not translated" not in msg
    a = analyse_mod.analyse(desc)
    draft = templates.confirm("ar", name="منى", issue=a.summary, module=a.module, outcome=a.desired_outcome, n=3)
    assert_clean(draft, arabic=True)
    assert "Not translated" not in draft and "Hi" not in draft.split("بتواجه")[1]


def test_arabic_is_cut_on_a_word_boundary_too():
    body = AR.split("، ", 1)[1] * 3
    out = slots.plain_slot(body, 50)
    assert 0 < len(out) <= 50 and body.startswith(out) and body[len(out)] == " " and "…" not in out


def test_text_that_is_not_a_hub_description_is_left_alone():
    assert slots.client_message("What do you expect?") == "What do you expect?"
    assert not slots.is_hub_description("the client said it is wrong")


# ---------------------------------------------------------------- review round: summary kept whole, smarter phrase pick

@pytest.mark.parametrize("text,phrase", [
    ("السلام عليكم… عندي مشكلة في الفاتورة مش بتترحل…", "عندي مشكلة في الفاتورة مش بتترحل"),
    ("Hi… the invoice is broken", "the invoice is broken"),
    ("Hello team. Thanks. The stock move fails", "The stock move fails"),
    ("Hello, I have a problem. The invoice report shows wrong totals", "I have a problem"),
    ("Thank you.\nPlease. The POS closes by itself", "The POS closes by itself"),
    ("شكراً. تسلم ايدك. الاوردر مش بيتأكد", "الاوردر مش بيتأكد"),
])
def test_ellipsis_is_a_break_and_greeting_or_thanks_only_sentences_are_skipped(text, phrase):
    assert slots.plain_slot(text) == phrase


def test_the_request_summary_keeps_the_whole_ask_not_the_first_sentence():
    for text in ("السلام عليكم… عندي مشكلة في الفاتورة مش بتترحل", "Hello team. Thanks. The stock move fails",
                 "Hi… the invoice is broken"):
        assert analyse_mod.analyse(text).summary == text
    long = "Hello team. " + "the stock move fails when we validate the receipt " * 6
    s = analyse_mod.analyse(long).summary
    assert len(s) <= 160 and long.startswith(s) and long[len(s)] == " " and "…" not in s
    assert analyse_mod.analyse("What the client said (English) the stock move fails").summary == "the stock move fails"
    assert analyse_mod.analyse("").summary == "(voice note / empty)"


def test_the_draft_keeps_the_problem_when_the_message_opens_with_a_greeting():
    d = templates.confirm("ar", name="منى", issue="السلام عليكم… عندي مشكلة في الفاتورة", module="Accounting", outcome="x", n=1)
    assert "بتواجه عندي مشكلة في الفاتورة في" in d
    d = templates.confirm("en", name="Mia", issue="Hello team. Thanks. The stock move fails", module="Inventory", outcome="", n=1)
    assert "you're seeing an issue where the stock move fails on Inventory" in d


@pytest.mark.parametrize("text,phrase", [
    ("1. Invoice 245 won't post", "Invoice 245 won't post"),
    ("2) The PDF is blank", "The PDF is blank"),
    ("1.\nInvoice 245 won't post", "Invoice 245 won't post"),
    ("We use Odoo v. 17 and e.g. the POS crashes", "We use Odoo v. 17 and e.g. the POS crashes"),
    ("Total is 3.5 off. Please check", "Total is 3.5 off"),
    ("- the stock count is wrong", "the stock count is wrong"),
])
def test_list_numbers_and_abbreviations_do_not_end_the_sentence(text, phrase):
    assert slots.plain_slot(text) == phrase
    d = templates.confirm("en", name="Mia", issue=text, module="Accounting", outcome="", n=1)
    assert "you're seeing 1 on" not in d and "you're seeing an issue" in d


@pytest.mark.parametrize("text,phrase", [
    ("اهلا يا جماعة، الفاتورة مش بتترحل", "الفاتورة مش بتترحل"),
    ("سلام عليكم ازيك الفاتورة غلط", "الفاتورة غلط"),
    ("أهلاً وسهلاً بيكم، التقرير فاضي", "التقرير فاضي"),
    ("يا جماعة الفاتورة مش بتطبع", "الفاتورة مش بتطبع"),
    ("صباح الخير يا فندم، المخزون مش مظبوط", "المخزون مش مظبوط"),
    ("مرحبا، الكاشير بيقفل لوحده", "الكاشير بيقفل لوحده"),
    ("الا ان الفاتورة غلط", "الا ان الفاتورة غلط"),
])
def test_egyptian_greetings_without_hamza_are_dropped(text, phrase):
    assert slots.plain_slot(text) == phrase
    d = templates.confirm("ar", name="منى", issue=text, module="Accounting", outcome="", n=1)
    assert "اهلا" not in d.split("بتواجه")[1] and "يا جماعة" not in d.split("بتواجه")[1]
