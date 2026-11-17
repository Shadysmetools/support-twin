"""Fakes for voice notes and WhatsApp (no network, no ffmpeg, no ElevenLabs). Synthetic data only."""
from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from .fakes import FakeTelegram


class FakeTTS:
    """Implements voice.tts.TTS. Each take differs (like the real service), so Re-record changes the audio."""

    cache_tag = "fake-voice-tag"

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.raise_exc: Exception | None = None

    def synthesize(self, text: str, *, lang: str) -> bytes:
        if self.raise_exc:
            raise self.raise_exc
        self.calls.append((text, lang))
        return b"FAKEMP3" + hashlib.sha256(f"{len(self.calls)}|{lang}|{text}".encode()).digest()


class FakeFfmpeg:
    """Stands in for subprocess.run(ffmpeg ...): writes an 'OGG' derived from the input bytes."""

    def __init__(self, returncode: int = 0, missing: bool = False):
        self.returncode, self.missing, self.commands = returncode, missing, []

    def __call__(self, cmd, **kw):
        if self.missing:
            raise FileNotFoundError("ffmpeg")
        self.commands.append((list(cmd), kw))
        src, dst = Path(cmd[cmd.index("-i") + 1]), Path(cmd[-1])
        if self.returncode == 0:
            dst.write_bytes(b"OggS" + hashlib.sha256(src.read_bytes()).digest())
        return SimpleNamespace(returncode=self.returncode, stdout=b"", stderr=b"")


class FakeTelegramVoice(FakeTelegram):
    """FakeTelegram that can also send voice notes."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.voice_sent: list[dict] = []

    async def send_voice(self, chat_id: int, path: str, *, caption: str, reply_to: int | None,
                         topic_id: int | None) -> dict:
        if self.raise_on_send:
            from .fakes import FakeSendError
            raise FakeSendError(self.raise_on_send)
        self._next_id += 1
        self.voice_sent.append({"chat_id": chat_id, "path": str(path), "caption": caption, "reply_to": reply_to,
                                "topic_id": topic_id, "msg_id": self._next_id, "bytes": Path(path).read_bytes()})
        return {"msg_id": self._next_id, "from_id": self.from_id}


class FakeWhatsAppHttp:
    """Records Graph API calls; answers like the Cloud API does."""

    def __init__(self):
        self.calls: list[dict] = []
        self.status, self.error_code = 200, None

    def __call__(self, method, url, *, headers, json=None, data=None, files=None, timeout=30):
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json, "data": data,
                           "files": files})
        if self.status != 200:
            return self.status, {"error": {"code": self.error_code, "message": "synthetic"}}
        if url.endswith("/media"):
            return 200, {"id": "media-1"}
        return 200, {"messaging_product": "whatsapp", "messages": [{"id": f"wamid.{len(self.calls)}"}]}
