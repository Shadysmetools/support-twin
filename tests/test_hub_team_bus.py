"""Swappable team bus (team_bus: telegram|teamhub). Teamhub is POST-ONLY and carries NO client PII; client-send
approvals ALWAYS stay on the Telegram approval gateway. No real network anywhere (FakeTeamhub / fake http)."""
from __future__ import annotations

import json
import logging
import re

import pytest

from twin_agent import config as config_mod
from twin_agent import creds
from twin_agent.hub import fakes, team_bus
from twin_agent.hub.model import Owner
from hub_helpers import approve, build_hub, deliver, load, msg, pending

SECRET_URL = "https://hooks.teamhub.test/in/SECRET-TOKEN-abc123"
OWNER = Owner(uid=7, name="Supervisor", tg_id=11)


def _teamhub(**kw):
    k = fakes.FakeTeamhub(**kw)
    return k, team_bus.TeamhubTeamBus(k, fallback=team_bus.TelegramTeamBus(fakes.FakeTelegram()))


def _both():
    tg = fakes.FakeTelegram()
    k = fakes.FakeTeamhub()
    return [("telegram", team_bus.TelegramTeamBus(tg)), ("teamhub", team_bus.TeamhubTeamBus(k))]


# ---------------------------------------------------------------- contract: every TeamBus behaves the same

@pytest.mark.parametrize("name", ["telegram", "teamhub"])
def test_contract_open_post_triage(name):
    bus = dict(_both())[name]
    assert isinstance(bus, team_bus.TeamBus) and bus.name == name
    ref = bus.open_thread(41, OWNER, "Ticket #41 brief")
    assert isinstance(ref, str) and ref
    assert bus.open_thread(41, OWNER, "Ticket #41 brief") == ref        # a retry gets the SAME thread, never a second
    bus.post(ref, "an update")
    bus.post_triage("a triage line")


def test_telegram_bus_wraps_the_gateway_unchanged():
    tg = fakes.FakeTelegram()
    bus = team_bus.TelegramTeamBus(tg)
    ref = bus.open_thread(41, OWNER, "BRIEF")
    bus.post(ref, "hello")
    bus.post_triage("t")
    assert tg.topics[int(ref)] == {"tid": 41, "owner": "Supervisor", "brief": "BRIEF"}
    assert tg.posts[-1] == {"topic_id": int(ref), "text": "hello"} and tg.triage == ["t"]
    assert bus.carries_client_text is True and bus.owns_approval_topic is True


# ---------------------------------------------------------------- hub wiring + config

def test_default_hub_uses_the_telegram_bus_and_behaves_as_before():
    hub = build_hub()
    assert hub.team_bus.name == "telegram"
    deliver(hub, "webhook_text.json")
    st = hub.all_tickets()[0]
    assert st.topic_id and hub.telegram.topics[st.topic_id]["tid"] == st.tid
    assert any("Ticket #" in p["text"] for p in hub.telegram.posts)


def test_config_teamhub_without_an_injected_bus_raises_never_silently_telegram():
    with pytest.raises(ValueError, match="team_bus"):
        build_hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "team_bus": "teamhub"})
    with pytest.raises(ValueError, match="team_bus"):
        build_hub(cfg={"phone_number_id": "PHONE_NUMBER_ID", "team_bus": "slack"})


def test_validate_config_checks_team_bus():
    base = {"run_mode": "dry_run", "send_enabled": False}
    assert config_mod.validate_config(base) == []
    assert config_mod.validate_config({**base, "team_bus": "teamhub"}) == []
    assert any("team_bus" in e for e in config_mod.validate_config({**base, "team_bus": "slack"}))


def test_resolve_team_bus_telegram_default_and_teamhub_blocks_without_the_secret():
    tg = fakes.FakeTelegram()
    assert team_bus.resolve_team_bus({}, tg).name == "telegram"
    with pytest.raises(team_bus.TeamBusBlocked, match="teamhub-hub"):
        team_bus.resolve_team_bus({"team_bus": "teamhub"}, tg, has_secret=lambda n: False)
    bus = team_bus.resolve_team_bus({"team_bus": "teamhub"}, tg, has_secret=lambda n: n == "teamhub-hub",
                                    get_secret=lambda n: SECRET_URL, http=lambda *a, **k: (200, {}))
    assert bus.name == "teamhub" and SECRET_URL not in repr(bus)


