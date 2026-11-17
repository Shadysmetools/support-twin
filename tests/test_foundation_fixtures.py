"""The shared fixtures and fakes behave as CONTRACT section 12 says."""
import asyncio
import json
import socket
import urllib.request

import pytest

from twin_agent import paths, schemas
from twin_agent.models import Button, Card, CardKind, Channel, OutboundMessage, OutKind
from twin_agent.testing import fakes


def test_network_is_blocked():
    with pytest.raises(Exception) as e:
        urllib.request.urlopen("https://api.telegram.org", timeout=2)
    assert "blocked" in str(e.value).lower()
    with pytest.raises(Exception):
        socket.create_connection(("192.0.2.1", 443), timeout=2)   # reserved documentation address


def test_asyncio_ip_literal_is_blocked():
    """Telethon connects to IP literals through asyncio (ProactorEventLoop on Windows): must be blocked too."""
    async def go():
        await asyncio.wait_for(asyncio.open_connection("192.0.2.1", 443), 2)
    with pytest.raises(Exception) as e:
        asyncio.run(go())
    assert "blocked" in str(e.value).lower()


def test_loopback_is_allowed():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    c = socket.create_connection(srv.getsockname(), timeout=2)
    c.close()
    srv.close()


def test_tmp_home_is_isolated(tmp_home, cfg):
    assert paths.home() == tmp_home
    assert paths.state_db().parent.is_dir() and paths.work_dir().is_dir()
    assert str(paths.vault_file_dir()).startswith(str(tmp_home))
    assert cfg["people"]["principal_tg_user_id"] == fakes.PRINCIPAL_ID
    assert cfg["send_enabled"] is False and cfg["run_mode"] == "dry_run"
    assert cfg["dial"]["L11_internal_diagnosis"] == "auto"
    assert all(v in ("observe", "off") for k, v in cfg["dial"].items() if k != "L11_internal_diagnosis")


def test_clock(clock):
    t0 = clock.now()
    assert t0.weekday() == 6 and t0.hour == 14        # Sunday, working hours
    assert (clock.advance(minutes=61) - t0).total_seconds() == 61 * 60


def test_fake_telegram_records_and_can_fail(fake_tg, make_event):
    async def go():
        got = []

        async def on_event(ev):
            got.append(ev)

        await fake_tg.start(on_event)
        await fake_tg.inject(make_event("WAVE/00001", msg_id=5))
        r = await fake_tg.send_message(fakes.GROUP_ID, "Try again now.", reply_to=5, topic_id=7)
        assert r["from_id"] == fakes.PRINCIPAL_ID
        assert [e.msg_id for e in await fake_tg.read_thread(fakes.GROUP_ID, 7, 4)] == [5]
        fake_tg.raise_on_send = "PeerFlood"
        with pytest.raises(fakes.FakeSendError):
            await fake_tg.send_message(fakes.GROUP_ID, "x", reply_to=None, topic_id=None)
        return got

    assert len(asyncio.run(go())) == 1
    assert len(fake_tg.sent) == 1


def test_fake_bot_cards_and_presses(fake_bot):
    async def go():
        presses = []

        async def cb(p):
            presses.append(p)

        async def noop(p):
            pass

        await fake_bot.start(cb, noop, noop)
        card = Card(kind=CardKind.new_request, text="REQ-0001", request_id="REQ-0001",
                    buttons=[[Button("Approve reply", "ap")]])
        mid = await fake_bot.send_card(card, lambda b: f"REQ-0001|{b.action}|nonce123456")
        await fake_bot.press("REQ-0001|ap|nonce123456", user_id=fakes.BACKUP_ID)
        return mid, presses

    mid, presses = asyncio.run(go())
    assert mid == 1 and fake_bot.cards[0][2] == [["REQ-0001|ap|nonce123456"]]
    assert presses[0]["user_id"] == fakes.BACKUP_ID


def test_fake_gmail(fake_gmail, make_event):
    fake_gmail.add(make_event("hello", channel=Channel.ticket_mail))
    evs, hid = fake_gmail.list_new("ticketmail", None)
    assert len(evs) == 1 and fake_gmail.list_new("ticketmail", hid)[0] == []
    msg = OutboundMessage("REQ-0001", "ticket-a", OutKind.ticket_email, Channel.email, "body", to=["help@example.com"])
    d = fake_gmail.create_draft(msg)
    assert fake_gmail.sent == [] and fake_gmail.send_draft(d) == "m1"


def test_fake_claude_returns_valid_packet(fake_claude, tmp_path):
    r = fake_claude.run("work on REQ-0007 please", kind="worker", cwd=tmp_path)
    body = r.text.split("```json\n", 1)[1].rsplit("```", 1)[0]
    assert schemas.validate_packet(json.loads(body), "REQ-0007") == []
    j = fake_claude.run("judge", kind="judge", cwd=tmp_path)
    assert '"pass": true' in j.text
    fake_claude.fail_kinds.add("worker")
    assert not fake_claude.run("x", kind="worker", cwd=tmp_path).ok
    assert [c["kind"] for c in fake_claude.calls] == ["worker", "judge", "worker"]


def test_fake_keyring(fake_keyring):
    fake_keyring.set_password("twinagent", "tg-api", "v")
    assert fake_keyring.get_password("twinagent", "tg-api") == "v"
    fake_keyring.delete_password("twinagent", "tg-api")
    assert fake_keyring.get_password("twinagent", "tg-api") is None


def test_export_fixture_is_synthetic(export_path):
    data = json.loads(export_path.read_text(encoding="utf-8"))
    assert data["messages"] and {m["from_id"] for m in data["messages"]} <= {"user1001", "user2001", "user2002", "user3001"}
