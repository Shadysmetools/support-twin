"""Shared Gmail helpers: parse a Gmail API message into an Event, and build the real API service.

Owner: CHANNELS. Pure parsing lives here so gmail_in, gmail_send and tickets share one implementation.
The real service is built only when run_mode is live and the vault holds the tokens (CONTRACT 0.3).
Google libraries are imported inside functions, so tests and other modules never need them.
"""
from __future__ import annotations

import base64
import html
import json
import re
from datetime import datetime
from email.utils import getaddresses, parseaddr
from typing import Any

from ..clock import CAIRO
from ..models import Channel, Event, Sender

SCOPE_PREFIX = "https://www.googleapis.com/auth/"
OAUTH_CLIENT_VAULT = "google-oauth-client"

# Headers we keep on the Event (lower-case keys). Everything else is dropped.
KEEP_HEADERS = ("from", "to", "cc", "subject", "message-id", "in-reply-to", "references", "auto-submitted",
                "x-autoreply", "x-autorespond", "precedence", "x-principal-req", "list-id", "date")

# Zero-width and padding characters that Odoo notification mails are full of.
_INVISIBLE = re.compile("[͏​‌‍⁠﻿­]")
_TAG = re.compile(r"<[^>]+>")
_BLOCK_TAG = re.compile(r"(?i)<\s*(br|/p|/div|/tr|/li|/h\d)\b[^>]*>")


class AdapterRefused(RuntimeError):
    """A real Gmail adapter was asked to start while the mode or the vault does not allow it."""


def full_scopes(short: list[str]) -> list[str]:
    """['gmail.readonly'] -> ['https://www.googleapis.com/auth/gmail.readonly']."""
    return [s if s.startswith("https://") else SCOPE_PREFIX + s for s in short]


def mailbox_cfg(cfg: dict, mailbox: str) -> dict:
    box = (cfg.get("mailboxes") or {}).get(mailbox)
    if box is None:
        raise KeyError(f"unknown mailbox {mailbox!r} (not in config mailboxes)")
    return box


def require_live(cfg: dict, mailbox: str, *, need_scope: str | None = None) -> dict:
    """Raise AdapterRefused unless run_mode is live, the mailbox is enabled and its vault names exist."""
    from .. import creds  # module attribute access, so tests can monkeypatch it
    if cfg.get("run_mode") != "live":
        raise AdapterRefused("run_mode is not live: use the fake Gmail adapter")
    box = mailbox_cfg(cfg, mailbox)
    if box.get("enabled") is False:
        raise AdapterRefused(f"mailbox {mailbox} is disabled in config")
    for name in (OAUTH_CLIENT_VAULT, box.get("vault_name", "")):
        if not name or not creds.has_secret(name):
            raise AdapterRefused(f"vault name missing: {name or '(none)'}")
    if need_scope and need_scope not in (box.get("scopes") or []):
        raise AdapterRefused(f"mailbox {mailbox} lacks scope {need_scope}")
    return box


def build_service(cfg: dict, mailbox: str, *, need_scope: str | None = None) -> Any:
    """Real Gmail API service for one mailbox. Values come from the vault and never leave this function."""
    box = require_live(cfg, mailbox, need_scope=need_scope)
    from .. import creds
    from google.oauth2.credentials import Credentials  # lazy: real adapter only
    from googleapiclient.discovery import build

    client = json.loads(creds._get(OAUTH_CLIENT_VAULT))
    client = client.get("installed") or client.get("web") or client
    token = Credentials(None, refresh_token=creds._get(box["vault_name"]),
                        token_uri=client.get("token_uri", "https://oauth2.googleapis.com/token"),
                        client_id=client["client_id"], client_secret=client["client_secret"],
                        scopes=full_scopes(box.get("scopes") or []))
    import google_auth_httplib2  # finite timeout: a network hang must not stall the poller thread forever
    import httplib2
    http = google_auth_httplib2.AuthorizedHttp(token, http=httplib2.Http(timeout=60))
    return build("gmail", "v1", http=http, cache_discovery=False)


# ------------------------------------------------------------------ parsing

def _b64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def html_to_text(s: str) -> str:
    s = _BLOCK_TAG.sub("\n", s)
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", s)
    return html.unescape(_TAG.sub("", s))