def test_teamhub_hub_is_a_known_vault_name():
    assert "teamhub-hub" in creds.NAMES


# ---------------------------------------------------------------- TeamhubTeamBus behaviour

def test_open_thread_posts_one_thread_per_ticket_with_a_stable_event_id():
    k, bus = _teamhub()
    ref = bus.open_thread(41, OWNER, "Ticket #41 -> @Supervisor\nType Bug")
    bus.open_thread(41, OWNER, "Ticket #41 -> @Supervisor\nType Bug")          # retry
    assert len(k.threads) == 1
    assert k.threads[0]["event_id"] == "ticket-41" and "#41" in k.threads[0]["title"]
    assert ref == "ticket-41"


def test_later_posts_append_when_the_transport_can_else_open_an_update_thread():
    k, bus = _teamhub(append=True)
    ref = bus.open_thread(41, OWNER, "brief")
    bus.post(ref, "owner approved")
    assert len(k.threads) == 1 and k.threads[0]["messages"] == ["owner approved"]
    k2, bus2 = _teamhub(append=False)                                         # the real webhook cannot append
    ref2 = bus2.open_thread(41, OWNER, "brief")
    bus2.post(ref2, "owner approved")
    bus2.post(ref2, "owner approved")                                       # identical text is a NEW event, never swallowed
    assert len(k2.threads) == 3 and "#41" in k2.threads[1]["title"]
    assert len({t["event_id"] for t in k2.threads}) == 3


def test_identical_triage_lines_are_distinct_events():
    k, bus = _teamhub()
    bus.post_triage("tick pass x failed")
    bus.post_triage("tick pass x failed")
    assert len(k.threads) == 2


def test_a_teamhub_outage_never_raises_and_falls_back_to_telegram_with_the_same_safe_text():
    tg = fakes.FakeTelegram()
    k = fakes.FakeTeamhub(fail=RuntimeError(f"boom {SECRET_URL}"))
    bus = team_bus.TeamhubTeamBus(k, fallback=team_bus.TelegramTeamBus(tg))
    ref = bus.open_thread(41, OWNER, "Ticket #41 brief")
    bus.post(ref, "an update")
    bus.post_triage("triage")
    assert ref == "ticket-41" and len(tg.triage) == 3
    assert all("teamhub" in t.lower() for t in tg.triage) and all("SECRET" not in t for t in tg.triage)


def test_the_bus_redacts_phone_email_and_tokens_as_a_second_layer():
    k, bus = _teamhub()
    ref = bus.open_thread(41, OWNER, "x")
    bus.post(ref, "call +20 100 123 4567 or mail oscar@client.example.net token=abcdef123456")
    blob = json.dumps(k.threads)
    assert "123 4567" not in blob and "client-corp" not in blob and "abcdef123456" not in blob


# ---------------------------------------------------------------- the real webhook transport (no network)

def test_webhook_transport_posts_title_content_event_id_and_never_leaks_the_url():
    sent = []

    def http(method, url, **kw):
        sent.append((method, url, kw["json"]))
        return 200, {}

    t = team_bus.WebhookTeamhubTransport(lambda: SECRET_URL, http=http)
    t.open("Ticket #41", "body", "ticket-41")
    assert sent == [("POST", SECRET_URL, {"title": "Ticket #41", "_content": "body", "_event_id": "ticket-41"})]
    assert t.supports_append is False and SECRET_URL not in repr(t)
    with pytest.raises(team_bus.TeamBusBlocked, match="agent"):
        t.append("x", "y", "e")


