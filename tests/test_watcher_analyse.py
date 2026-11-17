"""Sysadmin's side of the Watcher: fingerprint, match score, judge hook, analyse, digest, offline bootstrap."""
from __future__ import annotations

import json

import pytest

from twin_agent.testing.fakes import FakeClaude
from twin_agent.watcher import analyse, bootstrap, bundle, digest, fingerprint, similarity


def msg(text, requester="requester-aaaa", minutes=5.0, hour=11, ts="2026-11-01T09:00:00+00:00"):
    return {"kind": "own_message", "ts": ts, "text": text, "requester": requester, "reply_minutes": minutes,
            "hour": hour}


CASES = [
    {"request_id": "REQ-0001", "requester": "requester-aaaa", "records": [
        msg("ab3atly screenshot please"), {"kind": "tap", "ts": "2026-11-01T09:01:00+00:00", "action": "fx"},
        msg("tamam, fixed. try now please", minutes=20),
        {"kind": "edit", "ts": "2026-11-01T09:05:00+00:00", "draft": "Dear colleague, the issue has been resolved.",
         "final": "tamam, fixed. try now please", "via_agent": False, "unchanged": False, "similarity": 0.1,
         "draft_words": 7, "final_words": 5, "added": ["tamam"], "removed": ["Dear"]}]},
    {"request_id": "REQ-0002", "requester": "requester-bbbb", "records": [
        msg("Please send me the reference", requester="requester-bbbb", minutes=40, hour=15),
        msg("I will open a ticket for the dev team", requester="requester-bbbb", minutes=60, hour=16)]},
]


def test_language_and_moves():
    assert fingerprint.lang_of("tamam ya basha, 3ayez el screenshot") == "franco"
    assert fingerprint.lang_of("Please send the reference") == "en"
    assert fingerprint.lang_of("تمام هشوف الموضوع") == "ar"
    assert fingerprint.move_of("ab3atly screenshot") == "ask_evidence"
    assert fingerprint.move_of("I will open a ticket") == "escalate"


def test_build_fingerprint():
    fp = fingerprint.build_fingerprint(CASES)
    assert fp["cases"] == 2 and fp["messages"] == 4
    assert set(fp["per_requester"]) == {"requester-aaaa", "requester-bbbb"}
    assert fp["diagnosis"]["first_move"] == {"ask_evidence": 2}
    assert fp["diagnosis"]["escalate_rate"] == 0.5
    assert fp["edits"]["count"] == 1 and fp["edits"]["unchanged_rate"] == 0.0
    assert fp["overall"]["median_reply_min"] == 30.0
    assert json.dumps(fp)                                                    # JSON-safe


def test_match_score_prefers_his_style():
    fp = fingerprint.build_fingerprint(CASES)
    refs = ["tamam, fixed. try now please"]
    his = similarity.match_score("tamam, done. try now please", refs, fp)
    stiff = similarity.match_score("Dear colleague, we are pleased to inform you that the issue has been "
                                   "resolved by our technical team and you may now proceed 🙂", refs, fp)
    assert 0 <= stiff["score"] < his["score"] <= 100
    assert his["parts"]["language"] == 1.0 and stiff["parts"]["style"] == 0.0


def test_judge_hook_stub_fake_and_fail_closed(tmp_path):
    assert similarity.judge("x", ["y"])["status"] == "stub"
    fc = FakeClaude()
    fc.responses["fingerprint_judge"] = {"score": 72, "why": "close", "change": "shorter"}
    res = similarity.judge("draft", ["ref"], runner=fc, cwd=tmp_path)
    assert res == {"status": "ok", "score": 72.0, "why": "close", "change": "shorter"}
    assert "untrusted" in fc.calls[0]["prompt"].lower() and fc.calls[0]["kind"] == "fingerprint_judge"
    fc.responses["fingerprint_judge"] = {"score": 900}
    assert similarity.judge("d", ["r"], runner=fc, cwd=tmp_path)["status"] == "unavailable"
    fc.fail_kinds.add("fingerprint_judge")
    assert similarity.judge("d", ["r"], runner=fc, cwd=tmp_path)["status"] == "unavailable"


def _write_bundles(d, cases):
    (d / "cases").mkdir(parents=True)
    for c in cases:
        head = {k: c[k] for k in ("request_id", "requester")}
        (d / "cases" / f"{c['request_id']}.jsonl").write_text(
            "\n".join(json.dumps(x) for x in [{"case": head}, *c["records"]]) + "\n", encoding="utf-8")


def test_analyse_writes_fingerprint_addendum_and_digest(tmp_path):
    _write_bundles(tmp_path / "b", CASES)
    (tmp_path / "b" / "cases" / "BROKEN.jsonl").write_text("{not json", encoding="utf-8")
    add = tmp_path / "persona" / "ADDENDUM.md"
    add.parent.mkdir()
    add.write_text("# Persona\n\nHand-written rules stay.\n", encoding="utf-8")
    sender = digest.FakeDigestSender()
    rep = analyse.analyse(tmp_path / "b", tmp_path / "out", addendum=add, sender=sender)
    assert rep.cases == 2 and rep.new_cases == 2 and len(rep.scores) == 1
    assert (tmp_path / "out" / "FINGERPRINT.md").read_text(encoding="utf-8").startswith("# Principal's work fingerprint")
    text = add.read_text(encoding="utf-8")
    assert "Hand-written rules stay." in text and text.count(analyse.BEGIN) == 1
    analyse.analyse(tmp_path / "b", tmp_path / "out", addendum=add, sender=sender)
    assert add.read_text(encoding="utf-8").count(analyse.BEGIN) == 1            # updated in place
    assert len(sender.sent) == 2 and "Fingerprint match" in sender.sent[0]
    assert "0 case(s) in" in sender.sent[1] and "Change since last digest: +0.0" in sender.sent[1]