def clean_text(s: str) -> str:
    s = _INVISIBLE.sub("", s).replace("\r\n", "\n")
    s = re.sub(r"[ \t]+\n", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def strip_quoted(text: str) -> str:
    """Keep only the new part of a reply: drop '>' lines and everything after 'On ... wrote:'.
    2026-11-10: also Outlook's quoted history (a '____' rule, or a 'From:' line followed by 'Sent:'/'Date:' and
    'To:'), which put Signatory's whole thread into his one-line card."""
    out: list[str] = []
    lines = text.split("\n")
    for i, line in enumerate(lines):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        s = line.strip()
        if s.startswith("On ") and (line.rstrip().endswith("wrote:") or nxt.rstrip().endswith("wrote:")):
            break
        if re.match(r"^-{2,}\s*Original Message\s*-{2,}", s, re.I):
            break
        if re.match(r"^_{10,}$", s):                                        # Outlook's rule above the history
            break
        if re.match(r"^\*?From:\*?\s", s, re.I):
            head = "\n".join(x.strip() for x in lines[i + 1:i + 5])
            if re.search(r"(?im)^\*?(Sent|Date):", head) and re.search(r"(?im)^\*?(To|Subject):", head):
                break
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    return "\n".join(out).strip()


def _walk(payload: dict, plain: list[str], htmls: list[str], atts: list[dict]) -> None:
    mime = (payload.get("mimeType") or "").lower()
    body = payload.get("body") or {}
    filename = payload.get("filename") or ""
    if filename and body.get("attachmentId"):
        atts.append({"filename": filename, "attachment_id": body["attachmentId"], "mime": mime,
                     "size": body.get("size", 0)})
    elif mime == "text/plain" and body.get("data"):
        plain.append(_b64(body["data"]).decode("utf-8", "replace"))
    elif mime == "text/html" and body.get("data"):
        htmls.append(_b64(body["data"]).decode("utf-8", "replace"))
    for part in payload.get("parts") or []:
        _walk(part, plain, htmls, atts)


def message_parts(raw: dict) -> tuple[dict[str, str], str, list[dict]]:
    """Gmail API message (format=full) -> (kept headers lower-case, body text, attachment metadata)."""
    payload = raw.get("payload") or {}
    headers: dict[str, str] = {}
    for h in payload.get("headers") or []:
        k = h.get("name", "").lower()
        if k in KEEP_HEADERS:
            headers[k] = h.get("value", "")
    plain: list[str] = []
    htmls: list[str] = []
    atts: list[dict] = []
    _walk(payload, plain, htmls, atts)
    text = "\n".join(plain) if plain else html_to_text("\n".join(htmls))
    return headers, clean_text(text), atts


def address(header_value: str) -> str:
    """'"Name | Co" <a@b.c>' -> 'a@b.c' (lower). The display name is never trusted."""
    return parseaddr(header_value or "")[1].lower()


def addresses(header_value: str) -> list[str]:
    return [a.lower() for _, a in getaddresses([header_value or ""]) if a]


def domain_match(addr: str, rules: list[str]) -> bool:
    """rules: full addresses ('help@example.com') or domains ('example.com' also matches 'helpdesk.example.com')."""
    addr = addr.lower()
    dom = addr.rsplit("@", 1)[-1]
    for r in rules:
        r = r.lower().lstrip("@")
        if ("@" in r and addr == r) or ("@" not in r and (dom == r or dom.endswith("." + r))):
            return True
    return False


def attachment_ref(message_id: str, att: dict) -> str:
    """Media entry for an attachment: 'gmail-att:<message id>:<attachment id>:<safe file name>'."""
    safe = re.sub(r"[^\w.\- ]", "_", att["filename"])[:80]
    return f"gmail-att:{message_id}:{att['attachment_id']}:{safe}"


def to_event(raw: dict, mailbox: str, *, own_addresses: list[str], channel: Channel = Channel.email,
             strip: bool = True) -> Event:
    """Normalise one Gmail message. The caller decides allow-listing and the channel.

    strip=False keeps quoted text (ticket notifications carry the ticket number inside quotes)."""
    headers, text, atts = message_parts(raw)
    from_addr = address(headers.get("from", ""))
    name = parseaddr(headers.get("from", ""))[0]
    ts = datetime.fromtimestamp(int(raw.get("internalDate", "0")) / 1000, CAIRO)
    is_principal = bool(from_addr) and from_addr in [a.lower() for a in own_addresses]
    msg_id = raw.get("id", "")
    return Event(
        event_id=f"gm:{mailbox}:{msg_id}", channel=channel, chat=mailbox, chat_id=mailbox,
        topic=None, topic_id=None,
        sender=Sender(name=name, email=from_addr, is_principal=is_principal),
        ts=ts, text=(strip_quoted(text) or text) if strip else text, media=[attachment_ref(msg_id, a) for a in atts],
        reply_to=headers.get("in-reply-to") or None, untrusted=not is_principal, msg_id=msg_id,
        subject=headers.get("subject", ""), thread_id=raw.get("threadId", ""), headers=headers,
    )