def test_webhook_transport_errors_carry_only_the_type_or_status(caplog):
    def boom(method, url, **kw):
        raise ConnectionError(f"cannot reach {url}")

    t = team_bus.WebhookTeamhubTransport(lambda: SECRET_URL, http=boom)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(team_bus.TeamhubError) as ei:
            t.open("t", "c", "e")
        k = team_bus.TeamhubTeamBus(t)
        k.post_triage("hi")                                                 # swallowed + logged
    assert "SECRET" not in str(ei.value) and "ConnectionError" in str(ei.value)
    assert "SECRET" not in caplog.text and "teamhub.test" not in caplog.text
    t2 = team_bus.WebhookTeamhubTransport(lambda: SECRET_URL, http=lambda *a, **k: (403, {}))
    with pytest.raises(team_bus.TeamhubError, match="403"):
        t2.open("t", "c", "e")


# ---------------------------------------------------------------- hub end to end with the Teamhub bus

PII = ["Oscar", "Zenith", "oscar@client.example.net", "201000000001", "100 123 4567", "1001234567", "0100"]


class SpyBus:
    """Records EVERY argument the hub hands the bus (the bus's inputs, not just what it later sends)."""

    def __init__(self, inner):
        self.inner, self.calls = inner, []
        self.name, self.carries_client_text = inner.name, inner.carries_client_text
        self.owns_approval_topic = inner.owns_approval_topic

    def open_thread(self, tid, owner, brief):
        self.calls.append(("open_thread", tid, brief))
        return self.inner.open_thread(tid, owner, brief)

    def post(self, ref, text):
        self.calls.append(("post", ref, text))
        return self.inner.post(ref, text)

    def post_triage(self, text):
        self.calls.append(("post_triage", text))
        return self.inner.post_triage(text)

    def blob(self) -> str:
        return json.dumps(self.calls, ensure_ascii=False)


def _teamhub_hub(stt=None, **over):
    k = fakes.FakeTeamhub()
    spy = SpyBus(team_bus.TeamhubTeamBus(k, fallback=None))
    ctx = {3001: {"source": "odoo", "name": "Zenith Trading LLC", "modules": [], "owner_uids": []}}
    seams = dict(team_bus=spy, cfg={"phone_number_id": "PHONE_NUMBER_ID", "team_bus": "teamhub"},
                 knowledge_odoo=fakes.FakeOdooKnowledge(context=ctx))
    if stt is not None:
        seams["stt"] = stt
    seams.update(over)
    return build_hub(**seams), k, spy


def _pii_first(hub):
    p = load("webhook_text.json")
    p["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = (
        "Hi this is Oscar from Zenith Trading, mail oscar@client.example.net or call +20 100 123 4567: stock is negative")
    return deliver(hub, p)


def _assert_clean(*blobs):
    for b in blobs:
        for term in PII:
            assert term not in b, f"client data {term!r} reached Teamhub"


def test_no_client_pii_reaches_the_teamhub_bus_across_the_whole_loop():
    hub, k, spy = _teamhub_hub()
    _pii_first(hub)
    approve(hub, "confirm")                                                 # draft + judge line are posted
    deliver(hub, msg("r1", "Yes Oscar of Zenith here, oscar@client.example.net 01001234567 confirms"))
    approve(hub, "advance")
    approve(hub, "fix_ready")
    approve(hub, "resolve")
    deliver(hub, msg("r2", "Oscar again +201001234567: still wrong at Zenith", ts=1762999999))   # after resolve
    assert any(c[0] == "open_thread" for c in spy.calls) and len(k.threads) >= 2
    _assert_clean(spy.blob(), json.dumps(k.threads))


def test_a_hold_triage_and_guidance_never_carry_the_sender_or_the_detail():
    class DownSTT(fakes.FakeSTT):
        def transcribe(self, a, **kw):
            return {"status": "blocked", "why": "groq down for 201000000001 oscar@client.example.net"}

    hub, k, spy = _teamhub_hub(stt=DownSTT())
    deliver(hub, "webhook_voice.json")
    hub.tick()
    assert any(c[0] == "post_triage" and "HOLD" in c[1] for c in spy.calls)
    _assert_clean(spy.blob(), json.dumps(k.threads))
    assert not re.search(r"\.\.\.\d{4}", spy.blob())                        # not even the last 4 digits


