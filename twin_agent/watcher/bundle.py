"""Turn captured cases into redacted, pseudonymised case bundles (jsonl + md) in the outbox.

Every text passes `redact.redact` (secrets, tokens, phones, emails, vault values), then the pseudonymiser swaps
every known person name (requesters, staff, Principal, backups) for a stable alias like `person-3f2a`. A record that
still shows a finding after that is dropped, never written. Requesters become `requester-xxxx`; chat names are
replaced by the company code. The alias salt stays on the laptop (`data/watcher/salt`), never in a bundle.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .. import paths
from ..redact import redact, scan

TEXT_KEYS = ("text", "draft", "final")


def salt_file() -> Path:
    return paths.data_dir() / "watcher" / "salt"


def load_salt(path: Path | None = None) -> bytes:
    p = path or salt_file()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_hex(16), encoding="utf-8")
    return p.read_text(encoding="utf-8").strip().encode()


class Pseudonymiser:
    """Stable aliases for people, and name scrubbing in free text (whole words, longest name first)."""

    def __init__(self, salt: bytes, names: Iterable[str] = ()):
        self.salt = salt
        parts: set[str] = set()
        for n in names:
            n = (n or "").strip()
            if not n:
                continue
            parts.add(n)
            parts.update(p for p in re.split(r"[\s|,._()-]+", n) if len(p) >= 3 and not p.isdigit())
        self.names = sorted(parts, key=len, reverse=True)
        self._rx = (re.compile(r"(?<!\w)(?:" + "|".join(re.escape(p) for p in self.names) + r")(?!\w)", re.I)
                    if self.names else None)

    def alias(self, value, kind: str = "person") -> str:
        if value in (None, "", 0, "0"):
            return f"{kind}-unknown"
        h = hmac.new(self.salt, str(value).strip().lower().encode(), hashlib.sha256).hexdigest()[:4]
        return f"{kind}-{h}"

    def scrub(self, text: str) -> str:
        if not text or self._rx is None:
            return text or ""
        return self._rx.sub(lambda m: self.alias(m.group(0)), text)


def known_names(db, cfg: dict) -> list[str]:
    """Every person name the agent has seen (state.db + config people)."""
    people = cfg.get("people") or {}
    names = [people.get("principal_name") or "", *(people.get("backup_names") or [])]
    try:
        names += [r["n"] for r in db._all("SELECT DISTINCT sender_name AS n FROM messages WHERE sender_name != ''")]
        names += [r["n"] for r in db._all("SELECT DISTINCT requester_name AS n FROM requests WHERE requester_name != ''")]
    except Exception:                                       # noqa: BLE001 - fewer names, still redacted
        pass
    return [n for n in names if n]


def clean_text(text: str, pseudo: Pseudonymiser, vault_hashes: Iterable[str] = ()) -> str:
    return pseudo.scrub(redact(str(text or ""), tuple(vault_hashes)))


def is_clean(text: str) -> bool:
    """The final guard before disk: no secret or personal-data finding left."""
    return not scan(text or "")


def _clean_value(v, pseudo, vh):
    if isinstance(v, str):
        return clean_text(v, pseudo, vh)
    if isinstance(v, list):
        return [_clean_value(x, pseudo, vh) for x in v]
    if isinstance(v, dict):
        return {k: _clean_value(x, pseudo, vh) for k, x in v.items()}
    return v


def clean_record(rec: dict, pseudo: Pseudonymiser, vault_hashes: Iterable[str] = ()) -> dict | None:
    """Redact + pseudonymise every string in a record; None if anything is still unsafe."""
    vh = tuple(vault_hashes)
    out = {}
    for k, v in rec.items():
        if k == "requester":
            out[k] = pseudo.alias(v, "requester")
        elif k in ("request_id", "kind", "ts", "channel", "action", "session_id", "project", "started", "ended"):
            out[k] = v
        else:
            out[k] = _clean_value(v, pseudo, vh)
    blob = json.dumps(out, ensure_ascii=False)
    return out if is_clean(blob) else None


@dataclass
class ExportReport:
    cases: int = 0
    records: int = 0
    dropped: int = 0
    files: list[str] = field(default_factory=list)


def render_case_md(case: dict) -> str:
    lines = [f"# {case['request_id']} ({case.get('lane') or '-'}, {case.get('tier') or '-'}, "
             f"{case.get('company') or '-'}, {case.get('channel') or '-'})", "",
             f"Requester: {case.get('requester')}. State: {case.get('state')}.", ""]
    for r in case["records"]:
        head = f"- {r.get('ts') or ''} **{r['kind']}**"
        if r["kind"] == "tap":
            lines.append(f"{head}: {r.get('action')}")
        elif r["kind"] in ("own_message", "instruction", "shell_paste"):
            extra = f" (after {r['reply_minutes']} min)" if r.get("reply_minutes") is not None else ""
            lines.append(f"{head}{extra}: {str(r.get('text') or '')[:400]}")
        elif r["kind"] == "edit":
            lines.append(f"{head}: similarity {r.get('similarity')}, unchanged={r.get('unchanged')}")
            lines.append(f"  - draft: {str(r.get('draft') or '')[:400]}")
            lines.append(f"  - final: {str(r.get('final') or '')[:400]}")
        elif r["kind"] == "desk_session":
            lines.append(f"{head}: {len(r.get('turns') or [])} turns, {r.get('minutes')} min")
        else:
            lines.append(f"{head}: " + ", ".join(f"{k}={v}" for k, v in r.items()
                                                 if k not in ("kind", "ts", "request_id")))
    return "\n".join(lines) + "\n"


def export_cases(cases: list[dict], outbox: Path, pseudo: Pseudonymiser,
                 vault_hashes: Iterable[str] = ()) -> ExportReport:
    """Write cases/<REQ>.jsonl + cases/<REQ>.md (overwritten each export, so a bundle is always the latest view)."""
    rep = ExportReport()
    d = Path(outbox) / "cases"
    d.mkdir(parents=True, exist_ok=True)
    for case in cases:
        recs = []
        for r in case["records"]:
            c = clean_record(r, pseudo, vault_hashes)
            if c is None:
                rep.dropped += 1
            else:
                recs.append(c)
        if not recs:
            continue
        head = {k: case.get(k) for k in ("request_id", "lane", "tier", "company", "channel", "state")}
        head["requester"] = pseudo.alias(case.get("requester"), "requester")
        safe = {**head, "records": recs}
        jl = "\n".join(json.dumps(x, ensure_ascii=False) for x in [{"case": head}, *recs]) + "\n"
        md = render_case_md(safe)
        if not (is_clean(jl) and is_clean(md)):
            rep.dropped += len(recs)
            continue
        rid = re.sub(r"[^A-Za-z0-9-]", "_", case["request_id"])
        (d / f"{rid}.jsonl").write_text(jl, encoding="utf-8")
        (d / f"{rid}.md").write_text(md, encoding="utf-8")
        rep.cases += 1
        rep.records += len(recs)
        rep.files += [f"cases/{rid}.jsonl", f"cases/{rid}.md"]
    return rep


def read_bundle(path: Path) -> dict:
    """One cases/<REQ>.jsonl back into {case fields..., records: [...]}."""
    head, recs = {}, []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        if "case" in obj:
            head = obj["case"]
        else:
            recs.append(obj)
    return {**head, "records": recs}
