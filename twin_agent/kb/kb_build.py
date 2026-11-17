"""Build kb.db (SQLite FTS5) from Principal's handover data. Every doc is redacted BEFORE it is inserted (red-team #11).

Sources: the runbook (one doc per entry + a `runbook` table), memory notes, triage method + steering files,
Telegram group Q -> A pairs, exported emails, and the persona rules. The database is built in a temp file and
swapped in at the end, so a worker reading the old kb.db never sees a half-built one.

CLI (python -I aa.py kb-build):
    kb-build --handover <dir> [--principal-tg-id N ...] [--db <kb.db>]
    kb-build                       # uses config kb_sources + people.principal_tg_user_id
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths, persona
from ..redact import scan
from . import loaders

# Generic Acme mailboxes the worker may need to name; every other address is personal data.
from .. import profile as _profile
KEEP_EMAILS = {_profile.get("helpdesk_email")}


@dataclass
class KbSources:
    runbook: Path
    triage_dir: Path
    memory_dirs: list[Path] = field(default_factory=list)
    telegram_exports: list[Path] = field(default_factory=list)
    email_dir: Path | None = None
    steering_dirs: list[Path] = field(default_factory=list)   # extra folders searched for */steering.md
    principal_tg_ids: list[str] = field(default_factory=list)     # "user<id>" senders that are Principal in the export


@dataclass
class BuildReport:
    docs: int
    redactions: int
    by_kind: dict[str, int] = field(default_factory=dict)            # docs per kind
    redactions_by_kind: dict[str, int] = field(default_factory=dict)  # findings per redact kind
    runbook_status: dict[str, int] = field(default_factory=dict)
    telegram: list[dict] = field(default_factory=list)


SCHEMA = """
CREATE TABLE docs (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, doc_id TEXT UNIQUE NOT NULL, title TEXT,
                   body TEXT, status TEXT, date TEXT, source TEXT);
CREATE VIRTUAL TABLE docs_fts USING fts5(title, body, content='docs', content_rowid='id',
                                         tokenize='unicode61 remove_diacritics 2');
