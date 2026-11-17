"""Persona templates (07 section 6): fixed texts, only <...> fields filled by code."""
from __future__ import annotations

from datetime import date

import pytest

from twin_agent import persona
from twin_agent.checks.reply_checker import check_reply
from twin_agent.models import Channel


def test_out_of_hours_holding_exact_text():
    assert persona.holding_telegram(in_hours=False) == (
        "Received. Working hours are 10am to 6pm, Sun to Thu. This will be checked first thing.\n"
        "Can you work at all, or is something fully blocked?\n- Principal's assistant")


@pytest.mark.parametrize("shot,ref,ask", [
    (False, False, " Send the error screenshot and the document number please."),
    (True, False, " Send the document number please."),
    (False, True, " Send the error screenshot please."),
    (True, True, ""),
])
def test_in_hours_holding_asks_only_for_what_is_missing(shot, ref, ask):
    text = persona.holding_telegram(in_hours=True, has_screenshot=shot, has_ref=ref)
    assert text == f"Received, checking it now.{ask}\n- Principal's assistant"


def test_evidence_ask_franco_and_none():
    assert persona.evidence_ask(has_screenshot=True, has_ref=False, lang="franco") == "Rakam el document kam?"
    assert persona.evidence_ask(has_screenshot=True, has_ref=True) is None


def test_fully_blocked_only_after_card_delivered():
    assert persona.fully_blocked_telegram(card_delivered=False) is None
    assert persona.fully_blocked_telegram(card_delivered=True).startswith("Noted that you are fully blocked.")


def test_ticket_chase_strips_token_and_rejects_other_hosts():
    text = persona.ticket_chase_telegram("https://helpdesk.example.com/my/ticket/2641/AbCdEf123456?x=1")
    assert "https://helpdesk.example.com/my/ticket/2641" in text and "AbCdEf123456" not in text
    with pytest.raises(ValueError):
        persona.ticket_chase_telegram("https://evil.example/my/ticket/1")


def test_email_holding_fills_only_name_restatement_date():
    text = persona.email_holding(name="Radwa Test", restatement="the pricelist import.",
                                 update_date=date(2026, 11, 3))
    assert text.startswith("Hi Radwa,\n\nNoted. I am checking the pricelist import.\n")
    assert "You will have an update by Tuesday 3 November." in text
    assert text.endswith("- Principal's assistant")
    # A name code cannot trust (non-Latin, markup) falls back to a bare "Hi," so the holding floor never fails.
    odd = persona.email_holding(name="<script>", restatement="x", update_date=date(2026, 11, 3))
    assert odd.startswith("Hi,\n\nNoted.") and "<script>" not in odd
    assert persona.email_holding(name="", restatement="x", update_date=date(2026, 11, 3)).startswith("Hi,\n")
    with pytest.raises(ValueError):
        persona.email_holding(name="Radwa", restatement="", update_date=date(2026, 11, 3))


def test_email_approver_needs_named_approver():
    assert "*Hoda:* please confirm" in persona.email_approver(name="Fathi", approver="Hoda")
    with pytest.raises(ValueError):
        persona.email_approver(name="Fathi", approver="Somebody")


@pytest.mark.parametrize("text,channel", [
    (persona.holding_telegram(in_hours=False), Channel.telegram_group),
    (persona.holding_telegram(in_hours=True, has_screenshot=False, has_ref=False), Channel.telegram_group),
    (persona.fully_blocked_telegram(card_delivered=True), Channel.telegram_group),
    (persona.ticket_chase_telegram("https://helpdesk.example.com/my/ticket/2641"), Channel.telegram_group),
    (persona.email_holding(name="Principal", restatement="the transfer", update_date=date(2026, 11, 3)), Channel.email),
    (persona.email_approver(name="Principal", approver="Signatory"), Channel.email),
])
def test_every_template_passes_the_reply_checker(text, channel):
    rc = check_reply(text, channel=channel, lang="en", is_template=True)
    assert rc.ok, rc.problems


def test_template_override_must_keep_placeholders(tmp_path):
    f = tmp_path / "holding.yaml"
    f.write_text('tg_in_hours: "Got it, checking now.{ask}"\ntg_ticket_chase: "No placeholder here"\n',
                 encoding="utf-8")
    t = persona.load_templates(f)
    assert t["tg_in_hours"] == "Got it, checking now.{ask}"
    assert t["tg_ticket_chase"] == persona.DEFAULT_TEMPLATES["tg_ticket_chase"]
    assert persona.load_templates(tmp_path / "missing.yaml") == persona.DEFAULT_TEMPLATES


def test_rules_and_channel_family():
    assert "Never Arabic script" in persona.rules_for("telegram")
    assert "Never type a sign-off" in persona.rules_for("email")
    assert persona.channel_family("telegram_dm") == "telegram"
    assert persona.channel_family("ticket_mail") == "acme"
    assert persona.channel_family("email") == "email"
