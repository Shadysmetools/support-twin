"""Excellent Odoo tickets: title + description shape, the ticket-quality golden eval, the TicketWriter seam, and that the
richer text never reaches the team bus (Teamhub). No network, no model: FAKE writer and FAKE judge."""
from __future__ import annotations

import html

from twin_agent.hub import analyse as analyse_mod
from twin_agent.hub import fakes, ticket_text
from twin_agent.hub import judge as judge_mod
from twin_agent.hub.adapters_real import RealTicketWriter
from twin_agent.hub.evalharness import TicketQualityEval, load_ticket_golden
from twin_agent.hub.interfaces import TicketWriter
from twin_agent.hub.model import Analysis, Inbound
from twin_agent.testing.fakes import FakeKeyring
from twin_agent.hub.team_bus import TeamhubTeamBus, TelegramTeamBus
from hub_helpers import build_hub, deliver, msg

CLIENT = "Nile Trading Co"
CTX = {3001: {"name": CLIENT, "modules": [], "owner_uids": []}}
AR = "عندي مشكلة في المخزون، الكمية ظاهرة غلط بعد ما عملت تسليم"


def _hub(**kw):
    return build_hub(knowledge_odoo=fakes.FakeOdooKnowledge(context=CTX), **kw)


def _ticket(hub, tid=20001):
    return hub.odoo.t.data["helpdesk.ticket"][tid]


def _ar_payload():
    return msg("wamid.AR1", AR)


# ---------------------------------------------------------------- the golden eval

def test_golden_has_enough_cases_and_all_pass_the_rubric_with_a_fake_judge():
    res = TicketQualityEval().run()
    assert res["n"] >= 12
    bad = {r["id"]: [k for k, v in r["checks"].items() if not v] for r in res["rows"] if not all(r["checks"].values())}
    assert bad == {} and res["gate_pass"] is True
    assert res["judged_by"].startswith("fake")            # never claims a model judged a fake-judged run


def test_golden_covers_the_required_shapes():
    items = load_ticket_golden()
    assert {"text", "voice", "media"} <= {i["kind"] for i in items}
    assert {"Bug", "New Request", "Configuration", "Inquiries", "Customization"} <= {i["gt_type"] for i in items}
    assert any(i["gt_module"] == "unknown" for i in items) and any(i.get("follow_up") for i in items)
    assert any(i.get("lang") == "ar" for i in items) and any(i.get("lang") == "mixed" for i in items)
    assert any(i.get("attachments") for i in items)


def test_a_degraded_ticket_fails_the_gate():
    ev = TicketQualityEval()
    ev._ticket = lambda item: (analyse_mod.classify(item["input"]), "x", "<p>nothing</p>")
    assert ev.run()["gate_pass"] is False


def test_real_judge_is_blocked_and_the_run_says_so():
    res = TicketQualityEval(judge=judge_mod.VertexJudge(model="gemini-3.6-flash")).run()
    assert all(r["judge"]["status"] == "blocked" for r in res["rows"])
    assert res["judged_by"].startswith("none") and "model:" not in res["judged_by"]


# ---------------------------------------------------------------- title + description through the real hub

def test_english_text_makes_a_client_module_problem_title_and_full_description():
    hub = _hub()
    deliver(hub, "webhook_text.json")
    t = _ticket(hub)
    client, module, problem = t["name"].split(" · ")
    assert client == CLIENT and module == "Inventory" and problem and not t["name"].startswith("[WA]")
    d = t["description"]
    pos = [d.index(f"<b>{lbl}</b>") for lbl in ticket_text.SECTION_ORDER]
    assert pos == sorted(pos)
    assert "<details>" in d and "Original message (English)" in d
    assert "The stock quantity in Inventory is wrong" in d
    assert "Not given yet" in d and "None received" in d
    assert "201000000001" not in d


