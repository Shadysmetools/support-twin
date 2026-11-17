"""The client-support persona: the ONE place the hub's fixed client wording comes from (``messages.yaml``).

Loaded once at import (startup) and validated: a missing key, a missing language, a placeholder that does not match
``fields`` or an Arabic string not marked ``needs_native_review`` raises ``PersonaError`` and the hub does not start
(fail closed). ``client_languages`` (default ``["en"]``) decides which languages may be sent: a client who writes
Arabic still gets an English reply until "ar" is enabled.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

HERE = Path(__file__).parent
REQUIRED = {"company_question", "type_prompt", "confirmation", "feature_brief", "evidence", "meeting", "fallback_ack",
            "confirm", "confirm_need", "resolve", "resolve_video", "verify_step"}
LANGS = ("en", "ar")
_FIELD = re.compile(r"\{(\w+)\}")


class PersonaError(RuntimeError):
    pass


def _pair(name: str, d) -> None:
    if not isinstance(d, dict) or any(not str(d.get(x) or "").strip() for x in LANGS):
        raise PersonaError(f"messages.yaml: '{name}' needs a non-empty en and ar")
    if d.get("needs_native_review") is not True:
        raise PersonaError(f"messages.yaml: '{name}' must be marked needs_native_review: true (Arabic is unreviewed)")


def validate(doc: dict) -> dict:
    if not isinstance(doc, dict):
        raise PersonaError("messages.yaml is empty or not a mapping")
    langs = doc.get("client_languages")
    if not isinstance(langs, list) or not langs or any(x not in LANGS for x in langs):
        raise PersonaError("messages.yaml: client_languages must be a non-empty list of en/ar")
    for k in ("sign", "issue_fallback", "issue_prefix"):
        if k == "issue_prefix":
            if not isinstance(doc.get(k), dict) or "en" not in doc[k]:
                raise PersonaError(f"messages.yaml: '{k}' is missing")
        else:
            _pair(k, doc.get(k))
    msgs = doc.get("messages")
    missing = sorted(REQUIRED - set(msgs or {}))
    if missing:
        raise PersonaError(f"messages.yaml: missing messages {missing}")
    for name, m in msgs.items():
        _pair(name, m)
        want = set(m.get("fields") or [])
        for lang in LANGS:
            got = set(_FIELD.findall(m[lang]))
            if got != want:
                raise PersonaError(f"messages.yaml: '{name}.{lang}' uses {sorted(got)} but fields are {sorted(want)}")
    return doc


def load(path: Path | None = None) -> dict:
    p = path or HERE / "messages.yaml"
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise PersonaError(f"messages.yaml cannot be read ({type(e).__name__})") from None
    return validate(doc)


_DOC = load()
_LANGUAGES: list[str] = list(_DOC["client_languages"])


def client_languages() -> list[str]:
    return list(_LANGUAGES)


def set_client_languages(langs) -> None:
    """The config flag (``client_languages``). Anything else than a non-empty list of en/ar is refused."""
    if not isinstance(langs, (list, tuple)) or not langs or any(x not in LANGS for x in langs):
        raise PersonaError("client_languages must be a non-empty list of en/ar")
    _LANGUAGES[:] = list(langs)


def reply_lang(client_lang: str) -> str:
    """The language a reply may be written in: the client's own when it is enabled, otherwise English."""
    lang = "ar" if str(client_lang).lower().startswith("ar") else "en"
    return lang if lang in _LANGUAGES else "en"


def sign(lang: str) -> str:
    return _DOC["sign"][reply_lang(lang)]


def text(key: str, lang: str, **fields) -> str:
    """A fixed message rendered in the (allowed) reply language. ``sign`` is filled automatically."""
    m = _DOC["messages"][key]
    if "sign" in (m.get("fields") or []):
        fields.setdefault("sign", sign(lang))
    return m[reply_lang(lang)].format(**fields)


def phrase(key: str, lang: str) -> str:
    return _DOC[key][reply_lang(lang)]
