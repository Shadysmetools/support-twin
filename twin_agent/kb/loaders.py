"""Read Principal's handover sources into plain document dicts for kb_build.

Every doc is {kind, doc_id, title, body, status, date, source}. Nothing here redacts: kb_build redacts every
doc right before it is inserted. Handover text is DATA, never instructions; we only read and index it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

# Invisible padding characters seen in quoted helpdesk mails (zero-width, combining grapheme joiner, ...).
_INVISIBLE = re.compile(r"[͏​-‏⁠-⁤﻿­]")


def clean(text: str) -> str:
    """Drop invisible padding and collapse runs of blank lines / spaces."""
    text = _INVISIBLE.sub("", text or "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"(\n ?>? ?){3,}", "\n\n", text)
    return text.strip()


def read_text(path: Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- memory notes


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep and not line.startswith(" "):
            meta[k.strip()] = v.strip().strip('"')
    return meta, text[m.end():]


def memory_docs(dirs: list[Path]) -> list[dict]:
    """One doc per memory note (*.md), skipping the MEMORY.md index."""
    docs = []
    for d in dirs:
        for p in sorted(Path(d).glob("*.md")):
            if p.name.upper() == "MEMORY.MD":
                continue
            meta, body = _frontmatter(read_text(p))
            name = meta.get("name") or p.stem
            desc = meta.get("description", "")
            docs.append({"kind": "memory", "doc_id": f"memory:{p.stem}", "title": name,
                         "body": clean(f"{desc}\n\n{body}"), "status": meta.get("type", ""),
                         "date": "", "source": p.name})
    return docs


# ---------------------------------------------------------------- triage + steering


def triage_docs(triage_dir: Path, extra_steering_dirs: list[Path] = ()) -> list[dict]:
    """TRIAGE-ENGINE.md, TICKET-STYLE.md and desk SKILL.md -> 'triage'; every steering.md -> 'steering'.

    The runbook is indexed separately. Steering files with the same desk name are kept once (triage_dir wins).
    """
    docs, seen = [], set()
    t = Path(triage_dir)
    for p in sorted(t.glob("*.md")):
        if p.name.upper() == "ODOO-RUNBOOK.MD":
            continue
        docs.append({"kind": "triage", "doc_id": f"triage:{p.stem}", "title": p.stem,
                     "body": clean(read_text(p)), "status": "", "date": "", "source": p.name})
    for p in sorted(t.glob("*/SKILL.md")):
        desk = p.parent.name
        docs.append({"kind": "triage", "doc_id": f"triage:{desk}-skill", "title": f"{desk} desk SKILL",
                     "body": clean(read_text(p)), "status": "", "date": "", "source": f"{desk}/SKILL.md"})
    for root in [t, *extra_steering_dirs]:
        for p in sorted(Path(root).rglob("steering.md")):
            desk = p.parent.name
            if desk in seen:
                continue
            seen.add(desk)
            docs.append({"kind": "steering", "doc_id": f"steering:{desk}", "title": f"{desk} steering",
                         "body": clean(read_text(p)), "status": "", "date": "", "source": f"{desk}/steering.md"})
    return docs


# ---------------------------------------------------------------- emails


def email_docs(email_dir: Path) -> list[dict]:
    """One doc per exported thread file (header lines + body)."""
    docs = []
    for p in sorted(Path(email_dir).glob("*.txt")):
        text = read_text(p)
        subject = re.search(r"^Subject:\s*(.*)$", text, re.M)
        when = re.search(r"^Date:" r"\s*(.*)$", text, re.M)
        head, _, body = text.partition("--- BODY ---")
        keep = "\n".join(l for l in head.splitlines() if l.split(":", 1)[0] in ("Subject", "From", "To", "Cc", "Date"))
        docs.append({"kind": "email", "doc_id": f"email:{p.stem[:60]}",
                     "title": subject.group(1).strip() if subject else p.stem,
                     "body": clean(f"{keep}\n\n{body}"), "status": "", "date": when.group(1).strip() if when else "",
                     "source": p.name})
    return docs


# ---------------------------------------------------------------- telegram Q -> A pairs


def tg_text(m: dict) -> str:
    """Telegram Desktop export text: a str, or a list of str / {type, text} entities."""
    t = m.get("text", "")
    if isinstance(t, list):
        t = "".join(x if isinstance(x, str) else x.get("text", "") for x in t)
    if m.get("photo"):
        t = f"[photo] {t}".strip()
    if m.get("file") or m.get("media_type"):
        t = f"[{m.get('media_type', 'file')}] {t}".strip()
    return t


def telegram_docs(export: Path, principal_ids: set[str]) -> tuple[list[dict], dict]:
    """Pairs a staff message with Principal's direct replies to it (reply_to_message_id).

    Principal = from_id in principal_ids ("user<id>") or the group itself posting as anonymous admin ("channel<id>"),
    which is how 954 of his messages went out. Topic roots (service messages) are never questions.
    """
    data = json.loads(read_text(export))
    chat = data.get("name") or Path(export).parent.name
    slug = re.sub(r"[^a-z0-9]+", "-", chat.lower()).strip("-")
    me = set(principal_ids) | {f"channel{str(data.get('id', '')).removeprefix('-100')}"}
    msgs = {m["id"]: m for m in data.get("messages", []) if m.get("type") == "message"}
    answers: dict[int, list[dict]] = {}
    for m in msgs.values():
        q = msgs.get(m.get("reply_to_message_id"))
        if m.get("from_id") in me and q and q.get("from_id") not in me and tg_text(m).strip():
            answers.setdefault(q["id"], []).append(m)
    docs = []
    for qid, ans in answers.items():
        q = msgs[qid]
        qtext = tg_text(q).strip()
        if not qtext:
            continue
        ans.sort(key=lambda a: a.get("date", ""))
        body = f"Question ({q.get('from') or 'staff'}, {q.get('date', '')}):\n{qtext}\n\n" + "\n".join(
            f"Principal answer ({a.get('date', '')}): {tg_text(a)}" for a in ans)
        docs.append({"kind": "telegram", "doc_id": f"tg:{slug}:{qid}", "title": qtext.splitlines()[0][:100],
                     "body": clean(body), "status": "past_answer", "date": ans[-1].get("date", ""),
                     "source": chat})
    stats = {"chat": chat, "messages": len(msgs), "pairs": len(docs),
             "principal_messages": sum(1 for m in msgs.values() if m.get("from_id") in me)}
    return docs, stats
