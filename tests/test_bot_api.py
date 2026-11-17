"""bot_api (real Bot API transport) over httpx.MockTransport: never a real network call, token never leaks."""
import asyncio
import json

import httpx
import pytest

from twin_agent.channels.bot_api import BotApiError, TelegramBotApi
from twin_agent.channels.tg_client import AdapterRefused
from twin_agent.models import Button, Card, CardKind
from twin_agent.testing.fakes import PRINCIPAL_ID, BACKUP_ID

TOKEN = "123456:FAKE-test-token-not-real"


def make(cfg, handler):
    calls = []

    def record(request: httpx.Request):
        body = json.loads(request.content) if request.headers.get("content-type", "").startswith("application/json") \
            else {}
        calls.append((request.url.path.rsplit("/", 1)[-1], body))
        return handler(calls[-1][0], body)

    http = httpx.AsyncClient(transport=httpx.MockTransport(record))
    api = TelegramBotApi(dict(cfg, run_mode="live"), has_secret=lambda n: True, get_secret=lambda n: TOKEN, http=http)
    return api, calls


def ok(result):
    return httpx.Response(200, json={"ok": True, "result": result})


def test_refuses_unless_live_with_token(cfg):
    with pytest.raises(AdapterRefused):
        TelegramBotApi(cfg, has_secret=lambda n: True, get_secret=lambda n: TOKEN)
    with pytest.raises(AdapterRefused):
        TelegramBotApi(dict(cfg, run_mode="live"), has_secret=lambda n: False, get_secret=lambda n: TOKEN)


def test_send_card_with_buttons_and_target(cfg):
    api, calls = make(cfg, lambda m, b: ok({"message_id": 55}))
    card = Card(kind=CardKind.new_request, request_id="REQ-0142", text="Asked: WAVE/00001", silent=True,
                buttons=[[Button("Approve reply", "ap")]])
    mid = asyncio.run(api.send_card(card, lambda b: f"REQ-0142|{b.action}|nonce12345"))
    method, body = calls[0]
    assert mid == 55 and method == "sendMessage" and body["chat_id"] == PRINCIPAL_ID
    assert body["disable_notification"] is True and body["text"].startswith("REQ-0142")
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "REQ-0142|ap|nonce12345"
    asyncio.run(api.send_card(Card(kind=CardKind.escalation, text="x", to="backup"), lambda b: ""))
    assert calls[-1][1]["chat_id"] == BACKUP_ID


def test_force_reply_goes_to_principal(cfg):
    api, calls = make(cfg, lambda m, b: ok({"message_id": 9}))
    assert asyncio.run(api.ask_force_reply("type it", "REQ-0142")) == 9
    assert calls[0][1]["chat_id"] == PRINCIPAL_ID and calls[0][1]["reply_markup"]["force_reply"] is True


def test_errors_never_contain_the_token(cfg):
    api, _ = make(cfg, lambda m, b: httpx.Response(400, json={"ok": False, "description": f"bad {TOKEN}"}))
    with pytest.raises(BotApiError) as ei:
        asyncio.run(api.answer_callback("cb", "OK"))
    assert TOKEN not in str(ei.value) and "<token>" in str(ei.value)
    assert TOKEN not in repr(api)

    def boom(m, b):
        raise httpx.ConnectError(f"cannot reach https://api.telegram.org/bot{TOKEN}/x")
    api2, _ = make(cfg, boom)
    with pytest.raises(BotApiError) as ei2:
        asyncio.run(api2.call("getMe"))
    assert TOKEN not in str(ei2.value) and ei2.value.__cause__ is None


