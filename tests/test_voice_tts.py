"""voice/policy.py + voice/tts.py: consent gate, persona rule, ElevenLabs adapter, cache, budget, ffmpeg."""
import pytest
from sender_fakedb import FakeStateDB
from voice_helpers import SHORT_AR, SHORT_EN, make_maker, voice_on

from twin_agent.models import Channel, OutboundMessage, OutKind, SendTrigger
from twin_agent.redact import redact
from twin_agent.testing.voice_fakes import FakeFfmpeg, FakeTTS
from twin_agent.voice import policy, tts
from twin_agent.voice.desk import voice_card


# ------------------------------------------------------------ consent + persona rule

def test_consent_needs_d1_d2_and_enabled(cfg):
    assert not policy.voice_consent_ok(cfg)                       # shipped default: off
    on = voice_on(cfg)
    assert policy.voice_consent_ok(on)
    for path in (("consent", "d1_ref"), ("consent", "d2_voice_ref")):
        c = voice_on(cfg)
        c[path[0]][path[1]] = None
        assert not policy.voice_consent_ok(c)
    assert not policy.voice_consent_ok(voice_on(cfg, enabled="yes"))   # only a real true


def test_example_config_ships_voice_and_whatsapp_off(cfg):
    assert cfg["voice"]["enabled"] is False and cfg["voice"]["autopilot"] is False
    assert cfg["voice"]["model"] == "eleven_multilingual_v2"
    assert cfg["whatsapp"]["enabled"] is False and cfg["consent"]["channels"]["whatsapp"] is False


@pytest.mark.parametrize("text,lang", [(SHORT_EN, "en"), (SHORT_AR, "ar"),
                                       ("Received, checking it now. - Principal's assistant", None)])
def test_short_plain_replies_may_be_spoken(cfg, text, lang):
    assert policy.voice_text_problems(text, kind=OutKind.answer, lang=lang, cfg=cfg) == []


@pytest.mark.parametrize("text,why", [
    ("Open Inventory then Transfers. " * 12, "too long"),
    ("Qty 1250 moved to bin 3340 on line 12.", "numbers-heavy"),
    ("Check WAVE/00001 please.", "record references"),
    ("See https://helpdesk.example.com/my/ticket/1 for it.", "link"),
    ("Mail me at someone@example.com please.", "personal data"),
    ("Send me your password and I will check.", "sensitive"),
    ("ابعتلي السعر من فضلك", "sensitive"),
    ("Tamam, jarrab keda tany delwa2ty.", "Franco"),
])
def test_persona_rule_refuses(cfg, text, why):
    probs = policy.voice_text_problems(text, kind=OutKind.answer, lang=None, cfg=cfg)
    assert probs and any(why.lower() in p.lower() for p in probs), probs


def test_persona_rule_refuses_kinds_and_franco_lang(cfg):
    assert policy.voice_text_problems(SHORT_EN, kind=OutKind.ticket_email, lang="en", cfg=cfg)
    assert policy.voice_text_problems(SHORT_EN, kind=OutKind.answer, lang="franco", cfg=cfg)
    assert policy.detect_lang(SHORT_AR) == "ar" and policy.detect_lang(SHORT_EN) == "en"


def _msg(**kw):
    base = dict(request_id="REQ-0001", step="draft-1", kind=OutKind.answer, channel=Channel.telegram_group,
                text=SHORT_EN, chat_id=-100500, topic_id=7, reply_to=5)
    base.update(kw)
    return OutboundMessage(**base)


def test_voice_blockers(cfg, tmp_path):
    on = voice_on(cfg)
    assert policy.voice_blockers(_msg(), on, SendTrigger.approval) == []          # plain text: nothing to add
    f = tmp_path / "a.ogg"
    f.write_bytes(b"OggS-take-1")
    sha = policy.file_sha256(f)
    good = _msg(voice_path=str(f), voice_sha256=sha, voice_lang="en")
    assert policy.voice_blockers(good, on, SendTrigger.approval) == []
    assert "voice_consent" in policy.voice_blockers(good, cfg, SendTrigger.approval)
    assert "voice_autopilot" in policy.voice_blockers(good, on, SendTrigger.timeout)
    assert policy.voice_blockers(good, voice_on(cfg, autopilot=True), SendTrigger.timeout) == []
    assert "voice_lane" in policy.voice_blockers(_msg(kind=OutKind.evidence_ask, voice_path=str(f),
                                                      voice_sha256=sha), on, SendTrigger.approval)
    assert "voice_channel" in policy.voice_blockers(_msg(channel=Channel.email, voice_path=str(f),
                                                         voice_sha256=sha), on, SendTrigger.approval)
    assert "voice_persona" in policy.voice_blockers(_msg(text="Check WAVE/00001", voice_path=str(f),
                                                         voice_sha256=sha), on, SendTrigger.approval)
    assert "voice_binding" in policy.voice_blockers(_msg(voice_path=str(f), voice_sha256="0" * 64), on,
                                                    SendTrigger.approval)
    assert "voice_binding" in policy.voice_blockers(_msg(voice_path=str(tmp_path / "gone.ogg"), voice_sha256=sha),
                                                    on, SendTrigger.approval)