def test_approvals_stay_on_telegram_and_teamhub_never_sees_them():
    hub, k, spy = _teamhub_hub()
    _pii_first(hub)
    req = pending(hub, "confirm")                                           # the FakeTelegram approval gateway
    assert req and req["nonce"] and req["draft_hash"] and req["topic_id"] == 0
    secrets_ = [req["nonce"], req["draft_hash"], req["draft"]]
    assert all(s not in spy.blob() and s not in json.dumps(k.threads) for s in secrets_)
    approve(hub, "confirm")                                                 # the bound, actor-checked tap works
    assert hub.whatsapp.sent and hub.all_tickets()[0].stage == "awaiting_customer"


def test_teamhub_outage_does_not_block_the_single_ack_or_the_confirm_approval():
    tg = fakes.FakeTelegram()
    k = fakes.FakeTeamhub(fail=RuntimeError("down"))
    bus = team_bus.TeamhubTeamBus(k, fallback=team_bus.TelegramTeamBus(tg))
    hub = build_hub(team_bus=bus, telegram=tg, cfg={"phone_number_id": "PHONE_NUMBER_ID", "team_bus": "teamhub"})
    deliver(hub, "webhook_text.json")
    st = hub.all_tickets()[0]
    assert "7-ACK" in st.history and hub.whatsapp.automatic_count() == 1
    assert pending(hub, "confirm") is not None
    assert any("teamhub" in t.lower() for t in tg.triage)


# ---------------------------------------------------------------- review fixes: NO free client text, ever

LEAKY = [
    "Salary of our employee Mark Fathy (national ID 29801011234567) is wrong, he earns 15000 EGP, "
    "IBAN EG380019000500000000263180002",
    "رقمي ٠١٠٠١٢٣٤٥٦٧ والمرتب بتاع محمود فتحي غلط",
    "call me on 0100 123 4567 or wa.me/201001234567, buyer Nile Pharma owes 2.3M",
]


def _tokens(body: str) -> set[str]:
    return {t for t in re.split(r"[^\w]+", body) if len(t) >= 4}


@pytest.mark.parametrize("body", LEAKY)
def test_no_token_of_the_client_message_reaches_teamhub(body):
    hub, k, spy = _teamhub_hub()
    p = load("webhook_text.json")
    p["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = body
    deliver(hub, p)
    approve(hub, "confirm")
    deliver(hub, msg("r9", body, ts=1762858300))
    blob = spy.blob() + json.dumps(k.threads, ensure_ascii=False)
    assert hub.all_tickets() and "ticket-" in blob
    for tok in _tokens(body):
        assert tok not in blob, f"{tok!r} reached Teamhub"


def test_the_teamhub_brief_has_only_structured_fields():
    hub, k, spy = _teamhub_hub()
    deliver(hub, "webhook_text.json")
    brief = next(c[2] for c in spy.calls if c[0] == "open_thread")
    from twin_agent.hub import analyse
    mods = "|".join(re.escape(n) for n in [m for m, _ in analyse._MODULES] + ["unknown", "Other"])
    types = "Bug|New Request|Customization|Configuration|Inquiries"
    shapes = [r"Ticket #\d+( \(follow-up of #\d+\))? -> @[\w ]+",
              rf"Type ({types}) · track (inquiry|functional|development) · module ({mods})",
              r"Open questions: \d+", r"Odoo: https://\S+"]
    for line in brief.splitlines():
        assert any(re.fullmatch(s, line) for s in shapes), f"free text in the Teamhub brief: {line!r}"


# ---------------------------------------------------------------- review fixes: the webhook URL in logs

def test_the_real_http_call_never_logs_the_webhook_url(caplog):
    import functools
    import httpx
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={}))
    t = team_bus.WebhookTeamhubTransport(
        lambda: SECRET_URL, http=functools.partial(team_bus._httpx_request, _transport=transport))
    with caplog.at_level(logging.DEBUG):
        t.open("t", "c", "e")
    assert "SECRET" not in caplog.text and "teamhub.test" not in caplog.text


