"""RECEIVE parsing (review #13 batched webhooks, #14 media): every entry, change and message becomes an Inbound."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from twin_agent.hub import webhook

FIX = Path(__file__).parent.parent / "twin_agent" / "hub" / "fixtures"


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _value(p: dict) -> dict:
    return p["entry"][0]["changes"][0]["value"]


def test_two_messages_in_one_change_are_both_returned():
    p = _load("webhook_text.json")
    m2 = copy.deepcopy(_value(p)["messages"][0])
    m2["id"] = "wamid.SECOND"
    m2["text"]["body"] = "second one"
    _value(p)["messages"].append(m2)
    got = webhook.parse_all(p)
    assert [i.message_id for i in got] == ["wamid.TEXT0001", "wamid.SECOND"]


def test_a_status_change_does_not_stop_the_loop():
    p = _load("webhook_text.json")
    status_change = _load("webhook_status.json")["entry"][0]["changes"][0]
    p["entry"][0]["changes"].insert(0, status_change)            # statuses first, then the real message
    p["entry"].append(copy.deepcopy(_load("webhook_voice.json")["entry"][0]))   # a second entry, a voice note
    got = webhook.parse_all(p)
    assert [i.kind for i in got] == ["text", "voice"]


def test_contacts_name_is_matched_per_sender():
    p = _load("webhook_text.json")
    v = _value(p)
    v["contacts"].append({"profile": {"name": "Other Person"}, "wa_id": "201000000002"})
    m2 = copy.deepcopy(v["messages"][0])
    m2.update({"id": "wamid.B", "from": "201000000002"})
    v["messages"].append(m2)
    got = webhook.parse_all(p)
    assert [i.name for i in got] == ["Oscar Client", "Other Person"]


def test_image_video_document_are_parsed_as_media_not_dropped():
    for mtype, extra in (("image", {"caption": "see this", "mime_type": "image/jpeg"}),
                         ("video", {"mime_type": "video/mp4"}),
                         ("document", {"filename": "x.pdf", "mime_type": "application/pdf"})):
        p = _load("webhook_text.json")
        m = _value(p)["messages"][0]
        m.pop("text")
        m["type"] = mtype
        m[mtype] = {"id": "MEDIA_X1", **extra}
        got = webhook.parse_all(p)
        assert len(got) == 1 and got[0].kind == "media" and got[0].media_id == "MEDIA_X1", mtype
        assert got[0].media_type == mtype


def test_image_caption_becomes_text():
    p = _load("webhook_text.json")
    m = _value(p)["messages"][0]
    m.pop("text")
    m["type"] = "image"
    m["image"] = {"id": "M1", "caption": "the error is here"}
    assert webhook.parse_all(p)[0].text == "the error is here"


def test_other_message_types_are_ignored_not_crashed():
    p = _load("webhook_text.json")
    m = _value(p)["messages"][0]
    m.pop("text")
    m["type"] = "reaction"
    assert webhook.parse_all(p) == []


def test_phone_number_id_filter_is_per_change():
    p = _load("webhook_text.json")
    other = copy.deepcopy(p["entry"][0]["changes"][0])
    other["value"]["metadata"]["phone_number_id"] = "OTHER"
    other["value"]["messages"][0]["id"] = "wamid.OTHER"
    p["entry"][0]["changes"].append(other)
    got = webhook.parse_all(p, phone_number_id="PHONE_NUMBER_ID")
    assert [i.message_id for i in got] == ["wamid.TEXT0001"]
    assert webhook.parse_all(p, phone_number_id="") == []         # nothing configured -> fail closed


def test_garbage_payloads_do_not_raise():
    for bad in ({}, {"entry": None}, {"entry": [None]}, {"entry": [{"changes": [{"value": None}]}]},
                {"entry": [{"changes": [{"value": {"messages": [None]}}]}]}):
        assert webhook.parse_all(bad) == []


def test_parse_keeps_first_or_none_for_existing_callers():
    assert webhook.parse(_load("webhook_text.json")).message_id == "wamid.TEXT0001"
    assert webhook.parse(_load("webhook_status.json")) is None
