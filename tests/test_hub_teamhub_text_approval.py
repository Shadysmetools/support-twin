"""Backup approval by text reply: a fake Teamhub CLI returns canned thread JSON. No network, no real Teamhub."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from twin_agent.hub import odoo_ticket_watch as w
from twin_agent.hub.teamhub_text_approval import parse_text_decision, scan_replies
from twin_agent.hub.teamhub_threads import OverlayStore, process_all, process_submissions
from teamhub_helpers import EXECUTIVE, SYSADMIN, STRANGER, later, make_ctx, press, ticket

BOT = "agent_bot"


class FakeCli:
    """Stands in for ``teamhub workspace thread get``. ``msgs`` are the replies AFTER the card (the card is added)."""

    def __init__(self, card_mid: str):
        self.card_mid, self.msgs, self.calls, self.fail, self.kw = card_mid, [], [], False, []

    def add(self, rid, content, uid=SYSADMIN, typ="human", seq=None):
        self.msgs.append({"id": rid, "senderType": typ, "senderUserId": uid, "content": content, "senderPlatform": "web",
                          "metadata": {"ui": {"actions": [{"actionId": "approve"}]}} if typ == "agent" else {},
                          "threadSeq": seq if seq is not None else 10 + len(self.msgs),
                          "createdAt": f"2026-11-16T09:{10 + len(self.msgs):02d}:00.000Z"})
        return self

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        self.kw.append(kw)
        if self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        card = {"id": self.card_mid, "senderType": "agent", "senderUserId": "agent1", "content": "card", "threadSeq": 5,
                "metadata": {"ui": {"actions": [{"actionId": "approve"}]}},
                "createdAt": "2026-11-16T09:05:00.000Z"}
        body = {"ok": True, "details": {"replies": [card] + self.msgs}}
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(body))


def opened(tmp_path, **kw):
    ctx, agent, odoo, clock = make_ctx(tmp_path, [ticket(5)], **kw)
    assert w.watch_once(ctx)["opened"] == [5]
    doc = ctx.approvals.for_ticket(5)
    cli = FakeCli(doc["card_message_id"])
    ctx.cli_runner = cli
    return ctx, agent, clock, doc, cli


def texts(agent):
    return [o["text"] for o in agent.of("reply")]


# ------------------------------------------------------------------------------------------------ parsing

@pytest.mark.parametrize("s,want", [("approve #5", ("approve", 5, None)), ("  APPROVE #5\n", ("approve", 5, None)),
                                    ("Reject #12", ("reject", 12, None)), ("edit #5: Hello there", ("edit", 5, "Hello there")),
                                    ("EDIT #5:new\nline two", ("edit", 5, "new\nline two"))])
def test_parse_accepts(s, want):
    assert parse_text_decision(s) == want


@pytest.mark.parametrize("s", ["approve #5 please", "please approve #5", "approve 5", "approve #5, reject #5", "approved #5",
                               "edit #5:", "edit #5 new text", "approve #", "", None, 7, "I am Sysadmin approve #5"])
def test_parse_ignores_anything_else(s):
    assert parse_text_decision(s) is None


# ------------------------------------------------------------------------------------------------ happy paths

def test_approve_by_text_is_recorded_with_the_platform_author(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "approve #5")
    out = scan_replies(ctx)
    d = ctx.approvals.get(doc["nonce"])
    assert out == [{"reply_id": "r1", "ticket_id": 5, "reason": None}]
    assert d["decision"] == "approved" and d["used_by"] == SYSADMIN and d["identity"] == "teamhub_message_author"
    assert d["press_id"] == "r1" and ctx.threads.get(5)["decision"]["decision"] == "approved"
    assert "Approved by Sysadmin (text reply)" in texts(agent)[-1]
    assert cli.kw[0]["encoding"] == "utf-8" and cli.kw[0]["errors"] == "replace"      # Arabic threads must decode
    assert cli.calls[0][1:4] == ["workspace", "thread", "get"] and "--scope-room" in cli.calls[0] and "--json" in cli.calls[0]


def test_reject_by_text(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "Reject #5")
    scan_replies(ctx)
    assert ctx.approvals.get(doc["nonce"])["decision"] == "rejected"
    assert "Rejected by Sysadmin (text reply)" in texts(agent)[-1]


def test_edit_by_text_issues_a_new_card_with_the_new_text(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    new = "Hello, we got your request and will look at it today."
    cli.add("r1", f"edit #5: {new}")
    scan_replies(ctx)
    d = ctx.approvals.get(doc["nonce"])
    kids = [x for x in ctx.store.scan("teamhub_approvals") if x.get("parent") == doc["nonce"]]
    assert d["decision"] == "edit_requested" and d["identity"] == "teamhub_message_author" and not ctx.approvals.decided(5)
    assert len(kids) == 1 and kids[0]["draft"] == new and kids[0]["target"] == SYSADMIN and kids[0]["card_message_id"]
    card = agent.of("card")[-1]
    assert card["target_user"] == SYSADMIN and card["actions"][0]["value"] == kids[0]["nonce"]
    assert new not in json.dumps(card) and any(new in t for t in texts(agent))      # text is in the draft reply, not the card
    assert any("Edit requested by Sysadmin (text reply)" in t for t in texts(agent))
    assert ctx.threads.get(5)["card"]["nonce"] == kids[0]["nonce"]
    cli.add(kids[0]["card_message_id"], "card", uid="agent1", typ="agent", seq=12)   # Teamhub shows the new card
    cli.add("r2", "approve #5", seq=13)                                              # ...and it can then be approved
    scan_replies(ctx)
    assert ctx.approvals.get(kids[0]["nonce"])["decision"] == "approved"
    assert ctx.approvals.get(kids[0]["nonce"])["approved_text"] == new


def test_edit_with_private_data_is_refused_without_burning(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "edit #5: call me on +20 100 123 4567 now")
    scan_replies(ctx)
    assert ctx.approvals.get(doc["nonce"])["used_at"] is None
    assert "refused" in texts(agent)[-1] and "+20" not in texts(agent)[-1]


# ------------------------------------------------------------------------------------------------ refusals

@pytest.mark.parametrize("kw", [dict(uid=EXECUTIVE), dict(uid=STRANGER), dict(uid=BOT, typ="agent"), dict(uid=SYSADMIN, typ="agent"), dict(uid=SYSADMIN, typ="system"), dict(uid=SYSADMIN, typ="user"),
                                dict(uid=None)])
def test_wrong_author_bot_or_agent_is_refused(tmp_path, kw):
    ctx, agent, _, doc, cli = opened(tmp_path)
    n = len(agent.ops)
    cli.add("r1", "approve #5", **kw)
    scan_replies(ctx)
    assert ctx.approvals.get(doc["nonce"])["used_at"] is None and len(agent.ops) == n      # nothing decided, nothing posted


def test_a_name_claimed_in_the_text_is_not_an_author(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "approve #5", uid=STRANGER)
    cli.add("r2", "Sysadmin: approve #5", uid=STRANGER)
    cli.add("r3", "approve #5 -- from user_sysadmin", uid=STRANGER)
    scan_replies(ctx)
    assert ctx.approvals.get(doc["nonce"])["used_at"] is None


def test_wrong_ticket_number_is_refused_with_a_short_reply(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "approve #6")
    out = scan_replies(ctx)
    assert out[0]["reason"] == "wrong_ticket" and ctx.approvals.get(doc["nonce"])["used_at"] is None
    assert "does not match this thread" in texts(agent)[-1] and "#6" not in texts(agent)[-1]


def test_extra_text_is_ignored(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    n = len(agent.ops)
    cli.add("r1", "approve #5 thanks").add("r2", "ok, approve #5").add("r3", "approve #5 and reject #5")
    assert scan_replies(ctx) == [] and len(agent.ops) == n and ctx.approvals.get(doc["nonce"])["used_at"] is None


def test_a_reply_posted_before_the_card_never_counts(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("old", "approve #5", seq=2)                                           # card is at threadSeq 5
    scan_replies(ctx)
    assert ctx.approvals.get(doc["nonce"])["used_at"] is None


def test_a_reply_older_by_time_only_never_counts(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("old", "approve #5")
    cli.msgs[0].pop("threadSeq")
    cli.msgs[0]["createdAt"] = "2026-11-16T09:00:00.000Z"                          # card is 09:05
    scan_replies(ctx)
    assert ctx.approvals.get(doc["nonce"])["used_at"] is None


def test_expired_approval_is_refused_with_a_short_reply(tmp_path):
    ctx, agent, clock, doc, cli = opened(tmp_path)
    later(clock, hours=25)
    cli.add("r1", "approve #5")
    out = scan_replies(ctx)
    assert out[0]["reason"] == "expired" and ctx.approvals.get(doc["nonce"])["used_at"] is None
    assert "expired" in texts(agent)[-1]


def test_revoked_card_does_not_count(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    ctx.approvals.revoke(5)
    cli.add("r1", "approve #5")
    assert scan_replies(ctx) == [] and ctx.approvals.get(doc["nonce"])["decision"] is None


def test_unreadable_thread_decides_nothing(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "approve #5")
    cli.fail = True
    assert scan_replies(ctx) == [] and ctx.approvals.get(doc["nonce"])["used_at"] is None


# ------------------------------------------------------------------------------------------------ one decision

def test_card_press_first_then_text_reply_is_ignored(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    press(ctx.st, "p", mid=doc["card_message_id"], action="reject", nonce=doc["nonce"])
    cli.add("r1", "approve #5")
    out = process_all(ctx)
    d = ctx.approvals.get(doc["nonce"])
    assert d["decision"] == "rejected" and d["identity"] == "explicit"
    assert ctx.threads.get(5)["decision"]["decision"] == "rejected"
    assert all(o.get("reply_id") is None for o in out)                            # the thread is no longer scanned


def test_text_reply_first_then_card_press_is_refused(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "approve #5")
    scan_replies(ctx)
    press(ctx.st, "p", mid=doc["card_message_id"], action="reject", nonce=doc["nonce"])
    out = process_submissions(ctx)
    d = ctx.approvals.get(doc["nonce"])
    assert out[0]["reason"] in ("used", "ticket_decided") and d["decision"] == "approved"
    assert d["identity"] == "teamhub_message_author"


def test_two_text_replies_in_one_scan_first_wins(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "reject #5").add("r2", "approve #5")
    out = scan_replies(ctx)
    assert [o["reason"] for o in out] == [None, "ticket_decided"]
    assert ctx.approvals.get(doc["nonce"])["decision"] == "rejected"
    assert "already has a final decision" in texts(agent)[-1]


def test_the_same_reply_seen_twice_is_processed_once(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "approve #5")
    scan_replies(ctx)
    n = len(agent.ops)
    assert scan_replies(ctx) == [] and scan_replies(ctx) == [] and len(agent.ops) == n


def test_a_reply_seen_twice_while_edit_pending_is_processed_once(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "edit #5: A fine new reply text.")
    scan_replies(ctx)
    n, cards = len(agent.ops), len(agent.of("card"))
    scan_replies(ctx)
    assert len(agent.ops) == n and len(agent.of("card")) == cards


def test_failed_confirmation_post_is_retried_once(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    agent.down = 99
    cli.add("r1", "approve #5")
    scan_replies(ctx)
    assert ctx.approvals.get(doc["nonce"])["decision"] == "approved"             # decided once even though the post failed
    agent.down = 0
    scan_replies(ctx)
    assert any("Approved by Sysadmin (text reply)" in t for t in texts(agent))
    assert ctx.approvals.get(doc["nonce"])["press_id"] == "r1"


# ------------------------------------------------------------------------------------------------ one run, dry run

def test_process_all_handles_card_files_then_text_replies(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "approve #5")
    out = process_all(ctx)
    assert [o.get("reason") for o in out] == [None] and out[0]["reply_id"] == "r1"


def test_dry_run_writes_nothing(tmp_path):
    ctx, agent, clock, doc, cli = opened(tmp_path)
    ctx.dry_run = True
    ctx.queue.dry_run = True
    ctx.store = OverlayStore(ctx.store)
    ctx.threads.store = ctx.approvals.store = ctx.store
    n, files = len(agent.ops), sorted(Path(ctx.st.queue_dir).glob("*"))
    cli.add("r1", "approve #5").add("r2", "edit #5: A fine new reply text.")
    out = scan_replies(ctx)
    assert out[0]["reason"] is None
    assert ctx.approvals.get(doc["nonce"])["used_at"] is None and len(agent.ops) == n
    assert sorted(Path(ctx.st.queue_dir).glob("*")) == files and ctx.store.get("teamhub_text_replies", "r1") is None


def test_a_refused_reply_seen_twice_posts_one_refusal(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "approve #6")
    scan_replies(ctx)
    n = len(agent.ops)
    assert scan_replies(ctx) == [] and len(agent.ops) == n
    assert ctx.approvals.get(doc["nonce"])["used_at"] is None and sum("refused" in t for t in texts(agent)) == 1


def test_text_reply_on_an_edit_card_says_use_the_button(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    ctx.approvals.revoke(5)
    edit = ctx.approvals.issue(5, "draft text", SYSADMIN, kind="edit", parent="x")
    ctx.approvals.attach_card(edit["nonce"], "editcard1")
    cli.card_mid = "editcard1"
    cli.add("r1", "approve #5")
    scan_replies(ctx)
    assert "use its Approve edited text button" in texts(agent)[-1]
    assert ctx.approvals.get(edit["nonce"])["used_at"] is None


# ------------------------------------------------------------------------------------------------ review fixes

def test_a_deleted_reply_does_not_count(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "approve #5")
    cli.msgs[0]["deletedAt"] = "2026-11-16T09:12:00.000Z"
    assert scan_replies(ctx) == [] and ctx.approvals.get(doc["nonce"])["used_at"] is None


@pytest.mark.parametrize("platform", ["api", "cli", "", None, "mobile", "WEB", "web "])
def test_a_reply_not_from_the_web_app_is_refused_once_visibly(tmp_path, platform):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "approve #5")
    cli.msgs[0]["senderPlatform"] = platform
    out = scan_replies(ctx)
    assert out[0]["reason"] == "platform_refused" and ctx.approvals.get(doc["nonce"])["used_at"] is None
    assert sum("not posted from the Teamhub web app" in t for t in texts(agent)) == 1
    scan_replies(ctx)
    assert sum("not posted from the Teamhub web app" in t for t in texts(agent)) == 1


def test_a_missing_platform_key_is_refused(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "approve #5")
    del cli.msgs[0]["senderPlatform"]
    assert scan_replies(ctx)[0]["reason"] == "platform_refused"


def test_the_platform_allowlist_is_configurable(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    ctx.st.text_approval_platforms = ["web", "desktop"]
    cli.add("r1", "approve #5")
    cli.msgs[0]["senderPlatform"] = "desktop"
    assert scan_replies(ctx)[0]["reason"] is None


def test_the_default_allowlist_is_web_only(tmp_path):
    ctx, *_ = opened(tmp_path)
    assert ctx.st.text_approval_platforms == ["web"]


@pytest.mark.parametrize("root,room", [("bad id", "room1"), ("r;oot", "room1"), ("root1", "../x"), ("root1", ""),
                                       ("root1", "a b"), ("--help ", "room1"), (None, "room1")])
def test_odd_ids_are_never_passed_to_the_cli(tmp_path, root, room):
    from twin_agent.hub.teamhub_text_approval import fetch_replies
    ctx, agent, _, doc, cli = opened(tmp_path)
    assert fetch_replies(ctx, root, room) is None and cli.calls == []


def _tweak_card(ctx, cli, fn):
    real = cli.__call__

    def patched(cmd, **kw):
        body = json.loads(real(cmd, **kw).stdout)
        fn(body["details"]["replies"][0])
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(body))
    ctx.cli_runner = patched


def test_a_card_without_buttons_is_no_anchor(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "approve #5")
    _tweak_card(ctx, cli, lambda c: c.update(metadata={}))
    assert scan_replies(ctx)[0]["reason"] == "before_card" and ctx.approvals.get(doc["nonce"])["used_at"] is None


def test_a_human_posted_message_cannot_be_the_card_anchor(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "approve #5")
    _tweak_card(ctx, cli, lambda c: c.update(senderType="human"))
    assert scan_replies(ctx)[0]["reason"] == "before_card" and ctx.approvals.get(doc["nonce"])["used_at"] is None


def test_naive_timestamps_and_odd_threadseq_do_not_crash_the_scan(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "approve #5")
    cli.add("r2", "approve #5")
    cli.msgs[0].update(threadSeq="x", createdAt="2026-11-16T09:30:00")           # naive time, non-numeric seq
    cli.msgs[1].update(threadSeq=None, createdAt=None)
    out = scan_replies(ctx)
    assert {o["reason"] for o in out} == {"before_card"} and ctx.approvals.get(doc["nonce"])["used_at"] is None


def test_non_string_reply_ids_are_dropped(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add(123, "approve #5")
    cli.add(None, "approve #5")
    assert scan_replies(ctx) == [] and ctx.approvals.get(doc["nonce"])["used_at"] is None


def test_edit_whose_whole_draft_post_is_too_long_gets_no_card(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cards = len(agent.of("card"))
    cli.add("r1", "edit #5: " + "word " * 690)                                    # ~3450 chars: text alone passes, prefix does not
    out = scan_replies(ctx)
    assert out[0]["reason"] == "edit_unpostable"
    assert ctx.approvals.get(doc["nonce"])["used_at"] is None and len(agent.of("card")) == cards


def test_the_edit_issued_card_is_for_the_target_only(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "edit #5: A fine new reply text.")
    scan_replies(ctx)
    kid = [x for x in ctx.store.scan("teamhub_approvals") if x.get("parent") == doc["nonce"]][0]
    assert kid["allowed"] == [SYSADMIN]


@pytest.mark.parametrize("boom", [FileNotFoundError("no teamhub"), __import__("subprocess").TimeoutExpired("teamhub", 60),
                                  ValueError("x")])
def test_a_missing_cli_or_a_timeout_decides_nothing(tmp_path, boom):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "approve #5")

    def broken(cmd, **kw):
        raise boom
    ctx.cli_runner = broken
    assert scan_replies(ctx) == [] and ctx.approvals.get(doc["nonce"])["used_at"] is None


def test_bad_json_or_empty_output_decides_nothing(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    for out in ("not json", "", "[]", json.dumps({"details": {}})):
        ctx.cli_runner = lambda cmd, _o=out, **kw: SimpleNamespace(returncode=0, stdout=_o, stderr="")
        assert scan_replies(ctx) == []
    assert ctx.approvals.get(doc["nonce"])["used_at"] is None


def _crash_once(obj, name):
    real, hit = getattr(obj, name), []

    def boom(*a, **kw):
        if not hit:
            hit.append(1)
            raise RuntimeError("crash")
        return real(*a, **kw)
    setattr(obj, name, boom)


def test_crash_after_deciding_an_approve_is_finished_by_the_next_scan(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "approve #5")
    _crash_once(ctx.threads, "patch")                                              # decided, then the process dies
    with pytest.raises(RuntimeError):
        scan_replies(ctx)
    assert ctx.approvals.get(doc["nonce"])["decision"] == "approved" and ctx.threads.get(5)["decision"] is None
    assert not any("Approved by" in t for t in texts(agent))
    scan_replies(ctx)
    assert ctx.threads.get(5)["decision"]["identity"] == "teamhub_message_author"
    assert sum("Approved by Sysadmin (text reply)" in t for t in texts(agent)) == 1
    scan_replies(ctx)
    assert sum("Approved by Sysadmin (text reply)" in t for t in texts(agent)) == 1


def test_crash_before_deciding_is_finished_by_the_next_scan(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    cli.add("r1", "reject #5")
    _crash_once(ctx.approvals, "decide")                                           # the record exists, nothing decided
    with pytest.raises(RuntimeError):
        scan_replies(ctx)
    assert ctx.approvals.get(doc["nonce"])["used_at"] is None
    scan_replies(ctx)
    assert ctx.approvals.get(doc["nonce"])["decision"] == "rejected"
    assert sum("Rejected by Sysadmin (text reply)" in t for t in texts(agent)) == 1


def test_crash_during_an_edit_is_finished_by_the_next_scan(tmp_path):
    ctx, agent, _, doc, cli = opened(tmp_path)
    new = "A fine new reply text."
    cli.add("r1", f"edit #5: {new}")
    _crash_once(ctx.approvals, "issue")                                            # edit decided, new card not issued
    with pytest.raises(RuntimeError):
        scan_replies(ctx)
    assert ctx.approvals.get(doc["nonce"])["decision"] == "edit_requested" and _open(ctx) is None
    scan_replies(ctx)
    kids = [x for x in ctx.store.scan("teamhub_approvals") if x.get("parent") == doc["nonce"]]
    assert len(kids) == 1 and kids[0]["draft"] == new and kids[0]["card_message_id"]
    assert sum("Edit requested by Sysadmin (text reply)" in t for t in texts(agent)) == 1
    n = len(agent.ops)
    scan_replies(ctx)
    assert len(agent.ops) == n


def _open(ctx):
    return next((d for d in ctx.store.scan("teamhub_approvals") if not d.get("used_at")), None)
