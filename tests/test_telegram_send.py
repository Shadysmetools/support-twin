"""telegram_send: from_id post-check vs anonymous admin, threading, error mapping, own safety refusals."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from twin_agent.channels.telegram_send import (TelethonSend, TgFloodWait, TgNetworkDrop, TgPeerFlood,
                                                TgSendRefused, author_id, from_id_ok, map_error)
from twin_agent.testing.fakes import PRINCIPAL_ID, GROUP_ID, TOPIC_LOGISTICS


class FloodWaitError(Exception):
    seconds = 42


class PeerFloodError(Exception):
    pass


class FakeClient:
    def __init__(self, anonymous=False, raise_exc=None):
        self.anonymous, self.raise_exc, self.sent, self.deleted = anonymous, raise_exc, [], []

    def is_connected(self):
        return True

    async def get_dialogs(self):
        self.dialogs_loaded = True

    async def get_me(self):
        return NS(id=PRINCIPAL_ID)

    async def send_message(self, chat_id, text, reply_to=None, link_preview=True):
        if self.raise_exc:
            raise self.raise_exc
        self.sent.append((chat_id, text, reply_to, link_preview))
        from_id = None if self.anonymous else NS(user_id=PRINCIPAL_ID)
        return NS(id=777, chat_id=chat_id, from_id=from_id)

    async def delete_messages(self, chat_id, ids, revoke=True):
        self.deleted.append((chat_id, ids, revoke))

    async def get_messages(self, chat_id, **kw):
        m = NS(id=kw["min_id"] + 1, chat_id=chat_id, chat=NS(title="Test Group"), is_private=False,
               reply_to=NS(forum_topic=True, reply_to_top_id=None, reply_to_msg_id=TOPIC_LOGISTICS),
               sender_id=2001, sender=NS(first_name="Staff", last_name="A", bot=False), out=False,
               date=datetime(2026, 11, 1, 12, 41, tzinfo=timezone.utc), message="still", photo=None, action=None)
        return [m]


def sender(cfg, tmp_path, client, **over):
    c = dict(cfg, run_mode="live", send_enabled=True, **over)
    return TelethonSend(c, client, has_secret=lambda n: True, stop_file=lambda: tmp_path / "STOP")


def test_send_threads_and_passes_post_check(cfg, tmp_path):
    client = FakeClient()
    s = sender(cfg, tmp_path, client)
    r = asyncio.run(s.send_message(GROUP_ID, "Checking it, 10 min.", reply_to=5, topic_id=TOPIC_LOGISTICS))
    assert r == {"msg_id": 777, "from_id": PRINCIPAL_ID, "from_id_ok": True} and client.dialogs_loaded
    assert client.sent == [(GROUP_ID, "Checking it, 10 min.", 5, False)]
    asyncio.run(s.send_message(GROUP_ID, "x", reply_to=None, topic_id=TOPIC_LOGISTICS))
    assert client.sent[-1][2] == TOPIC_LOGISTICS                 # no reply target: post into the topic


def test_anonymous_admin_fails_post_check(cfg, tmp_path):
    r = asyncio.run(sender(cfg, tmp_path, FakeClient(anonymous=True)).send_message(
        GROUP_ID, "hi", reply_to=5, topic_id=TOPIC_LOGISTICS))
    assert r["from_id"] == GROUP_ID and r["from_id_ok"] is False
    assert author_id(NS(from_id=NS(channel_id=500), chat_id=GROUP_ID)) == -500
    assert not from_id_ok({"from_id": PRINCIPAL_ID}, 0)


@pytest.mark.parametrize("over,stop,chat,why", [
    ({"run_mode": "dry_run"}, False, GROUP_ID, "live"),
    ({"send_enabled": False}, False, GROUP_ID, "send_enabled"),
    ({}, True, GROUP_ID, "kill switch"),
    ({}, False, -100999, "allow-list"),
])
def test_adapter_refuses_on_its_own(cfg, tmp_path, over, stop, chat, why):
    client = FakeClient()
    s = sender(cfg, tmp_path, client)
    s.cfg.update(over)
    if stop:
        (tmp_path / "STOP").write_text("x")
    with pytest.raises(TgSendRefused, match=why):
        asyncio.run(s.send_message(chat, "hi", reply_to=None, topic_id=TOPIC_LOGISTICS))
    assert client.sent == []


def test_error_mapping(cfg, tmp_path):
    assert isinstance(map_error(FloodWaitError()), TgFloodWait) and map_error(FloodWaitError()).seconds == 42
    assert isinstance(map_error(PeerFloodError()), TgPeerFlood)
    assert isinstance(map_error(ConnectionResetError()), TgNetworkDrop)
    assert isinstance(map_error(ValueError("Could not find the input entity")), TgSendRefused)   # nothing went out
    s = sender(cfg, tmp_path, FakeClient(raise_exc=PeerFloodError("raw telegram text")))
    with pytest.raises(TgPeerFlood) as ei:
        asyncio.run(s.send_message(GROUP_ID, "hi", reply_to=None, topic_id=TOPIC_LOGISTICS))
    assert "raw telegram text" not in str(ei.value) and ei.value.__cause__ is None


def test_delete_and_read_thread(cfg, tmp_path):
    client = FakeClient()
    s = sender(cfg, tmp_path, client)
    asyncio.run(s.delete_message(GROUP_ID, 777))
    assert client.deleted == [(GROUP_ID, [777], True)]
    evs = asyncio.run(s.read_thread(GROUP_ID, TOPIC_LOGISTICS, after_msg_id=10))
    assert [e.msg_id for e in evs] == [11] and evs[0].text == "still"
    assert asyncio.run(s.read_thread(-100999, None, 0)) == []


def test_real_adapter_refuses_without_vault(cfg, tmp_path):
    from twin_agent.channels.tg_client import AdapterRefused
    with pytest.raises(AdapterRefused):
        TelethonSend(dict(cfg, run_mode="live"), FakeClient(), has_secret=lambda n: False)


def test_our_errors_match_sender_classification():
    """sender_errors.classify_error (SEND) must read our exceptions the right way."""
    from twin_agent.sender_errors import classify_error
    from twin_agent.channels.telegram_send import TgSendRefused
    assert classify_error(TgFloodWait(42)) == ("flood_wait", 42)
    assert classify_error(TgPeerFlood("PeerFloodError"))[0] == "peer_flood"
    assert classify_error(TgSendRefused("send_enabled is false"))[0] == "refused"
    assert classify_error(TgNetworkDrop("ConnectionResetError"))[0] == "unknown"      # -> maybe_sent