CREATE TABLE runbook (slug TEXT PRIMARY KEY, title TEXT, signature TEXT, status TEXT, status_raw TEXT,
                      confirmed_date TEXT, tags TEXT, fix TEXT, root_cause TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


# ---------------------------------------------------------------- runbook


def slugify(title: str) -> str:
    """'Stray IN line (orphan ...)' -> 'stray-in-line'."""
    head = re.split(r"[(:]", title, 1)[0]
    s = re.sub(r"[^a-z0-9]+", "-", head.lower()).strip("-")
    return s[:60].strip("-") or "entry"


def normalize_status(raw: str) -> str:
    """Runbook status text -> confirmed | provisional | open | pending.

    'confirmed' = Principal confirmed the fix worked: the raw status starts with 'confirmed', 'resolved' or 'fixed'.
    Everything else stays unconfirmed, so it can never make a packet verified (05 3.2). 'root cause confirmed'
    is a diagnosis, not a confirmed fix: it is 'pending'.
    """
    s = (raw or "").strip().lower()
    if re.match(r"(confirmed|resolved|fixed)\b", s) and not s.startswith("fixed per"):
        return "confirmed"
    if s.startswith("provisional"):
        return "provisional"
    if s.startswith("open"):
        return "open"
    return "pending"


def _field(body: str, name: str) -> str:
    m = re.search(rf"\*\*{name}:\*\*\s*(.*?)(?=\n- \*\*|\Z)", body, re.S)
    return " ".join(m.group(1).split()) if m else ""


def _status(body: str) -> str:
    """'**Status:** x' (template) or a plain 'Status: x.' inside the First seen line."""
    raw = _field(body, "Status")
    if not raw:
        m = re.search(r"\bStatus:\s*([^\n]*)", body)
        raw = m.group(1) if m else ""
    raw = re.split(r"\s+·\s+", raw)[0]
    return raw.strip().strip("*").strip()


def parse_runbook(text: str) -> list[dict]:
    """Split the runbook into entries: '### title' blocks with '- **Signature:**' ... '**Status:**' fields.

    Returns [{slug, title, signature, status, status_raw, confirmed_date, tags, fix, root_cause, body}].
    Template placeholders ('### <short symptom title>') are skipped; duplicate slugs get -2, -3 ...
    """
    entries, seen = [], {}
    blocks = re.split(r"^### +", text, flags=re.M)[1:]
    for block in blocks:
        title, _, body = block.partition("\n")
        body = re.split(r"^#{1,2} +", body, maxsplit=1, flags=re.M)[0]   # stop at the next section heading
        title = title.strip()
        if not title or title.startswith("<"):
            continue
        status_raw = _status(body)
        slug = slugify(title)
        seen[slug] = seen.get(slug, 0) + 1
        if seen[slug] > 1:
            slug = f"{slug}-{seen[slug]}"
        status = normalize_status(status_raw)
        date = re.search(r"\d{4}-\d{2}-\d{2}", status_raw)
        entries.append({
            "slug": slug, "title": title, "signature": _field(body, "Signature"), "status": status,
            "status_raw": status_raw, "confirmed_date": date.group(0) if (date and status == "confirmed") else "",
            "tags": _field(body, "Tags"), "fix": _field(body, "Fix"), "root_cause": _field(body, "Root cause"),
            "body": body.strip(),
        })
    return entries


# ---------------------------------------------------------------- redaction


# Leftovers the shared scanner misses in this export: key prefixes ("kc_live_5dgL...") and phone numbers inside
# URL-encoded tel: links ("+20%2010%2012345678"). Applied on top of redact.scan.
_KB_EXTRA = [("kc_key", re.compile(r"\bkc_(?:live|test)_\S*")),
             ("phone", re.compile(r"\+\d{1,3}(?:%20|[\s-])?\d{2,4}(?:%20|[\s-])?\d{3,4}(?:%20|[\s-])?\d{3,4}"))]


def redact_doc_text(text: str, counts: dict[str, int]) -> str:
    """redact.scan + the kb extras; replace every finding except the generic Acme help address."""
    found = [(f.start, f.end, f.kind) for f in scan(text)]
    for kind, rx in _KB_EXTRA:
        for m in rx.finditer(text):
            if not any(s < m.end() and m.start() < e for s, e, _ in found):
                found.append((m.start(), m.end(), kind))
    out, pos = [], 0
    for start, end, kind in sorted(found):
        if kind == "email" and text[start:end].lower() in KEEP_EMAILS:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        out.append(text[pos:start])
        out.append(f"[REDACTED:{kind}]")
        pos = end
    out.append(text[pos:])
    return "".join(out)


# ---------------------------------------------------------------- build


def collect(sources: KbSources) -> tuple[list[dict], list[dict], list[dict]]:
    """All docs (unredacted), runbook entries, telegram stats."""
    docs: list[dict] = []
    entries: list[dict] = []
    if sources.runbook and Path(sources.runbook).is_file():
        entries = parse_runbook(loaders.read_text(sources.runbook))
        for e in entries:
            docs.append({"kind": "runbook", "doc_id": f"runbook:{e['slug']}", "title": e["title"],
                         "body": loaders.clean(f"{e['title']}\n{e['body']}"), "status": e["status"],
                         "date": e["confirmed_date"], "source": "ODOO-RUNBOOK.md"})
    if sources.triage_dir and Path(sources.triage_dir).is_dir():
        docs += loaders.triage_docs(Path(sources.triage_dir), list(sources.steering_dirs))
    docs += loaders.memory_docs([Path(d) for d in sources.memory_dirs if Path(d).is_dir()])
    if sources.email_dir and Path(sources.email_dir).is_dir():
        docs += loaders.email_docs(Path(sources.email_dir))
    stats = []
    for exp in sources.telegram_exports:
        if Path(exp).is_file():
            tg, st = loaders.telegram_docs(Path(exp), {_as_user_id(i) for i in sources.principal_tg_ids})
            docs += tg
            stats.append(st)
    docs += [{"kind": "persona", "status": "", "date": "", "source": "persona.py", **d} for d in persona.persona_docs()]
    return docs, entries, stats


def _as_user_id(v) -> str:
    v = str(v).strip()
    return v if v.startswith("user") else f"user{v}"


def build(sources: KbSources, db_path: Path) -> BuildReport:
    """Build kb.db at db_path. Redacts title and body of every doc and every runbook field before insert."""
    docs, entries, stats = collect(sources)
    counts: dict[str, int] = {}
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    try:
        con.executescript(SCHEMA)
        by_kind: dict[str, int] = {}
        for d in docs:
            title = redact_doc_text(d["title"], counts)
            body = redact_doc_text(d["body"], counts)
            cur = con.execute("INSERT OR IGNORE INTO docs(kind, doc_id, title, body, status, date, source) "
                              "VALUES (?,?,?,?,?,?,?)",
                              (d["kind"], d["doc_id"], title, body, d["status"], d["date"], d["source"]))
            if cur.rowcount:
                con.execute("INSERT INTO docs_fts(rowid, title, body) VALUES (?,?,?)", (cur.lastrowid, title, body))
                by_kind[d["kind"]] = by_kind.get(d["kind"], 0) + 1
        scratch: dict[str, int] = {}   # runbook fields repeat text already counted in the docs
        for e in entries:
            con.execute("INSERT INTO runbook VALUES (?,?,?,?,?,?,?,?,?)",
                        (e["slug"], redact_doc_text(e["title"], scratch), redact_doc_text(e["signature"], scratch),
                         e["status"], redact_doc_text(e["status_raw"], scratch), e["confirmed_date"],
                         e["tags"], redact_doc_text(e["fix"], scratch), redact_doc_text(e["root_cause"], scratch)))
        status_counts: dict[str, int] = {}
        for e in entries:
            status_counts[e["status"]] = status_counts.get(e["status"], 0) + 1
        report = BuildReport(docs=sum(by_kind.values()), redactions=sum(counts.values()), by_kind=by_kind,
                             redactions_by_kind=counts, runbook_status=status_counts, telegram=stats)
        con.execute("INSERT INTO meta VALUES ('report', ?)", (json.dumps(report.__dict__),))
        con.commit()
    finally:
        con.close()
    os.replace(tmp, db_path)
    return report


# ---------------------------------------------------------------- CLI


# Only allow-listed chats may reach kb.db (red-team #18): kb_search feeds Claude. "Saudi Setup" stays out until
# Principal says yes (config chats[].read: false); add it with --telegram-export when he does.
DEFAULT_EXPORTS = ("odoo-x-client",)


def sources_from_handover(root: Path, principal_ids: list[str], exports: list[Path] | None = None) -> KbSources:
    """Standard layout of Principal's handover export folder. Telegram: odoo-x-client only unless exports is given."""
    root = Path(root)
    if exports is None:
        exports = [root / "telegram" / name / "result.json" for name in DEFAULT_EXPORTS]
    return KbSources(
        runbook=root / "triage" / "ODOO-RUNBOOK.md", triage_dir=root / "triage",
        memory_dirs=sorted(p for p in root.glob("sessions/*/memory") if p.is_dir()),
        telegram_exports=[Path(e) for e in exports], email_dir=root / "email",
        steering_dirs=sorted(p for p in root.glob("projects/*/.claude/skills") if p.is_dir()),
        principal_tg_ids=list(principal_ids))


def _export_allowed(path, cfg: dict) -> bool:
    """A config export is indexed only if its chat is in config chats[] with read: true (red-team #18)."""
    try:
        name = json.loads(Path(path).read_text(encoding="utf-8")).get("name")
    except (OSError, ValueError):
        return False
    return any(c.get("name") == name and c.get("read") for c in cfg.get("chats") or [])


def sources_from_config(cfg: dict) -> KbSources:
    ks = cfg.get("kb_sources") or {}
    principal = (cfg.get("people") or {}).get("principal_tg_user_id") or 0
    return KbSources(runbook=Path(ks.get("runbook") or ""), triage_dir=Path(ks.get("triage_dir") or ""),
                     memory_dirs=[Path(p) for p in ks.get("memory_dirs") or []],
                     telegram_exports=[Path(p) for p in ks.get("telegram_exports") or [] if _export_allowed(p, cfg)],
                     email_dir=Path(ks["email_dir"]) if ks.get("email_dir") else None,
                     steering_dirs=[Path(p) for p in ks.get("steering_dirs") or []],
                     principal_tg_ids=[str(principal)] if principal else [])


def _load_cfg() -> dict:
    try:
        from ..config import load_config
        return load_config()
    except Exception:   # CORE config not ready or no file: plain YAML read
        import yaml
        p = paths.config_dir() / "config.yaml"
        return yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kb-build", description="Build kb.db from Principal's handover data")
    ap.add_argument("--handover", type=Path, help="handover export folder (triage/, sessions/, telegram/, email/)")
    ap.add_argument("--principal-tg-id", action="append", default=[], help="Principal's Telegram user id (repeatable)")
    ap.add_argument("--telegram-export", action="append", type=Path, default=None,
                    help="Telegram result.json to index (repeatable; replaces the odoo-x-client default)")
    ap.add_argument("--db", type=Path, default=None, help="output kb.db (default: home/data/kb/kb.db)")
    a = ap.parse_args(argv)
    if a.handover:
        src = sources_from_handover(a.handover, a.principal_tg_id, a.telegram_export)
    else:
        src = sources_from_config(_load_cfg())
        src.principal_tg_ids += a.principal_tg_id
    rep = build(src, a.db or paths.kb_db())
    print(f"kb.db built: {rep.docs} docs, {rep.redactions} redactions")
    print("docs by kind: " + ", ".join(f"{k}={v}" for k, v in sorted(rep.by_kind.items())))
    print("redactions by kind: " + ", ".join(f"{k}={v}" for k, v in sorted(rep.redactions_by_kind.items())))
    print("runbook status: " + ", ".join(f"{k}={v}" for k, v in sorted(rep.runbook_status.items())))
    for st in rep.telegram:
        print(f"telegram: {st['messages']} messages, {st['principal_messages']} from Principal, {st['pairs']} Q->A pairs")
    return 0
