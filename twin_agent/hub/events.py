"""One INFO line per hub event, and the delivery-status record for the messages the hub sends.

* ``event(name, **fields)``: ``event=ack_sent tid=2714 kind=ack wamid=a1b2c3``. Ids, kinds and statuses ONLY. A value
  must be a short token (letters, digits, ``_ . : -``); anything else becomes ``?`` and a run of 7+ digits is masked, so a
  phone number, a name or a piece of message text can never reach the log by accident.
* ``sends`` namespace: one doc per message the hub sent (key = Meta message id), linked to the ticket and to what kind of
  message it was (``ack`` or ``reply``). Meta's status callbacks (sent -> delivered -> read, or failed + an error code) are
  applied with ``apply_status``; a status never moves a message backwards (callbacks arrive out of order).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

log = logging.getLogger("twin_agent.hub.events")

SENDS_NS = "sends"
RANK = {"sent": 1, "delivered": 2, "read": 3}
_TOKEN = re.compile(r"[A-Za-z0-9_.:\-]{1,32}")
_LONG_DIGITS = re.compile(r"\d{7,}")


def _safe(value) -> str:
    s = str(value)
    if not _TOKEN.fullmatch(s):
        return "?"
    return _LONG_DIGITS.sub("?", s)


def wamid_tail(wamid: str) -> str:
    """The last 6 letters/digits of a Meta message id: enough to match a log line to a callback, not an identifier."""
    return re.sub(r"[^A-Za-z0-9]", "", str(wamid or ""))[-6:]


def has_statuses(payload) -> bool:
    return any(isinstance(ch, dict) and (ch.get("value") or {}).get("statuses")
               for e in (payload or {}).get("entry") or [] if isinstance(e, dict) for ch in e.get("changes") or [])


def event(name: str, **fields) -> None:
    parts = [f"event={_safe(name)}"] + [f"{_safe('msg' if k == 'wamid' else k)}={_safe(v)}" for k, v in fields.items() if v not in (None, "")]
    log.info(" ".join(parts))


# ---------------------------------------------------------------- the sends store

def _iso(now) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def record_send(store, res, *, tid: int, kind: str, msg_kind: str, source_mid: str = "", now=None) -> str:
    """Remember a message Meta accepted. ``res`` is the sender's answer (``{"msg_id": ...}``); a sender that gives no id
    (a fake) records nothing. ``kind`` is "ack" or "reply"; ``msg_kind`` is confirm / reply / resolve / status / normal /
    fallback. Returns the id's log tail ("" when nothing was recorded)."""
    wamid = str((res or {}).get("msg_id") or "") if isinstance(res, dict) else ""
    if not wamid:
        return ""
    at = _iso(now)
    try:                                   # bookkeeping must never change the outcome of a send that already went out
        store.put_if_absent(SENDS_NS, wamid, {"wamid": wamid, "tid": int(tid or 0), "kind": kind, "msg_kind": msg_kind,
                                              "source_mid": source_mid, "sent_at": at, "status": "sent",
                                              "at": {"sent": at}, "error_code": 0})
    except Exception as e:                 # noqa: BLE001
        log.error("send not recorded: %s", type(e).__name__)
    return wamid_tail(wamid)


def bind_fallback_ack(store, source_mid: str, tid: int) -> None:
    """The 60s fallback ack went out before the ticket existed: link it to the ticket that now owns it."""
    for d in store.scan(SENDS_NS):
        if d.get("kind") == "ack" and not d.get("tid") and d.get("source_mid") == source_mid:
            store.update(SENDS_NS, d["wamid"], lambda x: {**x, "tid": int(tid)})


def _status_docs(payload: dict, phone_number_id: str) -> list[dict]:
    out = []
    for entry in (payload or {}).get("entry") or []:
        for ch in (entry or {}).get("changes") or []:
            v = (ch or {}).get("value") or {}
            if phone_number_id and str((v.get("metadata") or {}).get("phone_number_id") or "") != str(phone_number_id):
                continue
            out += [s for s in v.get("statuses") or [] if isinstance(s, dict)]
    return out


def apply_status(store, st: dict, now=None) -> dict | None:
    """Apply ONE Meta status to the matching send. None when it is unknown or changes nothing (so a status seen twice is
    logged once). Returns the changed doc (+ ``_changed``) otherwise."""
    wamid, status = str(st.get("id") or ""), str(st.get("status") or "")
    if not wamid or status not in ("sent", "delivered", "read", "failed"):
        return None
    when = _iso(_ts(st.get("timestamp")) or now)
    changed: list[bool] = []

    def fn(d: dict) -> dict:
        cur = d.get("status")
        if status == "failed":
            if cur == "failed":
                return d
            err = next((e for e in st.get("errors") or [] if isinstance(e, dict)), {})
            code = err.get("code")
            d.update(status="failed", error_code=int(code) if isinstance(code, int) else 0)
        elif cur == "failed" or RANK[status] <= RANK.get(cur, 0):
            return d
        else:
            d["status"] = status
        d["at"] = {**d.get("at", {}), status: when}
        changed.append(True)
        return d

    doc = store.update(SENDS_NS, wamid, fn)
    return {**doc, "wamid_tail": wamid_tail(wamid)} if doc is not None and changed else None


def _ts(raw):
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def ingest_statuses(store, payload: dict, phone_number_id: str, now=None) -> int:
    """Store every status Meta sent for OUR number against the send it belongs to; one log line per real change."""
    n = 0
    for st in _status_docs(payload, phone_number_id):
        doc = apply_status(store, st, now)
        if doc is None:
            continue
        n += 1
        event("delivery_status", tid=doc.get("tid") or "-", kind=doc.get("kind"), status=doc.get("status"),
              error_code=doc.get("error_code") or None, wamid=doc["wamid_tail"])
    return n


def sends_for(store) -> dict[int, list[dict]]:
    """Every recorded send, grouped by ticket id, oldest first."""
    out: dict[int, list[dict]] = {}
    for d in store.scan(SENDS_NS):
        out.setdefault(int(d.get("tid") or 0), []).append(d)
    for rows in out.values():
        rows.sort(key=lambda r: r.get("sent_at", ""))
    return out


def last_delivery(rows: list[dict]) -> dict | None:
    """The newest send's current state: {kind, status, at} (``at`` = when it reached that status)."""
    if not rows:
        return None
    r = rows[-1]
    return {"kind": r["kind"], "status": r["status"], "at": r["at"].get(r["status"]) or r["sent_at"]}
