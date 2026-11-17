"""Test helpers for the Gmail adapters: synthetic Gmail API messages and a fake Google service object.

Synthetic data only (CONTRACT 0.7): example.test addresses, made-up ticket numbers.
"""
from __future__ import annotations

import base64
from typing import Any


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def raw_msg(mid: str, *, frm: str, subject: str = "Hello", body: str = "hi", to: str = "principal@example.org",
            cc: str = "", labels: tuple[str, ...] = ("INBOX",), html: bool = False, thread: str = "t1",
            ts_ms: int = 1793536800000, extra_headers: dict[str, str] | None = None,
            attachments: tuple[str, ...] = ()) -> dict:
    headers = [{"name": "From", "value": frm}, {"name": "To", "value": to}, {"name": "Subject", "value": subject},
               {"name": "Message-ID", "value": f"<{mid}@mail.example.org>"}]
    if cc:
        headers.append({"name": "Cc", "value": cc})
    for k, v in (extra_headers or {}).items():
        headers.append({"name": k, "value": v})
    parts = [{"mimeType": "text/html" if html else "text/plain", "body": {"data": b64(body)}}]
    for i, name in enumerate(attachments):
        parts.append({"mimeType": "image/png", "filename": name, "body": {"attachmentId": f"att{i}", "size": 10}})
    return {"id": mid, "threadId": thread, "labelIds": list(labels), "internalDate": str(ts_ms),
            "payload": {"mimeType": "multipart/mixed", "headers": headers, "parts": parts}}


class HttpErr(Exception):
    def __init__(self, status: int):
        super().__init__(f"http {status}")
        self.resp = type("R", (), {"status": status})()


class _Call:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeService:
    """Mimics service.users().<resource>().<method>(...).execute() for the calls our adapters make."""

    def __init__(self):
        self.msgs: dict[str, dict] = {}
        self.hist: list[tuple[int, str]] = []       # (history id, message id)
        self.history_id = 100
        self.history_expired = False
        self.page_size = 2
        self.drafts_: dict[str, dict] = {}
        self.sent: list[str] = []
        self.calls: list[tuple[str, dict]] = []
        self.draft_headers_override: dict[str, list[dict]] = {}

    def add(self, raw: dict) -> None:
        self.history_id += 1
        self.msgs[raw["id"]] = raw
        self.hist.append((self.history_id, raw["id"]))

    # resource chain
    def users(self):
        return self

    def history(self):
        return _Res(self, "history")

    def messages(self):
        return _Res(self, "messages")

    def drafts(self):
        return _Res(self, "drafts")

    def attachments(self):
        return _Res(self, "attachments")

    def getProfile(self, userId):
        return _Call(lambda: {"historyId": str(self.history_id)})


class _Res:
    def __init__(self, svc: FakeService, kind: str):
        self.svc, self.kind = svc, kind

    def attachments(self):
        return _Res(self.svc, "attachments")

    def list(self, **kw):
        self.svc.calls.append((f"{self.kind}.list", kw))
        if self.kind == "history":
            return _Call(lambda: self._history(kw))
        return _Call(lambda: {"messages": [{"id": m} for m in self.svc.msgs]})

    def _history(self, kw: dict) -> dict:
        if self.svc.history_expired:
            raise HttpErr(404)
        start = int(kw["startHistoryId"])
        items = [(h, m) for h, m in self.svc.hist if h > start]
        offset = int(kw.get("pageToken") or 0)
        page = items[offset:offset + self.svc.page_size]
        out: dict[str, Any] = {"history": [{"id": str(h), "messagesAdded": [{"message": {"id": m}}]} for h, m in page],
                               "historyId": str(self.svc.history_id)}
        if offset + self.svc.page_size < len(items):
            out["nextPageToken"] = str(offset + self.svc.page_size)
        return out

    def get(self, **kw):
        self.svc.calls.append((f"{self.kind}.get", kw))
        if self.kind == "messages":
            def go():
                if kw["id"] not in self.svc.msgs:
                    raise HttpErr(404)
                return self.svc.msgs[kw["id"]]
            return _Call(go)
        if self.kind == "attachments":
            return _Call(lambda: {"data": b64("PNGDATA")})
        if self.kind == "drafts":
            d = self.svc.drafts_[kw["id"]]
            hdrs = self.svc.draft_headers_override.get(kw["id"], d["headers"])
            return _Call(lambda: {"id": kw["id"], "message": {"threadId": d.get("threadId", ""),
                                                              "payload": {"headers": hdrs}}})
        raise AssertionError(self.kind)

    def create(self, **kw):
        self.svc.calls.append((f"{self.kind}.create", kw))
        import email
        raw = base64.urlsafe_b64decode(kw["body"]["message"]["raw"])
        em = email.message_from_bytes(raw)
        did = f"r{len(self.svc.drafts_) + 1}"
        self.svc.drafts_[did] = {"raw": raw, "threadId": kw["body"]["message"].get("threadId", ""),
                                "headers": [{"name": k, "value": v} for k, v in em.items()]}
        return _Call(lambda: {"id": did})

    def send(self, **kw):
        self.svc.calls.append((f"{self.kind}.send", kw))
        self.svc.sent.append(kw["body"]["id"])
        return _Call(lambda: {"id": f"g{len(self.svc.sent)}"})
