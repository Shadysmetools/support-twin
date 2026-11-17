"""Bootstrap the fingerprint offline from the handover (read-only): the allow-listed Telegram export + emails.

    python -I aa.py watcher bootstrap --handover <handover dir> --principal-tg-id N [--principal-email ADDR]
                                      [--out data/watcher/bootstrap] [--docs docs/FINGERPRINT-BOOTSTRAP.md]

- Reads ONLY `telegram/odoo-x-client/result.json` (Saudi Setup is read: false) and `email/*.txt`. Never writes there.
- Principal = `user<id>` from --principal-tg-id, plus the group posting as anonymous admin (`channel<id>`), plus mail From
  his address. Ids and addresses come from the command line, never from the code.
- Raw-derived files (cleaned cases, full fingerprint with phrase bank) go under `--out` (in data/, gitignored).
- The docs file gets the de-identified view only, and is written only if `leak_hits` finds nothing: no
  redact finding, no part of any sender / recipient name, no Telegram user or chat id.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from email.utils import getaddresses
from pathlib import Path

from ..clock import CAIRO
from ..kb.loaders import read_text, tg_text
from ..models import utc_iso
from ..redact import scan
from .analyse import render_md
from .bundle import Pseudonymiser, clean_record, load_salt
from .fingerprint import build_fingerprint

EXPORT = Path("telegram") / "odoo-x-client" / "result.json"
QUOTE = re.compile(r"(?m)^(?:On .{5,200}wrote:|>|-{2,} ?Original Message|From: .+<[^>]+>)")
# His signature (name, job title, phone) starts at the sign-off: cut there so it never reaches the stats.
SIGNOFF = re.compile(r"(?im)^\s*[*_]*(?:best regards|kind regards|regards|thanks (?:&|and) regards|br)[ ,.!*_]*$|^--\s*$")


def _when(m: dict) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(m["date_unixtime"]), tz=timezone.utc).astimezone(CAIRO)
    except (KeyError, ValueError, TypeError):
        return None


def telegram_cases(export: Path, principal_ids: set[str]) -> tuple[list[dict], set[str], set[str]]:
    """Cases grouped by (requester, Cairo day). Returns (cases, display names seen, raw ids seen)."""
    data = json.loads(read_text(export))
    me = set(principal_ids) | {f"channel{str(data.get('id', '')).removeprefix('-100')}"}
    msgs = [m for m in data.get("messages", []) if m.get("type") == "message"]
    by_id = {m["id"]: m for m in msgs}
    names = {str(m.get("from") or "") for m in msgs if m.get("from")}
    ids = {str(m.get("from_id") or "") for m in msgs if m.get("from_id")} | {str(data.get("id", ""))}
    cases: dict[tuple[str, str], dict] = {}
    last_other: dict | None = None
    for m in msgs:
        if m.get("from_id") not in me:
            last_other = m
            continue
        text = tg_text(m).strip()
        if not text:
            continue
        q = by_id.get(m.get("reply_to_message_id"))
        q = q if q and q.get("from_id") not in me else last_other
        ts, qts = _when(m), _when(q) if q else None
        requester = str(q.get("from_id")) if q else "unknown"
        key = (requester, ts.date().isoformat() if ts else "")
        case = cases.setdefault(key, {"request_id": f"BOOT-TG-{len(cases) + 1:04d}", "requester": requester,
                                      "channel": "telegram_group", "records": []})
        case["records"].append({"kind": "own_message", "request_id": case["request_id"], "ts": utc_iso(ts) if ts else None,
                                "channel": "telegram_group", "text": text, "requester": requester,
                                "reply_minutes": round((ts - qts).total_seconds() / 60, 1) if ts and qts and ts >= qts
                                else None, "hour": ts.hour if ts else None})
    return list(cases.values()), names, ids


def email_cases(email_dir: Path, principal_email: str) -> tuple[list[dict], set[str]]:
    cases, names = [], set()
    if not principal_email or not Path(email_dir).is_dir():
        return cases, names
    for p in sorted(Path(email_dir).glob("*.txt")):
        head, _, body = read_text(p).partition("--- BODY ---")
        hdr = {k.strip(): v.strip() for k, _, v in (l.partition(":") for l in head.splitlines()) if _}
        people = getaddresses([hdr.get(k, "") for k in ("From", "To", "Cc")])
        names |= {n for n, _ in people if n}
        if principal_email.lower() not in hdr.get("From", "").lower():
            continue
        text = SIGNOFF.split(QUOTE.split(body.strip(), maxsplit=1)[0], maxsplit=1)[0].strip()
        if not text:
            continue
        to = getaddresses([hdr.get("To", "")])
        rid = f"BOOT-EM-{len(cases) + 1:04d}"
        requester = to[0][1].lower() if to else "unknown"
        cases.append({"request_id": rid, "requester": requester, "channel": "email",
                      "records": [{"kind": "own_message", "request_id": rid, "ts": None, "channel": "email",
                                   "text": text[:3000], "requester": requester, "reply_minutes": None, "hour": None}]})
    return cases, names


def name_parts(names: set[str], allow: set[str] = frozenset()) -> set[str]:
    out = set()
    for n in names:
        out |= {p.lower() for p in re.split(r"[\s|,._()<>@\"'-]+", n) if len(p) >= 3 and not p.isdigit()}
    return out - {a.lower() for a in allow}


def leak_hits(text: str, parts: set[str], ids: set[str]) -> list[str]:
    """Everything in `text` that could identify a person: redact findings, name parts, raw ids."""
    hits = [f"redact:{f.kind}" for f in scan(text)]
    low = text.lower()
    hits += [f"name:{p[:2]}..." for p in parts if re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", low)]
    hits += ["id" for i in ids if i and re.sub(r"\D", "", i) and re.sub(r"\D", "", i) in text]
    return hits


def run(handover: Path, principal_tg_ids: set[str], *, principal_email: str = "", out: Path, docs: Path | None,
        salt: bytes | None = None) -> dict:
    handover = Path(handover)
    cases, names, ids = telegram_cases(handover / EXPORT, principal_tg_ids)
    ecases, enames = email_cases(handover / "email", principal_email)
    names |= enames
    pseudo = Pseudonymiser(salt or load_salt(), names)
    clean = []
    for c in cases + ecases:
        recs = [r for r in (clean_record(r, pseudo) for r in c["records"]) if r]
        if recs:
            clean.append({"request_id": c["request_id"], "requester": pseudo.alias(c["requester"], "requester"),
                          "channel": c["channel"], "records": recs})
    fp = build_fingerprint(clean)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "cases.jsonl").write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in clean) + "\n", encoding="utf-8")
    (out / "fingerprint.json").write_text(json.dumps(fp, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "FINGERPRINT.md").write_text(render_md(fp, title="Principal's work fingerprint (bootstrap, private)"),
                                        encoding="utf-8")
    parts = name_parts(names, allow={"principal"})
    result = {"cases": len(clean), "messages": fp["messages"], "docs_written": False, "leaks": 0}
    if docs is not None:
        public = render_md(fp, public=True, blocked=parts | {"principal"},
                           title="Principal's work fingerprint: bootstrap from the handover (de-identified stats)")
        hits = leak_hits(public, parts, ids)
        result["leaks"] = len(hits)
        if not hits:
            Path(docs).parent.mkdir(parents=True, exist_ok=True)
            Path(docs).write_text(public, encoding="utf-8")
            result["docs_written"] = True
    return result