def test_digest_senders_refuse_unsafe(tmp_path):
    with pytest.raises(digest.DigestRefused):
        digest.FakeDigestSender().send("call +20 100 000 0000")
    with pytest.raises(digest.DigestRefused):
        digest.BotDigestSender({"run_mode": "dry_run"}, has_secret=lambda n: True)
    cfg = {"run_mode": "live", "watcher": {"digest": {"sysadmin_tg_user_id": 42}}}
    with pytest.raises(digest.DigestRefused):
        digest.BotDigestSender(cfg, has_secret=lambda n: False)
    posts = []

    class R:
        status_code = 200
    s = digest.BotDigestSender(cfg, has_secret=lambda n: True, get_secret=lambda n: "TOKEN",
                               post=lambda url, **kw: posts.append((url, kw)) or R())
    assert s.send("hello Sysadmin") and posts[0][1]["json"]["chat_id"] == 42 and "TOKEN" not in repr(s)


def _handover(tmp_path):
    h = tmp_path / "handover"
    ex = h / "telegram" / "odoo-x-client"
    ex.mkdir(parents=True)
    msgs = []
    for i in range(12):
        req = f"user{2001 + i % 4}"
        msgs.append({"id": 2 * i + 1, "type": "message", "from": f"Zedd{i % 4} Example", "from_id": req,
                     "date_unixtime": str(1761987600 + 3600 * i), "text": f"Zedd{i % 4} here, WAVE/0000{i} stuck"})
        msgs.append({"id": 2 * i + 2, "type": "message", "from": "Principal Test", "from_id": "user1001",
                     "reply_to_message_id": 2 * i + 1, "date_unixtime": str(1761987600 + 3600 * i + 600),
                     "text": [f"tamam ya Zedd{i % 4}, ", {"type": "bold", "text": "try now please"}]})
    (ex / "result.json").write_text(json.dumps({"name": "Odoo x Globex", "id": 1234567, "messages": msgs}),
                                    encoding="utf-8")
    sd = h / "telegram" / "saudi-setup"
    sd.mkdir(parents=True)
    (sd / "result.json").write_text(json.dumps({"name": "S", "id": 9, "messages": [
        {"id": 1, "type": "message", "from_id": "user1001", "text": "SAUDI ONLY TEXT", "date_unixtime": "1"}]}))
    em = h / "email"
    em.mkdir()
    (em / "01.txt").write_text('Subject: Re: x\nFrom: "Principal Test" <principal@example.org>\nTo: "Kay Sample" '
                               '<kay@example.org>\nDate: Sun, 1 Nov 2026 16:12:06 +0200\n--- BODY ---\n'
                               'Hi Kay, please try now and send me the reference.\n\nOn Sun someone wrote:' '\n> old',
                               encoding="utf-8")
    return h


def test_bootstrap_private_and_deidentified_docs(tmp_path):
    h = _handover(tmp_path)
    before = {p: p.stat().st_mtime for p in h.rglob("*")}
    res = bootstrap.run(h, {"user1001"}, principal_email="principal@example.org", out=tmp_path / "data",
                        docs=tmp_path / "docs" / "FP.md", salt=b"s")
    assert res["cases"] >= 4 and res["messages"] == 13 and res["docs_written"] and res["leaks"] == 0
    assert {p: p.stat().st_mtime for p in h.rglob("*")} == before                # handover untouched
    private = (tmp_path / "data" / "cases.jsonl").read_text(encoding="utf-8")
    assert "SAUDI" not in private and "Zedd" not in private and "Kay Sample" not in private
    assert "user2001" not in private and "try now" in private
    docs = (tmp_path / "docs" / "FP.md").read_text(encoding="utf-8")
    assert "de-identified" in docs and "R1" in docs
    assert bootstrap.leak_hits(docs, bootstrap.name_parts({"Zedd0 Example", "Kay Sample"}), {"user2001"}) == []


def test_leak_check_blocks_docs(tmp_path, monkeypatch):
    h = _handover(tmp_path)
    monkeypatch.setattr(bootstrap, "render_md", lambda *a, **k: "Example said hi")
    res = bootstrap.run(h, {"user1001"}, out=tmp_path / "data", docs=tmp_path / "docs" / "FP.md", salt=b"s")
    assert res["leaks"] > 0 and not res["docs_written"] and not (tmp_path / "docs" / "FP.md").exists()


def test_public_render_hides_name_like_phrases():
    fp = fingerprint.build_fingerprint(CASES)
    fp["phrase_bank"] = [{"phrase": "try now please", "count": 5, "requesters": 3},
                         {"phrase": "ya zedd", "count": 9, "requesters": 4},
                         {"phrase": "rare phrase", "count": 3, "requesters": 1}]
    out = analyse.render_md(fp, public=True, blocked={"zedd"})
    assert "try now please" in out and "zedd" not in out and "rare phrase" not in out
    assert "requester-aaaa" not in out


def test_bundle_round_trip_feeds_analyse(tmp_path):
    p = bundle.Pseudonymiser(b"s", [])
    rep = bundle.export_cases([{**CASES[0], "lane": "L3", "tier": "T1", "company": "EG", "channel": "telegram_group",
                                "state": "NEW"}], tmp_path / "o", p)
    assert rep.cases == 1
    cases = analyse.load_bundles(tmp_path / "o")
    assert cases[0]["request_id"] == "REQ-0001" and cases[0]["requester"].startswith("requester-")