# ------------------------------------------------------------ ElevenLabs adapter

class Post:
    def __init__(self, status=200, content=b"MP3"):
        self.status, self.content, self.calls = status, content, []

    def __call__(self, url, *, headers, json, params, timeout):
        self.calls.append({"url": url, "headers": headers, "json": json, "params": params})
        return self.status, self.content


FIELDS = {"api_key": "sk-test-not-real", "voice_id": "voice-test-id"}


def _live(cfg):
    c = voice_on(cfg)
    c["run_mode"] = "live"
    return c


def test_elevenlabs_refuses_unless_live_consented_and_vaulted(cfg):
    ok = dict(has_secret=lambda n: True, get_fields=lambda n: FIELDS, http_post=Post())
    with pytest.raises(tts.VoiceRefused):
        tts.ElevenLabsTTS(voice_on(cfg), **ok)                    # dry_run
    with pytest.raises(tts.VoiceRefused):
        tts.ElevenLabsTTS(dict(cfg, run_mode="live"), **ok)        # no consent
    with pytest.raises(tts.VoiceRefused):
        tts.ElevenLabsTTS(_live(cfg), has_secret=lambda n: False, get_fields=lambda n: FIELDS)


def test_elevenlabs_request_shape_and_secret_hygiene(cfg):
    post = Post()
    asked = []
    t = tts.ElevenLabsTTS(_live(cfg), has_secret=lambda n: asked.append(n) or True,
                          get_fields=lambda n: FIELDS, http_post=post)
    assert asked == ["elevenlabs"]
    assert t.synthesize(SHORT_AR, lang="ar") == b"MP3"
    call = post.calls[0]
    assert call["url"].endswith("/v1/text-to-speech/voice-test-id")
    assert call["json"]["model_id"] == "eleven_multilingual_v2" and call["json"]["text"] == SHORT_AR
    assert call["headers"]["xi-api-key"] == "sk-test-not-real"
    assert "sk-test" not in repr(t) and "voice-test-id" not in t.cache_tag
    post.status = 401
    with pytest.raises(tts.VoiceError) as e:
        t.synthesize(SHORT_EN, lang="en")
    assert "sk-test" not in str(e.value) and "401" in str(e.value)


# ------------------------------------------------------------ ffmpeg + maker (cache, budget)

def test_ffmpeg_command_is_ogg_opus_mono_48k_argv():
    cmd = tts.ffmpeg_command("ffmpeg", "in put.mp3", "out dir/x.ogg")
    assert cmd[0] == "ffmpeg" and "in put.mp3" in cmd and cmd[-1] == "out dir/x.ogg"
    joined = " ".join(cmd)
    assert "-ac 1" in joined and "-ar 48000" in joined and "-c:a libopus" in joined and "-f ogg" in joined


