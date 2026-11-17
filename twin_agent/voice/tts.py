"""ElevenLabs text-to-speech in Principal's cloned voice -> OGG/Opus mono 48 kHz (a Telegram voice note).

- `ElevenLabsTTS` is the real adapter. It refuses to start unless run_mode is live, voice consent (D1 + D2 +
  voice.enabled) is on file and the vault name exists. The vault value is JSON {"api_key"[, "voice_id"]}; it is
  read once in the constructor (creds.get_fields, audited) and never logged or shown in a repr. The voice_id may
  instead come from config (`voice.voice_id`), which wins over the vault's - so several people can share ONE
  ElevenLabs api_key and each pick their own voice (Principal: Hans, Supervisor: Gale). Model:
  eleven_multilingual_v2 (Arabic and English). `httpx` is imported lazily, inside the call.
- `VoiceMaker.make` = cache -> monthly character budget -> TTS -> ffmpeg -> playable preview copy.
  Cache key = sha256(voice tag | model | lang | text); file names never carry text or the voice id.
  Only real TTS calls count against the budget (cache hits are free). Budget lives in state.db cursor
  `voice_chars:<YYYY-MM>` (Cairo month).
- ffmpeg runs as an argument list (no shell). Missing ffmpeg = VoiceConvertError; the caller falls back to text.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .policy import file_sha256, voice_cfg, voice_consent_ok

ELEVEN_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
OUTPUT_FORMAT = "mp3_44100_128"
BUDGET_PREFIX = "voice_chars:"


class VoiceError(RuntimeError):
    """A voice note could not be made. The message never carries client text or secrets."""


class VoiceRefused(VoiceError):
    """Not live, no consent, voice off or no vault value."""


class VoiceBudgetExceeded(VoiceError):
    """The monthly ElevenLabs character cap would be passed."""


class VoiceConvertError(VoiceError):
    """ffmpeg missing or failed."""


class TTS(Protocol):
    cache_tag: str                    # hash of voice id + model (never the voice id itself)

    def synthesize(self, text: str, *, lang: str) -> bytes: ...   # encoded audio (mp3)


def _httpx_post(url: str, *, headers: dict, json: dict, params: dict, timeout: float) -> tuple[int, bytes]:
    import httpx                       # lazy: tests and dry runs never import it
    r = httpx.post(url, headers=headers, json=json, params=params, timeout=timeout)
    return r.status_code, r.content


class ElevenLabsTTS:
    """Real TTS. `http_post` is injectable for tests (they pass a fake; the network is blocked anyway)."""

    def __init__(self, cfg: dict, *, has_secret: Callable[[str], bool], get_fields: Callable[[str], dict] | None = None,
                 http_post: Callable[..., tuple[int, bytes]] | None = None):
        v = voice_cfg(cfg)
        if cfg.get("run_mode") != "live":
            raise VoiceRefused("run_mode is not live")
        if not voice_consent_ok(cfg):
            raise VoiceRefused("voice consent (D1 + D2) or voice.enabled missing")
        name = v["vault_name"]
        if not has_secret(name):
            raise VoiceRefused(f"vault name {name} is not set")
        if get_fields is None:
            from .. import creds
            get_fields = lambda n: creds.get_fields(n, caller="voice.tts", purpose="elevenlabs tts")  # noqa: E731
        fields = get_fields(name)
        # The vault entry holds the (shared) api_key; the voice_id may come from config so several people can
        # reuse ONE shared ElevenLabs key with their own distinct voice. Config wins; vault voice_id is the
        # fallback for an entry that still carries its own. One of the two must be set.
        self.__key = str(fields["api_key"])
        cfg_voice_id = str(v.get("voice_id") or "").strip()
        self.__voice_id = cfg_voice_id or str(fields.get("voice_id") or "").strip()
        if not self.__voice_id:
            raise VoiceRefused("no voice_id: set voice.voice_id in config or add it to the vault entry")
        self.model = str(v["model"])
        self.cache_tag = hashlib.sha256(f"{self.__voice_id}|{self.model}".encode()).hexdigest()[:16]
        self._post = http_post or _httpx_post

    def __repr__(self) -> str:
        return f"ElevenLabsTTS(model={self.model!r})"

    def synthesize(self, text: str, *, lang: str) -> bytes:
        body = {"text": text, "model_id": self.model,
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.8}}
        headers = {"xi-api-key": self.__key, "accept": "audio/mpeg", "content-type": "application/json"}
        try:
            status, content = self._post(ELEVEN_URL.format(voice_id=self.__voice_id), headers=headers, json=body,
                                         params={"output_format": OUTPUT_FORMAT}, timeout=60)
        except Exception as e:  # noqa: BLE001 - the exception text could echo headers: keep only its type
            raise VoiceError(f"tts call failed: {type(e).__name__}") from None
        if status != 200 or not content:
            raise VoiceError(f"tts http {status}")
        return content


class CharBudget:
    """Monthly character cap (`voice.monthly_char_cap`), stored as a state.db cursor per Cairo month."""

    def __init__(self, db, cfg: dict, clock):
        self.db, self.clock, self.cap = db, clock, int(voice_cfg(cfg)["monthly_char_cap"])

    def key(self) -> str:
        return BUDGET_PREFIX + self.clock.now().strftime("%Y-%m")

    def used(self) -> int:
        return int(self.db.get_cursor(self.key()) or 0)

    def allow(self, chars: int) -> bool:
        return self.used() + int(chars) <= self.cap

    def record(self, chars: int) -> None:
        self.db.set_cursor(self.key(), str(self.used() + int(chars)))


def ffmpeg_command(ffmpeg: str, src: Path, dst: Path) -> list[str]:
    """Any audio -> OGG container, Opus codec, mono, 48 kHz (what Telegram and WhatsApp play as a voice note)."""
    return [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "48000",
            "-c:a", "libopus", "-b:a", "32k", "-application", "voip", "-f", "ogg", str(dst)]


def to_ogg_opus(audio: bytes, dst: Path, *, ffmpeg: str = "ffmpeg", run=subprocess.run) -> Path:
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    src = dst.with_name(dst.stem + ".src.mp3")
    src.write_bytes(audio)
    try:
        res = run(ffmpeg_command(ffmpeg, src, dst), capture_output=True, timeout=120, shell=False)
    except (FileNotFoundError, OSError) as e:
        raise VoiceConvertError(f"ffmpeg not available: {type(e).__name__}") from None
    except subprocess.TimeoutExpired:
        raise VoiceConvertError("ffmpeg timed out") from None
    finally:
        src.unlink(missing_ok=True)
    if getattr(res, "returncode", 1) != 0 or not dst.is_file() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        raise VoiceConvertError(f"ffmpeg failed (exit {getattr(res, 'returncode', '?')})")
    return dst


@dataclass
class VoiceNote:
    path: Path                         # the playable preview copy, <REQ>-<12 hex of the audio>.ogg
    sha256: str                        # of the OGG bytes: the sender sends only this exact file
    text_sha256: str
    chars: int
    lang: str
    cached: bool


def voice_dir() -> Path:
    from .. import paths
    return paths.data_dir() / "voice"


class VoiceMaker:
    def __init__(self, tts: TTS, cfg: dict, *, budget: CharBudget, out_dir: Path | None = None, run=subprocess.run):
        self.tts, self.cfg, self.budget, self.run = tts, cfg, budget, run
        self.out_dir = Path(out_dir) if out_dir else voice_dir()
        self.v = voice_cfg(cfg)

    def cache_key(self, text: str, lang: str) -> str:
        raw = f"{self.tts.cache_tag}|{self.v['model']}|{lang}|{text}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def make(self, text: str, *, lang: str, request_id: str, fresh: bool = False) -> VoiceNote:
        """fresh=True (Re-record) skips the cache and makes a new take."""
        if not voice_consent_ok(self.cfg):
            raise VoiceRefused("voice consent (D1 + D2) or voice.enabled missing")
        cached = self.out_dir / "cache" / f"{self.cache_key(text, lang)}.ogg"
        hit = cached.is_file() and not fresh
        if not hit:
            if not self.budget.allow(len(text)):
                raise VoiceBudgetExceeded(f"monthly voice budget reached ({self.budget.cap} characters)")
            audio = self.tts.synthesize(text, lang=lang)
            self.budget.record(len(text))
            tmp = cached.with_name(cached.stem + f".{os.getpid()}.tmp.ogg")
            to_ogg_opus(audio, tmp, ffmpeg=str(self.v["ffmpeg"]), run=self.run)
            os.replace(tmp, cached)
        sha = file_sha256(cached)
        preview = self.out_dir / f"{request_id}-{sha[:12]}.ogg"
        if not preview.is_file():
            shutil.copyfile(cached, preview)
        return VoiceNote(path=preview, sha256=sha, text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                         chars=len(text), lang=lang, cached=hit)
