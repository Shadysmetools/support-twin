"""telegram_in: allow-list drop (red-team #18), Principal detection, media only for allowed chats, sweep min_id."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from twin_agent.channels.telegram_in import TelethonIn, chat_entry, normalize_tg, raw_from_message, topic_of
from twin_agent.channels.tg_client import AdapterRefused
from twin_agent.models import Channel, Company
from twin_agent.testing.fakes import PRINCIPAL_ID, GROUP_ID, TOPIC_LOGISTICS, TOPIC_STORES


def raw(**kw):
    base = {"chat_id": GROUP_ID, "chat_title": "Test Group", "topic_id": TOPIC_LOGISTICS, "msg_id": 5,
            "sender_id": 2001, "sender_name": "Staff A", "sender_is_bot": False, "out": False,
            "date": datetime(2026, 11, 1, 12, 40, tzinfo=timezone.utc), "text": "WAVE/00001", "media": [],
            "reply_to": None, "edited": False}
    base.update(kw)
    return base


def live(cfg):
    cfg = dict(cfg, run_mode="live")
    return cfg


def test_allowed_topic_becomes_event(cfg):
    ev = normalize_tg(raw(), cfg, PRINCIPAL_ID)
    assert ev.event_id == f"tg:{GROUP_ID}:5" and ev.channel == Channel.telegram_group
    assert ev.topic == "Logistics" and ev.sender.dept == "Logistics" and ev.company_hint == Company.EG
    assert ev.ts.utcoffset().total_seconds() == 2 * 3600 and ev.ts.hour == 14     # Cairo
    assert ev.untrusted and not ev.sender.is_principal


@pytest.mark.parametrize("kw", [{"chat_id": -100999}, {"chat_id": 0}, {"chat_id": 4242},
                                {"chat_id": -100600},             # Saudi Setup: read false
                                {"topic_id": 99}, {"is_service": True}])
def test_not_allow_listed_is_dropped(cfg, kw):
    assert normalize_tg(raw(**kw), cfg, PRINCIPAL_ID) is None


def test_unfilled_chat_id_zero_matches_nothing(cfg):
    cfg["chats"][0]["chat_id"] = 0
    assert normalize_tg(raw(chat_id=0), cfg, PRINCIPAL_ID) is None


def test_principal_and_anonymous_admin_are_principal(cfg):
    assert normalize_tg(raw(sender_id=PRINCIPAL_ID), cfg, PRINCIPAL_ID).sender.is_principal
    anon = normalize_tg(raw(sender_id=GROUP_ID, out=True), cfg, PRINCIPAL_ID)   # posted as the group
    assert anon.sender.is_principal and not anon.untrusted
    assert not normalize_tg(raw(sender_id=0), cfg, 0).sender.is_principal       # id 0 never matches


def test_allowed_dm(cfg):
    cfg["chats"].append({"name": "DM Staff B", "chat_id": 2002, "kind": "dm", "read": True, "company": "EG"})
    ev = normalize_tg(raw(chat_id=2002, topic_id=None, sender_id=2002), cfg, PRINCIPAL_ID)
    assert ev.channel == Channel.telegram_dm and ev.chat == "DM Staff B"
    assert normalize_tg(raw(chat_id=2003, topic_id=None), cfg, PRINCIPAL_ID) is None


def test_chat_entry_topics(cfg):
    assert chat_entry(cfg, GROUP_ID, TOPIC_STORES)[1]["name"] == "Stores"
    assert chat_entry(cfg, GROUP_ID, 123) is None


def test_topic_and_reply_from_telethon_shape():
    in_topic = NS(reply_to=NS(forum_topic=True, reply_to_top_id=None, reply_to_msg_id=7))
    reply_in_topic = NS(reply_to=NS(forum_topic=True, reply_to_top_id=7, reply_to_msg_id=42))
    assert topic_of(in_topic) == 7 and topic_of(reply_in_topic) == 7 and topic_of(NS(reply_to=None)) is None
    r = raw_from_message(_msg(42, reply=reply_in_topic.reply_to))
    assert r["topic_id"] == 7 and r["reply_to"] == 42


# ------------------------------------------------------------ real adapter with a duck-typed Telethon client

def _msg(mid, *, chat_id=GROUP_ID, photo=False, reply=None, text="WAVE/00001", sender_id=2001, out=False):
    return NS(id=mid, chat_id=chat_id, chat=NS(title="Test Group"), is_private=False,
              reply_to=reply if reply is not None else NS(forum_topic=True, reply_to_top_id=None,
                                                          reply_to_msg_id=TOPIC_LOGISTICS),
              sender_id=sender_id, sender=NS(first_name="Staff", last_name="A", bot=False), out=out,
              date=datetime(2026, 11, 1, 12, 40, tzinfo=timezone.utc), message=text,
              photo=object() if photo else None, action=None)


class FakeClient:
    def __init__(self, msgs=()):
        self.msgs, self.downloads, self.get_calls, self.handlers = list(msgs), [], [], []

    def is_connected(self):
        return True

    async def get_dialogs(self):
        self.get_calls.append(("dialogs", {}))

    async def download_media(self, msg, file):
        self.downloads.append(file)
        return file

    async def get_messages(self, chat_id, **kw):
        self.get_calls.append((chat_id, kw))
        return [m for m in self.msgs if m.id > kw["min_id"]][: kw["limit"]]

    def add_event_handler(self, h, ev):
        self.handlers.append(h)

    def remove_event_handler(self, h):
        self.handlers.remove(h)


def test_real_adapter_refuses_without_live_or_vault(cfg):
    with pytest.raises(AdapterRefused):
        TelethonIn(cfg, FakeClient(), has_secret=lambda n: True)                 # dry_run
    with pytest.raises(AdapterRefused):
        TelethonIn(live(cfg), FakeClient(), has_secret=lambda n: False)          # vault empty


def test_handle_downloads_media_only_for_allowed(cfg, tmp_path):
    client, got = FakeClient(), []
    a = TelethonIn(live(cfg), client, has_secret=lambda n: True, media_dir=tmp_path / "media")

    async def on_event(ev):
        got.append(ev)

    async def go():
        a._on_event = on_event
        await a.handle(_msg(9, chat_id=-100999, photo=True))       # personal chat: dropped, no download
        await a.handle(_msg(10, photo=True))
    asyncio.run(go())
    assert len(got) == 1 and got[0].media == [f"photo_{abs(GROUP_ID)}_10.jpg"]
    assert len(client.downloads) == 1 and client.downloads[0].endswith("photo_100500_10.jpg")


def test_sweep_reads_only_new_ids(cfg, tmp_path):
    client = FakeClient([_msg(i) for i in (3, 4, 5)])
    a = TelethonIn(live(cfg), client, has_secret=lambda n: True, media_dir=tmp_path)
    evs = asyncio.run(a.fetch_since(GROUP_ID, TOPIC_LOGISTICS, min_id=3, limit=50))
    assert [e.msg_id for e in evs] == [4, 5] and all(e.found_by_sweep for e in evs)
    assert client.get_calls == [(GROUP_ID, {"min_id": 3, "limit": 50, "reverse": True, "reply_to": TOPIC_LOGISTICS})]
    assert asyncio.run(a.fetch_since(-100999, None, 0)) == [] and len(client.get_calls) == 1   # no call at all


def test_handler_failure_logs_no_text(cfg, tmp_path, caplog):
    a = TelethonIn(live(cfg), FakeClient(), has_secret=lambda n: True, media_dir=tmp_path)

    async def boom(ev):
        raise ValueError("secret text WAVE/00001")
    a._on_event = boom
    asyncio.run(a._safe_handle(_msg(11), edited=False))
    assert "WAVE" not in caplog.text and "ValueError" in caplog.text