def test_a_non_https_webhook_url_fails_closed():
    t = team_bus.WebhookTeamhubTransport(lambda: "http://hooks.teamhub.test/in/SECRET", http=lambda *a, **k: (200, {}))
    with pytest.raises(team_bus.TeamhubError, match="https"):
        t.open("t", "c", "e")


# ---------------------------------------------------------------- review fixes: switching the bus on the same store

def test_switching_from_teamhub_back_to_telegram_keeps_posting_for_teamhub_era_tickets():
    hub, k, spy = _teamhub_hub()
    deliver(hub, "webhook_text.json")
    st = hub.all_tickets()[0]
    assert st.thread_ref == f"ticket-{st.tid}" and st.topic_id == 0
    hub2 = build_hub(store=hub.store, clock=hub.clock, telegram=hub.telegram, odoo=hub.odoo)
    approve(hub2, "confirm")
    deliver(hub2, msg("r1", "yes that is right"))
    after = hub2.ticket(st.tid)
    assert after.topic_id and after.thread_ref == str(after.topic_id)    # a Telegram topic was opened lazily
    assert len(hub2.telegram.topics) == 1
    assert any("Customer reply" in p["text"] for p in hub2.telegram.posts)
    assert [a["topic_id"] for a in hub2.telegram.approvals][-1] in (after.topic_id, 0)


def test_switching_from_telegram_to_teamhub_opens_a_teamhub_thread_for_old_tickets():
    hub = build_hub()
    deliver(hub, "webhook_text.json")
    st = hub.all_tickets()[0]
    k = fakes.FakeTeamhub()
    bus = team_bus.TeamhubTeamBus(k)
    hub2 = build_hub(store=hub.store, clock=hub.clock, telegram=hub.telegram, odoo=hub.odoo, team_bus=bus,
                     cfg={"phone_number_id": "PHONE_NUMBER_ID", "team_bus": "teamhub"})
    deliver(hub2, msg("r1", "yes that is right"))
    assert hub2.ticket(st.tid).thread_ref == f"ticket-{st.tid}"
    assert any(t["event_id"] == f"ticket-{st.tid}" for t in k.threads)


# ---------------------------------------------------------------- review fixes: triage goes somewhere in Teamhub mode

def test_in_teamhub_mode_the_full_triage_text_goes_to_telegram_and_a_safe_line_to_teamhub():
    hub, k, spy = _teamhub_hub()
    hub._triage("fallback FAILED for message wamid.SECRET1 ticket 9", safe="Hub notice: a fallback failed")
    hub._triage("another alert wamid.SECRET2")                                  # no safe line given
    assert [t for t in hub.telegram.triage if "wamid.SECRET1" in t] and [t for t in hub.telegram.triage if "SECRET2" in t]
    assert "SECRET" not in spy.blob() and len(k.threads) == 2
    assert len({t["event_id"] for t in k.threads}) == 2
    assert "Telegram triage" in k.threads[1]["content"]


def test_a_free_text_module_from_the_client_context_never_reaches_teamhub():
    leak = "Max Executive payroll NID 29801011234567"
    k = fakes.FakeTeamhub()
    bus = team_bus.TeamhubTeamBus(k)
    hub = build_hub(team_bus=bus, cfg={"phone_number_id": "PHONE_NUMBER_ID", "team_bus": "teamhub"},
                    knowledge_wiki=fakes.FakeWikiKnowledge(context={3001: {"modules": [leak]}}))
    p = load("webhook_text.json")
    p["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = "Please help, it does not work since yesterday"
    deliver(hub, p)
    blob = json.dumps(k.threads, ensure_ascii=False)
    assert k.threads and "29801011234567" not in blob and "Max" not in blob and "payroll" not in blob
    assert "module Other" in k.threads[0]["content"]