def test_arabic_without_a_writer_is_honest_and_keeps_the_original_in_a_rtl_fold_out():
    hub = _hub()
    deliver(hub, _ar_payload())
    t = _ticket(hub)
    d = t["description"]
    assert t["name"].startswith(f"{CLIENT} · Inventory · ") and not any("؀" <= c <= "ۿ" for c in t["name"])
    assert ticket_text.NOT_TRANSLATED in d
    assert f'<div dir="rtl">{html.escape(AR)}</div>' in d and "Original message (Arabic)" in d
    assert hub.ticket(20001).analysis.summary.startswith("عندي")      # the client-facing draft input stays Arabic


def test_arabic_with_the_fake_writer_gets_english_text():
    w = fakes.FakeTicketWriter({AR: "I have a problem in inventory: the quantity is wrong after a delivery"})
    hub = _hub(ticket_writer=w)
    deliver(hub, _ar_payload())
    t = _ticket(hub)
    assert "the quantity is wrong after a delivery" in t["description"]
    assert t["name"] == f"{CLIENT} · Inventory · I have a problem in inventory:"
    assert w.calls == ["ar"]


def test_blocked_real_writer_never_stops_intake_and_never_uses_the_fake(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    hub = _hub(ticket_writer=RealTicketWriter(backend=FakeKeyring()))
    deliver(hub, _ar_payload())
    d = _ticket(hub)["description"]
    assert "Not translated: no Claude key" in d and "fake translation" not in d


def test_a_crashing_writer_never_stops_intake():
    class Boom:
        def write(self, *a, **k):
            raise RuntimeError("boom")
    hub = _hub(ticket_writer=Boom())
    deliver(hub, _ar_payload())
    assert "Not translated: the translation service failed (RuntimeError)" in _ticket(hub)["description"]


def test_real_writer_blocks_with_a_plain_reason_and_both_satisfy_the_interface(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = RealTicketWriter(backend=FakeKeyring()).write("x", lang="en", analysis=None, client="c")
    assert r["status"] == "blocked" and "Claude key" in r["why"]
    assert isinstance(fakes.FakeTicketWriter(), TicketWriter) and isinstance(RealTicketWriter(), TicketWriter)


def test_client_text_is_escaped_and_follow_up_is_linked():
    a = Analysis(nature="issue", ticket_type="Bug", track="functional", module="Sales")
    ib = Inbound(message_id="m", wa_id="1", name="", kind="text", text="<img src=x onerror=alert(1)> broken", lang="en")
    _, d = ticket_text.compose(a, ib, client="<b>X</b>", partner_id=1, previous=77)
    assert "<img" not in d and "&lt;img" in d and "Follows up #77" in d


def test_title_fallbacks_when_client_and_module_are_unknown():
    a = Analysis(nature="issue", ticket_type="Bug", track="functional", module="unknown")
    ib = Inbound(message_id="m", wa_id="1", name="", kind="text", text=AR, lang="ar")
    title, _ = ticket_text.compose(a, ib, client="partner:3001", partner_id=3001)
    assert title == "Client #3001 · Module not identified · Issue reported"


# ---------------------------------------------------------------- the richer text never reaches the team bus

def test_teamhub_never_gets_the_client_name_title_or_original_text():
    k = fakes.FakeTeamhub()
    bus = TeamhubTeamBus(k, fallback=TelegramTeamBus(fakes.FakeTelegram()))
    w = fakes.FakeTicketWriter({AR: "I have a problem in inventory"})
    hub = _hub(team_bus=bus, cfg={"phone_number_id": "PHONE_NUMBER_ID", "team_bus": "teamhub"}, ticket_writer=w)
    deliver(hub, _ar_payload())
    title = _ticket(hub)["name"]
    blob = repr(k.threads)
    assert k.threads and CLIENT not in blob and title not in blob and "problem in inventory" not in blob
    assert "المخزون" not in blob


def test_minor_an_arabic_problem_phrase_from_the_writer_never_enters_the_title():
    class ArWriter:
        def write(self, text, **k):
            return {"status": "ok", "english": "Inventory quantity is wrong", "problem": "مشكلة في المخزون", "expect": "",
                    "steps": ""}
    hub = _hub(ticket_writer=ArWriter())
    deliver(hub, _ar_payload())
    title = _ticket(hub)["name"]
    assert not any("؀" <= c <= "ۿ" for c in title) and title.endswith("Issue reported")
