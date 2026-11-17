

def test_approval_card_text_is_principals_format(tmp_home):
    """Principal 2026-11-09: header | Message | ONE-sentence Analysis | the exact reply | Check first, screenshot found,
    and with a photo the whole card fits a 1024-char caption."""
    from twin_agent import paths
    from twin_agent.flow import approval_card_text
    media = paths.data_dir() / "media"
    media.mkdir(parents=True, exist_ok=True)
    (media / "photo_1_2.jpg").write_bytes(b"x")
    req = {"id": "REQ-0052", "requester_name": "Principal Emam Globex", "chat": "Odoo x Globex", "topic": "Logistics",
           "created_at": "2026-11-09T11:40:09+00:00"}
    thread = [{"is_principal": 0, "text": "محتاج افلد الباتش ده BATCH/13773 " * 30, "media_json": "[]",
               "ts": "2026-11-09T11:40:09+00:00"},
              {"is_principal": 0, "text": "", "media_json": '["photo_1_2.jpg"]', "ts": "2026-11-09T11:41:10+00:00"},
              {"is_principal": 1, "text": "Received, checking it now.", "media_json": "[]", "ts": "2026-11-09T13:07:55+00:00"}]
    packet = {"analysis": "Hugo validated the load without the Turquoise, so it sits on backorder Load/46811/26.",
              "understanding": "Long text. More.", "questions_for_principal": ["Is the unit on the car?", "other"]}
    text, photo = approval_card_text(req, thread, packet, "Turquoise is on Load/46811/26, validate it first.", "T2a")
    assert photo and photo.endswith("photo_1_2.jpg") and len(text) <= 1024
    assert text.startswith("REQ-0052 | Principal Emam Globex | Odoo x Globex / Logistics | 13:40 Cairo")
    assert "\nAnalysis: Hugo validated" in text and "REPLY APPROVE WILL POST:\nTurquoise is on" in text
    assert "Check first: Is the unit on the car?" in text and "Received, checking" not in text
    t2, _ = approval_card_text(req, thread[:1], dict(packet, analysis=None), "", "")
    assert "Analysis: Long text." in t2 and "NO DRAFT REPLY" in t2


def test_full_card_keeps_every_word_for_the_claude_panel(tmp_home):
    """Principal 2026-11-12: 'keep them all open until i close them i.e. not shortened'. With cards going to the
    Claude panel (bot.transport: claude) there is no 1024-char caption: message, analysis and Check first in full."""
    from twin_agent.flow import approval_card_text
    req = {"id": "REQ-0070", "requester_name": "Sysadmin Goodo", "chat": "Acme Team", "created_at": "2026-11-11T13:45:00+00:00"}
    long_msg = "word " * 400
    thread = [{"is_principal": 0, "text": long_msg, "media_json": "[]", "ts": "2026-11-11T13:45:00+00:00"},
              {"is_principal": 0, "text": "second " * 100, "media_json": "[]", "ts": "2026-11-11T13:46:00+00:00"}]
    packet = {"analysis": "a " * 600, "questions_for_principal": ["q " * 300]}
    text, _ = approval_card_text(req, thread, packet, "reply", "T3", full=True)
    assert "…" not in text
    assert text.count("word") == 400 and text.count("second") == 100
    assert text.count(" a") >= 599 and text.count("q") >= 300
    short, _ = approval_card_text(req, thread, packet, "reply", "T3")
    assert "…" in short                                     # Telegram transport: unchanged
