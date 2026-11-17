"""channels/whatsapp_cloud.py: off by default, consent-gated, 24 h window, template-only outside, audio upload."""
import copy
from datetime import timedelta

import pytest
from voice_helpers import voice_on

from twin_agent.channels.whatsapp_cloud import (WhatsAppCloud, WhatsAppError, WhatsAppRefused, WindowClosed,
                                                 in_service_window)
from twin_agent.testing.voice_fakes import FakeWhatsAppHttp

TO = "200000000001"                     # synthetic, not a real number
FIELDS = {"token": "EAAG-test-not-real", "phone_number_id": "pnid-test"}


def wa_live(cfg, **over):
    c = copy.deepcopy(cfg)
    c["run_mode"], c["send_enabled"] = "live", True
    c["consent"]["d1_ref"] = "D1-test"
    c["consent"]["channels"]["whatsapp"] = True
    c["whatsapp"] = {**c["whatsapp"], "enabled": True, "graph_version": "vTEST", "allowed_recipients": [TO], **over}
    return c


def make(cfg, tmp_path, http=None, has=True):
    return WhatsAppCloud(cfg, has_secret=lambda n: has, get_fields=lambda n: FIELDS, http=http or FakeWhatsAppHttp(),
                         stop_file=lambda: tmp_path / "STOP")


def test_disabled_by_default_and_every_gate_refuses(cfg, tmp_path):
    with pytest.raises(WhatsAppRefused):
        make(cfg, tmp_path)                                         # shipped config
    for mutate in (lambda c: c.update(run_mode="dry_run"), lambda c: c.update(send_enabled=False),
                   lambda c: c["whatsapp"].update(enabled=False), lambda c: c["consent"].update(d1_ref=None),
                   lambda c: c["consent"]["channels"].update(whatsapp=False),
                   lambda c: c["whatsapp"].update(graph_version="")):
        c = wa_live(cfg)
        mutate(c)
        with pytest.raises(WhatsAppRefused):
            make(c, tmp_path)
    with pytest.raises(WhatsAppRefused):
        make(wa_live(cfg), tmp_path, has=False)                     # no vault value


def test_window_boundaries(clock):
    now = clock.now()
    assert in_service_window(now - timedelta(hours=23, minutes=59), now)
    assert not in_service_window(now - timedelta(hours=24), now)
    assert not in_service_window(None, now)
    assert not in_service_window(now + timedelta(minutes=5), now)   # a future time is not trusted


def test_text_inside_window_and_template_only_outside(cfg, clock, tmp_path):
    http = FakeWhatsAppHttp()
    wa = make(wa_live(cfg), tmp_path, http)
    now = clock.now()
    r = wa.send_text(TO, "Received, checking it now.", last_inbound_at=now - timedelta(hours=2), now=now)
    assert r["msg_id"].startswith("wamid.")
    call = http.calls[0]
    assert call["url"] == "https://graph.facebook.com/vTEST/pnid-test/messages"
    assert call["json"]["type"] == "text" and call["json"]["to"] == TO and call["json"]["text"]["preview_url"] is False
    assert call["headers"]["Authorization"] == "Bearer EAAG-test-not-real"
    with pytest.raises(WindowClosed):
        wa.send_text(TO, "late", last_inbound_at=now - timedelta(hours=25), now=now)
    assert len(http.calls) == 1                                     # refused before any call
    wa.send_template(TO, "support_update", "en", ["REQ-0001"], now=now)
    t = http.calls[-1]["json"]
    assert t["type"] == "template" and t["template"]["name"] == "support_update"
    assert t["template"]["components"][0]["parameters"][0]["text"] == "REQ-0001"


def test_recipient_allow_list_and_stop_file(cfg, clock, tmp_path):
    wa = make(wa_live(cfg), tmp_path)
    now = clock.now()
    with pytest.raises(WhatsAppRefused):
        wa.send_text("200000000999", "hi", last_inbound_at=now, now=now)
    with pytest.raises(WhatsAppRefused):
        wa.send_template("200000000999", "support_update", "en", now=now)
    (tmp_path / "STOP").write_text("x", encoding="utf-8")
    with pytest.raises(WhatsAppRefused):
        wa.send_text(TO, "hi", last_inbound_at=now, now=now)


def test_audio_needs_voice_consent_window_and_uploads_first(cfg, clock, tmp_path):
    ogg = tmp_path / "REQ-0001-abc.ogg"
    ogg.write_bytes(b"OggS-synthetic")
    now = clock.now()
    no_voice = make(wa_live(cfg), tmp_path)
    with pytest.raises(WhatsAppRefused):
        no_voice.send_audio(TO, ogg, last_inbound_at=now, now=now)
    http = FakeWhatsAppHttp()
    wa = make(wa_live(voice_on(cfg)), tmp_path, http)
    with pytest.raises(WindowClosed):
        wa.send_audio(TO, ogg, last_inbound_at=now - timedelta(days=2), now=now)
    assert http.calls == []
    wa.send_audio(TO, ogg, last_inbound_at=now - timedelta(hours=1), now=now)
    up, msg = http.calls
    assert up["url"].endswith("/media") and up["data"]["type"] == "audio/ogg"
    assert up["files"]["file"][1] == b"OggS-synthetic"
    assert msg["json"]["type"] == "audio" and msg["json"]["audio"] == {"id": "media-1"}
    with pytest.raises(WhatsAppRefused):
        wa.send_audio(TO, tmp_path / "x.mp3", last_inbound_at=now, now=now)


def test_errors_never_carry_number_or_token(cfg, clock, tmp_path):
    http = FakeWhatsAppHttp()
    http.status, http.error_code = 400, 131047
    wa = make(wa_live(cfg), tmp_path, http)
    now = clock.now()
    with pytest.raises(WhatsAppError) as e:
        wa.send_text(TO, "hi", last_inbound_at=now, now=now)
    assert "131047" in str(e.value) and TO not in str(e.value) and "EAAG" not in str(e.value)
    assert "EAAG" not in repr(wa) and "pnid" not in repr(wa)
