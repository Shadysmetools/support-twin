"""kb_build + kb_search on synthetic sources only (CONTRACT 12). Red-team #11: redact before indexing."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from twin_agent.kb import kb_build, kb_search
from twin_agent.kb.kb_build import KbSources, build, normalize_status, parse_runbook
from twin_agent.testing.fakes import FIXTURES

FAKE_TOKEN = "Zq9xK2mP4vB7nR1tY6wE3uI8oA5sD0fG"   # synthetic, not a real token


def _sources(tmp: Path) -> KbSources:
    triage = tmp / "triage"
    (triage / "telegram-triage").mkdir(parents=True)
    (triage / "TRIAGE-ENGINE.md").write_text("# Triage engine\nRunbook first, both-sources gate.", encoding="utf-8")
    (triage / "telegram-triage" / "SKILL.md").write_text("telegram desk skill", encoding="utf-8")
    (triage / "telegram-triage" / "steering.md").write_text("Rakam el batch kam heuristic", encoding="utf-8")
    runbook = triage / "ODOO-RUNBOOK.md"
    runbook.write_text((FIXTURES / "runbook_small.md").read_text(encoding="utf-8"), encoding="utf-8")
    extra = tmp / "skills" / "auditor"
    extra.mkdir(parents=True)
    (extra / "steering.md").write_text("auditor steering note", encoding="utf-8")
    dup = tmp / "skills" / "telegram-triage"
    dup.mkdir(parents=True)
    (dup / "steering.md").write_text("older copy, must be skipped", encoding="utf-8")
    mem = tmp / "memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("- index only", encoding="utf-8")
    (mem / "no-em-dash.md").write_text('---\nname: no-em-dash\ndescription: "Never use the em dash"\n'
                                       "metadata:\n  type: feedback\n---\nUse a hyphen instead.", encoding="utf-8")
    emails = tmp / "email"
    emails.mkdir()
    (emails / "01-test.txt").write_text(
        "Subject: Test Ticket Thread\nFrom: Staff A <staff.a@example.com>\nDate: Sun, 1 Nov 2026\n--- BODY ---\n"
        f"View https://helpdesk.example.com/my/ticket/2641/{FAKE_TOKEN} or mail help@example.com\n"
        "Call +20 100 000 0000 please​͏\n", encoding="utf-8")
    export = tmp / "export.json"
    export.write_text(json.dumps({"name": "Test Group", "id": 500, "messages": [
        {"id": 7, "type": "service", "action": "topic_created", "date": "2026-10-28T10:00:00"},
        {"id": 10, "type": "message", "date": "2026-10-28T10:01:00", "from": "Staff A", "from_id": "user2001",
         "text": [{"type": "plain", "text": "WAVE/00001 cannot validate"}], "reply_to_message_id": 7},
        {"id": 11, "type": "message", "date": "2026-10-28T10:05:00", "from": "Principal Test", "from_id": "user1001",
         "text": "Send the validation error please", "reply_to_message_id": 10},
        {"id": 12, "type": "message", "date": "2026-10-28T10:06:00", "from": "Test Group", "from_id": "channel500",
         "text": "Try again now.", "reply_to_message_id": 10},
        {"id": 13, "type": "message", "date": "2026-10-28T10:07:00", "from": "Principal Test", "from_id": "user1001",
         "text": "topic reply, not an answer", "reply_to_message_id": 7},
        {"id": 14, "type": "message", "date": "2026-10-28T10:08:00", "from": "Staff B", "from_id": "user2002",
         "text": "no answer for me"},
    ]}), encoding="utf-8")
    return KbSources(runbook=runbook, triage_dir=triage, memory_dirs=[mem], telegram_exports=[export],
                     email_dir=emails, steering_dirs=[tmp / "skills"], principal_tg_ids=["1001"])


@pytest.fixture
def kb(tmp_path):
    db = tmp_path / "data" / "kb" / "kb.db"
    rep = build(_sources(tmp_path / "src"), db)
    return db, rep


def test_parse_runbook_fixture():
    entries = parse_runbook((FIXTURES / "runbook_small.md").read_text(encoding="utf-8"))
    assert [e["slug"] for e in entries] == ["stray-in-line", "phantom-reservation-after-a-cancelled-put-away"]
    stray = entries[0]
    assert stray["status"] == "confirmed" and stray["confirmed_date"] == "2026-07-26"
    assert "Purchase Order reference" in stray["signature"]
    assert entries[1]["status"] == "provisional" and entries[1]["confirmed_date"] == ""


def test_parse_runbook_skips_template_and_dedupes_slugs():
    text = ("## Entry template\n### <short symptom title>\n- **Status:** x\n## Entries\n"
            "### Same thing (a)\n- First seen: 2026-01-01. Status: fix handed over.\n"
            "### Same thing (b)\n- **Status:** **CONFIRMED FIXED 2026-08-20.** done\n")
    entries = parse_runbook(text)
    assert [e["slug"] for e in entries] == ["same-thing", "same-thing-2"]
    assert entries[0]["status_raw"].startswith("fix handed over") and entries[0]["status"] == "pending"
    assert entries[1]["status"] == "confirmed" and entries[1]["confirmed_date"] == "2026-08-20"


@pytest.mark.parametrize("raw,want", [
    ("confirmed 2026-07-26", "confirmed"), ("resolved 2026-07-22 by unticking", "confirmed"),
    ("provisional (drafted, unconfirmed)", "provisional"), ("open, no guard", "open"),
    ("root cause CONFIRMED 2026-08-31", "pending"), ("data-fixed per instance", "pending"), ("", "pending"),
])
def test_normalize_status_is_conservative(raw, want):
    assert normalize_status(raw) == want


def test_build_counts_by_kind(kb):
    _, rep = kb
    assert rep.by_kind == {"runbook": 2, "triage": 2, "steering": 2, "memory": 1, "email": 1, "telegram": 1,
                           "persona": 4}
    assert rep.docs == sum(rep.by_kind.values())
    assert rep.runbook_status == {"confirmed": 1, "provisional": 1}
    assert rep.telegram[0]["pairs"] == 1


def test_secrets_and_phones_redacted_before_indexing(kb):
    db, rep = kb
    con = sqlite3.connect(db)
    everything = " ".join(t + " " + b for t, b in con.execute("SELECT title, body FROM docs"))
    fts = " ".join(b for (b,) in con.execute("SELECT body FROM docs_fts"))
    for text in (everything, fts):
        assert FAKE_TOKEN not in text
        assert "100 000 0000" not in text
        assert "staff.a@example.com" not in text
        assert "help@example.com" in text                   # generic Acme address kept
        assert "​" not in text and "͏" not in text
    assert rep.redactions_by_kind.get("portal_token") == 1
    assert rep.redactions_by_kind.get("phone") == 1


def test_telegram_pairs_use_reply_links_and_anonymous_admin(kb):
    db, _ = kb
    con = sqlite3.connect(db)
    (body,) = con.execute("SELECT body FROM docs WHERE kind = 'telegram'").fetchone()
    assert "WAVE/00001 cannot validate" in body
    assert "Send the validation error please" in body and "Try again now." in body
    assert "topic reply" not in body and "no answer for me" not in body


def test_steering_dedupe_keeps_triage_copy(kb):
    db, _ = kb
    con = sqlite3.connect(db)
    (body,) = con.execute("SELECT body FROM docs WHERE doc_id = 'steering:telegram-triage'").fetchone()
    assert "Rakam el batch" in body
    assert con.execute("SELECT count(*) FROM docs WHERE doc_id = 'steering:auditor'").fetchone()[0] == 1


def test_memory_index_file_skipped(kb):
    db, _ = kb
    con = sqlite3.connect(db)
    assert [r[0] for r in con.execute("SELECT doc_id FROM docs WHERE kind = 'memory'")] == ["memory:no-em-dash"]


def test_rebuild_replaces_db(kb, tmp_path):
    db, _ = kb
    rep = build(_sources(tmp_path / "src2"), db)
    assert rep.docs == 13
    assert not db.with_suffix(".building").exists()


def test_search_runbook_first_for_signature(kb):
    db, _ = kb
    hits = kb_search.search("A receipt transfer cannot be validated without a Purchase Order reference", db_path=db)
    assert hits[0].doc_id == "runbook:stray-in-line" and hits[0].status == "confirmed"
    assert "[" in hits[0].snippet


def test_search_kinds_filter_and_refs(kb):
    db, _ = kb
    hits = kb_search.search("WAVE/00001", db_path=db, kinds=["telegram"])
    assert [h.kind for h in hits] == ["telegram"]
    assert kb_search.search("WAVE/00001", db_path=db, kinds=["memory"]) == []


@pytest.mark.parametrize("q", ['"', "AND OR NOT", 'col:value NEAR(a b) *', "(", "", "؟؟"])
def test_search_never_raises_on_odd_input(kb, q):
    db, _ = kb
    assert isinstance(kb_search.search(q, db_path=db), list)


def test_runbook_entries_api(kb):
    db, _ = kb
    rb = kb_search.runbook_entries(db)
    assert rb["stray-in-line"]["status"] == "confirmed"
    assert set(rb["stray-in-line"]) >= {"title", "signature", "status", "confirmed_date", "tags"}


def test_search_is_read_only(kb):
    db, _ = kb
    con = kb_search._connect(db)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM docs")
    con.close()


def test_cli_build_and_search(tmp_path, capsys):
    src = _sources(tmp_path / "src")
    db = tmp_path / "out" / "kb.db"
    root = tmp_path / "src"
    assert kb_build.main(["--db", str(db), "--handover", str(root), "--principal-tg-id", "1001"]) == 0
    assert db.exists()
    assert kb_search.main(["Purchase Order reference", "--db", str(db), "--k", "2"]) == 0
    assert "runbook" in capsys.readouterr().out
    assert src.runbook.exists()


def test_missing_db_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        kb_search.search("receipt validate", db_path=tmp_path / "nope.db")


# ---------------------------------------------------------------- allow-list + MCP tool

def _handover(tmp: Path) -> Path:
    """Synthetic handover layout with an allow-listed export and a second, not allow-listed one."""
    root = tmp / "handover"
    (root / "triage").mkdir(parents=True)
    (root / "triage" / "ODOO-RUNBOOK.md").write_text((FIXTURES / "runbook_small.md").read_text(encoding="utf-8"),
                                                    encoding="utf-8")
    for name, chat in (("odoo-x-client", "Test Group"), ("saudi-setup", "Other Group")):
        d = root / "telegram" / name
        d.mkdir(parents=True)
        d.joinpath("result.json").write_text(json.dumps({"name": chat, "id": 500, "messages": [
            {"id": 1, "type": "message", "from_id": "user2001", "text": f"question in {chat}"},
            {"id": 2, "type": "message", "from_id": "user1001", "text": "answer", "reply_to_message_id": 1},
        ]}), encoding="utf-8")
    return root


def test_handover_indexes_only_the_allow_listed_export_by_default(tmp_path):
    root = _handover(tmp_path)
    rep = build(kb_build.sources_from_handover(root, ["1001"]), tmp_path / "kb.db")
    assert [t["chat"] for t in rep.telegram] == ["Test Group"]
    con = sqlite3.connect(tmp_path / "kb.db")
    assert con.execute("SELECT count(*) FROM docs WHERE body LIKE '%Other Group%'").fetchone()[0] == 0
    extra = [root / "telegram" / "saudi-setup" / "result.json"]
    rep = build(kb_build.sources_from_handover(root, ["1001"], extra), tmp_path / "kb2.db")
    assert [t["chat"] for t in rep.telegram] == ["Other Group"]


def test_config_exports_need_read_true(tmp_path):
    root = _handover(tmp_path)
    exports = [str(p) for p in sorted((root / "telegram").glob("*/result.json"))]
    cfg = {"kb_sources": {"telegram_exports": exports},
           "chats": [{"name": "Test Group", "read": True}, {"name": "Other Group", "read": False}]}
    src = kb_build.sources_from_config(cfg)
    assert [p.parent.name for p in src.telegram_exports] == ["odoo-x-client"]


def test_mcp_kb_search_tool(kb, tmp_path):
    from twin_agent.kb.mcp_kb_search import TOOL_SCHEMA, kb_search_tool
    db, _ = kb
    out = kb_search_tool({"query": "Purchase Order reference", "k": 99, "kinds": ["runbook", "bogus"]}, db_path=db)
    assert out["hits"] and all(h["kind"] == "runbook" for h in out["hits"]) and len(out["hits"]) <= 20
    assert set(out["hits"][0]) == {"kind", "doc_id", "title", "snippet", "status", "score"}
    assert kb_search_tool({"query": "x", "k": "nope"}, db_path=db)["hits"] == []
    assert "error" in kb_search_tool({"query": "receipt validate"}, db_path=tmp_path / "none.db")
    assert TOOL_SCHEMA["name"] == "search"
