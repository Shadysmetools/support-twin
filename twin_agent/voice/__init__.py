"""Voice-note replies in Principal's cloned voice (HANDOFF 2026-11-02, consent D2).

- policy.py  who may get a voice note, persona rule (short, no numbers, nothing sensitive), consent gate
- tts.py     ElevenLabs TTS adapter, cache, monthly character budget, ffmpeg OGG/Opus conversion
- desk.py    the voice card (text + playable preview path) and the Approve/Re-record/No-voice taps

Sending goes through sender.py like every other reply (same kill switch, dial, approval, live re-read, guards).
"""