def test_maker_cache_budget_and_fresh_take(cfg, clock, tmp_path):
    on = voice_on(cfg, monthly_char_cap=100)
    db = FakeStateDB(clock)
    maker, fake, ff = make_maker(on, db, clock, tmp_path / "voice")
    a = maker.make(SHORT_EN, lang="en", request_id="REQ-0001")
    assert not a.cached and a.path.name == f"REQ-0001-{a.sha256[:12]}.ogg" and a.path.read_bytes()[:4] == b"OggS"
    assert ff.commands and ff.commands[0][1]["shell"] is False
    b = maker.make(SHORT_EN, lang="en", request_id="REQ-0001")
    assert b.cached and b.sha256 == a.sha256 and len(fake.calls) == 1        # cache hit: no second TTS call
    assert db.get_cursor("voice_chars:2026-11") == str(len(SHORT_EN))       # only real calls count
    c = maker.make(SHORT_EN, lang="en", request_id="REQ-0001", fresh=True)  # Re-record: a new take
    assert not c.cached and c.sha256 != a.sha256 and len(fake.calls) == 2
    with pytest.raises(tts.VoiceBudgetExceeded):
        maker.make("Another short reply that is new here.", lang="en", request_id="REQ-0002")
    assert len(fake.calls) == 2                                              # refused BEFORE calling ElevenLabs
    clock.set(clock.now().replace(month=12, day=1))
    maker.make("Another short reply that is new here.", lang="en", request_id="REQ-0002")   # new month
    names = " ".join(p.name for p in (tmp_path / "voice").rglob("*"))
    assert "try" not in names.lower() and "fake-voice-tag" not in names       # no text, no voice id in names


def test_maker_errors(cfg, clock, tmp_path):
    db = FakeStateDB(clock)
    with pytest.raises(tts.VoiceRefused):
        make_maker(cfg, db, clock, tmp_path)[0].make(SHORT_EN, lang="en", request_id="REQ-0001")
    on = voice_on(cfg)
    with pytest.raises(tts.VoiceConvertError):
        make_maker(on, db, clock, tmp_path, ffmpeg=FakeFfmpeg(missing=True))[0].make(
            SHORT_EN, lang="en", request_id="REQ-0001")
    with pytest.raises(tts.VoiceConvertError):
        make_maker(on, db, clock, tmp_path, ffmpeg=FakeFfmpeg(returncode=1))[0].make(
            SHORT_EN, lang="en", request_id="REQ-0001")
    bad = FakeTTS()
    bad.raise_exc = tts.VoiceError("tts http 500")
    with pytest.raises(tts.VoiceError):
        make_maker(on, db, clock, tmp_path, tts=bad)[0].make(SHORT_EN, lang="en", request_id="REQ-0001")
    assert not list(tmp_path.glob("*.src.mp3")) and not list(tmp_path.rglob("*.tmp.ogg"))


def test_voice_card_shows_text_and_playable_path_after_redaction(cfg, clock, tmp_home):
    on = voice_on(cfg)
    maker, _, _ = make_maker(on, FakeStateDB(clock), clock, tmp_home / "data" / "voice")
    note = maker.make(SHORT_EN, lang="en", request_id="REQ-0001")
    card = voice_card("REQ-0001", SHORT_EN, note, caption="AI voice of Principal's assistant")
    shown = redact(card.text)                       # the bot redacts card text again: the path must survive
    assert SHORT_EN in shown and str(note.path) in shown and card.attachments == [str(note.path)]
    actions = [b.action for row in card.buttons for b in row]
    assert actions[:3] == ["as", "nv", "rr"] and "ap" not in actions


def test_config_voice_id_overrides_vault_for_shared_key(cfg):
    """Shared ElevenLabs key: the vault holds only api_key; each person's config picks the voice."""
    shared = {"api_key": "sk-shared-key"}                       # no voice_id in the vault
    supervisor_cfg = _live(voice_on(cfg, voice_id="gale-supervisor"))
    post = Post()
    t = tts.ElevenLabsTTS(supervisor_cfg, has_secret=lambda n: True, get_fields=lambda n: shared, http_post=post)
    assert t.synthesize(SHORT_AR, lang="ar") == b"MP3"
    assert post.calls[0]["url"].endswith("/v1/text-to-speech/gale-supervisor")


def test_config_voice_id_wins_over_vault_voice_id(cfg):
    both = {"api_key": "sk-shared-key", "voice_id": "vault-voice"}
    c = _live(voice_on(cfg, voice_id="config-voice"))
    post = Post()
    t = tts.ElevenLabsTTS(c, has_secret=lambda n: True, get_fields=lambda n: both, http_post=post)
    t.synthesize(SHORT_EN, lang="en")
    assert post.calls[0]["url"].endswith("/config-voice")


def test_refuses_when_no_voice_id_anywhere(cfg):
    only_key = {"api_key": "sk-shared-key"}
    with pytest.raises(tts.VoiceRefused):
        tts.ElevenLabsTTS(_live(cfg), has_secret=lambda n: True, get_fields=lambda n: only_key)