def test_poll_dispatches_updates(cfg):
    updates = [
        {"update_id": 1, "callback_query": {"id": "c1", "from": {"id": PRINCIPAL_ID}, "data": "REQ-0142|ap|n1n1n1"}},
        {"update_id": 2, "message": {"chat": {"type": "private"}, "from": {"id": PRINCIPAL_ID}, "text": "/away 3h"}},
        {"update_id": 3, "message": {"chat": {"type": "private"}, "from": {"id": PRINCIPAL_ID}, "text": "ask for batch",
                                     "reply_to_message": {"message_id": 55}}},
        {"update_id": 4, "message": {"chat": {"type": "private"}, "from": {"id": PRINCIPAL_ID}, "text": "fwd text",
                                     "forward_date": 1}},
        {"update_id": 5, "message": {"chat": {"type": "group"}, "from": {"id": PRINCIPAL_ID}, "text": "/stop"}},
    ]
    api, calls = make(cfg, lambda m, b: ok(updates if b.get("offset", 0) == 0 else []))
    got = []

    async def cb(u):
        got.append(("cb", u))

    async def cmd(u):
        got.append(("cmd", u))

    async def rep(u):
        got.append(("rep", u))
    api._handlers = (cb, cmd, rep)
    assert asyncio.run(api.poll_once()) == 5
    assert got == [("cb", {"user_id": PRINCIPAL_ID, "data": "REQ-0142|ap|n1n1n1", "callback_id": "c1"}),
                   ("cmd", {"user_id": PRINCIPAL_ID, "command": "away", "args": "3h"}),
                   ("rep", {"user_id": PRINCIPAL_ID, "text": "ask for batch", "to_bot_msg_id": 55}),
                   ("cmd", {"user_id": PRINCIPAL_ID, "command": "new", "args": "fwd text"})]   # group msg ignored
    assert api._offset == 6


def test_one_bad_update_does_not_stop_the_bot(cfg):
    updates = [{"update_id": 1, "message": {"chat": {"type": "private"}, "from": {"id": PRINCIPAL_ID}, "text": "/boom"}},
               {"update_id": 2, "message": {"chat": {"type": "private"}, "from": {"id": PRINCIPAL_ID}, "text": "/stop"}}]
    api, _ = make(cfg, lambda m, b: ok(updates))
    got = []

    async def cmd(u):
        if u["command"] == "boom":
            raise KeyError("x")
        got.append(u["command"])

    async def none(u):
        pass
    api._handlers = (none, cmd, none)
    asyncio.run(api.poll_once())
    assert got == ["stop"] and api._offset == 3


def _raw(cfg):
    calls = []

    def record(request: httpx.Request):
        calls.append((request.url.path.rsplit("/", 1)[-1], request.content))
        return ok({"message_id": 70 + len(calls)})
    http = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return TelegramBotApi(dict(cfg, run_mode="live"), has_secret=lambda n: True, get_secret=lambda n: TOKEN,
                          http=http), calls


def test_card_with_screenshot_is_one_message_photo_caption_and_buttons(cfg, tmp_path):
    """Principal 2026-11-09: the screenshot, the card text and the buttons arrive as ONE message (sendPhoto + caption +
    reply_markup). A card too long for a caption falls back to photo first, then the card."""
    shot = tmp_path / "shot.jpg"
    shot.write_bytes(b"\xff\xd8fakejpeg")
    api, calls = _raw(cfg)
    card = Card(kind=CardKind.new_request, request_id="REQ-0052", text="REQ-0052 | short card",
                buttons=[[Button("Approve", "ap")]], attachments=[str(shot)])
    mid = asyncio.run(api.send_card(card, lambda b: f"REQ-0052|{b.action}|n0nce"))
    assert len(calls) == 1 and calls[0][0] == "sendPhoto" and mid == 71
    assert b"REQ-0052 | short card" in calls[0][1] and b"REQ-0052|ap|n0nce" in calls[0][1]
    api2, calls2 = _raw(cfg)
    long = Card(kind=CardKind.new_request, request_id="REQ-0052", text="REQ-0052 " + "x" * 1500,
                buttons=[[Button("Approve", "ap")]], attachments=[str(shot)])
    asyncio.run(api2.send_card(long, lambda b: "REQ-0052|ap|n"))
    assert [c[0] for c in calls2] == ["sendPhoto", "sendMessage"]
