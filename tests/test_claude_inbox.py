"""Cards to Principal's Claude chat instead of Telegram (Principal, 2026-11-09)."""
import asyncio
import json

from twin_agent.channels.claude_inbox import ClaudeInbox, find_callback, main
from twin_agent.models import Button, Card, CardKind

CFG = {"people": {"principal_tg_user_id": 1001}}


def _lines(p):
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_card_goes_to_the_inbox_file_with_photo_and_callbacks(tmp_path):
    shot = tmp_path / "shot.jpg"
    shot.write_bytes(b"x")
    bot = ClaudeInbox(CFG, data_dir=tmp_path)
    card = Card(kind=CardKind.run_this, request_id="REQ-0055", text="REQ-0055 | Leo\nREPLY APPROVE WILL POST:\nهلا",
                buttons=[[Button("Approve", "ap"), Button("Edit", "ed")]], attachments=[str(shot)])
    msg_id = asyncio.run(bot.send_card(card, lambda b: f"REQ-0055|{b.action}|n{b.action}"))
    rec = _lines(tmp_path / "claude_inbox.jsonl")[-1]
    assert rec["id"] == msg_id and rec["type"] == "card" and rec["request_id"] == "REQ-0055"
    assert "هلا" in rec["text"] and rec["photo"] == str(shot)
    assert [b["callback"] for b in rec["buttons"]] == ["REQ-0055|ap|nap", "REQ-0055|ed|ned"]


def test_cli_approve_becomes_a_tap_as_principal_and_runs_once(tmp_path):
    bot = ClaudeInbox(CFG, data_dir=tmp_path)
    card = Card(kind=CardKind.run_this, request_id="REQ-0055", text="t", buttons=[[Button("Approve", "ap")]])
    asyncio.run(bot.send_card(card, lambda b: "REQ-0055|ap|abc"))
    assert main(["approve", "REQ-0055", "--data-dir", str(tmp_path)]) == 0
    taps = []

    async def on_cb(u):
        taps.append(u)

    bot._handlers = (on_cb, None, None)
    assert asyncio.run(bot.poll_once()) == 1
    assert asyncio.run(bot.poll_once()) == 0                      # never twice
    assert taps[0]["user_id"] == 1001 and taps[0]["data"] == "REQ-0055|ap|abc"


def test_cli_edit_taps_then_answers_the_prompt(tmp_path):
    bot = ClaudeInbox(CFG, data_dir=tmp_path)
    asyncio.run(bot.send_card(Card(kind=CardKind.run_this, request_id="REQ-1", text="t",
                                   buttons=[[Button("Edit", "ed")]]), lambda b: "REQ-1|ed|e1"))
    main(["edit", "REQ-1", "new text", "--data-dir", str(tmp_path)])
    replies = []

    async def on_cb(u):
        await bot.ask_force_reply("type it", "REQ-1")

    async def on_reply(u):
        replies.append(u)

    bot._handlers = (on_cb, None, on_reply)
    asyncio.run(bot.poll_once())
    assert replies[0]["text"] == "new text" and replies[0]["to_bot_msg_id"] == bot._last_prompt


def test_cli_command_and_missing_button(tmp_path):
    assert main(["approve", "REQ-9", "--data-dir", str(tmp_path)]) == 2
    main(["command", "/close", "REQ-9", "--data-dir", str(tmp_path)])
    cmds = []

    async def on_cmd(u):
        cmds.append(u)

    bot = ClaudeInbox(CFG, data_dir=tmp_path)
    bot._handlers = (None, on_cmd, None)
    asyncio.run(bot.poll_once())
    assert cmds == [{"user_id": 1001, "command": "close", "args": "REQ-9"}]
    assert find_callback(tmp_path / "claude_inbox.jsonl", "REQ-9", "ap") is None


def test_inbox_is_trimmed_to_the_newest_lines(tmp_path):
    bot = ClaudeInbox(CFG, data_dir=tmp_path)
    for i in range(30):
        asyncio.run(bot.send_card(Card(kind=CardKind.info, text="x" * 200), lambda b: ""))
    bot._trim(max_bytes=2000, keep=5)
    assert len(_lines(tmp_path / "claude_inbox.jsonl")) == 5


def test_card_carries_its_chat_so_each_panel_shows_its_own(tmp_path):
    """Principal 2026-11-12: two panels. Acme Team cards go to the team panel, the rest to the main one; every
    card names its chat: card.chat, else the request's chat in state.db, else None (system cards)."""
    import sqlite3
    db = sqlite3.connect(tmp_path / "state.db")
    db.execute("create table requests (id text, chat text)")
    db.executemany("insert into requests values (?, ?)", [("REQ-0070", "Acme Team"), ("REQ-0069", "Odoo x Globex")])
    db.commit()
    db.close()
    bot = ClaudeInbox(CFG, data_dir=tmp_path)
    asyncio.run(bot.send_card(Card(kind=CardKind.info, text="Bot message for you", chat="Acme Team"), lambda b: ""))
    asyncio.run(bot.send_card(Card(kind=CardKind.info, text="x", request_id="REQ-0070"), lambda b: ""))
    asyncio.run(bot.send_card(Card(kind=CardKind.info, text="y", request_id="REQ-0069"), lambda b: ""))
    asyncio.run(bot.send_card(Card(kind=CardKind.info, text="Back online"), lambda b: ""))
    asyncio.run(bot.send_card(Card(kind=CardKind.info, text="z", request_id="REQ-9999"), lambda b: ""))
    assert [r["chat"] for r in _lines(tmp_path / "claude_inbox.jsonl")] == [
        "Acme Team", "Acme Team", "Odoo x Globex", None, None]


def test_bot_message_card_names_its_chat(tmp_home, cfg, clock):
    from tests.test_tagged_only_chat import _flow, team_cfg
    from tests.test_listener import settle
    from twin_agent.bus import Topics
    from twin_agent.models import Sender
    from twin_agent.testing.fakes import make_event
    team_cfg(cfg)
    f, db = _flow(tmp_home, cfg, clock)
    cards = []
    f.bus.subscribe(Topics.CARD_OUT, lambda c: cards.append(c))

    async def go():
        ev = make_event("@principaldoembot [pm-manager] -> Principal: done", msg_id=3300,
                        sender=Sender(tg_id=4242, name="Sysadmin's Bot", is_bot=True), chat_id=-100777, topic_id=20,
                        ts=clock.now())
        f.bus.publish(Topics.EVENT_IN, ev)
        await settle(f)
    try:
        asyncio.run(go())
        info = [c for c in cards if "Bot message for you" in c.text]
        assert info and info[0].chat == "Test Group"      # make_event's chat name
    finally:
        db.close()
